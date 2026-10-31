"""Bring each client's tunnel up to the run's target exit and prove it.

A connection only counts when all of these hold:

* the worker is ``Running`` and lists exactly one destination with the
  target's address (never another exit as a substitute);
* status reports that destination as connected;
* the public egress IP differs from the VM's own IP;
* the routes to the ping target and to the origin use the tunnel, not
  the WAN device.

Failures are classified into the fixed status list (results/schema.py).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..host import Host, run_script
from ..profiles import Profile
from ..status import Destination
from ..vpn import CtlError, VpnClient

log = logging.getLogger("gnosis_speedtest")


def vpn_for(host: Host, profile: Profile) -> VpnClient:
    """A VpnClient addressing this profile's worker. VMs never get the DNS rewrite."""
    return VpnClient(
        host=host, ctl_prefix=profile.worker.ctl_prefix(), dns_mode="never", json_flag=("-o", "json")
    )


@dataclass
class ConnectOutcome:
    client: str
    status: str  # a FAILURE_STATUSES value; "connected" on success
    detail: str = ""
    connect_time_s: float | None = None
    destination: Destination | None = None
    egress_ip: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


def wait_running(vpn: VpnClient, timeout_s: float = 360) -> bool:
    deadline_at = time.monotonic() + timeout_s
    while time.monotonic() < deadline_at:
        status = vpn.try_status()
        if status is not None and status.running and status.destinations:
            return True
        time.sleep(2)
    return False


def tunnel_routes_ok(host: Host, destinations: list[str]) -> tuple[bool, list[Any]]:
    """True if no destination is routed via the default (WAN) device."""
    wan = json.loads(host.run(["ip", "-j", "-4", "route", "show", "default"], timeout_s=20).stdout or "[{}]")
    wan_dev = wan[0].get("dev") if wan else None
    routes = []
    for address in destinations:
        result = host.run(["ip", "-j", "route", "get", address], timeout_s=20)
        routes.append(json.loads(result.stdout)[0] if result.ok else {"dst": address, "error": result.stderr})
    ok = wan_dev is not None and all(r.get("dev") and r.get("dev") != wan_dev for r in routes)
    return ok, routes


def connect_client(
    host: Host,
    vm_ip: str,
    profile: Profile,
    exit_address: str,
    *,
    route_checks: list[str],
    timeout_s: float = 240,
) -> ConnectOutcome:
    vpn = vpn_for(host, profile)
    try:
        vpn.ctl("start-client", "30m", timeout_s=45)
    except CtlError as error:
        return ConnectOutcome(
            host.name, "control_timeout" if "timed out" in str(error) else "error", str(error)
        )
    if not wait_running(vpn):
        return ConnectOutcome(host.name, "worker_not_ready", "worker did not reach Running with destinations")
    status = vpn.status()
    try:
        destination = status.find_by_address(exit_address)
    except ValueError as error:
        return ConnectOutcome(host.name, "error", str(error))
    if destination is None:
        return ConnectOutcome(host.name, "DestinationNotFound", f"{exit_address} not in client destinations")
    t0 = time.monotonic()
    try:
        response = vpn.ctl("connect", destination.id, timeout_s=45)
    except CtlError as error:
        status_name = "control_timeout" if "timed out" in str(error) else "connect_rejected"
        return ConnectOutcome(host.name, status_name, str(error), destination=destination)
    if "Connect" not in response:
        return ConnectOutcome(
            host.name, "connect_rejected", json.dumps(response)[:300], destination=destination
        )
    connected = False
    last_status = status
    while time.monotonic() < t0 + timeout_s:
        current = vpn.try_status()
        if current is not None:
            last_status = current
            if current.connected_id == destination.id:
                connected = True
                break
        time.sleep(2)
    elapsed = round(time.monotonic() - t0, 2)
    evidence = {
        "probe": last_status.probe,
        "route_health": last_status.route_health.get(destination.id),
        "connecting_phase": last_status.connecting_phase,
    }
    if not connected:
        evidence["worker_log_findings"] = log_findings(worker_log_tail(host, profile))
        return ConnectOutcome(
            host.name,
            classify_failure(evidence),
            f"not connected after {timeout_s:.0f} s",
            destination=destination,
            evidence=evidence,
        )
    egress = host.run(["curl", "-4", "-fsS", "--max-time", "30", "https://api.ipify.org"], timeout_s=40)
    egress_ip = egress.stdout.strip() if egress.ok else None
    if egress_ip is None or egress_ip == vm_ip:
        return ConnectOutcome(
            host.name, "route_bypass", "public IP unchanged by the VPN", elapsed, destination
        )
    routes_ok, routes = tunnel_routes_ok(host, route_checks)
    evidence["routes"] = routes
    if not routes_ok:
        return ConnectOutcome(
            host.name, "route_bypass", "a measurement destination bypasses the tunnel", elapsed
        )
    return ConnectOutcome(host.name, "connected", "", elapsed, destination, egress_ip, evidence)


_LOG_TAIL = 'f=$(ls -1 $1 2>/dev/null | tail -1); [ -n "$f" ] && tail -c 200000 "$f" || true\n'


def worker_log_tail(host: Host, profile: Profile) -> str:
    """Last ~200 kB of the worker log (v4: one file; v5: newest rotated file)."""
    result = run_script(host, _LOG_TAIL, [profile.worker.worker_log_glob()], timeout_s=30)
    return result.stdout if result.ok else ""


def log_findings(log_text: str) -> list[str]:
    """Distinctive failure lines from a worker log (at most the last 5)."""
    needles = ("UnacceptablePixParams", "clients/register", "DestinationNotFound", "probe check failed")
    lines = [line for line in log_text.splitlines() if any(n in line for n in needles)]
    return lines[-5:]


def classify_failure(evidence: dict[str, Any]) -> str:
    """Map client diagnostics to the fixed status list."""
    text = json.dumps(evidence)
    if "UnacceptablePixParams" in text:
        return "UnacceptablePixParams"
    if "404" in text and "clients/register" in text:
        return "http_404_register"
    if "TimedOut" in text or "timed out" in text.lower():
        return "control_timeout"
    return "timeout"


def disconnect(host: Host, profile: Profile, timeout_s: float = 60) -> bool:
    vpn = vpn_for(host, profile)
    try:
        vpn.ctl("disconnect", timeout_s=30)
    except CtlError as error:
        log.warning("%s: disconnect: %s", host.name, error)
    deadline_at = time.monotonic() + timeout_s
    while time.monotonic() < deadline_at:
        status = vpn.try_status()
        if status is not None and status.connected_id is None and status.connecting_id is None:
            return True
        time.sleep(2)
    return False
