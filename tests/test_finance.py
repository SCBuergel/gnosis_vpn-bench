"""Funding and recovery rules with fake chain objects. Never touches a real chain."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from test_runs import user_config

from gvpn_bench.finance import chain as chain_mod
from gvpn_bench.finance import recover
from gvpn_bench.finance.wallet import AmbiguousTransactionError, Journal, fund_nodes, parse_key_file
from gvpn_bench.remote.backup import validate_payment_archive
from gvpn_bench.runs import plan_run, write_private_json

CHANNELS = "0x" + "12" * 20


@pytest.fixture
def run(tmp_path):
    return plan_run(user_config(tmp_path), "v5-piz-palu-dev", "kaunas", run_id="f1")


def chain_data(network="piz-palu-dev"):
    return {
        "network": network,
        "chainId": 100,
        "contractAddresses": json.dumps({"token": chain_mod.WXHOPR_TOKEN, "channels": CHANNELS}),
    }


def test_chain_validation_rejects_wrong_network_token_and_channels():
    assert chain_mod.validate_chain(chain_data(), "piz-palu-dev")["contractAddresses"]["channels"] == CHANNELS
    with pytest.raises(ValueError):
        chain_mod.validate_chain(chain_data("jura-prod"), "piz-palu-dev")
    bad_token = {**chain_data(), "contractAddresses": {"token": "0x" + "00" * 20, "channels": CHANNELS}}
    with pytest.raises(ValueError, match="token"):
        chain_mod.validate_chain(bad_token, "piz-palu-dev")
    zero = {
        **chain_data(),
        "contractAddresses": {"token": chain_mod.WXHOPR_TOKEN, "channels": "0x" + "00" * 20},
    }
    with pytest.raises(ValueError, match="channels"):
        chain_mod.validate_chain(zero, "piz-palu-dev")


def test_missing_chain_metadata_has_no_fallback(run):
    with pytest.raises(ValueError, match="no pinned chain"):
        chain_mod.load_chain(run, run.config.profile())


def test_key_file_needs_all_fields():
    assert parse_key_file("private_key: 0xab\neoa: 0x1\nsafe: 0x2\n")["safe"] == "0x2"
    with pytest.raises(ValueError, match="safe"):
        parse_key_file("private_key: 0xab\neoa: 0x1\n")


def fake_w3(receipts=None, nonce=0):
    receipts = receipts or {}
    w3 = MagicMock()

    def get_receipt(tx_hash):
        if tx_hash not in receipts:
            raise LookupError("not found")
        return receipts[tx_hash]

    w3.eth.get_transaction_receipt.side_effect = get_receipt
    w3.eth.get_transaction_count.return_value = nonce
    return w3


def test_pending_transaction_without_receipt_blocks_sending(run):
    journal = Journal(run)
    journal.append(
        {"hash": "0xaa", "sender": "0x1", "nonce": 4, "purpose": "fund:client1:wxhopr", "status": "pending"}
    )
    w3 = fake_w3(nonce=5)
    with pytest.raises(AmbiguousTransactionError, match="nonce used"):
        journal.reconcile(w3)
    with pytest.raises(AmbiguousTransactionError):
        journal.send(w3, SimpleNamespace(address="0x1"), "fund:client1:xdai", to="0x2", value=1)
    w3.eth.send_raw_transaction.assert_not_called()


def test_receipt_resolves_pending_entry(run):
    journal = Journal(run)
    journal.append({"hash": "0xaa", "sender": "0x1", "nonce": 4, "purpose": "p", "status": "pending"})
    journal.reconcile(fake_w3({"0xaa": {"status": 1, "blockNumber": 9}}))
    assert journal.confirmed("p")


def test_already_funded_node_is_refused(run):
    w3 = fake_w3()
    w3.to_checksum_address.side_effect = lambda a: a
    token = MagicMock()
    token.functions.balanceOf.return_value.call.return_value = 5
    w3.eth.contract.return_value = token
    w3.eth.get_balance.return_value = 0
    source = SimpleNamespace(account=SimpleNamespace(address="0x1"), safe="0x2")
    with pytest.raises(RuntimeError, match="did not send"):
        fund_nodes(w3, run, source, [{"name": "client1", "address": "0x3", "wxhopr": "1", "xdai": "0.01"}])
    w3.eth.send_raw_transaction.assert_not_called()


def test_open_channel_cannot_be_reported_closed():
    w3 = MagicMock()
    w3.eth.get_logs.return_value = []
    contract = MagicMock()
    contract.functions.channels.return_value.call.return_value = [1, 0, 0, 0, 1]
    entry = {"node": "0x" + "34" * 20, "from_block": 1, "pending": [{"peer": "0x" + "56" * 20}]}
    with pytest.raises(RuntimeError, match="not closed"):
        recover.verify_channels_closed(w3, contract, entry)


def recovery_journal(run, clients):
    write_private_json(chain_mod.chain_file(run), chain_mod.validate_chain(chain_data(), "piz-palu-dev"))
    journal = {
        c: {
            "node": "0x" + "34" * 20,
            "safe": "0x" + "56" * 20,
            "pending": [],
            "from_block": 1,
            "network": "piz-palu-dev",
            "channels_contract": CHANNELS,
        }
        for c in clients
    }
    write_private_json(run.state / "recovery.json", journal)


def test_partial_journal_cannot_finalize_or_authorize_deletion(run):
    recovery_journal(run, ["client1"])
    assert not recover.recovery_complete(run, run.config.profile(), ["client1", "client2"])
    with pytest.raises(ValueError, match="every client"):
        recover.load_recovery(run, run.config.profile(), ["client1", "client2"])


def test_destroy_refuses_stale_inventory_and_missing_cleanup(run):
    recovery_journal(run, ["client1"])
    profile = run.config.profile()
    with pytest.raises(RuntimeError, match="no cleanup"):
        recover.verify_for_destroy(MagicMock(), run, profile, ["client1"], [1])
    cleanup = {
        "clients": ["client1"],
        "droplet_ids": [1],
        "recovery_digest": recover.recovery_digest(run),
        "network": "piz-palu-dev",
        "all_channels_closed": True,
        "safe_token_balances_zero": True,
        "node_native_balances": {"client1": 0},
    }
    write_private_json(run.state / "cleanup.json", cleanup)
    with pytest.raises(RuntimeError, match="droplets"):
        recover.verify_for_destroy(MagicMock(), run, profile, ["client1"], [2])


def test_refunded_node_cannot_be_deleted(run, monkeypatch):
    recovery_journal(run, ["client1"])
    cleanup = {
        "clients": ["client1"],
        "droplet_ids": [1],
        "recovery_digest": recover.recovery_digest(run),
        "network": "piz-palu-dev",
        "all_channels_closed": True,
        "safe_token_balances_zero": True,
        "node_native_balances": {"client1": 0},
    }
    write_private_json(run.state / "cleanup.json", cleanup)
    monkeypatch.setattr(recover, "node_account", lambda run, c: SimpleNamespace(address="0x" + "34" * 20))
    monkeypatch.setattr(recover, "verify_channels_closed", lambda *a: None)
    w3 = MagicMock()
    token = MagicMock()
    token.functions.balanceOf.return_value.call.return_value = 1
    monkeypatch.setattr(recover, "token_contract", lambda w3: token)
    with pytest.raises(RuntimeError, match="balances changed"):
        recover.verify_for_destroy(w3, run, run.config.profile(), ["client1"], [1])


def payment_archive(members):
    import gzip
    import io
    import tarfile

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


def test_payment_archive_validation():
    validate_payment_archive(payment_archive([("pix/db.sqlite", b"data")]))
    with pytest.raises(ValueError, match="no database"):
        validate_payment_archive(payment_archive([]))
    with pytest.raises(ValueError, match="unexpected"):
        validate_payment_archive(payment_archive([("pix/db", b"x"), ("etc/passwd", b"x")]))
    with pytest.raises(ValueError):
        validate_payment_archive(payment_archive([("pix/../x", b"x")]))
    with pytest.raises(OSError):
        validate_payment_archive(b"not gzip")
