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
swap = re.search(r"SwapTotal:\s+(\d+) kB", open("/proc/meminfo").read())
disk = os.statvfs("/")
def cpu_int(field):
    value = cpu.get(field, "")
    return int(value) if value.isdigit() else None
print(json.dumps({
    "vcpus": os.cpu_count(),
    "memory_mib": round(int(mem.group(1)) / 1024) if mem else None,
    "cpu_model": cpu.get("Model name"),
    "os": os_release.get("PRETTY_NAME"),
    "kernel": platform.release(),
    "architecture": platform.machine(),
    "cpu_vendor": cpu.get("Vendor ID"),
    "cpu_sockets": cpu_int("Socket(s)"),
    "cores_per_socket": cpu_int("Core(s) per socket"),
    "threads_per_core": cpu_int("Thread(s) per core"),
    "hypervisor": cpu.get("Hypervisor vendor"),
    "root_disk_gib": round(disk.f_blocks * disk.f_frsize / 1024**3, 2),
    "root_free_gib": round(disk.f_bavail * disk.f_frsize / 1024**3, 2),
    "swap_mib": round(int(swap.group(1)) / 1024) if swap else None,
    "ctl_version": run("gnosis_vpn-ctl", "--version") or None,
    "package_version": run("dpkg-query", "-W", "-f=${Version}", "gnosisvpn") or None,
}))
PY
