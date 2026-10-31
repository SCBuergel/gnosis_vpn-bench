import json
from pathlib import Path

import pytest
from sample_result import sample_result

from gvpn_bench.results import schema
from gvpn_bench.results.render import render
from gvpn_bench.results.schema import SchemaError, validate

JSON_SCHEMA = Path(__file__).resolve().parent.parent / "schema" / "result.schema.json"
DOC = Path(__file__).resolve().parent.parent / "docs" / "result-schema.md"


def test_sample_is_valid():
    validate(sample_result())


def test_json_schema_matches_python_constants():
    data = json.loads(JSON_SCHEMA.read_text())
    assert data["properties"]["schema_version"]["const"] == schema.SCHEMA_VERSION
    assert tuple(data["$defs"]["status"]["enum"]) == schema.FAILURE_STATUSES
    assert tuple(data["properties"]["run"]["properties"]["kind"]["enum"]) == schema.RUN_KINDS
    failure_enum = data["properties"]["failures"]["items"]["properties"]["status"]["enum"]
    assert tuple(failure_enum) == tuple(s for s in schema.FAILURE_STATUSES if s != "connected")


def test_doc_lists_every_status():
    text = DOC.read_text()
    for status in schema.FAILURE_STATUSES:
        assert f"| `{status}` |" in text
    assert f"schema version {schema.SCHEMA_VERSION}" in text


def test_unmeasured_rung_cannot_carry_a_rate():
    result = sample_result()
    result["concurrent"]["rungs"][1]["total_mbps"] = 0
    with pytest.raises(SchemaError, match="total_mbps must be null"):
        validate(result)


def test_unmeasured_rung_needs_a_status():
    result = sample_result()
    result["concurrent"]["rungs"][1]["failure_status"] = None
    with pytest.raises(SchemaError, match="failure_status"):
        validate(result)


def test_failed_availability_cannot_report_speed():
    result = sample_result()
    result["availability"][0]["download_mbps"] = 0.0
    with pytest.raises(SchemaError):
        validate(result)


def test_unknown_status_rejected():
    result = sample_result()
    result["failures"][0]["status"] = "flaky"
    with pytest.raises(SchemaError):
        validate(result)


def test_connected_is_not_a_failure():
    result = sample_result()
    result["failures"][0]["status"] = "connected"
    with pytest.raises(SchemaError):
        validate(result)


def test_mixed_case_address_rejected():
    result = sample_result()
    result["provenance"]["target"]["exit_address"] = "0x01961Ef9375E063aB32AddF44f0acC8adC71C888"
    with pytest.raises(SchemaError):
        validate(result)


def test_wrong_version_rejected():
    result = sample_result()
    result["schema_version"] = "0.9"
    with pytest.raises(SchemaError):
        validate(result)


def test_report_sections_are_in_fixed_order_and_escaped():
    html = render(sample_result())
    positions = [
        html.index(heading)
        for heading in (
            "1. Concurrent bandwidth",
            "2. Per-client results",
            "3. Availability and failures",
            "4. Provenance",
        )
    ]
    assert positions == sorted(positions)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "not measured" in html
    assert "<script src" not in html and "http://" not in html.replace("http://www.w3.org", "")


def test_report_without_ladder_says_so():
    result = sample_result()
    result["concurrent"] = None
    assert "no concurrent-load" in render(result)


def test_tick_labels_avoid_e_notation():
    from gvpn_bench.results.render import CSS, tick

    assert tick(1_000_000) == "1,000,000"
    assert tick(1820.4) == "1,820"
    assert tick(6.69) == "6.69"
    assert ':root[data-theme="dark"]' in CSS and ':root:not([data-theme="light"])' in CSS
