"""Tests for recovery without VMs, concurrent backups, ladder failure
handling, connect verification and journal reconciliation. All offline."""

import json
import threading
import time
from unittest.mock import MagicMock

import pytest
from conftest import V4_DESTINATION, FakeHost, fail, ok, status_payload
from test_runs import user_config

from gvpn_bench.finance import recover
from gvpn_bench.finance.wallet import Journal
from gvpn_bench.remote import backup, connect, ladder
from gvpn_bench.runs import plan_run, write_private_json

SERBIA = "0x4203490fc1747fa6d0ec13c1589cca9d308724ba"


@pytest.fixture
def run(tmp_path):
    return plan_run(user_config(tmp_path), "v4-jura-prod", "serbia-jura-prod", run_id="rv")


# -- recovery when the deletion timer already removed the VMs -------------------


def test_halt_uses_backups_when_vm_is_deleted(run, tmp_path):
    info = tmp_path / "safe-info.json"
    info.write_text(json.dumps({"node_address": "0x" + "34" * 20, "safe_address": "0x" + "56" * 20}))
    write_private_json(run.state / "backups.json", {"client1": {"safe_info": str(info)}})
    host = FakeHost(lambda argv, data: pytest.fail(f"VM is gone, nothing may run: {argv}"), name="client1")
    balance = recover.halt_client(None, run, run.config.profile(), host, vm_deleted=True)
    assert balance["info"]["node_address"] == "0x" + "34" * 20
    assert host.calls == []


def test_halt_without_any_backup_refuses(run):
    host = FakeHost(lambda argv, data: fail(), name="client1")
    with pytest.raises(RuntimeError, match="no Safe info"):
        recover.halt_client(None, run, run.config.profile(), host, vm_deleted=True)


# -- concurrent backup manifest updates ----------------------------------------------


def test_parallel_manifest_updates_are_not_lost(run):
    threads = [
        threading.Thread(target=backup._record, args=(run, f"client{i}"), kwargs={"payment_state": str(i)})
        for i in range(20)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(backup.manifest(run)) == 20


# -- ladder -----------------------------------------------------------------------------


def agent_result(start, end, nbytes):
    return {
        "scheduled_start": start,
        "start": start,
        "end": end,
        "bytes": nbytes,
        "complete": True,
        "timed_out": False,
        "sha256": "aa",
        "events": [[start, 0], [end, nbytes]],
        "pings": [],
    }


def test_missing_client_result_fails_the_rung_and_keeps_earlier_ones(run, monkeypatch):
    hosts = [FakeHost(lambda a, d: ok(), name=f"client{i}") for i in (1, 2, 3)]
    base = time.time() + 100

    def fake_collect(host, attempt, rung, give_up_at):
        if rung == 2 and host.name == "client2":
            return None
        return agent_result(base + rung * 400, base + rung * 400 + 50, 1_000_000)

    monkeypatch.setattr(ladder, "still_connected", lambda host, profile, target: True)
    monkeypatch.setattr(ladder, "launch", lambda host, **kwargs: None)
    monkeypatch.setattr(ladder, "collect", fake_collect)
    monkeypatch.setattr(ladder, "fetch_pings", lambda host, attempt, rung: None)
    rungs = ladder.run_ladder(run, run.config.profile(), run.config.target(), hosts, "192.0.2.1", "aa")
    assert [r["status"] for r in rungs] == ["measured", "failed", "not_run"]
    assert rungs[0]["total_mbps"] is not None
    assert rungs[1]["failure_status"] == "error" and "client2" in rungs[1]["detail"]
    assert (run.data / "rung1-client1.json").exists()


def test_disconnected_client_fails_rung(run, monkeypatch):
    hosts = [FakeHost(lambda a, d: ok(), name=f"client{i}") for i in (1, 2, 3)]
    monkeypatch.setattr(ladder, "still_connected", lambda host, profile, target: host.name != "client1")
    monkeypatch.setattr(ladder, "launch", lambda host, **kwargs: pytest.fail("must not launch"))
    rungs = ladder.run_ladder(run, run.config.profile(), run.config.target(), hosts, "192.0.2.1", "aa")
    assert [r["status"] for r in rungs] == ["failed", "not_run", "not_run"]
    assert "client1" in rungs[0]["detail"]


# -- connect verification ------------------------------------------------------------------


def connect_host(*, egress="198.51.100.7", route_dev="wg0_gnosisvpn", destinations=(V4_DESTINATION,)):
    connected = {"done": False}

    def respond(argv, _):
        if "gnosis_vpn-ctl" in argv:
            command = argv[argv.index("json") + 1]
            if command == "connect":
                connected["done"] = True
                return ok({"Connect": {"Connecting": V4_DESTINATION}})
            if command == "status":
                extra = {"connected": {"destination_id": "Serbia"}} if connected["done"] else {}
                return ok(status_payload(list(destinations), **extra))
            return ok({"StartClient": "Started"})
        if argv[0] == "curl":
            return ok(egress) if egress else fail(7, "could not resolve host")
        if argv[:5] == ["ip", "-j", "-4", "route", "show"]:
            return ok([{"dev": "eth0", "gateway": "10.0.0.1"}])
        if argv[:4] == ["ip", "-j", "route", "get"]:
            return ok([{"dst": argv[4], "dev": route_dev}])
        return ok()

    return FakeHost(respond, name="client1")


def try_connect(host):
    profile = MagicMock()
    profile.worker.ctl_prefix.return_value = []
    profile.worker.worker_log_glob.return_value = "/var/log/x"
    return connect.connect_client(
        host, "203.0.113.5", profile, SERBIA, route_checks=["192.0.2.1"], timeout_s=5
    )


def test_connect_succeeds_only_with_changed_egress_and_tunnel_routes():
    outcome = try_connect(connect_host())
    assert outcome.status == "connected"
    assert outcome.egress_ip == "198.51.100.7"


def test_unchanged_egress_is_route_bypass():
    assert try_connect(connect_host(egress="203.0.113.5")).status == "route_bypass"


def test_wan_route_is_route_bypass():
    assert try_connect(connect_host(route_dev="eth0")).status == "route_bypass"


def test_failed_egress_lookup_is_an_error_not_a_bypass():
    assert try_connect(connect_host(egress=None)).status == "error"


def test_missing_destination_is_destination_not_found():
    other = {**V4_DESTINATION, "id": "UK", "address": "0x" + "cd" * 20}
    assert try_connect(connect_host(destinations=(other,))).status == "DestinationNotFound"


# -- journal: never-broadcast entries ---------------------------------------------------------


def test_never_broadcast_entry_is_dropped_and_rechecked(run):
    journal = Journal(run)
    journal.append({"hash": "0xaa", "sender": "0x1", "nonce": 4, "purpose": "p", "status": "pending"})
    w3 = MagicMock()
    w3.eth.get_transaction_receipt.side_effect = LookupError
    w3.eth.get_transaction.side_effect = LookupError
    w3.eth.get_transaction_count.return_value = 4
    journal.reconcile(w3)
    assert journal.entries()[0]["status"] == "dropped"
    w3.eth.get_transaction_receipt.side_effect = None
    w3.eth.get_transaction_receipt.return_value = {"status": 1, "blockNumber": 7}
    journal.reconcile(w3)
    assert journal.confirmed("p")


def test_entry_still_in_mempool_blocks(run):
    journal = Journal(run)
    journal.append({"hash": "0xaa", "sender": "0x1", "nonce": 4, "purpose": "p", "status": "pending"})
    w3 = MagicMock()
    w3.eth.get_transaction_receipt.side_effect = LookupError
    w3.eth.get_transaction.return_value = {"hash": "0xaa"}
    w3.eth.get_transaction_count.return_value = 4
    with pytest.raises(Exception, match="mempool"):
        journal.reconcile(w3)


def test_send_after_drop_is_forced_onto_the_dropped_nonce(run):
    journal = Journal(run)
    journal.append({"hash": "0xaa", "sender": "0x1", "nonce": 5, "purpose": "p", "status": "dropped"})
    w3 = MagicMock()
    w3.eth.get_transaction_receipt.side_effect = LookupError
    w3.eth.get_transaction.side_effect = LookupError

    def count(sender, block):
        return 6 if block == "pending" else 5  # load-balanced node disagrees

    w3.eth.get_transaction_count.side_effect = count
    w3.eth.estimate_gas.return_value = 21000
    w3.eth.gas_price = 1
    w3.keccak.return_value = bytes(32)
    account = MagicMock(address="0x1")
    account.sign_transaction.return_value = MagicMock(raw_transaction=b"raw")
    w3.eth.wait_for_transaction_receipt.return_value = {"status": 1, "blockNumber": 9}
    w3.to_checksum_address.side_effect = lambda a: a
    journal.send(w3, account, "p", to="0x2", value=1)
    signed_tx = account.sign_transaction.call_args.args[0]
    assert signed_tx["nonce"] == 5


def test_send_refuses_when_dropped_nonce_was_consumed_elsewhere(run):
    journal = Journal(run)
    journal.append({"hash": "0xaa", "sender": "0x1", "nonce": 5, "purpose": "p", "status": "dropped"})
    w3 = MagicMock()
    w3.eth.get_transaction_receipt.side_effect = LookupError
    w3.eth.get_transaction_count.return_value = 6
    with pytest.raises(Exception, match="does not confirm"):
        journal.send(w3, MagicMock(address="0x1"), "p", to="0x2", value=1)
    w3.eth.send_raw_transaction.assert_not_called()


def test_replacement_is_superseded_when_dropped_entry_mined(run):
    journal = Journal(run)
    journal.append({"hash": "0xaa", "sender": "0x1", "nonce": 5, "purpose": "p", "status": "dropped"})
    journal.append({"hash": "0xbb", "sender": "0x1", "nonce": 5, "purpose": "q", "status": "pending"})
    w3 = MagicMock()
    receipts = {"0xaa": {"status": 1, "blockNumber": 9}}

    def receipt(tx_hash):
        if tx_hash not in receipts:
            raise LookupError
        return receipts[tx_hash]

    w3.eth.get_transaction_receipt.side_effect = receipt
    w3.eth.get_transaction_count.return_value = 6
    journal.reconcile(w3)
    assert [e["status"] for e in journal.entries()] == ["confirmed", "superseded"]


def _receipts(w3, receipts):
    def receipt(tx_hash):
        if tx_hash not in receipts:
            raise LookupError
        return receipts[tx_hash]

    w3.eth.get_transaction_receipt.side_effect = receipt


@pytest.mark.parametrize("replacement_status, final", [(1, "confirmed"), (0, "reverted")])
def test_mined_replacement_supersedes_the_dropped_entry(run, replacement_status, final):
    journal = Journal(run)
    journal.append({"hash": "0xaa", "sender": "0x1", "nonce": 5, "purpose": "p", "status": "dropped"})
    journal.append({"hash": "0xbb", "sender": "0x1", "nonce": 5, "purpose": "p", "status": "pending"})
    w3 = MagicMock()
    _receipts(w3, {"0xbb": {"status": replacement_status, "blockNumber": 9}})
    w3.eth.get_transaction_count.return_value = 6
    journal.reconcile(w3)
    assert [e["status"] for e in journal.entries()] == ["superseded", final]


def test_forced_send_whose_replacement_mines_reports_success(run):
    journal = Journal(run)
    journal.append({"hash": "0xaa", "sender": "0x1", "nonce": 5, "purpose": "p", "status": "dropped"})
    w3 = MagicMock()
    receipts = {}
    _receipts(w3, receipts)
    w3.eth.get_transaction.side_effect = LookupError
    state = {"latest": 5}
    w3.eth.get_transaction_count.side_effect = lambda sender, block: state["latest"]
    w3.eth.estimate_gas.return_value = 21000
    w3.eth.gas_price = 1
    w3.keccak.return_value = bytes.fromhex("bb" * 32)
    w3.to_checksum_address.side_effect = lambda a: a
    account = MagicMock(address="0x1")
    account.sign_transaction.return_value = MagicMock(raw_transaction=b"raw")

    def mined(tx_hash, timeout):
        receipts[tx_hash] = {"status": 1, "blockNumber": 9}
        state["latest"] = 6
        return receipts[tx_hash]

    w3.eth.wait_for_transaction_receipt.side_effect = mined
    journal.send(w3, account, "p", to="0x2", value=1)
    assert [e["status"] for e in journal.entries()] == ["superseded", "confirmed"]


def test_send_refuses_if_dropped_nonce_stops_being_next(run):
    journal = Journal(run)
    journal.append({"hash": "0xaa", "sender": "0x1", "nonce": 5, "purpose": "p", "status": "dropped"})
    w3 = MagicMock()
    w3.eth.get_transaction_receipt.side_effect = LookupError
    calls = {"n": 0}

    def count(sender, block):
        calls["n"] += 1
        # reconcile sees 5 (still free); by the time send checks, something took it
        return 5 if calls["n"] == 1 else 6

    w3.eth.get_transaction_count.side_effect = count
    with pytest.raises(Exception, match="no longer next"):
        journal.send(w3, MagicMock(address="0x1"), "p", to="0x2", value=1)
    w3.eth.send_raw_transaction.assert_not_called()
