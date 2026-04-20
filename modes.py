"""
Test mode implementations: locations, repeated, ramp, gap.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import (
    BASELINE_LOCATION_ID,
    CF_DL_10MB, CF_DL_100MB,
    CURL_MAX_TIME_S,
    DEFAULT_RUNS, DEFAULT_WARMUP_S, DEFAULT_WAIT_BETWEEN_S,
    GAP_SCHEDULE, GAP_WARMUP_S,
    LOG_FILE,
    PAUSE_BETWEEN_LOCS_S, PAUSE_BETWEEN_RUNS_S,
    RAMP_100MB_TIMEOUT_S, RAMP_GAP_S, RAMP_SIZES,
    REPEATED_COUNT, REPEATED_GAP_S,
)
from measure import (
    cf_download_url, create_upload_file,
    probe_latency_and_colo,
    run_cf_download, run_cf_upload,
)
from report import (
    _col, _fmt_mbit, _fmt_ms, _mean, _size_label, _stdev,
    flush_live, save_results,
)
from vpn import disconnect_vpn, get_destinations, vpn_connect, vpn_disconnect

log = logging.getLogger("gnosis_speedtest")


# ---------------------------------------------------------------------------
# Shared mode infrastructure
# ---------------------------------------------------------------------------

def _for_each_destination(mode_name: str, run_test, live_file: Path | None = None):
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
            flush_live(live_file, {"mode": mode_name, "results": all_results})
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
        flush_live(live_file, {"mode": mode_name, "results": all_results})

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


# =========================================================================
# MODE: locations  (baseline + per-exit upload/download/latency)
# =========================================================================

def _run_baseline_single(upload_file: str, run_number: int, wait_s: int) -> dict:
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
    sample["download_100mb_mbits"] = run_cf_download(CF_DL_100MB, "90MB")
    time.sleep(wait_s)
    sample["upload_mbits"] = run_cf_upload(upload_file)
    return sample


def _run_locations_single(dest: dict, upload_file: str, run: int,
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


def _compute_locations_stats(samples: list[dict]) -> dict:
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


def _render_locations_report(bl_samples, vpn_samples, bl_stats, vpn_stats,
                             n_runs, warmup_s, wait_s):
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    L: list[str] = []

    L.append("=" * 90)
    L.append(f"  GNOSIS VPN SPEED TEST [locations]  —  {now}  ({n_runs} runs/location)")
    L.append(f"  Endpoint: Cloudflare anycast  |  DL: 10 MB  |  Baseline DL: +90 MB  |  UL: 10 MB")
    L.append(f"  Warmup: {warmup_s}s  |  wait: {wait_s}s  |  log: {LOG_FILE}")
    L.append("=" * 90)

    # baseline raw
    L.append("\nBASELINE (no VPN)")
    hdr = f"{'Run':>4}  {'PoP':>4}  {'Lat(ms)':>8}  {'DL 10MB':>9}  {'DL 90MB':>9}  {'UL':>8}  Status"
    L += ["─" * len(hdr), hdr, "─" * len(hdr)]
    for s in bl_samples:
        st = f"ERR:{s['error']}" if s["error"] else "ok"
        L.append(f"{s['run']:>4}  {_col(s.get('cf_colo'),4)}  {_col(s['latency_ms'],8,1)}"
                 f"  {_col(s['download_mbits'],9)}  {_col(s.get('download_100mb_mbits'),9)}"
                 f"  {_col(s['upload_mbits'],8)}  {st}")
    bs = bl_stats
    c = ",".join(bs["cf_colos_unique"]) or "N/A"
    L.append(f"\n  Summary ({bs['n_complete']}/{bs['n_total']}): PoP={c}  lat={_fmt_ms(bs['latencies'])}ms"
             f"  dl10={_fmt_mbit(bs['downloads'])}  dl90={_fmt_mbit(bs['downloads_100mb'])}"
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


def _locations_live_payload(n_runs, bl, bl_stats, vpn_samples, vpn_stats):
    return {
        "mode": "locations", "n_runs": n_runs,
        "baseline": {"samples": bl, "stats": bl_stats},
        "vpn": {"samples": vpn_samples, "stats": vpn_stats},
    }


def cmd_locations(args) -> None:
    n_runs, warmup_s, wait_s = args.runs, args.warmup, args.wait
    live_file = Path(args.output) if args.output else None
    log.info("MODE: locations — %d runs, warmup=%ds, wait=%ds", n_runs, warmup_s, wait_s)

    destinations = get_destinations()
    if not destinations:
        log.error("No VPN destinations found.")
        sys.exit(1)
    disconnect_vpn()
    time.sleep(2)
    upload_file = create_upload_file()

    try:
        bl: list[dict] = []
        for r in range(1, n_runs + 1):
            bl.append(_run_baseline_single(upload_file, r, wait_s))
            bl_stats = _compute_locations_stats(bl)
            flush_live(live_file, _locations_live_payload(n_runs, bl, bl_stats, [], []))
        time.sleep(PAUSE_BETWEEN_LOCS_S)

        vpn_samples: list[dict] = []
        for i, dest in enumerate(destinations, 1):
            log.info("\n=== Location %d/%d: %s ===", i, len(destinations), dest["id"])
            for r in range(1, n_runs + 1):
                vpn_samples.append(_run_locations_single(dest, upload_file, r, warmup_s, wait_s))
                loc_order = list(dict.fromkeys(s["location_id"] for s in vpn_samples))
                vpn_stats = [_compute_locations_stats(
                    [s for s in vpn_samples if s["location_id"] == lid])
                    for lid in loc_order]
                flush_live(live_file, _locations_live_payload(
                    n_runs, bl, bl_stats, vpn_samples, vpn_stats))
                if r < n_runs:
                    time.sleep(PAUSE_BETWEEN_RUNS_S)
            if i < len(destinations):
                time.sleep(PAUSE_BETWEEN_LOCS_S)

        report = _render_locations_report(bl, vpn_samples, bl_stats, vpn_stats,
                                          n_runs, warmup_s, wait_s)
        save_results({
            "_report": report, "mode": "locations", "n_runs": n_runs,
            "baseline": {"samples": bl, "stats": bl_stats},
            "vpn": {"samples": vpn_samples, "stats": vpn_stats},
        }, "locations")
    finally:
        try:
            os.unlink(upload_file)
        except OSError:
            pass
        disconnect_vpn()


# =========================================================================
# MODE: repeated  (6 × 10 MB downloads — first immediately, then gaps)
# =========================================================================

def cmd_repeated(args) -> None:
    live_file = Path(args.output) if args.output else None
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

    all_results = _for_each_destination("repeated", run_test, live_file=live_file)

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
# MODE: ramp  (50 KB → 500 KB → 5 MB → 50 MB, 60 s gaps)
# =========================================================================

def cmd_ramp(args) -> None:
    live_file = Path(args.output) if args.output else None
    log.info("MODE: ramp — sizes %s, gap=%ds", [_size_label(s) for s in RAMP_SIZES], RAMP_GAP_S)

    def run_test(dest, ct, latency, colo):
        downloads: list[dict] = []
        for size in RAMP_SIZES:
            log.info("Waiting %ds…", RAMP_GAP_S)
            time.sleep(RAMP_GAP_S)
            label = _size_label(size)
            url = cf_download_url(size)
            timeout = RAMP_100MB_TIMEOUT_S if size >= 50 * 1024 * 1024 else CURL_MAX_TIME_S
            speed = run_cf_download(url, label, max_time_s=timeout)
            downloads.append({"size_bytes": size, "size_label": label, "speed_mbits": speed})
        return {"downloads": downloads}

    all_results = _for_each_destination("ramp", run_test, live_file=live_file)

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

def cmd_gap(args) -> None:
    live_file = Path(args.output) if args.output else None
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

    all_results = _for_each_destination("gap", run_test, live_file=live_file)

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



