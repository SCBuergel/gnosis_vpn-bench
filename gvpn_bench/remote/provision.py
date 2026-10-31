"""Create and prepare a run's VMs: one nginx origin and N VPN clients.

Order matters for safety:

1. Refuse if the run already has an inventory or the profile is unpinned.
2. Schedule the deadline deletion timer *before* creating anything, so a
   billing cap exists even if this process dies right after a create.
3. Create droplets one by one, saving the inventory after each.
4. Wait for SSH and cloud-init, then install origin and clients in parallel.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import hashlib
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from ..host import Host, SshHost, run_script, write_file
from ..profiles import Profile
from ..runs import Run, utc_now, write_private_json
from ..status import start_client_accepted
from ..vpn import CtlError
from . import deadline
from .assets import asset, asset_bytes
from .connect import vpn_for
from .digitalocean import Api, public_ipv4, run_tag
from .inventory import Machine, host_for, load_inventory, save_inventory, ssh_key_file

log = logging.getLogger("gnosis_speedtest")

READY_MARKER = "/var/lib/gvpn-bench-ready"
REMOTE_DIR = "/root/gvpn-bench"
PACKAGES = ["curl", "jq", "tmux", "iputils-ping", "nftables", "python3", "nginx", "chrony"]
_NAME_SAFE = re.compile(r"[^a-z0-9.-]")


def cloud_config(public_key: str) -> str:
    """cloud-init user data. JSON is valid YAML, so no text templating is needed."""
    config = {
        "ssh_authorized_keys": [public_key],
        # DigitalOcean images force a root password change on first login, which
        # makes non-interactive SSH fail. bootcmd runs before package installs.
        "bootcmd": [["sh", "-c", 'chage -d "$(date +%F)" -M 99999 root']],
        "package_update": True,
        "packages": PACKAGES,
        "runcmd": [
            ["systemctl", "stop", "nginx"],
            ["systemctl", "enable", "--now", "chrony"],
            ["touch", READY_MARKER],
        ],
    }
    return "#cloud-config\n" + json.dumps(config, indent=1) + "\n"


def droplet_name(run_id: str, machine: str) -> str:
    return _NAME_SAFE.sub("-", f"gvpn-bench-{run_id[-40:]}-{machine}".lower())


@dataclass(frozen=True)
class Plan:
    name: str
    role: str
    size: str


def machine_plan(run: Run) -> list[Plan]:
    infra = run.config.infra
    plans = [Plan("origin", "origin", infra.origin_size)]
    plans += [Plan(f"client{i}", "client", infra.client_size) for i in range(1, infra.clients + 1)]
    return plans


def preflight(run: Run, profile: Profile) -> None:
    if load_inventory(run):
        raise RuntimeError("this run already has an inventory; refusing to provision twice")
    if not profile.package.pinned:
        raise RuntimeError(f"profile {profile.name} has no package pin (version, url, sha256)")
    if run.config.client_config is not None:
        if not run.config.client_config.exists():
            raise FileNotFoundError(run.config.client_config)
        if profile.client_config_path is None:
            raise RuntimeError(f"run has a client_config but profile {profile.name} sets no client_config_path")
    if run.config.infra.provider != "digitalocean":
        raise RuntimeError(f"provider {run.config.infra.provider!r} is not supported")


def make_ssh_key(run: Run) -> str:
    key = ssh_key_file(run)
    if not key.exists():
        subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                f"gvpn-bench {run.config.id}",
                "-f",
                str(key),
            ],
            check=True,
        )
    return key.with_suffix(".pub").read_text().strip()


def create_machines(run: Run, api: Api, public_key: str) -> list[Machine]:
    infra = run.config.infra
    tag = run_tag(run.config.id)
    machines: list[Machine] = []
    for plan in machine_plan(run):
        droplet = api.create_droplet(
            name=droplet_name(run.config.id, plan.name),
            region=infra.region,
            size=plan.size,
            image=infra.image,
            user_data=cloud_config(public_key),
            tags=[tag],
        )
        machines.append(
            Machine(
                name=plan.name,
                role=plan.role,
                id=int(droplet["id"]),
                size=plan.size,
                region=infra.region,
                created_at=str(droplet.get("created_at", utc_now())),
            )
        )
        save_inventory(run, machines)
        log.info("created %s (%s, id %s)", plan.name, plan.size, droplet["id"])
    return machines


def wait_for_addresses(run: Run, api: Api, machines: list[Machine], timeout_s: float = 360) -> None:
    deadline_at = time.monotonic() + timeout_s
    for machine in machines:
        while True:
            droplet = api.get_droplet(machine.id)
            ip = public_ipv4(droplet) if droplet else None
            if droplet and droplet["status"] == "active" and ip:
                machine.ip = ip
                save_inventory(run, machines)
                break
            if time.monotonic() > deadline_at:
                raise TimeoutError(f"{machine.name} did not become active")
            time.sleep(3)


def wait_ready(host: SshHost, timeout_s: float = 600) -> None:
    """Poll for cloud-init's marker with fresh connections.

    A multiplexed master opened while the root password was still expired
    would keep that state for every later command, so probing never reuses one.
    """
    probe = dataclasses.replace(host, control_dir=None)
    deadline_at = time.monotonic() + timeout_s
    while time.monotonic() < deadline_at:
        if probe.run(["test", "-f", READY_MARKER], timeout_s=20).ok:
            log.info("%s ready", host.name)
            return
        time.sleep(5)
    raise TimeoutError(f"{host.name}: cloud-init did not finish")


def upload_tools(host: Host) -> None:
    host.run(["mkdir", "-p", REMOTE_DIR], timeout_s=20)
    for name in ("ssh-keeper.sh", "deadman.sh", "ladder_agent.py"):
        write_file(host, f"{REMOTE_DIR}/{name}", asset_bytes(name), mode="700")


def prepare_origin(run: Run, host: Host) -> dict[str, Any]:
    upload_tools(host)
    result = run_script(host, asset("setup-origin.sh"), [str(run.config.ladder.transfer_bytes)], timeout_s=300)
    if not result.ok:
        raise RuntimeError(f"origin setup failed: {result.stderr.strip()[-400:]}")
    sha256 = result.stdout.strip().splitlines()[-1]
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise RuntimeError(f"origin setup returned no checksum: {result.stdout[-200:]!r}")
    origin = {"bytes": run.config.ladder.transfer_bytes, "sha256": sha256}
    write_private_json(run.state / "origin.json", origin)
    return origin


def prepare_client(run: Run, profile: Profile, host: Host) -> None:
    upload_tools(host)
    worker = profile.worker
    package = profile.package
    args = [
        package.channel,
        profile.network.name,
        profile.network.indexer,
        str(package.url),
        str(package.sha256),
        str(package.version),
        worker.kind,
        worker.user,
        worker.home,
    ]
    for key, value in profile.sysctl:
        applied = host.run(["sysctl", "-w", f"{key}={value}"], timeout_s=20)
        if not applied.ok:
            raise RuntimeError(f"{host.name}: sysctl {key}={value} failed: {applied.stderr.strip()[:200]}")
    result = run_script(host, asset("install-client.sh"), args, timeout_s=900)
    (run.state / f"{host.name}-install.log").write_text(result.stdout + "\n--- stderr ---\n" + result.stderr)
    if not result.ok:
        raise RuntimeError(f"{host.name}: client install failed: {result.stderr.strip()[-400:]}")
    if run.config.client_config is not None and profile.client_config_path is not None:
        payload = run.config.client_config.read_bytes()
        write_file(host, profile.client_config_path, payload, mode="644")
        write_private_json(
            run.state / "client-config.json",
            {"source": str(run.config.client_config), "sha256": hashlib.sha256(payload).hexdigest()},
        )
        restarted = host.run(["systemctl", "restart", "gnosisvpn"], timeout_s=60)
        if not restarted.ok:
            raise RuntimeError(f"{host.name}: restart after config upload failed")
        # The restart stops the worker that install-client.sh started; start it again.
        start_worker(host, profile)


def start_worker(host: Host, profile: Profile, attempts: int = 30, pause_s: float = 2.0) -> None:
    """``start-client`` until the service accepts it (it needs a moment after a restart)."""
    vpn = vpn_for(host, profile)
    last = ""
    for _ in range(attempts):
        try:
            response = vpn.ctl("start-client", "30m", timeout_s=45)
            if start_client_accepted(response):
                return
            last = json.dumps(response)[:200]
        except CtlError as error:
            last = str(error)[:200]
        time.sleep(pause_s)
    raise RuntimeError(f"{host.name}: worker did not start after the config upload: {last}")


def client_config_sha256(run: Run) -> str | None:
    """Hash of the config actually uploaded (recorded at upload time)."""
    record = run.state / "client-config.json"
    if not record.exists():
        return None
    return json.loads(record.read_text())["sha256"]


def system_info(host: Host) -> dict[str, Any]:
    result = run_script(host, asset("system-info.sh"), timeout_s=60)
    if not result.ok:
        raise RuntimeError(f"{host.name}: system-info failed: {result.stderr.strip()[-200:]}")
    info = json.loads(result.stdout)
    version = info.get("ctl_version") or ""
    info["ctl_version"] = version.split()[-1] if version else None
    return info


def provision(run: Run, profile: Profile, api: Api) -> list[Machine]:
    preflight(run, profile)
    public_key = make_ssh_key(run)
    deadline.schedule(run)
    machines = create_machines(run, api, public_key)
    wait_for_addresses(run, api, machines)
    hosts = {m.name: host_for(run, m) for m in machines}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        list(pool.map(wait_ready, hosts.values()))
        jobs = [pool.submit(prepare_origin, run, hosts["origin"])]
        jobs += [pool.submit(prepare_client, run, profile, hosts[m.name]) for m in machines if m.role == "client"]
        for job in concurrent.futures.as_completed(jobs):
            job.result()
        infos = dict(zip(hosts, pool.map(system_info, hosts.values()), strict=True))
    machine_facts = [{"role": m.role, "name": m.name, "size": m.size, **infos[m.name]} for m in machines]
    write_private_json(run.state / "machines.json", machine_facts)
    run.mark("provision", droplet_ids=[m.id for m in machines])
    return machines


def describe_plan(run: Run, profile: Profile) -> str:
    infra = run.config.infra
    lines = [f"Provision for run {run.config.id}:"]
    for plan in machine_plan(run):
        lines.append(f"  {plan.name:<8} {plan.size:<6} {infra.provider} {infra.region}")
    lines.append(f"  package {profile.package.version} ({profile.package.channel}) on {profile.network.name}")
    lines.append(f"  deletion timer: {infra.deadline_hours} h from now (skips funded runs without identity backups)")
    return "\n".join(lines)
