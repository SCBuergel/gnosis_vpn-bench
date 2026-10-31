"""Tests for the faster-run changes: early give-up on refused registration,
connected-only rungs, one reconnect before a rung, the pre-flight indexer
check and the 90 s default deadline. All offline."""

import time
from unittest.mock import MagicMock

import pytest
from conftest import V4_DESTINATION, FakeHost, ok, status_payload
from test_review_fixes import agent_result
from test_runs import user_config

from gvpn_bench.remote import connect, ladder, preflight, steps
from gvpn_bench.runs import LadderSpec, plan_run

SERBIA = "0x4203490fc1747fa6d0ec13c1589cca9d308724ba"
REFUSAL = (
    "{t} ERROR gnosis_vpn_lib::core: connection failed err=GvpnClient(Request(reqwest::Error "
    '{{ kind: Status(404, None), url: "http://127.0.0.1:36513/api/v1/clients/register" }}))'
)


@pytest.fixture
def run(tmp_path):
    return plan_run(user_config(tmp_path), "v4-jura-prod", "serbia-jura-prod", run_id="fast")


# -- registration refusals ----------------------------------------------------------------------


def test_register_refusals_counts_only_timestamped_lines_since_the_attempt():
    log = "\n".join(
        [
            REFUSAL.format(t="2026-10-31T14:00:00.000000Z"),  # before the attempt
            REFUSAL.format(t="2026-10-31T14:05:00.000000Z"),
            "clients/register 404 without a timestamp",
            "2026-10-31T14:05:10.000000Z INFO connected",
            REFUSAL.format(t="2026-10-31T14:05:12.500000Z"),
        ]
    )
    found = connect.register_refusals(log, "2026-10-31T14:01:00.000000Z")
    assert len(found) == 2


def test_slots_from_probe():
    probe = {"load": {"slots": {"total": 2, "available": 0, "connected": 2}}}
    assert connect.free_slots(probe) == 0 and connect.total_slots(probe) == 2
    assert connect.free_slots(None) is None and connect.free_slots({"load": {}}) is None


def refusing_host(refusals, probe=None):
    """A worker that never connects; its log holds *refusals* register 404s."""

    def respond(argv, _):
        if "gnosis_vpn-ctl" in argv:
            command = argv[argv.index("json") + 1]
            if command == "connect":
                return ok({"Connect": {"Connecting": V4_DESTINATION}})
            if command == "status":
                return ok(status_payload([V4_DESTINATION], probe=probe))
            return ok({"StartClient": "Started"})
        if argv[0] == "bash":
            return ok("\n".join(REFUSAL.format(t=f"2999-01-01T00:00:0{i}.000000Z") for i in range(refusals)))
        return ok()

    return FakeHost(respond, name="client3")


def try_connect(host, timeout_s):
    profile = MagicMock()
    profile.worker.ctl_prefix.return_value = []
    profile.worker.worker_log_glob.return_value = "/var/log/x"
    return connect.connect_to(host, "203.0.113.5", profile, SERBIA, route_checks=["192.0.2.1"], timeout_s=timeout_s)


@pytest.fixture
def fast_clock(monkeypatch):
    monkeypatch.setattr(connect, "LOG_CHECK_INTERVAL_S", 0.0)
    monkeypatch.setattr(connect.time, "sleep", lambda s: None)


def test_two_refusals_end_the_attempt_early(fast_clock):
    started = time.monotonic()
    outcome = try_connect(refusing_host(2), timeout_s=60)
    assert outcome.status == "http_404_register"
    assert "refused registration 2x" in outcome.detail
    assert time.monotonic() - started < 10


def test_one_refusal_with_no_free_slot_ends_the_attempt(fast_clock):
    probe = {"load": {"slots": {"total": 2, "available": 0, "connected": 2}}}
    assert try_connect(refusing_host(1, probe), timeout_s=60).status == "http_404_register"


def test_one_refusal_with_free_slots_keeps_waiting(fast_clock):
    probe = {"load": {"slots": {"total": 2, "available": 1, "connected": 1}}}
    started = time.monotonic()
    outcome = try_connect(refusing_host(1, probe), timeout_s=0.3)
    assert time.monotonic() - started >= 0.3  # waited out the timeout
    assert "refused registration 1x" in outcome.detail


# -- ladder: connected clients only, one reconnect -------------------------------------------------


def fake_ladder(monkeypatch, connected):
    base = time.time() + 100
    monkeypatch.setattr(ladder, "still_connected", lambda host, profile, target: connected(host))
    monkeypatch.setattr(ladder, "launch", lambda host, **kwargs: None)
    monkeypatch.setattr(
        ladder,
        "collect",
        lambda host, attempt, rung, give_up_at: agent_result(base + rung * 400, base + rung * 400 + 50, 1_000_000),
    )
    monkeypatch.setattr(ladder, "fetch_pings", lambda host, attempt, rung: None)


def hosts(*names):
    return [FakeHost(lambda a, d: ok(), name=n) for n in names]


def test_rung_beyond_connected_clients_fails_with_their_status(run, monkeypatch):
    fake_ladder(monkeypatch, lambda host: True)
    rungs = ladder.run_ladder(
        run,
        run.config.profile(),
        run.config.target(),
        hosts("client1", "client2"),
        "192.0.2.1",
        "aa",
        unavailable={"client3": "http_404_register"},
    )
    assert [r["status"] for r in rungs] == ["measured", "measured", "failed"]
    assert rungs[2]["failure_status"] == "http_404_register"
    assert "only 2 client(s)" in rungs[2]["detail"] and "client3" in rungs[2]["detail"]


def test_no_connected_client_fails_rung_one_and_skips_the_rest(run, monkeypatch):
    fake_ladder(monkeypatch, lambda host: pytest.fail("nobody to check"))
    rungs = ladder.run_ladder(
        run, run.config.profile(), run.config.target(), [], "192.0.2.1", "aa", unavailable={"client1": "timeout"}
    )
    assert [(r["status"], r["failure_status"]) for r in rungs] == [
        ("failed", "timeout"),
        ("not_run", "not_run"),
        ("not_run", "not_run"),
    ]


def test_dropped_client_is_reconnected_once_and_measured(run, monkeypatch):
    state = {"client1": False}
    fake_ladder(monkeypatch, lambda host: state.get(host.name, True))
    calls = []

    def reconnect(host):
        calls.append(host.name)
        state[host.name] = True
        return True

    rungs = ladder.run_ladder(
        run, run.config.profile(), run.config.target(), hosts("client1", "client2", "client3"), "192.0.2.1", "aa",
        reconnect=reconnect,
    )  # fmt: skip
    assert calls == ["client1"]
    assert [r["status"] for r in rungs] == ["measured", "measured", "measured"]
    assert rungs[0]["detail"] == "reconnected before this rung: client1"
    assert rungs[1]["detail"] == ""


def test_failed_reconnect_fails_the_rung(run, monkeypatch):
    fake_ladder(monkeypatch, lambda host: host.name != "client1")
    monkeypatch.setattr(ladder, "launch", lambda host, **kwargs: pytest.fail("must not launch"))
    rungs = ladder.run_ladder(
        run, run.config.profile(), run.config.target(), hosts("client1", "client2", "client3"), "192.0.2.1", "aa",
        reconnect=lambda host: False,
    )  # fmt: skip
    assert [r["status"] for r in rungs] == ["failed", "not_run", "not_run"]
    assert "one reconnect attempt failed" in rungs[0]["detail"]


def test_step_ladder_passes_only_connected_clients(run, monkeypatch):
    from gvpn_bench.remote.inventory import Machine
    from gvpn_bench.runs import write_private_json

    machines = [Machine(f"client{i}", "client", i, "c-4", "fra1", "now", f"192.0.2.{i}") for i in (1, 2, 3)]
    pairs = [(m, type("H", (), {"name": m.name})()) for m in machines]
    origin = Machine("origin", "origin", 9, "c-2", "fra1", "now", "192.0.2.9")
    write_private_json(run.state / "origin.json", {"bytes": 1, "sha256": "aa"})
    write_private_json(
        run.state / f"connect-{run.config.target_name}.json",
        [
            {"client": "client1", "status": "connected"},
            {"client": "client2", "status": "connected"},
            {"client": "client3", "status": "http_404_register"},
        ],
    )
    monkeypatch.setattr(steps, "client_hosts", lambda run: pairs)
    monkeypatch.setattr(steps, "machine", lambda run, name: origin)
    seen = {}

    def fake_run_ladder(run, profile, target, client_hosts, origin_ip, sha, data_dir, *, unavailable, reconnect):
        seen.update(names=[h.name for h in client_hosts], unavailable=unavailable, reconnect=reconnect)
        return []

    monkeypatch.setattr(steps, "run_ladder", fake_run_ladder)
    steps.step_ladder(run, run.config.profile(), run.config.target())
    assert seen["names"] == ["client1", "client2"]
    assert seen["unavailable"] == {"client3": "http_404_register"}
    assert callable(seen["reconnect"])


# -- pre-flight ----------------------------------------------------------------------------------


def indexer(accounts, services=None, union=True):
    """A fake GraphQL endpoint: *accounts* maps lower-case address → multiaddresses."""
    asked = []

    def query(url, text):
        asked.append(text)
        if text.startswith("{accounts("):
            address = text.split('"')[1]
            found = [{"chainKey": address, "multiAddresses": accounts[address]}] if address in accounts else []
            return {"data": {"accounts": {"__typename": "AccountsList", "accounts": found}}}
        if "ServicesList" in text and not union:
            return {"errors": [{"message": 'Unknown type "ServicesList"'}]}
        rows = [{"node": node, "serviceType": "gvpn:exit"} for node in services or []]
        return {"data": {"services": {"services": rows}}}

    query.asked = asked
    return query


def v5(tmp_path):
    return plan_run(user_config(tmp_path), "v5-piz-palu-staging", "uk-piz-palu-staging", run_id="pf")


UK = "0x1d8ef6206b653ee03a640d1cceefc7105199d833"


def test_preflight_passes_a_registered_v4_exit_without_asking_for_services(run):
    query = indexer({SERBIA: ["/ip4/192.0.2.1/udp/9091/quic-v1"]})
    assert preflight.check(run.config.profile(), run.config.targets(), query) == []
    assert not any("services" in q for q in query.asked)


def test_preflight_rejects_unknown_and_unannounced_nodes(run):
    profile = run.config.profile()
    assert "not a registered node" in preflight.check(profile, run.config.targets(), indexer({}))[0]
    assert "announces no address" in preflight.check(profile, run.config.targets(), indexer({SERBIA: []}))[0]


def test_preflight_needs_a_gvpn_exit_service_for_v5(tmp_path):
    run = v5(tmp_path)
    profile, targets = run.config.profile(), run.config.targets()
    registered = {UK: ["/ip4/192.0.2.1/udp/9091/quic-v1"]}
    assert "gvpn:exit" in preflight.check(profile, targets, indexer(registered, services=[]))[0]
    assert preflight.check(profile, targets, indexer(registered, services=[UK])) == []
    assert preflight.check(profile, targets, indexer(registered, services=[UK], union=False)) == []
    assert preflight.check(profile, targets, indexer(registered, services=[]), run_client_config=True) == []


def test_preflight_reports_an_unreachable_indexer(run):
    def down(url, text):
        raise OSError("connection refused")

    problems = preflight.check(run.config.profile(), run.config.targets(), down)
    assert problems and "unusable" in problems[0]


def test_provision_stops_before_any_droplet_when_preflight_fails(run, monkeypatch):
    monkeypatch.setattr(preflight, "graphql", indexer({}))
    monkeypatch.setattr(steps, "provision", lambda *a: pytest.fail("must not provision"))
    monkeypatch.setattr(steps, "api_for", lambda user: None)
    with pytest.raises(RuntimeError, match="no VM created"):
        steps.step_provision(None, run, run.config.profile())


def test_resumed_provision_skips_preflight(run, monkeypatch):
    from gvpn_bench.remote.inventory import Machine, save_inventory

    save_inventory(run, [Machine("origin", "origin", 1, "c-2", "fra1", "now", "192.0.2.9")])
    monkeypatch.setattr(steps, "step_preflight", lambda *a: pytest.fail("must not re-check"))
    done = []
    monkeypatch.setattr(steps, "provision", lambda *a: done.append(True))
    monkeypatch.setattr(steps, "api_for", lambda user: None)
    steps.step_provision(None, run, run.config.profile())
    assert done == [True]


def test_default_deadline_is_90_seconds():
    assert LadderSpec().deadline_s == 90


# -- client config upload ------------------------------------------------------------------------


def test_config_upload_restarts_then_starts_the_worker_again(tmp_path, monkeypatch):
    from gvpn_bench.host import CommandResult
    from gvpn_bench.remote import provision

    run = plan_run(user_config(tmp_path), "v4-jura-prod", "lisbon-jura-prod", run_id="cfg")
    order = []
    starts = iter([{"StartClient": "ServiceNotReady"}, {"StartClient": "Started"}])

    def respond(argv, _):
        if argv[:2] == ["systemctl", "restart"]:
            order.append("restart")
        elif "gnosis_vpn-ctl" in argv:
            order.append("start-client")
            return ok(next(starts))
        return ok()

    monkeypatch.setattr(provision, "upload_tools", lambda host: None)
    monkeypatch.setattr(provision, "run_script", lambda *a, **k: CommandResult(0, "worker started", ""))
    monkeypatch.setattr(provision, "write_file", lambda *a, **k: order.append("upload"))
    monkeypatch.setattr(provision.time, "sleep", lambda s: None)
    provision.prepare_client(run, run.config.profile(), FakeHost(respond, name="client1"))
    assert order == ["upload", "restart", "start-client", "start-client"]


def test_worker_that_never_starts_after_upload_fails_provisioning(monkeypatch):
    from gvpn_bench.remote import provision

    monkeypatch.setattr(provision.time, "sleep", lambda s: None)
    profile = MagicMock()
    profile.worker.ctl_prefix.return_value = []
    host = FakeHost(lambda a, d: ok({"StartClient": "ServiceNotReady"}), name="client1")
    with pytest.raises(RuntimeError, match="did not start"):
        provision.start_worker(host, profile, attempts=3)
