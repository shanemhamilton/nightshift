#!/usr/bin/env python3
"""
fingerprint.py — deterministic change detection for automations.

Snapshots the observable state of one or more git repos (HEAD sha + dirty
working tree) and/or arbitrary shell commands (hashed stdout), then lets a
later run diff against that snapshot to decide whether anything actually
changed since last time — so a job can skip work when nothing moved instead
of redoing it blindly every run.

Subcommands: snapshot, diff. See --help on each.

Snapshot file (<job_dir>/state-fingerprints.json), deterministic + sorted:
  {
    "generated_at": "<ISO8601 UTC>",
    "fingerprints": {
       "repo_head:<repo_path>": "<HEAD sha, or 'unavailable'>",
       "repo_dirty:<repo_path>": "<sha256 of `git status --porcelain`, or 'unavailable'>",
       "cmd:<the command string>": "<sha256 of stdout, or 'unavailable'>"
    }
  }

Exit codes:
  0  success (snapshot written; or diff found UNCHANGED)
  1  diff found CHANGED, or no prior snapshot exists to diff against

A missing repo, a repo that isn't a git repo, or a command that fails/times
out never raises — the affected fingerprint is simply recorded as the string
"unavailable" and treated as its own distinct, comparable value.

Stdlib only (Python 3.11+): argparse, pathlib, subprocess, hashlib, json,
datetime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SNAPSHOT_FILE_NAME = "state-fingerprints.json"
SUBPROCESS_TIMEOUT_SECONDS = 5
UNAVAILABLE = "unavailable"
EXIT_CHANGED = 1


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run(cmd: list[str] | str, *, cwd: Path, shell: bool) -> str | None:
    """Run a command with a fixed timeout, capturing stdout. Returns None
    (never raises) on any failure, timeout, or nonzero exit."""
    try:
        result = subprocess.run(
            cmd, cwd=str(cwd), shell=shell,
            capture_output=True, text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError, subprocess.CalledProcessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def repo_head_fingerprint(repo: str) -> str:
    repo_path = Path(repo)
    if not repo_path.is_dir():
        return UNAVAILABLE
    stdout = _run(["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                   cwd=repo_path, shell=False)
    if stdout is None:
        return UNAVAILABLE
    return stdout.strip()


def repo_dirty_fingerprint(repo: str) -> str:
    repo_path = Path(repo)
    if not repo_path.is_dir():
        return UNAVAILABLE
    stdout = _run(["git", "-C", str(repo_path), "status", "--porcelain"],
                   cwd=repo_path, shell=False)
    if stdout is None:
        return UNAVAILABLE
    return _sha256(stdout)


def cmd_fingerprint(command: str, *, cwd: Path) -> str:
    stdout = _run(command, cwd=cwd, shell=True)
    if stdout is None:
        return UNAVAILABLE
    return _sha256(stdout)


def compute_fingerprints(job_dir: Path, repos: list[str], cmds: list[str]) -> dict[str, str]:
    """Compute the full fingerprint map, sorted by key for determinism."""
    fingerprints: dict[str, str] = {}
    for repo in repos:
        fingerprints[f"repo_head:{repo}"] = repo_head_fingerprint(repo)
        fingerprints[f"repo_dirty:{repo}"] = repo_dirty_fingerprint(repo)

    cmd_cwd = Path(repos[0]) if repos else job_dir
    for command in cmds:
        fingerprints[f"cmd:{command}"] = cmd_fingerprint(command, cwd=cmd_cwd)

    return dict(sorted(fingerprints.items()))


def snapshot_path(job_dir: Path) -> Path:
    return job_dir / SNAPSHOT_FILE_NAME


def write_snapshot(job_dir: Path, fingerprints: dict[str, str]) -> dict:
    snapshot = {
        "generated_at": _utcnow_iso(),
        "fingerprints": fingerprints,
    }
    job_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path(job_dir).write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return snapshot


def load_snapshot(job_dir: Path) -> dict | None:
    path = snapshot_path(job_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def cmd_snapshot(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir)
    fingerprints = compute_fingerprints(job_dir, args.repo or [], args.cmd or [])
    write_snapshot(job_dir, fingerprints)
    print(f"SNAPSHOT wrote {len(fingerprints)} fingerprints to {snapshot_path(job_dir)}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir)
    prior = load_snapshot(job_dir)
    if prior is None:
        print(f"NO-PRIOR-SNAPSHOT no {SNAPSHOT_FILE_NAME} found in {job_dir}")
        return EXIT_CHANGED

    current = compute_fingerprints(job_dir, args.repo or [], args.cmd or [])
    prior_fingerprints = prior.get("fingerprints", {})

    all_keys = set(prior_fingerprints) | set(current)
    changed_keys = sorted(
        key for key in all_keys
        if prior_fingerprints.get(key) != current.get(key)
    )

    if not changed_keys:
        print("UNCHANGED no fingerprints moved since last snapshot")
        exit_code = 0
    else:
        print("CHANGED the following fingerprints moved:")
        for key in changed_keys:
            print(key)
        exit_code = EXIT_CHANGED

    if args.update:
        write_snapshot(job_dir, current)
        print(f"UPDATED snapshot rewritten with current values ({len(current)} fingerprints)")

    return exit_code


def _add_common_target_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--job-dir", required=True,
                    help="job directory; snapshot file lives at <job-dir>/state-fingerprints.json")
    p.add_argument("--repo", action="append", default=[],
                    help="path to a git repo to fingerprint (repeatable)")
    p.add_argument("--cmd", action="append", default=[],
                    help="shell command whose stdout is hashed (repeatable)")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic change detection via repo + command fingerprints.")
    sub = ap.add_subparsers(dest="command", required=True)

    s = sub.add_parser("snapshot", help="compute and write the current fingerprints")
    _add_common_target_args(s)
    s.set_defaults(func=cmd_snapshot)

    d = sub.add_parser("diff", help="compare current fingerprints against the stored snapshot")
    _add_common_target_args(d)
    d.add_argument("--update", action="store_true",
                    help="after reporting, rewrite the snapshot with current values")
    d.set_defaults(func=cmd_diff)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
