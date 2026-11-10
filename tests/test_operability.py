"""Run log, quiet polling, config's web3 line, status from the journal, capacity checks.

All offline: fake hosts, fake chain objects, fake DigitalOcean API.
"""

import argparse
import importlib.machinery
import importlib.util
import json
import logging
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from test_runs import user_config

from gvpn_bench import cli, runcli
from gvpn_bench.finance import wallet
from gvpn_bench.host import _run_local
from gvpn_bench.profiles import load_profile
from gvpn_bench.remote import steps
from gvpn_bench.remote.digitalocean import Api, ApiError
from gvpn_bench.runs import plan_run, write_private_json

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def planned(tmp_path, monkeypatch):
    user = user_config(tmp_path)
    run = plan_run(user, "v4-jura-prod", "berlin-jura-prod", run_id="o1")
    monkeypatch.setattr(runcli, "load_user_config", lambda: user)
    monkeypatch.setattr("gvpn_bench.runs.load_user_config", lambda: user)
    return user, run


# -- run log -------------------------------------------------------------------------


def test_run_step_writes_prints_logs_and_the_finished_line_to_the_run_log(planned):
    _user, run = planned

    def cmd_ladder(args):
        logging.getLogger("gnosis_speedtest").info("rung 1: 3 of 3 clients connected")
        print("  rung 1: 7.40 Mbit/s")
        return 0

    original = sys.stdout
    assert cli.run_step(argparse.Namespace(run_func=cmd_ladder, run="o1", log_level="INFO")) == 0
    assert sys.stdout is original
    text = (run.path / "logs" / "run.log").read_text()
    assert "INFO rung 1: 3 of 3 clients connected" in text
    assert "  rung 1: 7.40 Mbit/s" in text
    assert text.rstrip().endswith("step ladder finished: exit 0")
    assert (run.path / "logs").stat().st_mode & 0o777 == 0o700


def test_run_log_is_skipped_for_steps_without_one_run(planned):
    def cmd_results_push(args):
        return 0

    args = argparse.Namespace(run_func=cmd_results_push, run=["o1", "o2"], log_level="INFO")
    assert cli.run_step(args) == 0
    assert cli.step_run_dir(argparse.Namespace(run="no-such-run")) is None


# -- quiet polling ----------------------------------------------------------------------


def test_expected_failures_log_at_debug_not_warning(caplog):
    caplog.set_level(logging.DEBUG, logger="gnosis_speedtest")
    _run_local(["false"], 5, None, expected_failure=True)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    _run_local(["false"], 5, None)
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


# -- config ------------------------------------------------------------------------------


def test_config_fails_without_web3(planned, monkeypatch, capsys):
    monkeypatch.setattr(runcli, "web3_available", lambda: False)
    assert runcli.cmd_config(argparse.Namespace()) == 1
    assert "web3             : MISSING" in capsys.readouterr().out


def test_launcher_prefers_the_checkout_venv(monkeypatch, tmp_path):
    loader = importlib.machinery.SourceFileLoader("launcher", str(REPO / "gnosis_vpn-bench"))
    spec = importlib.util.spec_from_loader("launcher", loader)
    launcher = importlib.util.module_from_spec(spec)
    loader.exec_module(launcher)
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").touch()
    monkeypatch.setattr(launcher, "VENV", venv)
    monkeypatch.delenv("GVPN_BENCH_NO_VENV", raising=False)
    monkeypatch.setattr(sys, "prefix", "/usr")
    assert launcher._venv_python() == venv / "bin" / "python"
    monkeypatch.setattr(sys, "prefix", str(venv))
    assert launcher._venv_python() is None  # already running in it
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setenv("GVPN_BENCH_NO_VENV", "1")
    assert launcher._venv_python() is None


# -- status ------------------------------------------------------------------------------


def nodes():
    return [
        {"name": "client1", "address": "0x" + "a1" * 20, "wxhopr": "241", "xdai": "0.005"},
        {"name": "client2", "address": "0x" + "a2" * 20, "wxhopr": "241", "xdai": "0.005"},
    ]


def test_status_reports_planned_funding_as_not_sent(planned, capsys):
    _user, run = planned
    write_private_json(run.state / "funding.json", nodes())
    journal = run.state / "transactions.jsonl"
    journal.write_text(
        json.dumps({"purpose": "fund:client1:wxhopr", "status": "confirmed"})
        + "\n"
        + json.dumps({"purpose": "fund:client1:xdai", "status": "pending"})
        + "\n"
    )
    runcli.cmd_status(argparse.Namespace(run="o1"))
    out = capsys.readouterr().out
    assert "client1: PARTLY sent (wxhopr confirmed)" in out
    assert "client2: planned, not sent" in out
    assert "NOT recovered yet" in out


def test_status_without_a_journal_says_nothing_was_sent(planned, capsys):
    _user, run = planned
    write_private_json(run.state / "funding.json", nodes())
    runcli.cmd_status(argparse.Namespace(run="o1"))
    out = capsys.readouterr().out
    assert "client1: planned, not sent" in out
    assert "nothing sent yet" in out
    assert ": sent " not in out


# -- funding capacity --------------------------------------------------------------------


def chain(safe_tokens: int, safe_native: int):
    """A fake w3 whose Safe (0xsafe) holds the given wei; nodes hold nothing."""
    w3 = MagicMock()
    w3.to_checksum_address.side_effect = lambda a: a
    w3.to_wei.side_effect = lambda value, unit: int(Decimal(str(value)) * 10**18)
    w3.from_wei.side_effect = lambda value, unit: Decimal(value) / 10**18
    balances = {"0xsafe": safe_tokens}
    token = MagicMock()
    token.functions.balanceOf.side_effect = lambda a: SimpleNamespace(call=lambda: balances.get(a, 0))
    w3.eth.contract.return_value = token
    w3.eth.get_balance.side_effect = lambda a: safe_native if a == "0xsafe" else 0
    w3.eth.get_transaction_count.return_value = 0
    return w3


SOURCE = SimpleNamespace(account=SimpleNamespace(address="0xowner"), safe="0xsafe")


def test_shortfall_names_each_missing_asset():
    w3 = chain(safe_tokens=500 * 10**18, safe_native=10**16)
    problems = wallet.funding_shortfall(w3, SOURCE, 482 * 10**18, 10**16)
    assert problems == []
    problems = wallet.funding_shortfall(w3, SOURCE, 723 * 10**18, 15 * 10**15)
    assert len(problems) == 2
    assert "needs 723" in problems[0] and "xDAI" in problems[1]


def test_fund_nodes_sends_nothing_when_the_safe_cannot_pay_every_client(planned):
    _user, run = planned
    w3 = chain(safe_tokens=482 * 10**18, safe_native=5 * 10**15)  # xDAI for one client only
    with pytest.raises(RuntimeError, match="nothing sent"):
        wallet.fund_nodes(w3, run, SOURCE, nodes())
    w3.eth.send_raw_transaction.assert_not_called()


class FakeAccountApi(Api):
    def __init__(self, limit: int | None, existing: int):
        super().__init__(token="t")
        self.limit = limit
        self.existing = existing

    def request(self, method, path, body=None):
        if path == "account":
            if self.limit is None:
                raise ApiError(403, "You are not authorized to perform this operation")
            return {"account": {"droplet_limit": self.limit}}
        assert path.startswith("droplets?")
        return {"droplets": [], "meta": {"total": self.existing}}


def test_droplet_headroom():
    assert FakeAccountApi(limit=25, existing=4).droplet_headroom() == 21
    assert FakeAccountApi(limit=None, existing=4).droplet_headroom() is None


def test_capacity_preflight_refuses_too_few_droplets(planned, monkeypatch):
    user, run = planned
    monkeypatch.setattr(steps, "api_for", lambda user: FakeAccountApi(limit=5, existing=3))
    with pytest.raises(RuntimeError, match="room for 2 more droplets, this run needs 4"):
        steps.capacity_preflight(user, run, load_profile("v4-jura-prod"))


def test_capacity_preflight_checks_the_profile_estimate(planned, monkeypatch):
    user, run = planned
    profile = load_profile("v4-jura-prod")
    assert profile.expected_funding is not None
    monkeypatch.setattr(steps, "api_for", lambda user: FakeAccountApi(limit=None, existing=0))
    monkeypatch.setattr(wallet, "connect_chain", lambda: chain(safe_tokens=10**21, safe_native=10**16))
    monkeypatch.setattr(wallet, "load_funding_source", lambda path: SOURCE)
    with pytest.raises(RuntimeError, match=r"funding for 3 clients: the Safe holds 0\.01 xDAI, needs 0\.015"):
        steps.capacity_preflight(user, run, profile)
    monkeypatch.setattr(wallet, "connect_chain", lambda: chain(safe_tokens=10**21, safe_native=10**17))
    notes = steps.capacity_preflight(user, run, profile)
    assert notes[0] == "droplet limit: unknown (the API token cannot read the account)"
    assert notes[1].startswith("funding: the Safe covers 3 ×")


def test_capacity_preflight_without_estimate_only_notes_it(planned, monkeypatch):
    user, run = planned
    monkeypatch.setattr(steps, "api_for", lambda user: FakeAccountApi(limit=50, existing=0))
    notes = steps.capacity_preflight(user, run, load_profile("v5-piz-palu-dev"))
    assert notes[1] == "funding: not checked (profile v5-piz-palu-dev has no [funding] estimate)"
