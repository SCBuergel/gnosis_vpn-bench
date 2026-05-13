#!/usr/bin/env python3
"""Record and plot gnosisVPN ping/curl traces, plus the `ping-load` test.

Ported from the standalone `gnosis_vpn-monitor` tool. Exposes four CLI
sub-command entry points (called from `gnosis_vpn-bench`'s argparse):

    cmd_ping        record ping latency to a host
    cmd_curl        record curl download throughput
    cmd_plot        render an SVG/PNG chart from a recording
    cmd_ping_load   run ping continuously and every N minutes fire a 10 MB
                    curl burst, appending its progress to a shared speed file

Recordings are plain text files: one timestamped sample per line, shaped
like `[<unix-seconds>.<microseconds>] <payload>`. Both recorders write
to stdout (tee) and to the configured file. Format is shared with the
upstream gnosis_vpn-monitor tool, so files are cross-compatible.

The plotter has no external dependencies (it emits raw SVG); the
optional PNG sibling needs rsvg-convert, ImageMagick or Inkscape on
$PATH.
"""

import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


log = logging.getLogger("gnosis_speedtest")


# ---------------------------------------------------------------------------
# Project layout
# ---------------------------------------------------------------------------
#
# Recordings live under data/ next to this module; rendered SVGs go under
# output/. Resolved relative to the module location (not the cwd) so the
# tool behaves the same regardless of where the user invokes it from.

SCRIPT_DIR = Path(__file__).parent.resolve()
DATA_DIR = SCRIPT_DIR / "data"
OUTPUT_DIR = SCRIPT_DIR / "output"


# ---------------------------------------------------------------------------
# Recording defaults
# ---------------------------------------------------------------------------
#
# Default output paths embed a process-start timestamp ("ping--YYYY-MM-DD
# --HH-MM-SS.txt") so re-running a recorder never silently overwrites the
# previous session's data. The timestamp is computed once at import time
# so every default produced in one invocation shares it — handy for
# `ping-load`, where the ping recording and the speed file should carry
# the same session stamp.

_RUN_TS = datetime.now().strftime("%Y-%m-%d--%H-%M-%S")

DEFAULT_PING_HOST = "google.com"
DEFAULT_CURL_URL = (
    "https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.0.5.tar.xz"
)
DEFAULT_PING_FILE = str(DATA_DIR / f"ping--{_RUN_TS}.txt")
DEFAULT_CURL_FILE = str(DATA_DIR / f"curl--{_RUN_TS}.txt")

# ping-load: speed--TS.txt is the shared, append-only file that
# accumulates every curl burst's progress meter; pingload's curl URL
# defaults to Cloudflare's 10 MB anycast endpoint so the bench's existing
# downstream tooling agrees on the payload size.
DEFAULT_SPEED_FILE = str(DATA_DIR / f"speed--{_RUN_TS}.txt")
DEFAULT_PING_LOAD_INTERVAL_MIN = 30
DEFAULT_PING_LOAD_CURL_URL = (
    "https://speed.cloudflare.com/__down?bytes=10485760"  # 10 MB
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

TIMESTAMP_RE = re.compile(r"\[(\d+(?:\.\d+)?)[^\]]*\]")
PING_TIME_RE = re.compile(r"time=([\d.]+)\s*ms")
CURL_SPEED_RE = re.compile(r"^(\d+(?:\.\d+)?)([kMG]?)$")
SPEED_UNIT_BYTES = {"": 1, "k": 1024, "M": 1024 ** 2, "G": 1024 ** 3}


def parse_speed(token):
    """Return a curl speed token in bytes/second, or None if unparseable."""
    m = CURL_SPEED_RE.match(token)
    if not m:
        return None
    return float(m.group(1)) * SPEED_UNIT_BYTES[m.group(2)]


def parse_recording(path):
    """Parse a ping or curl recording.

    Returns ``(kind, points)`` with ``kind`` ∈ {"ping","curl"} and
    ``points`` a list of ``(unix_timestamp, value)`` pairs in file order.
    Unrecognised lines are silently skipped — recordings made by multiple
    back-to-back curl bursts (as in ping-load mode) parse cleanly even
    though they contain repeated curl banner/handshake lines between
    progress segments.
    """
    points = []
    kind = None

    with open(path) as f:
        for line in f:
            ts_match = TIMESTAMP_RE.search(line)
            if not ts_match:
                continue
            t = float(ts_match.group(1))
            rest = line[ts_match.end():]

            ping_match = PING_TIME_RE.search(rest)
            if ping_match:
                points.append((t, float(ping_match.group(1))))
                kind = "ping"
                continue

            tokens = rest.split()
            if tokens:
                speed = parse_speed(tokens[-1])
                if speed is not None:
                    points.append((t, speed))
                    kind = "curl"

    if not points:
        raise ValueError(f"no recognisable ping or curl data in {path}")
    return kind, points


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------

def format_ms(v):
    if v == 0:
        return "0"
    return f"{v:g}"


def format_bytes_per_second(v):
    if v <= 0:
        return "0"
    for unit, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if v >= unit:
            return f"{v / unit:.3g}{suffix}"
    return f"{v:.0f}"


KIND_LABEL = {"ping": "RTT (ms)", "curl": "Speed (B/s)"}
KIND_FORMATTER = {"ping": format_ms, "curl": format_bytes_per_second}


# ---------------------------------------------------------------------------
# Style parsing (matplotlib-style format strings: "xb", "o-r", ".--g", …)
# ---------------------------------------------------------------------------

# Bare one-letter codes are the original CSS colours (back-compat — any
# style string that worked before still renders the same colour).
#
# The numbered variants 1/2/3 (light/mid/dark) come from a fixed
# OKLCH-derived palette: six hue angles (25° 95° 155° 200° 265° 325°)
# chosen to be perceptually equidistant around the wheel, three
# lightness levels (0.78 / 0.58 / 0.38) at chroma 0.14, serialized to
# sRGB. The warm-to-cool hues hit 0.14 cleanly; yellow/green/cyan darks
# get clipped to ~0.07–0.12 by the sRGB gamut (there's no such thing as
# a saturated dark yellow in sRGB) and read slightly more muted than the
# others — an unavoidable sRGB limit, not an aesthetic choice.
#
# `k` and `w` have no shade ladder.
COLOR_MAP = {
    # blue family — OKLCH H≈265°
    "b":  "blue",            # bare: pure blue (#0000ff, original)
    "b1": "#8BB5FF",         # light  L=0.77 C=0.12 (gamut-clipped from 0.14)
    "b2": "#5176CD",         # mid    L=0.58 C=0.14
    "b3": "#1C3A8B",         # dark   L=0.38 C=0.14
    # green family — OKLCH H≈155°
    "g":  "green",           # bare: pure green (#008000, original)
    "g1": "#63D18F",         # light  L=0.78 C=0.14
    "g2": "#0E9254",         # mid    L=0.58 C=0.14
    "g3": "#00561C",         # dark   L=0.40 C=0.12 (gamut-clipped, H drifts to 147°)
    # red family — OKLCH H≈25°
    "r":  "red",             # bare: pure red (#ff0000, original)
    "r1": "#FF9189",         # light  L=0.77 C=0.13
    "r2": "#BF534E",         # mid    L=0.58 C=0.14
    "r3": "#7C1117",         # dark   L=0.38 C=0.14
    # cyan family — OKLCH H≈200°
    "c":  "cyan",            # bare: pure cyan (#00ffff, original)
    "c1": "#00D1DA",         # light  L=0.78 C=0.13
    "c2": "#00919B",         # mid    L=0.60 C=0.10 (gamut-clipped)
    "c3": "#00555F",         # dark   L=0.41 C=0.07 (gamut-clipped; reads muted)
    # purple/magenta family — OKLCH H≈325°
    "m":  "magenta",         # bare: pure magenta (#ff00ff, original)
    "m1": "#E497E8",         # light  L=0.78 C=0.14
    "m2": "#A35AA7",         # mid    L=0.58 C=0.14
    "m3": "#651E6A",         # dark   L=0.38 C=0.14
    # yellow family — OKLCH H≈95°
    "y":  "olive",           # bare: olive (#808000, original — pure
                             # yellow is unreadable on white)
    "y1": "#D3B63B",         # light  L=0.78 C=0.14
    "y2": "#957700",         # mid    L=0.58 C=0.12 (gamut-clipped)
    "y3": "#5A3E00",         # dark   L=0.39 C=0.08 (gamut-clipped; reads muted)
    # neutrals — no shade ladder
    "k":  "black",
    "w":  "white",
}

# Letters that may take a digit suffix to select a shade. `k` and `w`
# are deliberately excluded: black and white don't have a shade family.
SHADED_COLOR_LETTERS = set("brgcmy")

LINESTYLE_DASH = {
    "-":  None,
    "--": "12,6",
    "-.": "10,5,2,5",
    ":":  "0,6",
}

MARKER_DEFS = {
    ".": '<g id="m-dot"><circle r="2" fill="currentColor"/></g>',
    "o": '<g id="m-circle"><circle r="6" fill="none" stroke="currentColor" stroke-width="2"/></g>',
    "x": '<g id="m-x"><path d="M-6,-6L6,6M-6,6L6,-6" stroke="currentColor" stroke-width="2" fill="none"/></g>',
    "+": '<g id="m-plus"><path d="M-6,0L6,0M0,-6L0,6" stroke="currentColor" stroke-width="2" fill="none"/></g>',
    "s": '<g id="m-square"><rect x="-5" y="-5" width="10" height="10" fill="none" stroke="currentColor" stroke-width="2"/></g>',
    "D": '<g id="m-diamond"><polygon points="0,-7 6,0 0,7 -6,0" fill="none" stroke="currentColor" stroke-width="2"/></g>',
    "^": '<g id="m-tri-up"><polygon points="0,-7 6,5 -6,5" fill="none" stroke="currentColor" stroke-width="2"/></g>',
    "v": '<g id="m-tri-down"><polygon points="0,7 6,-5 -6,-5" fill="none" stroke="currentColor" stroke-width="2"/></g>',
    "*": '<g id="m-star"><polygon points="0,-7 1.6,-2.2 6.7,-2.2 2.7,0.8 4.1,5.7 0,2.8 -4.1,5.7 -2.7,0.8 -6.7,-2.2 -1.6,-2.2" fill="currentColor"/></g>',
}

MARKER_HREFS = {
    ".": "#m-dot",     "o": "#m-circle",    "x": "#m-x",      "+": "#m-plus",
    "s": "#m-square",  "D": "#m-diamond",   "^": "#m-tri-up", "v": "#m-tri-down",
    "*": "#m-star",
}

DEFAULT_COLOR_CODE = "b"


def _xml_escape(s):
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


def parse_style(spec):
    """Parse a matplotlib-style format string into (linestyle, marker, color).

    Colour codes accept an optional 1/2/3 shade suffix (e.g. `r3`,
    `b1`) for the colourful families. The suffix is only consumed when
    the resulting two-char token is actually defined in COLOR_MAP — so
    a stray digit (`b9`) doesn't get silently absorbed and instead
    triggers the usual "unknown character" error.
    """
    linestyle = marker = color = None
    i = 0
    while i < len(spec):
        # Two-char linestyles checked first so "--" doesn't lose its first "-".
        if spec[i:i + 2] in ("--", "-."):
            if linestyle is not None:
                raise ValueError(f"duplicate linestyle in style {spec!r}")
            linestyle = spec[i:i + 2]
            i += 2
            continue
        ch = spec[i]
        if ch in ("-", ":"):
            if linestyle is not None:
                raise ValueError(f"duplicate linestyle in style {spec!r}")
            linestyle = ch
            i += 1
            continue
        if ch in MARKER_DEFS:
            if marker is not None:
                raise ValueError(f"duplicate marker in style {spec!r}")
            marker = ch
            i += 1
            continue
        if ch in COLOR_MAP:
            if color is not None:
                raise ValueError(f"duplicate colour in style {spec!r}")
            two = spec[i:i + 2]
            if (ch in SHADED_COLOR_LETTERS
                    and len(two) == 2
                    and two in COLOR_MAP):
                color = two
                i += 2
            else:
                color = ch
                i += 1
            continue
        raise ValueError(
            f"unknown character {ch!r} in style {spec!r} — "
            "expected one of linestyle (- -- -. :), "
            f"marker ({' '.join(sorted(MARKER_DEFS))}), "
            "or colour (b g r c m y k w; the colourful families also "
            "accept a 1/2/3 shade suffix, e.g. b3, r1, g2)"
        )
    return linestyle, marker, COLOR_MAP[color or DEFAULT_COLOR_CODE]


# ---------------------------------------------------------------------------
# Tick generation
# ---------------------------------------------------------------------------

def nice_step(raw):
    if raw <= 0:
        return 1
    mag = 10 ** math.floor(math.log10(raw))
    norm = raw / mag
    if norm <= 1.5:
        return 1 * mag
    if norm <= 3:
        return 2 * mag
    if norm <= 7:
        return 5 * mag
    return 10 * mag


def linear_ticks(lo, hi, n=5):
    if hi <= lo:
        return [lo]
    step = nice_step((hi - lo) / n)
    out = []
    v = math.ceil(lo / step) * step
    while v <= hi + step * 1e-9:
        out.append(0 if abs(v) < step * 1e-9 else round(v, 12))
        v += step
    return out


TIME_TICK_STEPS = [
    1, 2, 5, 10, 15, 30,
    60, 2 * 60, 5 * 60, 10 * 60, 15 * 60, 30 * 60,
    3600, 2 * 3600, 3 * 3600, 6 * 3600, 12 * 3600,
    86400, 2 * 86400, 7 * 86400, 14 * 86400, 28 * 86400,
    90 * 86400, 365 * 86400,
]


def time_ticks(t0, t1, n=5):
    if t1 <= t0:
        return [t0]
    raw = (t1 - t0) / n
    step = next((s for s in TIME_TICK_STEPS if s >= raw), TIME_TICK_STEPS[-1])
    origin = datetime.fromtimestamp(t0).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    out = []
    k = math.ceil((t0 - origin) / step - 1e-9)
    v = origin + k * step
    while v <= t1 + step * 1e-9:
        out.append(v)
        v += step
    return out


def log_decade_ticks(lo, hi):
    if lo <= 0 or hi <= 0:
        return []
    lo_e = math.ceil(math.log10(lo) - 1e-9)
    hi_e = math.floor(math.log10(hi) + 1e-9)
    if hi_e < lo_e:
        return []
    return [10 ** e for e in range(lo_e, hi_e + 1)]


def log_minor_ticks(lo, hi):
    if lo <= 0 or hi <= 0:
        return []
    lo_e = math.floor(math.log10(lo))
    hi_e = math.ceil(math.log10(hi))
    out = []
    for e in range(lo_e, hi_e + 1):
        base = 10 ** e
        for m in range(2, 10):
            v = m * base
            if lo <= v <= hi:
                out.append(v)
    return out


# ---------------------------------------------------------------------------
# Axes
# ---------------------------------------------------------------------------

class Axis:
    _MINOR_TICK_DECADE_LIMIT = 4

    def __init__(self, kind, values, log=False):
        self.kind = kind
        self.label = KIND_LABEL[kind]
        self.format = KIND_FORMATTER[kind]
        self.log = log

        if log:
            positive = [v for v in values if v > 0]
            if not positive:
                raise ValueError(f"cannot log-scale {kind}: no positive samples")
            self.lo = min(positive) / 2
            self.hi = max(positive) * 2
            self.lo_t = math.log10(self.lo)
            self.hi_t = math.log10(self.hi)
            self._show_minor = (self.hi_t - self.lo_t) < self._MINOR_TICK_DECADE_LIMIT
            self._step = None
        else:
            vmin, vmax = min(values), max(values)
            if vmin == vmax:
                vmin -= 0.5
                vmax += 0.5
            self._step = nice_step((vmax - vmin) / 5)
            self.lo = math.floor(vmin / self._step) * self._step
            self.hi = math.ceil(vmax / self._step) * self._step
            if self.hi == self.lo:
                self.hi = self.lo + self._step
            self.lo_t = self.lo
            self.hi_t = self.hi
            self._show_minor = False

    def transform(self, v):
        if self.log:
            return math.log10(max(v, self.lo))
        return v

    def is_visible(self, v):
        return not self.log or v > 0

    def major_ticks(self):
        if self.log:
            decades = log_decade_ticks(self.lo, self.hi)
            if decades:
                return decades
            return log_minor_ticks(self.lo, self.hi)
        return linear_ticks(self.lo, self.hi)

    def minor_ticks(self):
        if not self.log or not self._show_minor:
            return []
        if not log_decade_ticks(self.lo, self.hi):
            return []
        return log_minor_ticks(self.lo, self.hi)


# ---------------------------------------------------------------------------
# SVG rendering
# ---------------------------------------------------------------------------

DEFAULT_WIDTH = 1800
DEFAULT_HEIGHT = 840
MARGIN_LEFT = 80
MARGIN_RIGHT = 80
MARGIN_TOP = 30
MARGIN_BOTTOM = 50

FONT_SIZE = 14
STROKE_WIDTH = 3

# Default --style values per chart shape. Hand-picked for the small
# cases so the canonical 1-series and 2-series charts look identical to
# the original tool; longer cycles kick in once N+M > 2.
#
# Same-axis defaults rotate marker too (x → o → +), since multiple
# series sharing one y-scale benefit from shape disambiguation when
# samples overlap. Cross-axis defaults stick with the "x" marker per
# series and just rotate colour — readers separate the two sides by
# spine/label colour rather than glyph.

DEFAULT_SINGLE_AXIS_STYLES = ["xb", "or", "xg", "oc", "+m", "sy", "Dk"]
LEFT_AXIS_COLOR_CYCLE = ["b", "g", "c", "m"]
RIGHT_AXIS_COLOR_CYCLE = ["r", "y", "k"]


def default_styles_for_axes(n_left, n_right):
    """Return one style string per series in left-then-right order.

    Special-cases the historical 1-series and 1+1 charts so their look
    is preserved exactly; falls back to the cycles above for larger N+M.
    """
    if n_right == 0:
        if n_left == 1:
            return ["xb"]
        return [DEFAULT_SINGLE_AXIS_STYLES[i % len(DEFAULT_SINGLE_AXIS_STYLES)]
                for i in range(n_left)]
    if n_left == 1 and n_right == 1:
        return ["xb", "xr"]
    left = [f"x{LEFT_AXIS_COLOR_CYCLE[i % len(LEFT_AXIS_COLOR_CYCLE)]}"
            for i in range(n_left)]
    right = [f"x{RIGHT_AXIS_COLOR_CYCLE[i % len(RIGHT_AXIS_COLOR_CYCLE)]}"
             for i in range(n_right)]
    return left + right


class Plot:
    def __init__(self, x0, x1, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT):
        self.x0 = x0
        self.x1 = x1
        self.width = width
        self.height = height

    def x_pixel(self, x):
        if self.x1 == self.x0:
            return MARGIN_LEFT
        span = self.width - MARGIN_LEFT - MARGIN_RIGHT
        return MARGIN_LEFT + (x - self.x0) / (self.x1 - self.x0) * span

    def y_pixel(self, y, axis):
        v = axis.transform(y)
        span = self.height - MARGIN_TOP - MARGIN_BOTTOM
        return self.height - MARGIN_BOTTOM - (v - axis.lo_t) / (axis.hi_t - axis.lo_t) * span

    def header(self):
        return [
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{self.width}" height="{self.height}" '
            f'viewBox="0 0 {self.width} {self.height}" '
            f'font-family="sans-serif" font-size="{FONT_SIZE}">',
            f'<rect width="{self.width}" height="{self.height}" fill="white"/>',
        ]

    @staticmethod
    def footer():
        return ["</svg>"]

    def frame(self, left_color="black", right_color="black"):
        L = MARGIN_LEFT
        R = self.width - MARGIN_RIGHT
        T = MARGIN_TOP
        B = self.height - MARGIN_BOTTOM
        return [
            f'<line x1="{L}" y1="{T}" x2="{R}" y2="{T}" stroke="black"/>',
            f'<line x1="{L}" y1="{B}" x2="{R}" y2="{B}" stroke="black"/>',
            f'<line x1="{L}" y1="{T}" x2="{L}" y2="{B}" stroke="{left_color}"/>',
            f'<line x1="{R}" y1="{T}" x2="{R}" y2="{B}" stroke="{right_color}"/>',
        ]

    def x_axis(self):
        out = []
        bottom = self.height - MARGIN_BOTTOM
        ticks = time_ticks(self.x0, self.x1)
        step = ticks[1] - ticks[0] if len(ticks) > 1 else (self.x1 - self.x0)

        if step >= 86400:
            time_fmt = None
        elif step >= 60:
            time_fmt = "%H:%M"
        else:
            time_fmt = "%H:%M:%S"

        dates = [datetime.fromtimestamp(t).date() for t in ticks]
        show_date_row = time_fmt is not None and len(set(dates)) > 1

        prev_date = None
        for t, d in zip(ticks, dates):
            px = self.x_pixel(t)
            out.append(
                f'<line x1="{px}" y1="{bottom}" x2="{px}" y2="{bottom + 4}" '
                f'stroke="black"/>'
            )
            dt = datetime.fromtimestamp(t)
            if time_fmt is None:
                out.append(
                    f'<text x="{px}" y="{bottom + 18}" text-anchor="middle">'
                    f'{dt.strftime("%Y-%m-%d")}</text>'
                )
            else:
                out.append(
                    f'<text x="{px}" y="{bottom + 18}" text-anchor="middle">'
                    f'{dt.strftime(time_fmt)}</text>'
                )
                if show_date_row and d != prev_date:
                    out.append(
                        f'<text x="{px}" y="{bottom + 18 + FONT_SIZE + 2}" '
                        f'text-anchor="middle" fill="#666">'
                        f'{dt.strftime("%Y-%m-%d")}</text>'
                    )
            prev_date = d

        if not show_date_row:
            cx = (MARGIN_LEFT + self.width - MARGIN_RIGHT) / 2
            out.append(
                f'<text x="{cx}" y="{self.height - 10}" text-anchor="middle">time</text>'
            )
        return out

    def y_axis(self, axis, side, color):
        out = []
        left = MARGIN_LEFT
        right = self.width - MARGIN_RIGHT

        for v in axis.minor_ticks():
            py = self.y_pixel(v, axis)
            if side == "left":
                out.append(
                    f'<line x1="{left - 2}" y1="{py}" x2="{left}" y2="{py}" '
                    f'stroke="{color}"/>'
                )
            else:
                out.append(
                    f'<line x1="{right}" y1="{py}" x2="{right + 2}" y2="{py}" '
                    f'stroke="{color}"/>'
                )

        for v in axis.major_ticks():
            py = self.y_pixel(v, axis)
            if side == "left":
                out.append(
                    f'<line x1="{left - 4}" y1="{py}" x2="{left}" y2="{py}" '
                    f'stroke="{color}"/>'
                )
                out.append(
                    f'<line x1="{left}" y1="{py}" x2="{right}" y2="{py}" stroke="#eee"/>'
                )
                out.append(
                    f'<text x="{left - 6}" y="{py + 4}" text-anchor="end" '
                    f'fill="{color}">{axis.format(v)}</text>'
                )
            else:
                out.append(
                    f'<line x1="{right}" y1="{py}" x2="{right + 4}" y2="{py}" '
                    f'stroke="{color}"/>'
                )
                out.append(
                    f'<text x="{right + 6}" y="{py + 4}" text-anchor="start" '
                    f'fill="{color}">{axis.format(v)}</text>'
                )

        cy = (MARGIN_TOP + self.height - MARGIN_BOTTOM) / 2
        x = left - 55 if side == "left" else right + 55
        out.append(
            f'<text x="{x}" y="{cy}" transform="rotate(-90 {x},{cy})" '
            f'text-anchor="middle" fill="{color}">{axis.label}</text>'
        )
        return out

    def legend(self, entries):
        if not entries:
            return []

        line_h = FONT_SIZE * 1.5
        pad = FONT_SIZE * 0.6
        sample_w = FONT_SIZE * 2.5

        def _approx_width(s):
            spaces = s.count(" ")
            return ((len(s) - spaces) * 0.7 + spaces * 0.3) * FONT_SIZE
        text_w = max(_approx_width(lbl) for lbl, *_ in entries)
        box_w = sample_w + text_w + pad * 3
        box_h = len(entries) * line_h + pad

        chart_right = self.width - MARGIN_RIGHT
        chart_top = MARGIN_TOP
        box_x = chart_right - box_w - 10
        box_y = chart_top + 10

        out = [
            f'<rect x="{box_x}" y="{box_y}" width="{box_w}" height="{box_h}" '
            f'fill="white" stroke="#888" stroke-width="1"/>'
        ]

        sample_x0 = box_x + pad
        sample_x1 = sample_x0 + sample_w
        for i, (label, color, linestyle, marker) in enumerate(entries):
            cy = box_y + pad / 2 + line_h * (i + 0.5)
            if linestyle is not None:
                attrs = [
                    f'x1="{sample_x0:.1f}"', f'y1="{cy:.1f}"',
                    f'x2="{sample_x1:.1f}"', f'y2="{cy:.1f}"',
                    f'stroke="{color}"',
                    f'stroke-width="{STROKE_WIDTH}"',
                    'stroke-linecap="round"',
                ]
                dash = LINESTYLE_DASH[linestyle]
                if dash:
                    attrs.append(f'stroke-dasharray="{dash}"')
                out.append(f'<line {" ".join(attrs)}/>')
            if marker is not None:
                mx = (sample_x0 + sample_x1) / 2
                out.append(
                    f'<use href="{MARKER_HREFS[marker]}" '
                    f'x="{mx:.1f}" y="{cy:.1f}" color="{color}"/>'
                )
            out.append(
                f'<text x="{sample_x1 + pad:.1f}" y="{cy + FONT_SIZE * 0.35:.1f}" '
                f'fill="black">{_xml_escape(label)}</text>'
            )
        return out

    def series(self, points, axis, color, linestyle=None, marker=None):
        visible = [(x, y) for x, y in points if axis.is_visible(y)]
        if not visible:
            return []

        out = []
        coords = [(self.x_pixel(x), self.y_pixel(y, axis)) for x, y in visible]

        if linestyle is not None:
            path = "M " + " L ".join(f"{px:.1f},{py:.1f}" for px, py in coords)
            attrs = [
                f'd="{path}"',
                f'stroke="{color}"',
                'fill="none"',
                f'stroke-width="{STROKE_WIDTH}"',
                'stroke-linecap="round"',
                'stroke-linejoin="round"',
            ]
            dash = LINESTYLE_DASH[linestyle]
            if dash:
                attrs.append(f'stroke-dasharray="{dash}"')
            out.append(f'<path {" ".join(attrs)}/>')

        if marker is not None:
            href = MARKER_HREFS[marker]
            for px, py in coords:
                out.append(
                    f'<use href="{href}" x="{px:.1f}" y="{py:.1f}" '
                    f'color="{color}"/>'
                )

        return out


def render(left_series, right_series, log_y=False,
           width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
           styles=None, labels=None):
    """Render N left-axis + M right-axis series into a complete SVG.

    ``left_series`` and ``right_series`` are each a list of
    ``(kind, points)`` tuples. All series on the same axis must share a
    kind (all ping, or all curl), because a y-axis carries one unit.
    Cross-axis can mix freely — that's the whole point of having two
    axes.

    Chart shapes follow from the counts:
      * N=1, M=0   single-series chart, frame & axis tinted to the colour.
      * N>=2, M=0  shared left axis; spine and label stay black, series
                   carry distinct styles, legend disambiguates.
      * N>=1, M>=1 double-y. Each side's spine/label is tinted when it
                   carries one series and black otherwise — keeps the
                   1+1 case visually identical to the original.

    ``styles`` is one matplotlib-style string per series in
    left-then-right order; if shorter than the series count the last
    entry is recycled. ``labels`` is one legend entry per series in the
    same order (None → no legend, or filename basenames if the caller
    chooses).
    """
    n_left = len(left_series)
    n_right = len(right_series)
    if n_left + n_right < 1:
        raise ValueError("render needs at least one series")

    # A y-axis labels one unit, so each side must be one kind. The
    # cross-axis case is exactly what gets the two axes.
    for side_name, group in (("left", left_series), ("right", right_series)):
        kinds = {k for k, _ in group}
        if len(kinds) > 1:
            raise ValueError(
                f"{side_name}-axis files must all be the same kind "
                f"(all ping, or all curl); got {sorted(kinds)}"
            )

    all_series = left_series + right_series
    if styles is None:
        styles = default_styles_for_axes(n_left, n_right)
    parsed = [
        parse_style(styles[min(i, len(styles) - 1)])
        for i in range(len(all_series))
    ]
    left_parsed = parsed[:n_left]
    right_parsed = parsed[n_left:]

    all_xs = [t for _, pts in all_series for t, _ in pts]
    plot = Plot(min(all_xs), max(all_xs), width=width, height=height)

    out = plot.header()

    markers_used = sorted({m for _, m, _ in parsed if m is not None})
    if markers_used:
        out.append("<defs>" + "".join(MARKER_DEFS[m] for m in markers_used) + "</defs>")

    double_y = n_left > 0 and n_right > 0

    def _draw_side(side_series, side_parsed, side_name):
        """Draw one axis and every series on it."""
        kind = side_series[0][0]
        all_vals = [v for _, pts in side_series for _, v in pts]
        axis = Axis(kind, all_vals, log=log_y)
        # When a side carries exactly one series, tint its spine/label
        # to that series' colour (original 1- and 1+1-series look). With
        # multiple series the colour belongs to the legend, not the axis.
        axis_color = side_parsed[0][2] if len(side_series) == 1 else "black"
        lines = plot.y_axis(axis, side=side_name, color=axis_color)
        for (_, pts), (linestyle, marker, color) in zip(side_series, side_parsed):
            lines += plot.series(pts, axis, color=color,
                                 linestyle=linestyle, marker=marker)
        return axis_color, lines

    if double_y:
        left_axis_color, left_lines = _draw_side(left_series, left_parsed, "left")
        right_axis_color, right_lines = _draw_side(right_series, right_parsed, "right")
        out += plot.frame(left_color=left_axis_color, right_color=right_axis_color)
        out += left_lines + right_lines
    else:
        # Single axis. Right-only was already folded into left by the caller.
        left_axis_color, left_lines = _draw_side(left_series, left_parsed, "left")
        out += plot.frame(left_color=left_axis_color, right_color="black")
        out += left_lines

    out += plot.x_axis()

    if labels:
        if len(labels) != len(all_series):
            raise ValueError(
                f"got {len(labels)} legend labels for {len(all_series)} series"
            )
        entries = [
            (labels[i], parsed[i][2], parsed[i][0], parsed[i][1])
            for i in range(len(all_series))
        ]
        out += plot.legend(entries)

    out += plot.footer()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Output naming and SVG → PNG conversion
# ---------------------------------------------------------------------------

TIMESTAMP_FORMAT = "%Y-%m-%d--%H-%M-%S"

SVG_TO_PNG_TOOLS = [
    ["rsvg-convert", "-w", "{w}", "-h", "{h}", "-o", "{png}", "{svg}"],
    ["magick", "{svg}", "-resize", "{w}x{h}", "{png}"],
    ["convert", "{svg}", "-resize", "{w}x{h}", "{png}"],
    ["inkscape", "{svg}",
     "--export-type=png",
     "--export-width={w}", "--export-height={h}",
     "--export-filename={png}"],
]


def auto_output_path(series_list):
    kinds = {k for k, _ in series_list}
    prefix = kinds.pop() if len(kinds) == 1 else "combined"
    ts = datetime.now().strftime(TIMESTAMP_FORMAT)
    return os.path.join(str(OUTPUT_DIR), f"{prefix}-{ts}.svg")


def svg_to_png(svg_path, svg_width, svg_height, scale=1.0):
    png_path = os.path.splitext(svg_path)[0] + ".png"
    w = max(1, int(round(svg_width * scale)))
    h = max(1, int(round(svg_height * scale)))
    for template in SVG_TO_PNG_TOOLS:
        if not shutil.which(template[0]):
            continue
        cmd = [
            arg.format(svg=svg_path, png=png_path, w=w, h=h)
            for arg in template
        ]
        try:
            subprocess.run(
                cmd, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return png_path
        except subprocess.CalledProcessError:
            continue
    return None


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def _open_sinks(output_path, mode="w"):
    if not output_path:
        return [sys.stdout], None
    parent = os.path.dirname(os.path.abspath(output_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    f = open(output_path, mode)
    return [sys.stdout, f], f


def _emit(sinks, line):
    for s in sinks:
        s.write(line)
        s.flush()


PING_INTERVAL_SECONDS = 1.0


def record_ping(host, output_path, stop_event=None):
    """Send one ping per second, recording each reply with its timestamp.

    A spinning fresh `ping -c 1 -D <host>` per probe (rather than one
    long-running ping) defeats kernel/path caching that would otherwise
    bias VPN measurements toward the hot path.

    If ``stop_event`` is passed, the loop exits cleanly when it's set —
    used by ping-load mode to coordinate shutdown from the main thread.
    """
    cmd = ["ping", "-c", "1", "-W", "1", "-D", host]
    sinks, fh = _open_sinks(output_path, mode="w")
    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            t0 = time.monotonic()
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True)
            except FileNotFoundError as e:
                sys.exit(str(e))
            for line in proc.stdout.splitlines():
                if TIMESTAMP_RE.search(line):
                    _emit(sinks, line + "\n")
            sleep_for = PING_INTERVAL_SECONDS - (time.monotonic() - t0)
            if sleep_for > 0:
                if stop_event is not None:
                    if stop_event.wait(timeout=sleep_for):
                        break
                else:
                    time.sleep(sleep_for)
    except KeyboardInterrupt:
        pass
    finally:
        if fh is not None:
            fh.close()
    return 0


def _stream_curl(url, sinks):
    """Run curl once, fanning each \\r/\\n-delimited progress segment out
    to ``sinks`` with a high-resolution Unix timestamp prefix.

    Splitting on `\\r` lets us treat each in-place progress redraw as its
    own line — that's what gives the recording one sample per progress
    update instead of a single final blob.
    """
    cmd = ["curl", "-o", "/dev/null", url]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    buf = bytearray()
    try:
        while True:
            ch = proc.stdout.read(1)
            if not ch:
                if buf:
                    _emit(sinks, _stamp(buf))
                break
            if ch in (b"\r", b"\n"):
                if buf:
                    _emit(sinks, _stamp(buf))
                    buf.clear()
            else:
                buf.extend(ch)
    except KeyboardInterrupt:
        proc.terminate()
    finally:
        proc.wait()
    return proc.returncode


def record_curl(url, output_path):
    """One curl run, truncating ``output_path`` (the standalone behaviour)."""
    sinks, fh = _open_sinks(output_path, mode="w")
    try:
        return _stream_curl(url, sinks)
    finally:
        if fh is not None:
            fh.close()


def record_curl_burst(url, output_path):
    """One curl run, appending to ``output_path`` (used by ping-load).

    Append mode means multiple bursts accumulate in one file without any
    blank-line separators between them. The plotter's tolerant parser
    skips the curl banner/handshake lines between bursts on its own.
    """
    sinks, fh = _open_sinks(output_path, mode="a")
    try:
        return _stream_curl(url, sinks)
    finally:
        if fh is not None:
            fh.close()


def _stamp(buf):
    text = bytes(buf).decode("utf-8", "replace")
    return f"[{time.time():.6f}] {text}\n"


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------

def cmd_plot(args):
    # Positionals default to the left axis (the common case: one or more
    # same-kind recordings stack on one y-scale). --left appends more,
    # --right opens a second y-axis; both can repeat.
    left_paths = list(args.files or []) + list(args.left or [])
    right_paths = list(args.right or [])

    # A `--right`-only invocation is almost certainly user error (an
    # off-side single chart looks broken), but rather than reject it,
    # fold it back onto the left so they still get something useful.
    if not left_paths and right_paths:
        left_paths, right_paths = right_paths, []

    if not left_paths and not right_paths:
        sys.exit("error: no input files (pass paths positionally or via --left/--right)")

    try:
        left_series = [parse_recording(p) for p in left_paths]
        right_series = [parse_recording(p) for p in right_paths]
    except (FileNotFoundError, ValueError) as e:
        sys.exit(str(e))

    # Default the legend to file basenames whenever there's more than
    # one series — otherwise readers can't tell which curve is which.
    # Single-series charts stay un-labelled by default (no legend box).
    all_paths = left_paths + right_paths
    labels = args.legend
    if labels is None and len(all_paths) > 1:
        labels = [os.path.splitext(os.path.basename(p))[0] for p in all_paths]
    if labels is not None and len(labels) != len(all_paths):
        sys.exit(
            f"error: --legend got {len(labels)} label(s) for {len(all_paths)} series"
        )

    try:
        svg = render(
            left_series, right_series,
            log_y=args.log_y,
            width=args.width, height=args.height,
            styles=args.style,
            labels=labels,
        )
    except ValueError as e:
        sys.exit(str(e))

    output = args.output if args.output is not None else auto_output_path(left_series + right_series)

    if output == "-":
        sys.stdout.write(svg + "\n")
        return

    parent = os.path.dirname(os.path.abspath(output))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(output, "w") as f:
        f.write(svg + "\n")
    log.info("wrote %s", output)

    png = svg_to_png(output, args.width, args.height, args.png_scale)
    if png:
        log.info("wrote %s", png)
    else:
        log.info(
            "note: skipping PNG — install rsvg-convert (apt install librsvg2-bin), "
            "imagemagick, or inkscape to enable."
        )


def cmd_ping(args):
    sys.exit(record_ping(args.host, args.output))


def cmd_curl(args):
    sys.exit(record_curl(args.url, args.output))


def cmd_ping_load(args):
    """Continuous ping with a 10 MB curl burst every ``--interval`` minutes.

    ping samples stream into ``--ping-output`` (truncated at start);
    every curl burst's progress meter is appended to ``--speed-output``.
    Plotting the two files together via

        gnosis_vpn-bench plot data/ping.txt --right data/speed.txt

    gives one chart with latency on the left axis and throughput on the
    right — that's the whole point of the mode.
    """
    interval_seconds = args.interval * 60
    if interval_seconds <= 0:
        sys.exit("error: --interval must be a positive number of minutes")

    log.info(
        "MODE: ping-load — host=%s url=%s interval=%dmin",
        args.host, args.url, args.interval,
    )
    log.info("ping  → %s", args.ping_output)
    log.info("speed → %s", args.speed_output)

    # Make sure the parent dirs exist before either recorder fires; that
    # way an "oops, data/ doesn't exist" error surfaces immediately
    # rather than after the user has waited 30 minutes for the first
    # curl burst.
    for p in (args.ping_output, args.speed_output):
        if p:
            parent = os.path.dirname(os.path.abspath(p))
            if parent:
                os.makedirs(parent, exist_ok=True)

    # Start the speed file fresh, then close: each burst will re-open in
    # append mode. This gives a deterministic empty starting point
    # without forcing the ping-load session to keep a long-lived handle
    # to a file it only writes during bursts.
    if args.speed_output:
        open(args.speed_output, "w").close()

    stop_event = threading.Event()

    def _ping_runner():
        try:
            record_ping(args.host, args.ping_output, stop_event=stop_event)
        except Exception as exc:
            log.exception("ping recorder crashed: %s", exc)

    ping_thread = threading.Thread(
        target=_ping_runner, name="ping-recorder", daemon=True,
    )
    ping_thread.start()

    try:
        burst = 0
        while True:
            log.info(
                "Waiting %d min before next curl burst (Ctrl-C to stop)…",
                args.interval,
            )
            interrupted = stop_event.wait(timeout=interval_seconds)
            if interrupted:
                break
            burst += 1
            log.info("Curl burst #%d → %s", burst, args.url)
            try:
                record_curl_burst(args.url, args.speed_output)
            except Exception as exc:
                log.exception("curl burst #%d failed: %s", burst, exc)
            log.info("Curl burst #%d done.", burst)
    except KeyboardInterrupt:
        log.info("Stopping ping-load…")
    finally:
        stop_event.set()
        ping_thread.join(timeout=5)
