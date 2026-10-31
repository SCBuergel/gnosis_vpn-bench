"""Minimal DigitalOcean API client (stdlib only).

Safety properties:

* Every droplet is tagged ``gvpn-bench-run-<run id>``. Deletion checks the
  tag before deleting, so a wrong ID in an inventory cannot delete
  someone else's machine.
* The inventory is written after *each* create, so a crash mid-way never
  leaves an untracked droplet.
* Nothing here is called by tests; tests replace ``Api`` with a fake.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runs import read_secret

API_ROOT = "https://api.digitalocean.com/v2/"
_TOKEN = re.compile(r"dop_v1_[A-Za-z0-9]+")
_TAG_SAFE = re.compile(r"[^a-z0-9:_-]")


def run_tag(run_id: str) -> str:
    """DigitalOcean tags allow letters, digits, colons, dashes and underscores."""
    return "gvpn-bench-run-" + _TAG_SAFE.sub("-", run_id.lower())


def load_token(path: Path) -> str:
    text = read_secret(path).strip()
    match = _TOKEN.search(text)
    return match.group() if match else text


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"DigitalOcean API {status}: {message}")
        self.status = status


@dataclass
class Api:
    token: str
    timeout_s: float = 90

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        request = urllib.request.Request(
            API_ROOT + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            raise ApiError(error.code, error.read().decode(errors="replace")[:300]) from None
        return json.loads(payload) if payload else None

    # -- droplets --------------------------------------------------------

    def create_droplet(
        self,
        *,
        name: str,
        region: str,
        size: str,
        image: str,
        user_data: str,
        tags: list[str],
    ) -> dict[str, Any]:
        body = {
            "name": name,
            "region": region,
            "size": size,
            "image": image,
            "user_data": user_data,
            "tags": tags,
            "backups": False,
            "ipv6": False,
            "monitoring": False,
        }
        return self.request("POST", "droplets", body)["droplet"]

    def get_droplet(self, droplet_id: int) -> dict[str, Any] | None:
        try:
            return self.request("GET", f"droplets/{droplet_id}")["droplet"]
        except ApiError as error:
            if error.status == 404:
                return None
            raise

    def droplets_with_tag(self, tag: str) -> list[dict[str, Any]]:
        return self.request("GET", f"droplets?tag_name={tag}&per_page=200")["droplets"]

    def delete_droplet(self, droplet_id: int, expected_tag: str) -> bool:
        """Delete one droplet if it carries *expected_tag*. Returns True if gone."""
        droplet = self.get_droplet(droplet_id)
        if droplet is None:
            return True
        if expected_tag not in droplet.get("tags", []):
            raise RuntimeError(f"droplet {droplet_id} lacks tag {expected_tag}; refusing to delete it")
        try:
            self.request("DELETE", f"droplets/{droplet_id}")
        except ApiError as error:
            if error.status != 404:
                raise
        return True


def public_ipv4(droplet: dict[str, Any]) -> str | None:
    for network in droplet.get("networks", {}).get("v4", []):
        if network.get("type") == "public":
            return network["ip_address"]
    return None
