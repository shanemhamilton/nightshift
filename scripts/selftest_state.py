#!/usr/bin/env python3
"""
selftest_state.py — checks for the run_lock.py concurrency-lock helper.

Shells out to `python3 scripts/run_lock.py ...` (cleanest way to exercise a
CLI end-to-end) against a temp job dir + temp --locks-root. NEVER touches the
real ~/.codex. More test functions can be appended by registering them in
TESTS below; main() runs the full registry.

Run: python3 scripts/selftest_state.py   (exit 0 = all pass)
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN_LOCK = HERE / "run_lock.py"
FINGERPRINT = HERE / "fingerprint.py"
RUN_LEDGER = HERE / "run_ledger.py"
STATE_SCHEMA_PATH = HERE / "state_schema.py"
FLEET_HEALTH_PATH = HERE / "fleet_health.py"
FLEET_REPORT_PATH = HERE / "fleet_report.py"

_spec = importlib.util.spec_from_file_location("state_schema", STATE_SCHEMA_PATH)
STATE_SCHEMA = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(STATE_SCHEMA)

_fh_spec = importlib.util.spec_from_file_location("fleet_health", FLEET_HEALTH_PATH)
FLEET_HEALTH = importlib.util.module_from_spec(_fh_spec)
_fh_spec.loader.exec_module(FLEET_HEALTH)

_fr_spec = importlib.util.spec_from_file_location("fleet_report", FLEET_REPORT_PATH)
FLEET_REPORT = importlib.util.module_from_spec(_fr_spec)
_fr_spec.loader.exec_module(FLEET_REPORT)

_rl_spec = importlib.util.spec_from_file_location("run_lock_mod", RUN_LOCK)
RUN_LOCK_MOD = importlib.util.module_from_spec(_rl_spec)
_rl_spec.loader.exec_module(RUN_LOCK_MOD)

PASSED = 0
FAILED = 0

# Registry of test functions. Each entry is (name, callable(tmp_root: Path)).
# Append here (or via TESTS.append(...)) to add more coverage later.
TESTS: list[tuple[str, "callable"]] = []


def register(name: str):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


def run_lock(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RUN_LOCK), *args],
        capture_output=True, text=True,
    )


def make_job_dir(root: Path, name: str = "job") -> Path:
    job_dir = root / name
    job_dir.mkdir(parents=True, exist_ok=True)
    return job_dir


def fingerprint(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(FINGERPRINT), *args],
        capture_output=True, text=True,
    )


def run_ledger(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RUN_LEDGER), *args],
        capture_output=True, text=True,
    )


def make_scratch_git_repo(root: Path, name: str = "scratch-repo") -> Path:
    """Create a deterministic scratch git repo with one commit. Identity is
    passed via -c flags on each invocation so results never depend on
    ambient/global git config."""
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    (repo / "file.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=a@b.c", "-c", "user.name=test",
         "add", "file.txt"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=a@b.c", "-c", "user.name=test",
         "commit", "-q", "-m", "initial commit"],
        check=True,
    )
    return repo


def make_scratch_commit(repo: Path, message: str = "second commit") -> None:
    (repo / "file.txt").write_text("v2\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=a@b.c", "-c", "user.name=test",
         "add", "file.txt"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=a@b.c", "-c", "user.name=test",
         "commit", "-q", "-m", message],
        check=True,
    )


def craft_owner_file(lock_dir: Path, *, pid: int, lease_until: datetime,
                      token: str = "crafted-token") -> None:
    """Hand-craft a lock dir + owner file, bypassing run_lock.py's acquire,
    so tests can simulate abandoned or foreign locks deterministically."""
    lock_dir.mkdir(parents=True, exist_ok=True)
    start = lease_until - timedelta(minutes=60)
    owner_lines = [
        f"token: {token}",
        f"pid: {pid}",
        "host: test-host",
        "cwd: /tmp/test",
        f"start: {start.isoformat()}",
        f"lease_until: {lease_until.isoformat()}",
    ]
    (lock_dir / "owner").write_text("\n".join(owner_lines) + "\n", encoding="utf-8")


@register("fresh acquire succeeds and writes a fixed-format owner file")
def test_fresh_acquire(root: Path) -> None:
    job_dir = make_job_dir(root, "job-fresh")
    result = run_lock("acquire", str(job_dir))
    check("acquire exit 0", result.returncode == 0, result.stdout + result.stderr)
    check("stdout begins ACQUIRED", result.stdout.startswith("ACQUIRED"), result.stdout)

    lock_dir = job_dir / ".automation.lock"
    owner_file = lock_dir / "owner"
    check("lock dir exists", lock_dir.is_dir())
    check("owner file exists", owner_file.is_file())

    fields = {"token", "pid", "host", "cwd", "start", "lease_until"}
    owner_text = owner_file.read_text(encoding="utf-8")
    found = {line.split(":", 1)[0].strip() for line in owner_text.splitlines() if ":" in line}
    check("owner has all 6 fixed fields", fields <= found, str(found))


@register("contended acquire defers with exit 2, original owner untouched")
def test_contended_defer(root: Path) -> None:
    job_dir = make_job_dir(root, "job-contended")
    first = run_lock("acquire", str(job_dir))
    check("first acquire exit 0", first.returncode == 0, first.stdout)

    owner_before = (job_dir / ".automation.lock" / "owner").read_text(encoding="utf-8")
    second = run_lock("acquire", str(job_dir))
    check("second acquire exit 2 (DEFER)", second.returncode == 2, second.stdout)
    check("second acquire prints DEFER", second.stdout.startswith("DEFER"), second.stdout)

    owner_after = (job_dir / ".automation.lock" / "owner").read_text(encoding="utf-8")
    check("original owner untouched", owner_before == owner_after)


@register("dead-pid + expired-lease lock is reclaimable")
def test_reclaim_abandoned(root: Path) -> None:
    job_dir = make_job_dir(root, "job-abandoned")
    lock_dir = job_dir / ".automation.lock"
    craft_owner_file(
        lock_dir, pid=999999,
        lease_until=datetime.now(timezone.utc) - timedelta(minutes=5),
        token="dead-token",
    )

    result = run_lock("reclaim", str(job_dir))
    check("reclaim exit 0", result.returncode == 0, result.stdout + result.stderr)
    check("stdout begins RECLAIMED", result.stdout.startswith("RECLAIMED"), result.stdout)
    check("old_token=dead-token present", "old_token=dead-token" in result.stdout, result.stdout)

    new_owner = (lock_dir / "owner").read_text(encoding="utf-8")
    check("new owner token differs from dead-token", "token: dead-token" not in new_owner)


@register("reclaim refuses a non-abandoned lock (live pid, fresh lease)")
def test_reclaim_refuses_live(root: Path) -> None:
    job_dir = make_job_dir(root, "job-live")
    lock_dir = job_dir / ".automation.lock"
    craft_owner_file(
        lock_dir, pid=os.getpid(),  # this test process itself: guaranteed alive
        lease_until=datetime.now(timezone.utc) + timedelta(minutes=60),
        token="live-token",
    )

    result = run_lock("reclaim", str(job_dir))
    check("reclaim on live lock exit 2 (DEFER)", result.returncode == 2, result.stdout)
    check("reclaim on live lock refused", result.stdout.startswith("DEFER"), result.stdout)

    still_there = (lock_dir / "owner").read_text(encoding="utf-8")
    check("live-token lock left in place", "token: live-token" in still_there)


@register("wrong-token release refused, correct-token release succeeds")
def test_release_token_check(root: Path) -> None:
    job_dir = make_job_dir(root, "job-release")
    acquired = run_lock("acquire", str(job_dir))
    token = acquired.stdout.split("token=", 1)[1].split()[0]

    bogus = run_lock("release", str(job_dir), "--token", "BOGUS")
    check("bogus release exit != 0", bogus.returncode != 0, bogus.stdout)
    check("bogus release REFUSED", bogus.stdout.startswith("REFUSED"), bogus.stdout)
    check("lock still present after bogus release", (job_dir / ".automation.lock").is_dir())

    correct = run_lock("release", str(job_dir), "--token", token)
    check("correct release exit 0", correct.returncode == 0, correct.stdout)
    check("correct release RELEASED", correct.stdout.startswith("RELEASED"), correct.stdout)
    check("lock gone after correct release", not (job_dir / ".automation.lock").exists())


@register("workspace lock contention between two job dirs; loser's job lock not left behind")
def test_workspace_contention(root: Path) -> None:
    locks_root = root / "locks-root"
    workspace = str(root / "shared-workspace")
    job_a = make_job_dir(root, "job-a")
    job_b = make_job_dir(root, "job-b")

    result_a = run_lock("acquire", str(job_a), "--workspace", workspace,
                         "--locks-root", str(locks_root))
    check("jobA acquire with workspace exit 0", result_a.returncode == 0, result_a.stdout)

    result_b = run_lock("acquire", str(job_b), "--workspace", workspace,
                         "--locks-root", str(locks_root))
    check("jobB acquire exit 2 (workspace contended)", result_b.returncode == 2, result_b.stdout)
    check("jobB DEFER mentions workspace",
          "DEFER" in result_b.stdout and "workspace" in result_b.stdout.lower(),
          result_b.stdout)
    check("jobB's own job lock not left behind",
          not (job_b / ".automation.lock").exists())
    check("jobA's job lock still held", (job_a / ".automation.lock").is_dir())


@register("status output parses as JSON and reflects held/free")
def test_status_json(root: Path) -> None:
    job_dir = make_job_dir(root, "job-status")

    free = run_lock("status", str(job_dir))
    check("status (free) exit 0", free.returncode == 0, free.stdout)
    free_data = json.loads(free.stdout)
    check("status (free) held=False", free_data.get("held") is False, free.stdout)

    run_lock("acquire", str(job_dir))
    held = run_lock("status", str(job_dir))
    held_data = json.loads(held.stdout)
    check("status (held) held=True", held_data.get("held") is True, held.stdout)
    check("status (held) owner present", held_data.get("owner") is not None, held.stdout)


@register("fingerprint: snapshot then diff with no change reports UNCHANGED, exit 0")
def test_fingerprint_unchanged(root: Path) -> None:
    job_dir = make_job_dir(root, "fp-unchanged")
    repo = make_scratch_git_repo(root, "fp-unchanged-repo")

    snap = fingerprint("snapshot", "--job-dir", str(job_dir), "--repo", str(repo))
    check("snapshot exit 0", snap.returncode == 0, snap.stdout + snap.stderr)
    check("snapshot file written", (job_dir / "state-fingerprints.json").is_file())

    diff = fingerprint("diff", "--job-dir", str(job_dir), "--repo", str(repo))
    check("diff (no change) exit 0", diff.returncode == 0, diff.stdout + diff.stderr)
    check("diff (no change) starts UNCHANGED", diff.stdout.startswith("UNCHANGED"), diff.stdout)


@register("fingerprint: new commit is detected as a repo_head change, exit 1")
def test_fingerprint_repo_head_changed(root: Path) -> None:
    job_dir = make_job_dir(root, "fp-head-changed")
    repo = make_scratch_git_repo(root, "fp-head-changed-repo")

    fingerprint("snapshot", "--job-dir", str(job_dir), "--repo", str(repo))
    make_scratch_commit(repo)

    diff = fingerprint("diff", "--job-dir", str(job_dir), "--repo", str(repo))
    check("diff (new commit) exit 1", diff.returncode == 1, diff.stdout + diff.stderr)
    check("diff (new commit) starts CHANGED", diff.stdout.startswith("CHANGED"), diff.stdout)
    check("diff names repo_head key", f"repo_head:{repo}" in diff.stdout, diff.stdout)


@register("fingerprint: changed --cmd stdout is detected as a cmd change, exit 1")
def test_fingerprint_cmd_changed(root: Path) -> None:
    job_dir = make_job_dir(root, "fp-cmd-changed")
    watched = root / "fp-cmd-watched.txt"
    watched.write_text("before\n", encoding="utf-8")
    cmd = f"cat {watched}"

    fingerprint("snapshot", "--job-dir", str(job_dir), "--cmd", cmd)
    watched.write_text("after\n", encoding="utf-8")

    diff = fingerprint("diff", "--job-dir", str(job_dir), "--cmd", cmd)
    check("diff (cmd output changed) exit 1", diff.returncode == 1, diff.stdout + diff.stderr)
    check("diff (cmd output changed) starts CHANGED", diff.stdout.startswith("CHANGED"), diff.stdout)
    check("diff names cmd: key", f"cmd:{cmd}" in diff.stdout, diff.stdout)


@register("fingerprint: missing repo path stores 'unavailable' and does not crash")
def test_fingerprint_missing_repo(root: Path) -> None:
    job_dir = make_job_dir(root, "fp-missing-repo")
    missing = root / "fp-nonexistent-repo"

    snap = fingerprint("snapshot", "--job-dir", str(job_dir), "--repo", str(missing))
    check("snapshot on missing repo exit 0", snap.returncode == 0, snap.stdout + snap.stderr)

    data = json.loads((job_dir / "state-fingerprints.json").read_text(encoding="utf-8"))
    fps = data.get("fingerprints", {})
    check("repo_head:<missing> is 'unavailable'",
          fps.get(f"repo_head:{missing}") == "unavailable", str(fps))
    check("repo_dirty:<missing> is 'unavailable'",
          fps.get(f"repo_dirty:{missing}") == "unavailable", str(fps))


@register("state_schema: NEW frontmatter shape parses with typed fields")
def test_state_schema_frontmatter(root: Path) -> None:
    fixture = root / "state-schema-frontmatter" / "last-run.md"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(
        "---\n"
        "when: 2026-07-01T06:00:00Z\n"
        "outcome: success\n"
        "units_completed: 3\n"
        "stop_reason: queue-drained\n"
        "failure_class: none\n"
        "runtime_s: 420\n"
        "merged_shas: [abc123, def456]\n"
        "branches: [feature/x]\n"
        "tracker_ids: [JIRA-12]\n"
        "---\n"
        "Completed three units, nothing blocked.\n",
        encoding="utf-8",
    )

    record = STATE_SCHEMA.parse_last_run(fixture)
    check("frontmatter: does not return None", record is not None)
    check("frontmatter: schema == 'frontmatter'",
          record is not None and record.get("schema") == "frontmatter", str(record))
    check("frontmatter: units_completed == 3 (int)",
          record is not None and record.get("units_completed") == 3, str(record))
    check("frontmatter: merged_shas == ['abc123', 'def456']",
          record is not None and record.get("merged_shas") == ["abc123", "def456"],
          str(record))
    check("frontmatter: branches == ['feature/x']",
          record is not None and record.get("branches") == ["feature/x"], str(record))
    check("frontmatter: outcome == 'success'",
          record is not None and record.get("outcome") == "success", str(record))


@register("state_schema: LEGACY TEMPLATE bullet shape parses without raising")
def test_state_schema_template(root: Path) -> None:
    fixture = root / "state-schema-template" / "last-run.md"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(
        "# Last run\n"
        "- when:\n"
        "- outcome: success\n"
        "- runtime_s: 12\n"
        "- units_completed: 2\n"
        "- stop_reason: budget\n"
        "- failure_class: none\n"
        "- rollback: n/a\n"
        "- notes: ran fine\n",
        encoding="utf-8",
    )

    record = STATE_SCHEMA.parse_last_run(fixture)
    check("template: does not return None", record is not None)
    check("template: schema == 'template'",
          record is not None and record.get("schema") == "template", str(record))
    check("template: outcome == 'success'",
          record is not None and record.get("outcome") == "success", str(record))
    check("template: units_completed == 2 (int)",
          record is not None and record.get("units_completed") == 2, str(record))
    check("template: unknown 'rollback' bullet ignored without raising", True)


@register("state_schema: freeform prose shape returns schema=legacy with raw text")
def test_state_schema_legacy_prose(root: Path) -> None:
    fixture = root / "state-schema-legacy" / "last-run.md"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    prose = "Ran the nightly loop, merged two branches, nothing blocked.\n"
    fixture.write_text(prose, encoding="utf-8")

    record = STATE_SCHEMA.parse_last_run(fixture)
    check("legacy prose: does not return None", record is not None)
    check("legacy prose: schema == 'legacy'",
          record is not None and record.get("schema") == "legacy", str(record))
    check("legacy prose: raw contains the prose text",
          record is not None and "merged two branches" in (record.get("raw") or ""),
          str(record))


@register("state_schema: nonexistent path returns None, never raises")
def test_state_schema_missing_file(root: Path) -> None:
    missing = root / "state-schema-missing" / "last-run.md"
    result = STATE_SCHEMA.parse_last_run(missing)
    check("missing file: parse_last_run returns None", result is None, str(result))
    entry_result = STATE_SCHEMA.parse_run_entry(missing)
    check("missing file: parse_run_entry returns None", entry_result is None, str(entry_result))


@register("run_ledger: open then close --outcome success writes last-run.md + runs entry + counters")
def test_run_ledger_success_close(root: Path) -> None:
    job_dir = make_job_dir(root, "ledger-success")

    opened = run_ledger("open", str(job_dir))
    check("open exit 0", opened.returncode == 0, opened.stdout + opened.stderr)
    check("open prints OPENED", opened.stdout.startswith("OPENED"), opened.stdout)

    closed = run_ledger(
        "close", str(job_dir),
        "--outcome", "success", "--units", "2",
        "--stop-reason", "queue-drained", "--failure-class", "none",
    )
    check("close exit 0", closed.returncode == 0, closed.stdout + closed.stderr)
    check("close prints CLOSED", closed.stdout.startswith("CLOSED"), closed.stdout)

    last_run_path = job_dir / "last-run.md"
    check("last-run.md exists", last_run_path.is_file())
    record = STATE_SCHEMA.parse_last_run(last_run_path)
    check("last-run: schema == 'frontmatter'",
          record is not None and record.get("schema") == "frontmatter", str(record))
    check("last-run: outcome == 'success'",
          record is not None and record.get("outcome") == "success", str(record))
    check("last-run: units_completed == 2 (int)",
          record is not None and record.get("units_completed") == 2, str(record))

    runs_dir = job_dir / "runs"
    run_entries = list(runs_dir.glob("*.md")) if runs_dir.is_dir() else []
    check("a run entry file exists under runs/", len(run_entries) == 1, str(run_entries))

    memory_text = (job_dir / "memory.md").read_text(encoding="utf-8")
    check("memory.md has ao:counters block",
          "<!-- ao:counters -->" in memory_text and "<!-- /ao:counters -->" in memory_text,
          memory_text)
    check("memory.md consecutive_failures: 0", "consecutive_failures: 0" in memory_text, memory_text)
    check("memory.md last_success is set",
          re.search(r"last_success:\s*\S+", memory_text) is not None, memory_text)


@register("run_ledger: three consecutive failure closes escalate at threshold (default 3)")
def test_run_ledger_escalation(root: Path) -> None:
    job_dir = make_job_dir(root, "ledger-escalate")

    results = []
    for _ in range(3):
        results.append(run_ledger(
            "close", str(job_dir),
            "--outcome", "blocked-by-dirty-worktree", "--units", "0",
            "--stop-reason", "repeated-failure",
            "--failure-class", "blocked-by-dirty-worktree",
        ))

    check("closes 1-2 exit 0", results[0].returncode == 0 and results[1].returncode == 0,
          str([r.stdout for r in results[:2]]))
    check("3rd close exits 3 (ESCALATE)", results[2].returncode == 3, results[2].stdout)
    check("3rd close prints ESCALATE", results[2].stdout.startswith("ESCALATE:"), results[2].stdout)
    check("ESCALATE reason contains failure_class",
          "failure_class=blocked-by-dirty-worktree" in results[2].stdout, results[2].stdout)

    memory_text = (job_dir / "memory.md").read_text(encoding="utf-8")
    check("memory.md consecutive_failures reached 3",
          "consecutive_failures: 3" in memory_text, memory_text)


@register("run_ledger: success close after escalation resets consecutive_failures to 0")
def test_run_ledger_reset_after_escalation(root: Path) -> None:
    job_dir = make_job_dir(root, "ledger-reset")

    for _ in range(3):
        run_ledger(
            "close", str(job_dir),
            "--outcome", "blocked-by-dirty-worktree", "--units", "0",
            "--stop-reason", "repeated-failure",
            "--failure-class", "blocked-by-dirty-worktree",
        )
    before_text = (job_dir / "memory.md").read_text(encoding="utf-8")
    check("precondition: consecutive_failures: 3 before reset", "consecutive_failures: 3" in before_text,
          before_text)

    reset = run_ledger(
        "close", str(job_dir),
        "--outcome", "success", "--units", "1",
        "--stop-reason", "queue-drained", "--failure-class", "none",
    )
    check("reset close exit 0", reset.returncode == 0, reset.stdout + reset.stderr)

    after_text = (job_dir / "memory.md").read_text(encoding="utf-8")
    check("memory.md consecutive_failures reset to 0", "consecutive_failures: 0" in after_text, after_text)
    last_success_match = re.search(r"last_success:\s*(\S+)", after_text)
    check("memory.md last_success updated (non-empty)",
          last_success_match is not None and last_success_match.group(1) != "", after_text)


@register("run_ledger: merged_shas / branches / tracker_ids round-trip as lists")
def test_run_ledger_list_fields_roundtrip(root: Path) -> None:
    job_dir = make_job_dir(root, "ledger-lists")

    closed = run_ledger(
        "close", str(job_dir),
        "--outcome", "success", "--units", "1",
        "--stop-reason", "queue-drained", "--failure-class", "none",
        "--merged", "abc", "--merged", "def",
        "--branch", "feature/x",
        "--tracker", "T-1",
    )
    check("list-fields close exit 0", closed.returncode == 0, closed.stdout + closed.stderr)

    record = STATE_SCHEMA.parse_last_run(job_dir / "last-run.md")
    check("merged_shas == ['abc', 'def']",
          record is not None and record.get("merged_shas") == ["abc", "def"], str(record))
    check("branches == ['feature/x']",
          record is not None and record.get("branches") == ["feature/x"], str(record))
    check("tracker_ids == ['T-1']",
          record is not None and record.get("tracker_ids") == ["T-1"], str(record))

    runs_dir = job_dir / "runs"
    run_entries = list(runs_dir.glob("*.md"))
    check("exactly one run entry written", len(run_entries) == 1, str(run_entries))
    entry_record = STATE_SCHEMA.parse_run_entry(run_entries[0])
    check("run entry merged_shas == ['abc', 'def']",
          entry_record is not None and entry_record.get("merged_shas") == ["abc", "def"],
          str(entry_record))


@register("run_ledger: close preserves pre-existing human content in memory.md")
def test_run_ledger_preserves_human_memory(root: Path) -> None:
    job_dir = make_job_dir(root, "ledger-preserve")
    human_section = "## Stable decisions\n- keep this line\n"
    (job_dir / "memory.md").write_text(human_section, encoding="utf-8")

    closed = run_ledger(
        "close", str(job_dir),
        "--outcome", "success", "--units", "1",
        "--stop-reason", "queue-drained", "--failure-class", "none",
    )
    check("preserve-memory close exit 0", closed.returncode == 0, closed.stdout + closed.stderr)

    memory_text = (job_dir / "memory.md").read_text(encoding="utf-8")
    check("human content 'keep this line' still present", "keep this line" in memory_text, memory_text)
    check("ao:counters block present after close",
          "<!-- ao:counters -->" in memory_text and "<!-- /ao:counters -->" in memory_text,
          memory_text)


@register("run_ledger: two closes in the same second produce two distinct entries (no overwrite)")
def test_run_ledger_same_second_no_overwrite(root: Path) -> None:
    job_dir = make_job_dir(root, "ledger-same-second")

    first = run_ledger(
        "close", str(job_dir),
        "--outcome", "success", "--units", "1",
        "--stop-reason", "queue-drained", "--failure-class", "none",
    )
    second = run_ledger(
        "close", str(job_dir),
        "--outcome", "success", "--units", "1",
        "--stop-reason", "queue-drained", "--failure-class", "none",
    )
    check("first same-second close exit 0", first.returncode == 0, first.stdout + first.stderr)
    check("second same-second close exit 0", second.returncode == 0, second.stdout + second.stderr)

    runs_dir = job_dir / "runs"
    run_entries = sorted(runs_dir.glob("*.md")) if runs_dir.is_dir() else []
    check("two distinct run entries written (neither overwritten)",
          len(run_entries) == 2, str(run_entries))

    for entry in run_entries:
        parsed = STATE_SCHEMA.parse_run_entry(entry)
        check(f"run entry parses without raising: {entry.name}", parsed is not None, str(entry))


# --- fleet_health.py fixtures -------------------------------------------------
def _fh_write_toml(job_dir: Path, *, job_id: str, status: str = "ACTIVE",
                    rrule: str = "FREQ=DAILY;BYHOUR=3;BYMINUTE=0") -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "automation.toml").write_text(
        "version = 1\n"
        f'id = "{job_id}"\n'
        'kind = "cron"\n'
        f'name = "{job_id}"\n'
        'prompt = "do the thing"\n'
        f'status = "{status}"\n'
        f'rrule = "{rrule}"\n'
        'cwds = ["/tmp"]\n',
        encoding="utf-8",
    )


def _fh_write_last_run(job_dir: Path, *, when: datetime | None) -> None:
    record = STATE_SCHEMA.blank_record()
    record["when"] = when.isoformat() if when is not None else None
    record["outcome"] = "success"
    (job_dir / "last-run.md").write_text(
        STATE_SCHEMA.render_frontmatter(record), encoding="utf-8")


def _fh_write_lock(job_dir: Path, *, lease_until: datetime) -> None:
    lock_dir = job_dir / ".automation.lock"
    lock_dir.mkdir(parents=True, exist_ok=True)
    start = lease_until - timedelta(minutes=60)
    (lock_dir / "owner").write_text(
        "token: fh-test-token\n"
        "pid: 999999\n"
        "host: fh-test-host\n"
        "cwd: /tmp\n"
        f"start: {start.isoformat()}\n"
        f"lease_until: {lease_until.isoformat()}\n",
        encoding="utf-8",
    )


def build_fleet_fixture_home(root: Path, name: str = "fh-fixture") -> Path:
    """Build a `<home>/.codex/automations/<job>/` tree reproducing each of the
    five fleet_health flags on its own job, plus one clean active job."""
    home = root / name
    automations_root = home / ".codex" / "automations"
    now = datetime.now(timezone.utc)

    # (a) LOCK-EXPIRED: lease_until in the past.
    lock_expired = automations_root / "job-lock-expired"
    _fh_write_toml(lock_expired, job_id="job-lock-expired")
    _fh_write_last_run(lock_expired, when=now)
    (lock_expired / "runs").mkdir(parents=True, exist_ok=True)
    (lock_expired / "runs" / "20260701T000000Z.md").write_text("ok\n", encoding="utf-8")
    (lock_expired / "memory.md").write_text("small\n", encoding="utf-8")
    _fh_write_lock(lock_expired, lease_until=now - timedelta(hours=1))

    # (b) OVERDUE: ACTIVE, last-run `when` ~3 days ago (default 26h window).
    overdue = automations_root / "job-overdue"
    _fh_write_toml(overdue, job_id="job-overdue")
    _fh_write_last_run(overdue, when=now - timedelta(days=3))
    (overdue / "runs").mkdir(parents=True, exist_ok=True)
    (overdue / "runs" / "20260628T000000Z.md").write_text("ok\n", encoding="utf-8")
    (overdue / "memory.md").write_text("small\n", encoding="utf-8")

    # (c) EMPTY-RUNS: ACTIVE, runs/ present but empty.
    empty_runs = automations_root / "job-empty-runs"
    _fh_write_toml(empty_runs, job_id="job-empty-runs")
    _fh_write_last_run(empty_runs, when=now)
    (empty_runs / "runs").mkdir(parents=True, exist_ok=True)
    (empty_runs / "memory.md").write_text("small\n", encoding="utf-8")

    # (d) BLANK-LASTRUN: last-run.md missing entirely.
    blank_lastrun = automations_root / "job-blank-lastrun"
    _fh_write_toml(blank_lastrun, job_id="job-blank-lastrun")
    (blank_lastrun / "runs").mkdir(parents=True, exist_ok=True)
    (blank_lastrun / "runs" / "20260701T000000Z.md").write_text("ok\n", encoding="utf-8")
    (blank_lastrun / "memory.md").write_text("small\n", encoding="utf-8")

    # (e) HUGE-MEMORY: memory.md > 32 KiB.
    huge_memory = automations_root / "job-huge-memory"
    _fh_write_toml(huge_memory, job_id="job-huge-memory")
    _fh_write_last_run(huge_memory, when=now)
    (huge_memory / "runs").mkdir(parents=True, exist_ok=True)
    (huge_memory / "runs" / "20260701T000000Z.md").write_text("ok\n", encoding="utf-8")
    (huge_memory / "memory.md").write_text("x" * (33 * 1024), encoding="utf-8")

    # job-clean: ACTIVE, recent `when`, non-empty runs/, small memory, no lock.
    clean = automations_root / "job-clean"
    _fh_write_toml(clean, job_id="job-clean")
    _fh_write_last_run(clean, when=now)
    (clean / "runs").mkdir(parents=True, exist_ok=True)
    (clean / "runs" / "20260701T000000Z.md").write_text("ok\n", encoding="utf-8")
    (clean / "memory.md").write_text("small\n", encoding="utf-8")

    return home


def build_fleet_clean_only_home(root: Path, name: str = "fh-clean-only") -> Path:
    """A home containing ONLY the clean active job, for the exit-0 case."""
    home = root / name
    automations_root = home / ".codex" / "automations"
    now = datetime.now(timezone.utc)

    clean = automations_root / "job-clean"
    _fh_write_toml(clean, job_id="job-clean")
    _fh_write_last_run(clean, when=now)
    (clean / "runs").mkdir(parents=True, exist_ok=True)
    (clean / "runs" / "20260701T000000Z.md").write_text("ok\n", encoding="utf-8")
    (clean / "memory.md").write_text("small\n", encoding="utf-8")

    return home


def run_fleet_health_json(home: Path) -> tuple[int, dict]:
    """Invoke fleet_health.main(["--home", <home>, "--json"]) with stdout
    captured, returning (exit_code, parsed_json)."""
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = FLEET_HEALTH.main(["--home", str(home), "--json"])
    return rc, json.loads(buf.getvalue())


@register("fleet_health: each of the five conditions is flagged on the right job; "
          "clean job is not flagged; overall exit is nonzero")
def test_fleet_health_all_flags(root: Path) -> None:
    home = build_fleet_fixture_home(root)
    rc, data = run_fleet_health_json(home)

    flags_by_id = {job["id"]: set(job["flags"]) for job in data["jobs"]}

    check("exit code is 1 (fleet not clean)", rc == 1, str(data))
    check("job-lock-expired flagged LOCK-EXPIRED",
          "LOCK-EXPIRED" in flags_by_id.get("job-lock-expired", set()), str(flags_by_id))
    check("job-overdue flagged OVERDUE",
          "OVERDUE" in flags_by_id.get("job-overdue", set()), str(flags_by_id))
    check("job-empty-runs flagged EMPTY-RUNS",
          "EMPTY-RUNS" in flags_by_id.get("job-empty-runs", set()), str(flags_by_id))
    check("job-blank-lastrun flagged BLANK-LASTRUN",
          "BLANK-LASTRUN" in flags_by_id.get("job-blank-lastrun", set()), str(flags_by_id))
    check("job-huge-memory flagged HUGE-MEMORY",
          "HUGE-MEMORY" in flags_by_id.get("job-huge-memory", set()), str(flags_by_id))
    check("job-clean is NOT present in flagged jobs",
          "job-clean" not in flags_by_id, str(flags_by_id))
    check("summary.jobs_flagged == 5", data["summary"].get("jobs_flagged") == 5, str(data["summary"]))
    check("summary.healthy is False", data["summary"].get("healthy") is False, str(data["summary"]))


@register("fleet_health: clean-only fixture home returns exit 0")
def test_fleet_health_clean_exit_zero(root: Path) -> None:
    home = build_fleet_clean_only_home(root)
    rc, data = run_fleet_health_json(home)

    check("exit code is 0 (fleet clean)", rc == 0, str(data))
    check("no jobs flagged", data["summary"].get("jobs_flagged") == 0, str(data))
    check("summary.healthy is True", data["summary"].get("healthy") is True, str(data["summary"]))


# --- fleet_report.py fixtures --------------------------------------------------
FR_NOW = "2026-07-01T00:00:00+00:00"
_FR_NOW_DT = datetime.fromisoformat(FR_NOW)


def _fr_run_entry(runs_dir: Path, *, when: datetime, outcome: str = "success",
                   units_completed: int = 1, failure_class: str = "none",
                   runtime_s: int | None = None, merged_shas: list[str] | None = None,
                   suffix: str = "") -> None:
    """Write one `runs/<ts>.md` fixture via state_schema.render_frontmatter,
    with a `when` positioned relative to FR_NOW. `suffix` disambiguates
    same-timestamp entries so no two fixtures collide on disk."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    record = STATE_SCHEMA.blank_record()
    record["when"] = when.isoformat()
    record["outcome"] = outcome
    record["units_completed"] = units_completed
    record["failure_class"] = failure_class
    if runtime_s is not None:
        record["runtime_s"] = runtime_s
    if merged_shas:
        record["merged_shas"] = merged_shas
    fname = when.strftime("%Y%m%dT%H%M%SZ") + suffix + ".md"
    (runs_dir / fname).write_text(STATE_SCHEMA.render_frontmatter(record), encoding="utf-8")


def run_fleet_report_json(home: Path, now: str = FR_NOW) -> tuple[int, dict]:
    """Invoke fleet_report.main(["--home", <home>, "--json", "--now", now])
    with stdout captured, returning (exit_code, parsed_json)."""
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = FLEET_REPORT.main(["--home", str(home), "--json", "--now", now])
    return rc, json.loads(buf.getvalue())


@register("fleet_report: exact rollups for a crafted job (3 runs in last 7d, "
          "units 2/3/0, merges counted from merged_shas)")
def test_fleet_report_exact_rollups(root: Path) -> None:
    home = root / "fr-rollups"
    job = home / ".codex" / "automations" / "job-rollup"
    _fh_write_toml(job, job_id="job-rollup")
    runs = job / "runs"

    _fr_run_entry(runs, when=_FR_NOW_DT - timedelta(days=1), units_completed=2,
                  merged_shas=["a1"], suffix="-a")
    _fr_run_entry(runs, when=_FR_NOW_DT - timedelta(days=2), units_completed=3,
                  merged_shas=["b2", "c3"], suffix="-b")
    _fr_run_entry(runs, when=_FR_NOW_DT - timedelta(days=3), units_completed=0,
                  suffix="-c")

    rc, data = run_fleet_report_json(home)
    check("fleet_report exit 0", rc == 0, str(data))
    jobs_by_id = {j["id"]: j for j in data["jobs"]}
    job_data = jobs_by_id.get("job-rollup")
    check("job-rollup present", job_data is not None, str(jobs_by_id))
    check("runs_7d == 3", job_data is not None and job_data["runs_7d"] == 3, str(job_data))
    check("units_30d == 5 (2+3+0)", job_data is not None and job_data["units_30d"] == 5, str(job_data))
    check("merges_30d == 3 (a1,b2,c3)", job_data is not None and job_data["merges_30d"] == 3, str(job_data))


@register("fleet_report: zero-units-3x fires when the 3 most recent runs are all "
          "units_completed==0, and does not fire when one is >0")
def test_fleet_report_zero_units_3x(root: Path) -> None:
    home = root / "fr-zero-units"

    fires_job = home / ".codex" / "automations" / "job-zero-fires"
    _fh_write_toml(fires_job, job_id="job-zero-fires")
    runs_fires = fires_job / "runs"
    _fr_run_entry(runs_fires, when=_FR_NOW_DT - timedelta(days=1), units_completed=0, suffix="-a")
    _fr_run_entry(runs_fires, when=_FR_NOW_DT - timedelta(days=2), units_completed=0, suffix="-b")
    _fr_run_entry(runs_fires, when=_FR_NOW_DT - timedelta(days=3), units_completed=0, suffix="-c")

    no_fire_job = home / ".codex" / "automations" / "job-zero-no-fire"
    _fh_write_toml(no_fire_job, job_id="job-zero-no-fire")
    runs_no_fire = no_fire_job / "runs"
    _fr_run_entry(runs_no_fire, when=_FR_NOW_DT - timedelta(days=1), units_completed=0, suffix="-a")
    _fr_run_entry(runs_no_fire, when=_FR_NOW_DT - timedelta(days=2), units_completed=1, suffix="-b")
    _fr_run_entry(runs_no_fire, when=_FR_NOW_DT - timedelta(days=3), units_completed=0, suffix="-c")

    rc, data = run_fleet_report_json(home)
    jobs_by_id = {j["id"]: j for j in data["jobs"]}

    check("job-zero-fires has zero-units-3x flag",
          "zero-units-3x" in jobs_by_id.get("job-zero-fires", {}).get("flags", []),
          str(jobs_by_id))
    check("job-zero-no-fire does NOT have zero-units-3x flag",
          "zero-units-3x" not in jobs_by_id.get("job-zero-no-fire", {}).get("flags", []),
          str(jobs_by_id))


@register("fleet_report: blocked_ratio_7d computed correctly for a known mix "
          "(2 blocked of 4 -> 0.5)")
def test_fleet_report_blocked_ratio(root: Path) -> None:
    home = root / "fr-blocked-ratio"
    job = home / ".codex" / "automations" / "job-blocked-mix"
    _fh_write_toml(job, job_id="job-blocked-mix")
    runs = job / "runs"

    _fr_run_entry(runs, when=_FR_NOW_DT - timedelta(days=1), outcome="success",
                  failure_class="none", suffix="-a")
    _fr_run_entry(runs, when=_FR_NOW_DT - timedelta(days=2), outcome="success",
                  failure_class="none", suffix="-b")
    _fr_run_entry(runs, when=_FR_NOW_DT - timedelta(days=3), outcome="blocked",
                  failure_class="blocked-by-dirty-worktree", suffix="-c")
    _fr_run_entry(runs, when=_FR_NOW_DT - timedelta(days=4), outcome="failed",
                  failure_class="timeout", suffix="-d")

    rc, data = run_fleet_report_json(home)
    jobs_by_id = {j["id"]: j for j in data["jobs"]}
    job_data = jobs_by_id.get("job-blocked-mix")
    check("job-blocked-mix present", job_data is not None, str(jobs_by_id))
    check("blocked_ratio_7d == 0.5 (2 blocked of 4)",
          job_data is not None and job_data["blocked_ratio_7d"] == 0.5, str(job_data))


@register("fleet_report: a clean job (all success, non-zero units) has an empty flags list")
def test_fleet_report_clean_job_no_flags(root: Path) -> None:
    home = root / "fr-clean"
    job = home / ".codex" / "automations" / "job-clean"
    _fh_write_toml(job, job_id="job-clean")
    runs = job / "runs"

    _fr_run_entry(runs, when=_FR_NOW_DT - timedelta(days=1), outcome="success",
                  units_completed=2, failure_class="none", runtime_s=100, suffix="-a")
    _fr_run_entry(runs, when=_FR_NOW_DT - timedelta(days=2), outcome="success",
                  units_completed=3, failure_class="none", runtime_s=110, suffix="-b")
    _fr_run_entry(runs, when=_FR_NOW_DT - timedelta(days=8), outcome="success",
                  units_completed=1, failure_class="none", runtime_s=100, suffix="-c")

    rc, data = run_fleet_report_json(home)
    check("fleet_report exit 0", rc == 0, str(data))
    jobs_by_id = {j["id"]: j for j in data["jobs"]}
    job_data = jobs_by_id.get("job-clean")
    check("job-clean present", job_data is not None, str(jobs_by_id))
    check("job-clean has empty flags list",
          job_data is not None and job_data["flags"] == [], str(job_data))


# --- v0.7.2 Fix 1: parse_owner tolerates legacy owner formats ----------------
def _write_owner(lock_dir: Path, body: str) -> None:
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "owner").write_text(body, encoding="utf-8")


@register("parse_owner reads a legacy run_token=/started_at= owner; ISO timestamps intact")
def test_parse_owner_legacy_run_token(root: Path) -> None:
    lock_dir = make_job_dir(root, "legacy-runtoken") / ".automation.lock"
    _write_owner(
        lock_dir,
        "run_token=skincrafter-repo-hygiene-20260701T080124Z-55320\n"
        "pid=56006\n"
        "host=shanes.macbook.pro.lan\n"
        "cwd=/Users/shanehamilton/Documents/SkinCrafter\n"
        "started_at=2026-07-01T08:01:35Z\n"
        "lease_until=2026-07-01T10:01:35Z\n",
    )
    owner = RUN_LOCK_MOD.parse_owner(lock_dir)
    check("legacy owner parses (not None)", owner is not None, str(owner))
    check("run_token normalized to token",
          owner.get("token") == "skincrafter-repo-hygiene-20260701T080124Z-55320", str(owner))
    check("pid parsed from = format", owner.get("pid") == "56006", str(owner))
    check("started_at normalized to start, ISO intact",
          owner.get("start") == "2026-07-01T08:01:35Z", str(owner))
    check("lease_until not split on inner colons",
          owner.get("lease_until") == "2026-07-01T10:01:35Z", str(owner))


@register("parse_owner reads a legacy token=/start= owner (equals separator)")
def test_parse_owner_legacy_equals(root: Path) -> None:
    lock_dir = make_job_dir(root, "legacy-equals") / ".automation.lock"
    _write_owner(
        lock_dir,
        "token=abc-123\n"
        "pid=4242\n"
        "host=h\n"
        "cwd=/tmp/x\n"
        "start=2026-07-01T08:00:00Z\n"
        "lease_until=2026-07-01T09:30:00Z\n",
    )
    owner = RUN_LOCK_MOD.parse_owner(lock_dir)
    check("equals-format owner parses", owner is not None, str(owner))
    check("token from token=", owner.get("token") == "abc-123", str(owner))
    check("pid from pid=", owner.get("pid") == "4242", str(owner))
    check("lease_until ISO intact", owner.get("lease_until") == "2026-07-01T09:30:00Z", str(owner))


@register("legacy-format lock with dead pid + past lease is reclaimable")
def test_reclaim_legacy_abandoned(root: Path) -> None:
    job_dir = make_job_dir(root, "legacy-abandoned")
    lock_dir = job_dir / ".automation.lock"
    now = datetime.now(timezone.utc)
    _write_owner(
        lock_dir,
        "run_token=legacy-dead-token\n"
        "pid=999999\n"
        "host=h\n"
        "cwd=/tmp\n"
        f"started_at={(now - timedelta(minutes=70)).isoformat()}\n"
        f"lease_until={(now - timedelta(minutes=10)).isoformat()}\n",
    )
    result = run_lock("reclaim", str(job_dir))
    check("legacy reclaim exit 0", result.returncode == 0, result.stdout + result.stderr)
    check("legacy reclaim RECLAIMED", result.stdout.startswith("RECLAIMED"), result.stdout)
    check("old_token recovered from run_token=",
          "old_token=legacy-dead-token" in result.stdout, result.stdout)


@register("legacy-format lock defers reclaim while pid is live OR lease is in the future")
def test_reclaim_legacy_not_abandoned(root: Path) -> None:
    now = datetime.now(timezone.utc)

    # (i) live pid (this process) + past lease -> still live -> DEFER.
    live_job = make_job_dir(root, "legacy-live-pid")
    live_lock = live_job / ".automation.lock"
    _write_owner(
        live_lock,
        "run_token=legacy-live-token\n"
        f"pid={os.getpid()}\n"
        f"started_at={(now - timedelta(minutes=70)).isoformat()}\n"
        f"lease_until={(now - timedelta(minutes=5)).isoformat()}\n",
    )
    r_live = run_lock("reclaim", str(live_job))
    check("legacy live-pid reclaim defers (exit 2)", r_live.returncode == 2, r_live.stdout)
    check("legacy live-pid lock left in place", (live_lock / "owner").is_file())

    # (ii) dead pid + future lease -> lease not expired -> DEFER.
    future_job = make_job_dir(root, "legacy-future-lease")
    future_lock = future_job / ".automation.lock"
    _write_owner(
        future_lock,
        "run_token=legacy-future-token\n"
        "pid=999999\n"
        f"started_at={(now - timedelta(minutes=1)).isoformat()}\n"
        f"lease_until={(now + timedelta(minutes=60)).isoformat()}\n",
    )
    r_future = run_lock("reclaim", str(future_job))
    check("legacy future-lease reclaim defers (exit 2)", r_future.returncode == 2, r_future.stdout)
    check("legacy future-lease lock left in place", (future_lock / "owner").is_file())


@register("v5 key: value owners still parse unchanged (no regression)")
def test_parse_owner_v5_unchanged(root: Path) -> None:
    lock_dir = make_job_dir(root, "v5-owner") / ".automation.lock"
    lease = datetime.now(timezone.utc) + timedelta(minutes=60)
    craft_owner_file(lock_dir, pid=4242, lease_until=lease, token="v5-token")
    owner = RUN_LOCK_MOD.parse_owner(lock_dir)
    check("v5 owner parses", owner is not None, str(owner))
    check("v5 token unchanged", owner.get("token") == "v5-token", str(owner))
    check("v5 pid unchanged", owner.get("pid") == "4242", str(owner))
    check("v5 lease_until ISO intact",
          owner.get("lease_until") == lease.isoformat(), str(owner))
    check("v5 cwd preserved", owner.get("cwd") == "/tmp/test", str(owner))


# --- v0.7.2 Fix 2: reserved non-job dirs are not counted as jobs -------------
@register("fleet_health & fleet_report ignore reserved suites/.archive/.disabled dirs; "
          "only real job dirs are counted")
def test_reserved_dirs_not_counted_as_jobs(root: Path) -> None:
    home = root / "reserved-dirs-home"
    autos = home / ".codex" / "automations"
    now = datetime.now(timezone.utc)

    # Two real, clean jobs (each has automation.toml — the job_file).
    for jid in ("real-job-one", "real-job-two"):
        job = autos / jid
        _fh_write_toml(job, job_id=jid)
        _fh_write_last_run(job, when=now)
        (job / "runs").mkdir(parents=True, exist_ok=True)
        _fr_run_entry(job / "runs", when=_FR_NOW_DT - timedelta(days=1),
                      units_completed=1, suffix="-x")
        (job / "memory.md").write_text("small\n", encoding="utf-8")

    # Reserved non-job dirs that must NOT be counted as jobs.
    (autos / "suites").mkdir(parents=True, exist_ok=True)
    (autos / "suites" / "some-project.toml").write_text(
        '[suite]\nproject = "x"\n', encoding="utf-8")
    (autos / ".archive" / "old-job").mkdir(parents=True, exist_ok=True)
    (autos / ".archive" / "old-job" / "automation.toml").write_text(
        "version = 1\n", encoding="utf-8")
    (autos / ".disabled" / "off-job").mkdir(parents=True, exist_ok=True)
    (autos / ".disabled" / "off-job" / "automation.toml").write_text(
        "version = 1\n", encoding="utf-8")
    (autos / ".workspace-locks").mkdir(parents=True, exist_ok=True)

    # fleet_health: reserved dirs never surface as (flagged) jobs. Real jobs
    # are clean, so a healthy fleet here proves the reserved dirs produced no
    # phantom flags (pre-fix, suites/ flagged BLANK-LASTRUN + EMPTY-RUNS).
    rc_h, health = run_fleet_health_json(home)
    health_ids = {j["id"] for j in health["jobs"]}
    check("fleet_health is clean (no phantom flags from reserved dirs)",
          rc_h == 0 and health["summary"]["jobs_flagged"] == 0, str(health))
    for reserved in ("suites", "old-job", "off-job", ".archive", ".disabled",
                     ".workspace-locks"):
        check(f"fleet_health did not flag reserved '{reserved}'",
              reserved not in health_ids, str(health_ids))

    # fleet_report: exactly the two real jobs are reported, nothing else.
    _, report = run_fleet_report_json(home)
    report_ids = {j["id"] for j in report["jobs"]}
    check("fleet_report counts exactly the two real jobs",
          report_ids == {"real-job-one", "real-job-two"}, str(report_ids))
    check("fleet_report jobs_total == 2 (no phantom job rows)",
          report["summary"]["jobs_total"] == 2, str(report["summary"]))


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for name, fn in TESTS:
            print(f"-- {name} --")
            fn(root)

    total = PASSED + FAILED
    print(f"{PASSED} passed, {FAILED} failed (of {total})")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
