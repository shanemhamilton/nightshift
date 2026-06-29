#!/usr/bin/env python3
"""
pattern_bodies.py — adaptive task bodies + per-template defaults for P1..P8.

Pure data, kept separate from the materializer logic that consumes it. Each body
is a parameterized DISCOVER-then-ACT instruction; it never hardcodes a project
command. `{integrator}` and each template's params are filled in at build time.
Shared by agent_materializers.py (and thus scaffold_suite.py and lifecycle.py) so
there is exactly one copy.
"""
from __future__ import annotations

# Per-template defaults so every placeholder resolves even if params are omitted.
# `max_units` / `max_loops` / `max_changesets` / `max_edits` are each pattern's
# per-run continuation-loop budget: how many bounded units it completes before
# closeout (it still stops early on diminishing returns, repeated failure, or an
# empty queue). Reflectors (P5/P8) keep a deliberately low edit cap — for them
# "more loops" means reviewing more signals, not making more changes.
DEFAULTS = {
    "P1": {"coverage_floor": 75, "max_units": 5},
    "P2": {"max_loops": 10},
    "P3": {"default_branch": "main", "clean_after": True},
    "P4": {"safe_fix_only": True, "max_units": 5},
    "P5": {"lookback_hours": 24},
    "P6": {"max_changesets": 5},
    "P7": {"auto_fix_max_severity": "low", "escalate_at_or_above": "high", "max_units": 5},
    "P8": {"lookback_hours": 24, "max_edits": 3, "config_changes_need_approval": True},
}

BODIES = {
"P1": """## Task — coverage-and-quality ratchet (P1, producer)
Goal: raise meaningful automated-test protection, then improve test quality. You do NOT merge; hand work to '{integrator}'.
1. Discover each stack's test + coverage commands from project files. Compute FRESH aggregate coverage now; never trust a stored number.
2. If aggregate coverage < {coverage_floor}%: spend this unit on the highest-impact UNCOVERED behavior (high blast radius, no current protection). Add real tests on a branch; never weaken assertions to pass. Open a tracker ticket and hand the branch to '{integrator}'.
3. If aggregate coverage >= {coverage_floor}%: stop hunting new coverage. Improve quality instead — delete or rewrite weak/tautological tests, add mutation-style proof that tests catch regressions, and tighten quality gates. Tightening is always allowed; loosening a gate requires re-confirmation.
4. Then keep going: pick the next-highest-impact independent target and repeat steps 2-3 for up to {max_units} units this run, stopping early on diminishing returns, two consecutive blocked units, or an empty target list.
5. Evidence per unit = test names + coverage delta + mutation results. Respect the run budget.""",

"P2": """## Task — product-value explore/fix/confirm loop (P2, producer)
Goal: find and fix real user-facing issues by driving the running app. You do NOT merge; hand confirmed fixes to '{integrator}'.
1. Launch the app via the detected UI driver (iOS simulator or web e2e harness). Explore real screens by interacting.
2. Stop at the FIRST confirmed user-facing issue, then fix that one issue.
3. Relaunch and replay the exact path to PROVE the fix worked before continuing.
4. Loop: after a confirmed+replayed fix, explore for the next issue. Do up to {max_loops} explore/fix/confirm cycles this run; stop early on two consecutive cycles that find nothing or cannot confirm a fix. Record the stop reason.
5. Each confirmed fix goes to a branch + ticket handed to '{integrator}'; it merges only when project gates pass. Evidence = the replay result.""",

"P3": """## Task — repo-hygiene integrator (P3, integrator, SOLE MERGE AUTHORITY)
Goal: turn the night's produced work into a clean '{default_branch}'. You are the only job that merges.
1. Drain the handoff queue: loop over EVERY branch/ticket handed off this night and merge to '{default_branch}' each one that is safely mergeable (gates pass, no conflicts, clear ownership), then push. Producers now do multiple units per night, so expect several handoffs — keep merging until the queue is empty or no remaining item is safely mergeable. A long producer night that you cannot fully drain gets finished on the next integrator run.
2. Clean the repo so future work starts from a clean '{default_branch}'.
3. If code cannot be safely merged, classify exactly why (conflict | failing gate | ambiguous ownership | blocked) and leave a concrete tracker follow-up. Never force a merge.
4. Anything irreversible beyond a normal gated merge (history rewrite, force-push, deploy) goes to the approval queue, not executed.
If you cannot detect the 'gates pass' signal, operate in shadow: write what you WOULD merge to the approval queue instead of merging.""",

"P4": """## Task — leftover resolver (P4, janitor)
Runs after the producers and the integrator. Goal: clear what they left behind. You do NOT hold merge authority.
1. Look for leftovers: dirty files, WIP branches, failed checks, merge conflicts, ambiguous ownership, blocked work.
2. Resolve them in a loop, highest-value first, for up to {max_units} leftovers this run. For each: if it can be safely fixed, verified, committed, and merged WITHOUT triggering production, do it (route the merge through the integrator's gate, or queue it if the integrator already finished). If not, record the blocker and exactly ONE concrete next action as a tracker ticket.
3. Stop early when nothing actionable remains or two consecutive leftovers are blocked; record the stop reason.
4. Never 'fix' something a producer will simply regenerate; if you see that loop, escalate it to the reflector instead of ping-ponging.""",

"P5": """## Task — collaboration meta-learner (P5, reflector)
Runs last. Goal: improve how the agent works with the user over time. Bias hard toward NO change.
1. Review the last {lookback_hours}h of run ledgers and interactions for repeated shorthand, misunderstandings, slow feedback loops, over-broad checks, missed repo boundaries, repeated verification gaps, or stale deploy assumptions.
2. Prefer memory notes. Edit AGENTS.md / canonical instructions ONLY when the lesson is durable, project-specific, not already documented, and likely to prevent a repeated mistake.
3. Prefer no change over noisy daily churn: at most a few high-signal edits per run; everything else stays a memory note. Canonical edits go through the integrator's gate. For you, "use the night well" means review the FULL lookback window thoroughly, not make more edits — the low edit cap is intentional.""",

"P6": """## Task — code-simplification ratchet (P6, producer)
Goal: reduce complexity WITHOUT changing behavior. You do NOT merge; hand work to '{integrator}'.
1. Discover the test/coverage commands and a complexity signal (linter warnings, duplication, long/large functions, unused symbols).
2. Pick the highest-value BEHAVIOR-PRESERVING simplification that has a test safety net. Prefer small, independently reviewable changesets.
3. Prove behavior is unchanged: tests stay green, no public API/contract change, no gate weakened. A simplification that needs a test changed to pass is NOT behavior-preserving — discard it. Never delete a test to 'simplify' (that is P1's job).
4. Loop: pick the next-highest-value behavior-preserving simplification and repeat. Up to {max_changesets} changesets this run; each goes to a branch + ticket handed to '{integrator}'. Stop early when no proven-safe simplification remains or two consecutive candidates can't be proven behavior-preserving; record the stop reason. Evidence per changeset = complexity delta + green suite.""",

"P7": """## Task — code-security sweep (P7, producer, escalating)
Goal: find and remediate security issues. You do NOT merge; hand safe fixes to '{integrator}'.
1. Run the detected scanners (dependency audit / secret scan / SAST). Classify each finding by severity and type.
2. Loop over the safe, low-risk findings up to severity '{auto_fix_max_severity}' (e.g. a dependency bump whose gates pass, no behavior change): fix up to {max_units} of them this run, each on a branch + ticket handed to '{integrator}'. Stop early when none remain or two consecutive fixes fail their gate; record the stop reason. Escalations (step 3) are queued, not fixed, and never count against this budget.
3. Anything at or above '{escalate_at_or_above}', or touching auth, secrets, crypto, or security config, goes to the approval queue — NEVER auto-merged, even if gates pass.
4. Leaked secrets: record location and type ONLY, never the value; open a high-priority ticket and queue rotation for a human.
5. Never weaken a security gate to make a scan pass. Evidence = finding ids + severity + remediation, secrets redacted.""",

"P8": """## Task — dev-environment self-reflection (P8, reflector)
Runs last, alongside P5. Goal: keep THIS agent's instruction files and dev tooling current with how the project is actually worked. Bias hard toward NO change.
1. Gather the last {lookback_hours}h of signals: commits, merged/blocked PRs, CI results, review comments, and run ledgers. Look for DURABLE, recurring friction whose real fix lives in the environment (a restated convention, a guardrail that keeps catching the same mistake, a stale command in the docs, a repeated task with no skill/command, a CI gap).
2. Edit THIS agent's canonical instruction file (CLAUDE.md / AGENTS.md / GEMINI.md / .cursor/rules) SURGICALLY — add or correct ONE rule, never rewrite the file. Instruction edits go through the integrator's gate.
3. Route higher-risk dev-env changes to the approval queue WITH the exact diff: new/changed hooks, lint/format/editorconfig rules, CI steps, settings or permissions, or a new skill. Never edit hooks/settings/CI silently.
4. Bias to no change: at most {max_edits} high-signal changes this run; everything else is a memory note or a tracker ticket, with the reason recorded. Like P5, "use the night well" means scanning more signals thoroughly, NOT making more edits — the low cap is intentional. Never store secret values; reference signals by fingerprint.
5. Coordinate with P5: interaction-style lessons stay with P5 (memory); environment/config lessons are yours (instructions + approval-queued tooling).""",
}
