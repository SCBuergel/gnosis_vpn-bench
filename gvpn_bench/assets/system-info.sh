#!/bin/bash
# Print machine facts as JSON for result.json provenance. No arguments.
set -euo pipefail
python3 - <<'PY'
import json, os, platform, re, subprocess
def run(*argv):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
os_release = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', open("/etc/os-release").read(), re.M))
cpu = {f["field"].rstrip(":"): f["data"] for f in json.loads(run("lscpu", "-J") or '{"lscpu": []}')["lscpu"]}
mem = re.search(r"MemTotal:\s+(\d+) kB", open("/proc/meminfo").read())
print(json.dumps({
    "vcpus": os.cpu_count(),
    "memory_mib": round(int(mem.group(1)) / 1024) if mem else None,
    "cpu_model": cpu.get("Model name"),
    "os": os_release.get("PRETTY_NAME"),
    "kernel": platform.release(),
    "ctl_version": run("gnosis_vpn-ctl", "--version") or None,
    "package_version": run("dpkg-query", "-W", "-f=${Version}", "gnosisvpn") or None,
}))
PY
