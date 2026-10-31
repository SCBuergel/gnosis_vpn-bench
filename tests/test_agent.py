"""The VM-side ladder agent against a localhost HTTP server (no ICMP, no VPN)."""

import contextlib
import hashlib
import importlib.util
import io
import json
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

AGENT = Path(__file__).resolve().parent.parent / "gvpn_bench" / "assets" / "ladder_agent.py"
PAYLOAD = b"local measurement test" * 1024


def load_agent():
    spec = importlib.util.spec_from_file_location("ladder_agent", AGENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        try:
            if self.path == "/stall":
                time.sleep(1)
            self.wfile.write(PAYLOAD)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def run_agent(server, tmp_path, path, deadline_s, monkeypatch):
    agent = load_agent()
    monkeypatch.setattr(agent, "ping_once", lambda target, at: {"sent_at": at, "rtt_ms": 1.0})
    config = {
        "output": str(tmp_path / "result"),
        "start_at": time.time() + 0.05,
        "deadline_s": deadline_s,
        "bytes": len(PAYLOAD),
        "url": f"http://127.0.0.1:{server.server_port}{path}",
        "ping_target": "192.0.2.1",
    }
    with contextlib.redirect_stdout(io.StringIO()):
        agent.download(config)
    out = Path(config["output"])
    assert (out / "pings.json").exists()
    return json.loads((out / "result.json").read_text())


def test_complete_counts_payload_and_hash(server, tmp_path, monkeypatch):
    result = run_agent(server, tmp_path, "/file", 2, monkeypatch)
    assert result["complete"]
    assert result["bytes"] == len(PAYLOAD)
    assert result["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert result["events"][-1][1] == len(PAYLOAD)


def test_stall_obeys_shared_deadline(server, tmp_path, monkeypatch):
    result = run_agent(server, tmp_path, "/stall", 0.2, monkeypatch)
    assert result["timed_out"]
    assert not result["complete"]
    assert result["bytes"] == 0
    assert result["end"] <= result["scheduled_start"] + 0.2 + 1e-6


def test_missed_barrier_is_an_error(tmp_path):
    agent = load_agent()
    with pytest.raises(RuntimeError, match="missed"):
        agent.wait_for(agent.Clock(), time.time() - 5)
