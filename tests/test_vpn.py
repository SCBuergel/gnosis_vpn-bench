from conftest import V4_DESTINATION, V5_DESTINATION, fail, ok, status_payload

from gvpn_bench.status import parse_destination
from gvpn_bench.vpn import VpnClient


def test_json_flag_detection_falls_back_to_legacy_flag(fake_host_factory):
    def respond(argv, _):
        if "-o" in argv:
            return fail(2, "error: unexpected argument '-o'")
        return ok(status_payload([V4_DESTINATION]))

    host = fake_host_factory(respond)
    client = VpnClient(host=host)
    assert [d.id for d in client.destinations()] == ["Serbia"]
    assert client.json_flag == ("--json",)
    assert host.calls[-1] == ["gnosis_vpn-ctl", "--json", "status"]


def test_v5_ctl_prefix_uses_runuser(fake_host_factory):
    host = fake_host_factory(lambda argv, _: ok(status_payload([V5_DESTINATION])))
    client = VpnClient(host=host, ctl_prefix=["runuser", "-u", "gvpnbench", "--"])
    client.status()
    assert host.calls[-1] == ["runuser", "-u", "gvpnbench", "--", "gnosis_vpn-ctl", "-o", "json", "status"]


def test_connect_requires_json_connect_response(fake_host_factory):
    responses = {"connect": ok({"Error": "DestinationNotFound"})}

    def respond(argv, _):
        return responses.get(argv[-2], ok(status_payload([V4_DESTINATION])))

    client = VpnClient(host=fake_host_factory(respond), json_flag=("-o", "json"))
    destination = parse_destination(V4_DESTINATION)
    assert client.connect(destination) is False
    responses["connect"] = ok({"Connect": {"Connecting": V4_DESTINATION}})
    assert client.connect(destination) is True


def test_dns_auto_leaves_working_resolver_alone(fake_host_factory):
    host = fake_host_factory(lambda argv, _: ok("1.2.3.4 STREAM speed.cloudflare.com\n"))
    client = VpnClient(host=host, dns_mode="auto")
    client.ensure_dns()
    assert all(call[0] != "sudo" for call in host.calls)
    client.restore_dns()
    assert len(host.calls) == 1


def test_dns_fix_is_restored(fake_host_factory):
    def respond(argv, _):
        if argv[0] == "getent":
            return fail(2, "")
        if argv == ["cat", "/etc/resolv.conf"]:
            return ok("nameserver 127.0.0.53\n")
        return ok()

    host = fake_host_factory(respond)
    client = VpnClient(host=host, dns_mode="auto")
    client.ensure_dns()
    assert host.calls[-1] == ["sudo", "tee", "/etc/resolv.conf"]
    assert host.inputs[-1] == b"nameserver 1.1.1.1\n"
    client.restore_dns()
    assert host.inputs[-1] == b"nameserver 127.0.0.53\n"
    calls = len(host.calls)
    client.restore_dns()
    assert len(host.calls) == calls


def test_dns_never(fake_host_factory):
    host = fake_host_factory(lambda argv, _: fail())
    VpnClient(host=host, dns_mode="never").ensure_dns()
    assert host.calls == []
