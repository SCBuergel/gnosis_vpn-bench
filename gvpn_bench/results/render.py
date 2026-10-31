"""Render one ``result.json`` as a self-contained HTML report.

The section order is fixed so reports are comparable at a glance:

1. Concurrent bandwidth (the ladder) — total and per-client Mbit/s per rung
2. Per-client results
3. Availability and failures
4. Provenance

The page has inline CSS and SVG, no JavaScript and no external resources.
Missing values render as "not measured" with their failure status, never as 0.
"""

from __future__ import annotations

from html import escape
from typing import Any

from .schema import validate

COLORS = ("#2563eb", "#ea580c", "#059669", "#9333ea", "#ca8a04")

CSS = """
:root{--bg:#f7f8fa;--card:#fff;--fg:#172033;--muted:#667085;--line:#e5e8ee;--accent:#2563eb;--warn:#8a4b0f;--grid:#edf0f3}
@media (prefers-color-scheme: dark){:root{--bg:#0f141c;--card:#161d28;--fg:#e6e9ef;--muted:#98a2b3;
--line:#273142;--accent:#60a5fa;--warn:#f0b46b;--grid:#222b38}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif}
main{max-width:1140px;margin:auto;padding:32px 16px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:0 0 12px}h3{font-size:15px;margin:18px 0 8px}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:20px;margin:20px 0}
.muted,footer{color:var(--muted)}.warn{color:var(--warn)}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:8px 10px;border-bottom:1px solid var(--grid);text-align:left;vertical-align:top}
th{font-weight:500;color:var(--muted)}td{overflow-wrap:anywhere}
svg{width:100%;height:auto}svg text{font:13px system-ui;fill:var(--muted)}
.legend span{display:inline-flex;align-items:center;margin-right:14px;font-size:13px}
.legend i{width:10px;height:10px;border-radius:2px;margin-right:6px;display:inline-block}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;border:1px solid var(--line);font-size:12px}
footer{font-size:12px}code{font-size:13px}
"""


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


def table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(fmt(v))}</td>" for v in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def legend(names: list[str]) -> str:
    items = "".join(
        f'<span><i style="background:{COLORS[i % len(COLORS)]}"></i>{escape(name)}</span>'
        for i, name in enumerate(names)
    )
    return f'<div class="legend">{items}</div>'


def rung_chart(rungs: list[dict[str, Any]]) -> str:
    """Stacked bars: one bar per rung, one segment per client's overlap Mbit/s."""
    width, height = 900, 280
    left, right, top, bottom = 70, 20, 24, 46
    measured = [r["total_mbps"] for r in rungs if r["total_mbps"] is not None]
    ymax = max(measured, default=0) * 1.15 or 1.0
    slot = (width - left - right) / max(len(rungs), 1)
    bar = min(120.0, slot * 0.5)

    def y(value: float) -> float:
        return height - bottom - value / ymax * (height - top - bottom)

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Total Mbit/s per rung">']
    for i in range(5):
        value = ymax * i / 4
        parts.append(f'<path d="M{left},{y(value):.1f} H{width - right}" stroke="var(--grid)"/>')
        parts.append(f'<text x="{left - 8}" y="{y(value) + 4:.1f}" text-anchor="end">{value:.3g}</text>')
    parts.append(f'<text x="{left}" y="14">Mbit/s over the common overlap</text>')
    clients: list[str] = []
    for index, rung in enumerate(rungs):
        x = left + slot * index + (slot - bar) / 2
        center = x + bar / 2
        label = f"{rung['rung']} client{'s' if len(rung['clients']) != 1 else ''}"
        parts.append(
            f'<text x="{center:.1f}" y="{height - bottom + 22}" text-anchor="middle">{escape(label)}</text>'
        )
        if rung["total_mbps"] is None:
            reason = rung.get("failure_status") or "not measured"
            parts.append(
                f'<text x="{center:.1f}" y="{y(0) - 12:.1f}" text-anchor="middle" '
                f'style="fill:var(--warn)">not measured</text>'
                f'<text x="{center:.1f}" y="{y(0) - 30:.1f}" text-anchor="middle" '
                f'style="fill:var(--warn)">{escape(reason)}</text>'
            )
            continue
        base = 0.0
        for client in rung["per_client"]:
            if client["client"] not in clients:
                clients.append(client["client"])
            value = client["mbps"]
            if value is None:
                continue
            color = COLORS[clients.index(client["client"]) % len(COLORS)]
            parts.append(
                f'<rect x="{x:.1f}" y="{y(base + value):.1f}" width="{bar:.1f}" '
                f'height="{y(base) - y(base + value):.1f}" fill="{color}"/>'
            )
            base += value
        parts.append(
            f'<text x="{center:.1f}" y="{y(base) - 8:.1f}" text-anchor="middle" '
            f'style="fill:var(--fg)">{rung["total_mbps"]:.1f}</text>'
        )
    parts.append("</svg>")
    return "".join(parts) + legend(clients)


def line_chart(title: str, unit: str, series: list[tuple[str, list[float], list[float | None]]]) -> str:
    """Simple line chart; gaps (None) break the line instead of dropping to zero."""
    width, height = 900, 220
    left, right, top, bottom = 70, 20, 24, 40
    xs = [x for _, t, _ in series for x in t]
    ys = [v for _, _, vals in series for v in vals if v is not None]
    if not xs or not ys:
        return ""
    xmax = max(xs) or 1.0
    ymax = max(ys) * 1.15 or 1.0

    def px(x: float) -> float:
        return left + x / xmax * (width - left - right)

    def py(v: float) -> float:
        return height - bottom - v / ymax * (height - top - bottom)

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">']
    for i in range(5):
        value = ymax * i / 4
        parts.append(f'<path d="M{left},{py(value):.1f} H{width - right}" stroke="var(--grid)"/>')
        parts.append(f'<text x="{left - 8}" y="{py(value) + 4:.1f}" text-anchor="end">{value:.3g}</text>')
    parts.append(f'<text x="{left}" y="14">{escape(title)} ({escape(unit)})</text>')
    parts.append(
        f'<text x="{width - right}" y="{height - 6}" text-anchor="end">seconds since scheduled start</text>'
    )
    for index, (_, times, values) in enumerate(series):
        color = COLORS[index % len(COLORS)]
        path: list[str] = []
        dots: list[str] = []
        pen_down = False
        for t, v in zip(times, values, strict=True):
            if v is None:
                pen_down = False
                continue
            path.append(f"{'L' if pen_down else 'M'}{px(t):.1f},{py(v):.1f}")
            pen_down = True
            # A dot per sample keeps isolated points (between gaps) visible.
            dots.append(f'<circle cx="{px(t):.1f}" cy="{py(v):.1f}" r="2" fill="{color}"/>')
        parts.append(f'<path d="{" ".join(path)}" fill="none" stroke="{color}" stroke-width="2"/>')
        parts.extend(dots)
    parts.append("</svg>")
    return "".join(parts) + legend([name for name, _, _ in series])


def _concurrent_section(concurrent: dict[str, Any] | None) -> str:
    out = ["<section><h2>1. Concurrent bandwidth</h2>"]
    if concurrent is None:
        out.append(
            '<p class="muted">This run has no concurrent-load (ladder) measurement. '
            "That is different from a measured 0 Mbit/s.</p></section>"
        )
        return "".join(out)
    spec = concurrent["spec"]
    out.append(
        f'<p class="muted">Each client requests {spec["transfer_bytes"]:,} bytes; shared deadline '
        f"{spec['deadline_s']:g} s; {spec['gap_s']:g} s between rungs; one ping to "
        f"{escape(spec['ping_target'])} every {spec['ping_interval_s']:g} s. Rates count only the "
        "common overlap, from the latest start to the earliest end; tails are excluded.</p>"
    )
    rungs = concurrent["rungs"]
    if any(r["total_mbps"] is not None for r in rungs):
        out.append(rung_chart(rungs))
    elif rungs:
        reasons = sorted({r["failure_status"] for r in rungs if r["failure_status"]})
        out.append(
            f'<p class="warn"><strong>No rung was measured</strong> ({escape(", ".join(reasons))}). '
            "There is no throughput to plot; this is missing data, not 0 Mbit/s.</p>"
        )
    rows = []
    for rung in rungs:
        rows.append(
            [
                rung["rung"],
                ", ".join(rung["clients"]),
                rung["status"]
                if rung["status"] == "measured"
                else f"{rung['status']}: {rung['failure_status']}",
                rung["total_mbps"],
                rung["overlap_s"],
                rung["start_skew_ms"],
                rung["gap_before_s"],
            ]
        )
    out.append(
        table(
            ["Rung", "Clients", "Status", "Total Mbit/s", "Overlap s", "Start skew ms", "Gap before s"], rows
        )
    )
    detail_rows = []
    for rung in rungs:
        for client in rung["per_client"]:
            detail_rows.append(
                [
                    rung["rung"],
                    client["client"],
                    client["mbps"],
                    client["overlap_bytes"],
                    client["downloaded_bytes"],
                    client["complete"],
                    client["sha256_verified"],
                    client["timed_out"],
                    client["ping_mean_ms"],
                    client["ping_loss_pct"],
                ]
            )
    if detail_rows:
        out.append("<h3>Per client within each rung</h3>")
        out.append(
            table(
                [
                    "Rung",
                    "Client",
                    "Mbit/s",
                    "Overlap bytes",
                    "Downloaded bytes",
                    "Complete",
                    "SHA-256 ok",
                    "Timed out",
                    "Ping mean ms",
                    "Ping loss %",
                ],
                detail_rows,
            )
        )
    for rung in rungs:
        throughput = []
        latency = []
        for client in rung["per_client"]:
            series = client.get("timeseries")
            if series:
                throughput.append((client["client"], series["t_s"], series["mbps"]))
                latency.append((client["client"], series["t_s"], series["rtt_ms"]))
        if throughput:
            out.append(f"<h3>Rung {rung['rung']} over time</h3>")
            out.append(line_chart("Throughput", "Mbit/s", throughput))
            out.append(line_chart("Ping RTT", "ms", latency))
    baseline = concurrent.get("baseline") or []
    if baseline:
        out.append("<h3>Direct baseline (no VPN)</h3>")
        out.append(
            table(["Client", "Mbit/s", "Bytes"], [[b["client"], b["mbps"], b["bytes"]] for b in baseline])
        )
        out.append(
            '<p class="muted">Simultaneous direct downloads from the origin. This checks origin '
            "capacity; it is not a VPN result.</p>"
        )
    out.append("</section>")
    return "".join(out)


def _per_client_section(per_client: list[dict[str, Any]]) -> str:
    out = ["<section><h2>2. Per-client results</h2>"]
    if not per_client:
        out.append('<p class="muted">No per-client results recorded.</p></section>')
        return "".join(out)
    out.append(
        table(
            ["Client", "Connected", "Connect s", "Exit", "Egress IP changed"],
            [
                [
                    c["client"],
                    c["connected"],
                    c["connect_time_s"],
                    c.get("exit_address"),
                    c.get("egress_changed"),
                ]
                for c in per_client
            ],
        )
    )
    rows = [
        [c["client"], m["kind"], m["label"], m.get("size_bytes"), m["mbps"], m["ms"]]
        for c in per_client
        for m in c.get("measurements", [])
    ]
    if rows:
        out.append("<h3>Measurements</h3>")
        out.append(table(["Client", "Kind", "Label", "Bytes", "Mbit/s", "ms"], rows))
    out.append("</section>")
    return "".join(out)


def _availability_section(availability: list[dict[str, Any]], failures: list[dict[str, Any]]) -> str:
    out = ["<section><h2>3. Availability and failures</h2>"]
    if availability:
        out.append(
            table(
                [
                    "Exit",
                    "Location",
                    "Address",
                    "Client",
                    "Status",
                    "Mbit/s",
                    "Ping ms",
                    "Loss %",
                    "API RTT ms",
                    "Detail",
                ],
                [
                    [
                        a["exit_label"],
                        a.get("location"),
                        a["exit_address"],
                        a["client"],
                        a["status"],
                        a["download_mbps"],
                        a["ping_mean_ms"],
                        a["ping_loss_pct"],
                        a["api_rtt_ms"],
                        a["detail"],
                    ]
                    for a in availability
                ],
            )
        )
        out.append(
            '<p class="muted">Availability checks use short downloads; their rates are not '
            "overlap-normalised and are not comparable with ladder results.</p>"
        )
    else:
        out.append('<p class="muted">No availability sweep in this run.</p>')
    if failures:
        out.append("<h3>Failures</h3>")
        out.append(
            table(
                ["Scope", "Subject", "Status", "Detail", "At"],
                [[f["scope"], f["subject"], f["status"], f["detail"], f.get("at")] for f in failures],
            )
        )
    else:
        out.append("<p>No failures recorded.</p>")
    out.append("</section>")
    return "".join(out)


def _provenance_section(result: dict[str, Any]) -> str:
    prov = result["provenance"]
    run = result["run"]
    profile = prov["profile"]
    target = prov["target"]
    client = prov["client"]
    infra = prov["infrastructure"]
    rows = [
        ["Run id", run["id"]],
        ["Kind", run["kind"]],
        ["Started / finished (UTC)", f"{run['started_at'] or 'n/a'} / {run['finished_at'] or 'n/a'}"],
        ["Target", f"{target['label']} ({target['name']})"],
        ["Exit address", target["exit_address"]],
        ["Network", f"{profile['network']} (target registered on {target['network']})"],
        ["Profile", f"{profile['name']} — {profile['worker_model']} worker, channel {profile['channel']}"],
        ["Package pin", f"{profile['package_version']} sha256 {profile['package_sha256']}"],
        ["Installed client", f"package {client['package_version']}, ctl {client['ctl_version']}"],
        ["Infrastructure", f"{infra['provider']} {infra['region'] or ''}".strip()],
        ["Bench", f"{prov['bench']['version']} @ {prov['bench']['commit']}"],
        ["Reproducible", "yes" if run["reproducible"] else "no — see notes"],
    ]
    out = ["<section><h2>4. Provenance</h2>", table(["Field", "Value"], rows)]
    machines = infra["machines"]
    if machines:
        out.append("<h3>Machines</h3>")
        out.append(
            table(
                ["Role", "Name", "Size", "vCPU", "RAM MiB", "CPU", "OS", "Kernel"],
                [
                    [
                        m["role"],
                        m["name"],
                        m["size"],
                        m["vcpus"],
                        m["memory_mib"],
                        m["cpu_model"],
                        m["os"],
                        m["kernel"],
                    ]
                    for m in machines
                ],
            )
        )
    if run["notes"]:
        out.append(f"<h3>Notes</h3><p>{escape(run['notes'])}</p>")
    out.append("</section>")
    return "".join(out)


def render(result: dict[str, Any]) -> str:
    validate(result)
    run = result["run"]
    target = result["provenance"]["target"]
    profile = result["provenance"]["profile"]
    title = f"{target['label']} · {profile['name']}"
    badge = "" if run["reproducible"] else ' <span class="badge warn">historical, not reproducible</span>'
    header = (
        f"<h1>Gnosis VPN: {escape(title)}</h1>"
        f'<p class="muted">{escape(run["started_at"] or run["created_at"])} · {escape(profile["network"])} · '
        f"run {escape(run['id'])}{badge}</p>"
    )
    body = "".join(
        [
            header,
            _concurrent_section(result["concurrent"]),
            _per_client_section(result["per_client"]),
            _availability_section(result["availability"], result["failures"]),
            _provenance_section(result),
            "<footer>Generated by gnosis_vpn-bench from result.json "
            f"(schema {escape(result['schema_version'])}). "
            "No JavaScript or external resources.</footer>",
        ]
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{CSS}</style></head>"
        f"<body><main>{body}</main></body></html>\n"
    )
