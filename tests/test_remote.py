"""Remote orchestration safety, with fake APIs and hosts. No VM, VPN or chain."""

import json
import subprocess
from pathlib import Path

import pytest
from conftest import FakeHost as FakeHostFactory
from conftest import ok
from test_runs import user_config

from gvpn_bench.remote import deadline, provision
from gvpn_bench.remote.assets import ASSET_DIR
from gvpn_bench.remote.connect import classify_failure, log_findings
from gvpn_bench.remote.digitalocean import Api, run_tag
from gvpn_bench.remote.inventory import Machine, load_inventory, save_inventory
from gvpn_bench.remote.ladder import empty_rung, rung_entry
from gvpn_bench.remote.steps import build_result
from gvpn_bench.remote.sweep import ping_summary
from gvpn_bench.results.schema import validate
from gvpn_bench.runs import plan_run, write_private_json


@pytest.fixture
def run(tmp_path):
    return plan_run(user_config(tmp_path), "v5-piz-palu-dev", "kaunas", run_id="r1")


class FakeApi(Api):
    def __init__(self, fail_on_create: int | None = None, tags: list[str] | None = None):
        super().__init__(token="dop_v1_test")
        self.created = 0
        self.fail_on_create = fail_on_create
        self.tags = tags
        self.deleted: list[int] = []

    def request(self, method, path, body=None):
        if method == "POST":
            self.created += 1
            if self.created == self.fail_on_create:
                raise RuntimeError("API down")
            return {"droplet": {"id": 100 + self.created, "created_at": "2026-11-01T00:00:00Z"}}
        if method == "GET":
            return {"droplet": {"id": 1, "tags": self.tags or []}}
        if method == "DELETE":
            self.deleted.append(int(path.split("/")[1]))
            return None
        raise AssertionError(method)


def test_assets_are_valid_bash():
    for script in ASSET_DIR.glob("*.sh"):
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_cloud_config_is_json_after_header():
    text = provision.cloud_config("ssh-ed25519 AAAA test")
    header, body = text.split("\n", 1)
    assert header == "#cloud-config"
    data = json.loads(body)
    assert data["ssh_authorized_keys"] == ["ssh-ed25519 AAAA test"]
    assert ["touch", provision.READY_MARKER] in data["runcmd"]
    assert "chage" in json.dumps(data["bootcmd"])
    assert "chage" not in json.dumps(data["runcmd"])


def test_ready_probe_never_multiplexes(monkeypatch, tmp_path):
    from gvpn_bench.host import CommandResult, SshHost

    seen = []

    def fake_run(self, argv, *, timeout_s=30, input_bytes=None):
        seen.append(self.control_dir)
        return CommandResult(0, "", "")

    monkeypatch.setattr(SshHost, "run", fake_run)
    provision.wait_ready(SshHost(name="h", address="192.0.2.1", control_dir=tmp_path))
    assert seen == [None]


def test_droplet_name_is_sanitised():
    assert provision.droplet_name("20261101_X/Y", "client1") == "gvpn-bench-20261101-x-y-client1"


def test_inventory_is_saved_after_each_create(run):
    api = FakeApi(fail_on_create=3)
    with pytest.raises(RuntimeError, match="API down"):
        provision.create_machines(run, api, "ssh-ed25519 AAAA")
    assert [m.name for m in load_inventory(run)] == ["origin", "client1"]


def test_preflight_refuses_second_provision_and_unpinned_profile(run, tmp_path):
    save_inventory(run, [Machine("origin", "origin", 1, "c-2", "fra1", "now", "192.0.2.1")])
    with pytest.raises(RuntimeError, match="already has an inventory"):
        provision.preflight(run, run.config.profile())
    other = plan_run(user_config(tmp_path), "v5-piz-palu-prod", "serbia-piz-palu-prod", run_id="r2")
    with pytest.raises(RuntimeError, match="no package pin"):
        provision.preflight(other, other.config.profile())


def test_delete_refuses_untagged_droplet():
    with pytest.raises(RuntimeError, match="refusing"):
        FakeApi(tags=["someone-else"]).delete_droplet(1, run_tag("r1"))
    api = FakeApi(tags=[run_tag("r1")])
    assert api.delete_droplet(1, run_tag("r1"))
    assert api.deleted == [1]


def test_deadline_refuses_funded_run_without_backups(run):
    save_inventory(run, [Machine("client1", "client", 7, "c-4", "fra1", "now", "192.0.2.7")])
    write_private_json(run.state / "funding.json", [{"name": "client1", "address": "0x" + "11" * 20}])
    api = FakeApi(tags=[run_tag("r1")])
    record = deadline.fire(run, api)
    assert "refused" in record
    assert api.deleted == []


def test_deadline_deletes_when_backups_exist(run, tmp_path):
    save_inventory(run, [Machine("client1", "client", 7, "c-4", "fra1", "now", "192.0.2.7")])
    write_private_json(run.state / "funding.json", [{"name": "client1", "address": "0x" + "11" * 20}])
    key = tmp_path / "hopr.id"
    key.write_text("x")
    write_private_json(run.state / "backups.json", {"client1": {"identity_files": [str(key)]}})
    api = FakeApi(tags=[run_tag("r1")])
    assert deadline.fire(run, api)["deleted"] == ["client1"]
    assert api.deleted == [7]


def test_failure_classification():
    assert classify_failure({"x": "session rejected: UnacceptablePixParams"}) == "UnacceptablePixParams"
    assert (
        classify_failure({"log": ["Status(404, None), url: .../api/v1/clients/register"]})
        == "http_404_register"
    )
    assert classify_failure({"probe": {"last_error": "Request timed out"}}) == "control_timeout"
    assert classify_failure({}) == "timeout"
    log = "a\nWARN probe check failed check=Load\nb\nERROR UnacceptablePixParams\n"
    assert len(log_findings(log)) == 2


def test_ping_summary():
    out = "64 bytes from 9.9.9.9: icmp_seq=1 ttl=57 time=10.0 ms\n64 bytes: time=20.0 ms\n"
    assert ping_summary(out, 3) == (15.0, pytest.approx(100 / 3))
    assert ping_summary("", 3) == (None, 100.0)


def agent_result(start, end, nbytes, sha="aa"):
    return {
        "scheduled_start": start,
        "start": start,
        "end": end,
        "bytes": nbytes,
        "complete": True,
        "timed_out": False,
        "sha256": sha,
        "events": [[start, 0], [end, nbytes]],
        "pings": [],
    }


def test_rung_entry_verifies_hash_and_handles_no_overlap():
    entry = rung_entry(1, {"client1": agent_result(0, 2, 250_000)}, "aa", None)
    assert entry["status"] == "measured"
    assert entry["per_client"][0]["sha256_verified"]
    assert entry["total_mbps"] == pytest.approx(1.0)
    bad = rung_entry(1, {"client1": agent_result(0, 2, 250_000, sha="bb")}, "aa", None)
    assert not bad["per_client"][0]["sha256_verified"]
    split = rung_entry(2, {"a": agent_result(0, 1, 10), "b": agent_result(2, 3, 10)}, "aa", 10.0)
    assert (split["status"], split["failure_status"], split["total_mbps"]) == ("failed", "no_overlap", None)


def test_build_result_from_state_is_valid(run):
    write_private_json(
        run.state / "machines.json",
        [{"role": "client", "name": "client1", "size": "c-4", "vcpus": 4, "ctl_version": "0.101.1"}],
    )
    write_private_json(
        run.state / "ladder-kaunas.json",
        {
            "rungs": [
                rung_entry(1, {"client1": agent_result(0, 2, 250_000)}, "aa", None),
                empty_rung(2, ["client1", "client2"], "not_run", "not_run"),
            ],
            "finished_at": "2026-11-01T00:10:00Z",
        },
    )
    write_private_json(
        run.state / "connect-kaunas.json",
        [
            {
                "client": "client1",
                "status": "connected",
                "detail": "",
                "connect_time_s": 9.5,
                "exit_address": "0x01961ef9375e063ab32addf44f0acc8adc71c888",
                "egress_changed": True,
                "evidence": {},
                "at": "2026-11-01T00:00:00Z",
            },
            {
                "client": "client2",
                "status": "UnacceptablePixParams",
                "detail": "x",
                "connect_time_s": None,
                "exit_address": None,
                "egress_changed": None,
                "evidence": {},
                "at": "2026-11-01T00:00:00Z",
            },
        ],
    )
    result = build_result(run, run.config.profile(), run.config.target())
    validate(result)
    assert result["run"]["kind"] == "ladder"
    statuses = {f["status"] for f in result["failures"]}
    assert statuses == {"not_run", "UnacceptablePixParams"}


def test_install_script_takes_values_as_arguments():
    text = (ASSET_DIR / "install-client.sh").read_text()
    assert "PROFILE_" not in text and "{" not in text.split("set -euo pipefail", 1)[0]
    assert Path(ASSET_DIR / "ladder_agent.py").exists()


def test_fake_host_ok_helper_roundtrip():
    assert json.loads(ok({"a": 1}).stdout) == {"a": 1}


def test_identity_backup_before_and_after_safe_exists(run, tmp_path):
    import base64

    from gvpn_bench.remote import backup
    from gvpn_bench.runs import UserConfig

    user = UserConfig(
        runs_dir=tmp_path,
        identity_backup_dir=tmp_path / "ids",
        digitalocean_token_file=tmp_path / "t",
        funding_key_file=tmp_path / "k",
        results_repo=None,
        source=None,
    )
    files = {"hopr.id": b"id", "hopr.pass": b"pw"}

    def respond(argv, data):
        return ok({name: base64.b64encode(v).decode() for name, v in files.items()})

    host = FakeHostFactory(respond)
    profile = run.config.profile()
    saved = backup.backup_identity(user, run, profile, host)
    assert sorted(p.name for p in saved) == ["hopr.id", "hopr.pass"]
    with pytest.raises(RuntimeError, match="Safe file"):
        backup.backup_identity(user, run, profile, host, require_safe=True)
    files["hopr.safe"] = b"0xsafe"
    assert len(backup.backup_identity(user, run, profile, host, require_safe=True)) == 3
    files["hopr.id"] = b"changed"
    with pytest.raises(RuntimeError, match="changed"):
        backup.backup_identity(user, run, profile, host)
