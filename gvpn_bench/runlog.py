"""The run log: ``RUN/logs/run.log``, one file a watcher can follow for a whole run.

Every run step appends its log records and everything it prints, line by
line, with a UTC timestamp on log records. It is the file the runbook's
"run log" lines (``rung N: …``, ``step <name> finished: exit <code>``)
are read from. It stays in the run directory, outside the repository, and
is never published: it holds VM addresses.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

RUN_LOG = Path("logs") / "run.log"


class Tee:
    """A text stream that writes to *primary* and copies each write to *copy*."""

    def __init__(self, primary: TextIO, copy: TextIO):
        self.primary = primary
        self.copy = copy

    def write(self, text: str) -> int:
        self.copy.write(text)
        return self.primary.write(text)

    def flush(self) -> None:
        self.copy.flush()
        self.primary.flush()

    def __getattr__(self, name: str):
        return getattr(self.primary, name)


def _utc_formatter() -> logging.Formatter:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%SZ")
    formatter.converter = time.gmtime
    return formatter


@contextmanager
def run_log(run_dir: Path | None, level: int = logging.INFO) -> Iterator[Path | None]:
    """Line-buffer stdout and, for a known run, copy logs and prints to its run log."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(line_buffering=True)
    if run_dir is None:
        yield None
        return
    path = run_dir / RUN_LOG
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stream = path.open("a", encoding="utf-8", buffering=1)
    handler = logging.StreamHandler(stream)
    handler.setLevel(level)
    handler.setFormatter(_utc_formatter())
    root = logging.getLogger()
    root_level = root.level
    root.setLevel(min(root_level, level))
    root.addHandler(handler)
    original = sys.stdout
    sys.stdout = Tee(original, stream)  # type: ignore[assignment]
    try:
        yield path
    finally:
        sys.stdout = original
        root.removeHandler(handler)
        root.setLevel(root_level)
        stream.close()
