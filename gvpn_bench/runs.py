"""Run directories and user configuration.

One run = one directory = one ``run.toml``. Every later command takes only
the run (id or path); nothing has to be repeated through environment
variables.

Layout::

    <runs_dir>/<id>/
        run.toml         what to test: profile, target, VM sizes, ladder spec
        state/   (0700)  private: SSH key, inventory, step journal, tx ledger
        data/            raw measurements collected from the VMs
        result.json      versioned result (docs/result-schema.md)
        report.html      rendered from result.json
        provenance.json  copy of result.json's provenance block

The runs directory defaults to ``~/.local/state/gvpn-bench/runs``, outside
the (public) repository. Identity and payment backups go to a separate
directory, also outside the repository.

User configuration lives in ``~/.config/gvpn-bench/config.toml``. It only
points at files; the user's existing credential files are never moved.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .profiles import CONFIG_ROOT, Profile, Target, check_compatible, load_profile, load_target

log = logging.getLogger("gnosis_speedtest")

USER_CONFIG = Path("~/.config/gvpn-bench/config.toml")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")

STEPS = ("plan", "provision", "fund", "connect", "ladder", "sweep", "recover", "destroy")


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class UserConfig:
    runs_dir: Path
    identity_backup_dir: Path
    digitalocean_token_file: Path
    funding_key_file: Path
    results_repo: Path | None
    source: Path | None


def load_user_config(path: Path | None = None) -> UserConfig:
    """Read ``~/.config/gvpn-bench/config.toml``; every key has a default."""
    path = (path or USER_CONFIG).expanduser()
    data: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    credentials = data.get("credentials", {})
    results = data.get("results", {})

    def p(value: str) -> Path:
        return Path(value).expanduser()

    return UserConfig(
        runs_dir=p(data.get("runs_dir", "~/.local/state/gvpn-bench/runs")),
        identity_backup_dir=p(data.get("identity_backup_dir", "~/.local/state/gvpn-bench/identities")),
        # Defaults match where the ladder tools expected these files.
        digitalocean_token_file=p(credentials.get("digitalocean_token_file", "~/do-api-token")),
        funding_key_file=p(credentials.get("funding_key_file", "~/key.txt")),
        results_repo=p(results["repo"]) if "repo" in results else None,
        source=path if path.exists() else None,
    )


def check_private_file(path: Path) -> list[str]:
    """Return warnings if *path* is missing or readable by group/others."""
    if not path.exists():
        return [f"{path} does not exist"]
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return [f"{path} has mode {mode:03o}; credentials should be 600 (chmod 600 {path})"]
    return []


def read_secret(path: Path) -> str:
    for warning in check_private_file(path):
        log.warning("credential: %s", warning)
    return path.read_text()


@dataclass(frozen=True)
class InfraSpec:
    provider: str = "digitalocean"
    region: str = "fra1"
    image: str = "ubuntu-24-04-x64"
    client_size: str = "c-4"
    origin_size: str = "c-2"
    clients: int = 3
    deadline_hours: float = 4.0


@dataclass(frozen=True)
class LadderSpec:
    transfer_bytes: int = 200_000_000
    deadline_s: int = 300
    gap_s: int = 10
    rungs: tuple[int, ...] = (1, 2, 3)
    ping_target: str = "9.9.9.9"
    ping_interval_s: float = 1.0


@dataclass(frozen=True)
class RunConfig:
    id: str
    created_at: str
    profile_name: str
    target_name: str
    infra: InfraSpec
    ladder: LadderSpec
    client_config: Path | None
    notes: str

    def profile(self, root: Path = CONFIG_ROOT) -> Profile:
        return load_profile(self.profile_name, root)

    def target(self, root: Path = CONFIG_ROOT) -> Target:
        return load_target(self.target_name, root)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(str(value))


def render_run_toml(config: RunConfig) -> str:
    lines = [
        "# One run of gnosis_vpn-bench. Edit before `provision`; later steps read it as-is.",
        f"id = {_toml_value(config.id)}",
        f"created_at = {_toml_value(config.created_at)}",
        f"profile = {_toml_value(config.profile_name)}",
        f"target = {_toml_value(config.target_name)}",
    ]
    if config.client_config is not None:
        lines.append(f"client_config = {_toml_value(str(config.client_config))}")
    lines.append(f"notes = {_toml_value(config.notes)}")
    lines.append("")
    lines.append("[infrastructure]")
    for key, value in vars(config.infra).items():
        lines.append(f"{key} = {_toml_value(value)}")
    lines.append("")
    lines.append("[ladder]")
    for key, value in vars(config.ladder).items():
        lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def parse_run_toml(text: str) -> RunConfig:
    data = tomllib.loads(text)
    run_id = str(data["id"])
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"invalid run id {run_id!r}")
    infra = InfraSpec(**data.get("infrastructure", {}))
    ladder_data = dict(data.get("ladder", {}))
    if "rungs" in ladder_data:
        ladder_data["rungs"] = tuple(ladder_data["rungs"])
    ladder = LadderSpec(**ladder_data)
    if infra.clients < max(ladder.rungs, default=1):
        raise ValueError("infrastructure.clients is smaller than the largest ladder rung")
    client_config = data.get("client_config")
    return RunConfig(
        id=run_id,
        created_at=str(data["created_at"]),
        profile_name=str(data["profile"]),
        target_name=str(data["target"]),
        infra=infra,
        ladder=ladder,
        client_config=Path(client_config).expanduser() if client_config else None,
        notes=str(data.get("notes", "")),
    )


@dataclass
class Run:
    """A run directory on disk."""

    path: Path
    config: RunConfig
    _steps: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def state(self) -> Path:
        return self.path / "state"

    @property
    def data(self) -> Path:
        return self.path / "data"

    @property
    def result_file(self) -> Path:
        return self.path / "result.json"

    @property
    def report_file(self) -> Path:
        return self.path / "report.html"

    def ensure_dirs(self) -> None:
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        self.data.mkdir(parents=True, exist_ok=True)

    # -- step journal -------------------------------------------------

    @property
    def steps_file(self) -> Path:
        return self.state / "steps.json"

    def steps(self) -> dict[str, Any]:
        if self.steps_file.exists():
            return json.loads(self.steps_file.read_text())
        return {}

    def mark(self, step: str, **details: Any) -> None:
        if step not in STEPS:
            raise ValueError(f"unknown step {step}")
        steps = self.steps()
        steps[step] = {"done_at": utc_now(), **details}
        write_private_json(self.steps_file, steps)

    def done(self, step: str) -> bool:
        return step in self.steps()


def write_private_json(path: Path, payload: Any) -> None:
    """Atomically write JSON readable only by the owner."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    Path(tmp_name).replace(path)


def resolve_run(ref: str, user: UserConfig) -> Path:
    """Accept a run id (looked up in runs_dir) or a path to a run directory."""
    candidate = Path(ref).expanduser()
    if (candidate / "run.toml").exists():
        return candidate.resolve()
    by_id = user.runs_dir / ref
    if (by_id / "run.toml").exists():
        return by_id
    raise FileNotFoundError(f"no run.toml at {candidate} or {by_id}")


def open_run(ref: str, user: UserConfig) -> Run:
    path = resolve_run(ref, user)
    config = parse_run_toml((path / "run.toml").read_text())
    if config.id != path.name:
        raise ValueError(f"run.toml id {config.id!r} does not match directory {path.name!r}")
    run = Run(path=path, config=config)
    run.ensure_dirs()
    return run


def default_run_id(profile: str, target: str, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    return f"{now:%Y%m%d-%H%M%S}-{target}-{profile}"


def plan_run(
    user: UserConfig,
    profile_name: str,
    target_name: str,
    *,
    run_id: str | None = None,
    client_config: Path | None = None,
    notes: str = "",
    root: Path = CONFIG_ROOT,
) -> Run:
    """Create a run directory with run.toml. No network calls, no spending."""
    profile = load_profile(profile_name, root)
    target = load_target(target_name, root)
    check_compatible(profile, target)
    run_id = run_id or default_run_id(profile_name, target_name)
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"invalid run id {run_id!r}")
    client_config = client_config or target.client_config
    if client_config is not None and not client_config.exists():
        raise FileNotFoundError(client_config)
    path = user.runs_dir / run_id
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    config = RunConfig(
        id=run_id,
        created_at=utc_now(),
        profile_name=profile_name,
        target_name=target_name,
        infra=InfraSpec(),
        ladder=LadderSpec(),
        client_config=client_config.resolve() if client_config else None,
        notes=notes,
    )
    path.mkdir(parents=True)
    (path / "run.toml").write_text(render_run_toml(config))
    run = Run(path=path, config=config)
    run.ensure_dirs()
    run.mark("plan", profile=profile_name, target=target_name)
    return run
