"""Shared offline fixtures. Nothing here touches a network, VM, VPN or wallet."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pytest

from gvpn_bench.host import CommandResult

Responder = Callable[[list[str], bytes | None], CommandResult]


@dataclass
class FakeHost:
    """Records argv and answers from a responder function."""

    responder: Responder
    name: str = "fake"
    calls: list[list[str]] = field(default_factory=list)
    inputs: list[bytes | None] = field(default_factory=list)

    def run(self, argv: Sequence[str], *, timeout_s: float = 30, input_bytes: bytes | None = None):
        self.calls.append(list(argv))
        self.inputs.append(input_bytes)
        return self.responder(list(argv), input_bytes)

    def popen(self, argv: Sequence[str], *, merge_stderr: bool = False) -> subprocess.Popen[bytes]:
        raise AssertionError("popen not expected in this test")

    def privileged(self, argv: Sequence[str]) -> list[str]:
        return ["sudo", *argv]


def ok(stdout: object = "") -> CommandResult:
    text = stdout if isinstance(stdout, str) else json.dumps(stdout)
    return CommandResult(0, text, "")


def fail(returncode: int = 1, stderr: str = "error") -> CommandResult:
    return CommandResult(returncode, "", stderr)


V4_DESTINATION = {
    "id": "Serbia",
    "meta": {"location": "Serbia", "flag": "RS"},
    "address": "0x4203490fC1747fA6d0ec13C1589CCa9d308724Ba",
    "routing": 1,
}

V5_DESTINATION = {
    "id": "gvpn-lt-1-luiu",
    "meta": {"name": "GVPN-LT-1", "description": None, "other": {"location": "Kaunas", "flag": "LT"}},
    "geo": {"country_code": "LT", "source": "announced_ip"},
    "address": "0x01961Ef9375E063aB32AddF44f0acC8adC71C888",
    "routing": 1,
    "source": "Discovered",
}

# Pre-0.97 builds (bench's original target) sent addresses as byte lists.
LEGACY_DESTINATION = {
    "id": "Austria",
    "meta": {"location": "Vienna"},
    "address": list(bytes.fromhex("210a14012535321b919653fdee8c2d5971b0e534")),
}


def status_payload(destinations: list[dict], **extra: object) -> dict:
    body: dict[str, object] = {
        "run_mode": {"Running": {"hopr_status": "Running"}},
        "destinations": [{"destination": d, "route_health": {"state": "Routable"}} for d in destinations],
        "connected": None,
        "connecting": None,
        "disconnecting": [],
    }
    body.update(extra)
    return {"Status": body}


@pytest.fixture
def fake_host_factory() -> Callable[[Responder], FakeHost]:
    return FakeHost
