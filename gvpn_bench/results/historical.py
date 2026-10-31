"""Import the October 2026 ladder-repo results as historical runs.

Sources (read-only):

* ``gnosis-vpn-serbia-ladder/data/blocked.json`` — Serbia on piz-palu-prod (v5)
* ``gnosis-vpn-lithuania-ladder/results/kaunas/data/blocked.json`` — Kaunas on piz-palu-dev (v5)
* ``gnosis-vpn-lithuania-ladder/data/v5-summary.json`` — v5 exit sweep on piz-palu-dev
* ``gnosis-vpn-lithuania-ladder/results/serbia-v4-20261029-160144/data/blocked.json``
  — Serbia on jura-prod (v4)

None of these runs reached ladder rung 1, so every rung is ``not_run``.
Imported runs are marked ``reproducible: false``: the raw per-second data,
exact client configs and run state were never captured in this schema.

Only selected fields are copied. Raw curl dumps (which contain VM IPs),
inventories and balances are not.

Usage::

    python3 -m gvpn_bench.results.historical ~/gnosis-vpn-serbia-ladder ~/gnosis-vpn-lithuania-ladder
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..profiles import load_profile, load_target
from ..runs import load_user_config
from .schema import empty_result
from .store import (
    BENCH_VERSION,
    bench_commit,
    profile_provenance,
    target_provenance,
    write_report,
    write_result,
)

NOT_REPRODUCIBLE = (
    "Imported from the ladder harness. No ladder rung ran; raw per-second data, "
    "the exact run state and some client settings were not kept in this form."
)


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _machine(raw: dict[str, Any]) -> dict[str, Any]:
    cpu_model = None
    try:
        fields = {f["field"].rstrip(":"): f["data"] for f in json.loads(raw["lscpu"])["lscpu"]}
        cpu_model = fields.get("Model name")
    except (KeyError, ValueError, TypeError):
        pass
    memory = re.search(r"MemTotal:\s+(\d+) kB", raw.get("memory", ""))
    name = raw["name"]
    return {
        "role": "origin" if name == "origin" else "client",
        "name": name,
        "size": raw.get("size"),
        "vcpus": raw.get("vcpus"),
        "memory_mib": round(int(memory.group(1)) / 1024) if memory else None,
        "cpu_model": cpu_model,
        "os": (raw.get("os") or {}).get("PRETTY_NAME"),
        "kernel": raw.get("kernel"),
    }


def _client_versions(blocked: dict[str, Any]) -> dict[str, Any]:
    infos = [c.get("info", {}).get("Info", {}) for c in blocked["clients"].values()]
    versions = {i.get("version") for i in infos}
    packages = {i.get("package_version") for i in infos}
    return {
        "ctl_version": versions.pop() if len(versions) == 1 else None,
        "package_version": packages.pop() if len(packages) == 1 else None,
    }


def _baseline(blocked: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "client": name,
            "mbps": round(sample["speed_download"] * 8 / 1e6, 2),
            "bytes": sample["size_download"],
        }
        for name, sample in sorted(blocked.get("baseline", {}).items())
    ]


def _not_run_ladder(blocked: dict[str, Any], failure_status: str) -> dict[str, Any]:
    rungs = []
    for rung in (1, 2, 3):
        rungs.append(
            {
                "rung": rung,
                "clients": [f"client{i}" for i in range(1, rung + 1)],
                "status": "not_run",
                "failure_status": failure_status,
                "total_mbps": None,
                "overlap_s": None,
                "start_skew_ms": None,
                "gap_before_s": None,
                "per_client": [],
            }
        )
    return {
        "spec": {
            "transfer_bytes": 200_000_000,
            "deadline_s": 300,
            "gap_s": 10,
            "ping_target": "9.9.9.9",
            "ping_interval_s": 1,
        },
        "origin": blocked.get("file"),
        "baseline": _baseline(blocked),
        "rungs": rungs,
    }


def _blocked_run(
    blocked: dict[str, Any],
    *,
    run_id: str,
    profile_name: str,
    target_name: str,
    failure_status: str,
    source: str,
    notes: str,
) -> dict[str, Any]:
    profile = load_profile(profile_name)
    target = load_target(target_name)
    when = iso(blocked["captured_at"])
    prov_profile = profile_provenance(profile)
    client = _client_versions(blocked)
    if prov_profile["package_version"] is None:
        # Unpinned profile: record what was actually installed instead.
        prov_profile["package_version"] = client["package_version"]
    result = empty_result(
        run={
            "id": run_id,
            "kind": "ladder",
            "created_at": when,
            "started_at": None,
            "finished_at": when,
            "reproducible": False,
            "notes": f"{NOT_REPRODUCIBLE} {notes}".strip(),
            "historical_source": source,
        },
        provenance={
            "bench": {"version": BENCH_VERSION, "commit": bench_commit()},
            "profile": prov_profile,
            "client": client,
            "target": target_provenance(target),
            "infrastructure": {
                "provider": "digitalocean",
                "region": "fra1",
                "machines": [_machine(m) for m in blocked["machines"]],
            },
        },
    )
    result["concurrent"] = _not_run_ladder(blocked, failure_status)
    for name, info in sorted(blocked["clients"].items()):
        result["per_client"].append(
            {
                "client": name,
                "connected": bool(info.get("connected")),
                "connect_time_s": None,
                "exit_address": target.exit_address,
                "egress_changed": None,
            }
        )
    result["availability"].append(
        {
            "exit_address": target.exit_address,
            "exit_label": target.label,
            "exit_id": None,
            "location": target.location,
            "client": None,
            "status": failure_status,
            "detail": blocked["failure"],
            "download_mbps": None,
            "ping_mean_ms": None,
            "ping_loss_pct": None,
            "api_rtt_ms": None,
            "tested_at": when,
        }
    )
    result["failures"].append(
        {
            "scope": "run",
            "subject": target.exit_address,
            "status": failure_status,
            "detail": blocked["failure"],
            "at": when,
        }
    )
    return result


def _default_exit_rows(blocked: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for check in blocked.get("default_exit_checks", []):
        dest = check["destination"]
        complete = bool(check.get("connected") and check.get("download_complete"))
        status = "connected" if complete else ("download_incomplete" if check.get("connected") else "timeout")
        rows.append(
            {
                "exit_address": dest["address"].lower(),
                "exit_label": dest["id"],
                "exit_id": dest["id"],
                "location": (dest.get("meta") or {}).get("location"),
                "client": check["client"],
                "status": status,
                "detail": "5 MiB tunneled download + 3 pings; socket buffers 8/16 MiB",
                "download_bytes": check.get("download_bytes"),
                "download_mbps": round(check["download_mbps"], 3) if complete else None,
                "ping_mean_ms": check.get("ping_mean_ms") if complete else None,
                "ping_loss_pct": check.get("ping_loss_pct") if complete else None,
                "api_rtt_ms": None,
                "tested_at": iso(check["tested_at"]) if check.get("tested_at") else None,
            }
        )
    return rows


def import_serbia_v5(serbia_repo: Path) -> dict[str, Any]:
    blocked = json.loads((serbia_repo / "data/blocked.json").read_text())
    return _blocked_run(
        blocked,
        run_id="hist-20261028-serbia-piz-palu-prod-v5",
        profile_name="v5-piz-palu-prod",
        target_name="serbia-piz-palu-prod",
        failure_status="timeout",
        source="gnosis-vpn-serbia-ladder data/blocked.json",
        notes="Probe checks of the exit's control API timed out (Load, Version).",
    )


def import_kaunas(lithuania_repo: Path) -> dict[str, Any]:
    blocked = json.loads((lithuania_repo / "results/kaunas/data/blocked.json").read_text())
    result = _blocked_run(
        blocked,
        run_id="hist-20261028-kaunas-piz-palu-dev-v5",
        profile_name="v5-piz-palu-dev",
        target_name="kaunas",
        failure_status="http_404_register",
        source="gnosis-vpn-lithuania-ladder results/kaunas/data/blocked.json",
        notes="Packages tried: " + ", ".join(blocked.get("attempted_packages", [])) + ".",
    )
    return result


def import_v5_sweep(lithuania_repo: Path) -> dict[str, Any]:
    summary = json.loads((lithuania_repo / "data/v5-summary.json").read_text())
    profile = load_profile("v5-piz-palu-dev")
    target = load_target("kaunas")
    when = "2026-10-28T00:00:00Z"
    result = empty_result(
        run={
            "id": "hist-20261028-sweep-piz-palu-dev-v5",
            "kind": "sweep",
            "created_at": when,
            "started_at": None,
            "finished_at": None,
            "reproducible": False,
            "notes": f"{NOT_REPRODUCIBLE} Exact check times were not recorded; the date is the commit date.",
            "historical_source": "gnosis-vpn-lithuania-ladder data/v5-summary.json",
        },
        provenance={
            "bench": {"version": BENCH_VERSION, "commit": bench_commit()},
            "profile": profile_provenance(profile),
            "client": {"ctl_version": "0.101.1", "package_version": profile.package.version},
            "target": target_provenance(target),
            "infrastructure": {"provider": "digitalocean", "region": "fra1", "machines": []},
        },
    )
    for row in summary:
        address = row["address"].lower()
        statuses = []
        if row["session_rejection"]:
            statuses.append(row["session_rejection"])
        if row["registration_http_404"]:
            statuses.append("http_404_register")
        status = statuses[0] if statuses else "timeout"
        result["availability"].append(
            {
                "exit_address": address,
                "exit_label": row["name"],
                "exit_id": None,
                "location": None,
                "client": row["client"],
                "status": status,
                "detail": "; ".join([*statuses, f"last phase {row['last_phase']}"]),
                "download_mbps": None,
                "ping_mean_ms": None,
                "ping_loss_pct": None,
                "api_rtt_ms": round(row["api_probe_rtt_ms"], 1) if row.get("api_probe_rtt_ms") else None,
                "tested_at": None,
            }
        )
        for extra in statuses:
            result["failures"].append(
                {
                    "scope": "exit",
                    "subject": f"{row['name']} {address}",
                    "status": extra,
                    "detail": "",
                    "at": None,
                }
            )
    return result


def import_serbia_v4(lithuania_repo: Path) -> dict[str, Any]:
    blocked = json.loads((lithuania_repo / "results/serbia-v4-20261029-160144/data/blocked.json").read_text())
    result = _blocked_run(
        blocked,
        run_id="hist-20261029-serbia-jura-prod-v4",
        profile_name="v4-jura-prod",
        target_name="serbia-jura-prod",
        failure_status="control_timeout",
        source="gnosis-vpn-lithuania-ladder results/serbia-v4-20261029-160144/data/blocked.json",
        notes=blocked.get("default_exit_notes", ""),
    )
    result["availability"].extend(_default_exit_rows(blocked))
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("serbia_repo", type=Path)
    parser.add_argument("lithuania_repo", type=Path)
    parser.add_argument("--out", type=Path, help="default: the configured runs directory")
    args = parser.parse_args(argv)
    out = args.out or load_user_config().runs_dir
    results = [
        import_serbia_v5(args.serbia_repo.expanduser()),
        import_kaunas(args.lithuania_repo.expanduser()),
        import_v5_sweep(args.lithuania_repo.expanduser()),
        import_serbia_v4(args.lithuania_repo.expanduser()),
    ]
    for result in results:
        run_dir = out / result["run"]["id"]
        run_dir.mkdir(parents=True, exist_ok=True)
        write_result(run_dir, result)
        print("wrote", write_report(run_dir))


if __name__ == "__main__":
    main()
