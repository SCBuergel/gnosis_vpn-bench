"""The run steps behind ``provision``, ``fund``, ``connect``, ``ladder``,
``sweep``, ``recover`` and ``destroy``.

Each step reads the run directory, does one thing, records it in the step
journal and returns. The CLI asks for confirmation before steps that cost
money or move funds.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..host import Host
from ..profiles import Profile, Target
from ..results.schema import empty_result
from ..results.store import (
    BENCH_VERSION,
    bench_commit,
    profile_provenance,
    target_provenance,
    write_report,
    write_result,
)
from ..runs import Run, UserConfig, utc_now, write_private_json
from ..vpn import CtlError
from . import deadline, guards, preflight
from .backup import backup_identity, save_safe_info
from .connect import ConnectOutcome, connect_client, connect_to, disconnect, vpn_for
from .digitalocean import Api, load_token, run_tag
from .inventory import Machine, clients, host_for, load_inventory, machine
from .ladder import run_ladder
from .provision import client_config_sha256, provision
from .sweep import run_sweep

log = logging.getLogger("gnosis_speedtest")


def api_for(user: UserConfig) -> Api:
    return Api(load_token(user.digitalocean_token_file))


def client_hosts(run: Run) -> list[tuple[Machine, Host]]:
    return [(m, host_for(run, m)) for m in clients(run)]


def _parallel(function, items):
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(items))) as pool:
        return list(pool.map(function, items))


# -- provision ---------------------------------------------------------------


def step_preflight(run: Run, profile: Profile) -> None:
    """Refuse to create VMs for an unknown target or a package that cannot be downloaded."""
    problems = preflight.check(profile, run.config.targets(), run_client_config=run.config.client_config is not None)
    problems += preflight.package_problems(profile)
    if problems:
        raise RuntimeError("pre-flight check failed, no VM created:\n  " + "\n  ".join(problems))
    log.info("pre-flight: every target is a registered, announced node on %s", profile.network.name)


def step_provision(user: UserConfig, run: Run, profile: Profile) -> None:
    if not load_inventory(run):  # only before the first droplet; a resumed provision has passed it
        step_preflight(run, profile)
    provision(run, profile, api_for(user))


# -- fund ----------------------------------------------------------------------


def funding_requests(run: Run, profile: Profile, timeout_s: float = 300) -> list[dict[str, str]]:
    """Each client's PreparingSafe recommendation (node address and amounts)."""
    nodes = []
    for entry, host in client_hosts(run):
        vpn = vpn_for(host, profile)
        deadline_at = time.monotonic() + timeout_s
        request = None
        while time.monotonic() < deadline_at:
            status = vpn.try_status()
            if status is not None and status.funding_request is not None:
                request = status.funding_request
                break
            time.sleep(3)
        if request is None:
            raise RuntimeError(f"{entry.name}: no funding recommendation (is the worker in PreparingSafe?)")
        nodes.append(
            {
                "name": entry.name,
                "address": request.node_address,
                "wxhopr": request.wxhopr,
                "xdai": request.xdai,
            }
        )
    return nodes


def capacity_preflight(user: UserConfig, run: Run, profile: Profile) -> list[str]:
    """Check droplet headroom and funding before any droplet exists; one note per check.

    Raises when a check fails. A check that cannot be made (a token without
    account access, a profile without a funding estimate) is reported, not fatal.
    """
    clients = run.config.infra.clients
    needed = clients + 1  # the clients and the origin
    notes = []
    headroom = api_for(user).droplet_headroom()
    if headroom is None:
        notes.append("droplet limit: unknown (the API token cannot read the account)")
    elif headroom < needed:
        raise RuntimeError(f"droplet limit: room for {headroom} more droplets, this run needs {needed}")
    else:
        notes.append(f"droplet limit: room for {headroom} more, this run needs {needed}")
    estimate = profile.expected_funding
    if estimate is None:
        notes.append(f"funding: not checked (profile {profile.name} has no [funding] estimate)")
        return notes
    from ..finance.wallet import connect_chain, funding_shortfall, load_funding_source

    w3 = connect_chain()
    source = load_funding_source(user.funding_key_file)
    tokens = clients * w3.to_wei(estimate.wxhopr, "ether")
    native = clients * w3.to_wei(estimate.xdai, "ether")
    problems = funding_shortfall(w3, source, tokens, native)
    if problems:
        raise RuntimeError(f"funding for {clients} clients: " + "; ".join(problems))
    notes.append(f"funding: the Safe covers {clients} × ({estimate.wxhopr} wxHOPR + {estimate.xdai} xDAI)")
    return notes


def step_fund_plan(user: UserConfig, run: Run, profile: Profile) -> dict[str, Any]:
    """Read requests, pin chain metadata, back up identities. Moves no funds."""
    from ..finance.chain import pin_chain

    pin_chain(run, profile)
    nodes = funding_requests(run, profile)
    for _entry, host in client_hosts(run):
        backup_identity(user, run, profile, host)
    write_private_json(run.state / "funding.json", nodes)
    return {"nodes": nodes}


def step_fund_send(user: UserConfig, run: Run, profile: Profile) -> None:
    from ..finance.wallet import connect_chain, fund_nodes, load_funding_source

    nodes = json.loads((run.state / "funding.json").read_text())
    w3 = connect_chain()
    source = load_funding_source(user.funding_key_file)
    fund_nodes(w3, run, source, nodes)
    run.mark("fund", nodes=[n["name"] for n in nodes])


# -- connect -----------------------------------------------------------------------


def direct_baseline(run: Run, hosts: list[Host], origin_ip: str) -> list[dict[str, Any]]:
    """Simultaneous direct (no VPN) downloads: an origin capacity check."""

    def fetch(host: Host) -> dict[str, Any]:
        result = host.run(
            [
                "curl",
                "-sS",
                "--max-time",
                "120",
                "-o",
                "/dev/null",
                "-w",
                "%{json}",
                f"http://{origin_ip}/payload.bin",
            ],
            timeout_s=130,
        )
        data = json.loads(result.stdout) if result.ok else {}
        return {
            "client": host.name,
            "mbps": round(data["speed_download"] * 8 / 1e6, 2) if data.get("speed_download") else None,
            "bytes": data.get("size_download"),
        }

    rows = _parallel(fetch, hosts)
    write_private_json(run.state / "baseline.json", rows)
    return rows


def result_dir(run: Run, target: Target, pass_no: int | None = None) -> Path:
    """``results/<target>``; pass 2 and later of a repeated ladder get ``<target>-pass<N>``."""
    name = target.name if pass_no is None or pass_no == 1 else f"{target.name}-pass{pass_no}"
    return run.path / "results" / name


IDLE_PING_COUNT = 30
_PING_SUMMARY = re.compile(r"(\d+) packets transmitted, (\d+) received")
_PING_RTT = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)/")


def parse_ping(client: str, output: str) -> dict[str, Any]:
    """Summary of ``ping -q`` output; RTTs are null when nothing came back."""
    summary = _PING_SUMMARY.search(output)
    rtt = _PING_RTT.search(output)
    sent = int(summary.group(1)) if summary else None
    received = int(summary.group(2)) if summary else None
    return {
        "client": client,
        "sent": sent,
        "received": received,
        "loss_pct": round(100 * (sent - received) / sent, 1) if sent and received is not None else None,
        "min_ms": float(rtt.group(1)) if rtt else None,
        "mean_ms": float(rtt.group(2)) if rtt else None,
        "max_ms": float(rtt.group(3)) if rtt else None,
        "status": "measured" if rtt else "no_reply",
    }


def idle_ping(hosts: list[Host], ping_target: str, count: int = IDLE_PING_COUNT) -> list[dict[str, Any]]:
    """Unloaded round trip through each client's tunnel, all clients at once, before any transfer."""

    def measure(host: Host) -> dict[str, Any]:
        result = host.run(["ping", "-q", "-c", str(count), "-i", "1", "-W", "3", ping_target], timeout_s=count + 30)
        return parse_ping(host.name, result.stdout)

    rows = _parallel(measure, hosts) if hosts else []
    for row in rows:
        log.info(
            "idle ping %s: %s of %s replies, min/mean/max %s/%s/%s ms",
            row["client"],
            row["received"],
            row["sent"],
            row["min_ms"],
            row["mean_ms"],
            row["max_ms"],
        )
    return rows


def step_connect(user: UserConfig, run: Run, profile: Profile, target: Target) -> list[ConnectOutcome]:
    pairs = client_hosts(run)
    hosts = [h for _, h in pairs]
    origin = machine(run, "origin")
    for host in hosts:
        guards.start_guards(host, profile)
    guards.start_heartbeat(run)
    if not (run.state / "baseline.json").exists():
        direct_baseline(run, hosts, str(origin.ip))
    # Leave any previous target first (a run can test several exits).
    _parallel(lambda host: disconnect(host, profile), hosts)
    route_checks = [str(origin.ip), run.config.ladder.ping_target]
    outcomes = _parallel(
        lambda pair: connect_client(pair[1], str(pair[0].ip), profile, target.exit_address, route_checks=route_checks),
        pairs,
    )
    for outcome, (_entry, host) in zip(outcomes, pairs, strict=True):
        # The Safe file exists once the funded node deployed its Safe.
        backup_identity(user, run, profile, host, require_safe=outcome.status == "connected")
        # Every funded client has a Safe, connected or not: a client the exit
        # refused still needs its Safe info for recovery.
        try:
            info = vpn_for(host, profile).ctl("balance", timeout_s=45).get("Balance", {}).get("Ok") or {}
        except CtlError as error:
            log.warning("%s: balance unavailable after connect: %s", host.name, error)
            info = {}
        if info.get("info"):
            save_safe_info(user, run, profile, host.name, info["info"])
        elif outcome.status == "connected":
            log.warning("%s: connected but the balance call returned no Safe info", host.name)
    write_private_json(
        run.state / f"connect-{target.name}.json",
        [
            {
                "client": o.client,
                "status": o.status,
                "detail": o.detail,
                "connect_time_s": o.connect_time_s,
                "exit_address": o.destination.address if o.destination else None,
                "egress_changed": o.egress_ip is not None if o.status == "connected" else None,
                "evidence": o.evidence,
                "at": utc_now(),
            }
            for o in outcomes
        ],
    )
    run.mark("connect", target=target.name, statuses={o.client: o.status for o in outcomes})
    return outcomes


# -- ladder and sweep -----------------------------------------------------------


RECONNECT_TIMEOUT_S = 120


def connect_statuses(run: Run, target: Target) -> dict[str, str] | None:
    """Each client's connect status for *target*, or None before ``connect`` ran."""
    entries = _read(run, f"connect-{target.name}.json")
    return {e["client"]: e["status"] for e in entries} if entries is not None else None


def step_ladder(run: Run, profile: Profile, target: Target) -> list[dict[str, Any]]:
    origin = machine(run, "origin")
    origin_sha = json.loads((run.state / "origin.json").read_text())["sha256"]
    pairs = client_hosts(run)
    statuses = connect_statuses(run, target)
    if statuses is None:  # no connect record: every client must prove itself before each rung
        statuses = {host.name: "connected" for _, host in pairs}
    ips = {host.name: str(entry.ip) for entry, host in pairs}
    route_checks = [str(origin.ip), run.config.ladder.ping_target]

    def reconnect(host: Host) -> bool:
        outcome = connect_to(
            host, ips[host.name], profile, target.exit_address, route_checks=route_checks, timeout_s=RECONNECT_TIMEOUT_S
        )
        log.info("%s: reconnect → %s %s", host.name, outcome.status, outcome.detail)
        return outcome.status == "connected"

    connected = [h for _, h in pairs if statuses.get(h.name) == "connected"]
    started = utc_now()
    pings = idle_ping(connected, run.config.ladder.ping_target)
    rungs = run_ladder(
        run,
        profile,
        target,
        connected,
        str(origin.ip),
        origin_sha,
        data_dir=run.data / target.name,
        unavailable={name: status for name, status in statuses.items() if status != "connected"},
        reconnect=reconnect,
    )
    write_private_json(
        run.state / f"ladder-{target.name}.json",
        {"rungs": rungs, "idle_ping": pings, "started_at": started, "finished_at": utc_now()},
    )
    run.mark("ladder", target=target.name, totals={r["rung"]: r["total_mbps"] for r in rungs})
    return rungs


def step_sweep(run: Run, profile: Profile, only: list[str] | None) -> list[dict[str, Any]]:
    pairs = client_hosts(run)
    origin = machine(run, "origin")
    rows = run_sweep(
        [h for _, h in pairs],
        {m.name: str(m.ip) for m, _ in pairs},
        profile,
        str(origin.ip),
        run.config.ladder.ping_target,
        only,
    )
    write_private_json(run.state / "sweep.json", rows)
    run.mark("sweep", exits=len(rows))
    return rows


# -- recover and destroy -------------------------------------------------------------


def funds_sent(run: Run) -> bool:
    """True once any funding transaction of this run is confirmed (or still open)."""
    from ..finance.wallet import Journal

    return any(
        str(e.get("purpose", "")).startswith("fund:") and e["status"] in ("confirmed", "pending", "dropped")
        for e in Journal(run).entries()
    )


def step_recover_initiate(user: UserConfig, run: Run, profile: Profile) -> dict[str, Any]:
    from ..finance.chain import load_chain
    from ..finance.recover import halt_client, initiate
    from ..finance.wallet import connect_chain

    load_chain(run, profile)  # fail before touching any client if metadata is missing
    api = api_for(user)
    pairs = [(m, host_for(run, m), api.get_droplet(m.id) is None) for m in clients(run)]
    balances = {}
    for (entry, _host, _gone), balance in zip(
        pairs,
        _parallel(lambda item: halt_client(user, run, profile, item[1], vm_deleted=item[2]), pairs),
        strict=True,
    ):
        balances[entry.name] = balance
    guards.stop_heartbeat(run)
    return initiate(connect_chain(), run, profile, balances)


def step_recover_finalize(user: UserConfig, run: Run, profile: Profile) -> dict[str, Any]:
    from ..finance.recover import finalize
    from ..finance.wallet import connect_chain, load_funding_source

    destination = load_funding_source(user.funding_key_file).safe
    cleanup = finalize(connect_chain(), run, profile, [m.name for m in clients(run)], destination)
    run.mark("recover", recovered_wxHOPR=cleanup["recovered_wxHOPR"], recovered_xDAI=cleanup["recovered_xDAI"])
    return cleanup


def step_destroy(user: UserConfig, run: Run, profile: Profile) -> dict[str, Any]:
    """Delete only this run's tagged droplets, after re-verifying recovery."""
    inventory = load_inventory(run)
    if funds_sent(run):
        from ..finance.recover import verify_for_destroy
        from ..finance.wallet import connect_chain

        verify_for_destroy(connect_chain(), run, profile, [m.name for m in clients(run)], [m.id for m in inventory])
    guards.stop_heartbeat(run)
    api = api_for(user)
    tag = run_tag(run.config.id)
    for entry in inventory:
        api.delete_droplet(entry.id, tag)
        log.info("deleted %s (%s)", entry.name, entry.id)
    remaining = {d["id"] for d in api.droplets_with_tag(tag)}
    if remaining & {m.id for m in inventory}:
        raise RuntimeError(f"droplets still present: {sorted(remaining)}")
    deadline.cancel(run)
    record_path = run.state / "cleanup.json"
    record = json.loads(record_path.read_text()) if record_path.exists() else {}
    record.update({"droplets_deleted": len(inventory), "destroyed_at_utc": utc_now()})
    write_private_json(record_path, record)
    run.mark("destroy", droplets=len(inventory))
    return record


# -- result.json ------------------------------------------------------------------------


def _read(run: Run, name: str) -> Any:
    path = run.state / name
    return json.loads(path.read_text()) if path.exists() else None


def build_result(run: Run, profile: Profile, target: Target) -> dict[str, Any]:
    machines = _read(run, "machines.json") or []
    client_infos = [m for m in machines if m["role"] == "client"]
    ladder = _read(run, f"ladder-{target.name}.json")
    # A sweep covers all exits; it is reported with the run's primary target.
    sweep = (_read(run, "sweep.json") or []) if target.name == run.config.target_name else []
    connect = _read(run, f"connect-{target.name}.json") or []
    started = (ladder or {}).get("started_at")
    kind = "ladder" if ladder else "sweep"
    ctl_versions = {m.get("ctl_version") for m in client_infos}
    packages = {m.get("package_version") for m in client_infos}
    result = empty_result(
        run={
            "id": run.config.id,
            "kind": kind,
            "created_at": run.config.created_at,
            "started_at": started,
            "finished_at": (ladder or {}).get("finished_at"),
            "reproducible": True,
            "notes": run.config.notes,
        },
        provenance={
            "bench": {"version": BENCH_VERSION, "commit": bench_commit()},
            "profile": profile_provenance(profile),
            "client": {
                "ctl_version": ctl_versions.pop() if len(ctl_versions) == 1 else None,
                "package_version": packages.pop() if len(packages) == 1 else None,
            },
            "target": target_provenance(target),
            "infrastructure": {
                "provider": run.config.infra.provider,
                "region": run.config.infra.region,
                "machines": [
                    {
                        key: m.get(key)
                        for key in (
                            "role",
                            "name",
                            "size",
                            "vcpus",
                            "memory_mib",
                            "cpu_model",
                            "os",
                            "kernel",
                            "architecture",
                            "cpu_vendor",
                            "cpu_sockets",
                            "cores_per_socket",
                            "threads_per_core",
                            "hypervisor",
                            "root_disk_gib",
                            "root_free_gib",
                            "swap_mib",
                            "package_version",
                            "ctl_version",
                        )
                    }
                    for m in machines
                ],
            },
            "client_config_sha256": client_config_sha256(run),
        },
    )
    spec = run.config.ladder
    if ladder:
        result["concurrent"] = {
            "spec": {
                "transfer_bytes": spec.transfer_bytes,
                "deadline_s": spec.deadline_s,
                "gap_s": spec.gap_s,
                "ping_target": spec.ping_target,
                "ping_interval_s": spec.ping_interval_s,
            },
            "origin": _read(run, "origin.json"),
            "baseline": _read(run, "baseline.json") or [],
            "idle_ping": ladder.get("idle_ping", []),
            "rungs": ladder["rungs"],
        }
        for rung in ladder["rungs"]:
            if rung["status"] != "measured":
                result["failures"].append(
                    {
                        "scope": "rung",
                        "subject": f"rung {rung['rung']}",
                        "status": rung["failure_status"],
                        "detail": rung.get("detail", ""),
                        "at": None,
                    }
                )
    for entry in connect:
        result["per_client"].append(
            {
                "client": entry["client"],
                "connected": entry["status"] == "connected",
                "connect_time_s": entry["connect_time_s"],
                "exit_address": entry["exit_address"],
                "egress_changed": entry["egress_changed"],
            }
        )
        if entry["status"] != "connected":
            result["failures"].append(
                {
                    "scope": "client",
                    "subject": entry["client"],
                    "status": entry["status"],
                    "detail": entry["detail"],
                    "at": entry["at"],
                }
            )
    result["availability"] = sweep
    for row in sweep:
        if row["status"] != "connected":
            result["failures"].append(
                {
                    "scope": "exit",
                    "subject": f"{row['exit_label']} {row['exit_address']}",
                    "status": row["status"],
                    "detail": row["detail"] or "",
                    "at": row["tested_at"],
                }
            )
    return result


def step_result(run: Run, profile: Profile, target: Target, pass_no: int | None = None) -> Path:
    directory = result_dir(run, target, pass_no)
    directory.mkdir(parents=True, exist_ok=True)
    write_result(directory, build_result(run, profile, target))
    write_report(directory)
    return directory


# -- payment check ---------------------------------------------------------------

# Below this many wxHOPR spent, a PIX run counts as having taken no payment.
PIX_SPEND_EPSILON = Decimal("0.000001")


def payment_summary(run: Run, profile: Profile, transferred_bytes: int) -> dict[str, Any] | None:
    """Funded, recovered and spent wxHOPR after recovery, plus a warning when PIX took nothing."""
    nodes = _read(run, "funding.json")
    recovered = run.steps().get("recover", {}).get("recovered_wxHOPR")
    if not nodes or recovered is None:
        return None
    funded = sum((Decimal(str(n["wxhopr"])) for n in nodes), Decimal(0))
    spent = funded - Decimal(str(recovered))
    pix = profile.worker.payment_dir(profile.network.name) is not None
    summary: dict[str, Any] = {
        "funded_wxHOPR": str(funded),
        "recovered_wxHOPR": str(recovered),
        "spent_wxHOPR": str(spent),
        "pix": pix,
        "transferred_bytes": transferred_bytes,
    }
    if pix and transferred_bytes > 0 and spent < PIX_SPEND_EPSILON:
        summary["warning"] = {
            "code": "pix_no_payment",
            "detail": (
                f"{profile.network.name} uses PIX, {transferred_bytes} bytes went through the tunnel, "
                f"but only {spent} wxHOPR was spent: PIX took no payment"
            ),
        }
    return summary


def _transferred_bytes(result: dict[str, Any]) -> int:
    rungs = (result.get("concurrent") or {}).get("rungs") or []
    return sum(c.get("downloaded_bytes") or 0 for r in rungs for c in r.get("per_client", []))


def step_payment_check(run: Run, profile: Profile) -> list[str]:
    """After recovery: add the payment summary (and any warning) to every result, re-render.

    Returns the warnings; the caller pushes the updated results before deleting VMs.
    """
    warnings: list[str] = []
    for path in sorted((run.path / "results").glob("*/result.json")):
        result = json.loads(path.read_text())
        summary = payment_summary(run, profile, _transferred_bytes(result))
        if summary is None:
            continue
        warning = summary.pop("warning", None)
        result["payment"] = summary
        result["warnings"] = [w for w in result.get("warnings", []) if w.get("code") != "pix_no_payment"]
        if warning:
            result["warnings"].append(warning)
            warnings.append(f"{path.parent.name}: {warning['detail']}")
            log.warning("WARNING %s: %s", path.parent.name, warning["detail"])
        write_result(path.parent, result)
        write_report(path.parent)
    return warnings
