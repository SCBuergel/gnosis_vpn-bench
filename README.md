# Gnosis VPN Speed Tester

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
./gnosis_vpn-bench locations

# Run a quick single-run sweep
./gnosis_vpn-bench locations --runs 1 --warmup 5 --wait 2

# Stream results to a live JSON file (updated after every test)
./gnosis_vpn-bench -o live.json locations
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
./gnosis_vpn-bench {locations,repeated,ramp,gap}
```

### `locations` -- full per-exit benchmark

Runs a **no-VPN baseline** first (10 MB + 100 MB download, 10 MB upload,
latency), then connects to each exit and runs N cycles of
latency + 10 MB download + 10 MB upload.  Reports mean +/- stdev per location.

```bash
./gnosis_vpn-bench locations [--runs N] [--warmup S] [--wait S]
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
./gnosis_vpn-bench repeated
```

Use this to check whether throughput is stable once the tunnel is warm, or
whether the first transfer after connect is significantly slower.

### `ramp` -- throughput vs. transfer size

Per exit: downloads 50 KB, 500 KB, 5 MB, 50 MB with 60 s gaps.  A 60 s
warmup precedes the first download.

```bash
./gnosis_vpn-bench ramp
```

Small transfers are dominated by TCP slow-start and tunnel setup overhead.
This mode shows at what transfer size the tunnel reaches steady-state throughput.
The 50 MB download has a 10-minute timeout.

### `gap` -- idle-gap degradation

Per exit: 13 x 10 MB downloads after a 60 s warmup.  Gaps between downloads
increase: 0, 0, 5, 10, 15, ..., 55 s.

```bash
./gnosis_vpn-bench gap
```

Tests whether idle periods cause the tunnel to degrade (e.g. congestion-window
decay, session teardown, or path re-routing).

## Monitor recordings & continuous tests

Alongside the location-sweep benchmarks, `./gnosis_vpn-bench` exposes the
recording / plotting subcommands that originated in
[`gnosis_vpn-monitor`](https://github.com/SCBuergel/gnosis_vpn-monitor),
plus a new `ping-load` mode that combines them:

```
./gnosis_vpn-bench ping  [HOST]   # record ping latency
./gnosis_vpn-bench curl  [URL]    # record curl throughput
./gnosis_vpn-bench plot  FILE…    # render an SVG/PNG chart
./gnosis_vpn-bench ping-load      # ping continuously + 10 MB curl burst every N min
```

Recordings land in `./data/`, plots in `./output/` (both resolved
relative to the script and created on demand). Default filenames embed
the start timestamp — `ping--2026-05-11--23-57-47.txt` — so re-running
a recorder never silently overwrites the previous session's data.

### `ping` — latency recorder

```bash
./gnosis_vpn-bench ping                      # default host: google.com
./gnosis_vpn-bench ping example.org          # custom host
./gnosis_vpn-bench ping example.org -o run.txt
```

Spawns a fresh `ping -c 1 -W 1 -D <host>` per second rather than letting
a single long-running ping stream samples — that defeats the kernel /
intermediate-hop path caching that would otherwise bias VPN
measurements toward the hot path. Output is teed to stdout and to
`data/ping--TIMESTAMP.txt`. Stop with Ctrl-C. Pass `-o ''` to record to
stdout only.

### `curl` — throughput recorder

```bash
./gnosis_vpn-bench curl                      # default URL: kernel.org tarball
./gnosis_vpn-bench curl https://example.com/big.bin
```

Runs `curl -o /dev/null <url>` and timestamps each progress update.
Output is teed to stdout and to `data/curl--TIMESTAMP.txt`. The
recording ends when the download finishes or you Ctrl-C.

### `ping-load` — continuous ping with periodic load bursts

```bash
./gnosis_vpn-bench ping-load                   # 30-min interval (default)
./gnosis_vpn-bench ping-load --interval 10     # 10-min interval
./gnosis_vpn-bench ping-load --host 1.1.1.1 --interval 15
```

Runs `ping` continuously in a background thread (writing to
`data/ping--TIMESTAMP.txt`) and, every `--interval` minutes, fires a
single 10 MB curl burst whose progress meter is **appended** to
`data/speed--TIMESTAMP.txt`. The speed file gets no separator/blank
lines between bursts, so the same file accumulates the speed samples
from every burst contiguously, and `plot` consumes the pair directly:

```bash
./gnosis_vpn-bench plot \
    data/ping--2026-05-11--23-57-47.txt \
    --right data/speed--2026-05-11--23-57-47.txt
```

That gives you ping latency and burst throughput on one chart from two
files — exactly the workflow this mode exists for. Each `ping-load`
session shares one timestamp across both files, so the pair is easy to
spot.

| Flag | Default | Meaning |
|------|---------|---------|
| `--host` | `google.com` | Ping target |
| `--url` | Cloudflare 10 MB anycast | URL fetched on each burst |
| `--interval` | `30` | Minutes between curl bursts |
| `--ping-output` | `data/ping--TIMESTAMP.txt` | Ping recording path |
| `--speed-output` | `data/speed--TIMESTAMP.txt` | Burst speed log (append-only) |

### `plot` — render a chart

Positional files go on the **left** y-axis by default; `--right`
(repeatable) opens a second y-axis. Both `--left` and `--right` may be
passed any number of times, so any (N, M) split is expressible:

```bash
# 1 file → single y-axis
./gnosis_vpn-bench plot data/ping--TS.txt

# Multiple same-kind files share the left axis (each gets its own colour)
./gnosis_vpn-bench plot data/ping-before.txt data/ping-after.txt

# Independent left/right axes — the canonical ping+curl combined chart
./gnosis_vpn-bench plot data/ping--TS.txt --right data/speed--TS.txt

# Several files per side, fully explicit
./gnosis_vpn-bench plot \
    --left data/ping-a.txt --left data/ping-b.txt \
    --right data/curl-a.txt --right data/curl-b.txt
```

All files on the same axis must be the same kind (so the axis labels a
single unit); mixing ping and curl is what `--right` is for. The
recording kind is auto-detected from each file's contents.

With no `-o` the chart is written under `output/` with a timestamped
filename plus a sibling PNG (requires `rsvg-convert`, ImageMagick or
Inkscape on `PATH`; falls back to SVG-only if none is found). `--log-y`,
`--style` (matplotlib-style format strings like `xb`, `o-r`, `.--g`,
repeated once per series in left-then-right order), `--legend`,
`--width`, `--height`, `--png-scale` are also accepted — see
`./gnosis_vpn-bench plot --help` for the full list.

### Recording file format

One timestamped sample per line:

```
[1778315104.443890] 64 bytes from ... time=177 ms
[1778315105.426565] 64 bytes from ... time=352 ms
```

…or for curl:

```
[1778315901.123456]   0  149M    0  111k    0     0  84162  ...  84137
```

Headers, banners and other non-data lines are tolerated and skipped
during parsing — so `ping-load`'s speed file (which contains a fresh
curl banner before each burst) parses with the same code path as a
single-burst recording.

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

All timing constants are near the top of `./gnosis_vpn-bench`:

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
