import shlex
from pathlib import Path

from gvpn_bench.host import LocalHost, SshHost, run_script, write_file


def test_local_host_runs_argv_and_reports_missing_tool():
    host = LocalHost()
    assert host.run(["printf", "%s", "a b"]).stdout == "a b"
    assert host.run(["definitely-not-a-command-xyz"]).returncode == -127
    assert host.run(["sleep", "5"], timeout_s=0.2).timed_out


def test_ssh_argv_quotes_remote_command():
    host = SshHost(
        name="client1",
        address="192.0.2.10",
        identity_file=Path("/k/id"),
        known_hosts_file=Path("/k/known"),
    )
    argv = host.ssh_argv(["gnosis_vpn-ctl", "connect", "South Korea"])
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert "root@192.0.2.10" in argv
    assert shlex.split(argv[-1]) == ["gnosis_vpn-ctl", "connect", "South Korea"]
    assert host.privileged(["tee", "x"]) == ["tee", "x"]
    assert SshHost(name="h", address="h", user="bob").privileged(["tee"]) == ["sudo", "-n", "tee"]


def test_run_script_passes_values_as_arguments_not_text():
    host = LocalHost()
    result = run_script(host, 'printf "%s|%s" "$1" "$2"\n', ["it's", "$(id)"])
    assert result.stdout == "it's|$(id)"


def test_write_file_is_private(tmp_path):
    target = tmp_path / "secret.json"
    write_file(LocalHost(), str(target), b"{}")
    assert target.read_bytes() == b"{}"
    assert target.stat().st_mode & 0o777 == 0o600
