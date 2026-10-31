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
import time
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
from . import deadline, guards
from .backup import backup_identity, save_safe_info
from .connect import ConnectOutcome, connect_client, disconnect, vpn_for
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


def step_provision(user: UserConfig, run: Run, profile: Profile) -> None:
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


def result_dir(run: Run, target: Target) -> Path:
    return run.path / "results" / target.name


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
        lambda pair: connect_client(
            pair[1], str(pair[0].ip), profile, target.exit_address, route_checks=route_checks
        ),
        pairs,
    )
    for outcome, (_entry, host) in zip(outcomes, pairs, strict=True):
        if outcome.status == "connected":
            info = vpn_for(host, profile).ctl("balance", timeout_s=45).get("Balance", {}).get("Ok", {})
            if info.get("info"):
                save_safe_info(user, run, profile, host.name, info["info"])
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


def step_ladder(run: Run, profile: Profile, target: Target) -> list[dict[str, Any]]:
    origin = machine(run, "origin")
    origin_sha = json.loads((run.state / "origin.json").read_text())["sha256"]
    started = utc_now()
    rungs = run_ladder(
        run,
        profile,
        target,
        [h for _, h in client_hosts(run)],
        str(origin.ip),
        origin_sha,
        data_dir=run.data / target.name,
    )
    write_private_json(
        run.state / f"ladder-{target.name}.json",
        {"rungs": rungs, "started_at": started, "finished_at": utc_now()},
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
    run.mark(
        "recover", recovered_wxHOPR=cleanup["recovered_wxHOPR"], recovered_xDAI=cleanup["recovered_xDAI"]
    )
    return cleanup


def step_destroy(user: UserConfig, run: Run, profile: Profile) -> dict[str, Any]:
    """Delete only this run's tagged droplets, after re-verifying recovery."""
    inventory = load_inventory(run)
    if funds_sent(run):
        from ..finance.recover import verify_for_destroy
        from ..finance.wallet import connect_chain

        verify_for_destroy(
            connect_chain(), run, profile, [m.name for m in clients(run)], [m.id for m in inventory]
        )
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


def step_result(run: Run, profile: Profile, target: Target) -> Path:
    directory = result_dir(run, target)
    directory.mkdir(parents=True, exist_ok=True)
    write_result(directory, build_result(run, profile, target))
    write_report(directory)
    return directory
