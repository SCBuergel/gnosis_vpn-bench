"""Offline chart layout, style and PNG rendering checks."""

import shutil
import struct
import subprocess
import xml.etree.ElementTree as ET

import pytest
from sample_result import sample_result

from gvpn_bench.results.charts import (
    FILE_THEME,
    PngChart,
    nice_scale,
    overview_chart,
    rasterize,
    time_chart,
    total_series,
)
from gvpn_bench.results.collection import Collection, Recording
from gvpn_bench.results.report_model import Chart, build

NS = {"s": "http://www.w3.org/2000/svg"}


def lines(count):
    return [(f"client{i + 1}", [0, 1, 2, 3], [i + 1, None, i + 2, i + 3]) for i in range(count)]


@pytest.mark.parametrize("peak", [0.001, 1, 23.26, 40, 55.173, 59.999, 60, 1820])
def test_scale_ends_at_first_major_tick_strictly_above_peak(peak):
    top, step = nice_scale(peak)
    assert top - step <= peak < top
    assert top / step == pytest.approx(round(top / step))


def test_experiment_peak_needs_sixty_not_eighty():
    assert nice_scale(55.173) == (60, 20)


def test_overview_scale_includes_error_bar_maximum():
    recordings = []
    for index, value in enumerate((30, 55.173)):
        result = sample_result()
        result["concurrent"]["rungs"][0]["total_mbps"] = value
        recordings.append(Recording(str(index), result))
    root = ET.fromstring(overview_chart(Collection("t", recordings).series(), FILE_THEME))
    ticks = [node.text for node in root.findall("s:text[@text-anchor='end']", NS)]
    assert ticks == ["0", "20", "40", "60"]


def test_large_ladder_styles_match_legend_and_have_unique_pairs():
    root = ET.fromstring(time_chart("Throughput", "Mbit/s", lines(12), FILE_THEME))
    paths = root.findall("s:path[@class='series-line']", NS)
    legend = root.findall("s:g[@class='legend-entry']", NS)
    pairs = []
    for path, entry in zip(paths, legend, strict=True):
        sample, marker, *_ = list(entry)
        assert path.attrib["stroke"] == sample.attrib["stroke"]
        assert path.attrib["stroke-dasharray"] == sample.attrib["stroke-dasharray"]
        shape = (marker.tag, len(marker.attrib.get("points", "").split()), "l" in marker.attrib.get("d", ""))
        pairs.append((sample.attrib["stroke-dasharray"], shape))
        assert path.attrib["d"].count("M") == 2  # missing values break lines
    assert len(paths) == len(legend) == 12
    assert len(set(pairs)) == 12
    # Shape and dash selection repeats only after twelve clients, independent of colour.
    shapes = [list(entry)[1].tag for entry in legend]
    assert len(set(shapes)) >= 4
    assert len({path.attrib["stroke-dasharray"] for path in paths}) == 4


def test_five_clients_keep_solid_lines():
    root = ET.fromstring(time_chart("Ping", "ms", lines(5), FILE_THEME))
    assert all(not path.attrib["stroke-dasharray"] for path in root.findall("s:path[@class='series-line']", NS))


def test_legend_wraps_inside_image_and_above_plot():
    root = ET.fromstring(time_chart("Ping", "ms", lines(12), FILE_THEME))
    labels = root.findall("s:g/s:text", NS)
    ys = {float(label.attrib["y"]) for label in labels}
    assert len(ys) >= 3
    for label in labels:
        assert float(label.attrib["x"]) + len(label.text) * 26 * 0.7 <= 868
    plot_top = min(
        float(path.attrib["d"].split(",")[1].split()[0])
        for path in root.findall("s:path", NS)
        if " H" in path.attrib.get("d", "")
    )
    assert max(ys) < plot_top
    assert root.attrib["font-size"] == "26"
    assert root.attrib["height"] == "550"


def test_long_legend_labels_wrap_without_changing_names():
    name = "A very long exit and package label " * 5
    root = ET.fromstring(time_chart("Ping", "ms", [(name, [0, 1], [1, 2])], FILE_THEME))
    labels = root.findall("s:g/s:text", NS)
    assert len(labels) > 1
    assert " ".join(label.text for label in labels) == name.strip()
    assert all(float(label.attrib["x"]) + len(label.text) * 26 * 0.7 <= 868 for label in labels)
    assert float(root.attrib["height"]) > max(float(label.attrib["y"]) for label in labels) + 160


def test_overview_has_larger_font_height_and_wrapped_legend():
    recordings = []
    for index in range(11):
        result = sample_result()
        result["provenance"]["target"].update(exit_address=f"0x{index + 1:040x}", label=f"Exit {index + 1}")
        recordings.append(Recording(str(index), result))
    root = ET.fromstring(overview_chart(Collection("t", recordings).series(), FILE_THEME))
    assert root.attrib["font-size"] == "26"
    assert root.attrib["height"] == "850"
    assert len({label.attrib["y"] for label in root.findall("s:g/s:text", NS)}) > 1


@pytest.mark.skipif(shutil.which("rsvg-convert") is None, reason="optional PNG renderer is not installed")
def test_high_resolution_png_dimensions():
    png = rasterize(PngChart(time_chart("Ping", "ms", lines(12), FILE_THEME)))
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", png[16:24]) == (2700, 1650)


def test_missing_renderer_is_a_clear_error(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(RuntimeError, match="requires rsvg-convert"):
        rasterize(PngChart("<svg/>"))


def test_total_aligns_timestamps_instead_of_array_positions():
    times, values = total_series(
        [
            ("a", [0, 1, 2, 3], [1, 2, None, 4]),
            ("b", [1, 2, 3, 4], [10, 20, 30, 40]),
        ]
    )
    assert times == [0, 1, 2, 3, 4]
    assert values == [None, 12, None, 34, None]


def test_total_keeps_measured_zero_and_missing_client_distinct():
    assert total_series([("a", [0, 1], [0, 2]), ("b", [0, 1], [0, 3])]) == ([0, 1], [0, 5])
    assert total_series([("a", [0, 1], [0, 2]), ("b", [], [])]) == ([0, 1], [None, None])
    assert total_series([]) == ([], [])
    assert total_series([("a", [0, 1], [2, 3])]) == ([0, 1], [2, 3])


def test_total_rejects_duplicate_or_mismatched_samples():
    with pytest.raises(ValueError, match="duplicate"):
        total_series([("a", [0, 0], [1, 2])])
    with pytest.raises(ValueError):
        total_series([("a", [0, 1], [1])])


def test_download_plot_has_independent_axes_and_matching_total_legend():
    data = [("a", [0, 1], [30, 40]), ("b", [0, 1], [50, 60])]
    root = ET.fromstring(time_chart("Download", "Mbit/s", data, FILE_THEME, total=True))
    total = root.find("s:path[@class='total-line']", NS)
    assert total is not None and total.attrib["data-axis"] == "right"
    assert root.findall("s:text[@class='time-tick']", NS)
    assert root.findall("s:text[@class='right-tick']", NS)[-1].text == "125"
    assert "80" in [node.text for node in root.findall("s:text[@text-anchor='end']", NS)]
    legend = root.findall("s:g[@class='legend-entry']", NS)[-1]
    assert "Total (right)" in "".join(legend.itertext())
    assert legend[0].attrib["stroke"] == total.attrib["stroke"]
    assert legend[0].attrib["stroke-dasharray"] == total.attrib["stroke-dasharray"]
    assert all(path.attrib["stroke-dasharray"] == "" for path in root.findall("s:path[@class='series-line']", NS))


def test_total_breaks_at_missing_samples_and_legend_stays_in_bounds():
    root = ET.fromstring(time_chart("Download", "Mbit/s", lines(12), FILE_THEME, total=True))
    assert root.find("s:path[@class='total-line']", NS).attrib["d"].count("M") == 2
    for label in root.findall("s:g/s:text", NS):
        assert float(label.attrib["x"]) + len(label.text) * 26 * 0.7 <= 790


def test_total_without_complete_samples_is_a_gap_not_zero():
    root = ET.fromstring(time_chart("Download", "Mbit/s", [("a", [0], [1]), ("b", [], [])], FILE_THEME, total=True))
    assert root.find("s:path[@class='total-line']", NS).attrib["d"] == ""
    assert "Total: no complete samples" in "".join(root.itertext())


def test_report_adds_total_only_to_download_charts_and_accounts_for_missing_client():
    result = sample_result()
    row = result["concurrent"]["rungs"][0]
    row["clients"].append("absent")
    collection = Collection("t", [Recording("r", result)])
    charts = [b for b in build(collection) if isinstance(b, Chart)]
    throughput = [b for b in charts if b.name.endswith("-mbps")]
    latency = [b for b in charts if b.name.endswith("-ping")]
    assert throughput and latency
    assert all('class="total-line"' in b.draw(FILE_THEME) for b in throughput)
    assert all('class="total-line"' not in b.draw(FILE_THEME) for b in latency)
    assert "Total: no complete samples" in throughput[0].draw(FILE_THEME)
