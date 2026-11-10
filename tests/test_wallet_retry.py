"""RPC rate limits: reads are retried with backoff, sends are never retried."""

import pytest

from gvpn_bench.finance import wallet

web3 = pytest.importorskip("web3")
requests = pytest.importorskip("requests")


class RateLimitedSession:
    """Answers HTTP 429 a given number of times, then a JSON-RPC result."""

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def make_post_request(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise requests.HTTPError("429 Client Error: Too Many Requests")
        return b'{"jsonrpc":"2.0","id":0,"result":"0x64"}'


def provider(monkeypatch, failures: int):
    monkeypatch.setattr(wallet, "RPC_BACKOFF_S", 0.0)
    rpc = web3.Web3.HTTPProvider("http://127.0.0.1:9", exception_retry_configuration=wallet.read_retry_configuration())
    session = RateLimitedSession(failures)
    monkeypatch.setattr(rpc, "_request_session_manager", session)
    return rpc, session


def test_reads_are_retried_after_rate_limiting(monkeypatch):
    rpc, session = provider(monkeypatch, failures=3)
    assert rpc._make_request("eth_call", b"{}") is not None
    assert session.calls == 4


def test_sends_are_never_retried(monkeypatch):
    rpc, session = provider(monkeypatch, failures=1)
    with pytest.raises(requests.HTTPError):
        rpc._make_request("eth_sendRawTransaction", b"{}")
    assert session.calls == 1


def test_retry_allowlist_excludes_sending_and_signing():
    allowed = wallet.read_retry_configuration().method_allowlist
    assert "eth_call" in allowed and "eth_getLogs" in allowed
    assert not set(wallet.NOT_RETRIED) & set(allowed)
