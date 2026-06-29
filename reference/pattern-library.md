# Adaptive Pattern Library

Eight reusable, capability-driven automation patterns. They are written in terms of *discover X, then act*, never *run a fixed command*, so one template adapts across backend, iOS, and mixed projects. Each carries the managed optimizer block (`optimizer-block.md`) above its task body and is wired into a suite by `suite-manifest.md`.

Conventions used below:
- **phase** — producer | integrator | janitor | reflector (defines run order; see manifest).
- **merge_authority** — only the integrator is `true`. Producers/janitor/reflector are `false` and hand off via branches + tracker tickets.
- **requires / degrades** — capabilities the job needs, and what it does when one is absent (always *propose-only or no-op + record why*, never guess).
- **params** — manifest-overridable knobs with defaults.

---

## P1 — coverage-and-quality ratchet  (phase: producer, merge_authority: false)
From Example 1. Raise *meaningful* automated-test protection, then pivot to test quality.

- **requires:** a detectable test runner + coverage capability for at least one stack.
- **degrades:** if no coverage tooling is found for a stack, skip that stack and record it; if none at all, propose-only.
- **params:** `coverage_floor = 75` (aggregate %), `quality_mode_when_met = true`, `max_units = 5` (continuation-loop budget).
- **adaptive body:**
  - Discover stacks and their test/coverage commands from project files; compute *fresh* aggregate coverage (never trust a stored number — AO-08).
  - **If aggregate < `coverage_floor`:** spend the unit on the highest-impact *uncovered behavior* (not line-padding). Prefer behavior with high blast radius and no current protection. Add tests on a branch; open a tracker ticket; hand off to the integrator. Never weaken assertions to pass.
  - **If aggregate ≥ `coverage_floor`:** stop hunting new coverage by default. Improve quality instead — delete or rewrite weak/tautological tests, add mutation-style proof that tests actually catch regressions, and tighten quality gates. Tightening a gate never needs re-confirmation; loosening one does (fingerprint rule).
  - **Continuation loop:** after a unit, pick the next-highest-impact independent target and repeat — up to `max_units` units per run, stopping early on diminishing returns, two consecutive blocked units, or an empty target list.
  - Evidence per unit = test names + coverage delta + mutation results, not full logs.

## P2 — product-value explore/fix/confirm loop  (phase: producer, merge_authority: false)
From Example 2. Find and fix real user-facing issues by driving the running app.

- **requires:** a runnable UI target + a driver (e.g. iOS simulator via `xcrun simctl`, or a web/e2e harness). This is the most capability-gated pattern.
- **degrades:** if no runnable UI/driver is detected, **skip entirely** and record why — do not fabricate a UI session.
- **params:** `max_loops = 10`, `merge_confirmed_fixes = true` (still routed through the integrator, not self-merged).
- **adaptive body:**
  - Launch the app, explore real screens by interacting, and **stop at the first confirmed user-facing issue.**
  - Fix that one issue, relaunch, and **replay the exact path to prove the fix** before moving on.
  - Loop: after a confirmed+replayed fix, explore for the next issue — up to `max_loops` explore/fix/confirm cycles per run (AO-11/12 budget), stopping early on two consecutive cycles that find nothing or can't confirm a fix.
  - Confirmed fixes go to a branch + ticket and are handed to the integrator; they merge only when project gates pass. Capture the replay result as evidence.

## P3 — repo-hygiene integrator  (phase: integrator, merge_authority: TRUE — exactly one per suite)
From Example 3. The sole merge authority. Turns the night's produced work into a clean `main`.

- **requires:** a git repo with a detectable default branch and a detectable "gates pass" signal.
- **degrades:** if gates can't be detected, run in `shadow` (write what it *would* merge to the approval queue) until configured.
- **params:** `default_branch = <discovered>`, `gate = <discovered>`, `clean_after = true`.
- **adaptive body:**
  - For every branch/ticket handed off this night, merge to the default branch **only what can be safely merged** (gates pass, no conflicts, clear ownership). Then push.
  - Clean the repo so future work starts from a clean default branch (resolve stray state per project rules).
  - **If code cannot be safely merged, classify exactly why** (conflict | failing gate | ambiguous ownership | blocked) and leave a concrete tracker follow-up — never force the merge.
  - Anything irreversible beyond a normal gated merge (history rewrite, force-push, deploy) goes to the approval queue (AO-18), not executed.

## P4 — leftover resolver  (phase: janitor, merge_authority: false)
From Example 4. Runs *after* producers and the integrator; depends on them by contract.

- **requires:** read access to git state + the tracker.
- **degrades:** always runnable; if nothing is dirty, change-detection short-circuits to a no-op (AO-08).
- **params:** `safe_fix_only = true`, `max_units = 5` (continuation-loop budget).
- **adaptive body:**
  - Look for leftovers from the producer + integrator phases: dirty files, WIP branches, failed checks, merge conflicts, ambiguous ownership, blocked work.
  - **Resolve them in a loop, highest-value first, up to `max_units` per run.** For each: if it can be safely fixed, verified, committed, and merged without triggering production, do it (handing the merge to the integrator's gate, or queuing if the integrator has already finished). If not, record the blocker and exactly one concrete next action as a tracker ticket. Stop early when nothing actionable remains or two consecutive leftovers are blocked.
  - Avoid the ping-pong failure mode: never "fix" something a producer will simply regenerate — if you see a loop, escalate it to the reflector instead.

## P5 — collaboration meta-learner  (phase: reflector, merge_authority: false)
From Example 5. Runs last; improves how the agent works with the user over time.

- **requires:** access to the last ~24h of run ledgers / interaction signals.
- **degrades:** if there's nothing new, no-op; biased toward *no change*.
- **params:** `lookback_hours = 24`, `edit_canonical_only_if_durable = true`.
- **adaptive body:**
  - Review the lookback window for repeated shorthand, misunderstandings, slow feedback loops, over-broad checks, missed repo boundaries, repeated verification gaps, or stale deploy assumptions.
  - **Prefer memory notes.** Edit `AGENTS.md`/canonical instructions only when the lesson is durable, project-specific, not already documented, and likely to prevent a repeated mistake.
  - **Prefer no change over noisy daily churn.** At most a small number of high-signal edits per run; everything else stays a memory note. Canonical edits go through the integrator's gate like any other change.

## P6 — code-simplification ratchet  (phase: producer, merge_authority: false)
Reduce complexity without changing behavior: dead code, duplication, needless
indirection, over-broad scopes, stale flags.

- **requires:** a test/behavior safety net for the area being simplified — a
  passing test suite and ideally coverage on the touched code.
- **degrades:** if the touched area has no behavioral protection, **propose-only**
  (open a ticket describing the simplification) rather than refactor blind. If a
  simplification can't be proven behavior-preserving, it stays a proposal.
- **params:** `max_changesets = 5`, `behavior_must_be_proven = true`,
  `public_api_changes = false`.
- **adaptive body:**
  - Discover the test/coverage commands and a complexity signal (linter warnings,
    duplication report, large/long functions, unused symbols) from project files.
  - Pick the highest-value *behavior-preserving* simplification with a safety net.
    Prefer small, independently reviewable changesets over sweeping rewrites.
  - Prove behavior is unchanged: tests stay green, no public API/contract change
    (unless `public_api_changes` allows it), no gate weakened. A simplification
    that needs a test changed to pass is **not** behavior-preserving — discard it.
  - Up to `max_changesets` per run (AO-11/12). Each goes to a branch + ticket and
    is handed to the integrator; merges only when gates pass.
  - Coordinate with P1: never delete a test to "simplify"; that's P1's quality
    job and must preserve protection. Evidence = complexity delta + green suite.

## P7 — code-security sweep  (phase: producer, merge_authority: false, escalating)
Find and remediate security issues; the most safety-gated producer after P2.

- **requires:** at least one detectable scanner — dependency audit
  (`npm audit`/`pip-audit`/`bundler-audit`/`govulncheck`), secret scan
  (`gitleaks`/`trufflehog`), or SAST (`semgrep`/CodeQL).
- **degrades:** if no scanner is present, **propose-only** — report findings it
  can determine and recommend enabling a scanner; never invent a scan result.
- **params:** `auto_fix_max_severity = "low"`, `escalate_at_or_above = "high"`, `max_units = 5` (continuation-loop budget for safe fixes).
- **adaptive body:**
  - Run the detected scanners and classify each finding by severity and type
    (vulnerable dependency, leaked secret, injection/unsafe pattern, misconfig).
  - **Safe, low-risk fixes** (e.g. a dependency bump whose gates pass, with no
    behavior change) up to `auto_fix_max_severity` go to a branch + ticket and the
    integrator's gate — like any other producer. **Loop over them up to `max_units`
    per run,** stopping early when none remain or two consecutive fixes fail their
    gate. Escalations (below) are queued, never fixed, and don't count against the budget.
  - **Anything at or above `escalate_at_or_above`, or that touches auth, secrets,
    crypto, or security config, goes to the approval queue (AO-18) — never
    auto-merged**, even if gates pass. Security fixes can be subtly wrong; a human
    confirms the high-severity ones.
  - **Leaked secrets:** record the *location and type only, never the value*
    (AO-16), open a high-priority ticket, and queue rotation for a human. Do not
    commit a secret value anywhere, including memory.
  - Never weaken a security gate to make a scan pass. Evidence = finding ids +
    severity + remediation, with secrets redacted.

## P8 — dev-environment self-reflection  (phase: reflector, merge_authority: false)
Runs last, alongside P5. Where P5 learns how the agent *collaborates* with the user, P8 reflects on the agent's *operating environment* — it keeps the instruction files and dev tooling current with how the project is actually worked.

- **requires:** read access to the last ~24h of signals (commits, merged/blocked PRs, CI results, review comments, run ledgers) and at least one canonical instruction file or dev-env config present.
- **degrades:** if nothing recurring is actionable, no-op — biased hard toward *no change*. Sensitive, execution-shaping config (hooks, settings/permissions, CI) is never edited directly; it is proposed to the approval queue.
- **params:** `lookback_hours = 24`, `max_edits = 3`, `config_changes_need_approval = true`.
- **adaptive body:**
  - Gather the day's signals and look for *durable, recurring* friction whose real fix lives in the environment: a convention people keep restating, a guardrail that keeps catching the same class of mistake, a stale command in the docs, a repeated task with no skill/command, a gap in CI.
  - **Edit the canonical instruction file for this agent** — `CLAUDE.md` / `AGENTS.md` / `GEMINI.md` / `.cursor/rules` per the adapter table — *surgically*: add or correct a single rule, never rewrite the file. Instruction edits go through the integrator's gate like any other change.
  - **Route higher-risk dev-env changes to the approval queue (AO-18)** with the exact diff: new/changed hooks, lint/format/editorconfig rules, CI steps, settings or permissions, or a new skill. These shape execution, so a human confirms them — never edit hooks/settings/CI silently.
  - **Bias to no change.** At most `max_edits` high-signal changes per run; everything else becomes a memory note or a tracker ticket, with the reason recorded. Never store secret values; reference signals by fingerprint (AO-16).
  - Coordinate with P5: interaction-style lessons stay with P5 (→ memory); environment/config lessons are P8's (→ instruction files + approval-queued tooling). If a file is already correct, leave it.

---

## How they compose

Default healthy DAG when all eight apply:

```
producers (P1 coverage, P2 product-value, P6 simplification, P7 security)
        │  hand off branches + tickets  (P7 high-severity → approval queue)
        ▼
integrator (P3 hygiene)  ← the ONLY job that merges to main
        │
        ▼
janitor (P4 leftovers)   ← cleans what producers/integrator left
        │
        ▼
reflectors (P5 collaboration → memory, P8 dev-environment → instructions/config)
        ← run last, review the whole night, bias to no change
```

Producers never merge; they produce. The integrator is the single merge authority. The janitor depends on the producers and integrator finishing. The two reflectors run last and bias to no change — P5 tunes collaboration into memory, P8 keeps the instruction files and tooling current (instruction edits through the integrator's gate, execution-shaping config to the approval queue). The composer omits any pattern whose capabilities aren't present (e.g., P2 on a backend-only repo, P7 with no scanner) and still produces a valid smaller suite.

All four producers can share the same producer window; they don't conflict because none of them merge — they only open branches and tickets that the single integrator later reconciles. P7's high-severity findings and P6's unprovable simplifications route to the approval queue rather than the integrator.
