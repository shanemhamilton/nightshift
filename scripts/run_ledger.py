#!/usr/bin/env python3
"""
run_ledger.py — deterministic ledger + escalation counters for automations.

Subcommands: open, close. See --help on each.

`open` records a run start via a transient `<job_dir>/.run-start` marker
(ISO8601 UTC). `close` builds a record with state_schema.blank_record(),
writes it via state_schema.render_frontmatter to BOTH a new dated entry
under `<job_dir>/runs/<timestamp>.md` and `<job_dir>/last-run.md`, then
updates a MACHINE-OWNED counters block in `<job_dir>/memory.md` delimited by
`<!-- ao:counters -->` ... `<!-- /ao:counters -->` containing:

    consecutive_failures: <int>
    last_success: <iso or empty>

Counter rule: outcome in {"success", "no-op"} is a SUCCESS close (resets
consecutive_failures to 0, sets last_success). Any other outcome is a
FAILURE close (increments consecutive_failures, leaves last_success alone).
If consecutive_failures >= --threshold (default 3) after a failure close,
`close` prints an `ESCALATE:` line and exits 3.

Exit codes:
  0  normal close (or successful open)
  1  usage error
  3  escalate — consecutive_failures reached/exceeded threshold; this is a
     normal, expected outcome for a repeatedly-failing job, never an
     uncaught exception.

Stdlib only (Python 3.11+): argparse, pathlib, re, sys, datetime.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import state_schema  # noqa: E402

RUN_START_NAME = ".run-start"
RUNS_SUBDIR = "runs"
LAST_RUN_NAME = "last-run.md"
MEMORY_NAME = "memory.md"
COUNTERS_START = "<!-- ao:counters -->"
COUNTERS_END = "<!-- /ao:counters -->"
SUCCESS_OUTCOMES = {"success", "no-op"}
DEFAULT_THRESHOLD = 3
EXIT_USAGE = 1
EXIT_ESCALATE = 3

_COUNTERS_BLOCK_RE = re.compile(
    re.escape(COUNTERS_START) + r".*?" + re.escape(COUNTERS_END),
    re.DOTALL,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _timestamp_slug(dt: datetime) -> str:
    """Filesystem-safe, sortable UTC timestamp: 20260701T060000Z."""
    return dt.strftime("%Y%m%dT%H%M%SZ")


# --- open ------------------------------------------------------------------
def cmd_open(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    start_iso = _iso(_utcnow())
    (job_dir / RUN_START_NAME).write_text(start_iso + "\n", encoding="utf-8")
    print(f"OPENED {start_iso}")
    return 0


# --- close: runtime + record building --------------------------------------
def _read_run_start(job_dir: Path) -> datetime | None:
    marker = job_dir / RUN_START_NAME
    if not marker.is_file():
        return None
    try:
        text = marker.read_text(encoding="utf-8").strip()
        return _parse_iso(text)
    except (OSError, ValueError):
        return None


def _compute_runtime_s(job_dir: Path, now: datetime) -> int | None:
    start = _read_run_start(job_dir)
    if start is None:
        return None
    return int((now - start).total_seconds())


def _build_record(args: argparse.Namespace, now: datetime, runtime_s: int | None) -> dict:
    record = state_schema.blank_record()
    record["when"] = _iso(now)
    record["outcome"] = args.outcome
    record["units_completed"] = args.units
    record["stop_reason"] = args.stop_reason
    record["failure_class"] = args.failure_class
    record["runtime_s"] = runtime_s
    record["merged_shas"] = list(args.merged or [])
    record["branches"] = list(args.branch or [])
    record["tracker_ids"] = list(args.tracker or [])
    return record


def _unique_entry_path(runs_dir: Path, slug: str) -> Path:
    """Return a non-colliding entry path for this run's timestamp slug.

    Second-precision slugs collide when two closes land in the same wall-clock
    second. The existence check + numeric suffix guarantees a distinct file per
    entry so a close never silently overwrites a prior ledger entry:
    `<slug>.md`, then `<slug>-1.md`, `<slug>-2.md`, ...
    """
    candidate = runs_dir / f"{slug}.md"
    suffix = 1
    while candidate.exists():
        candidate = runs_dir / f"{slug}-{suffix}.md"
        suffix += 1
    return candidate


def _write_run_files(job_dir: Path, record: dict, prose: str, now: datetime) -> None:
    rendered = state_schema.render_frontmatter(record, prose=prose)

    runs_dir = job_dir / RUNS_SUBDIR
    runs_dir.mkdir(parents=True, exist_ok=True)
    entry_path = _unique_entry_path(runs_dir, _timestamp_slug(now))
    entry_path.write_text(rendered, encoding="utf-8")

    (job_dir / LAST_RUN_NAME).write_text(rendered, encoding="utf-8")


def _remove_run_start(job_dir: Path) -> None:
    marker = job_dir / RUN_START_NAME
    if marker.exists():
        marker.unlink()


# --- close: counters block ---------------------------------------------------
def _parse_counters_block(text: str) -> tuple[int, str]:
    """Extract (consecutive_failures, last_success) from an existing block.
    Defaults to (0, "") if absent or unparsable."""
    match = re.search(
        r"consecutive_failures:\s*(\d+)\s*\nlast_success:\s*(.*)", text
    )
    if not match:
        return 0, ""
    try:
        failures = int(match.group(1))
    except ValueError:
        failures = 0
    last_success = match.group(2).strip()
    return failures, last_success


def _render_counters_block(consecutive_failures: int, last_success: str) -> str:
    return (
        f"{COUNTERS_START}\n"
        f"consecutive_failures: {consecutive_failures}\n"
        f"last_success: {last_success}\n"
        f"{COUNTERS_END}"
    )


def _update_memory_counters(
    job_dir: Path, consecutive_failures: int, last_success: str
) -> None:
    memory_path = job_dir / MEMORY_NAME
    new_block = _render_counters_block(consecutive_failures, last_success)

    if not memory_path.is_file():
        memory_path.write_text(new_block + "\n", encoding="utf-8")
        return

    existing = memory_path.read_text(encoding="utf-8")
    if _COUNTERS_BLOCK_RE.search(existing):
        updated = _COUNTERS_BLOCK_RE.sub(new_block, existing, count=1)
    else:
        sep = "" if existing.endswith("\n") else "\n"
        updated = existing + sep + "\n" + new_block + "\n"
    memory_path.write_text(updated, encoding="utf-8")


def _read_counters(job_dir: Path) -> tuple[int, str]:
    memory_path = job_dir / MEMORY_NAME
    if not memory_path.is_file():
        return 0, ""
    match = _COUNTERS_BLOCK_RE.search(memory_path.read_text(encoding="utf-8"))
    if not match:
        return 0, ""
    return _parse_counters_block(match.group(0))


def _next_counters(
    outcome: str, prev_failures: int, prev_last_success: str, when_iso: str
) -> tuple[int, str]:
    if outcome in SUCCESS_OUTCOMES:
        return 0, when_iso
    return prev_failures + 1, prev_last_success


# --- close: main command ------------------------------------------------------
def cmd_close(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    now = _utcnow()

    runtime_s = _compute_runtime_s(job_dir, now)
    record = _build_record(args, now, runtime_s)
    _write_run_files(job_dir, record, args.notes or "", now)
    _remove_run_start(job_dir)

    prev_failures, prev_last_success = _read_counters(job_dir)
    consecutive_failures, last_success = _next_counters(
        args.outcome, prev_failures, prev_last_success, record["when"]
    )
    _update_memory_counters(job_dir, consecutive_failures, last_success)

    if consecutive_failures >= args.threshold:
        print(
            f"ESCALATE: {consecutive_failures} consecutive failures "
            f"(failure_class={args.failure_class}, stop_reason={args.stop_reason}) "
            f"— queue a human-approval item"
        )
        return EXIT_ESCALATE

    print(f"CLOSED outcome={args.outcome} consecutive_failures={consecutive_failures}")
    return 0


# --- CLI -----------------------------------------------------------------------
def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic run ledger + escalation counters for automations.")
    sub = ap.add_subparsers(dest="command", required=True)

    o = sub.add_parser("open", help="record run start (writes .run-start marker)")
    o.add_argument("job_dir", help="job directory")
    o.set_defaults(func=cmd_open)

    c = sub.add_parser("close", help="record run close, update ledger + counters")
    c.add_argument("job_dir", help="job directory")
    c.add_argument("--outcome", required=True, help="e.g. success, no-op, blocked, failed")
    c.add_argument("--units", type=int, required=True, help="units_completed (int)")
    c.add_argument("--stop-reason", required=True, help="why the run stopped")
    c.add_argument("--failure-class", required=True,
                    help="failure taxonomy class (e.g. 'none' on success)")
    c.add_argument("--merged", action="append", default=None,
                    help="merged commit SHA (repeatable)")
    c.add_argument("--branch", action="append", default=None,
                    help="branch touched this run (repeatable)")
    c.add_argument("--tracker", action="append", default=None,
                    help="tracker/issue id touched this run (repeatable)")
    c.add_argument("--notes", default="", help="free-text prose for the run entry")
    c.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD,
                    help=f"consecutive-failure escalation threshold (default: {DEFAULT_THRESHOLD})")
    c.set_defaults(func=cmd_close)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
