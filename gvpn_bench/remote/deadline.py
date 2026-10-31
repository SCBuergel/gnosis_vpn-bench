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
import subprocess
import sys
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
    write_private_json(
        run.state / "deadline-timer.json", {"unit": unit, "seconds": seconds, "set_at": utc_now()}
    )
    log.info("deletion timer %s fires in %d s", unit, seconds)
    return unit


def cancel(run: Run) -> None:
    subprocess.run(
        ["systemctl", "--user", "stop", unit_name(run) + ".timer"],
        capture_output=True,
        timeout=30,
        check=False,
    )


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
