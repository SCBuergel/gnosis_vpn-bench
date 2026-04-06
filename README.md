# Gnosis VPN Speed Tester

_Last changed: 2026-04-06_

Automated throughput and latency benchmarking for Gnosis VPN tunnels.
Connects to each configured exit in turn, runs downloads/uploads against
Cloudflare's anycast speed-test endpoint, and writes structured results
(JSON + plain-text report + debug log) to `logs/`.

## Prerequisites

- **Gnosis VPN service running** with `gnosis_vpn-ctl` on `PATH`
- Python 3.11+ (standard library only, no pip packages)
- `curl`
- `sudo` rights (the script writes `nameserver 1.1.1.1` to `/etc/resolv.conf`
  after each VPN connect to work around DNS leaks)

## Quick start

```bash
# Verify VPN is up and has exits configured
gnosis_vpn-ctl --json status

# Run the default full benchmark (baseline + all exits, 5 runs each)
python3 speedtest.py locations

# Run a quick single-run sweep
python3 speedtest.py locations --runs 1 --warmup 5 --wait 2

# Stream results to a live JSON file (updated after every test)
python3 speedtest.py -o live.json locations
```

Output lands in `logs/`:

| File | Content |
|------|---------|
| `speedtest_TIMESTAMP.log` | Full DEBUG-level trace of every command |
| `report_{mode}_TIMESTAMP.txt` | Human-readable report (also printed to stdout) |
| `results_{mode}_TIMESTAMP.json` | Machine-readable raw samples + computed stats |

With `-o FILE`, a **live JSON file** is also written after every individual test
completes.  This is useful for monitoring long runs or feeding results into a
dashboard.  The file is written atomically (write-to-tmp + rename).

## Test modes

```
python3 speedtest.py {locations,repeated,ramp,gap}
```

### `locations` -- full per-exit benchmark

Runs a **no-VPN baseline** first (10 MB + 100 MB download, 10 MB upload,
latency), then connects to each exit and runs N cycles of
latency + 10 MB download + 10 MB upload.  Reports mean +/- stdev per location.

```bash
python3 speedtest.py locations [--runs N] [--warmup S] [--wait S]
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--runs` | 5 | Test cycles per exit |
| `--warmup` | 10 | Seconds to wait after VPN connect before testing |
| `--wait` | 5 | Seconds between individual tests within a cycle |

Use this mode for a comprehensive comparison across all exits.

### `repeated` -- download stability over time

Per exit: 6 x 10 MB downloads.  The first runs immediately after connect
(cold-tunnel performance); the remaining 5 follow 60 s gaps.

```bash
python3 speedtest.py repeated
```

Use this to check whether throughput is stable once the tunnel is warm, or
whether the first transfer after connect is significantly slower.

### `ramp` -- throughput vs. transfer size

Per exit: downloads 50 KB, 500 KB, 5 MB, 50 MB with 60 s gaps.  A 60 s
warmup precedes the first download.

```bash
python3 speedtest.py ramp
```

Small transfers are dominated by TCP slow-start and tunnel setup overhead.
This mode shows at what transfer size the tunnel reaches steady-state throughput.
The 50 MB download has a 10-minute timeout.

### `gap` -- idle-gap degradation

Per exit: 13 x 10 MB downloads after a 60 s warmup.  Gaps between downloads
increase: 0, 0, 5, 10, 15, ..., 55 s.

```bash
python3 speedtest.py gap
```

Tests whether idle periods cause the tunnel to degrade (e.g. congestion-window
decay, session teardown, or path re-routing).

## How it works

All modes use **Cloudflare anycast** (`speed.cloudflare.com`):

| Operation | Endpoint |
|-----------|----------|
| Download N bytes | `GET /__down?bytes=N` |
| Upload | `POST /__up` (10 MB zero-filled payload) |
| Latency probe | `GET /__down?bytes=1` + read `time_starttransfer` |
| PoP detection | `colo:` response header (IATA code, e.g. `LHR`, `SYD`) |

Anycast means the same URL routes to the nearest Cloudflare datacenter from
wherever the request exits the VPN, so each exit naturally hits a different
PoP.  The PoP is logged per test to confirm traffic is actually egressing
where expected.

Speeds are derived from curl's `speed_download` / `speed_upload` output
(bytes/sec, converted to Mbit/s).  Latency is `time_starttransfer` on a
1-byte download (ms).

### Why not speedtest-cli / Ookla?

Ookla's server selection picks the closest server to the exit node's public IP.
Through some exits this measures the exit node's local ISP link, not the
end-to-end VPN tunnel.  Cloudflare anycast avoids this problem.

## Tuning

All timing constants are near the top of `speedtest.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `DEFAULT_RUNS` | 5 | `--runs` default for `locations` |
| `DEFAULT_WARMUP_S` | 10 | `--warmup` default for `locations` |
| `DEFAULT_WAIT_BETWEEN_S` | 5 | `--wait` default for `locations` |
| `PAUSE_BETWEEN_RUNS_S` | 10 | Pause between runs of the same exit |
| `PAUSE_BETWEEN_LOCS_S` | 3 | Pause between exits |
| `CONNECTION_TIMEOUT_S` | 60 | Max wait for VPN `Connected` state |
| `CURL_MAX_TIME_S` | 60 | Hard timeout per curl transfer |
| `RAMP_100MB_TIMEOUT_S` | 600 | Timeout for the 50 MB ramp download |
| `REPEATED_COUNT` | 6 | Downloads in `repeated` mode |
| `REPEATED_GAP_S` | 60 | Gap between `repeated` downloads |
| `RAMP_GAP_S` | 60 | Gap between `ramp` sizes |
| `GAP_WARMUP_S` | 60 | Warmup before first `gap` download |
| `GAP_SCHEDULE` | `[0,0,5,10,...,55]` | Per-download gaps in `gap` mode |

## Troubleshooting

- **"No VPN destinations found"** -- `gnosis_vpn-ctl --json status` returns no
  destinations.  Check that the VPN service is running and exits are configured.
- **All downloads fail with timeout** -- The DNS fix writes `1.1.1.1` to
  `resolv.conf`; if that is blocked, curl cannot resolve `speed.cloudflare.com`.
  Check firewall rules on the exit.
- **PoP shows unexpected location** -- Cloudflare anycast routing is
  best-effort; the PoP may not match the exit's city exactly.
- **Debug details** -- Every `gnosis_vpn-ctl` and `curl` invocation is logged
  with return code and timing in the `.log` file.
