"""
All tunable constants and derived path/URL values for the Gnosis VPN speed tester.
"""

from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Test timing
# ---------------------------------------------------------------------------

DEFAULT_RUNS: int = 5
DEFAULT_WARMUP_S: int = 10
DEFAULT_WAIT_BETWEEN_S: int = 5

PAUSE_BETWEEN_RUNS_S: int = 10
PAUSE_BETWEEN_LOCS_S: int = 3

CONNECTION_TIMEOUT_S: int = 60
POLL_INTERVAL_S: float = 1.0

# ---------------------------------------------------------------------------
# Cloudflare endpoints
# ---------------------------------------------------------------------------

CF_DL_URL_FMT: str = "https://speed.cloudflare.com/__down?bytes={}"
CF_DL_10MB:    str = CF_DL_URL_FMT.format(10 * 1024 * 1024)
CF_DL_100MB:   str = CF_DL_URL_FMT.format(100 * 1024 * 1024)
CF_UPLOAD:     str = "https://speed.cloudflare.com/__up"
CF_PROBE:      str = CF_DL_URL_FMT.format(1)

# ---------------------------------------------------------------------------
# curl limits
# ---------------------------------------------------------------------------

CURL_MAX_TIME_S: int = 60
CURL_UPLOAD_SIZE_BYTES: int = 10 * 1024 * 1024
LATENCY_TIMEOUT_S: int = 15

# ---------------------------------------------------------------------------
# Mode-specific constants
# ---------------------------------------------------------------------------

BASELINE_LOCATION_ID: str = "__baseline__"

REPEATED_COUNT: int = 6
REPEATED_GAP_S: int = 60

RAMP_SIZES: list[int] = [
    50 * 1024,          # 50 KB
    500 * 1024,         # 500 KB
    5 * 1024 * 1024,    # 5 MB
    50 * 1024 * 1024,   # 50 MB
]
RAMP_GAP_S: int = 60
RAMP_100MB_TIMEOUT_S: int = 600  # 10 min for the 50 MB download

GAP_WARMUP_S: int = 60
GAP_SCHEDULE: list[int] = [0, 0, 5] + list(range(10, 60, 5))  # 13 entries

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
LOG_DIR = SCRIPT_DIR / "logs"
_run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
LOG_FILE = LOG_DIR / f"speedtest_{_run_ts}.log"
