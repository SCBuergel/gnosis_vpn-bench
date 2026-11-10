"""Rank cloud regions by round-trip time to an exit node, before planning a run.

``probe-regions`` creates one small droplet per region, all tagged
``gvpn-bench-probe-<id>``, measures from each the RTT to the exit's announced
IPv4 address, and deletes every droplet with that tag again, also after
errors or Ctrl-C, then verifies none is left. It moves no funds and connects
no VPN.

Many exits ignore ICMP. The RTT is then that of the last hop that answered
in ``mtr`` (``rtt_source = "last_hop"``), which is usually the exit's
provider edge and a lower bound for the exit itself.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..host import Host, SshHost, run_script
from ..profiles import Target, load_networks
from .assets import asset
from .digitalocean import public_ipv4
from .preflight import Query, _account, graphql

log = logging.getLogger(__name__)

DEFAULT_REGIONS = ["ams3", "fra1", "lon1", "nyc3", "tor1", "sfo3", "blr1", "sgp1", "syd1"]
PROBE_SIZE = "s-1vcpu-512mb-10gb"
PROBE_IMAGE = "ubuntu-24-04-x64"
PROBE_COUNT = 10
_IP4 = re.compile(r"/ip4/([0-9.]+)/")
_PING_RTT = re.compile(r"= [\d.]+/([\d.]+)/")
_MTR_HOP = re.compile(r"^\s*\d+\.\|--\s+(\S+)\s+([\d.]+)%?\s+\d+\s+[\d.]+\s+([\d.]+)")


class ProbeApi(Protocol):
    def create_droplet(self, **kwargs: Any) -> dict[str, Any]: ...
    def get_droplet(self, droplet_id: int) -> dict[str, Any] | None: ...
    def droplets_with_tag(self, tag: str) -> list[dict[str, Any]]: ...
    def delete_droplet(self, droplet_id: int, expected_tag: str) -> bool: ...


@dataclass
class RegionRtt:
    region: str
    rtt_ms: float | None
    rtt_source: str | None  # "ping" (the node answered) or "last_hop"
    hop: str | None
    status: str  # measured / no_reply / unreachable


def probe_tag(probe_id: str) -> str:
    return "gvpn-bench-probe-" + re.sub(r"[^a-z0-9-]", "-", probe_id.lower())


def exit_ip(target: Target, query: Query | None = None) -> str:
    """The first public IPv4 address the exit announces on its network's indexer."""
    indexer = load_networks()[target.network].indexer
    account = _account(query or graphql, indexer, target.exit_address)
    if account is None:
        raise RuntimeError(f"{target.exit_address} is not a registered node on {target.network}")
    for address in account.get("multiAddresses") or []:
        match = _IP4.search(address + "/")
        if match and ipaddress.ip_address(match.group(1)).is_global:
            return match.group(1)
    raise RuntimeError(f"{target.exit_address} announces no public IPv4 address")


def parse_rtt(region: str, output: str) -> RegionRtt:
    """Prefer the node's own ping; fall back to the last mtr hop that answered."""
    ping_part, _, mtr_part = output.partition("== mtr")
    ping = _PING_RTT.search(ping_part)
    if ping:
        return RegionRtt(region, float(ping.group(1)), "ping", None, "measured")
    last: tuple[str, float] | None = None
    for line in mtr_part.splitlines():
        hop = _MTR_HOP.match(line)
        if hop and hop.group(1) != "???" and float(hop.group(2)) < 100:
            last = (hop.group(1), float(hop.group(3)))
    if last is None:
        return RegionRtt(region, None, None, None, "no_reply")
    return RegionRtt(region, last[1], "last_hop", last[0], "measured")


def cloud_config(public_key: str) -> str:
    config = {
        "ssh_authorized_keys": [public_key],
        # Same as the run VMs: without it DigitalOcean forces a password change and SSH fails.
        "bootcmd": [["sh", "-c", 'chage -d "$(date +%F)" -M 99999 root']],
        "packages": ["mtr-tiny"],
    }
    return "#cloud-config\n" + json.dumps(config, indent=1) + "\n"


def make_key(directory: Path) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = directory / "probe_key"
    if not key.exists():
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "gvpn-bench probe", "-f", str(key)], check=True
        )
    return key


def wait_for_ips(api: ProbeApi, ids: dict[str, int], timeout_s: float, poll_s: float) -> dict[str, str]:
    ips: dict[str, str] = {}
    deadline_at = time.monotonic() + timeout_s
    while len(ips) < len(ids) and time.monotonic() < deadline_at:
        for region, droplet_id in ids.items():
            if region in ips:
                continue
            droplet = api.get_droplet(droplet_id)
            ip = public_ipv4(droplet) if droplet and droplet.get("status") == "active" else None
            if ip:
                ips[region] = ip
        if len(ips) < len(ids):
            time.sleep(poll_s)
    return ips


def measure(host: Host, region: str, ip: str, count: int = PROBE_COUNT, attempts: int = 6) -> RegionRtt:
    """SSH can refuse for a minute while cloud-init runs; retry before giving up."""
    for attempt in range(attempts):
        result = run_script(host, asset("probe-rtt.sh"), [ip, str(count)], timeout_s=600)
        if result.returncode != 255:
            return parse_rtt(region, result.stdout)
        if attempt < attempts - 1:
            time.sleep(10)
    return RegionRtt(region, None, None, None, "unreachable")


def _leftovers(api: ProbeApi, tag: str, poll_s: float, checks: int = 12) -> list[int]:
    """Droplets still carrying *tag*; deleted ones can stay listed for a few seconds."""
    left: list[int] = []
    for check in range(checks):
        left = [d["id"] for d in api.droplets_with_tag(tag)]
        if not left:
            return []
        if check < checks - 1:
            time.sleep(poll_s)
    return left


def probe_regions(
    api: ProbeApi,
    target_ip: str,
    regions: list[str],
    tag: str,
    key_dir: Path,
    *,
    host_factory: Any = None,
    boot_timeout_s: float = 300,
    poll_s: float = 5,
) -> tuple[list[RegionRtt], list[int]]:
    """RTT per region, fastest first, and the IDs of any probe droplet still left (should be none)."""
    key = make_key(key_dir)
    user_data = cloud_config(key.with_suffix(".pub").read_text().strip())

    def default_host(region: str, ip: str) -> Host:
        return SshHost(
            name=f"probe-{region}",
            address=ip,
            identity_file=key,
            known_hosts_file=key_dir / "known_hosts",
            connect_timeout_s=20,
        )

    host_factory = host_factory or default_host
    ids: dict[str, int] = {}
    results: list[RegionRtt] = []
    try:
        for region in regions:
            droplet = api.create_droplet(
                name=f"probe-{region}",
                region=region,
                size=PROBE_SIZE,
                image=PROBE_IMAGE,
                user_data=user_data,
                tags=[tag],
            )
            ids[region] = droplet["id"]
            log.info("probe droplet %s in %s", droplet["id"], region)
        ips = wait_for_ips(api, ids, boot_timeout_s, poll_s)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(ips))) as pool:
            futures = {r: pool.submit(measure, host_factory(r, ip), r, target_ip) for r, ip in ips.items()}
        results = [_result(futures.get(r), r) for r in regions]
    finally:
        left = delete_probes(api, tag, ids, poll_s)
    return sorted(results, key=lambda r: (r.rtt_ms is None, r.rtt_ms or 0)), left


def _result(future: concurrent.futures.Future[RegionRtt] | None, region: str) -> RegionRtt:
    """One region's measurement; a region that failed does not lose the others."""
    if future is None:
        return RegionRtt(region, None, None, None, "unreachable")
    try:
        return future.result()
    except Exception as error:
        log.error("probe %s failed: %s", region, error)
        return RegionRtt(region, None, None, None, "unreachable")


def delete_probes(api: ProbeApi, tag: str, ids: dict[str, int], poll_s: float) -> list[int]:
    """Delete every droplet carrying *tag*, also ones created but never recorded; return what is left.

    Runs on every path, also after an error or Ctrl-C, and logs any leftover
    loudly: probe droplets have no deletion timer.
    """
    known = set(ids.values())
    try:
        known |= {d["id"] for d in api.droplets_with_tag(tag)}
    except Exception as error:
        log.error("cannot list probe droplets tagged %s: %s", tag, error)
    for droplet_id in sorted(known):
        try:
            api.delete_droplet(droplet_id, tag)
        except Exception as error:  # keep deleting the others; reported via the leftover check
            log.error("probe droplet %s not deleted: %s", droplet_id, error)
    try:
        left = _leftovers(api, tag, poll_s)
    except Exception as error:
        log.error("cannot verify probe droplets tagged %s are gone: %s", tag, error)
        left = sorted(known)
    if left:
        log.error("PROBE DROPLETS NOT DELETED: %s (tag %s); delete them in the DigitalOcean console", left, tag)
    elif known:
        # Also logged when the probe then fails, so every ending says what happened to the droplets.
        log.warning("all probe droplets deleted (no droplet with tag %s is left)", tag)
    return left
