"""Check every target exit on its network's indexer before creating any VM.

Costs seconds and nothing else, and saves a whole run when an exit is not
on the network at all. For each target:

* the exit's node must be a registered account with at least one announced
  multiaddress;
* for a v5 profile that discovers exits (no target ``client_config``), the
  node must also announce a ``gvpn:exit`` service, because that is the
  only way a v5 client learns about an exit.

The pinned package URL must also answer, so a package that left its
download location fails here and not after the droplets exist
(``package_problems``).

Free slots and a live probe are only visible to a running client; the
connect step handles those (``connect.free_slots``).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ..profiles import Profile, Target

Query = Callable[[str, str], dict[str, Any]]
Head = Callable[[str], int]

_ACCOUNT_FIELDS = "{__typename ... on AccountsList{accounts{chainKey multiAddresses}}}"
# Newer indexers return a union for ``services``; older ones the list type directly.
_SERVICES_UNION = "{services{__typename ... on ServicesList{services{node serviceType}}}}"
_SERVICES_PLAIN = "{services{services{node serviceType}}}"


class PreflightError(RuntimeError):
    pass


def graphql(indexer: str, query: str) -> dict[str, Any]:
    request = urllib.request.Request(
        indexer + "/graphql",
        data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _account(query: Query, indexer: str, address: str) -> dict[str, Any] | None:
    data = query(indexer, "{accounts(chainKey:" + json.dumps(address.lower()) + ")" + _ACCOUNT_FIELDS + "}")
    if data.get("errors"):
        raise PreflightError(f"indexer error: {data['errors'][0].get('message')}")
    accounts = ((data.get("data") or {}).get("accounts") or {}).get("accounts") or []
    return accounts[0] if accounts else None


def _exit_services(query: Query, indexer: str) -> set[str]:
    data = query(indexer, _SERVICES_UNION)
    if data.get("errors"):
        data = query(indexer, _SERVICES_PLAIN)
    if data.get("errors"):
        raise PreflightError(f"indexer error: {data['errors'][0].get('message')}")
    services = ((data.get("data") or {}).get("services") or {}).get("services") or []
    return {str(s["node"]).lower() for s in services if s.get("serviceType") == "gvpn:exit"}


def check(
    profile: Profile,
    targets: list[Target],
    query: Query | None = None,
    *,
    run_client_config: bool = False,
) -> list[str]:
    """Problems that would make the run unable to reach a target; empty if all is well.

    *run_client_config* is true when the run installs its own client config,
    which then lists the exits instead of the network's announcements.
    """
    query = query or graphql
    indexer = profile.network.indexer
    problems: list[str] = []
    try:
        announced: set[str] | None = None
        for target in targets:
            where = f"{target.name} ({target.exit_address}) on {profile.network.name}"
            account = _account(query, indexer, target.exit_address)
            if account is None:
                problems.append(f"{where}: not a registered node on this network")
                continue
            if not account.get("multiAddresses"):
                problems.append(f"{where}: registered but announces no address")
                continue
            if profile.worker.kind == "v5-user" and target.client_config is None and not run_client_config:
                if announced is None:
                    announced = _exit_services(query, indexer)
                if target.exit_address.lower() not in announced:
                    problems.append(f"{where}: does not announce a gvpn:exit service, so a v5 client cannot find it")
    except (OSError, ValueError, PreflightError) as error:
        problems.append(f"indexer {indexer} unusable: {error}")
    return problems


def head_status(url: str) -> int:
    """HTTP status of a HEAD request, following redirects (GitHub release assets redirect)."""
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "gnosis_vpn-bench"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def package_problems(profile: Profile, head: Head | None = None) -> list[str]:
    """Problems with the profile's pinned package download; empty if it answers 200."""
    url = profile.package.url
    if not url:
        return []
    head = head or head_status
    try:
        status = head(str(url))
    except (OSError, ValueError) as error:
        return [f"package {url} unreachable: {error}"]
    if status != 200:
        return [f"package {url} answered HTTP {status}; update the pin in profiles/{profile.name}.toml"]
    return []
