"""Lay out the private results repository.

Layout::

    README.md                       index of all reports and recordings
    runs/<recording>/               one recording = one run × one exit
        result.json  provenance.json  report.html  README.md  charts/*.png
    reports/<report>/               one or more recordings, possibly several
        report.json                 exits, runs or repetitions
        report.html  README.md  charts/*.png

``README.md`` files are what GitHub renders (web and app); ``report.html``
is the same content as a single page. Every push adds the run's recordings
and one report over them. ``results combine`` adds reports across runs;
``results rebuild`` regenerates every page from the stored ``result.json``
files after a renderer change.

Only the file names in ``ALLOWED`` are ever written, and every file is
scanned (``push.scan_text``) before anything is written.
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any

from .charts import PngChart, rasterize
from .collection import Collection, Recording, load_recording
from .markdown import render_markdown
from .push import destination_name, scan_text, timestamp_name, vm_ips_of
from .render import default_title, render_collection
from .schema import validate

ALLOWED = re.compile(
    r"(result\.json|provenance\.json|report\.html|README\.md|report\.json|charts/[a-z0-9-]+\.(svg|png))"
)
_SLUG = re.compile(r"[^a-z0-9_.-]+")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass
class Staged:
    folder: str  # repo-relative, e.g. "runs/2026-10-31-101146Z-netherlands-jura-prod-v4-jura-prod"
    files: dict[str, str | PngChart]
    vm_ips: set[str] = field(default_factory=set)


def slug(text: str) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")[:120]


def _json(payload: Any) -> str:
    return json.dumps(payload, indent=2) + "\n"


def recording_files(name: str, result: dict[str, Any]) -> dict[str, str | PngChart]:
    collection = Collection(f"Gnosis VPN: {default_title(result)}", [Recording(name, result)])
    readme, charts = render_markdown(collection)
    return {
        "result.json": _json(result),
        "provenance.json": _json(result["provenance"]),
        "report.html": render_collection(collection),
        "README.md": readme,
        **charts,
    }


def recording_identity(result: dict[str, Any]) -> tuple[Any, ...]:
    """Keep a recording addressable after an archive folder has been renamed."""
    run = result["run"]
    experiment = run.get("experiment", {})
    target = result["provenance"]["target"]
    return (
        run["id"],
        target["name"],
        target["network"],
        result["provenance"]["profile"]["name"],
        experiment.get("cycle"),
        None if experiment else run.get("started_at"),
    )


def stage_recording(result_dir: Path, existing: dict[str, Recording] | None = None) -> Staged:
    result = json.loads((result_dir / "result.json").read_text())
    validate(result)
    name = destination_name(result)
    matches = [r.name for r in (existing or {}).values() if recording_identity(r.result) == recording_identity(result)]
    if len(matches) > 1:
        raise ValueError("multiple published folders identify the same recording")
    if matches:
        name = matches[0]
    if name in (existing or {}) and recording_identity(existing[name].result) != recording_identity(result):
        raise ValueError(f"recording timestamp collision: {name}")
    return Staged(f"runs/{name}", recording_files(name, result), vm_ips_of(result_dir))


def folder_name(name: str) -> str:
    """A user-given report name as one safe folder name under reports/."""
    cleaned = _SAFE_NAME.sub("-", name).strip("-.")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"invalid report name {name!r}")
    return cleaned


def report_name(recordings: list[Recording]) -> str:
    return timestamp_name(min(r.result["run"]["created_at"] for r in recordings))


def report_files(
    name: str, title: str, recordings: list[Recording], links: dict[str, str]
) -> dict[str, str | PngChart]:
    collection = Collection(title, recordings)
    link_lines = [f"[{label}]({url})" for label, url in links.items()]
    link_lines += [f"[{r.name}](../../runs/{r.name}/)" for r in sorted(recordings, key=lambda r: r.started)]
    readme, charts = render_markdown(collection, extra_links=link_lines)
    meta = {"title": title, "recordings": [r.name for r in recordings], "links": links}
    return {"report.json": _json(meta), "report.html": render_collection(collection), "README.md": readme, **charts}


def default_report_title(recordings: list[Recording]) -> str:
    exits = sorted({r.target["label"] for r in recordings})
    networks = sorted({r.profile["network"] for r in recordings})
    days = sorted({r.started[:10] for r in recordings})
    return f"Gnosis VPN: {' + '.join(exits)} on {', '.join(networks)}, {days[0]}"


def stage_report(
    recordings: list[Recording],
    *,
    name: str | None = None,
    title: str | None = None,
    links: dict[str, str] | None = None,
) -> Staged:
    name = folder_name(name) if name else report_name(recordings)
    title = title or default_report_title(recordings)
    return Staged(f"reports/{name}", report_files(name, title, recordings, links or {}))


# -- reading the repository ------------------------------------------------------


def existing_links(repo: Path, name: str) -> dict[str, str]:
    """Links already attached to reports/<name> (kept when the report is regenerated)."""
    meta = repo / "reports" / folder_name(name) / "report.json"
    return json.loads(meta.read_text()).get("links", {}) if meta.exists() else {}


def repo_recordings(repo: Path) -> dict[str, Recording]:
    return {p.parent.name: load_recording(p.parent) for p in sorted((repo / "runs").glob("*/result.json"))}


def repo_reports(repo: Path) -> dict[str, dict[str, Any]]:
    return {p.parent.name: json.loads(p.read_text()) for p in sorted((repo / "reports").glob("*/report.json"))}


def _rung_totals(recording: Recording) -> str:
    rungs = recording.rungs()
    if not rungs:
        return "no ladder"
    return " / ".join(f"{r['total_mbps']:.1f}" if r["total_mbps"] is not None else "n/a" for r in rungs)


def experiment_summary(recordings: list[Recording]) -> str:
    """One factual sentence per experiment; missing throughput never becomes zero."""
    exits = sorted({r.target["label"] for r in recordings})
    networks = ", ".join(sorted({r.profile["network"] for r in recordings}))
    profiles = ", ".join(sorted({r.profile["name"] for r in recordings}))
    locations = " + ".join(exits) if len(exits) <= 2 else f"{len(exits)} exits"
    description = (
        f"{locations} on {networks} ({profiles}); {len(recordings)} recording{'s' if len(recordings) != 1 else ''}"
    )
    cycles = {r.experiment.get("cycle") for r in recordings if r.experiment}
    if cycles:
        description += f", {len(cycles)} cycle{'s' if len(cycles) != 1 else ''}"
    rungs = [(r, rung) for r in recordings for rung in r.rungs()]
    measured = [(r, rung) for r, rung in rungs if rung["status"] == "measured"]
    if rungs:
        sizes = [row["rung"] for _, row in rungs]
        description += f", {min(sizes)}–{max(sizes)} clients; {len(measured)}/{len(rungs)} rungs measured"
    if measured:
        recording, peak = max(measured, key=lambda pair: pair[1]["total_mbps"])
        description += f"; peak {peak['total_mbps']:.2f} Mbit/s at {peak['rung']} clients ({recording.target['label']})"
    else:
        checks = [row for r in recordings for row in r.result["availability"]]
        if checks:
            description += (
                f"; {sum(row['status'] == 'connected' for row in checks)}/{len(checks)} availability checks connected"
            )
        description += "; no ladder throughput measured"
    failed = Counter(row["failure_status"] for _, row in rungs if row["status"] == "failed")
    if failed:
        description += "; failures: " + ", ".join(f"{status} ×{count}" for status, count in sorted(failed.items()))
    if any(not r.result["run"]["reproducible"] for r in recordings):
        description += "; historical import"
    return description + "."


def _experiments(recordings: dict[str, Recording], reports: dict[str, dict[str, Any]]) -> list[str]:
    groups: dict[str, list[Recording]] = {}
    for recording in recordings.values():
        key = recording.experiment.get("id", recording.result["run"]["id"])
        groups.setdefault(key, []).append(recording)
    lines = [
        "## Experiments",
        "",
        "One line per experiment. Rates are total Mbit/s over concurrent clients' common overlap, "
        "not per-client rates.",
        "",
    ]
    for members in sorted(
        groups.values(), key=lambda group: min(r.result["run"]["created_at"] for r in group), reverse=True
    ):
        names = {r.name for r in members}
        candidates = [(name, meta) for name, meta in reports.items() if names <= set(meta["recordings"])]
        label = timestamp_name(min(r.result["run"]["created_at"] for r in members))
        if candidates:
            best, _ = min(candidates, key=lambda pair: (len(pair[1]["recordings"]), pair[0]))
            link = f"reports/{best}/"
        else:
            link = f"runs/{min(members, key=lambda r: r.started).name}/"
        summary = escape(experiment_summary(members), quote=False).replace("\n", " ")
        lines.append(f"- [{label}]({link}) — {summary}")
    return lines


def index_markdown(recordings: dict[str, Recording], reports: dict[str, dict[str, Any]]) -> str:
    lines = [
        "# Gnosis VPN results",
        "",
        "Generated by `gnosis_vpn-bench results push`; do not edit by hand. Open a folder to see its "
        "rendered report; `report.html` in the same folder is the same report as one page.",
        "",
        "Folder timestamps use UTC (`YYYY-MM-DD--HH-MM-SS`): experiments/reports use creation time; recordings use "
        "measurement start time, or creation time when the start is unknown. "
        "Original IDs remain in result.json for provenance.",
        "",
        *_experiments(recordings, reports),
        "",
        "## Reports",
        "",
        "| Report | Started | Exits | Networks | Recordings | Links |",
        "|---|---|---|---|---|---|",
    ]

    def started(meta: dict[str, Any]) -> str:
        members = [recordings[n] for n in meta["recordings"] if n in recordings]
        return min((r.started for r in members), default="")

    for name, meta in sorted(reports.items(), key=lambda item: started(item[1]), reverse=True):
        members = [recordings[n] for n in meta["recordings"] if n in recordings]
        exits = ", ".join(sorted({r.target["label"] for r in members}))
        networks = ", ".join(sorted({r.profile["network"] for r in members}))
        links = " · ".join(f"[{label}]({url})" for label, url in meta.get("links", {}).items())
        lines.append(
            f"| [{meta['title']}](reports/{name}/) | {started(meta)[:16].replace('T', ' ')} | {exits} | "
            f"{networks} | {len(members)} | {links} |"
        )
    lines += [
        "",
        "## Recordings",
        "",
        "One run × one exit. Ladder totals are Mbit/s for 1 / 2 / 3 … concurrent clients; n/a = not measured.",
        "",
        "| Recording | Started (UTC) | Exit | Network | Profile | Package | Ladder totals | Reproducible |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, recording in sorted(recordings.items(), key=lambda item: item[1].started, reverse=True):
        lines.append(
            f"| [{name}](runs/{name}/) | {recording.started[:16].replace('T', ' ')} | {recording.target['label']} | "
            f"{recording.profile['network']} | {recording.profile['name']} | "
            f"{recording.client.get('package_version') or 'n/a'} | {_rung_totals(recording)} | "
            f"{'yes' if recording.result['run']['reproducible'] else 'no'} |"
        )
    return "\n".join(lines) + "\n"


# -- scanning and writing -------------------------------------------------------------


def scan(staged: list[Staged]) -> list[str]:
    problems = []
    for item in staged:
        for path, text in item.files.items():
            shown = f"{item.folder}/{path}" if item.folder else path
            if not ALLOWED.fullmatch(path):
                problems.append(f"{shown}: not an allowed file name")
            if path.endswith(".png") != isinstance(text, PngChart):
                problems.append(f"{shown}: PNG files require a generated chart source")
            problems += scan_text(shown, text.source if isinstance(text, PngChart) else text, item.vm_ips)
    return problems


def write(repo: Path, staged: list[Staged]) -> list[str]:
    """Replace each staged folder's files; return the repo-relative files written.

    Commit these with ``push.commit(..., update=folders(staged))``: only these
    files are added, and the folders are updated for deletions only (old
    charts), so a stray untracked file in a folder is never committed.
    """
    problems = scan(staged)
    if problems:
        raise ValueError("Refusing unsafe results: " + "; ".join(problems))
    # Render before replacing any folders, so a missing converter keeps the old report intact.
    rendered = [
        {path: rasterize(value) if isinstance(value, PngChart) else value for path, value in item.files.items()}
        for item in staged
    ]
    written = []
    for item, files in zip(staged, rendered, strict=True):
        folder = repo / item.folder
        charts = folder / "charts"
        if charts.exists():
            shutil.rmtree(charts)  # charts are regenerated as a set
        for path, text in files.items():
            target = folder / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, bytes):
                target.write_bytes(text)
            else:
                target.write_text(text)
            written.append(f"{item.folder}/{path}" if item.folder else path)
    return written


def folders(staged: list[Staged]) -> list[str]:
    return [item.folder for item in staged if item.folder]


def rebuild_all(repo: Path) -> list[Staged]:
    """Regenerate every recording and report page from the stored data."""
    recordings = repo_recordings(repo)
    staged = [Staged(f"runs/{name}", recording_files(name, r.result)) for name, r in recordings.items()]
    for name, meta in repo_reports(repo).items():
        members = [recordings[n] for n in meta["recordings"] if n in recordings]
        if members:
            staged.append(Staged(f"reports/{name}", report_files(name, meta["title"], members, meta.get("links", {}))))
    return staged


def index_staged(repo: Path, staged: list[Staged]) -> Staged:
    """The index over what is in the repo plus what is about to be written."""
    recordings = repo_recordings(repo)
    reports = repo_reports(repo)
    for item in staged:
        name = item.folder.split("/", 1)[1]
        if item.folder.startswith("runs/"):
            recordings[name] = Recording(name, json.loads(item.files["result.json"]))
        else:
            reports[name] = json.loads(item.files["report.json"])
    return Staged("", {"README.md": index_markdown(recordings, reports)})
