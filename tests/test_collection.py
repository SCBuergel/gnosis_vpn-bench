"""Multi-location and repeated recordings: series, error bars, Markdown output."""

import copy
import re

from sample_result import sample_result

from gvpn_bench.results.charts import FILE_THEME, HTML_THEME, overview_chart
from gvpn_bench.results.collection import Collection, Recording
from gvpn_bench.results.markdown import render_markdown
from gvpn_bench.results.render import render_collection

AUSTRIA = "0x210a14012535321b919653fdee8c2d5971b0e534"


def recording(name, *, address=None, label=None, totals=(42.5, None), started="2026-11-01T12:30:00Z", package=None):
    result = sample_result()
    result["run"]["started_at"] = started
    if address:
        result["provenance"]["target"].update({"exit_address": address, "label": label, "name": label.lower()})
    if package:
        result["provenance"]["client"]["package_version"] = package
    for rung, total in zip(result["concurrent"]["rungs"], totals, strict=True):
        rung["total_mbps"] = total
        if total is not None:
            rung["status"] = "measured"
            rung["failure_status"] = None
    return Recording(name=name, result=result)


def test_two_locations_make_two_series():
    collection = Collection("t", [recording("a"), recording("b", address=AUSTRIA, label="Austria")])
    assert collection.multi_location
    assert [s.label for s in collection.series()] == ["GVPN-LT-1", "Austria"]


def test_repetitions_become_one_series_with_error_bar():
    reps = [
        recording(f"r{i}", totals=(t, None), started=f"2026-11-0{i + 1}T00:00:00Z") for i, t in enumerate((40, 44, 48))
    ]
    (series,) = Collection("t", reps).series()
    (point,) = series.points()
    assert (point.mean, point.low, point.high, len(point.values)) == (44, 40, 48, 3)
    assert series.missing_rungs() == [(2, "timeout")] * 3
    svg = overview_chart([series], FILE_THEME)
    assert svg.count("<circle") == 1
    assert "H" in re.search(r'<path d="M[^"]+V[^"]+"', svg).group(0)  # whisker drawn


def test_unmeasured_rungs_are_never_zero():
    series = Collection("t", [recording("a", totals=(None, None))]).series()
    assert series[0].points() == []
    assert "missing data, not 0" in overview_chart(series, HTML_THEME)


def test_different_client_versions_are_labelled():
    labels = [s.label for s in Collection("t", [recording("a"), recording("b", package="9.9")]).series()]
    assert len(labels) == 2 and any("pkg 9.9" in label for label in labels)


def test_markdown_has_sections_charts_and_no_zero_for_missing():
    collection = Collection("Two exits", [recording("a"), recording("b", address=AUSTRIA, label="Austria")])
    text, charts = render_markdown(collection, extra_links=["[app](https://example.invalid)"])
    for heading in ("## 1. Concurrent bandwidth", "## 2. Per client", "## 3. Availability", "## 4. Provenance"):
        assert heading in text
    assert "![Total Mbit/s" in text and "charts/overview.svg" in charts
    assert all(path in text for path in charts)
    assert "not measured (timeout)" in text
    assert 'fill="#ffffff"' in charts["charts/overview.svg"]  # own panel for GitHub dark mode


def test_html_collection_renders_both_locations():
    html = render_collection(
        Collection("Two exits", [recording("a"), recording("b", address=AUSTRIA, label="Austria")])
    )
    assert "Austria" in html and "GVPN-LT-1" in html and "var(--muted)" in html


def test_series_label_includes_profile_when_profiles_differ():
    other = recording("b")
    other.result["provenance"]["profile"] = copy.deepcopy(other.result["provenance"]["profile"]) | {"name": "v4-x"}
    labels = [s.label for s in Collection("t", [recording("a"), other]).series()]
    assert any("v4-x" in label for label in labels)


def test_nice_scale_steps():
    from gvpn_bench.results.charts import nice_scale

    assert nice_scale(23.26) == (30, 10) or nice_scale(23.26)[1] in (5, 10)
    top, step = nice_scale(23.26)
    assert top >= 23.26 * 1.1 and (top / step) == round(top / step)
    assert nice_scale(0) == (1.0, 0.25)
    assert nice_scale(1820)[1] in (500, 1000)


def test_partial_failures_in_repetitions_are_shown():
    reps = [
        recording("a", totals=(40, 50)),
        recording("b", totals=(42, None), started="2026-11-02T00:00:00Z"),
        recording("c", totals=(44, None), started="2026-11-03T00:00:00Z"),
    ]
    collection = Collection("t", reps)
    (series,) = collection.series()
    point = {p.clients: p for p in series.points()}[2]
    assert (point.values, point.missing, point.attempts) == ((50,), ("timeout", "timeout"), 3)
    text, charts = render_markdown(collection)
    assert "50.00; 2 of 3 not measured: timeout" in text
    assert ">1/3</text>" in charts["charts/overview.svg"]


def test_line_breaks_at_a_rung_nobody_measured():
    result = recording("a", totals=(40, None))
    result.result["concurrent"]["rungs"].append(
        {**result.result["concurrent"]["rungs"][0], "rung": 3, "total_mbps": 60.0}
    )
    (series,) = Collection("t", [result]).series()
    assert series.unmeasured() == [2]
    svg = overview_chart([series], FILE_THEME)
    assert not re.search(r'<path d="M[\d.,]+ L[\d.,]+" fill="none"', svg)


def test_series_with_the_same_label_are_both_drawn():
    a = recording("a")
    b = recording("b", address=AUSTRIA, label="GVPN-LT-1")
    series = Collection("t", [a, b]).series()
    assert len(series) == 2
    assert overview_chart(series, FILE_THEME).count("<circle") == 2


def test_markdown_escapes_html_from_data():
    text, _ = render_markdown(Collection("t", [recording("a")]))
    assert "<script>" not in text and "&lt;script&gt;" in text
