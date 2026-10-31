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

from .profiles import CONFIG_ROOT, list_names, load_target
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
    targets = run.config.targets()
    print(f"Planned run {run.config.id}")
    print(f"  directory : {run.path}")
    print(f"  profile   : {profile.name} ({profile.worker.kind}, {profile.package.channel})")
    for target in targets:
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
    print(f"  profile {config.profile_name}, targets {', '.join(config.target_names)}")
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
            print(f"  {node['name']}: sent {node['wxhopr']} wxHOPR + {node['xdai']} xDAI to {node['address']}")
        if recovered:
            print(
                f"  recovered {recovered.get('recovered_wxHOPR')} wxHOPR, "
                f"{recovered.get('recovered_xDAI')} xDAI to the funding Safe"
            )
        else:
            print("  NOT recovered yet: funds are held by the client nodes/Safes and open channels")
    for name in config.target_names:
        directory = run.path / "results" / name
        print(
            f"Result {name}: {_yes_no((directory / 'result.json').exists())}  "
            f"report: {_yes_no((directory / 'report.html').exists())}"
        )
    return 0


def result_dirs(ref: str) -> list[Path]:
    """Directories holding a result.json: the given one, or each target of a run."""
    direct = Path(ref).expanduser()
    if (direct / "result.json").exists():
        return [direct]
    run = open_run(ref, load_user_config())
    found = sorted(p.parent for p in (run.path / "results").glob("*/result.json"))
    if not found:
        raise FileNotFoundError(f"run {run.config.id} has no results yet")
    return found


def cmd_report(args: argparse.Namespace) -> int:
    directories = result_dirs(args.run)
    if args.output and len(directories) > 1:
        print("-o needs a single result; this run has one per target")
        return 2
    for directory in directories:
        output = write_report(directory, Path(args.output) if args.output else None)
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
    """User config, run, profile and the selected target (``--target``, default: first)."""
    user = load_user_config()
    run = open_run(args.run, user)
    name = getattr(args, "target", None) or run.config.target_name
    if name not in run.config.target_names:
        raise SystemExit(f"{name} is not one of this run's targets: {', '.join(run.config.target_names)}")
    return user, run, run.config.profile(), load_target(name)


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
        value = rung["total_mbps"] if rung["total_mbps"] is not None else f"not measured ({rung['failure_status']})"
        print(f"  rung {rung['rung']}: {value}")
    directory = steps.step_result(run, profile, target)
    print(f"Wrote {directory / 'result.json'} and {directory / 'report.html'}")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    from .remote import steps

    _user, run, profile, target = _open(args)
    rows = steps.step_sweep(run, profile, args.exit or None)
    for row in rows:
        print(f"  {row['exit_label']:<16} {row['status']:<22} {row['detail'] or ''}")
    directory = steps.step_result(run, profile, target)
    print(f"Wrote {directory / 'result.json'} and {directory / 'report.html'}")
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


def _results_repo(args: argparse.Namespace) -> Path | None:
    repo = Path(args.repo).expanduser() if args.repo else load_user_config().results_repo
    if repo is None:
        print("No results repository: pass --repo PATH or set [results] repo in ~/.config/gvpn-bench/config.toml")
    return repo


def _publish(args: argparse.Namespace, repo: Path, staged: list[Any], message: str) -> int:
    """Scan, show, ask, write, index, commit, push. Shared by every results command."""
    from .results import publish, push

    staged = [*staged, publish.index_staged(repo, staged)]
    print(f"Repo: {repo}")
    for item in staged:
        print(f"  {item.folder or '(index)'}/: {len(item.files)} files")
    problems = publish.scan(staged)
    if problems:
        for problem in problems:
            print(f"REFUSED: {problem}")
        return 1
    print("Scan clean (no private keys, tokens or VM/public IPs).")
    if args.dry_run:
        print("Dry run: nothing written, committed or pushed.")
        return 0
    remote = push.check_results_repo(repo)
    if not confirm(f"Write these files in {repo}, commit and push to {remote}?", args.yes):
        print("Nothing written, committed or pushed.")
        return 0
    written = publish.write(repo, staged)
    if not push.commit(repo, written, message, update=publish.folders(staged)):
        print("Nothing changed; no commit, nothing pushed.")
        return 0
    push.push(repo)
    print("Pushed.")
    return 0


def cmd_results_push(args: argparse.Namespace) -> int:
    """Recordings of one or more runs plus one report over all of them.

    The report is multi-location when the recordings cover several exits,
    and shows error bars where the same exit and client were recorded again.
    """
    from .results import publish
    from .results.collection import Recording

    repo = _results_repo(args)
    if repo is None:
        return 2
    directories = [directory for ref in args.run for directory in result_dirs(ref)]
    staged = [publish.stage_recording(directory) for directory in directories]
    recordings = [Recording(item.folder.split("/", 1)[1], json.loads(item.files["result.json"])) for item in staged]
    links = dict(args.link or [])
    try:
        staged.append(publish.stage_report(recordings, name=args.report_name, title=args.title, links=links))
    except ValueError as error:
        print(error)
        return 2
    return _publish(args, repo, staged, f"Add results {staged[-1].folder}")


def cmd_results_combine(args: argparse.Namespace) -> int:
    """A report over recordings already in the repo, e.g. the same exit over several days."""
    from .results import publish

    repo = _results_repo(args)
    if repo is None:
        return 2
    available = publish.repo_recordings(repo)
    missing = [name for name in args.recordings if name not in available]
    if missing:
        print(f"Not in {repo}/runs: {', '.join(missing)}")
        return 2
    members = [available[name] for name in args.recordings]
    try:
        report = publish.stage_report(members, name=args.name, title=args.title, links=dict(args.link or []))
    except ValueError as error:
        print(error)
        return 2
    return _publish(args, repo, [report], f"Add report {report.folder}")


def cmd_results_rebuild(args: argparse.Namespace) -> int:
    """Regenerate every page in the repo from its stored result.json files."""
    from .results import publish

    repo = _results_repo(args)
    if repo is None:
        return 2
    return _publish(args, repo, publish.rebuild_all(repo), "Rebuild reports")


def cmd_results_link(args: argparse.Namespace) -> int:
    """Attach a link (e.g. the Claude-app page) to a report and rebuild it."""
    from .results import publish

    repo = _results_repo(args)
    if repo is None:
        return 2
    try:
        name = publish.folder_name(args.report)
    except ValueError as error:
        print(error)
        return 2
    if name != args.report:
        print(f"No report {args.report} in {repo}/reports")
        return 2
    if not args.url.startswith("https://"):
        print("The link must start with https://")
        return 2
    meta_file = repo / "reports" / name / "report.json"
    if not meta_file.exists():
        print(f"No report {args.report} in {repo}/reports")
        return 2
    meta = json.loads(meta_file.read_text())
    recordings = publish.repo_recordings(repo)
    members = [recordings[name] for name in meta["recordings"]]
    links = {**meta.get("links", {}), args.label: args.url}
    staged = publish.stage_report(members, name=args.report, title=meta["title"], links=links)
    return _publish(args, repo, [staged], f"Link {args.label} for {args.report}")


def _link_pair(text: str) -> tuple[str, str]:
    label, sep, url = text.partition("=")
    if not sep or not url.startswith("https://"):
        raise argparse.ArgumentTypeError("use LABEL=https://...")
    return label, url


def _teardown(user: Any, run: Run, profile: Any) -> bool:
    """Recover funds (if any were sent), then delete the droplets. True on success."""
    from .finance.recover import recovery_complete
    from .remote import steps
    from .remote.inventory import clients, load_inventory

    try:
        if steps.funds_sent(run) and not run.done("recover"):
            if not recovery_complete(run, profile, [m.name for m in clients(run)]):
                print("Recovery: stopping clients and starting channel closure…")
                steps.step_recover_initiate(user, run, profile)
            print("Recovery: waiting for the closure grace period, then returning funds…")
            cleanup = steps.step_recover_finalize(user, run, profile)
            print(f"Recovered {cleanup['recovered_wxHOPR']} wxHOPR and {cleanup['recovered_xDAI']} xDAI.")
        if load_inventory(run) and not run.done("destroy"):
            steps.step_destroy(user, run, profile)
            print("All droplets of this run are deleted (verified with the API).")
        return True
    except (Exception, KeyboardInterrupt) as error:  # report loudly; the deletion timer is still armed
        timer = run.state / "deadline-timer.json"
        print(f"TEARDOWN FAILED: {type(error).__name__}: {error}")
        if timer.exists():
            unit = json.loads(timer.read_text())["unit"]
            print(f"The deletion timer ({unit}) will still delete the droplets.")
        print(f"Fix the cause, then run: recover {run.config.id} and destroy {run.config.id}")
        return False


def cmd_execute(args: argparse.Namespace) -> int:
    """provision → fund → (connect → ladder) per target → recover → destroy."""
    from .remote import provision as prov
    from .remote import steps

    user, run, profile, _target = _open(args)
    targets = run.config.targets()
    if not run.done("provision"):
        prov.preflight(run, profile)
        print(prov.describe_plan(run, profile))
    print(f"Targets: {', '.join(f'{t.label} ({t.exit_address})' for t in targets)}")
    print("Funding: each client's recommended wxHOPR and xDAI from the funding Safe.")
    if args.keep:
        print(f"Teardown: KEPT; the deletion timer still deletes after {run.config.infra.deadline_hours} h.")
    else:
        print("Teardown: recover all funds, then delete every droplet (also after errors or Ctrl-C).")
    if not confirm("Run all of this?", args.yes):
        return 1
    failure: BaseException | None = None
    try:
        if not run.done("provision"):
            steps.step_provision(user, run, profile)
        if not run.done("fund"):
            plan = steps.step_fund_plan(user, run, profile)
            for node in plan["nodes"]:
                print(f"  funding {node['name']}: {node['wxhopr']} wxHOPR + {node['xdai']} xDAI")
            if not confirm("Send these funds?", args.yes):
                raise RuntimeError("funding declined")
            steps.step_fund_send(user, run, profile)
        for target in targets:
            print(f"== {target.label} ({target.name})")
            for outcome in steps.step_connect(user, run, profile, target):
                print(f"  {outcome.client}: {outcome.status} {outcome.detail}")
            for rung in steps.step_ladder(run, profile, target):
                value = rung["total_mbps"]
                shown = f"{value:.2f} Mbit/s" if value is not None else f"not measured ({rung['failure_status']})"
                print(f"  rung {rung['rung']}: {shown}")
            print(f"  wrote {steps.step_result(run, profile, target)}")
    except KeyboardInterrupt as error:
        failure = error
        print("Interrupted.")
    except Exception as error:
        failure = error
        print(f"FAILED: {type(error).__name__}: {error}")
    torn_down = True
    if args.keep:
        print("Droplets kept as requested. Run `recover` and `destroy` when done.")
    else:
        torn_down = _teardown(user, run, profile)
    return 0 if failure is None and torn_down else 1


def add_run_commands(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("plan", help="create a run directory with run.toml (no spending)")
    p.add_argument("--profile", required=True, help="profiles/<name>.toml")
    p.add_argument("--target", required=True, action="append", help="targets/<name>.toml (repeat for several exits)")
    p.add_argument("--id", help="run id (default: <UTC time>-<target>-<profile>)")
    p.add_argument("--client-config", metavar="FILE", help="client config.toml to install on the VMs")
    p.add_argument("--notes", default="", help="free text stored in run.toml and result.json")
    p.set_defaults(run_func=cmd_plan)

    p = sub.add_parser("status", help="steps done, VMs, where funds are")
    p.add_argument("run", help="run id or directory")
    p.set_defaults(run_func=cmd_status)

    p = sub.add_parser("report", help="render each RUN/results/<target>/result.json as self-contained HTML")
    p.add_argument("run", help="run id, run directory, or any directory with a result.json")
    p.add_argument("-o", "--output", metavar="FILE", help="default: report.html next to result.json")
    p.set_defaults(run_func=cmd_report)

    p = sub.add_parser("config", help="show credential and run locations; check file modes")
    p.set_defaults(run_func=cmd_config)

    p = sub.add_parser("results", help="publish to the private results repo (push, combine, rebuild, link)")
    results_sub = p.add_subparsers(dest="results_command", required=True)

    def common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--repo", metavar="PATH", help="local checkout of the results repo (default: user config)")
        parser.add_argument("--dry-run", action="store_true", help="show files and scan result only")
        parser.add_argument("--yes", action="store_true", help="write, commit and push without asking")

    p = results_sub.add_parser("push", help="add runs' recordings and one report over all of them, scan, commit, push")
    p.add_argument(
        "run", nargs="+", help="run ids, run directories, or directories with a result.json (all go into one report)"
    )
    p.add_argument("--report-name", help="folder name under reports/ (default: start time + exits + profile)")
    p.add_argument("--title", help="report title")
    p.add_argument("--link", action="append", type=_link_pair, metavar="LABEL=URL", help="link shown on the report")
    common(p)
    p.set_defaults(run_func=cmd_results_push)

    p = results_sub.add_parser("combine", help="add a report over recordings already in the repo")
    p.add_argument("recordings", nargs="+", metavar="RECORDING", help="folder names under runs/")
    p.add_argument("--name", help="folder name under reports/ (default: start time + exits + profiles)")
    p.add_argument("--title", help="report title")
    p.add_argument("--link", action="append", type=_link_pair, metavar="LABEL=URL", help="link shown on the report")
    common(p)
    p.set_defaults(run_func=cmd_results_combine)

    p = results_sub.add_parser("rebuild", help="regenerate every report page from the stored result.json files")
    common(p)
    p.set_defaults(run_func=cmd_results_rebuild)

    p = results_sub.add_parser("link", help="attach a link (e.g. the Claude-app page) to a report")
    p.add_argument("report", help="folder name under reports/")
    p.add_argument("label")
    p.add_argument("url")
    common(p)
    p.set_defaults(run_func=cmd_results_link)

    p = sub.add_parser(
        "execute",
        help="provision, fund, connect + ladder for every target, then recover and destroy (default)",
    )
    p.add_argument("run", help="run id or directory")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument(
        "--keep",
        action="store_true",
        help="keep the droplets afterwards (the deletion timer still deletes them at the deadline)",
    )
    p.set_defaults(run_func=cmd_execute)

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
        if name in ("connect", "ladder"):
            p.add_argument("--target", help="which of the run's targets (default: the first)")
        p.set_defaults(run_func=func)
