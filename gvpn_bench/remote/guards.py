"""SSH guards on each client VM and the operator heartbeat that feeds them.

* ``ssh-keeper``: keeps a /32 route and killswitch exceptions for the
  operator's IP, so connecting the VPN does not cut our SSH session.
* ``deadman``: if the heartbeat file goes stale (operator gone), stops the
  VPN worker after 6 min and reboots once after 12 min.
* heartbeat: a detached local process that touches the heartbeat file on
  every client every 45 s until ``stop_heartbeat``.

Shut down in the reverse order at recovery: stop the workers first, then
the guards, then the heartbeat.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from ..host import Host
from ..profiles import Profile
from ..runs import Run, load_user_config, open_run
from .inventory import clients, host_for
from .provision import REMOTE_DIR

HEARTBEAT_FILE = "/run/gvpn-deadman.alive"
HEARTBEAT_INTERVAL_S = 45
REPO_DIR = Path(__file__).resolve().parent.parent.parent


def operator_route(host: Host) -> tuple[str, str, str]:
    """(operator IP as the VM sees it, WAN gateway, WAN device)."""
    seen = host.run(["sh", "-c", 'echo "$SSH_CONNECTION"'], timeout_s=20)
    operator_ip = seen.stdout.split()[0] if seen.ok and seen.stdout.split() else None
    routes = host.run(["ip", "-j", "-4", "route", "show", "default"], timeout_s=20)
    if not operator_ip or not routes.ok:
        raise RuntimeError(f"{host.name}: cannot determine operator IP or default route")
    default = json.loads(routes.stdout)[0]
    return operator_ip, default["gateway"], default["dev"]


def start_guards(host: Host, profile: Profile) -> None:
    operator_ip, gateway, device = operator_route(host)
    host.run(["touch", HEARTBEAT_FILE], timeout_s=20)
    host.run(["systemctl", "reset-failed", "gvpn-ssh-keeper", "gvpn-deadman"], timeout_s=20)
    for unit, argv in (
        ("gvpn-ssh-keeper", [f"{REMOTE_DIR}/ssh-keeper.sh", operator_ip, gateway, device]),
        ("gvpn-deadman", [f"{REMOTE_DIR}/deadman.sh", profile.worker.user]),
    ):
        active = host.run(["systemctl", "is-active", unit], timeout_s=20)
        if active.stdout.strip() == "active":
            continue
        started = host.run(["systemd-run", f"--unit={unit}", *argv], timeout_s=30)
        if not started.ok:
            raise RuntimeError(f"{host.name}: cannot start {unit}: {started.stderr.strip()[:200]}")
    check = host.run(["systemctl", "is-active", "gvpn-ssh-keeper", "gvpn-deadman"], timeout_s=20)
    if check.stdout.split() != ["active", "active"]:
        raise RuntimeError(f"{host.name}: guards not active: {check.stdout.split()}")


def stop_guards(host: Host) -> None:
    host.run(["systemctl", "stop", "gvpn-deadman", "gvpn-ssh-keeper"], timeout_s=60)


def heartbeat_pid_file(run: Run) -> Path:
    return run.state / "heartbeat.pid"


def start_heartbeat(run: Run) -> int:
    pid_file = heartbeat_pid_file(run)
    if pid_file.exists() and _alive(int(pid_file.read_text())):
        return int(pid_file.read_text())
    log_file = (run.state / "heartbeat.log").open("a")
    process = subprocess.Popen(
        [sys.executable, "-m", "gvpn_bench.remote.guards", "heartbeat", str(run.path)],
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(REPO_DIR)},
    )
    pid_file.write_text(str(process.pid))
    return process.pid


def stop_heartbeat(run: Run) -> None:
    pid_file = heartbeat_pid_file(run)
    if not pid_file.exists():
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(int(pid_file.read_text()), signal.SIGTERM)
    pid_file.unlink()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def heartbeat_loop(run: Run) -> None:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    hosts = [host_for(run, m) for m in clients(run)]
    while not stop.is_set():
        for host in hosts:
            if not host.run(["touch", HEARTBEAT_FILE], timeout_s=20).ok:
                print(f"{host.name}: heartbeat failed", flush=True)
        stop.wait(HEARTBEAT_INTERVAL_S)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "heartbeat":
        heartbeat_loop(open_run(sys.argv[2], load_user_config()))
    else:
        sys.exit("usage: python -m gvpn_bench.remote.guards heartbeat RUN_DIR")
