"""Parse ``gnosis_vpn-ctl`` JSON responses from v4 and v5 clients.

The two client generations share the outer shape but differ in detail:

* destination ``address``: a byte list in early v4 builds, a hex string
  (mixed case) from 0.97 on. Both normalise to lowercase ``0x…`` hex.
* destination ``meta``: v4 has ``{location, flag}``; v5 has
  ``{name, description, other: {location, flag}}`` plus a ``geo`` block.
* ``run_mode``: either a bare string (``"Running"``) or a single-key dict
  (``{"Running": {...}}``, ``{"PreparingSafe": {...}}``).
* connect response: v4 ``{"Connect": {"Connecting": dest}}``; v5
  ``{"Connect": {"type": "WaitingToConnect", "destination": dest}}``.

Everything here is pure: no I/O, so it is covered by offline tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_HEX_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


def normalise_address(raw: object) -> str:
    """Return a lowercase ``0x`` address from a hex string or byte list."""
    if isinstance(raw, list) and all(isinstance(b, int) and 0 <= b < 256 for b in raw):
        return "0x" + "".join(f"{b:02x}" for b in raw)
    if isinstance(raw, str) and _HEX_ADDRESS.fullmatch(raw):
        return raw.lower()
    raise ValueError(f"unrecognised destination address: {raw!r}")


def variant_name(raw: object) -> str:
    """Return the variant name of a serde enum: ``"X"`` or ``{"X": ...}``."""
    if isinstance(raw, dict) and isinstance(raw.get("type"), str):
        return raw["type"]
    if isinstance(raw, dict) and len(raw) == 1:
        return str(next(iter(raw)))
    return str(raw)


def variant_body(raw: object) -> dict[str, Any]:
    """Return the payload of a single-key enum dict, or ``{}``."""
    if isinstance(raw, dict) and len(raw) == 1:
        body = next(iter(raw.values()))
        if isinstance(body, dict):
            return body
    return {}


@dataclass(frozen=True)
class Destination:
    id: str
    address: str
    location: str
    name: str
    country: str | None
    raw: dict[str, Any] = field(repr=False, compare=False)


def parse_destination(raw: dict[str, Any]) -> Destination:
    meta = raw.get("meta") or {}
    other = meta.get("other") or {}
    geo = raw.get("geo") or {}
    location = meta.get("location") or other.get("location") or "Unknown"
    country = meta.get("flag") or other.get("flag") or geo.get("country_code")
    return Destination(
        id=str(raw["id"]),
        address=normalise_address(raw["address"]),
        location=str(location),
        name=str(meta.get("name") or raw["id"]),
        country=country,
        raw=raw,
    )


@dataclass(frozen=True)
class FundingRequest:
    node_address: str
    wxhopr: str
    xdai: str


@dataclass(frozen=True)
class ClientStatus:
    run_mode: str
    destinations: list[Destination]
    connected_id: str | None
    connecting_id: str | None
    connecting_phase: str | None
    reconnecting_id: str | None
    disconnecting_ids: list[str]
    funding_request: FundingRequest | None
    route_health: dict[str, Any]
    probe: dict[str, Any] | None
    raw: dict[str, Any] = field(repr=False)

    @property
    def running(self) -> bool:
        return self.run_mode == "Running"

    def connection_state(self, destination_id: str) -> str:
        """``Connected``, ``Connecting``, ``Reconnecting``, ``Disconnecting`` or ``None``."""
        if self.connected_id == destination_id:
            return "Connected"
        if self.connecting_id == destination_id:
            return "Connecting"
        if self.reconnecting_id == destination_id:
            return "Reconnecting"
        if destination_id in self.disconnecting_ids:
            return "Disconnecting"
        return "None"

    def find_by_address(self, address: str) -> Destination | None:
        """Return the single destination for *address*; raise if ambiguous."""
        wanted = address.lower()
        matches = [d for d in self.destinations if d.address == wanted]
        if len(matches) > 1:
            raise ValueError(f"ambiguous destination address {address}")
        return matches[0] if matches else None


def _destination_id(raw: object) -> str | None:
    if isinstance(raw, dict) and raw.get("destination_id") is not None:
        return str(raw["destination_id"])
    return None


def _funding_request(run_mode_raw: object) -> FundingRequest | None:
    if variant_name(run_mode_raw) != "PreparingSafe":
        return None
    body = variant_body(run_mode_raw)
    recommendation = body.get("balance_recommendation")
    if not recommendation or "node_address" not in body:
        return None
    return FundingRequest(
        node_address=normalise_address(body["node_address"]),
        wxhopr=str(recommendation["wxhopr"]).split()[0],
        xdai=str(recommendation["xdai"]).split()[0],
    )


def parse_status(payload: dict[str, Any]) -> ClientStatus:
    """Parse the full ``status`` response (``{"Status": {...}}``)."""
    status = payload.get("Status", payload)
    if not isinstance(status, dict):
        raise ValueError("status response has no Status object")
    destinations: list[Destination] = []
    route_health: dict[str, Any] = {}
    for entry in status.get("destinations") or []:
        destination = parse_destination(entry["destination"])
        destinations.append(destination)
        if entry.get("route_health") is not None:
            route_health[destination.id] = entry["route_health"]
    connecting = status.get("connecting")
    disconnecting = [
        dest_id for dest_id in map(_destination_id, status.get("disconnecting") or []) if dest_id
    ]
    run_mode_raw = status.get("run_mode", "Unknown")
    return ClientStatus(
        run_mode=variant_name(run_mode_raw),
        destinations=destinations,
        connected_id=_destination_id(status.get("connected")),
        connecting_id=_destination_id(connecting),
        connecting_phase=connecting.get("phase") if isinstance(connecting, dict) else None,
        reconnecting_id=_destination_id(status.get("reconnecting")),
        disconnecting_ids=disconnecting,
        funding_request=_funding_request(run_mode_raw),
        route_health=route_health,
        probe=status.get("probe"),
        raw=status,
    )


def start_client_accepted(response: dict[str, Any]) -> bool:
    return variant_name(response.get("StartClient")) in {"Started", "AlreadyRunning"}
