#!/usr/bin/env python3
"""Synchronised download with per-second ping and host samples.

Uploaded to each client VM by ``gnosis_vpn-bench`` and started with
``systemd-run`` so it survives an SSH drop. Standalone: stdlib only, no
imports from the bench package.

Usage::

    ladder_agent.py download CONFIG.json   # one ladder transfer
    ladder_agent.py monitor OUTPUT.jsonl SECONDS   # host samples only (origin)

CONFIG keys: ``start_at`` (UTC epoch shared by every client in the rung),
``deadline_s``, ``bytes`` (expected size), ``url``, ``output`` (new dir),
``ping_target``.

The agent waits for ``start_at`` using a monotonic clock anchored to wall
time, so every client starts within milliseconds of the others (given
chrony-synchronised clocks). It records the cumulative byte count at each
read (``events``) so the operator can compute rates over any interval
afterwards, and writes ``result.json`` as soon as the transfer ends so the
next rung is not delayed by outstanding pings.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

BARRIER_TOLERANCE_S = 0.1
READ_CHUNK = 65536


def system_sample() -> dict[str, Any]:
    cpu = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    meminfo = dict(re.findall(r"^(\w+):\s+(\d+)", Path("/proc/meminfo").read_text(), re.M))
    return {
        "cpu": [int(v) for v in cpu],
        "mem_available_kib": int(meminfo["MemAvailable"]),
        "load1": os.getloadavg()[0],
    }


def ping_once(target: str, sent_at: float) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["ping", "-n", "-c", "1", "-W", "5", "-w", "6", target],
            capture_output=True,
            text=True,
            timeout=8,
        )
        match = re.search(r"time[=<]([\d.]+) ms", completed.stdout)
    except subprocess.TimeoutExpired:
        match = None
    return {"sent_at": sent_at, "rtt_ms": float(match.group(1)) if match else None}


class Clock:
    """Wall-clock epoch derived from a monotonic clock (immune to steps)."""

    def __init__(self) -> None:
        self.wall = time.time()
        self.mono = time.monotonic()

    def now(self) -> float:
        return self.wall + time.monotonic() - self.mono


def wait_for(clock: Clock, start_at: float) -> float:
    while clock.now() < start_at:
        time.sleep(min(0.01, max(0.0, start_at - clock.now())))
    begin = clock.now()
    if begin - start_at > BARRIER_TOLERANCE_S:
        raise RuntimeError(f"missed the synchronised start by {begin - start_at:.3f} s")
    return begin


def download(config: dict[str, Any]) -> dict[str, Any]:
    out = Path(config["output"])
    out.mkdir(parents=True, exist_ok=False)
    expected = int(config["bytes"])
    clock = Clock()
    start_at = float(config["start_at"])
    deadline = start_at + float(config["deadline_s"])
    begin = wait_for(clock, start_at)

    command = [
        "curl",
        "--silent",
        "--show-error",
        "--fail",
        "--noproxy",
        "*",
        "--connect-timeout",
        "20",
        "--max-time",
        f"{max(0.01, deadline - begin):.3f}",
        "--header",
        "Accept-Encoding: identity",
        "--dump-header",
        str(out / "headers.txt"),
        config["url"],
    ]
    with (out / "curl-error.txt").open("w") as error_file:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_file)

        def stop(signum: int, _frame: object) -> None:
            proc.terminate()
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGHUP, stop)
        assert proc.stdout is not None
        os.set_blocking(proc.stdout.fileno(), False)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        digest = hashlib.sha256()
        total = 0
        events: list[list[float]] = [[begin, 0]]
        samples = [{"at": begin, "bytes": 0, **system_sample()}]
        first_byte: float | None = None
        last_byte: float | None = None
        timed_out = False
        next_sample = start_at + 1
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        pings = [pool.submit(ping_once, config["ping_target"], begin)]
        try:
            eof = False
            while not eof:
                now = clock.now()
                if now >= deadline:
                    timed_out = True
                    proc.terminate()
                    break
                if now >= next_sample:
                    samples.append({"at": now, "bytes": total, **system_sample()})
                    pings.append(pool.submit(ping_once, config["ping_target"], now))
                    next_sample += 1
                wait = min(0.05, max(0.0, next_sample - clock.now()))
                for _key, _mask in selector.select(timeout=wait):
                    try:
                        block = os.read(proc.stdout.fileno(), READ_CHUNK)
                    except BlockingIOError:
                        continue
                    at = clock.now()
                    if not block:
                        eof = True
                        break
                    if first_byte is None:
                        first_byte = at
                        events.append([at, 0])
                    last_byte = at
                    total += len(block)
                    digest.update(block)
                    events.append([at, total])
            finish = min(clock.now(), deadline)
            try:
                code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait(timeout=5)
            samples.append({"at": finish, "bytes": total, **system_sample()})
            result = {
                "scheduled_start": start_at,
                "start": begin,
                "end": finish,
                "first_byte": first_byte,
                "last_byte": last_byte,
                "bytes": total,
                "expected_bytes": expected,
                "sha256": digest.hexdigest(),
                "curl_exit": code,
                "timed_out": timed_out or code == 28,
                "complete": code == 0 and total == expected,
                "events": events,
                "samples": samples,
                "pings": [job.result() for job in pings if job.done()],
            }
            write_json(out / "result.json", result)
            # Publish the end first, then wait for outstanding pings, so slow
            # ICMP replies cannot delay the next rung's barrier.
            write_json(out / "pings.json", [job.result(timeout=10) for job in pings])
            return result
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            pool.shutdown(wait=True)
            selector.close()
            proc.stdout.close()


def monitor(output: Path, seconds: int) -> None:
    with output.open("w") as stream:
        for _ in range(seconds):
            stream.write(json.dumps({"at": time.time(), **system_sample()}) + "\n")
            stream.flush()
            time.sleep(1)


def write_json(path: Path, payload: Any) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "download":
        result = download(json.loads(Path(argv[2]).read_text()))
        summary = {k: v for k, v in result.items() if k not in {"events", "samples", "pings"}}
        print(json.dumps(summary), flush=True)
        return 0
    if len(argv) == 4 and argv[1] == "monitor":
        monitor(Path(argv[2]), int(argv[3]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
