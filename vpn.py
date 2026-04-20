"""
Shell helper and Gnosis VPN control (connect / disconnect / status).
"""

import json
import logging
import subprocess
import time

from config import CONNECTION_TIMEOUT_S, POLL_INTERVAL_S

log = logging.getLogger("gnosis_speedtest")


# ---------------------------------------------------------------------------
# Shell helper
# ---------------------------------------------------------------------------

def run_cmd(
    cmd: list[str],
    timeout_s: int = 30,
    stdin_data: bytes | None = None,
) -> tuple[int, str, str]:
    log.debug("CMD: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, input=stdin_data, capture_output=True, timeout=timeout_s)
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        if result.returncode not in (0, 28):
            log.warning("CMD exited %d | stderr: %s", result.returncode, stderr.strip()[:300])
        else:
            log.debug("CMD rc=%d stdout_len=%d", result.returncode, len(stdout))
        return result.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        log.error("CMD timed out after %ds: %s", timeout_s, " ".join(cmd))
        return -1, "", "process_timeout"
    except FileNotFoundError:
        log.error("CMD not found: %s", cmd[0])
        return -127, "", "not_found"
    except Exception as exc:
        log.error("CMD exception: %s", exc)
        return -1, "", str(exc)


# ---------------------------------------------------------------------------
# VPN status / destinations
# ---------------------------------------------------------------------------

def get_raw_status() -> dict | None:
    rc, stdout, stderr = run_cmd(["gnosis_vpn-ctl", "--json", "status"], timeout_s=15)
    if rc != 0:
        log.error("status failed (rc=%d): %s", rc, stderr.strip()[:200])
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        log.error("JSON parse error: %s", exc)
        return None


def get_destinations() -> list[dict]:
    status = get_raw_status()
    if not status:
        return []
    destinations = []
    for entry in status.get("Status", {}).get("destinations", []):
        dest = entry["destination"]
        addr_bytes: list[int] = dest["address"]
        addr_hex = "0x" + "".join(f"{b:02x}" for b in addr_bytes)
        destinations.append({
            "id": dest["id"],
            "location": dest.get("meta", {}).get("location", "Unknown"),
            "address": addr_hex,
        })
    return destinations


def _parse_state(raw) -> str:
    return next(iter(raw)) if isinstance(raw, dict) else str(raw)


def get_connection_state(location_id: str) -> str:
    status = get_raw_status()
    if not status:
        return "Unknown"
    s = status.get("Status", {})
    connected = s.get("connected")
    if connected and connected.get("destination_id") == location_id:
        return "Connected"
    connecting = s.get("connecting")
    if connecting and connecting.get("destination_id") == location_id:
        return "Connecting"
    for entry in s.get("disconnecting", []):
        if entry.get("destination_id") == location_id:
            return "Disconnecting"
    return "None"


# ---------------------------------------------------------------------------
# VPN connect / disconnect
# ---------------------------------------------------------------------------

_NOT_CONNECTED = {"none", "connecting", "disconnecting", "unknown"}


def wait_for_connection(location_id: str, timeout_s: int = CONNECTION_TIMEOUT_S) -> float | None:
    log.info("Waiting for %s to connect (timeout %ds)…", location_id, timeout_s)
    t0 = time.monotonic()
    deadline = t0 + timeout_s
    prev: str | None = None
    while time.monotonic() < deadline:
        state = get_connection_state(location_id)
        elapsed = time.monotonic() - t0
        if state != prev:
            log.info("[%.1fs] %s: %r → %r", elapsed, location_id, prev, state)
            prev = state
        if state.lower() not in _NOT_CONNECTED:
            log.info("[%.1fs] %s connected", elapsed, location_id)
            return round(elapsed, 2)
        log.debug("[%.1fs] %s still %r…", elapsed, location_id, state)
        time.sleep(POLL_INTERVAL_S)
    log.error("Timed out waiting for %s after %ds", location_id, timeout_s)
    return None


def wait_for_disconnection(location_id: str, timeout_s: int = 60) -> bool:
    log.info("Waiting for %s to disconnect…", location_id)
    deadline = time.monotonic() + timeout_s
    prev: str | None = None
    while time.monotonic() < deadline:
        state = get_connection_state(location_id)
        if state != prev:
            log.info("%s disconnect: %r → %r", location_id, prev, state)
            prev = state
        if state in ("None", "none"):
            log.info("%s disconnected", location_id)
            return True
        time.sleep(POLL_INTERVAL_S)
    log.warning("Timed out waiting for %s to disconnect", location_id)
    return False


def fix_dns() -> None:
    log.info("Fixing DNS → 1.1.1.1")
    rc, _, stderr = run_cmd(["sudo", "tee", "/etc/resolv.conf"], timeout_s=10,
                            stdin_data=b"nameserver 1.1.1.1\n")
    if rc == 0:
        log.info("DNS fix applied.")
    else:
        log.warning("DNS fix failed: %s", stderr.strip()[:200])


def connect_to(destination: dict) -> bool:
    loc_id = destination["id"]
    log.info("Connecting to %s (%s)", loc_id, destination["location"])
    rc, stdout, stderr = run_cmd(["gnosis_vpn-ctl", "connect", loc_id], timeout_s=20)
    if rc != 0:
        log.error("connect failed for %s (rc=%d): %s", loc_id, rc, stderr.strip()[:200])
        return False
    log.debug("connect: %s", stdout.strip()[:200])
    return True


def disconnect_vpn() -> bool:
    log.info("Disconnecting…")
    rc, _, stderr = run_cmd(["gnosis_vpn-ctl", "disconnect"], timeout_s=20)
    if rc != 0:
        if "not connected" in stderr.lower():
            log.debug("Already disconnected.")
            return True
        log.error("disconnect failed: %s", stderr.strip()[:200])
        return False
    return True


def vpn_connect(destination: dict) -> float | None:
    """Connect to *destination*, fix DNS.  Returns connect_time or None."""
    if not connect_to(destination):
        return None
    ct = wait_for_connection(destination["id"])
    if ct is None:
        disconnect_vpn()
        wait_for_disconnection(destination["id"], timeout_s=30)
        return None
    fix_dns()
    return ct


def vpn_disconnect(destination: dict) -> None:
    disconnect_vpn()
    wait_for_disconnection(destination["id"])
