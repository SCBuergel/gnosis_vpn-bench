"""Network metadata from the profile's indexer, validated and pinned per run.

Recovery must use the same channels contract the clients used. The
metadata is fetched once (``fund`` does it), validated, and stored in
``state/active-chain.json``. Recovery refuses missing or mismatching
metadata instead of falling back to another network.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Any

from ..profiles import Profile
from ..runs import Run, write_private_json

GNOSIS_CHAIN_ID = 100
WXHOPR_TOKEN = "0xd4fdec44db9d44b8f2b6d529620f9c0c7066a2c1"
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_QUERY = "{chainInfo{... on ChainInfo{network chainId contractAddresses channelClosureGracePeriod}}}"


def validate_chain(data: Any, expected_network: str) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("network") != expected_network:
        raise ValueError(f"chain metadata is not for {expected_network}")
    if data.get("chainId") != GNOSIS_CHAIN_ID:
        raise ValueError("chain metadata is not for Gnosis Chain")
    addresses = data.get("contractAddresses")
    if isinstance(addresses, str):
        addresses = json.loads(addresses)
    if not isinstance(addresses, dict) or str(addresses.get("token", "")).lower() != WXHOPR_TOKEN:
        raise ValueError("unexpected network token")
    channels = addresses.get("channels", "")
    if not isinstance(channels, str) or not _ADDRESS.fullmatch(channels) or int(channels[2:], 16) == 0:
        raise ValueError("invalid channels contract address")
    return {**data, "contractAddresses": addresses}


def chain_file(run: Run) -> Path:
    return run.state / "active-chain.json"


def fetch_chain(profile: Profile) -> dict[str, Any]:
    request = urllib.request.Request(
        profile.network.indexer + "/graphql",
        data=json.dumps({"query": _QUERY}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        data = json.load(response)
    return validate_chain(data["data"]["chainInfo"], profile.network.name)


def pin_chain(run: Run, profile: Profile) -> dict[str, Any]:
    """Fetch and store the metadata; refuse if a different one is already pinned."""
    chain = fetch_chain(profile)
    path = chain_file(run)
    if path.exists():
        pinned = load_chain(run, profile)
        if pinned["contractAddresses"]["channels"].lower() != chain["contractAddresses"]["channels"].lower():
            raise ValueError("the network's channels contract changed since this run pinned it")
        return pinned
    write_private_json(path, chain)
    return chain


def load_chain(run: Run, profile: Profile) -> dict[str, Any]:
    path = chain_file(run)
    if not path.exists():
        raise ValueError("this run has no pinned chain metadata (state/active-chain.json)")
    return validate_chain(json.loads(path.read_text()), profile.network.name)
