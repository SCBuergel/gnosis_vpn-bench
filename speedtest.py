#!/usr/bin/env python3
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

import argparse
import logging
import sys

from config import DEFAULT_RUNS, DEFAULT_WARMUP_S, DEFAULT_WAIT_BETWEEN_S, LOG_DIR, LOG_FILE
from modes import cmd_gap, cmd_locations, cmd_ramp, cmd_repeated

# ---------------------------------------------------------------------------
# Logging — must be configured before any other module emits log records
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(exist_ok=True)

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
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gnosis VPN speed tester — multiple test modes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
modes:
  locations   Baseline + per-exit upload/download/latency (N runs each)
  repeated    6 × 10 MB download per exit (immediately, then 60 s gaps)
  ramp        Download 50 KB → 500 KB → 5 MB → 50 MB (60 s gaps)
  gap         13 × 10 MB download with increasing pauses (0 → 55 s)
""",
    )
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="Write live JSON results to FILE (updated after every test)")
    sub = parser.add_subparsers(dest="mode")

    p_loc = sub.add_parser("locations", help="Full location benchmark (baseline + VPN exits)")
    p_loc.add_argument("--runs",   type=int, default=DEFAULT_RUNS)
    p_loc.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_S)
    p_loc.add_argument("--wait",   type=int, default=DEFAULT_WAIT_BETWEEN_S)
    p_loc.set_defaults(func=cmd_locations)

    p_rep = sub.add_parser("repeated", help="6 × 10 MB download (immediate + 60 s gaps)")
    p_rep.set_defaults(func=cmd_repeated)

    p_ramp = sub.add_parser("ramp", help="Download 50KB→500KB→5MB→50MB (60 s gaps)")
    p_ramp.set_defaults(func=cmd_ramp)

    p_gap = sub.add_parser("gap", help="13 × 10 MB with increasing pauses (0→55 s)")
    p_gap.set_defaults(func=cmd_gap)

    args = parser.parse_args()
    if args.mode is None:
        parser.print_help()
        sys.exit(0)
    log.info("Gnosis VPN Speed Tester — mode: %s — log: %s", args.mode, LOG_FILE)
    args.func(args)
    log.info("Done.")


if __name__ == "__main__":
    main()
