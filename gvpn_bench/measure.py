"""Cloudflare anycast speed measurements: download, upload, latency probe.

All commands run on a ``Host``, so the same measurement works locally and
on a remote VM.
"""

from __future__ import annotations

import logging
import threading

from .host import Host
from .settings import (
    CF_DL_URL_FMT,
    CF_PROBE,
    CF_UPLOAD,
    CURL_MAX_TIME_S,
    CURL_UPLOAD_SIZE_BYTES,
    LATENCY_TIMEOUT_S,
)

log = logging.getLogger("gnosis_speedtest")


def _bytes_to_mbit(bytes_per_second: float) -> float:
    return bytes_per_second * 8 / 1_000_000


def _run_curl_streaming(host: Host, argv: list[str], direction: str) -> float | None:
    """Run curl, log its progress meter live, return the final speed in Mbit/s."""
    log.info("curl %s on %s", direction, host.name)
    try:
        proc = host.popen(argv)
    except FileNotFoundError:
        log.error("curl (or ssh) not found")
        return None
    assert proc.stdout is not None and proc.stderr is not None
    stderr = proc.stderr

    def _read_stderr() -> None:
        for raw in stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line.strip():
                log.info("  curl [%s] %s", direction, line)

    reader = threading.Thread(target=_read_stderr, daemon=True)
    reader.start()
    try:
        stdout_bytes = proc.stdout.read()
    except OSError as exc:
        log.warning("curl %s stdout read error: %s", direction, exc)
        stdout_bytes = b""
    reader.join(timeout=CURL_MAX_TIME_S + 30)
    if reader.is_alive():
        proc.kill()
    proc.wait()
    if proc.returncode not in (0, 28):
        log.error("curl %s failed rc=%d", direction, proc.returncode)
        return None
    try:
        speed_mbit = round(_bytes_to_mbit(float(stdout_bytes.decode().strip())), 2)
    except ValueError:
        log.error("Cannot parse curl %s speed: %r", direction, stdout_bytes[:80])
        return None
    log.info("curl %s FINAL: %.2f Mbit/s", direction, speed_mbit)
    return speed_mbit


def parse_probe_output(stdout: str) -> tuple[float | None, str | None]:
    """Split ``curl -D - … --write-out '\\n%{time_starttransfer}'`` output.

    Headers come first; the last line is the time to first byte in seconds.
    """
    lines = stdout.strip().splitlines()
    if not lines:
        return None, None
    latency_ms: float | None
    try:
        latency_ms = round(float(lines[-1].strip()) * 1000, 1)
    except ValueError:
        latency_ms = None
    colo = None
    for line in lines[:-1]:
        if line.lower().startswith("colo:"):
            colo = line.split(":", 1)[1].strip()
            break
    return latency_ms, colo


def probe_latency_and_colo(host: Host) -> tuple[float | None, str | None]:
    """Time-to-first-byte against Cloudflare and the serving PoP (``colo``)."""
    result = host.run(
        [
            "curl",
            "-s",
            "-k",
            "-D",
            "-",
            "-o",
            "/dev/null",
            "--max-time",
            str(LATENCY_TIMEOUT_S),
            "--write-out",
            "\n%{time_starttransfer}",
            CF_PROBE,
        ],
        timeout_s=LATENCY_TIMEOUT_S + 10,
    )
    if result.returncode not in (0, 28):
        log.warning("Probe failed rc=%d", result.returncode)
        return None, None
    latency_ms, colo = parse_probe_output(result.stdout)
    log.info("Latency: %s ms  CF PoP: %s", latency_ms, colo)
    return latency_ms, colo


def cf_download_url(size_bytes: int) -> str:
    """Return the Cloudflare download URL for an arbitrary byte count."""
    return CF_DL_URL_FMT.format(size_bytes)


def run_cf_download(host: Host, url: str, label: str, max_time_s: int = CURL_MAX_TIME_S) -> float | None:
    log.info("DOWNLOAD [%s]  →  %s  (max %ds)", label, url, max_time_s)
    return _run_curl_streaming(
        host,
        [
            "curl",
            "-k",
            "--output",
            "/dev/null",
            "--max-time",
            str(max_time_s),
            "--write-out",
            "%{speed_download}",
            url,
        ],
        direction=f"download/{label}",
    )


def run_cf_upload(host: Host, upload_file: str) -> float | None:
    log.info("UPLOAD  →  %s  (max %ds)", CF_UPLOAD, CURL_MAX_TIME_S)
    return _run_curl_streaming(
        host,
        [
            "curl",
            "-k",
            "--request",
            "POST",
            "--data-binary",
            f"@{upload_file}",
            "--max-time",
            str(CURL_MAX_TIME_S),
            "--write-out",
            "%{speed_upload}",
            "--output",
            "/dev/null",
            CF_UPLOAD,
        ],
        direction="upload",
    )


def create_upload_file(host: Host) -> str:
    """Create a zero-filled upload payload on *host* and return its path."""
    log.info("Creating %.0f MB upload file on %s…", CURL_UPLOAD_SIZE_BYTES / 1024 / 1024, host.name)
    made = host.run(["mktemp", "--tmpdir", "gnosis_upload_XXXXXX.bin"], timeout_s=10)
    if not made.ok:
        raise RuntimeError(f"{host.name}: mktemp failed: {made.stderr.strip()[:200]}")
    path = made.stdout.strip()
    sized = host.run(["truncate", "-s", str(CURL_UPLOAD_SIZE_BYTES), path], timeout_s=30)
    if not sized.ok:
        raise RuntimeError(f"{host.name}: cannot size upload file: {sized.stderr.strip()[:200]}")
    log.info("Upload file: %s:%s (%d bytes)", host.name, path, CURL_UPLOAD_SIZE_BYTES)
    return path


def remove_file(host: Host, path: str) -> None:
    host.run(["rm", "-f", "--", path], timeout_s=10)
