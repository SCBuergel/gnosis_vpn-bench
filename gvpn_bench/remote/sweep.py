"""Availability sweep: try every exit once and classify the outcome.

Replaces the ladder repos' availability.py, blocked.py and their ad-hoc
retries. Each exit is checked by one client (clients work through a shared
queue in parallel):

1. connect and prove the tunnel (connect.connect_client);
2. download the first 5 MiB of the origin's file through it;
3. send three pings to the ping target;
4. disconnect.

Each row gets exactly one status from the fixed list. A failed check never
reports a speed: its rates stay ``None``.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import queue
import re
from typing import Any

from ..host import Host
from ..profiles import Profile
from ..runs import utc_now
from ..status import Destination
from .connect import connect_client, disconnect, vpn_for

log = logging.getLogger("gnosis_speedtest")

CHECK_BYTES = 5 * 1024 * 1024
_PING_LINE = re.compile(r"time[=<]([\d.]+) ms")


def ping_summary(output: str, sent: int) -> tuple[float | None, float | None]:
    rtts = [float(v) for v in _PING_LINE.findall(output)]
    mean = sum(rtts) / len(rtts) if rtts else None
    loss = 100 * (1 - len(rtts) / sent) if sent else None
    return mean, loss


def short_download(host: Host, url: str) -> dict[str, Any]:
    result = host.run(
        [
            "curl",
            "--noproxy",
            "*",
            "-sS",
            "--fail",
            "--connect-timeout",
            "15",
            "--max-time",
            "60",
            "-r",
            f"0-{CHECK_BYTES - 1}",
            "-o",
            "/dev/null",
            "-w",
            "%{json}",
            url,
        ],
        timeout_s=75,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"size_download": 0, "http_code": 0, "error": result.stderr.strip()[:200]}


def check_exit(
    host: Host,
    vm_ip: str,
    profile: Profile,
    destination: Destination,
    origin_ip: str,
    ping_target: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "exit_address": destination.address,
        "exit_label": destination.name,
        "exit_id": destination.id,
        "location": destination.location,
        "client": host.name,
        "status": "error",
        "detail": None,
        "download_bytes": None,
        "download_mbps": None,
        "ping_mean_ms": None,
        "ping_loss_pct": None,
        "api_rtt_ms": None,
        "tested_at": utc_now(),
    }
    try:
        outcome = connect_client(
            host,
            vm_ip,
            profile,
            destination.address,
            route_checks=[origin_ip, ping_target],
            timeout_s=180,
        )
        probe = outcome.evidence.get("probe") or {}
        if isinstance(probe.get("ping_rtt"), (int, float)):
            row["api_rtt_ms"] = round(probe["ping_rtt"], 1)
        if outcome.status != "connected":
            findings = outcome.evidence.get("worker_log_findings") or []
            row["status"] = outcome.status
            row["detail"] = "; ".join([outcome.detail, *findings[-2:]]).strip("; ")[:500]
            return row
        download = short_download(host, f"http://{origin_ip}/payload.bin?sweep={destination.id}")
        row["download_bytes"] = download.get("size_download")
        if download.get("size_download") != CHECK_BYTES or download.get("http_code") != 206:
            row["status"] = "download_incomplete"
            row["detail"] = f"http {download.get('http_code')}, {download.get('size_download')} bytes"
            return row
        row["status"] = "connected"
        row["download_mbps"] = round(download["speed_download"] * 8 / 1e6, 3)
        pings = host.run(
            ["ping", "-n", "-c", "3", "-i", "1", "-W", "5", "-w", "18", ping_target], timeout_s=25
        )
        row["ping_mean_ms"], row["ping_loss_pct"] = ping_summary(pings.stdout, 3)
        row["detail"] = f"{CHECK_BYTES} B tunnelled download + 3 pings to {ping_target}"
        return row
    except Exception as error:  # one exit's crash must not end the sweep
        row["status"] = "error"
        row["detail"] = f"{type(error).__name__}: {error}"[:300]
        return row
    finally:
        disconnect(host, profile)


def run_sweep(
    hosts: list[Host],
    vm_ips: dict[str, str],
    profile: Profile,
    origin_ip: str,
    ping_target: str,
    only: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Check every exit the first client discovers (or just the ``only`` addresses)."""
    status = vpn_for(hosts[0], profile).status()
    destinations = status.destinations
    if only:
        wanted = {a.lower() for a in only}
        destinations = [d for d in destinations if d.address in wanted]
    jobs: queue.Queue[Destination] = queue.Queue()
    for destination in destinations:
        jobs.put(destination)
    rows: list[dict[str, Any]] = []

    def worker(host: Host) -> None:
        while True:
            try:
                destination = jobs.get_nowait()
            except queue.Empty:
                return
            row = check_exit(host, vm_ips[host.name], profile, destination, origin_ip, ping_target)
            log.info("%s via %s: %s", destination.name, host.name, row["status"])
            rows.append(row)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        list(pool.map(worker, hosts))
    return sorted(rows, key=lambda r: r["exit_label"])
