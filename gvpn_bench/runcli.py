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


def _open(args: argparse.Namespace) -> tuple[Any, Run, Any, Any]:
    user = load_user_config()
    run = open_run(args.run, user)
    return user, run, run.config.profile(), run.config.target()


def cmd_provision(args: argparse.Namespace) -> int:
    from .remote import provision as prov
    from .remote import steps

    user, run, profile, _target = _open(args)
    prov.preflight(run, profile)
    print(prov.describe_plan(run, profile))
    if not confirm("Create these billable droplets?", args.yes):
        return 1
    steps.step_provision(user, run, profile)
    print(f"Provisioned. Next: fund {run.config.id}")
    return 0


def cmd_fund(args: argparse.Namespace) -> int:
    from .remote import steps

    user, run, profile, _target = _open(args)
    if run.done("fund"):
        print("already funded")
        return 0
    plan = steps.step_fund_plan(user, run, profile)
    print("Identities backed up. Funding requested by the clients:")
    for node in plan["nodes"]:
        print(f"  {node['name']}: {node['wxhopr']} wxHOPR + {node['xdai']} xDAI → {node['address']}")
    print(f"From the funding Safe in {user.funding_key_file} on {profile.network.name}.")
    if not confirm("Send these funds?", args.yes):
        return 1
    steps.step_fund_send(user, run, profile)
    print(f"Funded. Next: connect {run.config.id}")
    return 0


def cmd_connect(args: argparse.Namespace) -> int:
    from .remote import steps

    user, run, profile, target = _open(args)
    outcomes = steps.step_connect(user, run, profile, target)
    for outcome in outcomes:
        print(f"  {outcome.client}: {outcome.status} {outcome.detail}")
    return 0 if all(o.status == "connected" for o in outcomes) else 1


def cmd_ladder(args: argparse.Namespace) -> int:
    from .remote import steps

    _user, run, profile, target = _open(args)
    rungs = steps.step_ladder(run, profile, target)
    for rung in rungs:
        value = (
            rung["total_mbps"]
            if rung["total_mbps"] is not None
            else f"not measured ({rung['failure_status']})"
        )
        print(f"  rung {rung['rung']}: {value}")
    steps.step_result(run, profile, target)
    print(f"Wrote {run.result_file} and {run.report_file}")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    from .remote import steps

    _user, run, profile, target = _open(args)
    rows = steps.step_sweep(run, profile, args.exit or None)
    for row in rows:
        print(f"  {row['exit_label']:<16} {row['status']:<22} {row['detail'] or ''}")
    steps.step_result(run, profile, target)
    print(f"Wrote {run.result_file} and {run.report_file}")
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    from .remote import steps

    user, run, profile, _target = _open(args)
    if not (run.state / "funding.json").exists():
        print("this run was never funded; nothing to recover")
        return 0
    from .finance.recover import recovery_complete
    from .remote.inventory import clients

    if not recovery_complete(run, profile, [m.name for m in clients(run)]):
        print("This stops every client worker and guard, then starts closing all payment channels.")
        if not confirm("Stop the clients and start recovery?", args.yes):
            return 1
        steps.step_recover_initiate(user, run, profile)
    print("Finalizing: waits for the channel-closure grace period, then returns funds to the funding Safe.")
    if not confirm("Send the return transactions?", args.yes):
        return 1
    cleanup = steps.step_recover_finalize(user, run, profile)
    print(f"Recovered {cleanup['recovered_wxHOPR']} wxHOPR and {cleanup['recovered_xDAI']} xDAI.")
    print(f"Next: destroy {run.config.id}")
    return 0


def cmd_destroy(args: argparse.Namespace) -> int:
    from .remote import steps
    from .remote.inventory import load_inventory

    user, run, profile, _target = _open(args)
    inventory = load_inventory(run)
    if not inventory:
        print("no droplets in this run's inventory")
        return 0
    for entry in inventory:
        print(f"  {entry.name:<8} id {entry.id}")
    if not confirm("Delete these droplets (after re-verifying recovered funds)?", args.yes):
        return 1
    steps.step_destroy(user, run, profile)
    print("Deleted and verified gone.")
    return 0


def cmd_results_push(args: argparse.Namespace) -> int:
    from .results import push

    user = load_user_config()
    run_dir = Path(args.run).expanduser()
    if not (run_dir / "result.json").exists():
        run_dir = open_run(args.run, user).path
    repo = Path(args.repo).expanduser() if args.repo else user.results_repo
    if repo is None:
        print(
            "No results repository: pass --repo PATH or set [results] repo "
            "in ~/.config/gvpn-bench/config.toml"
        )
        return 2
    plan = push.prepare(run_dir, repo)
    print(f"Run       : {run_dir}")
    print(f"Repo      : {repo}")
    print(f"Target dir: {plan.destination.relative_to(repo)}")
    print(f"Files     : {', '.join(p.name for p in plan.files)}")
    if not plan.scan.clean:
        print("REFUSED — the scan found:")
        for problem in plan.scan.problems:
            print(f"  {problem}")
        return 1
    print("Scan      : clean (no private keys, tokens or VM/public IPs)")
    if args.dry_run:
        print("Dry run: nothing copied, committed or pushed.")
        return 0
    remote = push.check_results_repo(repo)
    if not confirm(f"Commit these files in {repo} and push to {remote}?", args.yes):
        print("Nothing copied, committed or pushed.")
        return 0
    if not push.copy_and_commit(plan, f"Add results {plan.destination.name}"):
        print("Nothing changed; no commit.")
    push.push(repo)
    print("Pushed.")
    return 0


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

    p = sub.add_parser("results", help="publish results (see `results push -h`)")
    results_sub = p.add_subparsers(dest="results_command", required=True)
    p = results_sub.add_parser(
        "push",
        help="copy result.json/report.html/provenance.json to the private results repo, scan, commit, push",
    )
    p.add_argument("run", help="run id, run directory, or any directory with a result.json")
    p.add_argument("--repo", metavar="PATH", help="local checkout of the results repo (default: user config)")
    p.add_argument("--dry-run", action="store_true", help="show files and scan result only")
    p.add_argument("--yes", action="store_true", help="push without asking")
    p.set_defaults(run_func=cmd_results_push)

    remote_steps = [
        ("provision", cmd_provision, "create and prepare the run's droplets (costs money)"),
        ("fund", cmd_fund, "back up identities, then fund each client from the funding Safe"),
        ("connect", cmd_connect, "start guards and heartbeat, baseline, connect every client to the target"),
        ("ladder", cmd_ladder, "run the 1→2→3 concurrent ladder; writes result.json and report.html"),
        ("sweep", cmd_sweep, "check every discovered exit once; writes result.json and report.html"),
        ("recover", cmd_recover, "stop clients, close channels, return funds (moves funds)"),
        ("destroy", cmd_destroy, "delete this run's droplets after re-verifying recovery"),
    ]
    for name, func, help_text in remote_steps:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("run", help="run id or directory")
        p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
        if name == "sweep":
            p.add_argument("--exit", action="append", metavar="ADDRESS", help="only this exit (repeatable)")
        p.set_defaults(run_func=func)
