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
Inkscape on `PATH`; falls back to SVG-only if none is found).
`--legend`, `--width`, `--height`, `--png-scale` are also accepted —
see `./gnosis_vpn-bench plot --help` for the full list.

#### Log-scale y-axis (`--log-y`)

Apply a base-10 log scale to *both* y-axes. Useful when one series
spans several orders of magnitude (typical for `ramp` results mixing
50 KB and 50 MB transfers, or any chart where small and large values
would otherwise be crushed against the axis):

```bash
./gnosis_vpn-bench plot data/ping--TS.txt --right data/speed--TS.txt --log-y
```

Non-positive samples are skipped on a log axis (a whole series of
zeros/negatives errors out with a clear message). Minor decade-internal
ticks appear automatically when the visible range spans fewer than
four decades.

#### Box plots (`--boxes`)

`--boxes FILE` renders a curl/speed recording as one box per burst
instead of one dot per progress sample. Designed for `ping-load`
output, where each 10 MB burst emits a tight cluster of sub-second
samples that collapses into a visually unreadable column at session
time-scale.

```bash
# Right-axis box plot alongside ping data on the left
./gnosis_vpn-bench plot data/ping--TS.txt --boxes data/speed--TS.txt

# Boxes alone — no left- or right-axis dot data
./gnosis_vpn-bench plot --boxes data/speed--TS.txt

# Two sessions overlaid (VPN vs no-VPN)
./gnosis_vpn-bench plot \
    --left  data/ping-vpn.txt    --left  data/ping-novpn.txt \
    --boxes data/speed-vpn.txt   --boxes data/speed-novpn.txt \
    --style b1 --style b3 --style r1 --style r3 \
    --legend "ping VPN"  --legend "ping no-VPN" \
    --legend "speed VPN" --legend "speed no-VPN"
```

Box anatomy per burst (standard Tukey five-number summary):

| element | encodes |
|---------|---------|
| whisker (vertical line with caps) | `min` / `max` (of inliers) |
| translucent body rectangle | `Q1` → `Q3` (interquartile range, R type 7 linear interpolation) |
| solid horizontal tick | `median` (`Q2`) |
| `*` marker | individual outlier sample |

**Outliers.** Before any of the five-number stats are computed, the
burst's samples are passed through a single-pass outlier filter: any
sample whose value differs from the median by more than `N×σ` (with
`N` set by `--box-outlier-sigma`, default `3.0`, and σ the population
standard deviation of the *original* samples) is classified as an
outlier. The filter runs **once** — σ is not recomputed after removal,
so a borderline second outlier doesn't get promoted by a shrinking-σ
feedback loop. Inliers feed every reported stat (`min`, `Q1`,
`median`, `Q3`, `max`); outliers are still drawn, as standalone `*`
markers in the box's colour, so they remain visible without distorting
the summary. At `--log-level DEBUG` each burst header logs the
pre-removal median, σ and threshold so the classification can be
verified arithmetically against the listed raw samples.

Bursts are auto-detected from the time gap between successive samples
in the recording — `--box-gap SECONDS` is the threshold. At default
`ping-load` parameters (intra-burst samples sub-second, 30-minute
inter-burst idle), any threshold from 1 s to ~1700 s works; the 60 s
default leaves both bounds comfortably far away.

Non-positive samples (`v ≤ 0`) are dropped from every burst before
summarization. curl's progress meter always emits a leading "0 B/s"
sample at the start of each burst that would otherwise pin every
whisker's minimum to 0 — wrong on linear axes and invisible on log-y.
At `--log-level DEBUG` the per-file header reports how many zeros were
dropped.

| Flag | Default | Meaning |
|------|---------|---------|
| `--box-gap` | `60` | Sample-gap threshold (seconds) that splits bursts |
| `--box-min-samples` | `1` | Drop bursts with fewer than N samples (1 = even degenerate single-sample boxes draw) |
| `--box-width` | `10` | Box width in pixels (fixed; not scaled to burst duration, which would render sub-pixel at session scale) |
| `--box-outlier-sigma` | `3.0` | σ multiplier for the outlier threshold (samples more than N×σ from the burst median are flagged) |
| `--box-show-dots` | off | Overlay each burst's raw samples as small translucent dots — useful for verifying the summary matches the data |
| `--box-label` | off | Print `N` / start time / `min` / `q1` / `median` / `q3` / `max` next to each box |

For `--style` on a box series, only the colour code is honoured;
linestyle and marker codes are silently ignored (a box has neither).
The legend swatch for a box series is a miniature box icon rather than
a line+marker sample.

Box files normally land on the **right axis** when there's any other
data on the chart. If `--boxes` is the only input (no `--left`, no
positional, no `--right`), the boxes ARE the chart and occupy a single
tinted axis — same shape as a single-series ping chart.

To verify each box matches its raw data, run with
`--log-level DEBUG` — the per-burst samples are then dumped on stdout
(see the **Log verbosity** section below).

#### Style strings (`--style`)

Each `--style` is one matplotlib-style format string, applied to series
in left-then-right order. A style combines up to three pieces in any
order:

| component | codes |
|-----------|-------|
| linestyle | `-` `--` `-.` `:` |
| marker    | `.` `o` `x` `+` `*` `s` `D` `^` `v` |
| colour    | `b` `g` `r` `c` `m` `y` `k` `w` |

Colour letters in the six colourful families (`b g r c m y`) accept an
optional `1`/`2`/`3` shade suffix — light/medium/dark — for charts that
need to disambiguate multiple series within one family (think four red
markers on one axis):

| family   | bare    | `1` light  | `2` mid    | `3` dark   |
|----------|---------|------------|------------|------------|
| blue     | blue    | `#8BB5FF`  | `#5176CD`  | `#1C3A8B`  |
| green    | green   | `#63D18F`  | `#0E9254`  | `#00561C`  |
| red      | red     | `#FF9189`  | `#BF534E`  | `#7C1117`  |
| cyan     | cyan    | `#00D1DA`  | `#00919B`  | `#00555F`  |
| magenta  | magenta | `#E497E8`  | `#A35AA7`  | `#651E6A`  |
| yellow   | olive   | `#D3B63B`  | `#957700`  | `#5A3E00`  |

The 1/2/3 variants are derived from OKLCH (a perceptually uniform colour
space): six hue angles equidistant around the wheel (25° 95° 155° 200°
265° 325°), three lightness steps (0.78 / 0.58 / 0.38) at chroma 0.14,
serialized to sRGB. The warm-to-cool hues hit the 0.14 chroma cleanly;
yellow/green/cyan darks get gamut-clipped by sRGB (saturated dark
yellow simply doesn't fit), so they read a touch more muted than the
warm darks — an unavoidable colour-space limit, not a styling choice.

A swatch sheet of all 24 entries side-by-side is at
`output/ladder-overview.{svg,png}` — regenerate it with
`python3 ladder_overview.py` after editing `COLOR_MAP`.

Each step shifts saturation/hue as well as lightness so neighbouring
shades read as a *different* colour, not just a brighter or dimmer copy
of the bare one — exactly so multiple markers of the same family stay
distinguishable.

`k` (black) and `w` (white) have no shade ladder. Bare letters keep
their original meanings — every style string that worked before still
renders the same colour.

```bash
# Four series on one axis, two shapes × two shades — every series is
# visually distinct without legend-hunting.
./gnosis_vpn-bench plot a.txt b.txt c.txt d.txt \
    --style sr1 --style sr3 --style xr1 --style xr3
```

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

## Log verbosity

The top-level `--log-level` flag controls stdout verbosity. Choices:
`DEBUG`, `INFO` (default), `WARNING`, `ERROR`. The on-disk log file
under `logs/` always captures `DEBUG` regardless of this setting — the
flag only filters the terminal stream.

```bash
# Default — INFO and above on stdout
./gnosis_vpn-bench plot --boxes data/speed--TS.txt

# Verbose — dump every raw sample that fed each box, with its
# timestamp, alongside the burst summary stats
./gnosis_vpn-bench --log-level DEBUG plot --boxes data/speed--TS.txt
```

The DEBUG dump is structured as one line per box file, one indented
line per burst, and one further-indented line per raw sample — so you
can confirm each box visually by lining its anatomy up against the
listed samples.

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
