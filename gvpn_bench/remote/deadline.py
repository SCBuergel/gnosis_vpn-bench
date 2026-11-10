"""Hard billing cap: delete a run's droplets after a fixed time.

``schedule`` registers a transient ``systemd --user`` timer that runs
``python -m gvpn_bench.remote.deadline RUN_DIR`` after
``infrastructure.deadline_hours``. It is set up before any droplet exists,
and provisioning refuses to continue if it cannot be set up.

When it fires on a run that was funded but not yet recovered, it deletes
only if every funded client's identity backup is present. Without the
keys, deleting the VM could strand the funds, so it leaves the VMs
running and records why in ``state/deadline.json``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..runs import Run, load_user_config, open_run, utc_now, write_private_json
from .digitalocean import Api, load_token, run_tag
from .inventory import load_inventory

log = logging.getLogger("gnosis_speedtest")

REPO_DIR = Path(__file__).resolve().parent.parent.parent


def unit_name(run: Run) -> str:
    return run_tag(run.config.id)[:200] + "-deadline"


def schedule(run: Run) -> str:
    seconds = int(run.config.infra.deadline_hours * 3600)
    if seconds <= 0:
        raise ValueError("deadline_hours must be positive")
    unit = unit_name(run)
    completed = subprocess.run(
        [
            "systemd-run",
            "--user",
            f"--unit={unit}",
            f"--on-active={seconds}",
            f"--setenv=PYTHONPATH={REPO_DIR}",
            sys.executable,
            "-m",
            "gvpn_bench.remote.deadline",
            str(run.path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"cannot schedule the deletion timer: {completed.stderr.strip()}")
    write_private_json(run.state / "deadline-timer.json", {"unit": unit, "seconds": seconds, "set_at": utc_now()})
    log.info("deletion timer %s fires in %d s", unit, seconds)
    return unit


def cancel(run: Run) -> None:
    subprocess.run(
        ["systemctl", "--user", "stop", _current_unit(run) + ".timer"],
        capture_output=True,
        timeout=30,
        check=False,
    )


def _current_unit(run: Run) -> str:
    path = run.state / "deadline-timer.json"
    base = unit_name(run)
    unit = json.loads(path.read_text())["unit"] if path.exists() else base
    if not isinstance(unit, str) or not re.fullmatch(re.escape(base) + r"(-[0-9]+s)?", unit):
        raise RuntimeError("refusing to control a deletion timer belonging to another run")
    return unit


def reschedule(run: Run, hours: float, *, now: datetime | None = None) -> str:
    """Replace the cap from the original provisioning time, with no unprotected gap."""
    path = run.state / "deadline-timer.json"
    old = json.loads(path.read_text())
    old_unit = _current_unit(run)
    seconds = int(hours * 3600)
    start = datetime.fromisoformat(old["set_at"].replace("Z", "+00:00"))
    if start.tzinfo is None:
        raise ValueError("deletion timer start must include its timezone")
    deadline_at = start + timedelta(seconds=seconds)
    if seconds <= 0 or deadline_at <= (now or datetime.now(UTC)):
        raise ValueError("replacement deletion deadline must be in the future")
    unit = f"{unit_name(run)}-{seconds}s"
    if unit == old_unit:
        raise ValueError("this deletion timeout is already scheduled")
    completed = subprocess.run(
        [
            "systemd-run",
            "--user",
            f"--unit={unit}",
            f"--on-calendar={deadline_at.astimezone(UTC):%Y-%m-%d %H:%M:%S UTC}",
            "--timer-property=AccuracySec=1s",
            f"--setenv=PYTHONPATH={REPO_DIR}",
            sys.executable,
            "-m",
            "gvpn_bench.remote.deadline",
            str(run.path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"cannot install replacement deletion timer: {completed.stderr.strip()}")
    active = subprocess.run(
        ["systemctl", "--user", "is-active", unit + ".timer"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if active.returncode != 0 or active.stdout.strip() != "active":
        raise RuntimeError("replacement deletion timer is not active; original timer retained")
    stopped = subprocess.run(
        ["systemctl", "--user", "stop", old_unit + ".timer"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if stopped.returncode != 0:
        raise RuntimeError("replacement installed, but original deletion timer could not be stopped")
    old_active = subprocess.run(
        ["systemctl", "--user", "is-active", old_unit + ".timer"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if old_active.returncode == 0:
        raise RuntimeError("original deletion timer is still active")
    write_private_json(
        path,
        {
            "unit": unit,
            "seconds": seconds,
            "set_at": old["set_at"],
            "deadline_at": deadline_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "rescheduled_at": utc_now(),
            "previous_unit": old_unit,
        },
    )
    return unit


def missing_backups(run: Run) -> list[str]:
    """Funded clients whose identity backup is not on disk."""
    funding_file = run.state / "funding.json"
    if not funding_file.exists() or (run.state / "cleanup.json").exists():
        return []
    manifest_file = run.state / "backups.json"
    manifest: dict[str, Any] = json.loads(manifest_file.read_text()) if manifest_file.exists() else {}
    missing = []
    for node in json.loads(funding_file.read_text()):
        entry = manifest.get(node["name"])
        files = [Path(p) for p in (entry or {}).get("identity_files", [])]
        if not files or not all(f.exists() for f in files):
            missing.append(node["name"])
    return missing


def fire(run: Run, api: Api) -> dict[str, Any]:
    record: dict[str, Any] = {"fired_at": utc_now(), "deleted": [], "failed": []}
    missing = missing_backups(run)
    if missing:
        record["refused"] = f"funded clients without identity backups: {', '.join(missing)}"
        write_private_json(run.state / "deadline.json", record)
        log.error("deadline: %s; droplets left running", record["refused"])
        return record
    tag = run_tag(run.config.id)
    for machine in load_inventory(run):
        try:
            api.delete_droplet(machine.id, tag)
            record["deleted"].append(machine.name)
        except Exception as error:  # record and continue with the other droplets
            record["failed"].append({"name": machine.name, "error": str(error)[:200]})
    write_private_json(run.state / "deadline.json", record)
    return record


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO)
    user = load_user_config()
    run = open_run(argv[1], user)
    record = fire(run, Api(load_token(user.digitalocean_token_file)))
    print(json.dumps(record))
    return 1 if record.get("failed") or record.get("refused") else 0


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main(sys.argv))
