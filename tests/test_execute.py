"""Multi-target runs and `execute` teardown policy, with all steps faked."""

import argparse

import pytest
from test_runs import user_config

from gvpn_bench import runcli
from gvpn_bench.profiles import load_profile
from gvpn_bench.remote import provision, steps
from gvpn_bench.runs import open_run, plan_run


def test_v4_profile_carries_socket_buffer_sysctls():
    settings = dict(load_profile("v4-jura-prod").sysctl)
    assert settings["net.core.rmem_max"] == "16777216"
    assert load_profile("v5-piz-palu-dev").sysctl == ()


def test_plan_with_two_targets(tmp_path):
    user = user_config(tmp_path)
    run = plan_run(user, "v4-jura-prod", ["netherlands-jura-prod", "austria-jura-prod"], run_id="m1")
    config = open_run("m1", user).config
    assert config.target_names == ("netherlands-jura-prod", "austria-jura-prod")
    assert [t.label for t in config.targets()] == ["Netherlands", "Austria"]
    assert run.config.client_config is None


def test_plan_refuses_mixed_networks_and_duplicates(tmp_path):
    user = user_config(tmp_path)
    with pytest.raises(ValueError):
        plan_run(user, "v4-jura-prod", ["netherlands-jura-prod", "kaunas"], run_id="m2")
    with pytest.raises(ValueError):
        plan_run(user, "v4-jura-prod", ["austria-jura-prod", "austria-jura-prod"], run_id="m3")


@pytest.fixture
def faked(tmp_path, monkeypatch):
    user = user_config(tmp_path)
    run = plan_run(user, "v4-jura-prod", ["netherlands-jura-prod", "austria-jura-prod"], run_id="e1")
    calls = []
    monkeypatch.setattr(runcli, "load_user_config", lambda: user)
    monkeypatch.setattr(provision, "preflight", lambda run, profile: None)
    monkeypatch.setattr(provision, "describe_plan", lambda run, profile: "plan")
    monkeypatch.setattr(runcli, "_teardown", lambda user, run, profile: calls.append("teardown") or True)
    return run, calls, monkeypatch


def args(run, keep=False):
    return argparse.Namespace(run=str(run.path), yes=True, keep=keep, target=None)


def test_execute_tears_down_after_a_failure(faked):
    run, calls, monkeypatch = faked

    def boom(*a, **k):
        raise RuntimeError("droplet quota exceeded")

    monkeypatch.setattr(steps, "step_provision", boom)
    assert runcli.cmd_execute(args(run)) == 1
    assert calls == ["teardown"]


def test_execute_tears_down_after_ctrl_c(faked):
    run, calls, monkeypatch = faked

    def interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(steps, "step_provision", interrupt)
    assert runcli.cmd_execute(args(run)) == 1
    assert calls == ["teardown"]


def test_execute_keep_skips_teardown(faked):
    run, calls, monkeypatch = faked
    monkeypatch.setattr(steps, "step_provision", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    runcli.cmd_execute(args(run, keep=True))
    assert calls == []


def test_execute_measures_every_target_then_tears_down(faked):
    run, calls, monkeypatch = faked
    monkeypatch.setattr(steps, "step_provision", lambda *a: run.mark("provision"))
    monkeypatch.setattr(steps, "step_fund_plan", lambda *a: {"nodes": []})
    monkeypatch.setattr(steps, "step_fund_send", lambda *a: run.mark("fund"))
    monkeypatch.setattr(
        steps, "step_connect", lambda user, run, profile, target: calls.append(target.name) or []
    )
    monkeypatch.setattr(steps, "step_ladder", lambda run, profile, target: [])
    monkeypatch.setattr(steps, "step_result", lambda run, profile, target: run.path / target.name)
    assert runcli.cmd_execute(args(run)) == 0
    assert calls == ["netherlands-jura-prod", "austria-jura-prod", "teardown"]


def test_destroy_without_confirmed_funding_skips_recovery(tmp_path):
    run = plan_run(user_config(tmp_path), "v4-jura-prod", "austria-jura-prod", run_id="d1")
    assert not steps.funds_sent(run)


def test_default_id_for_two_targets_is_valid(tmp_path):
    run = plan_run(user_config(tmp_path), "v4-jura-prod", ["netherlands-jura-prod", "austria-jura-prod"])
    assert "netherlands-jura-prod_austria-jura-prod" in run.config.id


# -- funds_sent decides whether destroy must first verify recovery ---------------


@pytest.mark.parametrize(
    "status, expected",
    [("pending", True), ("confirmed", True), ("dropped", True), ("reverted", False), ("superseded", False)],
)
def test_funds_sent_by_journal_state(tmp_path, status, expected):
    from gvpn_bench.finance.wallet import Journal

    run = plan_run(user_config(tmp_path), "v4-jura-prod", "austria-jura-prod", run_id="fs")
    Journal(run).append(
        {"hash": "0x1", "sender": "0x2", "nonce": 0, "purpose": "fund:client1:wxhopr", "status": status}
    )
    Journal(run).append(
        {"hash": "0x3", "sender": "0x2", "nonce": 1, "purpose": "close:client1:x", "status": "pending"}
    )
    assert steps.funds_sent(run) is expected


def test_destroy_of_funded_run_verifies_recovery_first(tmp_path, monkeypatch):
    from gvpn_bench.finance import recover
    from gvpn_bench.finance import wallet as wallet_mod
    from gvpn_bench.remote.inventory import Machine, save_inventory

    run = plan_run(user_config(tmp_path), "v4-jura-prod", "austria-jura-prod", run_id="dv")
    save_inventory(run, [Machine("client1", "client", 1, "c-4", "fra1", "now", "192.0.2.1")])
    wallet_mod.Journal(run).append(
        {"hash": "0x1", "sender": "0x2", "nonce": 0, "purpose": "fund:client1:xdai", "status": "confirmed"}
    )
    monkeypatch.setattr(wallet_mod, "connect_chain", lambda: object())

    def refuse(*a):
        raise RuntimeError("recovery has not finished")

    monkeypatch.setattr(recover, "verify_for_destroy", refuse)
    monkeypatch.setattr(steps, "api_for", lambda user: pytest.fail("must not reach the API"))
    with pytest.raises(RuntimeError, match="recovery has not finished"):
        steps.step_destroy(None, run, run.config.profile())


# -- _teardown, the default end of `execute` --------------------------------------


@pytest.fixture
def teardown_env(tmp_path, monkeypatch):
    from gvpn_bench.finance import recover
    from gvpn_bench.remote import inventory

    run = plan_run(user_config(tmp_path), "v4-jura-prod", "austria-jura-prod", run_id="td")
    calls = []
    state = {"funded": True, "complete": False, "inventory": ["x"]}
    monkeypatch.setattr(steps, "funds_sent", lambda run: state["funded"])
    monkeypatch.setattr(recover, "recovery_complete", lambda run, profile, clients: state["complete"])
    monkeypatch.setattr(inventory, "clients", lambda run: [])
    monkeypatch.setattr(inventory, "load_inventory", lambda run: state["inventory"])
    monkeypatch.setattr(steps, "step_recover_initiate", lambda *a: calls.append("initiate"))
    monkeypatch.setattr(
        steps,
        "step_recover_finalize",
        lambda *a: calls.append("finalize") or {"recovered_wxHOPR": "1", "recovered_xDAI": "0"},
    )
    monkeypatch.setattr(steps, "step_destroy", lambda *a: calls.append("destroy"))
    return run, calls, state, monkeypatch


def test_teardown_recovers_then_destroys(teardown_env):
    run, calls, _state, _ = teardown_env
    assert runcli._teardown(None, run, run.config.profile())
    assert calls == ["initiate", "finalize", "destroy"]


def test_teardown_without_funds_only_destroys(teardown_env):
    run, calls, state, _ = teardown_env
    state["funded"] = False
    assert runcli._teardown(None, run, run.config.profile())
    assert calls == ["destroy"]


def test_teardown_skips_done_steps_and_empty_inventory(teardown_env):
    run, calls, state, _ = teardown_env
    run.mark("recover")
    state["inventory"] = []
    assert runcli._teardown(None, run, run.config.profile())
    assert calls == []


def test_teardown_resumes_complete_journal_at_finalize(teardown_env):
    run, calls, state, _ = teardown_env
    state["complete"] = True
    assert runcli._teardown(None, run, run.config.profile())
    assert calls == ["finalize", "destroy"]


@pytest.mark.parametrize("error", [RuntimeError("rpc down"), KeyboardInterrupt()])
def test_teardown_failure_reports_and_keeps_vms(teardown_env, capsys, error):
    run, calls, _state, monkeypatch = teardown_env

    def fail(*a):
        raise error

    monkeypatch.setattr(steps, "step_recover_finalize", fail)
    from gvpn_bench.runs import write_private_json

    write_private_json(run.state / "deadline-timer.json", {"unit": "u"})
    assert runcli._teardown(None, run, run.config.profile()) is False
    out = capsys.readouterr().out
    assert "TEARDOWN FAILED" in out and "deletion timer (u)" in out
    assert "destroy" not in calls


# -- connect leaves the previous exit first ----------------------------------------


def test_connect_disconnects_before_connecting(tmp_path, monkeypatch):
    from gvpn_bench.remote import guards
    from gvpn_bench.remote.connect import ConnectOutcome
    from gvpn_bench.remote.inventory import Machine
    from gvpn_bench.runs import write_private_json

    run = plan_run(user_config(tmp_path), "v4-jura-prod", "austria-jura-prod", run_id="cd")
    write_private_json(run.state / "baseline.json", [])
    order = []
    host = type("H", (), {"name": "client1"})()
    machine = Machine("client1", "client", 1, "c-4", "fra1", "now", "192.0.2.1")
    monkeypatch.setattr(steps, "client_hosts", lambda run: [(machine, host)])
    monkeypatch.setattr(steps, "machine", lambda run, name: machine)
    monkeypatch.setattr(guards, "start_guards", lambda host, profile: None)
    monkeypatch.setattr(guards, "start_heartbeat", lambda run: None)
    monkeypatch.setattr(steps, "disconnect", lambda host, profile: order.append("disconnect"))
    monkeypatch.setattr(steps, "backup_identity", lambda *a, **k: None)
    monkeypatch.setattr(
        steps,
        "connect_client",
        lambda *a, **k: order.append("connect") or ConnectOutcome("client1", "DestinationNotFound"),
    )
    steps.step_connect(None, run, run.config.profile(), run.config.target())
    assert order == ["disconnect", "connect"]


def test_declining_the_funding_prompt_sends_nothing_and_tears_down(faked):
    run, calls, monkeypatch = faked
    answers = iter([True, False])  # yes to the run, no to the amounts
    monkeypatch.setattr(runcli, "confirm", lambda prompt, assume_yes: next(answers))
    monkeypatch.setattr(steps, "step_provision", lambda *a: run.mark("provision"))
    monkeypatch.setattr(
        steps, "step_fund_plan", lambda *a: {"nodes": [{"name": "c1", "wxhopr": "1", "xdai": "0"}]}
    )
    monkeypatch.setattr(steps, "step_fund_send", lambda *a: pytest.fail("funds sent without consent"))
    assert runcli.cmd_execute(argparse.Namespace(run=str(run.path), yes=False, keep=False, target=None)) == 1
    assert calls == ["teardown"]
