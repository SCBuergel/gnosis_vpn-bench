"""Back up client identities and v5 payment state outside the repository.

Backups go to ``<identity_backup_dir>/<network>/<run id>/<client>/`` with
mode 600 files in 700 directories. ``state/backups.json`` lists them so
the deadline timer and recovery can check they exist.

Rules carried over from the ladder harness:

* back up identities *before* funding; a later backup that differs from
  the first one is an error (the identity changed under us);
* validate a payment-state archive completely before replacing the last
  good copy;
* the Safe file (v4 ``gnosisvpn-hopr.safe``, v5 per-network ``hopr.safe``)
  is backed up as soon as it exists (after funding): an identity
  without it can attach to the wrong Safe.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import os
import tarfile
import threading
from pathlib import Path
from typing import Any

from ..host import Host, run_script
from ..profiles import Profile
from ..runs import Run, UserConfig, write_private_json

_READ_FILES = """
import base64, json, sys
from pathlib import Path
out = {}
for path in sys.argv[1:]:
    p = Path(path)
    if p.is_file():
        out[p.name] = base64.b64encode(p.read_bytes()).decode()
print(json.dumps(out))
"""

_PAYMENT_ARCHIVE = 'tar -C "$1" -czf - pix | base64 -w0\n'


def backup_dir(user: UserConfig, run: Run, profile: Profile, client: str) -> Path:
    path = user.identity_backup_dir
    for part in (profile.network.name, run.config.id, client):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path, 0o700)
        path = path / part
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_private(path: Path, payload: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
    tmp.replace(path)


def fetch_files(host: Host, paths: list[str]) -> dict[str, bytes]:
    result = host.run(["python3", "-c", _READ_FILES, *paths], timeout_s=60)
    if not result.ok:
        raise RuntimeError(f"{host.name}: cannot read identity files: {result.stderr.strip()[:200]}")
    return {name: base64.b64decode(data) for name, data in json.loads(result.stdout).items()}


def backup_identity(
    user: UserConfig, run: Run, profile: Profile, host: Host, *, require_safe: bool = False
) -> list[Path]:
    """Back up the key files (required) and the Safe file (once it exists).

    Called before funding (only the keys exist) and again after connecting,
    when the Safe file must have appeared (``require_safe``).
    """
    worker = profile.worker
    network = profile.network.name
    wanted = [f"{worker.identity_dir(network)}/{name}" for name in worker.identity_files()]
    safe_path = worker.safe_file(network)
    files = fetch_files(host, [*wanted, safe_path])
    if require_safe and Path(safe_path).name not in files:
        raise RuntimeError(f"{host.name}: Safe file {safe_path} missing after funding")
    missing = set(worker.identity_files()) - set(files)
    if missing:
        raise RuntimeError(f"{host.name}: incomplete identity backup, missing {sorted(missing)}")
    directory = backup_dir(user, run, profile, host.name)
    saved = []
    for name, payload in files.items():
        target = directory / name
        if target.exists() and target.read_bytes() != payload:
            raise RuntimeError(f"{host.name}: {name} changed since the first backup; refusing to overwrite")
        _write_private(target, payload)
        saved.append(target)
    _record(run, host.name, identity_files=[str(p) for p in saved])
    return saved


def validate_payment_archive(payload: bytes) -> None:
    """Accept only a complete tar.gz of regular files and dirs under ``pix/``."""
    raw = gzip.decompress(payload)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        members = archive.getmembers()
        if not any(m.isfile() and m.name.startswith("pix/") for m in members):
            raise ValueError("payment-state archive has no database files")
        for member in members:
            if member.name != "pix" and not member.name.startswith("pix/"):
                raise ValueError(f"unexpected payment archive path {member.name!r}")
            if ".." in member.name.split("/") or not (member.isdir() or member.isfile()):
                raise ValueError(f"unsafe payment archive member {member.name!r}")
            if member.isfile():
                stream = archive.extractfile(member)
                if stream is None or len(stream.read()) != member.size:
                    raise ValueError("incomplete payment-state archive")


def backup_payment_state(user: UserConfig, run: Run, profile: Profile, host: Host) -> Path | None:
    """v5 only: save the PIX database; the previous good copy survives a bad archive."""
    source = profile.worker.payment_dir(profile.network.name)
    if source is None:
        return None
    result = run_script(host, _PAYMENT_ARCHIVE, [source], timeout_s=120)
    if not result.ok:
        raise RuntimeError(f"{host.name}: payment-state archive failed: {result.stderr.strip()[:200]}")
    payload = base64.b64decode(result.stdout.strip(), validate=True)
    validate_payment_archive(payload)
    target = backup_dir(user, run, profile, host.name) / "pix-state.tar.gz"
    _write_private(target, payload)
    _record(run, host.name, payment_state=str(target))
    return target


def save_safe_info(user: UserConfig, run: Run, profile: Profile, client: str, info: dict[str, Any]) -> None:
    target = backup_dir(user, run, profile, client) / "safe-info.json"
    _write_private(target, json.dumps(info, indent=2).encode())
    _record(run, client, safe_info=str(target))


_MANIFEST_LOCK = threading.Lock()


def _record(run: Run, client: str, **fields: Any) -> None:
    """Update one client's entry; clients back up in parallel threads."""
    with _MANIFEST_LOCK:
        manifest_file = run.state / "backups.json"
        manifest = json.loads(manifest_file.read_text()) if manifest_file.exists() else {}
        manifest.setdefault(client, {}).update(fields)
        write_private_json(manifest_file, manifest)


def manifest(run: Run) -> dict[str, Any]:
    path = run.state / "backups.json"
    return json.loads(path.read_text()) if path.exists() else {}
