#!/usr/bin/env python3
"""
scaffold_suite.py — turn an APPROVED suite.toml into runnable Codex automations.

This is the last mile of composer mode: it materializes each job in the manifest
as a real <codex-home>/automations/<id>/automation.toml whose prompt is the
shared managed block (imported from optimize_codex_automations.py, so it never
drifts) followed by that pattern's adaptive task body, and it creates the sidecar
state files. In a Codex environment, an automation.toml under the automations
directory IS the registration; its `schedule` field carries the cadence.

Safety:
  * Only jobs that are APPROVED & CURRENT (fingerprint matches) are scaffolded.
    Pending or stale jobs block the install until re-approved (override with
    --allow-unapproved, which you should not normally do).
  * Default is a DRY RUN that writes nothing. Writing active nightly automations
    into ~/.codex is a system change, so it requires the explicit --install flag.
  * Every generated file is re-parsed; the prompt must round-trip and contain all
    optimizer anchors or the job is reported as an error and not left half-written.

Usage:
  scaffold_suite.py --suite <suite.toml>                 # dry run: show the plan
  scaffold_suite.py --suite <suite.toml> --install       # write into $CODEX_HOME
  options: --codex-home PATH  --cwd PATH  --model NAME  --allow-unapproved
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

# Import the optimizer module so the managed block, fingerprint, and sidecar
# templates are shared verbatim (single source of truth, no drift).
_OPT_PATH = Path(__file__).resolve().parent / "optimize_codex_automations.py"
_spec = importlib.util.spec_from_file_location("ao_opt", _OPT_PATH)
OPT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(OPT)  # type: ignore

tomllib = OPT.tomllib
REQUIRED_SECTIONS = OPT.REQUIRED_SECTIONS

DEFAULT_INTEGRATOR = "repo-hygiene"

# Per-template defaults so every placeholder resolves even if params are omitted.
DEFAULTS = {
    "P1": {"coverage_floor": 75},
    "P2": {"max_loops": 10},
    "P3": {"default_branch": "main", "clean_after": True},
    "P4": {"safe_fix_only": True},
    "P5": {"lookback_hours": 24},
    "P6": {"max_changesets": 5},
    "P7": {"auto_fix_max_severity": "low", "escalate_at_or_above": "high"},
    "P8": {"lookback_hours": 24, "max_edits": 3, "config_changes_need_approval": True},
}

# Adaptive task bodies — runtime operating instructions, parameterized. They
# describe DISCOVER-then-ACT behavior; they never hardcode a project command.
BODIES = {
"P1": """## Task — coverage-and-quality ratchet (P1, producer)
Goal: raise meaningful automated-test protection, then improve test quality. You do NOT merge; hand work to '{integrator}'.
1. Discover each stack's test + coverage commands from project files. Compute FRESH aggregate coverage now; never trust a stored number.
2. If aggregate coverage < {coverage_floor}%: spend this run on the highest-impact UNCOVERED behavior (high blast radius, no current protection). Add real tests on a branch; never weaken assertions to pass. Open a tracker ticket and hand the branch to '{integrator}'.
3. If aggregate coverage >= {coverage_floor}%: stop hunting new coverage. Improve quality instead — delete or rewrite weak/tautological tests, add mutation-style proof that tests catch regressions, and tighten quality gates. Tightening is always allowed; loosening a gate requires re-confirmation.
4. Evidence = test names + coverage delta + mutation results. Respect the run budget.""",

"P2": """## Task — product-value explore/fix/confirm loop (P2, producer)
Goal: find and fix real user-facing issues by driving the running app. You do NOT merge; hand confirmed fixes to '{integrator}'.
1. Launch the app via the detected UI driver (iOS simulator or web e2e harness). Explore real screens by interacting.
2. Stop at the FIRST confirmed user-facing issue, then fix that one issue.
3. Relaunch and replay the exact path to PROVE the fix worked before continuing.
4. Do at most {max_loops} explore/fix/confirm cycles this run, then stop.
5. Each confirmed fix goes to a branch + ticket handed to '{integrator}'; it merges only when project gates pass. Evidence = the replay result.""",

"P3": """## Task — repo-hygiene integrator (P3, integrator, SOLE MERGE AUTHORITY)
Goal: turn the night's produced work into a clean '{default_branch}'. You are the only job that merges.
1. For every branch/ticket handed off this night, merge to '{default_branch}' ONLY what is safely mergeable: gates pass, no conflicts, clear ownership. Then push.
2. Clean the repo so future work starts from a clean '{default_branch}'.
3. If code cannot be safely merged, classify exactly why (conflict | failing gate | ambiguous ownership | blocked) and leave a concrete tracker follow-up. Never force a merge.
4. Anything irreversible beyond a normal gated merge (history rewrite, force-push, deploy) goes to the approval queue, not executed.
If you cannot detect the 'gates pass' signal, operate in shadow: write what you WOULD merge to the approval queue instead of merging.""",

"P4": """## Task — leftover resolver (P4, janitor)
Runs after the producers and the integrator. Goal: clear what they left behind. You do NOT hold merge authority.
1. Look for leftovers: dirty files, WIP branches, failed checks, merge conflicts, ambiguous ownership, blocked work.
2. If a leftover can be safely fixed, verified, committed, and merged WITHOUT triggering production, do it (route the merge through the integrator's gate, or queue it if the integrator already finished).
3. If not, record the blocker and exactly ONE concrete next action as a tracker ticket.
4. Never 'fix' something a producer will simply regenerate; if you see that loop, escalate it to the reflector instead of ping-ponging.""",

"P5": """## Task — collaboration meta-learner (P5, reflector)
Runs last. Goal: improve how the agent works with the user over time. Bias hard toward NO change.
1. Review the last {lookback_hours}h of run ledgers and interactions for repeated shorthand, misunderstandings, slow feedback loops, over-broad checks, missed repo boundaries, repeated verification gaps, or stale deploy assumptions.
2. Prefer memory notes. Edit AGENTS.md / canonical instructions ONLY when the lesson is durable, project-specific, not already documented, and likely to prevent a repeated mistake.
3. Prefer no change over noisy daily churn: at most a few high-signal edits per run; everything else stays a memory note. Canonical edits go through the integrator's gate.""",

"P6": """## Task — code-simplification ratchet (P6, producer)
Goal: reduce complexity WITHOUT changing behavior. You do NOT merge; hand work to '{integrator}'.
1. Discover the test/coverage commands and a complexity signal (linter warnings, duplication, long/large functions, unused symbols).
2. Pick the highest-value BEHAVIOR-PRESERVING simplification that has a test safety net. Prefer small, independently reviewable changesets.
3. Prove behavior is unchanged: tests stay green, no public API/contract change, no gate weakened. A simplification that needs a test changed to pass is NOT behavior-preserving — discard it. Never delete a test to 'simplify' (that is P1's job).
4. Up to {max_changesets} changesets this run; each goes to a branch + ticket handed to '{integrator}'. Evidence = complexity delta + green suite.""",

"P7": """## Task — code-security sweep (P7, producer, escalating)
Goal: find and remediate security issues. You do NOT merge; hand safe fixes to '{integrator}'.
1. Run the detected scanners (dependency audit / secret scan / SAST). Classify each finding by severity and type.
2. Safe, low-risk fixes up to severity '{auto_fix_max_severity}' (e.g. a dependency bump whose gates pass, no behavior change) go to a branch + ticket handed to '{integrator}'.
3. Anything at or above '{escalate_at_or_above}', or touching auth, secrets, crypto, or security config, goes to the approval queue — NEVER auto-merged, even if gates pass.
4. Leaked secrets: record location and type ONLY, never the value; open a high-priority ticket and queue rotation for a human.
5. Never weaken a security gate to make a scan pass. Evidence = finding ids + severity + remediation, secrets redacted.""",

"P8": """## Task — dev-environment self-reflection (P8, reflector)
Runs last, alongside P5. Goal: keep THIS agent's instruction files and dev tooling current with how the project is actually worked. Bias hard toward NO change.
1. Gather the last {lookback_hours}h of signals: commits, merged/blocked PRs, CI results, review comments, and run ledgers. Look for DURABLE, recurring friction whose real fix lives in the environment (a restated convention, a guardrail that keeps catching the same mistake, a stale command in the docs, a repeated task with no skill/command, a CI gap).
2. Edit THIS agent's canonical instruction file (CLAUDE.md / AGENTS.md / GEMINI.md / .cursor/rules) SURGICALLY — add or correct ONE rule, never rewrite the file. Instruction edits go through the integrator's gate.
3. Route higher-risk dev-env changes to the approval queue WITH the exact diff: new/changed hooks, lint/format/editorconfig rules, CI steps, settings or permissions, or a new skill. Never edit hooks/settings/CI silently.
4. Bias to no change: at most {max_edits} high-signal changes this run; everything else is a memory note or a tracker ticket, with the reason recorded. Never store secret values; reference signals by fingerprint.
5. Coordinate with P5: interaction-style lessons stay with P5 (memory); environment/config lessons are yours (instructions + approval-queued tooling).""",
}


def build_prompt(job: dict, job_dir: str) -> str:
    """Managed protocol block (with this job's absolute state paths) + adaptive body."""
    template = job["template"]
    params = dict(DEFAULTS.get(template, {}))
    params.update(job.get("params", {}) or {})
    params["integrator"] = job.get("hands_off_to") or DEFAULT_INTEGRATOR
    body = BODIES[template].format(**params)
    return OPT.managed_block(job_dir) + "\n\n" + body


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def cron_to_rrule(schedule: str) -> str:
    """Convert a simple 5-field cron (m h dom mon dow) to an iCalendar RRULE.
    Handles the common nightly/weekly cases the composer emits."""
    parts = (schedule or "").split()
    if len(parts) != 5:
        return "FREQ=DAILY;BYHOUR=3;BYMINUTE=0;BYSECOND=0"
    m, h, dom, mon, dow = parts
    minute = m if m.isdigit() else "0"
    hour = h if h.isdigit() else "3"
    if dow != "*":
        days = {"0": "SU", "1": "MO", "2": "TU", "3": "WE",
                "4": "TH", "5": "FR", "6": "SA"}
        byday = ",".join(days.get(d, "MO") for d in dow.split(","))
        return f"FREQ=WEEKLY;BYDAY={byday};BYHOUR={hour};BYMINUTE={minute};BYSECOND=0"
    return f"FREQ=DAILY;BYHOUR={hour};BYMINUTE={minute};BYSECOND=0"


def emit_job_toml(job: dict, prompt: str, cwd: str, model: str | None) -> str:
    """Emit a real Codex automation.toml (version/id/kind/name/prompt/status/rrule/cwds)."""
    if "'''" in prompt:  # literal triple-quote can't contain '''
        raise ValueError(f"{job.get('id')}: prompt contains ''' and can't be emitted")
    name = job.get("name") or job["id"].replace("-", " ").title()
    lines = [
        "version = 1",
        f"id = {_q(job['id'])}",
        'kind = "cron"',
        f"name = {_q(name)}",
        f"# template = {job['template']} | phase = {job.get('phase','?')} | "
        f"mode = {job.get('mode','active')} | merge_authority = {bool(job.get('merge_authority'))}",
        "prompt = '''",
        prompt,
        "'''",
        'status = "ACTIVE"',
        f"rrule = {_q(cron_to_rrule(job.get('schedule', '')))}",
        f"execution_environment = {_q(job.get('execution_environment', 'local'))}",
        f"cwds = [{_q(cwd)}]",
    ]
    if model:
        lines.append(f"model = {_q(model)}")
    return "\n".join(lines) + "\n"


def approval_state(job: dict) -> str:
    approved = job.get("approved_fingerprint")
    if not approved:
        return "pending"
    return "current" if approved == OPT.compute_fingerprint(job) else "stale"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Scaffold runnable automations from an "
                                             "approved suite.toml.")
    ap.add_argument("--suite", required=True, help="path to an approved suite.toml")
    ap.add_argument("--install", action="store_true",
                    help="actually write into <codex-home> (default: dry run)")
    ap.add_argument("--codex-home", default=None, help="override $CODEX_HOME / ~/.codex")
    ap.add_argument("--cwd", default=None, help="override job cwd (default: suite workspace)")
    ap.add_argument("--model", default=None, help="model name to set on each job (optional)")
    ap.add_argument("--allow-unapproved", action="store_true",
                    help="scaffold pending/stale jobs too (not recommended)")
    args = ap.parse_args(argv)

    suite_path = Path(args.suite).expanduser()
    if not suite_path.is_file():
        print(f"No such suite file: {suite_path}")
        return 2
    data = tomllib.loads(suite_path.read_text(encoding="utf-8"))
    suite = data.get("suite", {})
    jobs = data.get("job", [])
    if not jobs:
        print("Manifest has no [[job]] entries.")
        return 2

    cwd = args.cwd or suite.get("workspace")
    if not cwd:
        print("No cwd: manifest has no [suite].workspace and --cwd not given.")
        return 2

    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) \
        if not args.codex_home else Path(args.codex_home).expanduser()
    autos = home / "automations"

    # Gate on approval
    blockers = [(j.get("id"), approval_state(j)) for j in jobs
                if approval_state(j) != "current"]
    if blockers and not args.allow_unapproved:
        print("Refusing to scaffold — these jobs are not approved & current:")
        for jid, st in blockers:
            print(f"  - {jid}: {st}")
        print("\nApprove first:  profile_project.py approve --suite "
              f"{suite_path}\n(or pass --allow-unapproved to override, not recommended).")
        return 1

    action = "WRITE" if args.install else "DRY RUN (nothing written)"
    print(f"Scaffold suite '{suite.get('project','?')}' → {autos}  [{action}]\n")

    written, errors = [], []
    for j in jobs:
        jid = j.get("id")
        job_dir = autos / jid
        toml_path = job_dir / "automation.toml"
        try:
            prompt = build_prompt(j, str(job_dir))
            text = emit_job_toml(j, prompt, cwd, args.model)
        except (KeyError, ValueError) as e:
            errors.append((jid, str(e)))
            continue

        auth = " [merge authority]" if j.get("merge_authority") else ""
        print(f"  {jid:22} {j.get('phase','?'):11} mode={j.get('mode','active'):7}"
              f" -> {toml_path}{auth}")
        if not args.install:
            continue

        job_dir.mkdir(parents=True, exist_ok=True)
        toml_path.write_text(text, encoding="utf-8")
        # verify: parses, prompt round-trips, all required protocol sections present
        check = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        cp = check.get("prompt", "")
        missing = [s for s in REQUIRED_SECTIONS if s not in cp]
        if cp.strip() != prompt.strip() or missing:
            errors.append((jid, f"verification failed (missing sections: {missing})"))
            continue
        created: list[str] = []
        OPT.scaffold_sidecars(job_dir, created)
        written.append(jid)

    if args.install:
        # place the manifest alongside the jobs so --fleet can find it
        autos.mkdir(parents=True, exist_ok=True)
        (autos / "suite.toml").write_text(suite_path.read_text(encoding="utf-8"),
                                          encoding="utf-8")

    print()
    if args.install:
        print(f"Installed {len(written)} job(s): {', '.join(written)}")
        print(f"Manifest copied to {autos / 'suite.toml'}")
    if errors:
        print(f"Errors ({len(errors)}):")
        for jid, why in errors:
            print(f"  - {jid}: {why}")
    if not args.install:
        print("Dry run only. Re-run with --install to write these automations.")
    else:
        print("\nVerify:")
        print(f"  optimize_codex_automations.py --codex-home {home} --strict")
        print(f"  optimize_codex_automations.py --fleet {autos / 'suite.toml'} --require-approved")
        print("Your scheduler picks up automation.toml files under "
              f"{autos}; confirm they appear and are enabled.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
