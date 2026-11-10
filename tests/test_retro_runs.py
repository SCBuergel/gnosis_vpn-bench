"""Retrospective fixes, part 2: repeated passes keep their own data, idle
ping, the PIX payment warning, the end-of-step log line, the app copy and
probe-regions. All offline: fakes, temporary directories, no DigitalOcean."""

import argparse
import json

import pytest
from conftest import FakeHost, ok
from sample_result import sample_result
from test_fast_runs import fake_ladder, hosts
from test_runs import user_config

from gvpn_bench import cli, runcli
from gvpn_bench.remote import ladder, probe, steps
from gvpn_bench.results.collection import Collection, Recording
from gvpn_bench.results.render import render_collection
from gvpn_bench.results.report_model import Heading, Paragraph, build
from gvpn_bench.results.schema import SchemaError, validate
from gvpn_bench.runs import plan_run, write_private_json


@pytest.fixture
def run(tmp_path):
    return plan_run(user_config(tmp_path), "v4-jura-prod", "serbia-jura-prod", run_id="retro2")


# -- repeated passes ---------------------------------------------------------------


def test_two_passes_keep_both_raw_data_sets(run, monkeypatch):
    fake_ladder(monkeypatch, lambda host: True)
    data = run.data / "serbia-jura-prod"
    for _ in range(2):
        ladder.run_ladder(
            run, run.config.profile(), run.config.target(), hosts("client1"), "192.0.2.1", "aa", data_dir=data
        )
    folders = sorted(p for p in data.iterdir() if p.is_dir())
    assert len(folders) == 2
    assert all((f / "rung1-client1.json").exists() for f in folders)


def test_ladder_passes_write_one_result_each_and_push_after_each(run, monkeypatch):
    user = user_config(run.path.parent.parent)
    monkeypatch.setattr(runcli, "load_user_config", lambda: user)
    monkeypatch.setattr(runcli, "_open", lambda args: (user, run, run.config.profile(), run.config.target()))
    monkeypatch.setattr(steps, "step_ladder", lambda run, profile, target: [])
    written, pushes = [], []
    monkeypatch.setattr(
        steps, "step_result", lambda run, profile, target, pass_no=None: written.append(pass_no) or run.path
    )
    monkeypatch.setattr(runcli, "auto_push", lambda run: pushes.append(len(written)))
    runcli.cmd_ladder(argparse.Namespace(run=str(run.path), target=None, no_push=False, passes=3))
    assert written == [1, 2, 3] and pushes == [1, 2, 3]


def test_pass_result_folders(run):
    target = run.config.target()
    assert steps.result_dir(run, target).name == "serbia-jura-prod"
    assert steps.result_dir(run, target, 1).name == "serbia-jura-prod"
    assert steps.result_dir(run, target, 3).name == "serbia-jura-prod-pass3"


# -- idle ping --------------------------------------------------------------------------

PING_OK = (
    "--- 9.9.9.9 ping statistics ---\n"
    "30 packets transmitted, 29 received, 3.33333% packet loss, time 29040ms\n"
    "rtt min/avg/max/mdev = 191.672/253.495/299.286/30.090 ms\n"
)
PING_NONE = "--- 9.9.9.9 ping statistics ---\n30 packets transmitted, 0 received, 100% packet loss\n"


def test_parse_ping():
    row = steps.parse_ping("client1", PING_OK)
    assert row == {
        "client": "client1",
        "sent": 30,
        "received": 29,
        "loss_pct": 3.3,
        "min_ms": 191.672,
        "mean_ms": 253.495,
        "max_ms": 299.286,
        "status": "measured",
    }
    none = steps.parse_ping("client2", PING_NONE)
    assert none["status"] == "no_reply" and none["mean_ms"] is None and none["loss_pct"] == 100.0


def test_idle_ping_runs_on_every_connected_client():
    fakes = [FakeHost(lambda argv, data: ok(PING_OK), name=n) for n in ("client1", "client3")]
    rows = steps.idle_ping(fakes, "9.9.9.9", count=30)
    assert [r["client"] for r in rows] == ["client1", "client3"]
    assert fakes[0].calls[0][:4] == ["ping", "-q", "-c", "30"] and fakes[0].calls[0][-1] == "9.9.9.9"


def test_idle_ping_is_validated_and_shown():
    result = sample_result()
    result["concurrent"]["idle_ping"] = [steps.parse_ping("client1", PING_OK)]
    validate(result)
    assert "Idle tunnel ping" in render_collection(Collection("t", [Recording("r1", result)]))
    result["concurrent"]["idle_ping"][0]["status"] = "no_reply"
    with pytest.raises(SchemaError):
        validate(result)


# -- PIX payment warning -------------------------------------------------------------


def v5_run(tmp_path):
    return plan_run(user_config(tmp_path), "v5-piz-palu-staging", "uk-piz-palu-staging", run_id="pix")


def funded_and_recovered(run, funded, recovered):
    write_private_json(run.state / "funding.json", [{"name": "client1", "wxhopr": funded, "xdai": "0.005"}])
    run.mark("recover", recovered_wxHOPR=recovered, recovered_xDAI="0")


def test_pix_run_that_spent_nothing_is_flagged(tmp_path):
    run = v5_run(tmp_path)
    funded_and_recovered(run, "241", "241.0")
    summary = steps.payment_summary(run, run.config.profile(), transferred_bytes=169_000_000)
    assert summary["pix"] is True and summary["spent_wxHOPR"] == "0.0"
    assert summary["warning"]["code"] == "pix_no_payment"


def test_pix_run_that_paid_and_v4_runs_are_not_flagged(tmp_path, run):
    pix = v5_run(tmp_path / "v5")
    funded_and_recovered(pix, "241", "230")
    assert "warning" not in steps.payment_summary(pix, pix.config.profile(), transferred_bytes=10)
    funded_and_recovered(run, "241", "241")
    summary = steps.payment_summary(run, run.config.profile(), transferred_bytes=10)
    assert summary["pix"] is False and "warning" not in summary
    assert steps.payment_summary(pix, pix.config.profile(), transferred_bytes=0).get("warning") is None


def test_payment_check_writes_results_and_warnings_come_first(tmp_path):
    run = v5_run(tmp_path)
    funded_and_recovered(run, "241", "241")
    directory = run.path / "results" / "uk-piz-palu-staging"
    directory.mkdir(parents=True)
    (directory / "result.json").write_text(json.dumps(sample_result()))
    warnings = steps.step_payment_check(run, run.config.profile())
    assert len(warnings) == 1 and "PIX took no payment" in warnings[0]
    result = json.loads((directory / "result.json").read_text())
    validate(result)
    assert result["payment"]["spent_wxHOPR"] == "0" and result["warnings"][0]["code"] == "pix_no_payment"
    blocks = build(Collection("t", [Recording("r1", result)]))
    assert isinstance(blocks[0], Heading) and blocks[0].text == "Warnings"
    assert isinstance(blocks[1], Paragraph) and blocks[1].warn
    assert "PIX took no payment" in (directory / "report.html").read_text()
    steps.step_payment_check(run, run.config.profile())  # run again: still one warning
    assert len(json.loads((directory / "result.json").read_text())["warnings"]) == 1


# -- end-of-step log line ---------------------------------------------------------------


def test_every_step_ends_with_a_finished_line(capsys):
    def cmd_destroy(args):
        return 0

    def cmd_results_push(args):
        raise RuntimeError("boom")

    assert cli.run_step(argparse.Namespace(run_func=cmd_destroy)) == 0
    assert "step destroy finished: exit 0" in capsys.readouterr().out
    with pytest.raises(RuntimeError):
        cli.run_step(argparse.Namespace(run_func=cmd_results_push))
    assert "step results push finished: exit 1" in capsys.readouterr().out


# -- app copy -------------------------------------------------------------------------------


def test_push_prints_the_app_copy(tmp_path, capsys):
    staged = [
        type("S", (), {"folder": "runs/2026-11-09--22-31-42", "files": {"report.html": ""}})(),
        type("S", (), {"folder": "reports/2026-11-09--22-22-54", "files": {"report.html": ""}})(),
    ]
    runcli._print_app_copies(tmp_path, staged)
    out = capsys.readouterr().out.strip().splitlines()
    assert out == [f"App copy: {tmp_path / 'reports/2026-11-09--22-22-54/report.html'}"]


# -- probe-regions --------------------------------------------------------------------------

MTR_AMS3 = """== ping
10 packets transmitted, 0 received, 100% packet loss, time 9231ms
== mtr
  6.|-- 203.0.113.89    0.0%     5   89.1  85.9  84.7  89.1   1.8
  9.|-- 203.0.113.61   20.0%     5  128.0 128.2 127.9 128.7   0.4
 10.|-- 198.51.100.242    0.0%     5  134.3 134.7 134.3 135.8   0.6
 11.|-- ???             100.0     5    0.0   0.0   0.0   0.0   0.0
"""


def test_parse_rtt_uses_the_last_responding_hop_or_the_ping():
    row = probe.parse_rtt("ams3", MTR_AMS3)
    assert (row.rtt_ms, row.rtt_source, row.hop, row.status) == (134.7, "last_hop", "198.51.100.242", "measured")
    direct = probe.parse_rtt("fra1", "== ping\n" + PING_OK + "== mtr\n")
    assert (direct.rtt_ms, direct.rtt_source) == (253.495, "ping")
    assert probe.parse_rtt("x", "== ping\n== mtr\n").status == "no_reply"


def test_exit_ip_takes_the_announced_public_ipv4(tmp_path, run):
    def query(indexer, text):
        # A private and a documentation address are skipped; 9.9.9.9 stands in for a public one.
        addresses = [
            "/ip4/10.0.0.1/udp/9091/quic-v1",
            "/ip4/192.0.2.178/udp/9091/quic-v1",
            "/ip4/9.9.9.9/udp/9091/quic-v1",
        ]
        accounts = [{"chainKey": "x", "multiAddresses": addresses}]
        return {"data": {"accounts": {"__typename": "AccountsList", "accounts": accounts}}}

    assert probe.exit_ip(run.config.target(), query) == "9.9.9.9"


class FakeApi:
    def __init__(self):
        self.droplets = {}
        self.next_id = 1

    def create_droplet(self, **kwargs):
        droplet = {"id": self.next_id, "status": "active", "tags": kwargs["tags"], "region": kwargs["region"]}
        droplet["networks"] = {"v4": [{"type": "public", "ip_address": f"198.51.100.{self.next_id}"}]}
        self.droplets[self.next_id] = droplet
        self.next_id += 1
        return droplet

    def get_droplet(self, droplet_id):
        return self.droplets.get(droplet_id)

    def droplets_with_tag(self, tag):
        return [d for d in self.droplets.values() if tag in d["tags"]]

    def delete_droplet(self, droplet_id, expected_tag):
        assert expected_tag in self.droplets[droplet_id]["tags"]
        del self.droplets[droplet_id]
        return True


def test_probe_regions_ranks_and_deletes_every_droplet(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "make_key", lambda d: _fake_key(tmp_path))
    answers = {"ams3": MTR_AMS3, "fra1": "== ping\n" + PING_OK + "== mtr\n"}
    api = FakeApi()

    def host(region, ip):
        return FakeHost(lambda argv, data: ok(answers[region]), name=region)

    results, left = probe.probe_regions(api, "192.0.2.178", ["fra1", "ams3"], "t", tmp_path, host_factory=host)
    assert [r.region for r in results] == ["ams3", "fra1"] and left == [] and api.droplets == {}


def test_probe_regions_deletes_droplets_after_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "make_key", lambda d: _fake_key(tmp_path))
    api = FakeApi()

    def host(region, ip):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        probe.probe_regions(api, "192.0.2.178", ["ams3", "lon1"], "t", tmp_path, host_factory=host)
    assert api.droplets == {}


def _fake_key(directory):
    key = directory / "probe_key"
    key.write_text("private")
    key.with_suffix(".pub").write_text("ssh-ed25519 AAAA test")
    return key


def test_probe_regions_deletes_a_droplet_whose_create_call_raised(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(probe, "make_key", lambda d: _fake_key(tmp_path))
    api = FakeApi()
    create = api.create_droplet

    def flaky_create(**kwargs):
        droplet = create(**kwargs)
        if droplet["id"] == 2:
            raise TimeoutError("created on the API, but the answer was lost")
        return droplet

    api.create_droplet = flaky_create
    with caplog.at_level("WARNING"), pytest.raises(TimeoutError):
        probe.probe_regions(api, "192.0.2.178", ["ams3", "lon1"], "t", tmp_path, host_factory=None, poll_s=0)
    assert api.droplets == {}
    assert "all probe droplets deleted" in caplog.text


def test_one_failing_region_keeps_the_others(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "make_key", lambda d: _fake_key(tmp_path))
    api = FakeApi()

    def answer(region):
        def respond(argv, data):
            if region == "lon1":
                raise TimeoutError("ssh hung")
            return ok(MTR_AMS3)

        return respond

    results, left = probe.probe_regions(
        api, "192.0.2.178", ["ams3", "lon1"], "t", tmp_path, host_factory=lambda r, ip: FakeHost(answer(r), name=r)
    )
    assert [(r.region, r.status) for r in results] == [("ams3", "measured"), ("lon1", "unreachable")]
    assert left == [] and api.droplets == {}
