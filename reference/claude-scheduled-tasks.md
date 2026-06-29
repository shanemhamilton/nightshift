# Claude / Non-Codex Scheduled Tasks

There is no universal registry for Claude scheduled tasks. Find the source of truth before editing.

## Discover
Search, in order:
1. The project itself — look for committed scheduled-task prompts, runbooks, or cron definitions (`*.md`, `Makefile`, `.github/workflows/*.yml`, `crontab`, `*.runbook.md`).
2. `~/.claude` and any project `.claude/` — named automation prompts or task definitions.
3. OS schedulers — `launchd` (`~/Library/LaunchAgents/*.plist` on macOS), `cron` (`crontab -l`), systemd timers (`systemctl --user list-timers`). These point *to* a command/prompt; they are rarely the prompt itself.

## Edit
- Apply the same contract (`optimizer-contract.md`) and the same managed block (`optimizer-block.md`). The block is plain text — prepend it to the task's prompt/runbook just as the helper does for Codex TOML.
- Keep `state/` next to the task definition.
- The Python helper is Codex-TOML-specific. For Claude tasks, inject the block by editing the discovered file directly (or extend the helper's `discover()` to recognize the file type — it's structured for that).

## Scheduler boundary
- If an external scheduler (launchd/cron/systemd/CI) owns the cadence, **update the prompt/runbook only**. Do not rewrite the scheduler entry blindly.
- If the scheduler itself needs a change (new cadence, new env, new path), report the exact change needed and put it in the approval queue — that is a system-level change requiring human sign-off.

## Claude `mcp__scheduled-tasks__*`
If the task is a Claude-managed scheduled task, its prompt is the thing to harden. List with `list_scheduled_tasks`, update the prompt text with `update_scheduled_task`, and keep the cadence unless asked to change it. The managed block goes at the top of that prompt.
