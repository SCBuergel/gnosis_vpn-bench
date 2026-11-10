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

import itertools
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
    series: dict[str, list[Any]] = {
        "t_s": times,
        "mbps": [round(r, 3) if r is not None else None for r in rates],
        "rtt_ms": rtt,
    }
    samples = item.get("samples") or []
    if samples:
        cpu: list[float | None] = [None] * len(times)
        mem: list[float | None] = [None] * len(times)
        load: list[float | None] = [None] * len(times)
        for previous, sample in itertools.pairwise(samples):
            index = int(sample["at"] - origin)
            if 0 <= index < len(times):
                cpu[index] = cpu_busy_pct(previous.get("cpu"), sample.get("cpu"))
        for sample in samples:
            index = int(sample["at"] - origin)
            if 0 <= index < len(times):
                if isinstance(sample.get("mem_available_kib"), (int, float)):
                    mem[index] = round(sample["mem_available_kib"] / 1024, 1)
                if isinstance(sample.get("load1"), (int, float)):
                    load[index] = round(sample["load1"], 2)
        series.update(cpu_pct=cpu, mem_available_mib=mem, load1=load)
    return series


def cpu_busy_pct(before: list[int] | None, after: list[int] | None) -> float | None:
    """Busy share of all CPUs between two ``/proc/stat`` cpu lines (idle and iowait count as idle)."""
    if not before or not after or len(before) < 5 or len(after) != len(before):
        return None
    # guest and guest_nice (fields 9, 10) are already included in user and nice.
    total = sum(after[:8]) - sum(before[:8])
    idle = (after[3] + after[4]) - (before[3] + before[4])
    if total <= 0:
        return None
    return round(100 * (total - idle) / total, 1)


def system_summary(item: dict[str, Any]) -> dict[str, float | None] | None:
    """Host load on the client during its transfer, from the agent's per-second samples."""
    samples = item.get("samples") or []
    if len(samples) < 2:
        return None
    busy = [cpu_busy_pct(a.get("cpu"), b.get("cpu")) for a, b in itertools.pairwise(samples)]
    busy_values = [value for value in busy if value is not None]
    first, last = samples[0].get("cpu"), samples[-1].get("cpu")
    mem = [s["mem_available_kib"] / 1024 for s in samples if isinstance(s.get("mem_available_kib"), (int, float))]
    load = [s["load1"] for s in samples if isinstance(s.get("load1"), (int, float))]
    return {
        "cpu_mean_pct": cpu_busy_pct(first, last),
        "cpu_max_pct": max(busy_values) if busy_values else None,
        "mem_available_min_mib": round(min(mem), 1) if mem else None,
        "load1_max": round(max(load), 2) if load else None,
    }
