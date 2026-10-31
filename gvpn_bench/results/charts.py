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
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from .collection import Series

SERIES_COLORS = ("#2563eb", "#ea580c", "#059669", "#9333ea", "#ca8a04", "#db2777", "#0891b2")


@dataclass(frozen=True)
class Theme:
    panel: str | None  # background rect fill; None = transparent
    text: str
    grid: str
    strong: str
    warn: str


HTML_THEME = Theme(panel=None, text="var(--muted)", grid="var(--grid)", strong="var(--fg)", warn="var(--warn)")
FILE_THEME = Theme(panel="#ffffff", text="#4b5563", grid="#e5e7eb", strong="#111827", warn="#9a3412")


def tick(value: float) -> str:
    """Axis label: whole numbers from 100 up, three significant digits below."""
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:.3g}"


def nice_scale(peak: float, ticks: int = 4) -> tuple[float, float]:
    """(axis top, step) with a 1/2/2.5/5 × 10^k step covering *peak* plus headroom."""
    if peak <= 0:
        return 1.0, 0.25
    raw = peak * 1.1 / ticks
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    return step * math.ceil(peak * 1.1 / step), step


def color(index: int) -> str:
    return SERIES_COLORS[index % len(SERIES_COLORS)]


def _open(width: int, height: int, label: str, theme: Theme) -> list[str]:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" aria-label="{escape(label)}" '
        'font-family="system-ui, -apple-system, Segoe UI, sans-serif" font-size="13">'
    ]
    if theme.panel:
        parts.append(f'<rect width="{width}" height="{height}" rx="8" fill="{theme.panel}"/>')
    return parts


def _legend(parts: list[str], names: Sequence[str], x: float, y: float, theme: Theme) -> None:
    offset = x
    for index, name in enumerate(names):
        parts.append(f'<rect x="{offset:.1f}" y="{y - 9:.1f}" width="10" height="10" rx="2" fill="{color(index)}"/>')
        parts.append(f'<text x="{offset + 15:.1f}" y="{y:.1f}" fill="{theme.strong}">{escape(name)}</text>')
        offset += 30 + 7.5 * len(name)


def overview_chart(series: list[Series], theme: Theme, width: int = 900, height: int = 340) -> str:
    """Total Mbit/s against concurrent clients: one line per series.

    Repetitions show as the mean with a min–max error bar.
    """
    left, right, top, bottom = 70, 24, 44, 52
    points = {s.label: s.points() for s in series}
    all_points = [p for pts in points.values() for p in pts]
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
            f'<text x="{left - 8}" y="{py(value) + 4:.1f}" text-anchor="end" fill="{theme.text}">{tick(value)}</text>'
        )
    for clients in xs:
        parts.append(
            f'<text x="{px(clients):.1f}" y="{height - bottom + 20}" text-anchor="middle" '
            f'fill="{theme.text}">{clients}</text>'
        )
    parts.append(
        f'<text x="{(left + width - right) / 2:.1f}" y="{height - 10}" text-anchor="middle" '
        f'fill="{theme.text}">concurrent clients</text>'
    )
    parts.append(f'<text x="{left}" y="18" fill="{theme.text}">Total Mbit/s over the common overlap</text>')
    if not all_points:
        parts.append(
            f'<text x="{width / 2:.1f}" y="{height / 2:.1f}" text-anchor="middle" fill="{theme.warn}">'
            "No rung was measured: missing data, not 0 Mbit/s</text>"
        )
    for index, s in enumerate(series):
        pts = points[s.label]
        stroke = color(index)
        if len(pts) > 1:
            path = " ".join(f"{'M' if i == 0 else 'L'}{px(p.clients):.1f},{py(p.mean):.1f}" for i, p in enumerate(pts))
            parts.append(f'<path d="{path}" fill="none" stroke="{stroke}" stroke-width="2.5"/>')
        for p in pts:
            x = px(p.clients)
            if len(p.values) > 1:
                parts.append(
                    f'<path d="M{x:.1f},{py(p.low):.1f} V{py(p.high):.1f} '
                    f'M{x - 6:.1f},{py(p.low):.1f} H{x + 6:.1f} M{x - 6:.1f},{py(p.high):.1f} H{x + 6:.1f}" '
                    f'stroke="{stroke}" stroke-width="1.5"/>'
                )
            parts.append(f'<circle cx="{x:.1f}" cy="{py(p.mean):.1f}" r="4.5" fill="{stroke}"/>')
            if len(series) == 1:
                parts.append(
                    f'<text x="{x:.1f}" y="{py(p.mean) - 12:.1f}" text-anchor="middle" '
                    f'fill="{theme.strong}">{p.mean:.1f}</text>'
                )
    _legend(parts, [s.label for s in series], left, 36, theme)
    parts.append("</svg>")
    return "".join(parts)


def time_chart(
    title: str,
    unit: str,
    lines: list[tuple[str, list[float], list[float | None]]],
    theme: Theme,
    width: int = 900,
    height: int = 220,
) -> str:
    """Per-second series since the rung's scheduled start."""
    left, right, top, bottom = 70, 20, 44, 40
    xs = [x for _, times, _ in lines for x in times]
    ys = [v for _, _, values in lines for v in values if v is not None]
    if not xs or not ys:
        return ""
    xmax = max(xs) or 1.0
    ymax, ystep = nice_scale(max(ys))

    def px(x: float) -> float:
        return left + x / xmax * (width - left - right)

    def py(v: float) -> float:
        return height - bottom - v / ymax * (height - top - bottom)

    parts = _open(width, height, title, theme)
    for i in range(round(ymax / ystep) + 1):
        value = ystep * i
        parts.append(f'<path d="M{left},{py(value):.1f} H{width - right}" stroke="{theme.grid}"/>')
        parts.append(
            f'<text x="{left - 8}" y="{py(value) + 4:.1f}" text-anchor="end" fill="{theme.text}">{tick(value)}</text>'
        )
    parts.append(f'<text x="{left}" y="18" fill="{theme.text}">{escape(title)} ({escape(unit)})</text>')
    parts.append(
        f'<text x="{width - right}" y="{height - 8}" text-anchor="end" fill="{theme.text}">'
        "seconds since scheduled start</text>"
    )
    for index, (_, times, values) in enumerate(lines):
        stroke = color(index)
        path: list[str] = []
        dots: list[str] = []
        pen_down = False
        for t, v in zip(times, values, strict=True):
            if v is None:
                pen_down = False
                continue
            path.append(f"{'L' if pen_down else 'M'}{px(t):.1f},{py(v):.1f}")
            dots.append(f'<circle cx="{px(t):.1f}" cy="{py(v):.1f}" r="1.8" fill="{stroke}"/>')
            pen_down = True
        parts.append(f'<path d="{" ".join(path)}" fill="none" stroke="{stroke}" stroke-width="1.6"/>')
        parts.extend(dots)
    _legend(parts, [name for name, _, _ in lines], left, 36, theme)
    parts.append("</svg>")
    return "".join(parts)
