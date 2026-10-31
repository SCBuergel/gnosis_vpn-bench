from pathlib import Path

import pytest

from gvpn_bench.runs import (
    UserConfig,
    check_private_file,
    open_run,
    parse_run_toml,
    plan_run,
    render_run_toml,
)


def user_config(tmp_path: Path) -> UserConfig:
    return UserConfig(
        runs_dir=tmp_path / "runs",
        identity_backup_dir=tmp_path / "ids",
        digitalocean_token_file=tmp_path / "token",
        funding_key_file=tmp_path / "key",
        results_repo=None,
        source=None,
    )


def test_plan_writes_run_toml_and_private_state(tmp_path):
    user = user_config(tmp_path)
    run = plan_run(user, "v4-jura-prod", "serbia-jura-prod", run_id="r1")
    assert (run.path / "run.toml").exists()
    assert run.state.stat().st_mode & 0o777 == 0o700
    reopened = open_run("r1", user)
    assert reopened.config.target_name == "serbia-jura-prod"
    assert reopened.config.ladder.transfer_bytes == 200_000_000
    assert reopened.config.ladder.rungs == (1, 2, 3)
    assert reopened.done("plan")


def test_plan_refuses_network_mismatch(tmp_path):
    with pytest.raises(ValueError, match="jura-prod"):
        plan_run(user_config(tmp_path), "v5-piz-palu-dev", "serbia-jura-prod", run_id="r2")


def test_plan_refuses_existing_run(tmp_path):
    user = user_config(tmp_path)
    plan_run(user, "v5-piz-palu-dev", "kaunas", run_id="r3")
    with pytest.raises(FileExistsError):
        plan_run(user, "v5-piz-palu-dev", "kaunas", run_id="r3")


def test_run_toml_round_trip(tmp_path):
    run = plan_run(user_config(tmp_path), "v5-piz-palu-dev", "kaunas", run_id="r4", notes='quote " here')
    assert parse_run_toml(render_run_toml(run.config)) == run.config


def test_rung_larger_than_client_count_rejected(tmp_path):
    run = plan_run(user_config(tmp_path), "v5-piz-palu-dev", "kaunas", run_id="r5")
    text = render_run_toml(run.config).replace("clients = 3", "clients = 2")
    with pytest.raises(ValueError):
        parse_run_toml(text)


def test_private_file_check(tmp_path):
    secret = tmp_path / "s"
    secret.write_text("x")
    secret.chmod(0o644)
    assert check_private_file(secret)
    secret.chmod(0o600)
    assert check_private_file(secret) == []
    assert check_private_file(tmp_path / "missing")
