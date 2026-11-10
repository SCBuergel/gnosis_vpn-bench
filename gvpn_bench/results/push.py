"""Safety checks and git plumbing for the private results repository.

``publish.py`` decides what is written (only generated reports and the
result data, never logs, run state, inventories, SSH keys or identity
backups). This module scans that content, checks the repository and
commits.

The scan refuses to write if any file contains:

* a PEM private key block, or a JSON key named like a private key;
* a 0x-prefixed 32-byte hex value (the shape of an Ethereum private key);
* a DigitalOcean token (``dop_v1_…``);
* one of this run's VM IP addresses, or any other public IPv4 address
  that is not a well-known resolver used as a ping target.

The tool never creates repositories. It refuses a checkout whose remote
is this (public) bench repository or any repository GitHub reports as
public.
"""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ALLOWED_PUBLIC_IPS = {"9.9.9.9", "1.1.1.1", "8.8.8.8", "149.112.112.112", "1.0.0.1", "8.8.4.4"}

_PEM_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_KEY_NAME = re.compile(r'"(private_key|chain_key|packet_key|secret|password|mnemonic)"\s*:', re.I)
# Lookarounds instead of \b: Markdown puts "_" (a word character) right
# after italic text, which would hide a match from a \b boundary.
_HEX32 = re.compile(r"0x[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_DO_TOKEN = re.compile(r"dop_v1_[A-Za-z0-9]+")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


@dataclass
class ScanResult:
    problems: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.problems


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_global


def scan_text(name: str, text: str, vm_ips: set[str]) -> list[str]:
    problems = []
    if _PEM_KEY.search(text):
        problems.append(f"{name}: contains a PEM private key")
    match = _KEY_NAME.search(text)
    if match:
        problems.append(f"{name}: contains a field named {match.group(1)!r}")
    if _HEX32.search(text):
        problems.append(f"{name}: contains a 0x-prefixed 32-byte hex value (possible private key)")
    if _DO_TOKEN.search(text):
        problems.append(f"{name}: contains a DigitalOcean token")
    for address in sorted(set(_IPV4.findall(text))):
        if address in vm_ips:
            problems.append(f"{name}: contains VM IP {address}")
        elif _is_public(address) and address not in ALLOWED_PUBLIC_IPS:
            problems.append(f"{name}: contains public IP {address}")
    return problems


def scan_files(files: list[Path], vm_ips: set[str]) -> ScanResult:
    result = ScanResult()
    for path in files:
        result.problems += scan_text(path.name, path.read_text(errors="replace"), vm_ips)
    return result


def timestamp_name(when: str) -> str:
    """A UTC timestamp shared by published recording and experiment folder names."""
    instant = datetime.fromisoformat(when.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("result timestamps must include a timezone")
    return instant.astimezone(UTC).strftime("%Y-%m-%d--%H-%M-%S")


def destination_name(result: dict[str, Any]) -> str:
    """Timestamp-only names for ordinary recordings; stable slots for live experiments.

    Ordinary recordings use the start time. Experiment recordings use the
    creation time and cycle suffix so pending results can be updated in place.
    """
    run = result["run"]
    if not run.get("experiment"):
        return timestamp_name(run.get("started_at") or run["created_at"])
    when = run["created_at"] if run.get("experiment") else run.get("started_at") or run["created_at"]
    stamp = f"{when[:10]}-{when[11:13]}{when[14:16]}{when[17:19]}Z"
    target = result["provenance"]["target"]["name"]
    profile = result["provenance"]["profile"]["name"]
    name = f"{stamp}-{target}-{profile}"
    if run.get("experiment"):
        name += f"-cycle{run['experiment']['cycle']}"
    if run["id"].startswith("hist-"):
        name += "-historical"
    return name


def vm_ips_of(result_dir: Path) -> set[str]:
    """VM IPs of the run that owns *result_dir* (``RUN`` or ``RUN/results/<target>``)."""
    for candidate in (result_dir, *result_dir.parents[:2]):
        inventory = candidate / "state" / "inventory.json"
        if inventory.exists():
            return {m["ip"] for m in json.loads(inventory.read_text()) if m.get("ip")}
    return set()


def git(repo: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)
    if check and completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {completed.stderr.strip()}")
    return completed.stdout.strip()


_GITHUB_REMOTE = re.compile(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$")


def github_slug(remote_url: str) -> str | None:
    match = _GITHUB_REMOTE.search(remote_url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def github_is_public(slug: str) -> bool | None:
    """True if anonymous GitHub API access sees the repo; None if unknown."""
    try:
        with urllib.request.urlopen(f"https://api.github.com/repos/{slug}", timeout=20) as response:
            return json.load(response).get("private") is False
    except urllib.error.HTTPError as error:
        return False if error.code == 404 else None
    except OSError:
        return None


def check_results_repo(repo: Path) -> str:
    """Return the push remote URL; raise unless the checkout is safe to publish to."""
    if not (repo / ".git").exists():
        raise RuntimeError(
            f"{repo} is not a git checkout. Create the private results repository yourself "
            "(this tool never creates repositories), clone it there, and retry."
        )
    remote = git(repo, "remote", "get-url", "origin", check=False)
    if not remote:
        raise RuntimeError(f"{repo} has no 'origin' remote")
    slug = github_slug(remote)
    if slug and slug.lower().endswith("/gnosis_vpn-bench"):
        raise RuntimeError("refusing to publish results into the bench repository itself")
    if slug:
        public = github_is_public(slug)
        if public is True:
            raise RuntimeError(f"{slug} is public; results go only to a private repository")
        if public is None:
            raise RuntimeError(f"cannot confirm {slug} is private (GitHub API unreachable); not pushing")
    return remote


def commit(repo: Path, files: list[str], message: str, update: list[str] | None = None) -> bool:
    """Add exactly *files*, record deletions of tracked files under *update*, commit.

    ``git add -u`` never adds untracked files, so nothing that was not
    generated and scanned can be committed. Returns False if nothing changed.
    """
    if git(repo, "diff", "--cached", "--name-only"):
        raise RuntimeError(f"{repo} already has staged changes; commit or unstage them first")
    git(repo, "add", "--", *files)
    if update:
        git(repo, "add", "-u", "--", *update)
    if not git(repo, "diff", "--cached", "--name-only"):
        return False
    git(repo, "commit", "-m", message)
    return True


def push(repo: Path) -> None:
    git(repo, "push", "origin", "HEAD")
