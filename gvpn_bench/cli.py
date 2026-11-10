"""
Gnosis VPN Speed Tester
=======================
Multiple test modes against Cloudflare's anycast speed-test infrastructure,
each accessed via a CLI subcommand:

  locations  — baseline + per-exit upload/download/latency (N runs each)
  repeated   — 6 × 10 MB download per exit (first immediately, then 60 s gaps)
  ramp       — download 50 KB → 500 KB → 5 MB → 50 MB per exit (60 s gaps)
  gap        — 13 × 10 MB download with increasing pauses (0 s → 55 s)

All modes share the same Cloudflare endpoint, VPN plumbing, and reporting
infrastructure.  Results are written to logs/ as .log, .txt, and .json.
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from .host import Host, LocalHost, SshHost
from .modes import Bench, cmd_gap, cmd_locations, cmd_ramp, cmd_repeated
from .monitor import (
    DEFAULT_BOX_GAP_S,
    DEFAULT_BOX_MIN_SAMPLES,
    DEFAULT_BOX_OUTLIER_SIGMA,
    DEFAULT_BOX_WIDTH_PX,
    DEFAULT_CURL_URL,
    DEFAULT_HEIGHT,
    DEFAULT_PING_HOST,
    DEFAULT_PING_LOAD_CURL_URL,
    DEFAULT_PING_LOAD_INTERVAL_MIN,
    DEFAULT_WIDTH,
    cmd_curl,
    cmd_ping,
    cmd_ping_load,
    cmd_plot,
)
from .profiles import load_target
from .runcli import add_run_commands
from .settings import DEFAULT_RUNS, DEFAULT_WAIT_BETWEEN_S, DEFAULT_WARMUP_S, LOG_DIR
from .vpn import VpnClient

log = logging.getLogger("gnosis_speedtest")

LOG_LEVEL_CHOICES = ["DEBUG", "INFO", "WARNING", "ERROR"]


def setup_logging(level: str, log_file: Path) -> None:
    """File always captures DEBUG; stdout defaults to INFO (``--log-level``)."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(getattr(logging, level))
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s.%(msecs)03d [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[stdout_handler, file_handler],
    )


def _ssh_host(destination: str, key: str | None) -> SshHost:
    user, _, address = destination.rpartition("@")
    return SshHost(
        name=address,
        address=address,
        user=user or "root",
        identity_file=Path(key).expanduser() if key else None,
        control_dir=Path(tempfile.mkdtemp(prefix="gvpn-bench-ssh-")),
    )


def _run_host(args: argparse.Namespace) -> tuple[Host, list[str], str]:
    """Host, ctl prefix and target exit for ``--run RUN --on CLIENT``."""
    from .remote.inventory import host_for, machine
    from .runs import load_user_config, open_run

    run = open_run(args.run_ref, load_user_config())
    host = host_for(run, machine(run, args.on))
    profile = run.config.profile()
    return host, profile.worker.ctl_prefix(), run.config.target().exit_address


def build_bench(args: argparse.Namespace, stamp: str, log_file: Path) -> Bench:
    if args.run_ref and args.ssh:
        raise SystemExit("use either --run/--on or --ssh, not both")
    host: Host = _ssh_host(args.ssh, args.ssh_key) if args.ssh else LocalHost()
    ctl_prefix = ["runuser", "-u", args.ctl_user, "--"] if args.ctl_user else []
    exit_address = args.exit
    dns_mode = args.dns
    if args.run_ref:
        if not args.on:
            raise SystemExit("--run needs --on CLIENT (e.g. client1)")
        host, ctl_prefix, exit_address = _run_host(args)
        dns_mode = "never"  # run VMs keep their resolver; see README
    if args.target:
        exit_address = load_target(args.target).exit_address
    return Bench(
        host=host,
        vpn=VpnClient(host=host, ctl_prefix=ctl_prefix, dns_mode=dns_mode),
        out_dir=LOG_DIR,
        stamp=stamp,
        log_file=log_file,
        live_file=Path(args.live_output) if args.live_output else None,
        exit_address=exit_address,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def step_name(args: argparse.Namespace) -> str:
    """``cmd_results_push`` → ``results push``: the name the run log ends a step with."""
    return args.run_func.__name__.removeprefix("cmd_").replace("_", " ")


def run_step(args: argparse.Namespace) -> int:
    """Run a step and always end with ``step <name> finished: exit <code>``.

    Log watchers stop on that line, ``step destroy finished`` being a run's last.
    """
    code: int | str | None = 1
    try:
        code = args.run_func(args)
    except SystemExit as error:
        code = error.code
        raise
    finally:
        print(f"step {step_name(args)} finished: exit {code if code is not None else 0}", flush=True)
    return code


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Gnosis VPN speed tester — multiple test modes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
modes:
  locations   Baseline + per-exit upload/download/latency (N runs each)
  repeated    6 × 10 MB download per exit (immediately, then 60 s gaps)
  ramp        Download 50 KB → 500 KB → 5 MB → 50 MB (60 s gaps)
  gap         13 × 10 MB download with increasing pauses (0 → 55 s)
  ping        Record ping latency to a host (one sample/sec)
  curl        Record curl download throughput
  plot        Render an SVG/PNG chart from one or two recordings
  ping-load   Continuous ping + periodic 10 MB curl bursts
""",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        dest="live_output",
        help="Write live JSON results to FILE (updated after every test; locations/repeated/ramp/gap)",
    )
    where = parser.add_argument_group("where to run (default: this machine)")
    where.add_argument("--ssh", metavar="[USER@]HOST", help="run every command on HOST over SSH instead of locally")
    where.add_argument("--ssh-key", metavar="FILE", help="SSH identity file for --ssh")
    where.add_argument(
        "--run",
        metavar="RUN",
        dest="run_ref",
        help="run on a VM of this run (with --on); uses the run's profile and target exit",
    )
    where.add_argument("--on", metavar="CLIENT", help="machine name within --run, e.g. client1")
    where.add_argument(
        "--ctl-user",
        metavar="USER",
        help="v5 per-user worker: run gnosis_vpn-ctl via `runuser -u USER --` (needs root on the host)",
    )
    which = parser.add_argument_group("which exits (default: all the client lists)")
    which.add_argument("--exit", metavar="ADDRESS", help="test only the exit with this address")
    which.add_argument("--target", metavar="NAME", help="test only targets/NAME.toml's exit")
    parser.add_argument(
        "--dns",
        choices=["auto", "always", "never"],
        default="auto",
        help="resolv.conf workaround after connecting: auto = only if a "
        "lookup fails (default); always = old behaviour; never. "
        "The original file is restored at exit.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=LOG_LEVEL_CHOICES,
        help="Stdout log verbosity (default: INFO). DEBUG "
        "additionally prints e.g. the raw samples that "
        "feed each box plot. The on-disk log file always "
        "captures DEBUG regardless.",
    )
    sub = parser.add_subparsers(dest="mode")

    p_loc = sub.add_parser("locations", help="Full location benchmark (baseline + VPN exits)")
    p_loc.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    p_loc.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_S)
    p_loc.add_argument("--wait", type=int, default=DEFAULT_WAIT_BETWEEN_S)
    p_loc.set_defaults(func=cmd_locations)

    p_rep = sub.add_parser("repeated", help="6 × 10 MB download (immediate + 60 s gaps)")
    p_rep.set_defaults(func=cmd_repeated)

    p_ramp = sub.add_parser("ramp", help="Download 50KB→500KB→5MB→50MB (60 s gaps)")
    p_ramp.set_defaults(func=cmd_ramp)

    p_gap = sub.add_parser("gap", help="13 × 10 MB with increasing pauses (0→55 s)")
    p_gap.set_defaults(func=cmd_gap)

    # ---- monitor recording / plotting (ported from gnosis_vpn-monitor) ----

    p_ping = sub.add_parser(
        "ping",
        help="record ping latency to a host (one sample/sec)",
        description="Record `ping -D <host>` to stdout and a file.",
    )
    p_ping.add_argument(
        "host", nargs="?", default=DEFAULT_PING_HOST, help=f"host to ping (default: {DEFAULT_PING_HOST})"
    )
    p_ping.add_argument(
        "-o",
        "--output",
        default=None,
        help="copy of the trace (default: data/ping--TIMESTAMP.txt; pass '' to skip the file copy)",
    )
    p_ping.set_defaults(func=cmd_ping)

    p_curl = sub.add_parser(
        "curl",
        help="record curl download throughput",
        description="Download a URL with curl and record its progress meter.",
    )
    p_curl.add_argument(
        "url", nargs="?", default=DEFAULT_CURL_URL, help=f"URL to download (default: {DEFAULT_CURL_URL})"
    )
    p_curl.add_argument(
        "-o",
        "--output",
        default=None,
        help="copy of the trace (default: data/curl--TIMESTAMP.txt; pass '' to skip the file copy)",
    )
    p_curl.set_defaults(func=cmd_curl)

    p_plot = sub.add_parser(
        "plot",
        help="render an SVG/PNG chart from one or more recordings",
        description="Render an SVG line chart from ping/curl recordings. "
        "Positional files default to the left y-axis; pass "
        "--right (repeatable) to add a second y-axis.",
    )
    p_plot.add_argument(
        "files",
        nargs="*",
        help="recording files for the left y-axis (shorthand for --left). All same-kind files share the axis.",
    )
    p_plot.add_argument(
        "--left",
        action="append",
        metavar="FILE",
        help="add a file to the left y-axis (repeatable). All left-axis files must be the same kind (ping or curl).",
    )
    p_plot.add_argument(
        "--right",
        action="append",
        metavar="FILE",
        help="add a file to the right y-axis (repeatable). "
        "Presence of --right makes the chart double-y; "
        "all right-axis files must be the same kind.",
    )
    p_plot.add_argument(
        "--boxes",
        action="append",
        metavar="FILE",
        help="render this curl/speed file as one box per "
        "burst (whisker = min/max, body = mean±σ, "
        "tick = median). Boxes land on the right "
        "axis when other data is present; if --boxes "
        "is the only input they ARE the chart.",
    )
    p_plot.add_argument(
        "--box-gap",
        type=float,
        default=DEFAULT_BOX_GAP_S,
        metavar="SECONDS",
        help=f"sample-gap threshold splitting bursts "
        f"(default: {DEFAULT_BOX_GAP_S} s — far above "
        f"any plausible intra-burst gap, far below the "
        f"30-min ping-load default)",
    )
    p_plot.add_argument(
        "--box-min-samples",
        type=int,
        default=DEFAULT_BOX_MIN_SAMPLES,
        metavar="N",
        help=f"drop bursts with fewer than N samples "
        f"(default: {DEFAULT_BOX_MIN_SAMPLES} — even "
        f"degenerate single-sample bursts are drawn)",
    )
    p_plot.add_argument(
        "--box-width",
        type=int,
        default=DEFAULT_BOX_WIDTH_PX,
        metavar="PIXELS",
        help=f"box width in pixels, fixed regardless of x-range (default: {DEFAULT_BOX_WIDTH_PX})",
    )
    p_plot.add_argument(
        "--box-outlier-sigma",
        type=float,
        default=DEFAULT_BOX_OUTLIER_SIGMA,
        metavar="N",
        help=f"σ multiplier defining the outlier threshold: "
        f"samples more than N×σ from the burst median "
        f"are flagged as outliers and drawn as "
        f"separate ``*`` markers (default: "
        f"{DEFAULT_BOX_OUTLIER_SIGMA}). The σ is "
        f"computed once on the original samples; the "
        f"filter does not iterate.",
    )
    p_plot.add_argument(
        "--box-show-dots",
        action="store_true",
        help="overlay each burst's raw samples as small "
        "translucent dots — handy for verifying the "
        "summary matches the data",
    )
    p_plot.add_argument(
        "--box-label",
        action="store_true",
        help="print N / start time / min / max / median / σ next to each box",
    )
    p_plot.add_argument("--log-y", action="store_true", help="logarithmic y-axis (applies to both sides)")
    p_plot.add_argument(
        "-o",
        "--output",
        default=None,
        help="SVG path. Default: output/{ping,curl,combined}-"
        "YYYY-MM-DD--HH-MM-SS.svg with a sibling .png. "
        "Pass '-' to write SVG to stdout.",
    )
    p_plot.add_argument(
        "--width", type=int, default=DEFAULT_WIDTH, help=f"SVG width in pixels (default: {DEFAULT_WIDTH})"
    )
    p_plot.add_argument(
        "--height", type=int, default=DEFAULT_HEIGHT, help=f"SVG height in pixels (default: {DEFAULT_HEIGHT})"
    )
    p_plot.add_argument("--png-scale", type=float, default=1.0, help="PNG dimension multiplier (default: 1.0)")
    p_plot.add_argument(
        "--style",
        action="append",
        default=None,
        metavar="STYLE",
        help="matplotlib-style format string per series (e.g. --style xb --style or)",
    )
    p_plot.add_argument("--legend", action="append", default=None, metavar="LABEL", help="legend entry per series")
    p_plot.add_argument(
        "--title",
        default=None,
        metavar="TEXT",
        help="chart title, centered above the plot frame in the same font/size as labels and legend",
    )
    p_plot.set_defaults(func=cmd_plot)

    p_pl = sub.add_parser(
        "ping-load",
        help="continuous ping + a 10 MB curl burst every N minutes",
        description="Run ping continuously while firing a periodic 10 MB curl "
        "download. Ping samples go to --ping-output (truncated at "
        "start). Each burst's curl progress is appended to "
        "--speed-output, with no separator lines between bursts, "
        "so the two files can be plotted together via "
        "`plot PING --right SPEED`.",
    )
    p_pl.add_argument("--host", default=DEFAULT_PING_HOST, help=f"ping host (default: {DEFAULT_PING_HOST})")
    p_pl.add_argument(
        "--url",
        default=DEFAULT_PING_LOAD_CURL_URL,
        help=f"curl URL for each burst (default: {DEFAULT_PING_LOAD_CURL_URL})",
    )
    p_pl.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_PING_LOAD_INTERVAL_MIN,
        help=f"minutes between curl bursts (default: {DEFAULT_PING_LOAD_INTERVAL_MIN})",
    )
    p_pl.add_argument("--ping-output", default=None, help="ping recording path (default: data/ping--TIMESTAMP.txt)")
    p_pl.add_argument(
        "--speed-output",
        default=None,
        help="curl-burst recording path, append-only (default: data/speed--TIMESTAMP.txt)",
    )
    p_pl.set_defaults(func=cmd_ping_load)

    add_run_commands(sub)

    args = parser.parse_args(argv)
    if args.mode is None:
        parser.print_help()
        sys.exit(0)
    if hasattr(args, "run_func"):
        logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(message)s")
        sys.exit(run_step(args))
    now = datetime.now()
    log_file = LOG_DIR / f"speedtest_{now:%Y%m%d_%H%M%S}.log"
    setup_logging(args.log_level, log_file)
    bench = build_bench(args, now.strftime("%Y-%m-%d--%H-%M-%S"), log_file)
    log.info("Gnosis VPN Speed Tester — mode: %s — host: %s — log: %s", args.mode, bench.host.name, log_file)
    try:
        args.func(bench, args)
    finally:
        bench.vpn.restore_dns()
    log.info("Done.")


if __name__ == "__main__":
    main()
