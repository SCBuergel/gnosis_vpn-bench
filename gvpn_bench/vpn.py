"""Gnosis VPN control (status / connect / disconnect) on any ``Host``.

``VpnClient`` wraps ``gnosis_vpn-ctl`` for one host and one worker model:
locally it calls the CLI directly; on a v5 VM it prefixes
``runuser -u <user> --`` so the per-user worker is addressed.

Responses are inspected as JSON. A zero exit code alone never counts as
success.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .host import Host
from .settings import CONNECTION_TIMEOUT_S, POLL_INTERVAL_S
from .status import ClientStatus, Destination, parse_status

log = logging.getLogger("gnosis_speedtest")

CTL = "gnosis_vpn-ctl"
# Older builds take ``--json``; 0.97+ (v4 stable) and v5 take ``-o json``.
JSON_FLAG_CANDIDATES: tuple[tuple[str, ...], ...] = (("-o", "json"), ("--json",))

DnsMode = Literal["auto", "always", "never"]
DNS_RESOLVER_LINE = b"nameserver 1.1.1.1\n"
DNS_CHECK_NAME = "speed.cloudflare.com"


class CtlError(RuntimeError):
    pass


@dataclass
class VpnClient:
    host: Host
    ctl_prefix: list[str] = field(default_factory=list)
    dns_mode: DnsMode = "auto"
    json_flag: tuple[str, ...] | None = None
    _saved_resolv_conf: bytes | None = field(default=None, init=False, repr=False)

    # -- raw control ---------------------------------------------------

    def _argv(self, args: Sequence[str], json_flag: Sequence[str]) -> list[str]:
        return [*self.ctl_prefix, CTL, *json_flag, *args]

    def _detect_json_flag(self) -> tuple[str, ...]:
        errors: list[str] = []
        for candidate in JSON_FLAG_CANDIDATES:
            result = self.host.run(self._argv(["status"], candidate), timeout_s=15)
            if result.stdout.lstrip().startswith("{"):
                log.debug("%s: using JSON flag %s", self.host.name, " ".join(candidate))
                return candidate
            errors.append(f"{' '.join(candidate)}: rc={result.returncode} {result.stderr.strip()[:120]}")
        raise CtlError(f"{self.host.name}: no JSON status from {CTL} ({'; '.join(errors)})")

    def ctl(self, *args: str, timeout_s: float = 45) -> dict[str, Any]:
        """Run one control command and return its parsed JSON response."""
        if self.json_flag is None:
            self.json_flag = self._detect_json_flag()
        result = self.host.run(self._argv(args, self.json_flag), timeout_s=timeout_s)
        if result.timed_out:
            raise CtlError(f"{self.host.name}: {CTL} {' '.join(args)} timed out")
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise CtlError(
                f"{self.host.name}: {CTL} {' '.join(args)} rc={result.returncode}: "
                f"{(result.stderr or result.stdout).strip()[:200]}"
            ) from None
        if not isinstance(response, dict):
            raise CtlError(f"{self.host.name}: unexpected response {response!r:.200}")
        return response

    # -- status ----------------------------------------------------------

    def status(self) -> ClientStatus:
        return parse_status(self.ctl("status", timeout_s=15))

    def try_status(self) -> ClientStatus | None:
        try:
            return self.status()
        except (CtlError, ValueError, KeyError) as exc:
            log.error("status failed: %s", exc)
            return None

    def destinations(self) -> list[Destination]:
        status = self.try_status()
        return status.destinations if status else []

    def connection_state(self, destination_id: str) -> str:
        status = self.try_status()
        return status.connection_state(destination_id) if status else "Unknown"

    # -- connect / disconnect -------------------------------------------

    def connect(self, destination: Destination) -> bool:
        log.info("Connecting to %s (%s, %s)", destination.id, destination.location, destination.address)
        try:
            response = self.ctl("connect", destination.id, timeout_s=30)
        except CtlError as exc:
            log.error("connect failed for %s: %s", destination.id, exc)
            return False
        log.debug("connect response: %s", json.dumps(response)[:300])
        if "Connect" not in response:
            log.error("connect rejected for %s: %s", destination.id, json.dumps(response)[:300])
            return False
        return True

    def disconnect(self) -> bool:
        log.info("Disconnecting…")
        try:
            response = self.ctl("disconnect", timeout_s=30)
        except CtlError as exc:
            if "not connected" in str(exc).lower():
                return True
            log.error("disconnect failed: %s", exc)
            return False
        log.debug("disconnect response: %s", json.dumps(response)[:300])
        return True

    def wait_for_connection(self, destination_id: str, timeout_s: int = CONNECTION_TIMEOUT_S) -> float | None:
        log.info("Waiting for %s to connect (timeout %ds)…", destination_id, timeout_s)
        t0 = time.monotonic()
        previous: str | None = None
        while time.monotonic() < t0 + timeout_s:
            state = self.connection_state(destination_id)
            elapsed = time.monotonic() - t0
            if state != previous:
                log.info("[%.1fs] %s: %r → %r", elapsed, destination_id, previous, state)
                previous = state
            if state == "Connected":
                return round(elapsed, 2)
            time.sleep(POLL_INTERVAL_S)
        log.error("Timed out waiting for %s after %ds", destination_id, timeout_s)
        return None

    def wait_for_disconnection(self, destination_id: str, timeout_s: int = 60) -> bool:
        log.info("Waiting for %s to disconnect…", destination_id)
        deadline = time.monotonic() + timeout_s
        previous: str | None = None
        while time.monotonic() < deadline:
            state = self.connection_state(destination_id)
            if state != previous:
                log.info("%s disconnect: %r → %r", destination_id, previous, state)
                previous = state
            if state == "None":
                return True
            time.sleep(POLL_INTERVAL_S)
        log.warning("Timed out waiting for %s to disconnect", destination_id)
        return False

    def vpn_connect(self, destination: Destination) -> float | None:
        """Connect, wait, and repair DNS if needed. Returns connect time or None."""
        if not self.connect(destination):
            return None
        connect_time = self.wait_for_connection(destination.id)
        if connect_time is None:
            self.disconnect()
            self.wait_for_disconnection(destination.id, timeout_s=30)
            return None
        self.ensure_dns()
        return connect_time

    def vpn_disconnect(self, destination: Destination) -> None:
        self.disconnect()
        self.wait_for_disconnection(destination.id)

    # -- DNS ---------------------------------------------------------------
    #
    # Some client builds leave the host without a working resolver once the
    # tunnel is up. The old workaround wrote ``nameserver 1.1.1.1`` into
    # /etc/resolv.conf after every connect and never restored it. Now:
    #   auto   — only if a lookup fails after connecting; restore on exit
    #   always — the old unconditional behaviour, still restored on exit
    #   never  — leave DNS alone

    def resolves(self) -> bool:
        result = self.host.run(["getent", "ahostsv4", DNS_CHECK_NAME], timeout_s=10)
        return result.ok and bool(result.stdout.strip())

    def ensure_dns(self) -> None:
        if self.dns_mode == "never":
            return
        if self.dns_mode == "auto" and self.resolves():
            log.debug("DNS resolves through the tunnel; leaving resolv.conf alone")
            return
        if self._saved_resolv_conf is None:
            current = self.host.run(["cat", "/etc/resolv.conf"], timeout_s=10)
            if not current.ok:
                log.warning("Cannot read /etc/resolv.conf; DNS fix skipped")
                return
            self._saved_resolv_conf = current.stdout.encode()
        log.info("Fixing DNS → 1.1.1.1 (mode %s)", self.dns_mode)
        result = self.host.run(
            self.host.privileged(["tee", "/etc/resolv.conf"]),
            timeout_s=10,
            input_bytes=DNS_RESOLVER_LINE,
        )
        if not result.ok:
            log.warning("DNS fix failed: %s", result.stderr.strip()[:200])

    def restore_dns(self) -> None:
        if self._saved_resolv_conf is None:
            return
        log.info("Restoring original /etc/resolv.conf")
        result = self.host.run(
            self.host.privileged(["tee", "/etc/resolv.conf"]),
            timeout_s=10,
            input_bytes=self._saved_resolv_conf,
        )
        if result.ok:
            self._saved_resolv_conf = None
        else:
            log.warning("DNS restore failed: %s", result.stderr.strip()[:200])
