"""A synthetic, valid result.json used by several tests."""

from __future__ import annotations

import copy
from typing import Any

_BASE: dict[str, Any] = {
    "schema_version": "1.0",
    "run": {
        "id": "20261101-120000-kaunas-v5-piz-palu-dev",
        "kind": "ladder",
        "created_at": "2026-11-01T12:00:00Z",
        "started_at": "2026-11-01T12:30:00Z",
        "finished_at": "2026-11-01T12:50:00Z",
        "reproducible": True,
        "notes": "",
    },
    "provenance": {
        "bench": {"version": "0.2.0", "commit": "abc123"},
        "profile": {
            "name": "v5-piz-palu-dev",
            "network": "piz-palu-dev",
            "channel": "experimental",
            "package_version": "2026.10.28+build.020144.experimental",
            "package_sha256": "9b" * 32,
            "worker_model": "v5-user",
        },
        "client": {"ctl_version": "0.101.1", "package_version": "2026.10.28+build.020144.experimental"},
        "target": {
            "name": "kaunas",
            "label": "GVPN-LT-1",
            "exit_address": "0x01961ef9375e063ab32addf44f0acc8adc71c888",
            "network": "piz-palu-dev",
            "location": "Kaunas",
        },
        "infrastructure": {
            "provider": "digitalocean",
            "region": "fra1",
            "machines": [
                {
                    "role": "client",
                    "name": "client1",
                    "size": "c-4",
                    "vcpus": 4,
                    "memory_mib": 7940,
                    "cpu_model": "Xeon",
                    "os": "Ubuntu 24.04",
                    "kernel": "6.8.0",
                }
            ],
        },
    },
    "concurrent": {
        "spec": {
            "transfer_bytes": 200_000_000,
            "deadline_s": 300,
            "gap_s": 10,
            "ping_target": "9.9.9.9",
            "ping_interval_s": 1,
        },
        "origin": {"bytes": 200_000_000, "sha256": "00" * 32},
        "baseline": [{"client": "client1", "mbps": 900.0, "bytes": 200_000_000}],
        "rungs": [
            {
                "rung": 1,
                "clients": ["client1"],
                "status": "measured",
                "failure_status": None,
                "total_mbps": 42.5,
                "overlap_s": 37.6,
                "start_skew_ms": 0.0,
                "gap_before_s": None,
                "per_client": [
                    {
                        "client": "client1",
                        "mbps": 42.5,
                        "overlap_bytes": 200_000_000,
                        "downloaded_bytes": 200_000_000,
                        "complete": True,
                        "sha256_verified": True,
                        "timed_out": False,
                        "ping_mean_ms": 81.0,
                        "ping_loss_pct": 0.0,
                        "timeseries": {"t_s": [0, 1, 2], "mbps": [0.0, 40.1, None], "rtt_ms": [80, None, 82]},
                    }
                ],
            },
            {
                "rung": 2,
                "clients": ["client1", "client2"],
                "status": "not_run",
                "failure_status": "timeout",
                "total_mbps": None,
                "overlap_s": None,
                "start_skew_ms": None,
                "gap_before_s": None,
                "per_client": [],
            },
        ],
    },
    "per_client": [
        {
            "client": "client1",
            "connected": True,
            "connect_time_s": 12.3,
            "exit_address": None,
            "egress_changed": True,
        }
    ],
    "availability": [
        {
            "exit_address": "0x01961ef9375e063ab32addf44f0acc8adc71c888",
            "exit_label": "GVPN-LT-1",
            "client": "client2",
            "status": "UnacceptablePixParams",
            "detail": "session rejected <script>",
            "download_mbps": None,
            "ping_mean_ms": None,
            "ping_loss_pct": None,
            "api_rtt_ms": 41.0,
        }
    ],
    "failures": [
        {
            "scope": "rung",
            "subject": "rung 2",
            "status": "timeout",
            "detail": "client2 did not connect",
            "at": None,
        }
    ],
}


def sample_result() -> dict[str, Any]:
    return copy.deepcopy(_BASE)
