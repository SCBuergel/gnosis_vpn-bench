"""Static files shipped to VMs (scripts, the ladder agent)."""

from __future__ import annotations

from pathlib import Path

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"


def asset_bytes(name: str) -> bytes:
    return (ASSET_DIR / name).read_bytes()


def asset(name: str) -> str:
    return asset_bytes(name).decode()
