"""
Cloudflare anycast speed measurements: download, upload, latency probe.
"""

import logging
import os
import subprocess
import tempfile
import threading

from config import (
    CF_DL_URL_FMT, CF_DL_10MB, CF_UPLOAD, CF_PROBE,
    CURL_MAX_TIME_S, CURL_UPLOAD_SIZE_BYTES, LATENCY_TIMEOUT_S,
)
from vpn import run_cmd

log = logging.getLogger("gnosis_speedtest")


def _bytes_to_mbit(bps: float) -> float:
    return bps * 8 / 1_000_000


def _run_curl_streaming(cmd: list[str], direction: str) -> float | None:
    log.info("curl %s: %s", direction, " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError:
        log.error("curl not found")
        return None

    def _read_stderr() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line.strip():
                log.info("  curl [%s] %s", direction, line)

    t = threading.Thread(target=_read_stderr, daemon=True)
    t.start()
    try:
        assert proc.stdout is not None
        stdout_bytes = proc.stdout.read()
    except Exception as exc:
        log.warning("curl %s stdout read error: %s", direction, exc)
        stdout_bytes = b""
    t.join(timeout=CURL_MAX_TIME_S + 30)
    if t.is_alive():
        proc.kill()
    proc.wait()
    rc = proc.returncode
    if rc not in (0, 28):
        log.error("curl %s failed rc=%d", direction, rc)
        return None
    try:
        speed_mbit = round(_bytes_to_mbit(float(stdout_bytes.decode().strip())), 2)
        log.info("curl %s FINAL: %.2f Mbit/s", direction, speed_mbit)
        return speed_mbit
    except ValueError:
        log.error("Cannot parse curl %s speed: %r", direction, stdout_bytes[:80])
        return None


def probe_latency_and_colo() -> tuple[float | None, str | None]:
    hdr_fd, hdr_path = tempfile.mkstemp(suffix=".hdr")
    os.close(hdr_fd)
    try:
        rc, stdout, stderr = run_cmd(
            ["curl", "-s", "-k", "-D", hdr_path, "-o", "/dev/null",
             "--max-time", str(LATENCY_TIMEOUT_S),
             "--write-out", "%{time_starttransfer}", CF_PROBE],
            timeout_s=LATENCY_TIMEOUT_S + 5,
        )
        latency_ms: float | None = None
        colo: str | None = None
        if rc in (0, 28):
            try:
                latency_ms = round(float(stdout.strip()) * 1000, 1)
            except ValueError:
                pass
            try:
                with open(hdr_path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if line.lower().startswith("colo:"):
                            colo = line.split(":", 1)[1].strip()
                            break
            except OSError:
                pass
        else:
            log.warning("Probe failed rc=%d", rc)
        log.info("Latency: %s ms  CF PoP: %s", latency_ms, colo)
        return latency_ms, colo
    finally:
        try:
            os.unlink(hdr_path)
        except OSError:
            pass


def cf_download_url(size_bytes: int) -> str:
    """Return the Cloudflare download URL for an arbitrary byte count."""
    return CF_DL_URL_FMT.format(size_bytes)


def run_cf_download(url: str, label: str, max_time_s: int = CURL_MAX_TIME_S) -> float | None:
    log.info("DOWNLOAD [%s]  →  %s  (max %ds)", label, url, max_time_s)
    return _run_curl_streaming(
        ["curl", "-k", "--output", "/dev/null",
         "--max-time", str(max_time_s),
         "--write-out", "%{speed_download}", url],
        direction=f"download/{label}",
    )


def run_cf_upload(upload_file: str) -> float | None:
    log.info("UPLOAD  →  %s  (max %ds)", CF_UPLOAD, CURL_MAX_TIME_S)
    return _run_curl_streaming(
        ["curl", "-k", "--request", "POST",
         "--data-binary", f"@{upload_file}",
         "--max-time", str(CURL_MAX_TIME_S),
         "--write-out", "%{speed_upload}",
         "--output", "/dev/null", CF_UPLOAD],
        direction="upload",
    )


def create_upload_file() -> str:
    log.info("Creating %.0f MB upload file…", CURL_UPLOAD_SIZE_BYTES / 1024 / 1024)
    chunk = b"\x00" * (1024 * 1024)
    with tempfile.NamedTemporaryFile(
        delete=False, suffix=".bin", prefix="gnosis_upload_", dir="/tmp"
    ) as f:
        for _ in range(CURL_UPLOAD_SIZE_BYTES // len(chunk)):
            f.write(chunk)
        rem = CURL_UPLOAD_SIZE_BYTES % len(chunk)
        if rem:
            f.write(b"\x00" * rem)
        path = f.name
    log.info("Upload file: %s (%d bytes)", path, os.path.getsize(path))
    return path


