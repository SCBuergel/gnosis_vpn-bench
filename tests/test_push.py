"""results push: allowlist, scan and refusal rules. Uses a local bare repo only."""

import json
import subprocess

import pytest
from sample_result import sample_result

from gvpn_bench.results import push
from gvpn_bench.results.store import write_report, write_result


def make_run(tmp_path, result=None):
    run_dir = tmp_path / "run"
    (run_dir / "state").mkdir(parents=True)
    write_result(run_dir, result or sample_result())
    write_report(run_dir)
    (run_dir / "state" / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    (run_dir / "state" / "inventory.json").write_text(json.dumps([{"name": "client1", "ip": "203.0.113.9"}]))
    (run_dir / "run.log").write_text("log")
    return run_dir


def test_scan_flags_keys_tokens_and_ips():
    text = '{"private_key": "x", "t": "dop_v1_abc", "k": "0x' + "ab" * 32 + '", "ip": "203.0.113.9 46.4.1.1"}'
    problems = push.scan_text("f", text, {"203.0.113.9"})
    joined = " ".join(problems)
    for needle in ("private_key", "DigitalOcean", "32-byte", "VM IP 203.0.113.9", "public IP 46.4.1.1"):
        assert needle in joined
    assert push.scan_text("f", "ping 9.9.9.9 via 172.30.0.1 sha " + "ab" * 32, set()) == []


def test_prepare_copies_only_the_allowlist(tmp_path):
    run_dir = make_run(tmp_path)
    plan = push.prepare(run_dir, tmp_path / "repo")
    assert sorted(p.name for p in plan.files) == sorted(push.ALLOWLIST)
    assert plan.scan.clean, plan.scan.problems
    assert plan.destination.name == "2026-11-01-kaunas-v5-piz-palu-dev"


def test_vm_ip_in_result_is_refused(tmp_path):
    result = sample_result()
    result["run"]["notes"] = "client1 at 203.0.113.9"
    plan = push.prepare(make_run(tmp_path, result), tmp_path / "repo")
    assert not plan.scan.clean


def test_refuses_bench_repo_and_missing_checkout(tmp_path):
    with pytest.raises(RuntimeError, match="not a git checkout"):
        push.check_results_repo(tmp_path / "nothing")
    repo = tmp_path / "bench"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:SCBuergel/gnosis_vpn-bench.git"],
        check=True,
    )
    with pytest.raises(RuntimeError, match="bench repository"):
        push.check_results_repo(repo)


def test_copy_and_commit_into_local_repo(tmp_path):
    run_dir = make_run(tmp_path)
    repo = tmp_path / "results"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
    plan = push.prepare(run_dir, repo)
    assert push.copy_and_commit(plan, "add")
    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files"], capture_output=True, text=True
    ).stdout.split()
    assert sorted(tracked) == sorted(f"runs/{plan.destination.name}/{n}" for n in push.ALLOWLIST)
    assert not push.copy_and_commit(plan, "again")


def test_github_slug():
    assert push.github_slug("git@github.com:a/b.git") == "a/b"
    assert push.github_slug("https://github.com/a/b") == "a/b"
    assert push.github_slug("/srv/git/x") is None


def test_refuses_repo_with_staged_changes(tmp_path):
    run_dir = make_run(tmp_path)
    repo = tmp_path / "results"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "other.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "other.txt"], check=True)
    with pytest.raises(RuntimeError, match="staged"):
        push.copy_and_commit(push.prepare(run_dir, repo), "add")
