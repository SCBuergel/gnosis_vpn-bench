"""A run's machines: the inventory file and SSH access to each machine."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from ..host import SshHost
from ..runs import Run, write_private_json


@dataclass
class Machine:
    name: str
    role: str  # "origin" or "client"
    id: int
    size: str
    region: str
    created_at: str
    ip: str | None = None


def inventory_file(run: Run) -> Path:
    return run.state / "inventory.json"


def load_inventory(run: Run) -> list[Machine]:
    path = inventory_file(run)
    if not path.exists():
        return []
    return [Machine(**entry) for entry in json.loads(path.read_text())]


def save_inventory(run: Run, machines: list[Machine]) -> None:
    write_private_json(inventory_file(run), [asdict(m) for m in machines])


def ssh_key_file(run: Run) -> Path:
    return run.state / "id_ed25519"


def ssh_control_dir() -> Path:
    """Short, private directory for SSH control sockets.

    Unix socket paths are limited to ~108 bytes, so the sockets cannot live
    in the (long) run directory.
    """
    path = Path(tempfile.gettempdir()) / f"gvpnb-{os.getuid()}"
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError(f"{path} is not a private directory owned by this user")
    return path


def host_for(run: Run, machine: Machine) -> SshHost:
    if machine.ip is None:
        raise RuntimeError(f"{machine.name} has no IP yet")
    control = ssh_control_dir()
    return SshHost(
        name=machine.name,
        address=machine.ip,
        user="root",
        identity_file=ssh_key_file(run),
        known_hosts_file=run.state / "known_hosts",
        control_dir=control,
    )


def machine(run: Run, name: str) -> Machine:
    for entry in load_inventory(run):
        if entry.name == name:
            return entry
    raise KeyError(f"no machine {name!r} in this run's inventory")


def clients(run: Run) -> list[Machine]:
    return [m for m in load_inventory(run) if m.role == "client"]
