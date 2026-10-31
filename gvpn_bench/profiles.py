"""Client profiles, networks and targets, loaded from TOML files.

A *profile* combines four independent choices:

* network   — which HOPR network the client joins (``networks.toml``)
* channel   — the package channel (stable / experimental)
* package   — an exact version + sha256 pin of the .deb
* worker    — the client generation's process model (see ``WorkerModel``)

A *target* names one exit (address, network, label). A run pairs a
profile with a target; their networks must match, so an exit is never
tested on a network it is not registered on.

Nothing is read at import time. Callers pass a config root (the repo's
directory by default) and get frozen dataclasses back.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

CONFIG_ROOT = Path(__file__).resolve().parent.parent

_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_ADDRESS = re.compile(r"0x[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

WorkerKind = Literal["v4-root", "v5-user"]


def _check_name(kind: str, value: str) -> str:
    if not _NAME.fullmatch(value):
        raise ValueError(f"invalid {kind} name {value!r}")
    return value


@dataclass(frozen=True)
class Network:
    name: str
    indexer: str


@dataclass(frozen=True)
class PackagePin:
    channel: str
    version: str | None
    url: str | None
    sha256: str | None

    @property
    def pinned(self) -> bool:
        return bool(self.version and self.url and self.sha256)


@dataclass(frozen=True)
class WorkerModel:
    """Where a client generation keeps its worker, identity and payment state.

    v4 (``v4-root``): one root-owned worker. ``gnosis_vpn-ctl`` runs as
    root; identity files live in ``/var/lib/gnosisvpn/.config``.

    v5 (``v5-user``): a worker per Unix user. Root cannot own one, so a
    dedicated non-root user is created and every control call goes through
    ``runuser -u <user> --``. Identity and PIX payment state live under
    that user's home, per network.
    """

    kind: WorkerKind
    user: str
    home: str

    def ctl_prefix(self) -> list[str]:
        if self.kind == "v4-root":
            return []
        return ["runuser", "-u", self.user, "--"]

    def identity_dir(self, network: str) -> str:
        if self.kind == "v4-root":
            return "/var/lib/gnosisvpn/.config"
        return f"{self.home}/.local/share/gnosisvpn/networks/{network}"

    def identity_files(self) -> tuple[str, ...]:
        if self.kind == "v4-root":
            return ("gnosisvpn-hopr.id", "gnosisvpn-hopr.pass", "gnosisvpn-hopr.safe")
        return ("hopr.id", "hopr.pass")

    def safe_cache_file(self, network: str) -> str | None:
        """v5 caches the Safe address per network; it must travel with the identity."""
        if self.kind == "v4-root":
            return None
        return f"{self.home}/.cache/gnosisvpn/networks/{network}/hopr.safe"

    def payment_dir(self, network: str) -> str | None:
        """Directory containing the ``pix/`` payment database (v5 only)."""
        if self.kind == "v4-root":
            return None
        return self.identity_dir(network)

    def worker_log_glob(self) -> str:
        if self.kind == "v4-root":
            return "/var/log/gnosisvpn/gnosisvpn.log"
        return f"{self.home}/.local/state/gnosisvpn/worker.log.*"


@dataclass(frozen=True)
class Profile:
    name: str
    network: Network
    package: PackagePin
    worker: WorkerModel
    description: str
    client_config_path: str | None = None


@dataclass(frozen=True)
class Target:
    name: str
    label: str
    exit_address: str
    network: str
    profile: str | None
    location: str | None
    last_verified: date | None
    notes: str


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def load_networks(root: Path = CONFIG_ROOT) -> dict[str, Network]:
    data = _read_toml(root / "networks.toml")
    return {
        _check_name("network", name): Network(name=name, indexer=str(entry["indexer"]).rstrip("/"))
        for name, entry in data.items()
    }


def _worker(data: dict[str, Any]) -> WorkerModel:
    kind = data["model"]
    if kind == "v4-root":
        return WorkerModel(kind="v4-root", user="root", home="/var/lib/gnosisvpn")
    if kind == "v5-user":
        user = _check_name("worker user", str(data["user"]))
        if user == "root":
            raise ValueError("v5 workers cannot be owned by root")
        home = str(data.get("home") or f"/var/lib/gnosisvpn/{user}-home")
        return WorkerModel(kind="v5-user", user=user, home=home)
    raise ValueError(f"unknown worker model {kind!r}")


def _package(data: dict[str, Any]) -> PackagePin:
    sha256 = data.get("sha256")
    if sha256 is not None and not _SHA256.fullmatch(sha256):
        raise ValueError("package sha256 must be 64 lowercase hex characters")
    channel = str(data["channel"])
    if channel not in {"stable", "experimental"}:
        raise ValueError(f"unknown channel {channel!r}")
    return PackagePin(
        channel=channel,
        version=data.get("version"),
        url=data.get("url"),
        sha256=sha256,
    )


def load_profile(name: str, root: Path = CONFIG_ROOT) -> Profile:
    _check_name("profile", name)
    path = root / "profiles" / f"{name}.toml"
    if not path.exists():
        available = ", ".join(list_names(root / "profiles")) or "none"
        raise ValueError(f"unknown profile {name!r} (available: {available})")
    data = _read_toml(path)
    networks = load_networks(root)
    network_name = data["network"]
    if network_name not in networks:
        raise ValueError(f"profile {name}: unknown network {network_name!r}")
    return Profile(
        name=name,
        network=networks[network_name],
        package=_package(data["package"]),
        worker=_worker(data["worker"]),
        description=str(data.get("description", "")),
        client_config_path=data.get("client_config_path"),
    )


def load_target(name: str, root: Path = CONFIG_ROOT) -> Target:
    _check_name("target", name)
    path = root / "targets" / f"{name}.toml"
    if not path.exists():
        available = ", ".join(list_names(root / "targets")) or "none"
        raise ValueError(f"unknown target {name!r} (available: {available})")
    data = _read_toml(path)
    address = str(data["exit_address"]).lower()
    if not _ADDRESS.fullmatch(address):
        raise ValueError(f"target {name}: invalid exit address")
    if data["network"] not in load_networks(root):
        raise ValueError(f"target {name}: unknown network {data['network']!r}")
    verified = data.get("last_verified")
    return Target(
        name=name,
        label=str(data["label"]),
        exit_address=address,
        network=str(data["network"]),
        profile=data.get("profile"),
        location=data.get("location"),
        last_verified=verified if isinstance(verified, date) else None,
        notes=str(data.get("notes", "")),
    )


def list_names(directory: Path) -> list[str]:
    return sorted(path.stem for path in directory.glob("*.toml"))


def check_compatible(profile: Profile, target: Target) -> None:
    """Refuse a profile/target pair on different networks."""
    if profile.network.name != target.network:
        raise ValueError(
            f"target {target.name} is on {target.network}, "
            f"but profile {profile.name} joins {profile.network.name}"
        )
