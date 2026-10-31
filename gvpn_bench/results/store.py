"""Write and read a run's result.json, provenance.json and report.html."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ..profiles import Profile, Target
from .render import render
from .schema import validate

BENCH_VERSION = "0.2.0"
REPO_DIR = Path(__file__).resolve().parent.parent.parent


def bench_commit() -> str | None:
    """The checkout's commit, with ``-dirty`` when there are local changes."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(REPO_DIR), "describe", "--always", "--dirty", "--abbrev=12"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() or None


def profile_provenance(profile: Profile) -> dict[str, Any]:
    return {
        "name": profile.name,
        "network": profile.network.name,
        "channel": profile.package.channel,
        "package_version": profile.package.version,
        "package_sha256": profile.package.sha256,
        "worker_model": profile.worker.kind,
    }


def target_provenance(target: Target) -> dict[str, Any]:
    return {
        "name": target.name,
        "label": target.label,
        "exit_address": target.exit_address,
        "network": target.network,
        "location": target.location,
    }


def write_result(run_dir: Path, result: dict[str, Any]) -> Path:
    """Validate, then write result.json and provenance.json atomically."""
    validate(result)
    for name, payload in (("result.json", result), ("provenance.json", result["provenance"])):
        path = run_dir / name
        tmp = path.with_name(name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(path)
    return run_dir / "result.json"


def load_result(run_dir: Path) -> dict[str, Any]:
    result = json.loads((run_dir / "result.json").read_text())
    validate(result)
    return result


def write_report(run_dir: Path, output: Path | None = None) -> Path:
    html = render(load_result(run_dir))
    output = output or run_dir / "report.html"
    output.write_text(html)
    return output
