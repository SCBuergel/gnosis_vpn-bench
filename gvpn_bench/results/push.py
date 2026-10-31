"""Publish a run's results to the separate, private results repository.

``results push RUN`` copies an allowlist of files into a local checkout of
the results repo at ``runs/<date>-<target>-<profile>/``, scans them,
commits, and pushes after asking.

Only ``result.json``, ``report.html`` and ``provenance.json`` are ever
copied. Never logs, run state, inventories, SSH keys or identity backups.

The scan refuses to commit if any copied file contains:

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
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ALLOWLIST = ("result.json", "report.html", "provenance.json")
ALLOWED_PUBLIC_IPS = {"9.9.9.9", "1.1.1.1", "8.8.8.8", "149.112.112.112", "1.0.0.1", "8.8.4.4"}

_PEM_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_KEY_NAME = re.compile(r'"(private_key|chain_key|packet_key|secret|password|mnemonic)"\s*:', re.I)
_HEX32 = re.compile(r"0x[0-9a-fA-F]{64}\b")
_DO_TOKEN = re.compile(r"dop_v1_[A-Za-z0-9]+")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


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


def destination_name(result: dict[str, Any]) -> str:
    run = result["run"]
    stamp = (run.get("started_at") or run["created_at"])[:10]
    target = result["provenance"]["target"]["name"]
    profile = result["provenance"]["profile"]["name"]
    name = f"{stamp}-{target}-{profile}"
    if run["id"].startswith("hist-"):
        name += "-historical"
    return name


def vm_ips_of(run_dir: Path) -> set[str]:
    inventory = run_dir / "state" / "inventory.json"
    if not inventory.exists():
        return set()
    return {m["ip"] for m in json.loads(inventory.read_text()) if m.get("ip")}


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


@dataclass
class PushPlan:
    repo: Path
    destination: Path
    files: list[Path]
    scan: ScanResult


def prepare(run_dir: Path, repo: Path) -> PushPlan:
    result_file = run_dir / "result.json"
    if not result_file.exists():
        raise FileNotFoundError(f"{run_dir} has no result.json")
    from .store import load_result

    result = load_result(run_dir)
    files = [run_dir / name for name in ALLOWLIST if (run_dir / name).exists()]
    scan = scan_files(files, vm_ips_of(run_dir))
    destination = repo / "runs" / destination_name(result)
    return PushPlan(repo=repo, destination=destination, files=files, scan=scan)


def copy_and_commit(plan: PushPlan, message: str) -> bool:
    """Copy the allowlisted files and commit. Returns False if nothing changed."""
    if git(plan.repo, "diff", "--cached", "--name-only"):
        raise RuntimeError(f"{plan.repo} already has staged changes; commit or unstage them first")
    plan.destination.mkdir(parents=True, exist_ok=True)
    for path in plan.files:
        shutil.copyfile(path, plan.destination / path.name)
    relative = plan.destination.relative_to(plan.repo)
    git(plan.repo, "add", "--", *[str(relative / p.name) for p in plan.files])
    if not git(plan.repo, "diff", "--cached", "--name-only"):
        return False
    git(plan.repo, "commit", "-m", message)
    return True


def push(repo: Path) -> None:
    git(repo, "push", "origin", "HEAD")
