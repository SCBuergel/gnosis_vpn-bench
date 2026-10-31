"""results push: allowlist, scan and refusal rules. Uses a local bare repo only."""

import json
import subprocess

import pytest
from sample_result import sample_result

from gvpn_bench.results import publish, push
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


def git_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for key, value in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)
    return path


def test_scan_flags_keys_tokens_and_ips():
    text = '{"private_key": "x", "t": "dop_v1_abc", "k": "0x' + "ab" * 32 + '", "ip": "203.0.113.9 46.4.1.1"}'
    problems = push.scan_text("f", text, {"203.0.113.9"})
    joined = " ".join(problems)
    for needle in ("private_key", "DigitalOcean", "32-byte", "VM IP 203.0.113.9", "public IP 46.4.1.1"):
        assert needle in joined
    assert push.scan_text("f", "ping 9.9.9.9 via 172.30.0.1 sha " + "ab" * 32, set()) == []


def test_recording_folder_holds_only_generated_allowlisted_files(tmp_path):
    staged = publish.stage_recording(make_run(tmp_path))
    assert staged.folder == "runs/2026-11-01-123000Z-kaunas-v5-piz-palu-dev"
    assert {"result.json", "provenance.json", "report.html", "README.md", "charts/overview.svg"} <= set(staged.files)
    assert all(publish.ALLOWED.fullmatch(path) for path in staged.files)
    assert not any("id_ed25519" in path or path.endswith(".log") for path in staged.files)
    assert publish.scan([staged]) == []


def test_vm_ip_anywhere_in_staged_files_is_refused(tmp_path):
    result = sample_result()
    result["run"]["notes"] = "client1 at 203.0.113.9"
    staged = publish.stage_recording(make_run(tmp_path, result))
    problems = publish.scan([staged])
    assert any("VM IP 203.0.113.9" in p and "README.md" in p for p in problems)


def test_unknown_file_name_is_refused():
    staged = publish.Staged("runs/x", {"state/inventory.json": "{}"})
    assert any("not an allowed file name" in p for p in publish.scan([staged]))


def test_refuses_bench_repo_and_missing_checkout(tmp_path):
    with pytest.raises(RuntimeError, match="not a git checkout"):
        push.check_results_repo(tmp_path / "nothing")
    repo = git_repo(tmp_path / "bench")
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:SCBuergel/gnosis_vpn-bench.git"],
        check=True,
    )
    with pytest.raises(RuntimeError, match="bench repository"):
        push.check_results_repo(repo)


def test_publish_writes_recording_report_and_index(tmp_path):
    repo = git_repo(tmp_path / "results")
    recording = publish.stage_recording(make_run(tmp_path))
    from gvpn_bench.results.collection import Recording

    member = Recording(recording.folder.split("/")[1], json.loads(recording.files["result.json"]))
    report = publish.stage_report([member], links={"Claude app": "https://claude.ai/artifact/x"})
    staged = [recording, report, publish.index_staged(repo, [recording, report])]
    assert push.commit(repo, publish.write(repo, staged), "add")
    tracked = set(subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True).stdout.split())
    assert "README.md" in tracked
    assert f"{recording.folder}/README.md" in tracked and f"{report.folder}/report.json" in tracked
    index = (repo / "README.md").read_text()
    assert recording.folder in index and "Claude app" in index and "42.5 / n/a" in index
    assert not push.commit(repo, publish.write(repo, staged), "again")
    rebuilt = publish.rebuild_all(repo)
    assert {s.folder for s in rebuilt} == {recording.folder, report.folder}


def test_refuses_repo_with_staged_changes(tmp_path):
    repo = git_repo(tmp_path / "results")
    (repo / "other.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "other.txt"], check=True)
    with pytest.raises(RuntimeError, match="staged"):
        push.commit(repo, ["other.txt"], "add")


def test_vm_ips_found_from_a_target_result_dir(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "state").mkdir(parents=True)
    (run_dir / "state" / "inventory.json").write_text(json.dumps([{"name": "c", "ip": "203.0.113.9"}]))
    target_dir = run_dir / "results" / "austria-jura-prod"
    target_dir.mkdir(parents=True)
    assert push.vm_ips_of(target_dir) == {"203.0.113.9"}
    result = sample_result()
    result["run"]["notes"] = "via 203.0.113.9"
    write_result(target_dir, result)
    assert publish.scan([publish.stage_recording(target_dir)])


def test_github_slug():
    assert push.github_slug("git@github.com:a/b.git") == "a/b"
    assert push.github_slug("https://github.com/a/b") == "a/b"
    assert push.github_slug("/srv/git/x") is None


def test_same_day_runs_get_separate_folders():
    first = sample_result()
    second = sample_result()
    second["run"]["started_at"] = "2026-11-01T15:04:05Z"
    assert push.destination_name(first) != push.destination_name(second)
    assert push.destination_name(second) == "2026-11-01-150405Z-kaunas-v5-piz-palu-dev"


def test_scan_sees_through_markdown_emphasis():
    assert push.scan_text("README.md", "_note: client at 203.0.113.9_", {"203.0.113.9"})
    assert push.scan_text("README.md", "_key 0x" + "ab" * 32 + "_", set())
    assert push.scan_text("README.md", "**46.4.1.1**", set())


def test_cli_push_two_locations_then_combine_and_link(tmp_path, monkeypatch, capsys):
    import argparse

    from gvpn_bench import runcli

    repo = git_repo(tmp_path / "results")
    monkeypatch.setattr(push, "check_results_repo", lambda repo: "origin")
    monkeypatch.setattr(push, "push", lambda repo: None)
    first = tmp_path / "nl"
    second = tmp_path / "at"
    first.mkdir()
    second.mkdir()
    write_result(first, sample_result())
    other = sample_result()
    other["run"]["started_at"] = "2026-11-01T13:00:00Z"
    other["provenance"]["target"].update(
        {"name": "austria-jura-prod", "label": "Austria", "exit_address": "0x210a14012535321b919653fdee8c2d5971b0e534"}
    )
    write_result(second, other)

    def ns(**kw):
        base = {"repo": str(repo), "dry_run": False, "yes": True, "link": None, "title": None}
        return argparse.Namespace(**{**base, **kw})

    assert runcli.cmd_results_push(ns(run=[str(first), str(second)], report_name=None)) == 0
    reports = list((repo / "reports").iterdir())
    assert len(reports) == 1
    meta = json.loads((reports[0] / "report.json").read_text())
    assert len(meta["recordings"]) == 2
    assert "Austria" in (reports[0] / "README.md").read_text()

    names = meta["recordings"]
    assert runcli.cmd_results_combine(ns(recordings=names, name="both-again", title="Both")) == 0
    assert runcli.cmd_results_link(ns(report="both-again", label="Claude app", url="https://claude.ai/artifact/x")) == 0
    assert "Claude app" in (repo / "README.md").read_text()
    assert runcli.cmd_results_combine(ns(recordings=["missing"], name=None)) == 2
    assert runcli.cmd_results_rebuild(ns()) == 0
    capsys.readouterr()


def test_commit_records_charts_dropped_by_a_rebuild(tmp_path):
    repo = git_repo(tmp_path / "results")
    first = publish.Staged("reports/r", {"README.md": "x", "charts/a.svg": "<svg/>", "charts/b.svg": "<svg/>"})
    assert push.commit(repo, publish.write(repo, [first]), "one")
    second = publish.Staged("reports/r", {"README.md": "y", "charts/a.svg": "<svg/>"})
    assert push.commit(repo, publish.write(repo, [second]), "two")
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True, text=True).stdout.split()
    assert "reports/r/charts/b.svg" not in tracked
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    assert status.stdout == ""


@pytest.mark.parametrize("name", ["..", ".", "///", "", "-"])
def test_report_names_cannot_escape_reports(name):
    with pytest.raises(ValueError):
        publish.folder_name(name)
    assert publish.folder_name("a/../b") == "a-..-b"


def test_link_validates_name_and_url(tmp_path, capsys):
    import argparse

    from gvpn_bench import runcli

    def ns(report, url):
        return argparse.Namespace(repo=str(tmp_path), report=report, label="x", url=url, dry_run=True, yes=False)

    assert runcli.cmd_results_link(ns("..", "https://a")) == 2
    assert runcli.cmd_results_link(ns("r", "http://a")) == 2
    capsys.readouterr()
