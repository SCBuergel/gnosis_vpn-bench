"""Timestamp archive names and a one-line index derived only from recorded facts."""

import copy
import json

import pytest
from sample_result import sample_result

from gvpn_bench.results import publish
from gvpn_bench.results.collection import Recording
from gvpn_bench.results.push import destination_name, timestamp_name


def test_timestamp_names_convert_offsets_to_utc():
    assert timestamp_name("2026-11-01T12:34:56Z") == "2026-11-01--12-34-56"
    assert timestamp_name("2026-11-01T14:34:56+02:00") == "2026-11-01--12-34-56"
    with pytest.raises(ValueError, match="timezone"):
        timestamp_name("2026-11-01T12:34:56")


def test_report_name_uses_creation_time_and_recording_uses_start():
    result = sample_result()
    result["run"].update(created_at="2026-10-31T19:53:32Z", started_at="2026-11-01T02:58:03Z")
    assert publish.report_name([Recording("r", result)]) == "2026-10-31--19-53-32"
    assert destination_name(result) == "2026-11-01--02-58-03"


def test_migrated_recording_name_is_reused_on_later_pushes(tmp_path):
    result = sample_result()
    result["run"]["experiment"] = {"id": "e", "cycle": 1, "cycles": 3, "position": 1, "targets": 2}
    (tmp_path / "result.json").write_text(json.dumps(result))
    name = "2026-11-01--12-30-00"
    staged = publish.stage_recording(tmp_path, {name: Recording(name, copy.deepcopy(result))})
    assert staged.folder == f"runs/{name}"
    assert staged.files["result.json"] == json.dumps(result, indent=2) + "\n"


def test_recording_timestamp_collision_cannot_overwrite_another_run(tmp_path):
    result = sample_result()
    (tmp_path / "result.json").write_text(json.dumps(result))
    other = copy.deepcopy(result)
    other["run"]["id"] = "another-run"
    name = destination_name(other)
    with pytest.raises(ValueError, match="collision"):
        publish.stage_recording(tmp_path, {name: Recording(name, other)})


def test_index_has_exactly_one_summary_line_per_experiment():
    first = sample_result()
    second = copy.deepcopy(first)
    second["run"]["started_at"] = "2026-11-01T13:00:00Z"
    members = {"a": Recording("a", first), "b": Recording("b", second)}
    reports = {"2026-11-01--12-00-00": {"title": "Repeated", "recordings": ["a", "b"], "links": {}}}
    text = publish.index_markdown(members, reports)
    section = text.split("## Experiments\n", 1)[1].split("## Reports", 1)[0]
    assert section.count("\n- [") == 1
    assert "2 recordings" in section and "2/4 rungs measured" in section
    assert "peak 42.50 Mbit/s" in section and "reports/2026-11-01--12-00-00/" in section


def test_summary_distinguishes_zero_from_no_measurement():
    measured = sample_result()
    measured["concurrent"]["rungs"][0]["total_mbps"] = 0.0
    assert "peak 0.00 Mbit/s" in publish.experiment_summary([Recording("zero", measured)])
    for row in measured["concurrent"]["rungs"]:
        row.update(status="failed", total_mbps=None, failure_status="control_timeout")
    summary = publish.experiment_summary([Recording("missing", measured)])
    assert "no ladder throughput measured" in summary and "control_timeout ×2" in summary
    assert "0.00 Mbit/s" not in summary


def test_historical_experiments_get_separate_lines_and_missing_starts_use_creation():
    records = {}
    for i in (1, 2):
        result = sample_result()
        result["run"].update(id=f"history-{i}", started_at=None, reproducible=False)
        result["concurrent"] = None
        records[str(i)] = Recording(str(i), result)
        assert destination_name(result) == timestamp_name(result["run"]["created_at"])
    reports = {"combined": {"title": "History", "recordings": ["1", "2"], "links": {}}}
    section = publish.index_markdown(records, reports).split("## Reports", 1)[0]
    assert section.count("\n- [") == 2
    assert section.count("historical import") == 2
