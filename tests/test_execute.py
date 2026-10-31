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
