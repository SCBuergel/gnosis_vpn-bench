"""The content of a report, built once and rendered as HTML or Markdown.

Fixed section order:

1. Concurrent bandwidth: overview line chart (one line per series, error
   bars for repetitions) and the series table.
2. Per client: for each recording, the per-rung per-client table and the
   per-second throughput and ping charts.
3. Availability and failures.
4. Provenance: which recording used which exit, network, client version,
   region and machines.

Missing values render as "n/a" with their status, never as 0.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .charts import Theme, overview_chart, time_chart
from .collection import Collection, Recording


@dataclass(frozen=True)
class Heading:
    level: int
    text: str
    anchor: str | None = None


@dataclass(frozen=True)
class Paragraph:
    text: str
    muted: bool = False
    warn: bool = False


@dataclass(frozen=True)
class Table:
    headers: list[str]
    rows: list[list[Any]]


@dataclass(frozen=True)
class Chart:
    name: str  # file stem for the standalone SVG
    alt: str
    draw: Callable[[Theme], str]


Block = Heading | Paragraph | Table | Chart


def fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return str(value)


def _short(recording: Recording) -> str:
    return f"{recording.target['label']} {recording.started[:16].replace('T', ' ')}Z"


def _concurrent(collection: Collection) -> list[Block]:
    blocks: list[Block] = [Heading(2, "1. Concurrent bandwidth")]
    with_ladder = [r for r in collection.recordings if r.result.get("concurrent")]
    if not with_ladder:
        return [
            *blocks,
            Paragraph("No recording has a concurrent-load ladder. That is not 0 Mbit/s.", muted=True),
        ]
    spec = with_ladder[0].result["concurrent"]["spec"]
    blocks.append(
        Paragraph(
            f"Each client downloads {spec['transfer_bytes']:,} bytes from a dedicated origin; all clients of "
            f"a rung start at the same instant with a {spec['deadline_s']:g} s deadline. Rates count only the "
            "common overlap (latest start to earliest end). Points are the total over all clients; with "
            "repeated recordings the point is the mean and the bar spans min to max.",
            muted=True,
        )
    )
    series = collection.series()
    blocks.append(
        Chart("overview", "Total Mbit/s by number of concurrent clients", lambda t: overview_chart(series, t))
    )
    rungs = sorted({p.clients for s in series for p in s.points()} | {r for s in series for r, _ in s.missing_rungs()})
    rows = []
    for s in series:
        by_clients = {p.clients: p for p in s.points()}
        missing = dict(s.missing_rungs())
        row: list[Any] = [s.label, len(s.recordings)]
        for clients in rungs:
            point = by_clients.get(clients)
            if point is None:
                row.append(f"not measured ({missing.get(clients, 'not_run')})")
            elif len(point.values) == 1:
                row.append(f"{point.mean:.2f}")
            else:
                row.append(f"{point.mean:.2f} ({point.low:.2f}–{point.high:.2f}, n={len(point.values)})")
        rows.append(row)
    blocks.append(Table(["Series", "Recordings", *[f"{c} client{'s' if c != 1 else ''}" for c in rungs]], rows))
    return blocks


def _per_client(collection: Collection) -> list[Block]:
    blocks: list[Block] = [Heading(2, "2. Per client")]
    for index, recording in enumerate(sorted(collection.recordings, key=lambda r: r.started)):
        blocks.append(Heading(3, _short(recording), anchor=recording.name))
        rows = []
        for rung in recording.rungs():
            if rung["status"] != "measured":
                rows.append([rung["rung"], "—", f"{rung['status']}: {rung['failure_status']}", *["n/a"] * 6])
                continue
            for client in rung["per_client"]:
                rows.append(
                    [
                        rung["rung"],
                        client["client"],
                        fmt(client["mbps"]),
                        fmt(client["downloaded_bytes"]),
                        fmt(client["complete"]),
                        fmt(client["sha256_verified"]),
                        fmt(client["timed_out"]),
                        fmt(client["ping_mean_ms"], 0),
                        fmt(client["ping_loss_pct"], 1),
                    ]
                )
        if rows:
            blocks.append(
                Table(
                    [
                        "Rung",
                        "Client",
                        "Mbit/s",
                        "Bytes",
                        "Complete",
                        "SHA-256 ok",
                        "Timed out",
                        "Ping ms",
                        "Loss %",
                    ],
                    rows,
                )
            )
        connections = recording.result["per_client"]
        if connections:
            blocks.append(
                Table(
                    ["Client", "Connected", "Connect s", "Egress IP changed"],
                    [
                        [
                            c["client"],
                            fmt(c["connected"]),
                            fmt(c["connect_time_s"]),
                            fmt(c.get("egress_changed")),
                        ]
                        for c in connections
                    ],
                )
            )
        for rung in recording.rungs():
            throughput = []
            latency = []
            for client in rung["per_client"]:
                series = client.get("timeseries")
                if series:
                    throughput.append((client["client"], series["t_s"], series["mbps"]))
                    latency.append((client["client"], series["t_s"], series["rtt_ms"]))
            if not throughput:
                continue
            stem = f"r{index + 1}-rung{rung['rung']}"
            label = f"{_short(recording)}, rung {rung['rung']}"
            blocks.append(
                Chart(
                    f"{stem}-mbps",
                    f"Throughput, {label}",
                    _time(f"Throughput, rung {rung['rung']}", "Mbit/s", throughput),
                )
            )
            blocks.append(
                Chart(f"{stem}-ping", f"Ping, {label}", _time(f"Ping RTT, rung {rung['rung']}", "ms", latency))
            )
    return blocks


def _time(title: str, unit: str, lines: list[tuple[str, list[float], list[float | None]]]) -> Callable[[Theme], str]:
    return lambda theme: time_chart(title, unit, lines, theme)


def _availability(collection: Collection) -> list[Block]:
    blocks: list[Block] = [Heading(2, "3. Availability and failures")]
    rows = [
        [
            row["exit_label"],
            row.get("location"),
            row["status"],
            fmt(row["download_mbps"]),
            fmt(row["ping_mean_ms"], 0),
            fmt(row["api_rtt_ms"], 0),
            row["detail"] or "",
        ]
        for recording in collection.recordings
        for row in recording.result["availability"]
    ]
    if rows:
        blocks.append(Table(["Exit", "Location", "Status", "Mbit/s", "Ping ms", "API RTT ms", "Detail"], rows))
        blocks.append(
            Paragraph(
                "Availability checks are short downloads; their rates are not comparable with the ladder.",
                True,
            )
        )
    failures = [
        [_short(recording), f["scope"], f["subject"], f["status"], f["detail"]]
        for recording in collection.recordings
        for f in recording.result["failures"]
    ]
    if failures:
        blocks.append(Table(["Recording", "Scope", "Subject", "Status", "Detail"], failures))
    if not rows and not failures:
        blocks.append(Paragraph("No availability checks and no failures recorded."))
    return blocks


def _provenance(collection: Collection) -> list[Block]:
    blocks: list[Block] = [Heading(2, "4. Provenance")]
    rows = []
    for recording in sorted(collection.recordings, key=lambda r: r.started):
        prov = recording.result["provenance"]
        clients = [m for m in prov["infrastructure"]["machines"] if m["role"] == "client"]
        sizes = sorted({m["size"] for m in clients if m.get("size")})
        rows.append(
            [
                recording.name,
                recording.started.replace("T", " "),
                f"{prov['target']['label']} ({prov['target']['exit_address']})",
                prov["profile"]["network"],
                prov["profile"]["name"],
                f"{prov['client']['package_version'] or 'n/a'} / ctl {prov['client']['ctl_version'] or 'n/a'}",
                f"{prov['infrastructure']['provider']} {prov['infrastructure']['region'] or ''}".strip(),
                f"{len(clients)} × {', '.join(sizes) or 'n/a'}",
                recording.result["run"]["id"],
                "yes" if recording.result["run"]["reproducible"] else "no",
            ]
        )
    blocks.append(
        Table(
            [
                "Recording",
                "Started (UTC)",
                "Exit",
                "Network",
                "Profile",
                "Package / client",
                "Where",
                "Client VMs",
                "Run",
                "Reproducible",
            ],
            rows,
        )
    )
    notes = [(r.name, r.result["run"]["notes"]) for r in collection.recordings if r.result["run"]["notes"]]
    for name, note in notes:
        blocks.append(Paragraph(f"{name}: {note}", muted=True))
    return blocks


def build(collection: Collection) -> list[Block]:
    return [
        *_concurrent(collection),
        *_per_client(collection),
        *_availability(collection),
        *_provenance(collection),
    ]


def subtitle(collection: Collection) -> str:
    exits = sorted({r.target["label"] for r in collection.recordings})
    return (
        f"{collection.date_range()} · {', '.join(collection.networks())} · "
        f"{len(collection.recordings)} recording{'s' if len(collection.recordings) != 1 else ''} · "
        f"{', '.join(exits)}"
    )
