#!/usr/bin/env python3
"""
fleet_report.py — read-only rollup of fleet run history across every installed
agent. Writes ONLY `<home>/.codex/FLEET-REPORT.md`; never touches a job's own
state files.

Scans every dir-layout adapter in agent_adapters.ADAPTERS whose automations_root
exists under --home (cloud/None-root agents, e.g. Cursor, are skipped
gracefully — same house pattern as fleet_health.py). For each job directory,
parses every `runs/*.md` entry via state_schema.parse_run_entry (entries with
no parseable `when` are skipped) and buckets each entry by how many days before
--now its `when` falls:
  RECENT-7d  0-6 days ago
  PRIOR-7d   7-13 days ago
  30d        0-29 days ago (superset used for the "30d" rollup columns)

Per job, computes over the 30d window (unless noted):
  - runs_7d / runs_30d    count of entries in each window
  - units_30d             sum of units_completed over the 30d window
  - merges_30d            sum of len(merged_shas) over the 30d window
  - blocked_ratio_7d      fraction of RECENT-7d runs whose outcome/failure_class
                          indicates blocked/failed (see BLOCKED_SET below)
  - mean_runtime_s        mean of runtime_s over 30d runs that have a runtime_s
  - top_failure_class     most common non-none failure_class over 30d runs

A job with no runs/ (or an empty one) is reported with zeros and no flags —
never raises, never skipped from the report.

FLAGS (per job; independent, any subset may fire):
  - zero-units-3x     the 3 most recent runs (by `when`, across ALL time, not
                      windowed) all have units_completed == 0. Fewer than 3
                      total runs never fires this flag.
  - blocked-rising    blocked_ratio(RECENT-7d) >= 1.5 * blocked_ratio(PRIOR-7d),
                      guarded: requires PRIOR-7d to have >=1 run. If PRIOR-7d
                      has 0 runs (ratio undefined -> treated as 0) and RECENT-7d
                      has >=2 runs with ratio > 0, this also fires (covers the
                      "brand-new blocked pattern" case the multiplicative rule
                      can't reach from a zero base).
  - runtime-doubled   mean_runtime_s(RECENT-7d) >= 2 * mean_runtime_s(PRIOR-7d),
                      requires BOTH periods to have >=1 run with a runtime_s
                      value; otherwise never fires.

CLI: `python3 fleet_report.py [--home PATH] [--json] [--now ISO8601]`
  --home PATH   Override the home directory to scan under (default:
                Path.home()). Tests pass a fixture dir here so the real
                ~/.codex / ~/.claude are never touched.
  --json        Emit {generated_for, jobs: [...], summary: {...}} as JSON
                instead of writing/printing the human table.
  --now ISO8601 Override "today" for deterministic bucketing (default: current
                UTC instant). Accepts anything datetime.fromisoformat parses.

Always writes `<home>/.codex/FLEET-REPORT.md` (human table) as a side effect,
regardless of --json (a report, not a gate: exit code is always 0). Malformed
or missing run files are skipped, never raise. Stdlib only (Python 3.11+).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import agent_adapters  # noqa: E402
import state_schema  # noqa: E402

RECENT_WINDOW_DAYS = 7
PRIOR_WINDOW_START_DAYS = 7
PRIOR_WINDOW_END_DAYS = 14
ROLLUP_WINDOW_DAYS = 30

# Outcomes that are NOT blocked (allow-list — everything else counts as blocked).
OK_OUTCOMES = {"success", "no-op", "passed", "fixed"}
# failure_class values that are NOT blocked (allow-list — everything else,
# when present, counts as blocked).
OK_FAILURE_CLASSES = {"none", "", None}

FLAG_ZERO_UNITS_3X = "zero-units-3x"
FLAG_BLOCKED_RISING = "blocked-rising"
FLAG_RUNTIME_DOUBLED = "runtime-doubled"

BLOCKED_RISING_MULTIPLIER = 1.5
RUNTIME_DOUBLED_MULTIPLIER = 2.0
ZERO_UNITS_STREAK_LEN = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_blocked(record: dict) -> bool:
    """BLOCKED set: any outcome not in OK_OUTCOMES, OR failure_class not in
    OK_FAILURE_CLASSES."""
    outcome = (record.get("outcome") or "").strip().lower()
    failure_class = record.get("failure_class")
    failure_class_norm = failure_class.strip().lower() if isinstance(failure_class, str) else failure_class
    outcome_ok = outcome in OK_OUTCOMES
    failure_ok = failure_class_norm in OK_FAILURE_CLASSES
    return not (outcome_ok and failure_ok)


def _iter_job_dirs(automations_root: Path) -> list[Path]:
    try:
        return sorted(p for p in automations_root.iterdir() if p.is_dir())
    except OSError:
        return []


def _load_run_entries(job_dir: Path) -> list[dict]:
    """Every parseable `runs/*.md` entry, each annotated with a parsed
    `_when_dt` (UTC datetime). Entries with no parseable `when` are skipped.
    Never raises."""
    runs_dir = job_dir / "runs"
    if not runs_dir.is_dir():
        return []
    entries: list[dict] = []
    try:
        paths = sorted(runs_dir.iterdir())
    except OSError:
        return []
    for p in paths:
        if not p.is_file():
            continue
        try:
            record = state_schema.parse_run_entry(p)
        except Exception:
            record = None
        if record is None:
            continue
        when_raw = record.get("when")
        if not when_raw:
            continue
        when_dt = _parse_iso(when_raw)
        if when_dt is None:
            continue
        record["_when_dt"] = when_dt
        entries.append(record)
    return entries


def _bucket(entries: list[dict], now: datetime) -> dict[str, list[dict]]:
    recent7: list[dict] = []
    prior7: list[dict] = []
    within30: list[dict] = []
    for e in entries:
        age = now - e["_when_dt"]
        if age < timedelta(0):
            # a `when` in the future relative to --now: still include it in the
            # 30d rollup (it's real data) but it can't land in either 7d window.
            if age >= -timedelta(days=ROLLUP_WINDOW_DAYS):
                within30.append(e)
            continue
        age_days = age.total_seconds() / 86400.0
        if age_days < RECENT_WINDOW_DAYS:
            recent7.append(e)
        elif PRIOR_WINDOW_START_DAYS <= age_days < PRIOR_WINDOW_END_DAYS:
            prior7.append(e)
        if age_days < ROLLUP_WINDOW_DAYS:
            within30.append(e)
    return {"recent7": recent7, "prior7": prior7, "within30": within30}


def _blocked_ratio(entries: list[dict]) -> float:
    if not entries:
        return 0.0
    blocked = sum(1 for e in entries if _is_blocked(e))
    return blocked / len(entries)


def _mean_runtime(entries: list[dict]) -> float | None:
    values = [e["runtime_s"] for e in entries if isinstance(e.get("runtime_s"), int)]
    if not values:
        return None
    return sum(values) / len(values)


def _top_failure_class(entries: list[dict]) -> str | None:
    classes = [e.get("failure_class") for e in entries
               if e.get("failure_class") not in OK_FAILURE_CLASSES]
    if not classes:
        return None
    return Counter(classes).most_common(1)[0][0]


def _flag_zero_units_3x(entries: list[dict]) -> bool:
    if len(entries) < ZERO_UNITS_STREAK_LEN:
        return False
    most_recent = sorted(entries, key=lambda e: e["_when_dt"], reverse=True)[:ZERO_UNITS_STREAK_LEN]
    return all((e.get("units_completed") or 0) == 0 for e in most_recent)


def _flag_blocked_rising(recent7: list[dict], prior7: list[dict]) -> bool:
    if not prior7:
        recent_ratio = _blocked_ratio(recent7)
        return len(recent7) >= 2 and recent_ratio > 0
    recent_ratio = _blocked_ratio(recent7)
    prior_ratio = _blocked_ratio(prior7)
    return recent_ratio >= BLOCKED_RISING_MULTIPLIER * prior_ratio and recent_ratio > 0


def _flag_runtime_doubled(recent7: list[dict], prior7: list[dict]) -> bool:
    recent_mean = _mean_runtime(recent7)
    prior_mean = _mean_runtime(prior7)
    if recent_mean is None or prior_mean is None or prior_mean == 0:
        return False
    return recent_mean >= RUNTIME_DOUBLED_MULTIPLIER * prior_mean


def _evaluate_job(agent_name: str, job_dir: Path, now: datetime) -> dict:
    entries = _load_run_entries(job_dir)
    buckets = _bucket(entries, now)
    recent7, prior7, within30 = buckets["recent7"], buckets["prior7"], buckets["within30"]

    units_30d = sum((e.get("units_completed") or 0) for e in within30)
    merges_30d = sum(len(e.get("merged_shas") or []) for e in within30)
    merges_7d = sum(len(e.get("merged_shas") or []) for e in recent7)
    mean_runtime = _mean_runtime(within30)

    flags: list[str] = []
    if _flag_zero_units_3x(entries):
        flags.append(FLAG_ZERO_UNITS_3X)
    if _flag_blocked_rising(recent7, prior7):
        flags.append(FLAG_BLOCKED_RISING)
    if _flag_runtime_doubled(recent7, prior7):
        flags.append(FLAG_RUNTIME_DOUBLED)

    return {
        "agent": agent_name,
        "id": job_dir.name,
        "runs_7d": len(recent7),
        "runs_30d": len(within30),
        "units_30d": units_30d,
        "merges_30d": merges_30d,
        "merges_7d": merges_7d,
        "blocked_ratio_7d": round(_blocked_ratio(recent7), 4),
        "mean_runtime_s": round(mean_runtime, 2) if mean_runtime is not None else None,
        "top_failure_class": _top_failure_class(within30),
        "flags": flags,
    }


def scan_fleet(home: Path, now: datetime) -> dict:
    """Scan every dir-layout agent under `home`. Returns
    {generated_for, jobs: [...], summary: {...}} — never raises."""
    jobs: list[dict] = []
    agents_scanned: list[str] = []

    for agent_name, adapter in agent_adapters.ADAPTERS.items():
        if adapter.get("job_layout") != "dir":
            continue
        root_template = adapter.get("automations_root")
        if not root_template:
            continue
        root = Path(root_template.replace("~", str(home), 1)).expanduser() \
            if root_template.startswith("~") else Path(root_template)
        if not root.is_dir():
            continue
        agents_scanned.append(agent_name)
        for job_dir in _iter_job_dirs(root):
            jobs.append(_evaluate_job(agent_name, job_dir, now))

    runs_7d_total = sum(j["runs_7d"] for j in jobs)
    merges_7d_total = sum(j["merges_7d"] for j in jobs)

    summary = {
        "agents_scanned": agents_scanned,
        "jobs_total": len(jobs),
        "runs_7d_total": runs_7d_total,
        "merges_7d_total": merges_7d_total,
        "merges_30d_total": sum(j["merges_30d"] for j in jobs),
        "jobs_flagged": sum(1 for j in jobs if j["flags"]),
        "headline": f"{len(jobs)} jobs, {runs_7d_total} runs (7d), {merges_7d_total} merges (7d)",
    }
    return {"generated_for": now.isoformat(), "jobs": jobs, "summary": summary}


def _fmt(v) -> str:
    return "-" if v is None else str(v)


def render_markdown(result: dict) -> str:
    jobs = result["jobs"]
    summary = result["summary"]
    lines = [
        f"# Fleet report — generated for {result['generated_for']}",
        "",
        summary["headline"],
        "",
    ]
    if not jobs:
        lines.append("_no jobs found_")
        return "\n".join(lines) + "\n"

    header = ("| Agent | Job | Runs 7d | Runs 30d | Units 30d | Merges 30d | "
               "Blocked 7d | Mean runtime (s) | Top failure | Flags |")
    sep = "|---|---|---|---|---|---|---|---|---|---|"
    lines += [header, sep]
    for j in sorted(jobs, key=lambda j: (j["agent"], j["id"])):
        lines.append(
            f"| {j['agent']} | {j['id']} | {j['runs_7d']} | {j['runs_30d']} | "
            f"{j['units_30d']} | {j['merges_30d']} | {j['blocked_ratio_7d']} | "
            f"{_fmt(j['mean_runtime_s'])} | {_fmt(j['top_failure_class'])} | "
            f"{', '.join(j['flags']) if j['flags'] else '-'} |"
        )
    return "\n".join(lines) + "\n"


def _write_report(home: Path, markdown: str) -> Path:
    out_dir = home / ".codex"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "FLEET-REPORT.md"
    out_path.write_text(markdown, encoding="utf-8")
    return out_path


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Read-only rollup of fleet run history across every installed agent.")
    ap.add_argument("--home", default=None,
                     help="override home dir to scan under (default: Path.home(); "
                          "tests pass a fixture dir here)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of the table")
    ap.add_argument("--now", default=None,
                     help="override 'today' (ISO8601) for deterministic bucketing")
    args = ap.parse_args(argv)

    home = Path(args.home).expanduser() if args.home else Path.home()
    now = _parse_iso(args.now) if args.now else _utcnow()
    if now is None:
        now = _utcnow()

    result = scan_fleet(home, now)
    markdown = render_markdown(result)

    try:
        _write_report(home, markdown)
    except OSError:
        pass  # a report, not a gate: never fail the run over a write error

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(markdown)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
