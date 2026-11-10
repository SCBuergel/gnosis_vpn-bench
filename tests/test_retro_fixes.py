"""Fixes from the 2026-11-09 retrospective: a dead package link fails the
pre-flight, partial connects carry on, refused clients keep their Safe info
and recovery retries a failed balance read. All offline."""

import argparse

from test_runs import user_config

from gvpn_bench import runcli
from gvpn_bench.finance import recover
from gvpn_bench.remote import preflight, steps
from gvpn_bench.remote.connect import ConnectOutcome
from gvpn_bench.remote.inventory import Machine
from gvpn_bench.runs import plan_run, write_private_json
from gvpn_bench.vpn import CtlError


def make_run(tmp_path):
    return plan_run(user_config(tmp_path), "v4-jura-prod", "serbia-jura-prod", run_id="retro")


def test_dead_package_link_fails_the_preflight(tmp_path):
    profile = make_run(tmp_path).config.profile()
    assert preflight.package_problems(profile, head=lambda url: 200) == []
    problem = preflight.package_problems(profile, head=lambda url: 404)
    assert len(problem) == 1 and "HTTP 404" in problem[0] and profile.name in problem[0]

    def unreachable(url):
        raise OSError("no route")

    assert "unreachable" in preflight.package_problems(profile, head=unreachable)[0]


def test_step_preflight_refuses_a_dead_package(tmp_path, monkeypatch):
    run = make_run(tmp_path)
    monkeypatch.setattr(preflight, "check", lambda *a, **k: [])
    monkeypatch.setattr(preflight, "package_problems", lambda profile: ["package x answered HTTP 404"])
    try:
        steps.step_preflight(run, run.config.profile())
    except RuntimeError as error:
        assert "HTTP 404" in str(error) and "no VM created" in str(error)
    else:
        raise AssertionError("pre-flight passed a dead package")


def outcomes(*statuses):
    return [ConnectOutcome(f"client{i}", status) for i, status in enumerate(statuses, 1)]


def test_connect_succeeds_when_some_clients_connect(tmp_path, monkeypatch, capsys):
    run = make_run(tmp_path)
    args = argparse.Namespace(run=str(run.path), target=None, yes=True)
    monkeypatch.setattr(steps, "step_connect", lambda *a: outcomes("connected", "http_404_register"))
    assert runcli.cmd_connect(args) == 0
    assert "1 of 2 clients connected" in capsys.readouterr().out
    monkeypatch.setattr(steps, "step_connect", lambda *a: outcomes("http_404_register", "timeout"))
    assert runcli.cmd_connect(args) == 1


class Vpn:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def ctl(self, command, timeout_s=0):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_refused_client_keeps_its_safe_info(tmp_path, monkeypatch):
    from gvpn_bench.remote import guards

    run = make_run(tmp_path)
    write_private_json(run.state / "baseline.json", [])
    machine = Machine("client1", "client", 1, "c-4", "ams3", "now", "192.0.2.1")
    host = type("H", (), {"name": "client1"})()
    saved = []
    monkeypatch.setattr(steps, "client_hosts", lambda run: [(machine, host)])
    monkeypatch.setattr(steps, "machine", lambda run, name: machine)
    monkeypatch.setattr(guards, "start_guards", lambda host, profile: None)
    monkeypatch.setattr(guards, "start_heartbeat", lambda run: None)
    monkeypatch.setattr(steps, "disconnect", lambda host, profile: None)
    monkeypatch.setattr(steps, "backup_identity", lambda *a, **k: None)
    monkeypatch.setattr(steps, "connect_client", lambda *a, **k: ConnectOutcome("client1", "http_404_register"))
    monkeypatch.setattr(steps, "vpn_for", lambda h, p: Vpn([{"Balance": {"Ok": {"info": {"safe": "0xs"}}}}]))
    monkeypatch.setattr(steps, "save_safe_info", lambda user, run, profile, name, info: saved.append((name, info)))
    steps.step_connect(None, run, run.config.profile(), run.config.target())
    assert saved == [("client1", {"safe": "0xs"})]


def test_refused_client_without_balance_does_not_break_connect(tmp_path, monkeypatch):
    from gvpn_bench.remote import guards

    run = make_run(tmp_path)
    write_private_json(run.state / "baseline.json", [])
    machine = Machine("client1", "client", 1, "c-4", "ams3", "now", "192.0.2.1")
    host = type("H", (), {"name": "client1"})()
    monkeypatch.setattr(steps, "client_hosts", lambda run: [(machine, host)])
    monkeypatch.setattr(steps, "machine", lambda run, name: machine)
    monkeypatch.setattr(guards, "start_guards", lambda host, profile: None)
    monkeypatch.setattr(guards, "start_heartbeat", lambda run: None)
    monkeypatch.setattr(steps, "disconnect", lambda host, profile: None)
    monkeypatch.setattr(steps, "backup_identity", lambda *a, **k: None)
    monkeypatch.setattr(steps, "connect_client", lambda *a, **k: ConnectOutcome("client1", "http_404_register"))
    monkeypatch.setattr(steps, "vpn_for", lambda h, p: Vpn([CtlError("exit 70")]))
    result = steps.step_connect(None, run, run.config.profile(), run.config.target())
    assert [o.status for o in result] == ["http_404_register"]


def test_recovery_retries_a_failed_balance_read():
    ok = {"Balance": {"Ok": {"info": {"safe": "0xs"}}}}
    vpn = Vpn([CtlError("exit 70"), ok])
    assert recover.read_balance(vpn, "client2", attempts=3, pause_s=0) == {"info": {"safe": "0xs"}}
    assert vpn.calls == 2
    vpn = Vpn([CtlError("exit 70")] * 3)
    assert recover.read_balance(vpn, "client2", attempts=3, pause_s=0) is None
    assert vpn.calls == 3
