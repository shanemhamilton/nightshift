#!/usr/bin/env python3
"""
run_lock.py — deterministic concurrency-lock helper for automations.

Implements the lock semantics prosed in the Automation Optimizer Protocol v4
managed block (see optimize_codex_automations.py BLOCK_HISTORY[4]): the lock
is a DIRECTORY created with `os.mkdir` (atomic — raises FileExistsError if it
already exists, which is the contention signal). Owner info is recorded in a
FIXED-format `owner` file INSIDE that directory, so every agent that acquires
a lock writes the same shape instead of improvising (the live fleet today has
inconsistent `token=` vs `run_token=` owner files because agents free-form it).

Subcommands: acquire, status, release, reclaim, extend. See --help on each.

Exit codes:
  0  success
  1  usage error (bad args, missing job dir, etc.)
  2  DEFER — lock is contended/not-abandoned; this is a normal, expected
     outcome for a concurrent run, never an uncaught exception.

Owner file format (one `key: value` line each, ISO 8601 UTC timestamps):
  token: <unique run token>
  pid: <int>
  host: <str>
  cwd: <str>
  start: <ISO8601 UTC>
  lease_until: <ISO8601 UTC>

Stdlib only (Python 3.11+): argparse, pathlib, os, socket, json, datetime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCK_DIR_NAME = ".automation.lock"
WORKSPACE_LOCKS_SUBDIR = ".workspace-locks"
OWNER_FILE_NAME = "owner"
DEFAULT_LEASE_MINUTES = 120
OWNER_FIELDS = ("token", "pid", "host", "cwd", "start", "lease_until")
EXIT_USAGE = 1
EXIT_DEFER = 2


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def make_token(pid: int, host: str, start_iso: str) -> str:
    """Deterministically-unique token from pid+host+start, no randomness."""
    raw = f"{pid}-{host}-{start_iso}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{pid}-{digest}"


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours
    except OSError:
        return False
    return True


def parse_owner(lock_dir: Path) -> dict | None:
    """Leniently read `<lock_dir>/owner` into a dict. None if missing/unreadable."""
    owner_path = lock_dir / OWNER_FILE_NAME
    if not owner_path.is_file():
        return None
    data: dict[str, str] = {}
    try:
        text = owner_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        data[key.strip()] = value.strip()
    return data or None


def write_owner(lock_dir: Path, owner: dict) -> None:
    lines = [f"{k}: {owner[k]}" for k in OWNER_FIELDS if k in owner]
    (lock_dir / OWNER_FILE_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_owner(lease_minutes: int) -> dict:
    start = _utcnow()
    pid = os.getpid()
    host = socket.gethostname()
    start_iso = _iso(start)
    return {
        "token": make_token(pid, host, start_iso),
        "pid": str(pid),
        "host": host,
        "cwd": os.getcwd(),
        "start": start_iso,
        "lease_until": _iso(start + timedelta(minutes=lease_minutes)),
    }


def is_abandoned(owner: dict | None) -> bool:
    """True only when PID is provably dead AND the lease has expired."""
    if owner is None:
        return True
    pid_alive = _owner_pid_alive(owner)
    lease_expired = _owner_lease_expired(owner)
    return (not pid_alive) and lease_expired


def _owner_pid_alive(owner: dict) -> bool:
    try:
        pid = int(owner.get("pid", ""))
    except ValueError:
        return False
    return is_pid_alive(pid)


def _owner_lease_expired(owner: dict) -> bool:
    lease_raw = owner.get("lease_until")
    if not lease_raw:
        return True
    try:
        return _utcnow() >= _parse_iso(lease_raw)
    except ValueError:
        return True


def _acquire_single(lock_dir: Path, lease_minutes: int) -> dict | None:
    """Try to mkdir + write owner. Returns the owner dict on success, else None
    (lock already existed — caller decides DEFER vs. something else)."""
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        return None
    owner = build_owner(lease_minutes)
    write_owner(lock_dir, owner)
    return owner


def _defer_line(owner: dict | None) -> str:
    if owner is None:
        return "DEFER lock held by <unknown> until <unknown>"
    token = owner.get("token", "<unknown>")
    lease_until = owner.get("lease_until", "<unknown>")
    return f"DEFER lock held by {token} until {lease_until}"


def _workspace_lock_dir(locks_root: Path, workspace: str) -> Path:
    slug = _slugify(workspace)
    return locks_root / WORKSPACE_LOCKS_SUBDIR / f"{slug}.lock"


def _slugify(s: str) -> str:
    """Local mirror of naming.slugify — avoids a sys.path-order dependency
    when this script is invoked directly (e.g. `python3 scripts/run_lock.py`)
    from an arbitrary working directory."""
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    from naming import slugify

    return slugify(s)


def _job_lock_dir(job_dir: Path) -> Path:
    return job_dir / LOCK_DIR_NAME


def cmd_acquire(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir)
    job_lock = _job_lock_dir(job_dir)
    job_owner = _acquire_single(job_lock, args.lease_minutes)
    if job_owner is None:
        existing = parse_owner(job_lock)
        print(_defer_line(existing))
        return EXIT_DEFER

    locks_root = Path(args.locks_root).expanduser()
    acquired_workspaces: list[Path] = []
    for workspace in args.workspace or []:
        ws_lock = _workspace_lock_dir(locks_root, workspace)
        ws_lock.parent.mkdir(parents=True, exist_ok=True)
        ws_owner = _acquire_single(ws_lock, args.lease_minutes)
        if ws_owner is None:
            _rollback_acquire(job_lock, job_owner["token"], acquired_workspaces)
            existing = parse_owner(ws_lock)
            print(f"DEFER workspace contended: {workspace} "
                  f"held by {existing.get('token') if existing else '<unknown>'} "
                  f"until {existing.get('lease_until') if existing else '<unknown>'}")
            return EXIT_DEFER
        acquired_workspaces.append(ws_lock)

    print(f"ACQUIRED token={job_owner['token']} lease_until={job_owner['lease_until']}")
    return 0


def _rollback_acquire(job_lock: Path, token: str, workspace_locks: list[Path]) -> None:
    for ws_lock in reversed(workspace_locks):
        _release_if_token_matches(ws_lock, token)
    _release_if_token_matches(job_lock, token)


def _release_if_token_matches(lock_dir: Path, token: str) -> bool:
    owner = parse_owner(lock_dir)
    if owner is None or owner.get("token") != token:
        return False
    _rmdir_lock(lock_dir)
    return True


def _rmdir_lock(lock_dir: Path) -> None:
    owner_file = lock_dir / OWNER_FILE_NAME
    if owner_file.exists():
        owner_file.unlink()
    lock_dir.rmdir()


def cmd_release(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir)
    job_lock = _job_lock_dir(job_dir)
    locks_root = Path(args.locks_root).expanduser()

    for workspace in reversed(args.workspace or []):
        ws_lock = _workspace_lock_dir(locks_root, workspace)
        _release_if_token_matches(ws_lock, args.token)

    owner = parse_owner(job_lock)
    if owner is None or owner.get("token") != args.token:
        print("REFUSED wrong token")
        return EXIT_DEFER
    _rmdir_lock(job_lock)
    print("RELEASED")
    return 0


def cmd_reclaim(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir)
    job_lock = _job_lock_dir(job_dir)
    owner = parse_owner(job_lock)

    if not job_lock.is_dir():
        print("DEFER nothing to reclaim (no lock present)")
        return EXIT_DEFER
    if not is_abandoned(owner):
        print(_defer_line(owner))
        return EXIT_DEFER

    old_token = owner.get("token", "<unknown>") if owner else "<unknown>"
    _rmdir_lock(job_lock)
    new_owner = _acquire_single(job_lock, DEFAULT_LEASE_MINUTES)
    if new_owner is None:
        # Another run beat us to it during the reclaim window.
        foreign = parse_owner(job_lock)
        print(_defer_line(foreign))
        return EXIT_DEFER

    # Read back and confirm it holds OUR token (guards a same-window race).
    confirm = parse_owner(job_lock)
    if confirm is None or confirm.get("token") != new_owner["token"]:
        print(_defer_line(confirm))
        return EXIT_DEFER

    print(f"RECLAIMED old_token={old_token} new_token={new_owner['token']}")
    return 0


def cmd_extend(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir)
    job_lock = _job_lock_dir(job_dir)
    owner = parse_owner(job_lock)
    if owner is None:
        print("DEFER nothing to extend (no lock present)")
        return EXIT_DEFER
    if args.token is not None and owner.get("token") != args.token:
        print("REFUSED wrong token")
        return EXIT_DEFER

    new_lease = _iso(_utcnow() + timedelta(minutes=args.minutes))
    owner["lease_until"] = new_lease
    write_owner(job_lock, owner)
    print(f"EXTENDED lease_until={new_lease}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir)
    job_lock = _job_lock_dir(job_dir)
    owner = parse_owner(job_lock)
    held = job_lock.is_dir()
    result = {
        "held": held,
        "owner": owner,
        "abandoned": is_abandoned(owner) if held else None,
    }
    print(json.dumps(result))
    return 0


def _add_common_lock_args(p: argparse.ArgumentParser, *, with_lease: bool = False,
                           with_workspace: bool = False) -> None:
    p.add_argument("job_dir", help="job directory containing the lock")
    p.add_argument("--locks-root", default="~/.codex/automations",
                    help="root for workspace locks (default: ~/.codex/automations; "
                         "override for tests)")
    if with_lease:
        p.add_argument("--lease-minutes", type=int, default=DEFAULT_LEASE_MINUTES,
                        help=f"lease length in minutes (default: {DEFAULT_LEASE_MINUTES})")
    if with_workspace:
        p.add_argument("--workspace", action="append", default=None,
                        help="workspace path to also lock (repeatable, in order)")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic concurrency-lock helper (mkdir-based, atomic).")
    sub = ap.add_subparsers(dest="command", required=True)

    a = sub.add_parser("acquire", help="acquire the job lock (+ optional workspace locks)")
    _add_common_lock_args(a, with_lease=True, with_workspace=True)
    a.set_defaults(func=cmd_acquire)

    s = sub.add_parser("status", help="print lock status as JSON")
    _add_common_lock_args(s)
    s.set_defaults(func=cmd_status)

    r = sub.add_parser("release", help="release the job lock (+ optional workspace locks)")
    _add_common_lock_args(r, with_workspace=True)
    r.add_argument("--token", required=True, help="owner token; must match to release")
    r.set_defaults(func=cmd_release)

    rc = sub.add_parser("reclaim", help="reclaim a provably-abandoned job lock")
    _add_common_lock_args(rc)
    rc.set_defaults(func=cmd_reclaim)

    e = sub.add_parser("extend", help="extend the lease on a held job lock")
    _add_common_lock_args(e)
    e.add_argument("--minutes", type=int, required=True, help="new lease length in minutes")
    e.add_argument("--token", default=None, help="owner token; must match if given")
    e.set_defaults(func=cmd_extend)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError) as exc:
        print(f"DEFER error: {exc}")
        return EXIT_DEFER


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main(sys.argv[1:]))
