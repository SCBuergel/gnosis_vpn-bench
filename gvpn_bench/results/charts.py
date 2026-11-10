"""SVG charts for reports, in two flavours.

* ``HTML_THEME`` uses the page's CSS custom properties, so inline charts
  follow the viewer's light/dark theme.
* ``FILE_THEME`` uses fixed colours on a light panel, for standalone SVG
  files. GitHub shows those as images and strips CSS, so they must carry
  their own background to stay readable on both of its themes.

Gaps (``None``) break a line; they are never drawn as zero.
"""

from __future__ import annotations

import math
import subprocess
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from .collection import Series

SERIES_COLORS = ("#2563eb", "#ea580c", "#059669", "#9333ea", "#ca8a04", "#db2777", "#0891b2")
FONT_SIZE = 26
LINE_STYLES = ("", "12 6", "3 5", "12 5 3 5")
MARKERS = ("circle", "square", "triangle", "diamond", "cross", "plus")


@dataclass(frozen=True)
class PngChart:
    """Scannable vector source, converted to a metadata-free PNG only when written."""

    source: str


def rasterize(chart: PngChart) -> bytes:
    """Render at 3× logical resolution without adding Python plotting dependencies."""
    try:
        result = subprocess.run(
            ["rsvg-convert", "--format=png", "--zoom=3"],
            input=chart.source.encode(),
            capture_output=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as error:
        raise RuntimeError("PNG publication requires rsvg-convert (librsvg2-bin)") from error
    if result.returncode or not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("PNG chart rendering failed")
    return result.stdout


@dataclass(frozen=True)
class Theme:
    panel: str | None  # background rect fill; None = transparent
    surface: str  # fill for hollow markers
    text: str
    grid: str
    strong: str
    warn: str


HTML_THEME = Theme(
    panel=None, surface="var(--card)", text="var(--muted)", grid="var(--grid)", strong="var(--fg)", warn="var(--warn)"
)
FILE_THEME = Theme(panel="#ffffff", surface="#ffffff", text="#4b5563", grid="#e5e7eb", strong="#111827", warn="#9a3412")


def tick(value: float) -> str:
    """Axis label: whole numbers from 100 up, three significant digits below."""
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:.3g}"


def nice_scale(peak: float, ticks: int = 4) -> tuple[float, float]:
    """Return a readable major-tick step and the first major tick strictly above peak."""
    if peak <= 0:
        return 1.0, 0.25
    raw = peak / ticks
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    return step * (math.floor(peak / step) + 1), step


def color(index: int) -> str:
    return SERIES_COLORS[index % len(SERIES_COLORS)]


def _open(width: int, height: int, label: str, theme: Theme) -> list[str]:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-label="{escape(label)}" '
        f'font-family="DejaVu Sans, sans-serif" font-size="{FONT_SIZE}">'
    ]
    if theme.panel:
        parts.append(f'<rect width="{width}" height="{height}" rx="8" fill="{theme.panel}"/>')
    return parts


def _style(index: int, count: int) -> tuple[str, str]:
    if count <= 5:
        return "", "circle"
    return LINE_STYLES[index % len(LINE_STYLES)], MARKERS[index % len(MARKERS)]


def _marker(x: float, y: float, shape: str, stroke: str, fill: str, radius: float = 6) -> str:
    attrs = f'fill="{fill}" stroke="{stroke}" stroke-width="2"'
    if shape == "circle":
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" {attrs}/>'
    if shape == "square":
        return f'<rect x="{x - radius:.1f}" y="{y - radius:.1f}" width="{2 * radius}" height="{2 * radius}" {attrs}/>'
    if shape in ("triangle", "diamond"):
        points = [(x, y - radius), (x + radius, y + radius), (x - radius, y + radius)]
        if shape == "diamond":
            points = [(x, y - radius), (x + radius, y), (x, y + radius), (x - radius, y)]
        coords = " ".join(f"{a:.1f},{b:.1f}" for a, b in points)
        return f'<polygon points="{coords}" {attrs}/>'
    if shape == "cross":
        path = (
            f"M{x - radius:.1f},{y - radius:.1f} l{2 * radius},{2 * radius} "
            f"M{x - radius:.1f},{y + radius:.1f} l{2 * radius},{-2 * radius}"
        )
    else:
        path = f"M{x - radius:.1f},{y:.1f} h{2 * radius} M{x:.1f},{y - radius:.1f} v{2 * radius}"
    return f'<path d="{path}" {attrs}/>'


def _legend(
    parts: list[str],
    names: Sequence[str],
    x: float,
    y: float,
    right: float,
    theme: Theme,
    styles: Sequence[tuple[str, str, str]] | None = None,
) -> float:
    """Lay out bounded rows and reserve their height above the plotting area."""
    offset = x
    row_height = 42
    row_lines = 1
    sample_width = 64
    char_width = FONT_SIZE * 0.7
    available = right - x
    max_chars = max(1, int((available - sample_width - 16) / char_width))
    for index, name in enumerate(names):
        labels = textwrap.wrap(name, width=max_chars, break_on_hyphens=False) or [""]
        item_width = sample_width + max(map(len, labels)) * char_width + 16
        if offset > x and offset + item_width > right:
            offset = x
            y += row_height * row_lines
            row_lines = 1
        if styles is None:
            dash, shape = _style(index, len(names))
            stroke = color(index)
        else:
            stroke, dash, shape = styles[index]
        parts.append(f'<g class="legend-entry" data-series="{index}">')
        parts.append(
            f'<path d="M{offset:.1f},{y - 9:.1f} h48" fill="none" stroke="{stroke}" '
            f'stroke-width="3" stroke-dasharray="{dash}"/>'
        )
        parts.append(_marker(offset + 24, y - 9, shape, stroke, stroke))
        for line, label in enumerate(labels):
            parts.append(
                f'<text x="{offset + sample_width:.1f}" y="{y + line * row_height:.1f}" '
                f'fill="{theme.strong}">{escape(label)}</text>'
            )
        parts.append("</g>")
        row_lines = max(row_lines, len(labels))
        offset += item_width
    return y + row_height * row_lines


def overview_chart(series: list[Series], theme: Theme, width: int = 900, height: int = 850) -> str:
    """Total Mbit/s against concurrent clients: one line per series.

    Repetitions show as the mean with a min–max error bar.
    """
    left, right, bottom = 110, 32, 96
    legend: list[str] = []
    top = _legend(legend, [s.label for s in series], left, 80, width - right, theme) + 16
    height = max(height, int(top + bottom + 220))
    points = [s.points() for s in series]
    all_points = [p for pts in points for p in pts]
    xs = sorted({p.clients for p in all_points}) or [1]
    xmin, xmax = min(xs), max(xs)
    ymax, ystep = nice_scale(max((p.high for p in all_points), default=0.0))

    def px(clients: float) -> float:
        if xmax == xmin:
            return (left + width - right) / 2
        pad = 40
        return left + pad + (clients - xmin) / (xmax - xmin) * (width - left - right - 2 * pad)

    def py(value: float) -> float:
        return height - bottom - value / ymax * (height - top - bottom)

    parts = _open(width, height, "Total Mbit/s by number of concurrent clients", theme)
    for i in range(round(ymax / ystep) + 1):
        value = ystep * i
        parts.append(f'<path d="M{left},{py(value):.1f} H{width - right}" stroke="{theme.grid}"/>')
        parts.append(
            f'<text x="{left - 12}" y="{py(value) + 8:.1f}" text-anchor="end" fill="{theme.text}">{tick(value)}</text>'
        )
    for clients in xs:
        parts.append(
            f'<text x="{px(clients):.1f}" y="{height - bottom + 38}" text-anchor="middle" '
            f'fill="{theme.text}">{clients}</text>'
        )
    parts.append(
        f'<text x="{(left + width - right) / 2:.1f}" y="{height - 22}" text-anchor="middle" '
        f'fill="{theme.text}">concurrent clients</text>'
    )
    parts.append(f'<text x="{left}" y="34" fill="{theme.text}">Total Mbit/s over the common overlap</text>')
    if not all_points:
        parts.append(
            f'<text x="{width / 2:.1f}" y="{(top + height - bottom) / 2:.1f}" text-anchor="middle" fill="{theme.warn}">'
            f'<tspan x="{width / 2:.1f}">No rung was measured:</tspan>'
            f'<tspan x="{width / 2:.1f}" dy="36">missing data, not 0 Mbit/s</tspan></text>'
        )
    for index, s in enumerate(series):
        pts = points[index]
        stroke = color(index)
        dash, shape = _style(index, len(series))
        gaps = set(s.unmeasured())
        segments: list[list[str]] = [[]]
        previous: int | None = None
        for p in pts:
            # A rung attempted but never measured between two points breaks the line.
            if previous is not None and any(previous < gap < p.clients for gap in gaps):
                segments.append([])
            segments[-1].append(f"{'L' if segments[-1] else 'M'}{px(p.clients):.1f},{py(p.mean):.1f}")
            previous = p.clients
        for segment in segments:
            if len(segment) > 1:
                parts.append(
                    f'<path class="series-line" d="{" ".join(segment)}" fill="none" stroke="{stroke}" '
                    f'stroke-width="3" stroke-dasharray="{dash}"/>'
                )
        for p in pts:
            x = px(p.clients)
            if len(p.values) > 1:
                parts.append(
                    f'<path d="M{x:.1f},{py(p.low):.1f} V{py(p.high):.1f} '
                    f'M{x - 6:.1f},{py(p.low):.1f} H{x + 6:.1f} M{x - 6:.1f},{py(p.high):.1f} H{x + 6:.1f}" '
                    f'stroke="{stroke}" stroke-width="1.5"/>'
                )
            if p.missing:
                # Hollow marker plus "measured/attempted": some repetitions failed here.
                parts.append(_marker(x, py(p.mean), shape, stroke, theme.surface, radius=8))
                parts.append(
                    f'<text x="{x + 12:.1f}" y="{py(p.mean) + 8:.1f}" fill="{theme.warn}">'
                    f"{len(p.values)}/{p.attempts}</text>"
                )
            else:
                parts.append(_marker(x, py(p.mean), shape, stroke, stroke))
            if len(series) == 1:
                parts.append(
                    f'<text x="{x:.1f}" y="{py(p.mean) - 18:.1f}" text-anchor="middle" '
                    f'fill="{theme.strong}">{p.mean:.1f}</text>'
                )
    parts.extend(legend)
    parts.append("</svg>")
    return "".join(parts)


def total_series(
    lines: list[tuple[str, list[float], list[float | None]]],
) -> tuple[list[float], list[float | None]]:
    """Sum aligned samples only where every participating client has a value."""
    samples = []
    for _, times, values in lines:
        by_time = dict(zip(times, values, strict=True))
        if len(by_time) != len(times):
            raise ValueError("duplicate timestamps in a client series")
        samples.append(by_time)
    times = sorted({t for sample in samples for t in sample})
    totals: list[float | None] = []
    for t in times:
        values = [sample.get(t) for sample in samples]
        totals.append(math.fsum(v for v in values if v is not None) if all(v is not None for v in values) else None)
    return times, totals


def time_chart(
    title: str,
    unit: str,
    lines: list[tuple[str, list[float], list[float | None]]],
    theme: Theme,
    width: int = 900,
    height: int = 550,
    *,
    total: bool = False,
) -> str:
    """Per-second client series, optionally with their sum on an independent right axis."""
    left, right, bottom = 110, 110 if total else 32, 96 if total else 68
    styles = [(color(i), *_style(i, len(lines))) for i in range(len(lines))]
    names = [name for name, _, _ in lines]
    total_times, total_values = total_series(lines) if total else ([], [])
    if total:
        names.append("Total (right)")
        styles.append((theme.strong, "10 6", "diamond"))
    legend: list[str] = []
    top = _legend(legend, names, left, 80, width - right, theme, styles) + (42 if total else 12)
    height = max(height, int(top + bottom + 160))
    xs = [x for _, times, _ in lines for x in times]
    ys = [v for _, _, values in lines for v in values if v is not None]
    if not xs or not ys:
        return ""
    xmax = max(xs) or 1.0
    ymax, ystep = nice_scale(max(ys))
    total_max, total_step = nice_scale(max((v for v in total_values if v is not None), default=0.0))

    def px(x: float) -> float:
        return left + x / xmax * (width - left - right)

    def py(v: float, scale: float = ymax) -> float:
        return height - bottom - v / scale * (height - top - bottom)

    parts = _open(width, height, title, theme)
    for i in range(round(ymax / ystep) + 1):
        value = ystep * i
        parts.append(f'<path d="M{left},{py(value):.1f} H{width - right}" stroke="{theme.grid}"/>')
        parts.append(
            f'<text x="{left - 12}" y="{py(value) + 8:.1f}" text-anchor="end" fill="{theme.text}">{tick(value)}</text>'
        )
    parts.append(f'<text x="{left}" y="34" fill="{theme.text}">{escape(title)} ({escape(unit)})</text>')
    if total:
        _, xstep = nice_scale(xmax)
        for i in range(math.floor(xmax / xstep) + 1):
            value = xstep * i
            parts.append(
                f'<text class="time-tick" x="{px(value):.1f}" y="{height - bottom + 36}" '
                f'text-anchor="middle" fill="{theme.text}">{tick(value)}</text>'
            )
        parts.append(f'<text x="{left}" y="{top - 14}" fill="{theme.text}">Client {escape(unit)}</text>')
        parts.append(
            f'<text x="{width - right}" y="{top - 14}" text-anchor="end" '
            f'fill="{theme.strong}">Total {escape(unit)}</text>'
        )
        parts.append(f'<path d="M{width - right},{top} V{height - bottom}" stroke="{theme.strong}"/>')
        for i in range(round(total_max / total_step) + 1):
            value = total_step * i
            y = py(value, total_max)
            parts.append(f'<path d="M{width - right},{y:.1f} h6" stroke="{theme.strong}"/>')
            parts.append(
                f'<text class="right-tick" x="{width - right + 12}" y="{y + 8:.1f}" '
                f'fill="{theme.strong}">{tick(value)}</text>'
            )
        if not any(v is not None for v in total_values):
            parts.append(f'<text x="{left + 12}" y="{top + 32}" fill="{theme.warn}">Total: no complete samples</text>')
    parts.append(
        f'<text x="{width - right}" y="{height - 22}" text-anchor="end" fill="{theme.text}">'
        "seconds since scheduled start</text>"
    )
    plotted = [*lines, ("Total", total_times, total_values)] if total else lines
    for index, (_, times, values) in enumerate(plotted):
        is_total = total and index == len(lines)
        stroke, dash, shape = styles[index]
        scale = total_max if is_total else ymax
        path: list[str] = []
        dots: list[str] = []
        pen_down = False
        for t, v in zip(times, values, strict=True):
            if v is None:
                pen_down = False
                continue
            path.append(f"{'L' if pen_down else 'M'}{px(t):.1f},{py(v, scale):.1f}")
            dots.append(_marker(px(t), py(v, scale), shape, stroke, stroke, radius=4))
            pen_down = True
        parts.append(
            f'<path class="{"total-line" if is_total else "series-line"}" '
            f'data-axis="{"right" if is_total else "left"}" d="{" ".join(path)}" fill="none" stroke="{stroke}" '
            f'stroke-width="{3.5 if is_total else 2}" stroke-dasharray="{dash}"/>'
        )
        parts.extend(dots)
    parts.extend(legend)
    parts.append("</svg>")
    return "".join(parts)
