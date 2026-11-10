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

import json
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
    name: str  # file stem for the standalone chart
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
    started = recording.result["run"].get("started_at")
    when = f"{started[:19].replace('T', ' ')}Z" if started else "start not recorded"
    label = f"{recording.target['label']} {when}"
    experiment = recording.experiment
    if experiment:
        label += f" · cycle {experiment['cycle']}/{experiment['cycles']}"
    return label


def _schedule(collection: Collection) -> list[Block]:
    recordings = sorted(collection.recordings, key=lambda r: r.order_key())
    rung_numbers = sorted({rung["rung"] for recording in recordings for rung in recording.rungs()})
    rows = []
    for recording in recordings:
        experiment = recording.experiment
        rungs = {rung["rung"]: rung for rung in recording.rungs()}
        rows.append(
            [
                recording.name,
                experiment.get("id", recording.result["run"]["id"]),
                f"{experiment['cycle']}/{experiment['cycles']}" if experiment else "n/a",
                f"{experiment['position']}/{experiment['targets']}" if experiment else "n/a",
                f"{recording.target['label']} ({recording.target['exit_address']})",
                recording.result["run"].get("started_at") or "not recorded",
                f"{sum(r['status'] == 'measured' for r in rungs.values())}/{len(rungs)}",
                *[
                    fmt(rungs[n]["total_mbps"])
                    if n in rungs and rungs[n]["status"] == "measured"
                    else f"not measured ({rungs[n]['failure_status']})"
                    if n in rungs
                    else "not recorded"
                    for n in rung_numbers
                ],
            ]
        )
    return [
        Heading(3, "Experiment schedule"),
        Paragraph(
            "Rows follow experiment, cycle and exit order. Each rate is total Mbit/s for that recording, "
            "not an average across cycles.",
            muted=True,
        ),
        Table(
            [
                "Recording",
                "Experiment / run",
                "Cycle",
                "Order",
                "Exit",
                "Started (UTC)",
                "Measured / planned",
                *[f"{n} clients" for n in rung_numbers],
            ],
            rows,
        ),
    ]


def _concurrent(collection: Collection) -> list[Block]:
    blocks: list[Block] = [Heading(2, "1. Concurrent bandwidth")]
    with_ladder = [r for r in collection.recordings if r.result.get("concurrent")]
    if not with_ladder:
        return [
            *blocks,
            Paragraph("No recording has a concurrent-load ladder. That is not 0 Mbit/s.", muted=True),
        ]
    details: list[Block] = [*_schedule(collection)]
    specs = {json.dumps(r.result["concurrent"]["spec"], sort_keys=True) for r in with_ladder}
    details.append(Heading(3, "Measurement specifications"))
    details.append(
        Table(
            ["Bytes per client", "Deadline s", "Gap s", "Ping target", "Ping interval s"],
            [
                [
                    spec["transfer_bytes"],
                    spec["deadline_s"],
                    spec["gap_s"],
                    spec["ping_target"],
                    spec["ping_interval_s"],
                ]
                for spec in [json.loads(value) for value in sorted(specs)]
            ],
        )
    )
    blocks.append(
        Paragraph(
            "Each client downloads from a dedicated origin; all clients of a rung share a scheduled start. "
            "Different measurement specifications or origin regions form separate series. Rates count only the "
            "common overlap (latest start to earliest end). Points are the total over all clients; with "
            "repeated recordings the point is the mean and the bar spans min to max. A hollow point "
            "marked m/n means only m of n attempts measured that rung.",
            muted=True,
        )
    )
    series = collection.series()
    blocks.insert(
        1, Chart("overview", "Total Mbit/s by number of concurrent clients", lambda t: overview_chart(series, t))
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
                continue
            if len(point.values) == 1:
                cell = f"{point.mean:.2f}"
            else:
                cell = f"{point.mean:.2f} ({point.low:.2f}–{point.high:.2f}, n={len(point.values)})"
            if point.missing:
                statuses = ", ".join(sorted(set(point.missing)))
                cell += f"; {len(point.missing)} of {point.attempts} not measured: {statuses}"
            row.append(cell)
        rows.append(row)
    blocks.append(Table(["Series", "Recordings", *[f"{c} client{'s' if c != 1 else ''}" for c in rungs]], rows))
    # The overview of every ladder comes first; schedule and specifications follow it.
    return [*blocks, *details]


def _system_cells(system: dict[str, Any] | None) -> list[str]:
    """Client host load during its transfer; n/a for recordings without host samples."""
    if not system:
        return ["n/a"] * 4
    return [
        fmt(system.get("cpu_mean_pct"), 1),
        fmt(system.get("cpu_max_pct"), 1),
        fmt(system.get("mem_available_min_mib"), 0),
        fmt(system.get("load1_max"), 2),
    ]


def _idle_ping(recording: Recording) -> list[Block]:
    """Unloaded tunnel ping before rung 1; nothing for recordings made before it existed."""
    rows = (recording.result.get("concurrent") or {}).get("idle_ping") or []
    if not rows:
        return []
    return [
        Paragraph("Idle tunnel ping before rung 1, all connected clients at once, no transfer running.", muted=True),
        Table(
            ["Client", "Replies", "Loss %", "Min ms", "Mean ms", "Max ms"],
            [
                [
                    row["client"],
                    f"{fmt(row['received'])} of {fmt(row['sent'])}",
                    fmt(row["loss_pct"], 1),
                    fmt(row["min_ms"], 0),
                    fmt(row["mean_ms"], 0),
                    fmt(row["max_ms"], 0),
                ]
                for row in rows
            ],
        ),
    ]


def _payment(recording: Recording) -> list[Block]:
    payment = recording.result.get("payment")
    if not payment:
        return []
    kind = "PIX" if payment.get("pix") else "channels"
    return [
        Paragraph(
            f"Payment ({kind}), whole run: funded {payment['funded_wxHOPR']} wxHOPR, returned "
            f"{payment['recovered_wxHOPR']}, spent {payment['spent_wxHOPR']}.",
            muted=True,
        )
    ]


def warning_blocks(collection: Collection) -> list[Block]:
    """Every recording's warnings, shown above everything else."""
    blocks: list[Block] = []
    for recording in sorted(collection.recordings, key=lambda r: r.order_key()):
        for warning in recording.result.get("warnings") or []:
            blocks.append(Paragraph(f"{_short(recording)}: {warning['detail']}", warn=True))
    return [Heading(2, "Warnings"), *blocks] if blocks else []


def _connection_cells(check: dict[str, Any] | None) -> list[str]:
    """Connected clients right before a rung, and the exit's slots as last probed."""
    if not check:
        return ["n/a", "n/a"]
    clients = f"{check['clients_connected']} of {check['clients_total']}"
    used, total = check.get("exit_slots_in_use"), check.get("exit_slots_total")
    if used is None or total is None:
        return [clients, "n/a"]
    age = check.get("probe_age_s")
    return [clients, f"{used} of {total}" + (f" (probe {age:.0f} s old)" if age is not None else "")]


def _per_client(collection: Collection) -> list[Block]:
    blocks: list[Block] = [
        Heading(2, "2. Per client"),
        Paragraph(
            "Download-speed plots show each client's per-second Mbit/s on the left axis and the sum at each "
            "timestamp on the independent right axis (black dashed line). The total has a gap wherever any "
            "participating client lacks a sample; missing data is not zero. These per-second sums are not "
            "the overlap-averaged ladder totals.",
            muted=True,
        ),
    ]
    for index, recording in enumerate(sorted(collection.recordings, key=lambda r: r.order_key())):
        blocks.append(Heading(3, _short(recording), anchor=recording.name))
        blocks.extend(_idle_ping(recording))
        blocks.extend(_payment(recording))
        if recording.rungs():
            blocks.append(
                Table(
                    [
                        "Rung",
                        "Status",
                        "Connected at check",
                        "Exit slots in use",
                        "Total Mbit/s",
                        "Overlap s",
                        "Start skew ms",
                        "Gap s",
                        "Detail",
                    ],
                    [
                        [
                            r["rung"],
                            r["status"] if r["status"] == "measured" else f"{r['status']}: {r['failure_status']}",
                            *_connection_cells(r.get("connection_check")),
                            fmt(r["total_mbps"]),
                            fmt(r["overlap_s"]),
                            fmt(r["start_skew_ms"]),
                            fmt(r["gap_before_s"]),
                            r.get("detail", ""),
                        ]
                        for r in recording.rungs()
                    ],
                )
            )
        rows = []
        for rung in recording.rungs():
            if rung["status"] != "measured":
                rows.append([rung["rung"], "—", f"{rung['status']}: {rung['failure_status']}", *["n/a"] * 10])
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
                        *_system_cells(client.get("system")),
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
                        "CPU mean %",
                        "CPU max %",
                        "Min free RAM MiB",
                        "Load1 max",
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
            cpu = []
            for client in rung["per_client"]:
                series = client.get("timeseries")
                if series:
                    throughput.append((client["client"], series["t_s"], series["mbps"]))
                    latency.append((client["client"], series["t_s"], series["rtt_ms"]))
                    if series.get("cpu_pct"):
                        cpu.append((client["client"], series["t_s"], series["cpu_pct"]))
                else:
                    throughput.append((client["client"], [], []))
            present = {name for name, _, _ in throughput}
            throughput.extend((name, [], []) for name in rung["clients"] if name not in present)
            if not any(times for _, times, _ in throughput):
                continue
            stem = f"r{index + 1}-rung{rung['rung']}"
            label = f"{_short(recording)}, rung {rung['rung']}"
            blocks.append(
                Chart(
                    f"{stem}-mbps",
                    f"Throughput, {label}",
                    _time(f"Throughput, rung {rung['rung']}", "Mbit/s", throughput, total=True),
                )
            )
            blocks.append(
                Chart(f"{stem}-ping", f"Ping, {label}", _time(f"Ping RTT, rung {rung['rung']}", "ms", latency))
            )
            if cpu:
                blocks.append(
                    Chart(
                        f"{stem}-cpu", f"Client CPU, {label}", _time(f"Client CPU busy, rung {rung['rung']}", "%", cpu)
                    )
                )
    return blocks


def _time(
    title: str, unit: str, lines: list[tuple[str, list[float], list[float | None]]], *, total: bool = False
) -> Callable[[Theme], str]:
    return lambda theme: time_chart(title, unit, lines, theme, total=total)


def _availability(collection: Collection) -> list[Block]:
    blocks: list[Block] = [Heading(2, "3. Availability and failures")]
    rows = [
        [
            recording.name,
            recording.experiment.get("cycle"),
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
        blocks.append(
            Table(
                ["Recording", "Cycle", "Exit", "Location", "Status", "Mbit/s", "Ping ms", "API RTT ms", "Detail"], rows
            )
        )
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
        missing = sum(rung["status"] != "measured" for recording in collection.recordings for rung in recording.rungs())
        blocks.append(
            Paragraph(
                f"No availability checks or explicit failure records. {missing} rungs are unmeasured; see the schedule."
                if missing
                else "No availability checks and no failures recorded."
            )
        )
    return blocks


def _provenance(collection: Collection) -> list[Block]:
    blocks: list[Block] = [Heading(2, "4. Provenance")]
    rows = []
    for recording in sorted(collection.recordings, key=lambda r: r.order_key()):
        prov = recording.result["provenance"]
        clients = [m for m in prov["infrastructure"]["machines"] if m["role"] == "client"]
        sizes = sorted({m["size"] for m in clients if m.get("size")})
        rows.append(
            [
                recording.name,
                (recording.result["run"].get("started_at") or "not recorded").replace("T", " "),
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
    blocks.extend(_machines(collection))
    notes = [(r.name, r.result["run"]["notes"]) for r in collection.recordings if r.result["run"]["notes"]]
    for name, note in notes:
        blocks.append(Paragraph(f"{name}: {note}", muted=True))
    return blocks


def _machines(collection: Collection) -> list[Block]:
    rows = []
    seen: set[str] = set()
    for recording in sorted(collection.recordings, key=lambda r: r.order_key()):
        infrastructure = recording.result["provenance"]["infrastructure"]
        for machine in infrastructure["machines"]:
            run_id = recording.result["run"]["id"]
            identity = json.dumps(
                [run_id, infrastructure["provider"], infrastructure.get("region"), machine], sort_keys=True
            )
            if identity in seen:
                continue
            seen.add(identity)
            topology = " / ".join(
                fmt(machine.get(key), 0) for key in ("cpu_sockets", "cores_per_socket", "threads_per_core")
            )
            rows.append(
                [
                    run_id,
                    machine["name"],
                    machine["role"],
                    infrastructure["provider"],
                    infrastructure.get("region"),
                    machine.get("size"),
                    machine.get("vcpus"),
                    machine.get("memory_mib"),
                    machine.get("cpu_model"),
                    machine.get("architecture"),
                    machine.get("cpu_vendor"),
                    topology,
                    machine.get("hypervisor"),
                    fmt(machine.get("root_disk_gib")),
                    fmt(machine.get("root_free_gib")),
                    machine.get("swap_mib"),
                    machine.get("os"),
                    machine.get("kernel"),
                    machine.get("package_version"),
                    machine.get("ctl_version"),
                ]
            )
    return [
        Heading(3, "Machine inventory"),
        Paragraph(
            "Every origin and client is listed. Identical facts are shown once within a run; "
            "machines with the same name in different runs remain separate. Missing historical facts are n/a. "
            "CPU topology is sockets / cores per socket / threads per core. "
            "Disk figures describe the root filesystem at collection time. "
            "No IP addresses, hostnames or credentials are included.",
            muted=True,
        ),
        Table(
            [
                "Run",
                "Machine",
                "Role",
                "Provider",
                "Region",
                "Size",
                "vCPUs",
                "RAM MiB",
                "CPU model",
                "Architecture",
                "CPU vendor",
                "CPU topology",
                "Hypervisor",
                "Root disk GiB",
                "Root free GiB",
                "Swap MiB",
                "OS",
                "Kernel",
                "Package",
                "Client",
            ],
            rows,
        ),
    ]


def build(collection: Collection) -> list[Block]:
    return [
        *warning_blocks(collection),
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
