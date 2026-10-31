import pytest
from conftest import LEGACY_DESTINATION, V4_DESTINATION, V5_DESTINATION, status_payload

from gvpn_bench.status import normalise_address, parse_status, start_client_accepted, variant_name


def test_v4_destination_fields():
    status = parse_status(status_payload([V4_DESTINATION]))
    (dest,) = status.destinations
    assert dest.id == "Serbia"
    assert dest.address == "0x4203490fc1747fa6d0ec13c1589cca9d308724ba"
    assert (dest.location, dest.country, dest.name) == ("Serbia", "RS", "Serbia")
    assert status.running


def test_v5_destination_reads_nested_meta_and_name():
    (dest,) = parse_status(status_payload([V5_DESTINATION])).destinations
    assert (dest.location, dest.country, dest.name) == ("Kaunas", "LT", "GVPN-LT-1")
    assert dest.address == "0x01961ef9375e063ab32addf44f0acc8adc71c888"


def test_legacy_byte_list_address():
    (dest,) = parse_status(status_payload([LEGACY_DESTINATION])).destinations
    assert dest.address == "0x210a14012535321b919653fdee8c2d5971b0e534"
    assert dest.country is None


def test_connection_states():
    payload = status_payload(
        [V4_DESTINATION, V5_DESTINATION, LEGACY_DESTINATION],
        connected={"destination_id": "Serbia"},
        connecting={"destination_id": "gvpn-lt-1-luiu", "phase": "OpeningPing"},
        disconnecting=[{"destination_id": "Austria"}],
    )
    status = parse_status(payload)
    assert status.connection_state("Serbia") == "Connected"
    assert status.connection_state("gvpn-lt-1-luiu") == "Connecting"
    assert status.connecting_phase == "OpeningPing"
    assert status.connection_state("Austria") == "Disconnecting"
    assert status.connection_state("nope") == "None"


def test_funding_request_from_preparing_safe():
    payload = status_payload(
        [],
        run_mode={
            "PreparingSafe": {
                "node_address": "0x" + "ab" * 20,
                "balance_recommendation": {"wxhopr": "240 wxHOPR", "xdai": "0.01 xDAI"},
            }
        },
    )
    status = parse_status(payload)
    assert status.run_mode == "PreparingSafe"
    assert not status.running
    assert status.funding_request is not None
    assert (status.funding_request.wxhopr, status.funding_request.xdai) == ("240", "0.01")


def test_string_run_mode():
    assert parse_status(status_payload([], run_mode="Running")).running


def test_find_by_address_is_case_insensitive_and_rejects_duplicates():
    status = parse_status(status_payload([V4_DESTINATION]))
    assert status.find_by_address(V4_DESTINATION["address"].upper().replace("0X", "0x")).id == "Serbia"
    assert status.find_by_address("0x" + "00" * 20) is None
    twice = parse_status(status_payload([V4_DESTINATION, {**V4_DESTINATION, "id": "Other"}]))
    with pytest.raises(ValueError):
        twice.find_by_address(V4_DESTINATION["address"])


def test_variant_name_handles_both_enum_encodings():
    assert variant_name({"Connecting": {}}) == "Connecting"
    assert variant_name({"type": "WaitingToConnect", "destination": {}}) == "WaitingToConnect"
    assert variant_name("Running") == "Running"


def test_bad_address_rejected():
    with pytest.raises(ValueError):
        normalise_address("0x123")


def test_start_client_responses():
    assert start_client_accepted({"StartClient": "Started"})
    assert start_client_accepted({"StartClient": "AlreadyRunning"})
    assert not start_client_accepted({"StartClient": {"Error": "x"}})
