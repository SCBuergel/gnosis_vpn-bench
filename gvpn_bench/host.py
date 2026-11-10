"""One way to run commands on a machine: locally or over SSH.

Every measurement and VPN-control call goes through a ``Host``. Local mode
and remote mode therefore share all mode code; only the host differs.

Commands are always argument vectors, never shell strings. ``SshHost``
quotes the vector with ``shlex.join`` so the remote shell receives the
same arguments that ``LocalHost`` passes to ``subprocess``.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger("gnosis_speedtest")

# Return codes for failures that happen before the command produces one.
RC_TIMEOUT = -1
RC_NOT_FOUND = -127

# curl exits 28 when --max-time expires; that still yields a usable speed.
_QUIET_RETURN_CODES = (0, 28)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def timed_out(self) -> bool:
        return self.returncode == RC_TIMEOUT


class Host(Protocol):
    """A machine that can run argument vectors."""

    name: str

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float = 30,
        input_bytes: bytes | None = None,
        expected_failure: bool = False,
    ) -> CommandResult: ...

    def popen(self, argv: Sequence[str], *, merge_stderr: bool = False) -> subprocess.Popen[bytes]:
        """Start *argv* with stdout (and stderr) as byte pipes for streaming."""
        ...

    def privileged(self, argv: Sequence[str]) -> list[str]:
        """Return *argv* wrapped so it runs as root on this host."""
        ...


def _run_local(
    argv: Sequence[str], timeout_s: float, input_bytes: bytes | None, expected_failure: bool = False
) -> CommandResult:
    """Run *argv* with subprocess; never raises for timeouts or missing tools.

    *expected_failure* logs a non-zero exit at DEBUG: polling and probes that
    fail until something is ready must not flood the run log with warnings.
    """
    try:
        completed = subprocess.run(
            list(argv),
            input=input_bytes,
            capture_output=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        log.error("CMD timed out after %ss: %s", timeout_s, shlex.join(argv))
        return CommandResult(RC_TIMEOUT, "", "process_timeout")
    except FileNotFoundError:
        log.error("CMD not found: %s", argv[0])
        return CommandResult(RC_NOT_FOUND, "", "not_found")
    result = CommandResult(
        completed.returncode,
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
    )
    if result.returncode in _QUIET_RETURN_CODES or expected_failure:
        log.debug(
            "CMD rc=%d stdout_len=%d stderr: %s", result.returncode, len(result.stdout), result.stderr.strip()[:300]
        )
    else:
        log.warning("CMD exited %d | stderr: %s", result.returncode, result.stderr.strip()[:300])
    return result


@dataclass
class LocalHost:
    """The machine this process runs on. Root commands go through sudo."""

    name: str = "local"

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float = 30,
        input_bytes: bytes | None = None,
        expected_failure: bool = False,
    ) -> CommandResult:
        log.debug("CMD[%s]: %s", self.name, shlex.join(argv))
        return _run_local(argv, timeout_s, input_bytes, expected_failure)

    def popen(self, argv: Sequence[str], *, merge_stderr: bool = False) -> subprocess.Popen[bytes]:
        log.debug("POPEN[%s]: %s", self.name, shlex.join(argv))
        return subprocess.Popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            bufsize=0,
        )

    def privileged(self, argv: Sequence[str]) -> list[str]:
        return ["sudo", *argv]


@dataclass
class SshHost:
    """A remote machine reached with a dedicated key and known_hosts file.

    BatchMode prevents password prompts. A per-run known_hosts file keeps
    disposable VM host keys out of the user's own ~/.ssh/known_hosts.
    """

    name: str
    address: str
    user: str = "root"
    identity_file: Path | None = None
    known_hosts_file: Path | None = None
    connect_timeout_s: int = 10
    control_dir: Path | None = None
    extra_options: list[str] = field(default_factory=list)

    def _multiplex_options(self) -> list[str]:
        """Reuse one TCP/SSH session for many short commands (e.g. 1 Hz ping)."""
        if self.control_dir is None:
            return []
        return [
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={self.control_dir}/%C",
            "-o",
            "ControlPersist=60",
        ]

    def ssh_argv(self, remote_argv: Sequence[str] | None = None) -> list[str]:
        """Build the local ssh argv; *remote_argv* is quoted for the remote shell."""
        argv = ["ssh", "-T", "-o", "BatchMode=yes"]
        if self.identity_file is not None:
            argv += ["-i", str(self.identity_file), "-o", "IdentitiesOnly=yes"]
        if self.known_hosts_file is not None:
            argv += [
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                f"UserKnownHostsFile={self.known_hosts_file}",
            ]
        argv += [
            "-o",
            f"ConnectTimeout={self.connect_timeout_s}",
            "-o",
            "ServerAliveInterval=10",
            "-o",
            "ServerAliveCountMax=3",
            *self._multiplex_options(),
            *self.extra_options,
            f"{self.user}@{self.address}",
        ]
        if remote_argv is not None:
            argv.append(shlex.join(remote_argv))
        return argv

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float = 30,
        input_bytes: bytes | None = None,
        expected_failure: bool = False,
    ) -> CommandResult:
        log.debug("CMD[%s]: %s", self.name, shlex.join(argv))
        result = _run_local(self.ssh_argv(argv), timeout_s, input_bytes, expected_failure)
        if result.returncode == 255 and not expected_failure:
            log.error("ssh to %s failed: %s", self.name, result.stderr.strip()[:300])
        return result

    def popen(self, argv: Sequence[str], *, merge_stderr: bool = False) -> subprocess.Popen[bytes]:
        log.debug("POPEN[%s]: %s", self.name, shlex.join(argv))
        return subprocess.Popen(
            self.ssh_argv(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            bufsize=0,
        )

    def privileged(self, argv: Sequence[str]) -> list[str]:
        if self.user == "root":
            return list(argv)
        return ["sudo", "-n", *argv]


def run_script(
    host: Host,
    script: str,
    args: Sequence[str] = (),
    *,
    timeout_s: float = 90,
) -> CommandResult:
    """Run a fixed bash script on *host* (sent on stdin) with *args* as ``$1 $2 ...``.

    Scripts are static text. Values always travel as positional
    arguments, never by substituting them into the script body.
    """
    return host.run(
        ["bash", "-euo", "pipefail", "-s", "--", *args],
        timeout_s=timeout_s,
        input_bytes=script.encode(),
    )


def write_file(host: Host, path: str, data: bytes, *, mode: str = "600", timeout_s: float = 60) -> None:
    """Write *data* to *path* on *host* atomically with the given octal mode."""
    script = 'umask 077; tmp="$1.tmp.$$"; cat > "$tmp"; chmod "$2" "$tmp"; mv -f "$tmp" "$1"\n'
    result = host.run(
        ["sh", "-c", script, "write_file", path, mode],
        timeout_s=timeout_s,
        input_bytes=data,
    )
    if not result.ok:
        raise RuntimeError(f"{host.name}: cannot write {path}: {result.stderr.strip()[:200]}")


def read_file(host: Host, path: str, *, timeout_s: float = 30) -> str:
    """Read a text file on *host*; raise if it cannot be read."""
    result = host.run(["cat", "--", path], timeout_s=timeout_s)
    if not result.ok:
        raise RuntimeError(f"{host.name}: cannot read {path}: {result.stderr.strip()[:200]}")
    return result.stdout
