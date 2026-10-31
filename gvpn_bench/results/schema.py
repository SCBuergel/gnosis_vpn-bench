"""The ``result.json`` contract, version 1.0.

This module is the executable form of ``docs/result-schema.md`` and
``schema/result.schema.json``. A test keeps the three in agreement.

Rules that the validator enforces beyond types:

* A missing measurement is ``null``, never ``0``. A rung or check that
  did not produce data has a ``failure_status`` from ``FAILURE_STATUSES``.
* Every failure carries a status from the same fixed list.
* Exit addresses are lowercase 0x-hex.

Validation is stdlib-only so local mode keeps working without extras.
"""

from __future__ import annotations

import re
from typing import Any

SCHEMA_VERSION = "1.0"

RUN_KINDS = ("ladder", "sweep", "local")
WORKER_MODELS = ("v4-root", "v5-user", "local")
PROVIDERS = ("digitalocean", "local", "other")
RUNG_STATUSES = ("measured", "failed", "not_run")

# The fixed list of outcome statuses. "connected" is the one success value
# (used by availability rows); all others describe why data is missing.
FAILURE_STATUSES = (
    "connected",
    "UnacceptablePixParams",
    "DestinationNotFound",
    "http_404_register",
    "timeout",
    "control_timeout",
    "worker_not_ready",
    "connect_rejected",
    "route_bypass",
    "download_incomplete",
    "no_overlap",
    "not_run",
    "error",
)

FAILURE_SCOPES = ("run", "rung", "client", "exit")

_ADDRESS = re.compile(r"0x[0-9a-f]{40}")
_ISO_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+00:00)")


class SchemaError(ValueError):
    pass


def _require(obj: Any, key: str, path: str) -> Any:
    if not isinstance(obj, dict) or key not in obj:
        raise SchemaError(f"{path}: missing {key!r}")
    return obj[key]


def _check_type(value: Any, types: tuple[type, ...], path: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    # bool is an int subclass; a boolean is never a valid number here.
    if isinstance(value, bool) and bool not in types:
        raise SchemaError(f"{path}: expected {types}, got bool")
    if not isinstance(value, types):
        raise SchemaError(f"{path}: expected {'/'.join(t.__name__ for t in types)}, got {value!r:.60}")


def _number(obj: dict[str, Any], key: str, path: str, *, nullable: bool = True) -> None:
    _check_type(_require(obj, key, path), (int, float), f"{path}.{key}", nullable=nullable)


def _string(obj: dict[str, Any], key: str, path: str, *, nullable: bool = False) -> None:
    _check_type(_require(obj, key, path), (str,), f"{path}.{key}", nullable=nullable)


def _enum(value: Any, allowed: tuple[str, ...], path: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if value not in allowed:
        raise SchemaError(f"{path}: {value!r} is not one of {', '.join(allowed)}")


def _timestamp(obj: dict[str, Any], key: str, path: str, *, nullable: bool = False) -> None:
    value = _require(obj, key, path)
    if value is None and nullable:
        return
    if not isinstance(value, str) or not _ISO_UTC.fullmatch(value):
        raise SchemaError(f"{path}.{key}: expected ISO 8601 UTC timestamp, got {value!r}")


def _address(value: Any, path: str) -> None:
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value):
        raise SchemaError(f"{path}: expected lowercase 0x address, got {value!r}")


def _validate_run(run: dict[str, Any]) -> None:
    _string(run, "id", "run")
    _enum(_require(run, "kind", "run"), RUN_KINDS, "run.kind")
    _timestamp(run, "created_at", "run")
    _timestamp(run, "started_at", "run", nullable=True)
    _timestamp(run, "finished_at", "run", nullable=True)
    _check_type(_require(run, "reproducible", "run"), (bool,), "run.reproducible")
    _string(run, "notes", "run")


def _validate_provenance(prov: dict[str, Any]) -> None:
    bench = _require(prov, "bench", "provenance")
    _string(bench, "version", "provenance.bench")
    _string(bench, "commit", "provenance.bench", nullable=True)

    profile = _require(prov, "profile", "provenance")
    for key in ("name", "network"):
        _string(profile, key, "provenance.profile")
    for key in ("channel", "package_version", "package_sha256"):
        _string(profile, key, "provenance.profile", nullable=True)
    _enum(
        _require(profile, "worker_model", "provenance.profile"),
        WORKER_MODELS,
        "provenance.profile.worker_model",
    )

    client = _require(prov, "client", "provenance")
    _string(client, "ctl_version", "provenance.client", nullable=True)
    _string(client, "package_version", "provenance.client", nullable=True)

    target = _require(prov, "target", "provenance")
    for key in ("name", "label", "network"):
        _string(target, key, "provenance.target")
    _address(_require(target, "exit_address", "provenance.target"), "provenance.target.exit_address")
    _string(target, "location", "provenance.target", nullable=True)

    infra = _require(prov, "infrastructure", "provenance")
    _enum(
        _require(infra, "provider", "provenance.infrastructure"),
        PROVIDERS,
        "provenance.infrastructure.provider",
    )
    _string(infra, "region", "provenance.infrastructure", nullable=True)
    machines = _require(infra, "machines", "provenance.infrastructure")
    _check_type(machines, (list,), "provenance.infrastructure.machines")
    for i, machine in enumerate(machines):
        path = f"provenance.infrastructure.machines[{i}]"
        for key in ("role", "name"):
            _string(machine, key, path)
        for key in ("size", "cpu_model", "os", "kernel"):
            _string(machine, key, path, nullable=True)
        for key in ("vcpus", "memory_mib"):
            _number(machine, key, path)


def _validate_concurrent(concurrent: dict[str, Any]) -> None:
    spec = _require(concurrent, "spec", "concurrent")
    for key in ("transfer_bytes", "deadline_s", "gap_s", "ping_interval_s"):
        _number(spec, key, "concurrent.spec", nullable=False)
    _string(spec, "ping_target", "concurrent.spec")
    rungs = _require(concurrent, "rungs", "concurrent")
    _check_type(rungs, (list,), "concurrent.rungs")
    for i, rung in enumerate(rungs):
        path = f"concurrent.rungs[{i}]"
        _number(rung, "rung", path, nullable=False)
        _check_type(_require(rung, "clients", path), (list,), f"{path}.clients")
        status = _require(rung, "status", path)
        _enum(status, RUNG_STATUSES, f"{path}.status")
        _enum(_require(rung, "failure_status", path), FAILURE_STATUSES, f"{path}.failure_status", nullable=True)
        if status != "measured" and rung["failure_status"] is None:
            raise SchemaError(f"{path}: an unmeasured rung needs a failure_status")
        for key in ("total_mbps", "overlap_s", "start_skew_ms", "gap_before_s"):
            _number(rung, key, path)
        if status != "measured" and rung["total_mbps"] is not None:
            raise SchemaError(f"{path}: total_mbps must be null when the rung was not measured")
        clients = _require(rung, "per_client", path)
        _check_type(clients, (list,), f"{path}.per_client")
        for j, client in enumerate(clients):
            cpath = f"{path}.per_client[{j}]"
            _string(client, "client", cpath)
            for key in ("mbps", "overlap_bytes", "downloaded_bytes", "ping_mean_ms", "ping_loss_pct"):
                _number(client, key, cpath)
            for key in ("complete", "sha256_verified", "timed_out"):
                _check_type(_require(client, key, cpath), (bool,), f"{cpath}.{key}", nullable=True)


def _validate_availability(rows: list[Any]) -> None:
    for i, row in enumerate(rows):
        path = f"availability[{i}]"
        _address(_require(row, "exit_address", path), f"{path}.exit_address")
        _string(row, "exit_label", path)
        _string(row, "client", path, nullable=True)
        status = _require(row, "status", path)
        _enum(status, FAILURE_STATUSES, f"{path}.status")
        _string(row, "detail", path, nullable=True)
        for key in ("download_mbps", "ping_mean_ms", "ping_loss_pct", "api_rtt_ms"):
            _number(row, key, path)
        if status != "connected" and row["download_mbps"] is not None:
            raise SchemaError(f"{path}: download_mbps requires status 'connected'")


def _validate_failures(rows: list[Any]) -> None:
    for i, row in enumerate(rows):
        path = f"failures[{i}]"
        _enum(_require(row, "scope", path), FAILURE_SCOPES, f"{path}.scope")
        _string(row, "subject", path)
        status = _require(row, "status", path)
        _enum(status, FAILURE_STATUSES, f"{path}.status")
        if status == "connected":
            raise SchemaError(f"{path}: 'connected' is not a failure")
        _string(row, "detail", path)
        _timestamp(row, "at", path, nullable=True)


def validate(result: dict[str, Any]) -> None:
    """Raise ``SchemaError`` unless *result* is a valid version-1.0 result."""
    version = _require(result, "schema_version", "result")
    if version != SCHEMA_VERSION:
        raise SchemaError(f"unsupported schema_version {version!r} (expected {SCHEMA_VERSION})")
    _validate_run(_require(result, "run", "result"))
    _validate_provenance(_require(result, "provenance", "result"))
    concurrent = _require(result, "concurrent", "result")
    if concurrent is not None:
        _validate_concurrent(concurrent)
    per_client = _require(result, "per_client", "result")
    _check_type(per_client, (list,), "per_client")
    for i, client in enumerate(per_client):
        _string(client, "client", f"per_client[{i}]")
        _check_type(_require(client, "connected", f"per_client[{i}]"), (bool,), f"per_client[{i}].connected")
        _number(client, "connect_time_s", f"per_client[{i}]")
    availability = _require(result, "availability", "result")
    _check_type(availability, (list,), "availability")
    _validate_availability(availability)
    failures = _require(result, "failures", "result")
    _check_type(failures, (list,), "failures")
    _validate_failures(failures)


def empty_result(run: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    """A valid skeleton to fill section by section."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run": run,
        "provenance": provenance,
        "concurrent": None,
        "per_client": [],
        "availability": [],
        "failures": [],
    }
