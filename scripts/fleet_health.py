#!/usr/bin/env python3
"""
fleet_health.py — read-only fleet health check across every installed agent.

Scans every adapter in agent_adapters.ADAPTERS whose `job_layout == "dir"` and
whose `automations_root` exists on disk (cloud/None-root agents, e.g. Cursor,
are skipped gracefully). For each job directory under each agent's automations
root, evaluates five independent flags:

  (a) LOCK-EXPIRED   a `.automation.lock` dir is present but its owner's
                      `lease_until` (run_lock.parse_owner) is in the past —
                      an abandoned/expired lock.
  (b) OVERDUE         an ACTIVE job whose last close (last-run.md `when`,
                      state_schema.parse_last_run) is older than its schedule
                      implies. Codex `rrule`/schedule is read loosely from
                      automation.toml; a daily cadence maps to a ~26h window,
                      anything else not confidently parseable falls back to
                      26h too. Only fires when `when` parsed successfully
                      (an unparseable `when` is flag (d), not (b)).
  (c) EMPTY-RUNS      an ACTIVE job whose `runs/` dir is missing or empty
                      (never produced a ledger entry).
  (d) BLANK-LASTRUN   last-run.md missing, empty, or unparseable (parse
                      returns None, or schema=="legacy" with empty raw).
  (e) HUGE-MEMORY     `memory.md` larger than 32 KiB.

Flags (b) and (c) apply only to ACTIVE jobs (see `_is_active`); (a), (d), (e)
apply regardless of status. This tool NEVER writes or modifies a job — it
only reads. Malformed/missing/unreadable files degrade to "flag it" or
"skip it", never a raised exception.

CLI: `python3 fleet_health.py [--home PATH] [--json]`
  --home PATH   Override the home directory to scan under (default:
                Path.home()). Tests pass a fixture dir here so the real
                ~/.codex / ~/.claude are never touched.
  --json        Emit {jobs: [...], summary: {...}} as JSON instead of a table.

Exit codes:
  0  fleet is clean (no flags fired anywhere)
  1  at least one flag fired somewhere in the fleet

Stdlib only (Python 3.11+): argparse, json, sys, importlib, pathlib, tomllib,
datetime. Reuses run_lock.parse_owner and state_schema.parse_last_run rather
than reimplementing lock/state parsing.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # codex automation.toml parsing degrades to "skip cadence"

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import agent_adapters  # noqa: E402
import run_lock  # noqa: E402
import state_schema  # noqa: E402

# Same lowercase inactive-status set as optimize_codex_automations.INACTIVE_STATUSES.
INACTIVE_STATUSES = {"disabled", "archived", "paused", "inactive", "off"}

DEFAULT_OVERDUE_WINDOW_HOURS = 26
MEMORY_SIZE_LIMIT_BYTES = 32 * 1024  # 32 KiB

FLAG_LOCK_EXPIRED = "LOCK-EXPIRED"
FLAG_OVERDUE = "OVERDUE"
FLAG_EMPTY_RUNS = "EMPTY-RUNS"
FLAG_BLANK_LASTRUN = "BLANK-LASTRUN"
FLAG_HUGE_MEMORY = "HUGE-MEMORY"


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


def _load_toml(path: Path) -> dict:
    """Best-effort TOML load; never raises. Empty dict on any failure."""
    if tomllib is None or not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}


def _overdue_window_hours(job_dir: Path, adapter: dict) -> int:
    """Loosely derive an expected close cadence in hours. Falls back to the
    default 26h window whenever the cadence can't be confidently parsed."""
    if adapter.get("schedule_field") != "rrule":
        return DEFAULT_OVERDUE_WINDOW_HOURS
    toml_path = job_dir / "automation.toml"
    data = _load_toml(toml_path)
    rrule = data.get("rrule")
    if not isinstance(rrule, str):
        return DEFAULT_OVERDUE_WINDOW_HOURS
    parts = dict(
        p.split("=", 1) for p in rrule.split(";") if "=" in p
    )
    freq = parts.get("FREQ", "").upper()
    if freq == "DAILY":
        return DEFAULT_OVERDUE_WINDOW_HOURS
    # WEEKLY/MONTHLY/anything else: cadence isn't a simple daily window, and
    # we don't have enough here to safely derive a longer one — fall back.
    return DEFAULT_OVERDUE_WINDOW_HOURS


def _is_active(job_dir: Path, adapter: dict) -> bool:
    """Active determination per the adapter's status_field. Agents with no
    status_field (claude SKILL.md, gemini) are always treated as active."""
    status_field = adapter.get("status_field")
    if not status_field:
        return True
    if adapter.get("schedule_field") == "rrule":
        # codex: status lives in automation.toml
        data = _load_toml(job_dir / "automation.toml")
        status = str(data.get(status_field, "")).lower()
        return status not in INACTIVE_STATUSES
    return True


def _check_lock_expired(job_dir: Path, adapter: dict, detail: dict) -> bool:
    lock_name = adapter.get("lock")
    if not lock_name:
        return False
    lock_dir = job_dir / lock_name
    if not lock_dir.is_dir():
        return False
    try:
        owner = run_lock.parse_owner(lock_dir)
    except Exception:
        owner = None
    if owner is None:
        # Lock dir present with no readable owner file — can't prove
        # expiry either way; don't flag on absence of evidence.
        return False
    lease_raw = owner.get("lease_until")
    lease_dt = _parse_iso(lease_raw) if lease_raw else None
    if lease_dt is None:
        return False
    if _utcnow() >= lease_dt:
        detail["lock_lease_until"] = lease_raw
        return True
    return False


def _last_run_record(job_dir: Path) -> dict | None:
    try:
        return state_schema.parse_last_run(job_dir / "last-run.md")
    except Exception:
        return None


def _check_blank_lastrun(record: dict | None, detail: dict) -> bool:
    if record is None:
        return True
    if record.get("schema") == "legacy" and not (record.get("raw") or "").strip():
        return True
    if not record.get("when") and record.get("schema") == "legacy":
        return True
    return False


def _check_overdue(record: dict | None, job_dir: Path, adapter: dict,
                    detail: dict) -> bool:
    if record is None:
        return False
    when_raw = record.get("when")
    if not when_raw:
        return False  # unparseable/missing `when` is BLANK-LASTRUN's job
    when_dt = _parse_iso(when_raw)
    if when_dt is None:
        return False
    window_hours = _overdue_window_hours(job_dir, adapter)
    deadline = when_dt + timedelta(hours=window_hours)
    if _utcnow() >= deadline:
        detail["last_when"] = when_raw
        detail["overdue_window_hours"] = window_hours
        return True
    return False


def _check_empty_runs(job_dir: Path, detail: dict) -> bool:
    runs_dir = job_dir / "runs"
    if not runs_dir.is_dir():
        return True
    try:
        has_entry = any(runs_dir.iterdir())
    except OSError:
        return True
    return not has_entry


def _check_huge_memory(job_dir: Path, detail: dict) -> bool:
    memory_path = job_dir / "memory.md"
    if not memory_path.is_file():
        return False
    try:
        size = memory_path.stat().st_size
    except OSError:
        return False
    if size > MEMORY_SIZE_LIMIT_BYTES:
        detail["memory_bytes"] = size
        return True
    return False


def _evaluate_job(job_dir: Path, adapter: dict) -> dict | None:
    """Evaluate one job directory. Returns a flagged-job dict, or None when
    the job is clean (so callers can filter for the report)."""
    detail: dict = {}
    flags: list[str] = []

    if _check_lock_expired(job_dir, adapter, detail):
        flags.append(FLAG_LOCK_EXPIRED)

    record = _last_run_record(job_dir)
    if _check_blank_lastrun(record, detail):
        flags.append(FLAG_BLANK_LASTRUN)

    active = _is_active(job_dir, adapter)
    if active:
        if _check_overdue(record, job_dir, adapter, detail):
            flags.append(FLAG_OVERDUE)
        if _check_empty_runs(job_dir, detail):
            flags.append(FLAG_EMPTY_RUNS)

    if _check_huge_memory(job_dir, detail):
        flags.append(FLAG_HUGE_MEMORY)

    if not flags:
        return None
    return {
        "agent": adapter.get("label", "?"),
        "id": job_dir.name,
        "flags": flags,
        "detail": detail,
    }


# Reserved non-job dir names that live under an automations root after the
# v0.7.x layout (alongside the dotdirs .archive/.disabled/.workspace-locks/.git).
RESERVED_JOB_DIR_NAMES = frozenset({"suites"})


def _is_job_dir(path: Path, adapter: dict) -> bool:
    """A subdir under an automations root is a job only if it is not a hidden
    or reserved dir AND actually contains the adapter's job file (codex ->
    automation.toml, claude -> SKILL.md, gemini -> prompt.md). Mirrors
    approval_digest.iter_queue_files skipping dot-prefixed dirs."""
    name = path.name
    if name.startswith(".") or name in RESERVED_JOB_DIR_NAMES:
        return False
    job_file = adapter.get("job_file")
    if job_file and not (path / job_file).is_file():
        return False
    return True


def _iter_job_dirs(automations_root: Path, adapter: dict) -> list[Path]:
    try:
        return sorted(p for p in automations_root.iterdir()
                       if p.is_dir() and _is_job_dir(p, adapter))
    except OSError:
        return []


def scan_fleet(home: Path) -> dict:
    """Scan every dir-layout agent under `home`. Returns
    {jobs: [...], summary: {...}} — never raises."""
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
        for job_dir in _iter_job_dirs(root, adapter):
            flagged = _evaluate_job(job_dir, adapter)
            if flagged is not None:
                flagged["agent"] = agent_name
                jobs.append(flagged)

    flag_counts: dict[str, int] = {}
    for job in jobs:
        for f in job["flags"]:
            flag_counts[f] = flag_counts.get(f, 0) + 1

    summary = {
        "agents_scanned": agents_scanned,
        "jobs_flagged": len(jobs),
        "flag_counts": flag_counts,
        "healthy": len(jobs) == 0,
    }
    return {"jobs": jobs, "summary": summary}


def _print_table(result: dict) -> None:
    jobs = result["jobs"]
    summary = result["summary"]
    if not jobs:
        print(f"Fleet clean — {len(summary['agents_scanned'])} agent(s) scanned, "
              f"0 jobs flagged.")
        return
    header = f"{'AGENT':<10} {'JOB':<40} FLAGS"
    print(header)
    print("-" * len(header))
    for job in jobs:
        print(f"{job['agent']:<10} {job['id']:<40} {', '.join(job['flags'])}")
    print()
    print(f"{summary['jobs_flagged']} job(s) flagged across "
          f"{len(summary['agents_scanned'])} agent(s): {summary['flag_counts']}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Read-only fleet health check across every installed agent.")
    ap.add_argument("--home", default=None,
                     help="override home dir to scan under (default: Path.home(); "
                          "tests pass a fixture dir here)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args(argv)

    home = Path(args.home).expanduser() if args.home else Path.home()
    result = scan_fleet(home)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_table(result)

    return 1 if result["summary"]["jobs_flagged"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
