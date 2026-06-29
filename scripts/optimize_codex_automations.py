#!/usr/bin/env python3
"""
optimize_codex_automations.py — inject / upgrade the Automation Optimizer
managed block in Codex automation.toml files, idempotently and safely.

Modes:
  (no args)   Dry-run audit: print a status table and a unified diff of what
              WOULD change. Changes nothing. Exit 0.
  --apply     Apply changes: back up each modified file as <name>.bak.<ts>,
              inject/upgrade the managed block, and scaffold sidecar state files.
  --strict    Validate: every active automation must carry the CURRENT block
              (all anchors present) and required sidecar files. Exit 1 on any
              failure. Changes nothing.

Options:
  --codex-home PATH   Override $CODEX_HOME / ~/.codex.
  --prompt-key KEY    TOML key that holds the prompt (default: "prompt").

Design notes:
  * The block is bounded by versioned sentinel markers, so detection and
    upgrade are exact, not fuzzy.
  * Re-parses each file after writing and restores from backup if the prompt
    does not round-trip — writes are safe by construction.
  * Reads TOML with the stdlib `tomllib` (Python 3.11+). Writes by a bounded
    text replacement of only the prompt field, preserving the rest of the file.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import hashlib
import json
import os
import re
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # backport for Python 3.8–3.10: `pip install tomli`
    except ModuleNotFoundError:
        sys.exit(
            "This helper needs a TOML reader: Python 3.11+ (stdlib 'tomllib') "
            "or `pip install tomli` on older Pythons."
        )

# --- Managed protocol block (real Codex format) ------------------------------
# Matches the live convention: "## Automation Optimizer Protocol" ... "Protocol
# version: N" ... "## End Automation Optimizer Protocol", with sidecars at the
# JOB ROOT (not a state/ subfolder). State-file paths are absolute, per job.
PROTOCOL_VERSION = 4
BEGIN_MARKER = "## Automation Optimizer Protocol"
END_MARKER = "## End Automation Optimizer Protocol"
PROTOCOL_VERSION_RE = re.compile(r"Protocol version:\s*(\d+)")
# Default number of bounded work-units a single run completes when the task body
# states no cap of its own. The continuation loop honors a pattern's own
# loop/changeset/edit cap first, and falls back to this otherwise.
DEFAULT_RUN_UNIT_BUDGET = 5
# Stable section headers used by --strict as an integrity check (prose may vary).
REQUIRED_SECTIONS = [
    "Start-of-run protocol", "Agentic execution protocol",
    "Duplicate-avoidance protocol", "Failure taxonomy",
    "Continuation loop", "Push, sync, and merge bias", "Evidence and closeout",
]
# Sidecars live at the JOB ROOT directory.
LOCK_FILE = ".automation.lock"
SIDECAR_DIRS = ["runs"]


def managed_block(job_dir: str) -> str:
    """The canonical protocol block, with this job's absolute state-file paths."""
    d = job_dir.rstrip("/")
    return f"""## Automation Optimizer Protocol

Protocol version: {PROTOCOL_VERSION}

This is a recurring automation. Use this protocol before the task-specific instructions so repeated runs learn, avoid duplicate work, and stop safely.

State files for this automation:
- Memory: `{d}/memory.md`
- Last run summary: `{d}/last-run.md`
- Dated run ledgers: `{d}/runs/`
- Priority queue: `{d}/priority-queue.md`
- Human approval queue: `{d}/human-approval.md`
- Baseline failure registry: `{d}/baseline-failures.md`
- Concurrency lock: `{d}/{LOCK_FILE}`

Start-of-run protocol:
1. Acquire the concurrency lock ATOMICALLY before touching repos or trackers, then hold it for the whole session (every continuation-loop unit). The acquire MUST fail when a lock already exists — never read-then-write, which races: two runs both judge the other "stale" and both proceed in parallel on the same repos.
   - Acquire by creating the lock as a directory: `mkdir {d}/{LOCK_FILE}` (atomic; fails if it already exists). Equivalent atomic alternative: write the lock file under `set -o noclobber`. On success, record owner info inside it (e.g. `{d}/{LOCK_FILE}/owner`): a unique run token, PID, host, cwd, ISO start time, and `lease_until` = start + your maximum wall-clock budget.
   - If the acquire FAILS, the lock is held — DEFER, do not run in parallel. Read the owner. If its recorded PID is still alive OR now is before `lease_until`, write a `deferred` ledger entry (`deferred: lock held by <token> until <lease_until>`) and STOP this run. Another instance owns the night; racing it corrupts shared repos.
   - Reclaim ONLY a provably abandoned lock — its recorded PID is not alive AND now is past `lease_until` plus a grace margin. Reclaim atomically: note the dead token, remove the lock, re-acquire with the same atomic op, then READ IT BACK and confirm it now holds YOUR token. If any other token appears, another run beat you to it — defer and STOP. Record why the prior lock was abandoned.
2. Read memory, the last run summary, baseline failures, priority queue, and human approval queue. Treat these as hints, not proof.
3. Re-check live source of truth: repo status, tracker state, relevant tools, scheduled task config, and deployment state when applicable.
4. Run a tool/environment preflight for required commands and services. If a prerequisite is missing, record it once, choose another safe target if possible, or stop with a precise blocker.

Agentic execution protocol:
- For non-trivial work, do not run as a single monolithic agent when the active tool supports subagents, project agents, or equivalent parallel review lanes. Split the work into bounded roles such as inventory/triage, implementation, verification/review, and integration/merge.
- Keep agent scopes non-overlapping. Give each agent exact files, commands, or routes. Require evidence, not self-certification.
- Use direct single-agent execution only for small config-only updates, read-only audits, or cases where subagent tooling is unavailable. Record that limitation in the run ledger when it applies.
- Always include a final integrator pass that reconciles findings, reruns required checks, updates memory/tracker state, and decides whether the work is safe to push and merge.

Duplicate-avoidance protocol:
- Build a stable fingerprint for every finding: route/slice, failure class, normalized error or symptom, likely file/symbol, tracker id, and verification command. Exclude timestamps, machine-specific ids, screenshot paths, and drifting line numbers.
- If the same fingerprint is already open, update the existing tracker item and move to another safe target.
- If the same fingerprint was previously fixed, replay its saved confirmation path first. If it passes, classify as duplicate or false alarm. If it fails, classify as a regression and update or reopen the prior tracker item before fixing.
- If the fingerprint is environment-only or false-positive, verify that once, record the skip, and continue.
- If it is genuinely new, create or update one focused tracker item and proceed under the normal task rules.

Failure taxonomy:
- Use consistent labels: `passed`, `fixed`, `known-open`, `regression`, `duplicate`, `false-positive`, `environment-only`, `blocked-by-dirty-worktree`, `blocked-by-missing-tool`, `blocked-by-test-baseline`, `blocked-by-approval`, `unsafe-to-merge`, `needs-human-decision`.

Scope and target selection:
- Prefer high-value targets that are least recently covered, adjacent to recent fixes, or newly unblocked.
- Respect any task-specific loop, time, file-count, merge, and deploy limits. Each unit of work stays bounded. If the task states no per-run cap, complete up to {DEFAULT_RUN_UNIT_BUDGET} bounded units this run (see the continuation loop below).
- Do not spend the whole run on a known blocker unless its status changed.

Continuation loop:
- You are running overnight and usually finish one unit with time to spare. Do not stop after a single unit. After a unit closes out safely, loop back and start the next one so the run delivers as much verified value as the budget allows.
- Each loop: re-read live state, fingerprint-dedupe against the work you just completed this run (so your own commits/tickets are never mistaken for new work), then pick the next highest-value unblocked target from the priority queue.
- Keep looping until ANY stop condition is hit, then go to closeout: (a) the per-run unit budget is reached — the task's own loop/changeset/edit cap, or {DEFAULT_RUN_UNIT_BUDGET} if none is stated; (b) no new high-value unblocked target remains (priority queue drained, nothing else changed); (c) two consecutive units this run fail or are blocked; (d) the next unit would cross an approval or otherwise unsafe boundary — queue it and stop that line of work; (e) you detect you are repeating or ping-ponging your own output.
- Every unit independently obeys this whole protocol (duplicate-avoidance, failure taxonomy, merge bias). Hold the SAME concurrency lock for the whole session across all units — do not release and re-acquire it between units. Write one ledger entry per unit.
- This is a between-unit loop, not the start-of-run change-detection gate: if NOTHING watched changed since the last run, that gate still ends the run as a no-op. The continuation loop only applies once there is genuine high-value work to do.

Push, sync, and merge bias:
- Default posture: verified completed work should be synced, pushed, and merged to the project default branch instead of left local. Bias toward LANDING safe work, not stranding it behind a human gate it does not need.
- SAFE-MERGE LANE (auto-merge, no human approval) — the integrator (sole merge authority) may push and merge to the default branch when ALL of these hold: (a) every required gate/CI check passes on the branch; (b) every changed path is inside the producing job's declared `write_scope`; (c) NO changed path touches a production-config, secret/credential, database migration, deploy/release, CI/workflow, auth, billing, or other externally-facing surface; (d) the worktree is clean except the intended diff (agent-local tool metadata does NOT count as dirty — see below); (e) ownership is clear and there is no concurrency-lock conflict. Fetch/prune first; record the merged sha and a rollback note in the ledger.
- Anything OUTSIDE that lane does NOT auto-merge: push a feature branch when allowed, add a STRUCTURED item to the human approval queue (see closeout), and report the exact approval needed. This covers any change touching the surfaces in (c), a failing or again-uncertain gate, ambiguous ownership, history rewrite, force-push, deploy, or a cross-repo/coordinated change.
- Agent-local tool metadata is NOT a merge blocker: files like `.serena/`, `.beads/issues.jsonl`, local editor/scratch state, and similar local-only artifacts must be ignored by the clean-worktree check. If they are not yet git-ignored, adding that ignore rule is itself a safe instruction-level change (route it through the integrator / the P8 reflector) — never a reason to block the night's real work.
- Evidence is PROPORTIONAL to the change: require screenshot/visual proof ONLY for user-visible UI changes. For logic, test, refactor, docs, or config changes, green automated checks are sufficient evidence — do not block a merge solely for a missing screenshot.
- Prefer the project's documented merge path; use existing PRs when appropriate, otherwise a non-history-rewriting local merge/squash for automation-owned work when policy permits.
- If checks fail because of a known baseline failure, do not hide it. Update the baseline registry and merge only when project policy explicitly permits that exception.

Evidence and closeout:
- Keep compact evidence only: commands, pass/fail summaries, tracker ids, commit hashes, screenshot paths when UI proof matters, and the exact next action.
- Update memory, baseline failures, priority queue, human approval queue, `last-run.md`, and a dated run ledger before final reporting.
- When you queue a human decision, write it to `human-approval.md` as a STRUCTURED item the cross-project digest can read: a `## <one-line ask>` heading, then `- risk:` low|medium|high, `- suggested_default:` (what you would do absent other input), `- action:` (the exact command / branch / ticket id to act on), `- first_seen:` (ISO date), and `- evidence:` (ids/paths, never secrets). Keep these fields current; remove an item once it is resolved.
- Release the concurrency lock at the end ONLY if it still holds YOUR run token — never delete a lock you no longer own. If you crashed mid-run, the lease lets the next run reclaim it safely.
- Final report must state what was checked, what was skipped as known, what was fixed, what was pushed or merged, what remains blocked, and the next best target.

## End Automation Optimizer Protocol"""


# --- Sidecar templates (created at the job root) -----------------------------
SIDECARS = {
    "memory.md": (
        "# Memory\nTreat as a hint, never as proof. Re-check live state every run.\n\n"
        "## Watched fingerprints\n- repo_head:\n- open_tracker_items:\n- inputs_hash:\n"
        "- last_success:\n\n## Stable decisions\n\n## Consecutive failures\n- count: 0\n"
    ),
    "last-run.md": (
        "# Last run\n- when:\n- outcome:\n- runtime_s:\n- items_touched:\n"
        "- units_completed: 0\n- stop_reason:\n- retries:\n"
        "- failure_class: none\n- rollback: n/a\n- notes:\n"
    ),
    "priority-queue.md": (
        "# Priority queue\nHighest-value unblocked targets, most important first.\n"
    ),
    "baseline-failures.md": (
        "# Baseline failures\nKnown/expected failures. Do not re-report or auto-fix.\n"
    ),
    "human-approval.md": (
        "# Human approval queue\nUnsafe actions awaiting a human. Nothing here is auto-executed.\n"
        "Format per item — a `## <one-line ask>` heading, then: "
        "`- risk:` low|medium|high  `- suggested_default:` ...  "
        "`- action:` exact command/branch/id  `- first_seen:` ISO date  "
        "`- evidence:` ids/paths (no secrets). The daily cross-project digest reads these.\n"
    ),
}
REQUIRED_SIDECARS = list(SIDECARS.keys())

INACTIVE_STATUSES = {"disabled", "archived", "paused", "inactive", "off"}

# --- Fleet / suite constants -------------------------------------------------
KNOWN_TEMPLATES = {"P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"}
PHASE_RANK = {"producer": 0, "integrator": 1, "janitor": 2, "reflector": 3}
FINGERPRINT_FIELDS = (
    "template", "template_version", "merge_authority",
    "write_scope", "phase", "schedule", "params",
)


# --- Core helpers ------------------------------------------------------------
def codex_home(override: str | None) -> Path:
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def find_automations(home: Path) -> list[Path]:
    base = home / "automations"
    if not base.is_dir():
        return []
    return sorted(p / "automation.toml" for p in base.iterdir()
                  if (p / "automation.toml").is_file())


def parse_prompt(raw: str, key: str) -> str | None:
    """Return the parsed prompt string, or None if the key is absent/non-string."""
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"TOML parse error: {e}") from e
    val = data.get(key)
    return val if isinstance(val, str) else None


_BLOCK_SPAN_RE = re.compile(
    re.escape(BEGIN_MARKER) + r".*?" + re.escape(END_MARKER),
    re.DOTALL,
)


def strip_existing_block(prompt: str) -> str:
    """Remove any managed protocol block and tidy surrounding blank lines."""
    return _BLOCK_SPAN_RE.sub("", prompt).strip("\n")


def build_new_prompt(prompt: str, job_dir: str) -> str:
    body = strip_existing_block(prompt).lstrip()
    block = managed_block(job_dir)
    return block + ("\n\n" + body if body else "\n")


def current_block_version(prompt: str) -> int | None:
    """Protocol version if the block is present, else None. Block present without a
    version line reads as 0 (needs upgrade)."""
    if BEGIN_MARKER not in prompt:
        return None
    m = PROTOCOL_VERSION_RE.search(prompt)
    return int(m.group(1)) if m else 0


def choose_quote(content: str) -> tuple[str, str] | None:
    """Pick a triple-quote style that can hold content verbatim/safely."""
    if "'''" not in content:
        return ("'''", content)            # literal: preserved verbatim
    if '"""' not in content:
        return ('"""', content.replace("\\", "\\\\"))  # basic: escape backslashes
    return None                            # cannot represent safely


def replace_prompt_in_raw(raw: str, key: str, new_content: str) -> str | None:
    """Bounded replacement of the prompt assignment; preserves the rest of the file."""
    quote = choose_quote(new_content)
    if quote is None:
        return None
    delim, payload = quote
    replacement = f"{key} = {delim}\n{payload}\n{delim}"
    # Match: key = """...""" | '''...''' | "..." | '...'
    assign = re.compile(
        rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=[ \t]*"
        r'(?:"""(?:\\.|[^\\]|"(?!""))*?"""'
        r"|'''.*?'''"
        r'|"(?:\\.|[^"\\\n])*"'
        r"|'[^'\n]*')",
        re.DOTALL,
    )
    if not assign.search(raw):
        return None
    return assign.sub(lambda _: replacement, raw, count=1)


def status_of(path: Path, key: str) -> tuple[str, str]:
    """Return (status_code, detail). status_code in: compliant, needs-sidecars,
    needs-upgrade, needs-block, no-prompt, error, inactive."""
    raw = path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        return ("error", f"TOML parse error: {e}")
    if str(data.get("status", "")).lower() in INACTIVE_STATUSES:
        return ("inactive", f"status={data.get('status')}")
    prompt = data.get(key)
    if not isinstance(prompt, str):
        return ("no-prompt", f"no string key '{key}'")
    ver = current_block_version(prompt)
    if ver is None:
        return ("needs-block", "no protocol block")
    if ver < PROTOCOL_VERSION:
        return ("needs-upgrade", f"protocol v{ver} < v{PROTOCOL_VERSION}")
    if ver > PROTOCOL_VERSION:
        return ("needs-upgrade", f"protocol v{ver} newer than helper v{PROTOCOL_VERSION}")
    missing = [s for s in REQUIRED_SIDECARS if not (path.parent / s).is_file()]
    if missing:
        return ("needs-sidecars", f"missing {', '.join(missing)}")
    return ("compliant", f"protocol v{ver}, sidecars ok")


def scaffold_sidecars(auto_dir: Path, created: list[str]) -> None:
    for d in SIDECAR_DIRS:
        (auto_dir / d).mkdir(parents=True, exist_ok=True)
    for rel, content in SIDECARS.items():
        f = auto_dir / rel
        if not f.exists():
            f.write_text(content, encoding="utf-8")
            created.append(str(f))


def timestamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%dT%H%M%S")


# --- Modes -------------------------------------------------------------------
def audit(paths: list[Path], key: str) -> int:
    print(f"Automation Optimizer audit — protocol v{PROTOCOL_VERSION}\n")
    if not paths:
        print("No automation.toml files found.")
        return 0
    for p in paths:
        code, detail = status_of(p, key)
        print(f"[{code:14}] {p}  ({detail})")
        if code in ("needs-block", "needs-upgrade"):
            raw = p.read_text(encoding="utf-8")
            new_prompt = build_new_prompt(parse_prompt(raw, key) or "", str(p.parent))
            new_raw = replace_prompt_in_raw(raw, key, new_prompt)
            if new_raw and new_raw != raw:
                diff = difflib.unified_diff(
                    raw.splitlines(), new_raw.splitlines(),
                    fromfile=str(p), tofile=str(p) + " (proposed)", lineterm="",
                )
                print("\n".join(f"    {ln}" for ln in diff) + "\n")
    print("\nDry run only. Re-run with --apply to write changes.")
    return 0


def apply(paths: list[Path], key: str) -> int:
    updated, compliant, sidecared, skipped, errors = [], [], [], [], []
    created_files: list[str] = []
    for p in paths:
        code, detail = status_of(p, key)
        if code in ("inactive", "no-prompt", "error"):
            skipped.append((p, f"{code}: {detail}"))
            continue
        if code in ("needs-block", "needs-upgrade"):
            raw = p.read_text(encoding="utf-8")
            new_prompt = build_new_prompt(parse_prompt(raw, key) or "", str(p.parent))
            new_raw = replace_prompt_in_raw(raw, key, new_prompt)
            if not new_raw or new_raw == raw:
                errors.append((p, "could not place block (unusual prompt quoting?)"))
                continue
            bak = p.with_suffix(p.suffix + f".bak.{timestamp()}")
            bak.write_text(raw, encoding="utf-8")
            p.write_text(new_raw, encoding="utf-8")
            # Verify round-trip; restore on any mismatch.
            check = parse_prompt(p.read_text(encoding="utf-8"), key)
            if check is None or check.strip() != new_prompt.strip():
                p.write_text(raw, encoding="utf-8")
                errors.append((p, f"round-trip failed; restored from {bak.name}"))
                continue
            updated.append(p)
        else:
            compliant.append(p)
        before = len(created_files)
        scaffold_sidecars(p.parent, created_files)
        if len(created_files) > before and p not in updated:
            sidecared.append(p)

    print(f"Automation Optimizer apply — protocol v{PROTOCOL_VERSION}\n")
    print(f"Updated ({len(updated)}):")
    for p in updated:
        print(f"  {p}")
    print(f"Already compliant ({len(compliant)}):")
    for p in compliant:
        print(f"  {p}")
    if sidecared:
        print(f"Sidecars added without prompt change ({len(sidecared)}):")
        for p in sidecared:
            print(f"  {p}")
    print(f"State files created ({len(created_files)}):")
    for f in created_files:
        print(f"  {f}")
    if skipped:
        print(f"Skipped ({len(skipped)}):")
        for p, why in skipped:
            print(f"  {p} — {why}")
    if errors:
        print(f"Errors ({len(errors)}):")
        for p, why in errors:
            print(f"  {p} — {why}")
    return 1 if errors else 0


def strict(paths: list[Path], key: str) -> int:
    failures: list[tuple[Path, str]] = []
    checked = 0
    for p in paths:
        raw = p.read_text(encoding="utf-8")
        try:
            data = tomllib.loads(raw)
        except tomllib.TOMLDecodeError as e:
            failures.append((p, f"TOML parse error: {e}"))
            continue
        if str(data.get("status", "")).lower() in INACTIVE_STATUSES:
            continue
        checked += 1
        prompt = data.get(key)
        if not isinstance(prompt, str):
            failures.append((p, f"no string key '{key}'"))
            continue
        ver = current_block_version(prompt)
        if ver != PROTOCOL_VERSION:
            failures.append((p, f"protocol version {ver} != required v{PROTOCOL_VERSION}"))
            continue
        missing_sections = [s for s in REQUIRED_SECTIONS if s not in prompt]
        if missing_sections:
            failures.append((p, f"missing protocol sections: {', '.join(missing_sections)}"))
        missing_files = [s for s in REQUIRED_SIDECARS if not (p.parent / s).is_file()]
        if missing_files:
            failures.append((p, f"missing sidecars: {', '.join(missing_files)}"))

    print(f"Strict validation — {checked} active automation(s) checked, "
          f"protocol v{PROTOCOL_VERSION}\n")
    if not failures:
        print("PASS — all active automations carry the current protocol and sidecars.")
        return 0
    print(f"FAIL ({len(failures)}):")
    for p, why in failures:
        print(f"  {p} — {why}")
    return 1


# --- Fleet validation --------------------------------------------------------
def find_suite(home: Path, override: str | None) -> Path | None:
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None
    candidate = home / "automations" / "suite.toml"
    return candidate if candidate.is_file() else None


def compute_fingerprint(job: dict) -> str:
    norm = {}
    for f in FINGERPRINT_FIELDS:
        v = job.get(f)
        if f == "write_scope" and isinstance(v, list):
            v = sorted(v)
        if f == "params" and isinstance(v, dict):
            v = {k: v[k] for k in sorted(v)}
        norm[f] = v
    blob = json.dumps(norm, sort_keys=True, separators=(",", ":"))
    return "ao1:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _cron_hour(schedule: str | None) -> int | None:
    if not isinstance(schedule, str):
        return None
    parts = schedule.split()
    if len(parts) != 5:
        return None
    try:
        return int(parts[1])  # standard 5-field cron: m h dom mon dow
    except ValueError:
        return None


def fleet(suite_path: Path | None, require_approved: bool) -> int:
    if suite_path is None:
        print("No suite.toml found. Pass --fleet PATH or create "
              "${CODEX_HOME}/automations/suite.toml.")
        return 1
    raw = suite_path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as e:
        print(f"FAIL — manifest TOML parse error: {e}")
        return 1

    jobs = data.get("job", [])
    project = data.get("suite", {}).get("project", "<unnamed>")
    print(f"Fleet validation — suite '{project}' ({len(jobs)} jobs), "
          f"protocol v{PROTOCOL_VERSION}\n")
    if not jobs:
        print("FAIL — manifest declares no [[job]] entries.")
        return 1

    errors: list[str] = []
    by_id = {}

    # Rule 7: unique ids
    for j in jobs:
        jid = j.get("id")
        if not jid:
            errors.append("a job is missing 'id'")
        elif jid in by_id:
            errors.append(f"duplicate job id: {jid}")
        else:
            by_id[jid] = j

    # Rule 6: known templates
    for j in jobs:
        if j.get("template") not in KNOWN_TEMPLATES:
            errors.append(f"{j.get('id')}: unknown template "
                          f"{j.get('template')!r} (expected P1..P9)")

    # Rule 1 + 2: at most one merge authority; exactly one when any producer or
    # janitor exists; the authority (if any) must be the integrator; and every
    # integrator must hold it. (A reflector-only suite may have zero.)
    authorities = [j for j in jobs if j.get("merge_authority") is True]
    phases = {j.get("phase") for j in jobs}
    integrators = [j for j in jobs if j.get("phase") == "integrator"]
    needs_authority = bool({"producer", "janitor"} & phases)

    for integ in integrators:
        if integ.get("merge_authority") is not True:
            errors.append(f"{integ.get('id')}: integrator must set merge_authority = true")
    for a in authorities:
        if a.get("phase") != "integrator":
            errors.append(f"{a.get('id')}: holds merge authority but phase is "
                          f"{a.get('phase')!r}, must be 'integrator'")
    if len(authorities) > 1:
        ids = ", ".join(j.get("id", "?") for j in authorities)
        errors.append(f"multiple merge authorities ({ids}): exactly one allowed")
    if needs_authority and len(authorities) == 0:
        errors.append("producers/janitors present but no merge authority — exactly "
                      "one integrator must set merge_authority = true")

    # Rule 3: producers hand off to an existing integrator
    for j in jobs:
        if j.get("phase") == "producer":
            if j.get("merge_authority") is True:
                errors.append(f"{j.get('id')}: producer must not hold merge authority")
            target = j.get("hands_off_to")
            if not target:
                errors.append(f"{j.get('id')}: producer missing 'hands_off_to'")
            elif target not in by_id:
                errors.append(f"{j.get('id')}: hands_off_to '{target}' is not a job id")
            elif by_id[target].get("phase") != "integrator":
                errors.append(f"{j.get('id')}: hands_off_to '{target}' is not an integrator")

    # Rule 4: consumers exist
    if "producer" in phases and not integrators:
        errors.append("producers present but no integrator to consume their work")
    if "janitor" in phases and not integrators:
        errors.append("janitor present but no integrator it can depend on")

    # Rule 5: phase ordering by cron hour
    ranked = [(PHASE_RANK.get(j.get("phase"), 99), _cron_hour(j.get("schedule")),
               j.get("id")) for j in jobs]
    timed = [(r, h, i) for (r, h, i) in ranked if h is not None]
    for a in range(len(timed)):
        for b in range(len(timed)):
            ra, ha, ia = timed[a]
            rb, hb, ib = timed[b]
            if ra < rb and ha > hb:
                errors.append(f"phase order: '{ia}' (earlier phase) is scheduled "
                              f"at h{ha} after '{ib}' at h{hb}")
    if len(timed) < len([j for j in jobs if j.get("schedule")]):
        print("note: some schedules were not standard 5-field cron; "
              "ordering partially checked.\n")

    # Approval / fingerprint reporting
    print("Approval status:")
    pending_or_stale = 0
    for j in jobs:
        jid = j.get("id", "?")
        expected = compute_fingerprint(j)
        approved = j.get("approved_fingerprint")
        if not approved:
            state = "pending (never confirmed)"
            pending_or_stale += 1
        elif approved == expected:
            state = "approved & current"
        else:
            state = f"approved but STALE (now {expected}, approved {approved})"
            pending_or_stale += 1
        auth = " [merge authority]" if j.get("merge_authority") else ""
        mode = j.get("mode", "active")
        print(f"  {jid:22} {j.get('phase','?'):11} mode={mode:7} {state}{auth}")
    print()

    if errors:
        print(f"FAIL ({len(errors)} structural error(s)):")
        for e in errors:
            print(f"  - {e}")
        return 1
    if require_approved and pending_or_stale:
        print(f"FAIL — --require-approved: {pending_or_stale} job(s) pending or stale.")
        return 1
    print("PASS — suite is internally consistent"
          + (" and all active jobs are approved & current." if require_approved
             else "; see approval status above."))
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Optimize Codex automation.toml files.")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--strict", action="store_true", help="validate per-job block + sidecars")
    ap.add_argument("--fleet", nargs="?", const=True, default=False,
                    metavar="SUITE_TOML",
                    help="validate a suite manifest (default: <codex-home>/automations/suite.toml)")
    ap.add_argument("--require-approved", action="store_true",
                    help="with --fleet: also fail if any active job is pending/stale")
    ap.add_argument("--codex-home", default=None, help="override $CODEX_HOME / ~/.codex")
    ap.add_argument("--prompt-key", default="prompt", help="TOML key holding the prompt")
    args = ap.parse_args(argv)

    home = codex_home(args.codex_home)

    if args.fleet is not False:
        override = args.fleet if isinstance(args.fleet, str) else None
        return fleet(find_suite(home, override), args.require_approved)

    paths = find_automations(home)
    if args.strict:
        return strict(paths, args.prompt_key)
    if args.apply:
        return apply(paths, args.prompt_key)
    return audit(paths, args.prompt_key)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
