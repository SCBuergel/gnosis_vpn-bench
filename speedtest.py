#!/usr/bin/env python3
"""
Gnosis VPN Speed Tester
=======================
Multiple test modes against Cloudflare's anycast speed-test infrastructure,
each accessed via a CLI subcommand:

  locations  — baseline + per-exit upload/download/latency (N runs each)
  repeated   — 6 × 10 MB download per exit (first immediately, then 60 s gaps)
  ramp       — download 100 KB → 1 MB → 10 MB → 100 MB per exit (60 s gaps)
  gap        — 13 × 10 MB download with increasing pauses (0 s → 55 s)

All modes share the same Cloudflare endpoint, VPN plumbing, and reporting
infrastructure.  Results are written to logs/ as .log, .txt, and .json.
"""

import argparse
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_RUNS: int = 5
PAUSE_BETWEEN_RUNS_S: int = 10
PAUSE_BETWEEN_LOCS_S: int = 3

CONNECTION_TIMEOUT_S: int = 60
POLL_INTERVAL_S: float = 1.0

CF_DL_URL_FMT: str = "https://speed.cloudflare.com/__down?bytes={}"
CF_DL_10MB:    str = CF_DL_URL_FMT.format(10 * 1024 * 1024)
CF_DL_100MB:   str = CF_DL_URL_FMT.format(100 * 1024 * 1024)
CF_UPLOAD:     str = "https://speed.cloudflare.com/__up"
CF_PROBE:      str = CF_DL_URL_FMT.format(1)

CURL_MAX_TIME_S: int = 60
CURL_UPLOAD_SIZE_BYTES: int = 10 * 1024 * 1024

LATENCY_TIMEOUT_S: int = 15
DEFAULT_WARMUP_S: int = 10
DEFAULT_WAIT_BETWEEN_S: int = 5

BASELINE_LOCATION_ID: str = "__baseline__"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
LOG_DIR = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

_run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE = LOG_DIR / f"speedtest_{_run_ts}.log"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d [%(levelname)-8s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("gnosis_speedtest")

# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------

def run_cmd(
    cmd: list[str],
    timeout_s: int = 30,
    stdin_data: bytes | None = None,
) -> tuple[int, str, str]:
    log.debug("CMD: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, input=stdin_data, capture_output=True, timeout=timeout_s)
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        if result.returncode not in (0, 28):
            log.warning("CMD exited %d | stderr: %s", result.returncode, stderr.strip()[:300])
        else:
            log.debug("CMD rc=%d stdout_len=%d", result.returncode, len(stdout))
        return result.returncode, stdout, stderr
    except subprocess.TimeoutExpired:
        log.error("CMD timed out after %ds: %s", timeout_s, " ".join(cmd))
        return -1, "", "process_timeout"
    except FileNotFoundError:
        log.error("CMD not found: %s", cmd[0])
        return -127, "", "not_found"
    except Exception as exc:
        log.error("CMD exception: %s", exc)
        return -1, "", str(exc)

# ---------------------------------------------------------------------------
# VPN control
# ---------------------------------------------------------------------------

def get_raw_status() -> dict | None:
    rc, stdout, stderr = run_cmd(["gnosis_vpn-ctl", "--json", "status"], timeout_s=15)
    if rc != 0:
        log.error("status failed (rc=%d): %s", rc, stderr.strip()[:200])
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        log.error("JSON parse error: %s", exc)
        return None


def get_destinations() -> list[dict]:
    status = get_raw_status()
    if not status:
        return []
    destinations = []
    for entry in status.get("Status", {}).get("destinations", []):
        dest = entry["destination"]
        addr_bytes: list[int] = dest["address"]
        addr_hex = "0x" + "".join(f"{b:02x}" for b in addr_bytes)
        destinations.append({
            "id": dest["id"],
            "location": dest.get("meta", {}).get("location", "Unknown"),
            "address": addr_hex,
        })
    return destinations


def _parse_state(raw) -> str:
    return next(iter(raw)) if isinstance(raw, dict) else str(raw)


def get_connection_state(location_id: str) -> str:
    status = get_raw_status()
    if not status:
        return "Unknown"
    for entry in status.get("Status", {}).get("destinations", []):
        if entry["destination"]["id"] == location_id:
            return _parse_state(entry.get("connection_state", "None"))
    return "Unknown"


_NOT_CONNECTED = {"none", "connecting", "unknown"}


def wait_for_connection(location_id: str, timeout_s: int = CONNECTION_TIMEOUT_S) -> float | None:
    log.info("Waiting for %s to connect (timeout %ds)…", location_id, timeout_s)
    t0 = time.monotonic()
    deadline = t0 + timeout_s
    prev: str | None = None
    while time.monotonic() < deadline:
        state = get_connection_state(location_id)
        elapsed = time.monotonic() - t0
        if state != prev:
            log.info("[%.1fs] %s: %r → %r", elapsed, location_id, prev, state)
            prev = state
        if state.lower() not in _NOT_CONNECTED:
            log.info("[%.1fs] %s connected", elapsed, location_id)
            return round(elapsed, 2)
        log.debug("[%.1fs] %s still %r…", elapsed, location_id, state)
        time.sleep(POLL_INTERVAL_S)
    log.error("Timed out waiting for %s after %ds", location_id, timeout_s)
    return None


def wait_for_disconnection(location_id: str, timeout_s: int = 60) -> bool:
    log.info("Waiting for %s to disconnect…", location_id)
    deadline = time.monotonic() + timeout_s
    prev: str | None = None
    while time.monotonic() < deadline:
        state = get_connection_state(location_id)
        if state != prev:
            log.info("%s disconnect: %r → %r", location_id, prev, state)
            prev = state
        if state in ("None", "none"):
            log.info("%s disconnected", location_id)
            return True
        time.sleep(POLL_INTERVAL_S)
    log.warning("Timed out waiting for %s to disconnect", location_id)
    return False


def fix_dns() -> None:
    log.info("Fixing DNS → 1.1.1.1")
    rc, _, stderr = run_cmd(["sudo", "tee", "/etc/resolv.conf"], timeout_s=10,
                            stdin_data=b"nameserver 1.1.1.1\n")
    if rc == 0:
        log.info("DNS fix applied.")
    else:
        log.warning("DNS fix failed: %s", stderr.strip()[:200])


def connect_to(destination: dict) -> bool:
    loc_id = destination["id"]
    log.info("Connecting to %s (%s)", loc_id, destination["location"])
    rc, stdout, stderr = run_cmd(["gnosis_vpn-ctl", "connect", loc_id], timeout_s=20)
    if rc != 0:
        log.error("connect failed for %s (rc=%d): %s", loc_id, rc, stderr.strip()[:200])
        return False
    log.debug("connect: %s", stdout.strip()[:200])
    return True


def disconnect_vpn() -> bool:
    log.info("Disconnecting…")
    rc, _, stderr = run_cmd(["gnosis_vpn-ctl", "disconnect"], timeout_s=20)
    if rc != 0:
        if "not connected" in stderr.lower():
            log.debug("Already disconnected.")
            return True
        log.error("disconnect failed: %s", stderr.strip()[:200])
        return False
    return True


def vpn_connect(destination: dict) -> float | None:
    """Connect to *destination*, fix DNS.  Returns connect_time or None."""
    if not connect_to(destination):
        return None
    ct = wait_for_connection(destination["id"])
    if ct is None:
        disconnect_vpn()
        wait_for_disconnection(destination["id"], timeout_s=30)
        return None
    fix_dns()
    return ct


def vpn_disconnect(destination: dict) -> None:
    disconnect_vpn()
    wait_for_disconnection(destination["id"])

# ---------------------------------------------------------------------------
# Speed-test helpers
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Stats / formatting helpers
# ---------------------------------------------------------------------------

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _fmt_ms(values: list[float]) -> str:
    if not values:
        return "N/A"
    return f"{_mean(values):.1f} ± {_stdev(values):.1f}"


def _fmt_mbit(values: list[float]) -> str:
    if not values:
        return "N/A"
    return f"{_mean(values):.2f} ± {_stdev(values):.2f}"


def _col(value, width: int, decimals: int = 2, align: str = ">") -> str:
    if value is None:
        s = "–"
    elif isinstance(value, float):
        s = f"{value:.{decimals}f}"
    else:
        s = str(value)
    return f"{s:{align}{width}}"


def _size_label(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.0f}MB"
    return f"{size_bytes / 1024:.0f}KB"


def save_results(payload: dict, mode: str) -> None:
    report_file = LOG_DIR / f"report_{mode}_{_run_ts}.txt"
    results_file = LOG_DIR / f"results_{mode}_{_run_ts}.json"
    report = payload.get("_report", "")
    if report:
        print("\n" + report)
        report_file.write_text(report, encoding="utf-8")
        log.info("Report saved to %s", report_file)
    json_payload = {k: v for k, v in payload.items() if k != "_report"}
    results_file.write_text(json.dumps(json_payload, indent=2, default=str), encoding="utf-8")
    log.info("JSON saved to %s", results_file)


# =========================================================================
# MODE: locations  (original full test — baseline + per-exit speed tests)
# =========================================================================

def run_baseline_single(upload_file: str, run_number: int, wait_s: int) -> dict:
    sample: dict = {
        "location_id": BASELINE_LOCATION_ID, "location": "No VPN",
        "run": run_number, "connect_time_s": None, "cf_colo": None,
        "latency_ms": None, "download_mbits": None,
        "download_100mb_mbits": None, "upload_mbits": None,
        "error": None, "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }
    log.info("── baseline run %d ──", run_number)
    sample["latency_ms"], sample["cf_colo"] = probe_latency_and_colo()
    time.sleep(wait_s)
    sample["download_mbits"] = run_cf_download(CF_DL_10MB, "10MB")
    time.sleep(wait_s)
    sample["download_100mb_mbits"] = run_cf_download(CF_DL_100MB, "100MB")
    time.sleep(wait_s)
    sample["upload_mbits"] = run_cf_upload(upload_file)
    return sample


def run_locations_single(dest: dict, upload_file: str, run: int,
                         warmup_s: int, wait_s: int) -> dict:
    loc_id = dest["id"]
    sample: dict = {
        "location_id": loc_id, "location": dest["location"],
        "run": run, "connect_time_s": None, "cf_colo": None,
        "latency_ms": None, "download_mbits": None,
        "download_100mb_mbits": None, "upload_mbits": None,
        "error": None, "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }
    log.info("── %s run %d ──", loc_id, run)
    ct = vpn_connect(dest)
    if ct is None:
        sample["error"] = "connect_failed"
        return sample
    sample["connect_time_s"] = ct
    log.info("Warmup %ds…", warmup_s)
    time.sleep(warmup_s)
    sample["latency_ms"], sample["cf_colo"] = probe_latency_and_colo()
    time.sleep(wait_s)
    sample["download_mbits"] = run_cf_download(CF_DL_10MB, "10MB")
    time.sleep(wait_s)
    sample["upload_mbits"] = run_cf_upload(upload_file)
    time.sleep(wait_s)
    vpn_disconnect(dest)
    return sample


def compute_locations_stats(samples: list[dict]) -> dict:
    loc_id = samples[0]["location_id"]
    is_bl = loc_id == BASELINE_LOCATION_ID
    connected = [s for s in samples if s["error"] is None]
    if is_bl:
        complete = [s for s in connected
                    if all(s.get(k) is not None for k in
                           ("latency_ms", "download_mbits",
                            "download_100mb_mbits", "upload_mbits"))]
    else:
        complete = [s for s in connected
                    if all(s.get(k) is not None for k in
                           ("connect_time_s", "latency_ms",
                            "download_mbits", "upload_mbits"))]
    colos = [s["cf_colo"] for s in complete if s.get("cf_colo")]
    return {
        "location_id": loc_id, "location": samples[0]["location"],
        "n_total": len(samples), "n_connected": len(connected),
        "n_complete": len(complete),
        "connect_times": [s["connect_time_s"] for s in complete if s["connect_time_s"] is not None],
        "latencies": [s["latency_ms"] for s in complete],
        "downloads": [s["download_mbits"] for s in complete],
        "downloads_100mb": [s.get("download_100mb_mbits") for s in complete
                            if s.get("download_100mb_mbits") is not None],
        "uploads": [s["upload_mbits"] for s in complete],
        "cf_colos_unique": sorted(set(colos), key=lambda c: -colos.count(c)),
    }


def render_locations_report(bl_samples, vpn_samples, bl_stats, vpn_stats,
                            n_runs, warmup_s, wait_s):
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    L: list[str] = []

    L.append("=" * 90)
    L.append(f"  GNOSIS VPN SPEED TEST [locations]  —  {now}  ({n_runs} runs/location)")
    L.append(f"  Endpoint: Cloudflare anycast  |  DL: 10 MB  |  Baseline DL: +100 MB  |  UL: 10 MB")
    L.append(f"  Warmup: {warmup_s}s  |  wait: {wait_s}s  |  log: {LOG_FILE}")
    L.append("=" * 90)

    # baseline raw
    L.append("\nBASELINE (no VPN)")
    hdr = f"{'Run':>4}  {'PoP':>4}  {'Lat(ms)':>8}  {'DL 10MB':>9}  {'DL 100MB':>10}  {'UL':>8}  Status"
    L += ["─" * len(hdr), hdr, "─" * len(hdr)]
    for s in bl_samples:
        st = f"ERR:{s['error']}" if s["error"] else "ok"
        L.append(f"{s['run']:>4}  {_col(s.get('cf_colo'),4)}  {_col(s['latency_ms'],8,1)}"
                 f"  {_col(s['download_mbits'],9)}  {_col(s.get('download_100mb_mbits'),10)}"
                 f"  {_col(s['upload_mbits'],8)}  {st}")
    bs = bl_stats
    c = ",".join(bs["cf_colos_unique"]) or "N/A"
    L.append(f"\n  Summary ({bs['n_complete']}/{bs['n_total']}): PoP={c}  lat={_fmt_ms(bs['latencies'])}ms"
             f"  dl10={_fmt_mbit(bs['downloads'])}  dl100={_fmt_mbit(bs['downloads_100mb'])}"
             f"  ul={_fmt_mbit(bs['uploads'])} Mbit/s")

    # vpn raw
    L.append("\n\nVPN EXIT TESTS (Mbit/s)")
    vhdr = (f"{'Location':<14} {'City':<14} {'Run':>4}  {'Conn':>6}  {'Lat':>6}  {'PoP':>4}"
            f"  {'DL 10MB':>9}  {'UL':>8}  Status")
    L += ["─" * len(vhdr), vhdr, "─" * len(vhdr)]
    prev = None
    for s in vpn_samples:
        if prev and s["location_id"] != prev:
            L.append("")
        prev = s["location_id"]
        st = f"ERR:{s['error']}" if s["error"] else "ok"
        L.append(f"{s['location_id']:<14} {s['location']:<14} {s['run']:>4}"
                 f"  {_col(s['connect_time_s'],6,1)}  {_col(s['latency_ms'],6,0)}"
                 f"  {_col(s.get('cf_colo'),4)}  {_col(s['download_mbits'],9)}"
                 f"  {_col(s['upload_mbits'],8)}  {st}")

    # vpn summary
    L.append("\n\nVPN SUMMARY (mean ± stdev, complete runs)")
    shdr = (f"{'Location':<14} {'City':<14} {'ok':>3} {'PoP':>4}"
            f"  {'Conn(s)':>14}  {'Lat(ms)':>14}  {'DL 10MB':>16}  {'UL':>16}")
    L += [shdr, "─" * len(shdr)]
    for st in vpn_stats:
        c = ",".join(st["cf_colos_unique"]) or "?"
        L.append(f"{st['location_id']:<14} {st['location']:<14} {st['n_complete']:>3} {c:>4}"
                 f"  {_fmt_ms(st['connect_times']):>14}  {_fmt_ms(st['latencies']):>14}"
                 f"  {_fmt_mbit(st['downloads']):>16}  {_fmt_mbit(st['uploads']):>16}")
    L.append("\n" + "=" * 90)
    return "\n".join(L)


def cmd_locations(args) -> None:
    n_runs, warmup_s, wait_s = args.runs, args.warmup, args.wait
    log.info("MODE: locations — %d runs, warmup=%ds, wait=%ds", n_runs, warmup_s, wait_s)

    destinations = get_destinations()
    if not destinations:
        log.error("No VPN destinations found.")
        sys.exit(1)
    disconnect_vpn()
    time.sleep(2)
    upload_file = create_upload_file()

    try:
        bl = [run_baseline_single(upload_file, r, wait_s) for r in range(1, n_runs + 1)]
        bl_stats = compute_locations_stats(bl)
        time.sleep(PAUSE_BETWEEN_LOCS_S)

        vpn_samples: list[dict] = []
        for i, dest in enumerate(destinations, 1):
            log.info("\n=== Location %d/%d: %s ===", i, len(destinations), dest["id"])
            for r in range(1, n_runs + 1):
                vpn_samples.append(run_locations_single(dest, upload_file, r, warmup_s, wait_s))
                if r < n_runs:
                    time.sleep(PAUSE_BETWEEN_RUNS_S)
            if i < len(destinations):
                time.sleep(PAUSE_BETWEEN_LOCS_S)

        loc_order = list(dict.fromkeys(s["location_id"] for s in vpn_samples))
        vpn_stats = [compute_locations_stats([s for s in vpn_samples if s["location_id"] == lid])
                     for lid in loc_order]

        report = render_locations_report(bl, vpn_samples, bl_stats, vpn_stats,
                                         n_runs, warmup_s, wait_s)
        save_results({
            "_report": report, "mode": "locations", "n_runs": n_runs,
            "baseline": {"samples": bl, "stats": bl_stats},
            "vpn": {"samples": vpn_samples, "stats": vpn_stats},
        }, "locations")
    finally:
        try: os.unlink(upload_file)
        except OSError: pass
        disconnect_vpn()


# =========================================================================
# MODE: repeated  (6 × 10 MB downloads — first immediately, then 60 s gaps)
# =========================================================================

REPEATED_COUNT: int = 6
REPEATED_GAP_S: int = 60


def _for_each_destination(mode_name: str, run_test):
    """Iterate all VPN destinations: connect, probe, call run_test(), disconnect.

    *run_test(dest, ct, latency, colo)* performs the mode-specific work and
    returns a dict of extra fields to merge into the per-location result.
    """
    destinations = get_destinations()
    if not destinations:
        log.error("No VPN destinations found.")
        sys.exit(1)
    disconnect_vpn()
    time.sleep(2)

    all_results: list[dict] = []

    for di, dest in enumerate(destinations, 1):
        loc_id = dest["id"]
        log.info("\n=== [%s] Location %d/%d: %s ===", mode_name, di, len(destinations), loc_id)

        ct = vpn_connect(dest)
        if ct is None:
            all_results.append({"location_id": loc_id, "location": dest["location"],
                                "connect_time_s": None, "error": "connect_failed",
                                "cf_colo": None, "downloads": []})
            continue

        latency, colo = probe_latency_and_colo()
        extra = run_test(dest, ct, latency, colo)
        vpn_disconnect(dest)

        result = {
            "location_id": loc_id, "location": dest["location"],
            "connect_time_s": ct, "cf_colo": colo, "latency_ms": latency,
            "error": None,
        }
        result.update(extra)
        all_results.append(result)

        if di < len(destinations):
            time.sleep(PAUSE_BETWEEN_LOCS_S)

    return all_results


def _render_per_location_report(title_lines: list[str], all_results: list[dict],
                                columns: list[tuple[str, str, int]],
                                row_fields, include_summary: bool = False) -> str:
    """Build a plain-text report for per-location download modes.

    *columns*: list of (header, dict_key, width) for the per-download table.
    *row_fields*: callable(download_dict) → list of formatted cell strings.
    """
    L: list[str] = []
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    width = max(len(t) for t in title_lines) + 10
    L.append("=" * width)
    for t in title_lines:
        L.append(f"  {t}")
    L[-len(title_lines)] = f"  {title_lines[0]}  —  {now}"
    L.append("=" * width)

    for r in all_results:
        L.append(f"\n{r['location_id']} ({r['location']})  PoP={r.get('cf_colo','?')}"
                 f"  connect={r.get('connect_time_s','FAIL')}s"
                 f"  lat={r.get('latency_ms','?')}ms")
        if r["error"]:
            L.append(f"  ERROR: {r['error']}")
            continue
        hdr = "  " + "  ".join(f"{name:>{w}}" for name, _, w in columns)
        L += [hdr, "  " + "─" * (len(hdr) - 2)]
        for d in r["downloads"]:
            L.append("  " + "  ".join(row_fields(d)))
        if include_summary and r.get("mean") is not None:
            n = len([d for d in r["downloads"] if d["speed_mbits"] is not None])
            L.append(f"\n  mean ± stdev: {r['mean']} ± {r['stdev']} Mbit/s  (n={n})")

    L.append("\n" + "=" * width)
    return "\n".join(L)


def cmd_repeated(args) -> None:
    log.info("MODE: repeated — %d × 10 MB download, gap=%ds after first", REPEATED_COUNT, REPEATED_GAP_S)

    def run_test(dest, ct, latency, colo):
        downloads: list[dict] = []
        for i in range(1, REPEATED_COUNT + 1):
            if i > 1:
                log.info("Waiting %ds before download %d…", REPEATED_GAP_S, i)
                time.sleep(REPEATED_GAP_S)
            else:
                log.info("Download %d: immediately after connect", i)
            speed = run_cf_download(CF_DL_10MB, f"10MB-#{i}")
            downloads.append({"run": i, "gap_before_s": 0 if i == 1 else REPEATED_GAP_S,
                              "speed_mbits": speed})
        speeds = [d["speed_mbits"] for d in downloads if d["speed_mbits"] is not None]
        return {
            "downloads": downloads,
            "mean": round(_mean(speeds), 2) if speeds else None,
            "stdev": round(_stdev(speeds), 2) if speeds else None,
        }

    all_results = _for_each_destination("repeated", run_test)

    report = _render_per_location_report(
        title_lines=[
            "REPEATED DOWNLOAD TEST",
            f"{REPEATED_COUNT} × 10 MB  |  first: immediately after connect  |  then: {REPEATED_GAP_S}s gaps",
        ],
        all_results=all_results,
        columns=[("#", "run", 3), ("Gap(s)", "gap_before_s", 7), ("Speed (Mbit/s)", "speed_mbits", 15)],
        row_fields=lambda d: [f"{d['run']:>3}", f"{d['gap_before_s']:>7}", _col(d["speed_mbits"], 15)],
        include_summary=True,
    )
    save_results({"_report": report, "mode": "repeated", "results": all_results}, "repeated")
    disconnect_vpn()


# =========================================================================
# MODE: ramp  (100 KB → 1 MB → 10 MB → 100 MB, 60 s gaps)
# =========================================================================

RAMP_SIZES: list[int] = [
    100 * 1024,         # 100 KB
    1 * 1024 * 1024,    # 1 MB
    10 * 1024 * 1024,   # 10 MB
    100 * 1024 * 1024,  # 100 MB
]
RAMP_GAP_S: int = 60
RAMP_100MB_TIMEOUT_S: int = 600  # 10 min for the 100 MB download


def cmd_ramp(args) -> None:
    log.info("MODE: ramp — sizes %s, gap=%ds", [_size_label(s) for s in RAMP_SIZES], RAMP_GAP_S)

    def run_test(dest, ct, latency, colo):
        downloads: list[dict] = []
        for i, size in enumerate(RAMP_SIZES):
            log.info("Waiting %ds…", RAMP_GAP_S)
            time.sleep(RAMP_GAP_S)
            label = _size_label(size)
            url = cf_download_url(size)
            timeout = RAMP_100MB_TIMEOUT_S if size >= 100 * 1024 * 1024 else CURL_MAX_TIME_S
            speed = run_cf_download(url, label, max_time_s=timeout)
            downloads.append({"size_bytes": size, "size_label": label, "speed_mbits": speed})
        return {"downloads": downloads}

    all_results = _for_each_destination("ramp", run_test)

    report = _render_per_location_report(
        title_lines=[
            "RAMP DOWNLOAD TEST",
            f"Sizes: {', '.join(_size_label(s) for s in RAMP_SIZES)}  |  {RAMP_GAP_S}s between each",
        ],
        all_results=all_results,
        columns=[("Size", "size_label", 8), ("Speed (Mbit/s)", "speed_mbits", 15)],
        row_fields=lambda d: [f"{d['size_label']:>8}", _col(d["speed_mbits"], 15)],
    )
    save_results({"_report": report, "mode": "ramp", "results": all_results}, "ramp")
    disconnect_vpn()


# =========================================================================
# MODE: gap  (13 × 10 MB downloads with increasing pauses)
# =========================================================================
#
# Schedule after 60 s warmup:
#   DL1 — immediately (gap = 0)
#   DL2 — immediately after DL1 (gap = 0)
#   DL3 — 5 s after DL2
#   DL4–DL13 — gap increases by 5 s each time (10, 15, 20, …, 55 s)

GAP_WARMUP_S: int = 60
GAP_SCHEDULE: list[int] = [0, 0, 5] + list(range(10, 60, 5))  # 13 entries


def cmd_gap(args) -> None:
    log.info("MODE: gap — %d downloads, warmup=%ds, gaps=%s",
             len(GAP_SCHEDULE), GAP_WARMUP_S, GAP_SCHEDULE)

    def run_test(dest, ct, latency, colo):
        log.info("Warmup: waiting %ds…", GAP_WARMUP_S)
        time.sleep(GAP_WARMUP_S)
        downloads: list[dict] = []
        for i, gap in enumerate(GAP_SCHEDULE, 1):
            if gap > 0:
                log.info("Waiting %ds before download %d…", gap, i)
                time.sleep(gap)
            else:
                log.info("Download %d: no gap", i)
            speed = run_cf_download(CF_DL_10MB, f"10MB-gap{gap}s-#{i}")
            downloads.append({"run": i, "gap_before_s": gap, "speed_mbits": speed})
        speeds = [d["speed_mbits"] for d in downloads if d["speed_mbits"] is not None]
        return {
            "downloads": downloads,
            "mean": round(_mean(speeds), 2) if speeds else None,
            "stdev": round(_stdev(speeds), 2) if speeds else None,
        }

    all_results = _for_each_destination("gap", run_test)

    report = _render_per_location_report(
        title_lines=[
            "INCREASING GAP DOWNLOAD TEST",
            f"{len(GAP_SCHEDULE)} × 10 MB  |  warmup: {GAP_WARMUP_S}s  |  gaps: {GAP_SCHEDULE}",
        ],
        all_results=all_results,
        columns=[("#", "run", 3), ("Gap(s)", "gap_before_s", 7), ("Speed (Mbit/s)", "speed_mbits", 15)],
        row_fields=lambda d: [f"{d['run']:>3}", f"{d['gap_before_s']:>7}", _col(d["speed_mbits"], 15)],
        include_summary=True,
    )
    save_results({"_report": report, "mode": "gap", "results": all_results}, "gap")
    disconnect_vpn()


# =========================================================================
# CLI entry point
# =========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gnosis VPN speed tester — multiple test modes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
modes:
  locations   Baseline + per-exit upload/download/latency (N runs each)
  repeated    6 × 10 MB download per exit (immediately, then 60 s gaps)
  ramp        Download 100 KB → 1 MB → 10 MB → 100 MB (60 s gaps)
  gap         13 × 10 MB download with increasing pauses (0 → 55 s)
""",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    # -- locations --
    p_loc = sub.add_parser("locations", help="Full location benchmark (baseline + VPN exits)")
    p_loc.add_argument("--runs",   type=int, default=DEFAULT_RUNS)
    p_loc.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_S)
    p_loc.add_argument("--wait",   type=int, default=DEFAULT_WAIT_BETWEEN_S)
    p_loc.set_defaults(func=cmd_locations)

    # -- repeated --
    p_rep = sub.add_parser("repeated", help="6 × 10 MB download (immediate + 60 s gaps)")
    p_rep.set_defaults(func=cmd_repeated)

    # -- ramp --
    p_ramp = sub.add_parser("ramp", help="Download 100KB→1MB→10MB→100MB (60 s gaps)")
    p_ramp.set_defaults(func=cmd_ramp)

    # -- gap --
    p_gap = sub.add_parser("gap", help="13 × 10 MB with increasing pauses (0→55 s)")
    p_gap.set_defaults(func=cmd_gap)

    args = parser.parse_args()
    log.info("Gnosis VPN Speed Tester — mode: %s — log: %s", args.mode, LOG_FILE)
    args.func(args)
    log.info("Done.")


if __name__ == "__main__":
    main()
