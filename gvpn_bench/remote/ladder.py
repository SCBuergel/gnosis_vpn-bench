"""The 1 → 2 → 3 concurrent-client ladder.

For each rung, the first N clients download the origin's file at the same
UTC instant. Each transfer has the shared deadline; the next rung starts
``gap_s`` after the last transfer of the previous rung ended. The next rung
is scheduled *before* anything else is fetched or computed so the gap holds.

Only clients that connected to the target take part. A rung needing more
clients than connected is ``failed`` with the first missing client's
connect status (often ``http_404_register``: the exit has fewer slots than
the run has clients), and later rungs are ``not_run``.

Before every rung each client must still be connected to the target exit.
A client that dropped is reconnected once (``reconnect``); if that fails,
the rung is ``failed`` and later rungs are ``not_run``. Nothing is measured
through another exit or without the tunnel.

Each attempted rung records ``connection_check``: how many of the run's
clients were connected to the target right before it, and the exit's own
slot counts from the freshest probe a connected client holds.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, TypeVar

from ..host import Host, read_file, write_file
from ..profiles import Profile, Target
from ..runs import LadderSpec, Run
from .analysis import NoOverlapError, summarize, system_summary, timeseries
from .connect import vpn_for
from .provision import REMOTE_DIR

log = logging.getLogger("gnosis_speedtest")

T = TypeVar("T")
FIRST_RUNG_LEAD_S = 8.0
MIN_LEAD_S = 2.0
COLLECT_GRACE_S = 25.0


def parallel(function: Callable[[Host], T], hosts: list[Host]) -> dict[str, T]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        return dict(zip([h.name for h in hosts], pool.map(function, hosts), strict=True))


def still_connected(host: Host, profile: Profile, target: Target) -> bool:
    status = vpn_for(host, profile).try_status()
    if status is None:
        return False
    destination = status.find_by_address(target.exit_address)
    return destination is not None and status.connected_id == destination.id


def connection_state(host: Host, profile: Profile, target: Target) -> tuple[bool, dict[str, Any] | None]:
    """(connected to the target, the client's latest exit probe)."""
    status = vpn_for(host, profile).try_status()
    if status is None:
        return False, None
    destination = status.find_by_address(target.exit_address)
    if destination is None or status.connected_id != destination.id:
        return False, None
    return True, status.probe


def connection_check(
    hosts: list[Host], unavailable: dict[str, str], profile: Profile, target: Target
) -> dict[str, Any]:
    """Who is connected to the target now, and the exit's slots as last probed.

    Clients that never connected (*unavailable*) count as not connected.
    Exit slot values are ``null`` when no connected client holds a probe.
    """
    states: dict[str, tuple[bool, dict[str, Any] | None]] = {}
    if hosts:
        try:
            states = parallel(partial(connection_state, profile=profile, target=target), hosts)
        except (OSError, RuntimeError, ValueError) as error:
            log.warning("connection check failed: %s", error)
    connected = sorted(name for name, (ok, _) in states.items() if ok)
    probes = [probe for ok, probe in states.values() if ok and probe]
    freshest = max(probes, key=lambda p: p.get("checked_at") or 0, default=None)
    slots = ((freshest or {}).get("load") or {}).get("slots") or {}
    checked_ms = (freshest or {}).get("checked_at")
    now = time.time()

    def count(key: str) -> int | None:
        value = slots.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    return {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "clients_total": len(hosts) + len(unavailable),
        "clients_connected": len(connected),
        "connected": connected,
        "exit_slots_total": count("total"),
        "exit_slots_in_use": count("connected"),
        "exit_slots_free": count("available"),
        "probe_age_s": round(now - checked_ms / 1000, 1) if isinstance(checked_ms, (int, float)) else None,
    }


def remote_dir(attempt: str, rung: int) -> str:
    return f"{REMOTE_DIR}/ladder-{attempt}/rung{rung}"


def launch(host: Host, spec: LadderSpec, attempt: str, rung: int, start_at: float, origin_ip: str) -> None:
    out = remote_dir(attempt, rung)
    config = {
        "start_at": start_at,
        "deadline_s": spec.deadline_s,
        "bytes": spec.transfer_bytes,
        "output": out,
        "url": f"http://{origin_ip}/payload.bin?rung={rung}&client={host.name}",
        "ping_target": spec.ping_target,
    }
    config_path = f"{out}.json"
    host.run(["mkdir", "-p", f"{REMOTE_DIR}/ladder-{attempt}"], timeout_s=20)
    write_file(host, config_path, json.dumps(config).encode())
    started = host.run(
        [
            "systemd-run",
            f"--unit=gvpn-bench-{attempt}-rung{rung}",
            "/usr/bin/python3",
            f"{REMOTE_DIR}/ladder_agent.py",
            "download",
            config_path,
        ],
        timeout_s=30,
    )
    if not started.ok:
        raise RuntimeError(f"{host.name}: cannot start rung {rung}: {started.stderr.strip()[:200]}")


def collect(host: Host, attempt: str, rung: int, give_up_at: float) -> dict[str, Any] | None:
    """The agent's result.json, or None if it never appeared (agent failed, SSH lost)."""
    path = f"{remote_dir(attempt, rung)}/result.json"
    while time.time() < give_up_at:
        try:
            return json.loads(read_file(host, path, timeout_s=15))
        except (RuntimeError, ValueError):
            time.sleep(0.5)
    log.error("%s: rung %d result unavailable", host.name, rung)
    return None


def fetch_pings(host: Host, attempt: str, rung: int) -> list[dict[str, Any]] | None:
    path = f"{remote_dir(attempt, rung)}/pings.json"
    for _ in range(15):
        try:
            return json.loads(read_file(host, path, timeout_s=15))
        except (RuntimeError, ValueError):
            time.sleep(1)
    return None


def empty_rung(rung: int, clients: list[str], status: str, failure: str, detail: str = "") -> dict[str, Any]:
    return {
        "rung": rung,
        "clients": clients,
        "status": status,
        "failure_status": failure,
        "detail": detail,
        "total_mbps": None,
        "overlap_s": None,
        "start_skew_ms": None,
        "gap_before_s": None,
        "per_client": [],
    }


def rung_entry(
    rung: int,
    raw: dict[str, dict[str, Any]],
    origin_sha256: str,
    gap_before_s: float | None,
) -> dict[str, Any]:
    clients = sorted(raw)
    try:
        summary = summarize(raw)
    except NoOverlapError:
        return empty_rung(rung, clients, "failed", "no_overlap", "transfers share no common interval")
    per_client = []
    for name in clients:
        metrics = summary["clients"][name]
        item = raw[name]
        per_client.append(
            {
                "client": name,
                "mbps": round(metrics["mbps"], 3),
                "overlap_bytes": round(metrics["overlap_bytes"]),
                "downloaded_bytes": metrics["downloaded_bytes"],
                "complete": metrics["complete"],
                "sha256_verified": bool(metrics["complete"] and item["sha256"] == origin_sha256),
                "timed_out": metrics["timed_out"],
                "ping_mean_ms": metrics["ping_mean_ms"],
                "ping_loss_pct": metrics["ping_loss_pct"],
                "ping_sent": metrics["ping_sent"],
                "ping_received": metrics["ping_received"],
                "system": system_summary(item),
                "timeseries": timeseries(item),
            }
        )
    return {
        "rung": rung,
        "clients": clients,
        "status": "measured",
        "failure_status": None,
        "total_mbps": round(summary["total_mbps"], 3),
        "overlap_s": round(summary["overlap_seconds"], 3),
        "start_skew_ms": round(summary["start_skew_ms"], 3),
        "gap_before_s": round(gap_before_s, 3) if gap_before_s is not None else None,
        "overlap_start": None,
        "overlap_end": None,
        "detail": "",
        "per_client": per_client,
    }


def check_connected(
    hosts: list[Host],
    profile: Profile,
    target: Target,
    reconnect: Callable[[Host], bool] | None,
) -> tuple[list[str], list[str]]:
    """(lost, reconnected) client names; each lost client gets one reconnect attempt."""
    connected = parallel(partial(still_connected, profile=profile, target=target), hosts)
    dropped = [h for h in hosts if not connected[h.name]]
    if not dropped or reconnect is None:
        return [h.name for h in dropped], []
    log.warning("reconnecting %s to the target", ", ".join(h.name for h in dropped))
    retried = parallel(reconnect, dropped)
    lost = [name for name, ok in retried.items() if not ok]
    return lost, [name for name, ok in retried.items() if ok]


def new_attempt(data_dir: Path) -> str:
    """A UTC timestamp naming this ladder pass, unique among *data_dir*'s raw-data folders."""
    base = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    attempt, suffix = base, 1
    while (data_dir / attempt).exists():
        suffix += 1
        attempt = f"{base}-{suffix}"
    (data_dir / attempt).mkdir(parents=True)
    return attempt


def run_ladder(
    run: Run,
    profile: Profile,
    target: Target,
    client_hosts: list[Host],
    origin_ip: str,
    origin_sha256: str,
    data_dir: Path | None = None,
    *,
    unavailable: dict[str, str] | None = None,
    reconnect: Callable[[Host], bool] | None = None,
) -> list[dict[str, Any]]:
    """Run the rungs with *client_hosts*, the clients connected to the target.

    *unavailable* maps clients that did not connect to their connect status;
    *reconnect* is tried once for a client that dropped before a rung.
    """
    unavailable = unavailable or {}
    spec = run.config.ladder
    data_dir = data_dir or run.data
    data_dir.mkdir(parents=True, exist_ok=True)
    attempt = new_attempt(data_dir)
    rungs: list[dict[str, Any]] = []
    raw_by_rung: dict[int, dict[str, dict[str, Any]]] = {}
    previous_end: float | None = None
    next_start: float | None = None
    for index, rung in enumerate(spec.rungs):
        hosts = client_hosts[:rung]
        names = [h.name for h in hosts]
        if any(r["status"] != "measured" for r in rungs):
            rungs.append(empty_rung(rung, names, "not_run", "not_run"))
            continue
        if rung > len(client_hosts):
            missing = sorted(unavailable.items())
            status = missing[0][1] if missing else "not_run"
            causes = ", ".join(f"{name}: {why}" for name, why in missing) or "too few clients"
            detail = f"only {len(client_hosts)} client(s) connected to the target ({causes})"
            log.error("rung %d: %s", rung, detail)
            rungs.append(empty_rung(rung, names, "failed", status, detail))
            rungs[-1]["connection_check"] = connection_check(client_hosts, unavailable, profile, target)
            continue
        lost, reconnected = check_connected(hosts, profile, target, reconnect)
        snapshot = connection_check(client_hosts, unavailable, profile, target)
        log.info(
            "rung %d: %d of %d clients connected; exit slots %s/%s in use",
            rung, snapshot["clients_connected"], snapshot["clients_total"],
            snapshot["exit_slots_in_use"], snapshot["exit_slots_total"],
        )  # fmt: skip
        if lost:
            log.error("rung %d: not connected to the target: %s", rung, ", ".join(lost))
            detail = f"not connected to the target: {', '.join(lost)}"
            if reconnect is not None:
                detail += " (one reconnect attempt failed)"
            rungs.append(empty_rung(rung, names, "failed", "timeout", detail))
            rungs[-1]["connection_check"] = snapshot
            continue
        start_at = next_start if next_start is not None else time.time() + FIRST_RUNG_LEAD_S
        if reconnected:
            # A reconnect outlasts the gap; start afresh (gap_before_s records the real gap).
            start_at = max(start_at, time.time() + FIRST_RUNG_LEAD_S)
        if start_at - time.time() < MIN_LEAD_S:
            log.error("rung %d: too late to keep the %ss gap", rung, spec.gap_s)
            rungs.append(empty_rung(rung, names, "failed", "error", "too late to keep the inter-rung gap"))
            rungs[-1]["connection_check"] = snapshot
            continue
        try:
            parallel(
                partial(launch, spec=spec, attempt=attempt, rung=rung, start_at=start_at, origin_ip=origin_ip),
                hosts,
            )
        except RuntimeError as error:
            rungs.append(empty_rung(rung, names, "failed", "error", str(error)[:300]))
            rungs[-1]["connection_check"] = snapshot
            continue
        log.info("rung %d scheduled for %.3f with %s", rung, start_at, names)
        give_up = start_at + spec.deadline_s + COLLECT_GRACE_S
        collected = parallel(partial(collect, attempt=attempt, rung=rung, give_up_at=give_up), hosts)
        missing = [name for name, item in collected.items() if item is None]
        if missing:
            detail = f"no result from {', '.join(missing)} (agent failed or SSH lost)"
            rungs.append(empty_rung(rung, names, "failed", "error", detail))
            rungs[-1]["connection_check"] = snapshot
            continue
        raw = {name: item for name, item in collected.items() if item is not None}
        last_end = max(item["end"] for item in raw.values())
        if index + 1 < len(spec.rungs):
            next_start = last_end + spec.gap_s
        gap = min(item["start"] for item in raw.values()) - previous_end if previous_end else None
        previous_end = last_end
        raw_by_rung[rung] = raw
        rungs.append(rung_entry(rung, raw, origin_sha256, gap))
        rungs[-1]["connection_check"] = snapshot
        if reconnected:
            note = f"reconnected before this rung: {', '.join(reconnected)}"
            rungs[-1]["detail"] = f"{rungs[-1]['detail']}; {note}" if rungs[-1]["detail"] else note
        log.info("rung %d: %s Mbit/s", rung, rungs[-1]["total_mbps"])
        for client in rungs[-1]["per_client"]:
            log.info(
                "rung %d %s: %s Mbit/s, %s bytes, complete %s, ping %s ms",
                rung, client["client"], client["mbps"], client["downloaded_bytes"], client["complete"],
                round(client["ping_mean_ms"]) if client["ping_mean_ms"] is not None else "n/a",
            )  # fmt: skip
    # All rungs done: now fetch the complete ping logs and recompute. Raw data
    # goes to one folder per ladder attempt so a later pass never overwrites it.
    attempt_dir = data_dir / attempt
    attempt_dir.mkdir(parents=True, exist_ok=True)
    for position, rung in enumerate(spec.rungs):
        raw = raw_by_rung.get(rung)
        if not raw:
            continue
        for host in client_hosts[:rung]:
            pings = fetch_pings(host, attempt, rung)
            if pings is not None:
                raw[host.name]["pings"] = pings
            (attempt_dir / f"rung{rung}-{host.name}.json").write_text(json.dumps(raw[host.name]))
        previous = rungs[position]
        rungs[position] = rung_entry(rung, raw, origin_sha256, previous["gap_before_s"])
        rungs[position]["detail"] = previous["detail"]
        if "connection_check" in previous:
            rungs[position]["connection_check"] = previous["connection_check"]
    return rungs
