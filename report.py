"""
Statistics helpers, text formatting, and result file I/O.
"""

import json
import logging
import math
from pathlib import Path

from config import LOG_DIR, _run_ts

log = logging.getLogger("gnosis_speedtest")


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_ms(values: list[float]) -> str:
    if not values:
        return "N/A"
    return f"{_mean(values):.1f} ± {_stdev(values):.1f}"


def _fmt_mbit(values: list[float]) -> str:
    if not values:
        return "N/A"
    return f"{_mean(values):.2f} ± {_stdev(values):.2f}"


def _col(value, width: int, decimals: int = 2, align: str = ">") -> str:
    if value is None:
        s = "–"
    elif isinstance(value, float):
        s = f"{value:.{decimals}f}"
    else:
        s = str(value)
    return f"{s:{align}{width}}"


def _size_label(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.0f}MB"
    return f"{size_bytes / 1024:.0f}KB"


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _write_json(path: Path, payload: dict) -> None:
    """Atomically write *payload* as JSON to *path*."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.rename(path)


def flush_live(live_file: Path | None, payload: dict) -> None:
    """Write the current results dict to the live JSON file (if configured)."""
    if live_file is None:
        return
    json_payload = {k: v for k, v in payload.items() if k != "_report"}
    _write_json(live_file, json_payload)
    log.debug("Live JSON updated: %s", live_file)


def save_results(payload: dict, mode: str) -> None:
    report_file = LOG_DIR / f"report_{mode}_{_run_ts}.txt"
    results_file = LOG_DIR / f"results_{mode}_{_run_ts}.json"
    report = payload.get("_report", "")
    if report:
        print("\n" + report)
        report_file.write_text(report, encoding="utf-8")
        log.info("Report saved to %s", report_file)
    json_payload = {k: v for k, v in payload.items() if k != "_report"}
    _write_json(results_file, json_payload)
    log.info("JSON saved to %s", results_file)
