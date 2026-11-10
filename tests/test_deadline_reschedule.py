"""Replacing an armed billing cap must never remove the only working timer."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from gvpn_bench.remote import deadline
from gvpn_bench.runs import write_private_json

NOW = datetime(2026, 10, 31, 21, 0, tzinfo=UTC)


@pytest.fixture
def run(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    return SimpleNamespace(path=tmp_path, state=state, config=SimpleNamespace(id="deadline-test"))


def armed(run):
    record = {"unit": deadline.unit_name(run), "seconds": 21600, "set_at": "2026-10-31T20:14:37Z"}
    write_private_json(run.state / "deadline-timer.json", record)
    return record


def test_new_timer_is_verified_before_old_timer_is_stopped(run, monkeypatch):
    old = armed(run)
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        if args[0] == "systemd-run":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        active = args[2] == "is-active" and args[-1] != old["unit"] + ".timer"
        return SimpleNamespace(
            returncode=0 if active or args[2] == "stop" else 3, stdout="active\n" if active else "inactive\n", stderr=""
        )

    monkeypatch.setattr(deadline.subprocess, "run", command)
    unit = deadline.reschedule(run, 18, now=NOW)
    assert "--on-calendar=2026-11-01 14:14:37 UTC" in calls[0]
    assert [c[2] for c in calls[1:]] == ["is-active", "stop", "is-active"]
    record = json.loads((run.state / "deadline-timer.json").read_text())
    assert record["unit"] == unit and record["seconds"] == 64800
    assert record["set_at"] == old["set_at"]
    deadline.cancel(run)
    assert calls[-1] == ["systemctl", "--user", "stop", unit + ".timer"]


@pytest.mark.parametrize("failure", ["create", "verify"])
def test_failed_replacement_retains_original_timer(run, monkeypatch, failure):
    old = armed(run)
    calls = []

    def command(args, **kwargs):
        calls.append(args)
        failed = failure == "create" or args[0] == "systemctl"
        return SimpleNamespace(returncode=1 if failed else 0, stdout="inactive", stderr="fake failure")

    monkeypatch.setattr(deadline.subprocess, "run", command)
    with pytest.raises(RuntimeError):
        deadline.reschedule(run, 18, now=NOW)
    assert not any(c[0] == "systemctl" and c[2] == "stop" for c in calls)
    assert json.loads((run.state / "deadline-timer.json").read_text()) == old


def test_expired_deadline_is_rejected_without_touching_timer(run, monkeypatch):
    armed(run)
    monkeypatch.setattr(deadline.subprocess, "run", lambda *a, **k: pytest.fail("must not touch systemd"))
    with pytest.raises(ValueError):
        deadline.reschedule(run, 0.1, now=NOW)


def test_cancel_cannot_stop_a_foreign_unit(run, monkeypatch):
    write_private_json(run.state / "deadline-timer.json", {"unit": "another-important-service"})
    monkeypatch.setattr(deadline.subprocess, "run", lambda *a, **k: pytest.fail("must not touch systemd"))
    with pytest.raises(RuntimeError, match="another run"):
        deadline.cancel(run)
