"""Overlap-only goodput for one ladder rung.

Every client's rate is computed over the same window: from the *latest*
transfer start to the *earliest* transfer end. Bytes received before the
window opens or after it closes (the tails) are excluded, so a client that
started early or finished late cannot inflate or deflate the total.

Each agent records cumulative byte counts at every read (``events``). The
count at any instant is interpolated linearly between the surrounding
events; before the first event it is 0 and after the last it stays flat.
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Any

Events = list[list[float]]


class NoOverlapError(ValueError):
    pass


def counter_at(events: Events, at: float) -> float:
    times = [event[0] for event in events]
    index = bisect_right(times, at) - 1
    if index < 0:
        return 0.0
    if index == len(events) - 1:
        return float(events[index][1])
    t0, b0 = events[index]
    t1, b1 = events[index + 1]
    if t1 == t0:
        return float(b1)
    return b0 + (b1 - b0) * (at - t0) / (t1 - t0)


def per_second_mbps(events: Events, origin: float, end: float) -> tuple[list[float], list[float | None]]:
    """Throughput per one-second bin since *origin* (the scheduled start)."""
    times: list[float] = []
    rates: list[float | None] = []
    second = 0
    while origin + second < end:
        lo = origin + second
        hi = min(origin + second + 1, end)
        delta = counter_at(events, hi) - counter_at(events, lo)
        times.append(float(second))
        rates.append(delta * 8 / (hi - lo) / 1e6 if hi > lo else None)
        second += 1
    return times, rates


def summarize(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Overlap metrics for the agents' ``result.json`` payloads, keyed by client."""
    if not results:
        raise NoOverlapError("no client results")
    start = max(item["start"] for item in results.values())
    end = min(item["end"] for item in results.values())
    if end <= start:
        raise NoOverlapError("transfers share no common interval")
    clients: dict[str, dict[str, Any]] = {}
    for name, item in results.items():
        count = counter_at(item["events"], end) - counter_at(item["events"], start)
        pings = [p for p in item.get("pings", []) if start <= p["sent_at"] < end]
        replies = [p["rtt_ms"] for p in pings if p["rtt_ms"] is not None]
        clients[name] = {
            "mbps": count * 8 / (end - start) / 1e6,
            "overlap_bytes": count,
            "downloaded_bytes": item["bytes"],
            "complete": item["complete"],
            "timed_out": item["timed_out"],
            "ping_mean_ms": sum(replies) / len(replies) if replies else None,
            "ping_sent": len(pings),
            "ping_received": len(replies),
            "ping_loss_pct": 100 * (1 - len(replies) / len(pings)) if pings else None,
        }
    starts = [item["start"] for item in results.values()]
    return {
        "clients": clients,
        "overlap_start": start,
        "overlap_end": end,
        "overlap_seconds": end - start,
        "start_skew_ms": (max(starts) - min(starts)) * 1000,
        "total_mbps": sum(c["mbps"] for c in clients.values()),
    }


def timeseries(item: dict[str, Any]) -> dict[str, list[Any]]:
    """Per-second Mbit/s and ping RTT since the scheduled start."""
    origin = item["scheduled_start"]
    times, rates = per_second_mbps(item["events"], origin, item["end"])
    rtt: list[float | None] = [None] * len(times)
    for ping in item.get("pings", []):
        index = int(ping["sent_at"] - origin)
        if 0 <= index < len(rtt):
            rtt[index] = ping["rtt_ms"]
    return {"t_s": times, "mbps": [round(r, 3) if r is not None else None for r in rates], "rtt_ms": rtt}
