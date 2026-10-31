"""Run-level subcommands: plan, status, report (and the remote steps).

Each subcommand takes a run (id or path). Steps that create VMs, move
funds or publish anything ask for confirmation unless ``--yes`` is given.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .profiles import CONFIG_ROOT, list_names
from .results.store import write_report
from .runs import STEPS, Run, check_private_file, load_user_config, open_run, plan_run


def confirm(prompt: str, assume_yes: bool) -> bool:
    """Ask on the terminal; refuse when there is no terminal to ask on."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(f"{prompt} — refusing without a terminal; pass --yes to confirm.", file=sys.stderr)
        return False
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def cmd_plan(args: argparse.Namespace) -> int:
    user = load_user_config()
    run = plan_run(
        user,
        args.profile,
        args.target,
        run_id=args.id,
        client_config=Path(args.client_config) if args.client_config else None,
        notes=args.notes,
    )
    profile = run.config.profile()
    target = run.config.target()
    print(f"Planned run {run.config.id}")
    print(f"  directory : {run.path}")
    print(f"  profile   : {profile.name} ({profile.worker.kind}, {profile.package.channel})")
    print(f"  target    : {target.label} {target.exit_address} on {target.network}")
    if not profile.package.pinned:
        print("  WARNING   : profile has no package pin; provision will refuse until it is pinned")
    infra = run.config.infra
    print(
        f"  machines  : 1 origin ({infra.origin_size}) + {infra.clients} clients ({infra.client_size}) "
        f"in {infra.provider} {infra.region}; deadline {infra.deadline_hours} h"
    )
    print(f"Edit {run.path / 'run.toml'} to change sizes or the ladder, then: provision {run.config.id}")
    return 0


def _read_state(run: Run, name: str) -> Any:
    path = run.state / name
    return json.loads(path.read_text()) if path.exists() else None


def _yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


def cmd_status(args: argparse.Namespace) -> int:
    user = load_user_config()
    run = open_run(args.run, user)
    config = run.config
    print(f"Run {config.id}  ({run.path})")
    print(f"  profile {config.profile_name}, target {config.target_name}")
    steps = run.steps()
    print("Steps:")
    for step in STEPS:
        entry = steps.get(step)
        mark = f"done {entry['done_at']}" if entry else "-"
        print(f"  {step:<10} {mark}")
    inventory = _read_state(run, "inventory.json") or []
    deleted = (_read_state(run, "cleanup.json") or {}).get("destroyed_at_utc")
    print("VMs:" if inventory else "VMs: none provisioned")
    for entry in inventory:
        state = "deleted" if deleted else "live (as of last step; `destroy` verifies with the API)"
        print(f"  {entry['name']:<8} id {entry['id']}  {entry['size']}  {entry.get('region', '')}  {state}")
    funding = _read_state(run, "funding.json") or []
    if funding:
        recovered = _read_state(run, "cleanup.json")
        print("Funds:")
        for node in funding:
            print(
                f"  {node['name']}: sent {node['wxhopr']} wxHOPR + {node['xdai']} xDAI to {node['address']}"
            )
        if recovered:
            print(
                f"  recovered {recovered.get('recovered_wxHOPR')} wxHOPR, "
                f"{recovered.get('recovered_xDAI')} xDAI to the funding Safe"
            )
        else:
            print("  NOT recovered yet: funds are held by the client nodes/Safes and open channels")
    print(f"Result: {_yes_no(run.result_file.exists())}  Report: {_yes_no(run.report_file.exists())}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    user = load_user_config()
    run_dir = Path(args.run).expanduser()
    if not (run_dir / "result.json").exists():
        run_dir = open_run(args.run, user).path
    output = write_report(run_dir, Path(args.output) if args.output else None)
    print(f"Wrote {output}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Show where bench looks for credentials and runs; check file modes."""
    user = load_user_config()
    print(f"user config      : {user.source or '(none; defaults in use)'}")
    print(f"runs dir         : {user.runs_dir}")
    print(f"identity backups : {user.identity_backup_dir}")
    print(f"results repo     : {user.results_repo or '(not set)'}")
    status = 0
    for label, path in (
        ("DigitalOcean token", user.digitalocean_token_file),
        ("funding key", user.funding_key_file),
    ):
        problems = check_private_file(path)
        print(f"{label:<17}: {path}  {'ok' if not problems else '; '.join(problems)}")
        status |= bool(problems)
    print(f"profiles         : {', '.join(list_names(CONFIG_ROOT / 'profiles'))}")
    print(f"targets          : {', '.join(list_names(CONFIG_ROOT / 'targets'))}")
    return status


def add_run_commands(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("plan", help="create a run directory with run.toml (no spending)")
    p.add_argument("--profile", required=True, help="profiles/<name>.toml")
    p.add_argument("--target", required=True, help="targets/<name>.toml")
    p.add_argument("--id", help="run id (default: <UTC time>-<target>-<profile>)")
    p.add_argument("--client-config", metavar="FILE", help="client config.toml to install on the VMs")
    p.add_argument("--notes", default="", help="free text stored in run.toml and result.json")
    p.set_defaults(run_func=cmd_plan)

    p = sub.add_parser("status", help="steps done, VMs, where funds are")
    p.add_argument("run", help="run id or directory")
    p.set_defaults(run_func=cmd_status)

    p = sub.add_parser("report", help="render RUN/result.json as self-contained HTML")
    p.add_argument("run", help="run id, run directory, or any directory with a result.json")
    p.add_argument("-o", "--output", metavar="FILE", help="default: RUN/report.html")
    p.set_defaults(run_func=cmd_report)

    p = sub.add_parser("config", help="show credential and run locations; check file modes")
    p.set_defaults(run_func=cmd_config)
