# Optimizer Contract

Every recurring job must satisfy these items. Each line states the control and the one-line reason. The canonical managed block in `optimizer-block.md` encodes all of them; `--strict` in the helper checks that the markers for each are present.

## Core state
1. **Persistent memory file** — `state/memory.md` holds stable fingerprints and prior decisions so the job recognizes what it has already seen.
2. **Run ledger** — `state/last-run.md` plus dated entries in `state/runs/` record every run's outcome, so the job (and you) can audit history.
3. **Observability metrics** — each ledger entry records outcome, runtime, items touched, and retries, exposing drift (slowing runs, rising failures) that a pass/fail log hides. Store metrics, never logs/dumps.

## Safe execution
4. **Concurrency lock** — refuse to run if another instance holds the lock (`state/.lock`); prevents two runs racing on the same repo/tracker.
5. **Idempotency guarantee** — an interrupted-then-retried run must not double-create, double-comment, or double-merge. Enforced via dedupe-by-fingerprint + the ledger. (The lock prevents *parallel* runs; this covers *sequential* reruns.)
6. **Tool/environment preflight** — verify required tools, network, and working dirs are available before acting; fail fast with a clear reason rather than half-running.
7. **Least-privilege credential preflight** — confirm required credentials/scopes are present without printing them; refuse to run if missing.

## Deciding what to do
8. **Change-detection short-circuit** — re-read live state (repo HEAD, open tracker items, inputs), compare to the last run's fingerprints; if nothing watched changed, log a no-op and exit. This is the primary defense against duplicate work and wasted cost.
9. **Baseline failure registry** — `state/known-failures.md` lists pre-existing/expected failures so the job doesn't re-report or try to "fix" known-bad baselines.
10. **Priority queue / next-best-target logic** — pick the highest-value unblocked target rather than re-doing the first thing every run.

## Bounds and failure handling
11. **Continuation loop with scope / loop stop rules** — each *unit* of work is bounded, but a *run* keeps completing the next highest-value unblocked unit instead of stopping after one (operators finish fast and the night is long). The run continues until a stop rule fires: the per-run unit budget is reached (a pattern's own `max_loops`/`max_changesets`/`max_edits`/`max_units` cap, or a default of 5 when none is stated), the priority queue is drained, two consecutive units fail/block, the next unit crosses an approval boundary, or the job detects it is ping-ponging its own output. This is distinct from the start-of-run change-detection gate (item 8), which still exits as a no-op when nothing changed.
12. **Cost & runtime budget** — explicit caps: max wall-clock, max tool calls, max subagents, and a max-units-per-run count (the continuation-loop budget). Prevents runaway cost while still using the whole night.
13. **Retry/backoff policy** — transient failures retry with capped exponential backoff + jitter; only `transient`-classified failures are retried.
14. **Failure taxonomy** — classify each failure (`transient`, `config`, `baseline`, `blocked`, `needs-human`) so the right handler runs.
15. **Failure-escalation threshold** — after N consecutive failed runs (default 3), stop retrying silently and surface to the approval queue / notify a human.

## Evidence and change control
16. **Evidence bundle rules** — capture the minimal proof a change is safe (test names, diffs, ids) — not full logs or screenshots.
17. **Rollback note** — for any state-changing action, the ledger entry records how to undo it (branch, issue id, commit sha).
18. **Human-approval queue** — `state/approval-queue.md` holds actions that exceed the safe boundary (deploy, secrets, history rewrite, external sends, deletes) for a human to approve.

## Agentic execution and closeout
19. **Agentic execution for non-trivial runs** — split inventory, implementation, verification/review, and integration across specialist agents/subagents when the tool supports it.
20. **Verified safe-merge bias** — when project rules allow, a completed safe fix is pushed, synced, and merged to the default branch rather than left stranded; otherwise it goes to the approval queue with a reason.
21. **Safe closeout & report** — end every run by writing the ledger entry, updating memory fingerprints, releasing the lock, and emitting the plain-English report.

## Hygiene
22. **Versioned managed block, no duplicates** — exactly one current managed block (with a `VERSION` marker) sits before the task-specific instructions; older/duplicate optimizer blocks are removed on upgrade.
