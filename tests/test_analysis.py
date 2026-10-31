"""Overlap-only rate calculation (ported from the ladder harness)."""

import pytest

from gvpn_bench.remote.analysis import NoOverlapError, counter_at, per_second_mbps, summarize, timeseries


def item(start, end, events, nbytes, **extra):
    base = {
        "start": start,
        "end": end,
        "events": events,
        "complete": True,
        "bytes": nbytes,
        "timed_out": False,
    }
    base["pings"] = []
    base.update(extra)
    return base


def test_interpolation():
    assert counter_at([[0, 0], [2, 100]], 1) == 50
    assert counter_at([[0, 0], [2, 100]], 3) == 100
    assert counter_at([[1, 0], [2, 100]], 0) == 0


def test_excludes_tail_and_early_start():
    a = item(0, 2, [[0, 0], [2, 200]], 200)
    b = item(1, 3, [[1, 0], [3, 400]], 400)
    result = summarize({"a": a, "b": b})
    assert result["overlap_seconds"] == 1
    assert result["total_mbps"] == pytest.approx(0.0024)
    assert result["clients"]["a"]["overlap_bytes"] == 100
    assert result["clients"]["b"]["overlap_bytes"] == 200
    assert result["start_skew_ms"] == 1000


def test_no_overlap():
    with pytest.raises(NoOverlapError):
        summarize({"a": item(0, 1, [], 0), "b": item(2, 3, [], 0)})


def test_initial_wait_is_not_interpolated_as_traffic():
    events = [[0, 0], [5, 0], [5, 100], [6, 200]]
    assert counter_at(events, 4) == 0
    assert counter_at(events, 5) == 100


def test_timeout_retains_partial_bytes_and_ping_loss():
    timed_out = item(
        0,
        300,
        [[0, 0], [300, 30_000_000]],
        30_000_000,
        complete=False,
        timed_out=True,
        pings=[{"sent_at": 1, "rtt_ms": 100}, {"sent_at": 2, "rtt_ms": None}, {"sent_at": 300, "rtt_ms": 1}],
    )
    summary = summarize({"client1": timed_out})
    assert summary["total_mbps"] == pytest.approx(0.8)
    assert summary["clients"]["client1"]["ping_loss_pct"] == 50
    assert summary["clients"]["client1"]["ping_mean_ms"] == 100


def test_per_second_series():
    times, rates = per_second_mbps([[0, 0], [2, 250_000]], 0, 2)
    assert times == [0.0, 1.0]
    assert rates == [pytest.approx(1.0), pytest.approx(1.0)]


def test_timeseries_places_pings_by_second():
    data = item(0, 2, [[0, 0], [2, 250_000]], 250_000, scheduled_start=0)
    data["pings"] = [{"sent_at": 0.01, "rtt_ms": 10}, {"sent_at": 1.2, "rtt_ms": None}]
    series = timeseries(data)
    assert series["rtt_ms"] == [10, None]
    assert len(series["mbps"]) == 2
