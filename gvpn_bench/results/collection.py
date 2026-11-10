"""Combine recordings into one report.

A *recording* is one ``result.json``: one run measuring one exit. A
*collection* is what a report shows: one or more recordings, possibly from
different runs, exits, networks or client versions.

Comparable recordings form a *series*: matching exit address, network,
profile, installed client package, measurement settings, provider and origin
region. Several recordings form a set of repetitions; the overview shows the
mean per rung with min–max error bars.

Missing rates stay missing: an unmeasured rung adds nothing to a series,
it never counts as 0.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schema import validate


@dataclass(frozen=True)
class Recording:
    name: str  # stable identifier, e.g. the results-repo folder name
    result: dict[str, Any]

    @property
    def target(self) -> dict[str, Any]:
        return self.result["provenance"]["target"]

    @property
    def profile(self) -> dict[str, Any]:
        return self.result["provenance"]["profile"]

    @property
    def client(self) -> dict[str, Any]:
        return self.result["provenance"]["client"]

    @property
    def started(self) -> str:
        run = self.result["run"]
        return run.get("started_at") or run["created_at"]

    @property
    def experiment(self) -> dict[str, Any]:
        return self.result["run"].get("experiment", {})

    def order_key(self) -> tuple[Any, ...]:
        if self.experiment:
            return (
                self.experiment["id"],
                self.experiment["cycle"],
                self.experiment["position"],
                self.started,
                self.name,
            )
        return (self.result["run"]["id"], 0, 0, self.started, self.name)

    def series_key(self) -> tuple[str, ...]:
        concurrent = self.result.get("concurrent")
        spec = (
            {
                key: float(value) if isinstance(value, (int, float)) else value
                for key, value in concurrent["spec"].items()
            }
            if concurrent
            else None
        )
        infrastructure = self.result["provenance"]["infrastructure"]
        return (
            self.target["exit_address"],
            self.profile["network"],
            self.profile["name"],
            str(self.client.get("package_version")),
            json.dumps(spec, sort_keys=True),
            f"{infrastructure['provider']} {infrastructure.get('region') or 'n/a'}",
        )

    def rungs(self) -> list[dict[str, Any]]:
        concurrent = self.result.get("concurrent")
        return concurrent["rungs"] if concurrent else []


def load_recording(directory: Path, name: str | None = None) -> Recording:
    result = json.loads((directory / "result.json").read_text())
    validate(result)
    return Recording(name=name or directory.name, result=result)


@dataclass(frozen=True)
class Point:
    clients: int
    values: tuple[float, ...]  # one total Mbit/s per measured repetition
    missing: tuple[str, ...]  # failure statuses of repetitions that did not measure this rung

    @property
    def attempts(self) -> int:
        return len(self.values) + len(self.missing)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.values)

    @property
    def low(self) -> float:
        return min(self.values)

    @property
    def high(self) -> float:
        return max(self.values)


@dataclass
class Series:
    key: tuple[str, ...]
    label: str
    recordings: list[Recording] = field(default_factory=list)

    def points(self) -> list[Point]:
        """One point per rung with at least one measurement; failures are kept in ``missing``."""
        values: dict[int, list[float]] = {}
        failures: dict[int, list[str]] = {}
        for recording in self.recordings:
            for rung in recording.rungs():
                clients = int(rung["rung"])
                values.setdefault(clients, [])
                failures.setdefault(clients, [])
                if rung["total_mbps"] is None:
                    failures[clients].append(str(rung["failure_status"]))
                else:
                    values[clients].append(rung["total_mbps"])
        return [
            Point(clients, tuple(values[clients]), tuple(failures[clients]))
            for clients in sorted(values)
            if values[clients]
        ]

    def unmeasured(self) -> list[int]:
        """Rungs attempted in this series that no repetition measured."""
        measured = {p.clients for p in self.points()}
        return sorted({rung for rung, _ in self.missing_rungs()} - measured)

    def missing_rungs(self) -> list[tuple[int, str]]:
        """(rung, failure_status) for every unmeasured rung in the series."""
        return [
            (int(rung["rung"]), str(rung["failure_status"]))
            for recording in self.recordings
            for rung in recording.rungs()
            if rung["total_mbps"] is None
        ]


@dataclass
class Collection:
    title: str
    recordings: list[Recording]

    def series(self) -> list[Series]:
        """Group recordings; label only with what differs across the collection."""
        groups: dict[tuple[str, ...], Series] = {}
        for recording in sorted(self.recordings, key=lambda r: r.order_key()):
            key = recording.series_key()
            groups.setdefault(key, Series(key=key, label="")).recordings.append(recording)
        networks = {k[1] for k in groups}
        profiles = {k[2] for k in groups}
        workloads = {k[4] for k in groups}
        regions = {k[5] for k in groups}
        labels_by_exit: dict[str, set[str]] = {}
        for key in groups:
            labels_by_exit.setdefault(key[0], set()).add(key[3])
        for key, series in groups.items():
            target = series.recordings[0].target
            parts = [target["label"]]
            if len(networks) > 1:
                parts.append(key[1])
            if len(profiles) > 1:
                parts.append(key[2])
            if len(labels_by_exit[key[0]]) > 1:
                parts.append(f"pkg {key[3]}")
            if len(workloads) > 1:
                spec = json.loads(key[4])
                parts.append(
                    f"{spec['transfer_bytes']:,} B / {spec['deadline_s']:g} s / "
                    f"{spec['gap_s']:g} s gap / {spec['ping_target']}@{spec['ping_interval_s']:g} s"
                    if spec
                    else "no ladder"
                )
            if len(regions) > 1:
                parts.append(key[5])
            series.label = " · ".join(parts)
        return list(groups.values())

    @property
    def multi_location(self) -> bool:
        return len({r.target["exit_address"] for r in self.recordings}) > 1

    def date_range(self) -> str:
        days = sorted({r.started[:10] for r in self.recordings})
        return days[0] if len(days) == 1 else f"{days[0]} – {days[-1]}"

    def networks(self) -> list[str]:
        return sorted({r.profile["network"] for r in self.recordings})
