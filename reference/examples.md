# Worked Examples

## Example 1 — Nightly repo-hygiene job (Codex)

**Before:** `automation.toml` prompt is "Each night, check the repo for lint errors and broken tests, fix what you can, and open issues for the rest."

Problems: no memory (re-opens the same issues nightly), no lock (two runs can race), no change-detection (runs full analysis even when nothing changed), no failure handling, fixes left on a local branch.

**After optimizer:**
1. Discover: found under `~/.codex/automations/nightly-hygiene/automation.toml`, schedule `0 6 * * *`, status active — preserved.
2. `python3 .../optimize_codex_automations.py` → dry-run diff shows the v2 block would be prepended; status `needs-update`.
3. `--apply` → block injected, `.bak` written, `state/` scaffolded with all templates.
4. Result: the job now short-circuits when `repo_head` is unchanged (AO-08), dedupes issues by fingerprint so it stops re-opening the same one (AO-05/AO-01), retries flaky tests with backoff (AO-13), queues any force-push or deploy for approval (AO-18), and merges clean lint fixes to the default branch when checks pass (AO-20).
5. `--strict` → all anchors present, sidecars exist, exit 0.

Report: Updated `nightly-hygiene`; state files created; verification via `--strict` passed.

## Example 2 — Claude reminder/monitor prompt (non-Codex)

**Before:** A Claude scheduled task: "Every morning, check if the staging deploy is green and tell me if it's broken."

Problems: messages every morning even when nothing changed; no record of what it already told you; no escalation if staging is broken for days.

**After optimizer:**
1. Discover via `list_scheduled_tasks` (no Codex TOML involved).
2. Prepend the managed block to the task prompt with `update_scheduled_task`; cadence unchanged.
3. Keep `state/` beside the task. Now it only notifies on a *change* in staging status (AO-08), records each notification (AO-02), and after 3 consecutive broken-staging mornings escalates instead of repeating the same alert (AO-15).

Report: Updated the staging-monitor task prompt; cadence preserved; no scheduler change needed.

## Example 3 — Externally-managed cron (boundary case)

A `crontab` entry runs a project script nightly. The optimizer hardens the *prompt/runbook the script invokes*, scaffolds `state/` in the project, and reports: "Cron cadence is owned by the system crontab; no change made there. If you want hourly instead of nightly, that crontab edit needs your approval." The cron line itself is left untouched.
