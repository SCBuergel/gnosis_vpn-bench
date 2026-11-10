"""Experiment ordering, comparable repetitions and per-machine provenance."""

import copy

import pytest
from sample_result import sample_result

from gvpn_bench.results.collection import Collection, Recording
from gvpn_bench.results.markdown import render_markdown
from gvpn_bench.results.push import destination_name
from gvpn_bench.results.render import render_collection
from gvpn_bench.results.schema import SchemaError, validate


def member(cycle: int, position: int, label: str) -> Recording:
    result = sample_result()
    result["run"]["experiment"] = {
        "id": "fleet-a",
        "cycle": cycle,
        "cycles": 3,
        "position": position,
        "targets": 2,
    }
    result["provenance"]["target"].update(label=label, name=label.lower())
    result["provenance"]["target"]["exit_address"] = "0x" + str(position) * 40
    return Recording(f"{label.lower()}-c{cycle}", result)


def test_experiment_matrix_keeps_cycle_and_exit_order():
    recordings = [member(2, 2, "Berlin"), member(1, 2, "Berlin"), member(1, 1, "Austria")]
    text, _ = render_markdown(Collection("Experiment", recordings))
    matrix = text.split("### Experiment schedule", 1)[1].split("###", 1)[0]
    assert matrix.index("austria-c1") < matrix.index("berlin-c1") < matrix.index("berlin-c2")
    assert "Cycle" in matrix and "Order" in matrix and "Measured / planned" in matrix
    assert "cycle 2/3" in text


def test_one_experiment_orders_cycles_across_different_run_directories():
    first, second = member(1, 1, "Berlin"), member(2, 1, "Berlin")
    first.result["run"].update(id="later-name", created_at="2026-11-02T00:00:00Z")
    second.result["run"].update(id="earlier-name", created_at="2026-11-01T00:00:00Z")
    text, _ = render_markdown(Collection("t", [second, first]))
    matrix = text.split("### Experiment schedule", 1)[1].split("###", 1)[0]
    assert matrix.index("berlin-c1") < matrix.index("berlin-c2")


def test_different_workloads_and_regions_are_not_averaged():
    first = member(1, 1, "Berlin")
    deadline = copy.deepcopy(first)
    deadline.result["concurrent"]["spec"]["deadline_s"] = 90
    region = copy.deepcopy(first)
    region.result["provenance"]["infrastructure"]["region"] = "lon1"
    collection = Collection("t", [first, deadline, region])
    assert len(collection.series()) == 3
    text, _ = render_markdown(collection)
    assert "300" in text and "90" in text and "lon1" in text
    assert "Measurement specifications" in text


def test_equivalent_numeric_specifications_share_a_series():
    first = member(1, 1, "Berlin")
    second = copy.deepcopy(first)
    second.result["concurrent"]["spec"]["deadline_s"] = 300.0
    assert len(Collection("t", [first, second]).series()) == 1


def test_experiment_and_machine_text_is_escaped_in_both_formats():
    recording = member(1, 1, "<script>Berlin</script>")
    recording.result["provenance"]["infrastructure"]["machines"][0]["cpu_vendor"] = "<script>vendor</script>"
    collection = Collection("<script>title</script>", [recording])
    text, _ = render_markdown(collection)
    html = render_collection(collection)
    assert "<script>" not in text and "<script>" not in html
    assert "&lt;script&gt;vendor" in text and "&lt;script&gt;vendor" in html


def test_machine_inventory_includes_origin_and_deduplicates_only_within_run():
    first = member(1, 1, "Berlin")
    machines = first.result["provenance"]["infrastructure"]["machines"]
    machines[0].update(architecture="x86_64", cpu_vendor="GenuineIntel", root_disk_gib=80.0)
    machines.append({**machines[0], "role": "origin", "name": "origin", "size": "c-2", "vcpus": 2})
    second = copy.deepcopy(first)
    second = Recording("berlin-c2", second.result)
    second.result["run"]["experiment"]["cycle"] = 2
    third = copy.deepcopy(first)
    third.result["run"]["id"] = "another-fleet"
    text, _ = render_markdown(Collection("t", [first, second, third]))
    inventory = text.split("### Machine inventory", 1)[1].split("###", 1)[0]
    assert inventory.count("| client1 |") == 2
    assert inventory.count("| origin |") == 2
    for value in ("Xeon", "7,940", "x86_64", "GenuineIntel", "Ubuntu 24.04", "6.8.0", "80.00"):
        assert value in inventory
    html = render_collection(Collection("t", [first]))
    assert "Machine inventory" in html and "GenuineIntel" in html


def test_individual_ladders_show_reconnects_and_overlap():
    recording = member(1, 1, "Berlin")
    recording.rungs()[0].update(detail="reconnected before this rung: client1", gap_before_s=19.22)
    text, _ = render_markdown(Collection("t", [recording]))
    assert "Overlap s" in text and "Start skew ms" in text and "Gap s" in text
    assert "19.22" in text and "reconnected before this rung" in text
    assert "not_run: timeout" in text


def test_cycles_have_distinct_recording_paths_even_without_start_times():
    first, second = member(1, 1, "Berlin"), member(2, 1, "Berlin")
    first.result["run"]["started_at"] = None
    second.result["run"]["started_at"] = None
    assert destination_name(first.result) != destination_name(second.result)


def test_experiment_recording_path_is_stable_when_measurement_starts():
    recording = member(1, 1, "Berlin")
    recording.result["run"]["started_at"] = None
    pending = destination_name(recording.result)
    recording.result["run"]["started_at"] = "2026-11-01T18:00:00Z"
    assert destination_name(recording.result) == pending


@pytest.mark.parametrize("field,value", [("cycle", 0), ("cycle", True), ("cycles", 0), ("position", 3)])
def test_invalid_experiment_metadata_is_rejected(field, value):
    result = member(1, 1, "Berlin").result
    result["run"]["experiment"][field] = value
    with pytest.raises(SchemaError):
        validate(result)


def test_old_records_remain_valid_without_new_machine_fields():
    validate(sample_result())
    text, _ = render_markdown(Collection("old", [Recording("old", sample_result())]))
    assert "Machine inventory" in text and "n/a" in text


@pytest.mark.parametrize("field,value", [("architecture", 123), ("root_disk_gib", "unknown"), ("cpu_sockets", True)])
def test_optional_machine_facts_are_typed(field, value):
    result = sample_result()
    result["provenance"]["infrastructure"]["machines"][0][field] = value
    with pytest.raises(SchemaError):
        validate(result)
