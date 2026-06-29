# Agent Adapters

The skill supports four coding agents. Each stores recurring automations and canonical instructions differently; the differences are encoded once in `scripts/agent_adapters.py` (the source of truth) and summarized here. The managed **protocol block is the same plain text for all four** — only the container file and the scheduler differ.

`scheduler` drives install policy:
- **native_file** — writing the registry file *is* the registration. The scaffolder can write it (with `--install`).
- **external_cron** — no native registry; scheduling is cron + a headless CLI call. The tooling **emits** the crontab line and the prompt file; it never edits crontab.
- **cloud_api** — automations live in the vendor cloud. The tooling **emits** the config/payload; it never creates cloud resources.

## Codex CLI — `codex`  (native_file)
- Registry: `~/.codex/automations/<id>/automation.toml`.
- Schema (real): `version`, `id`, `kind="cron"`, `name`, flat `prompt` (string), `status="ACTIVE"`, **`rrule`** (iCalendar, e.g. `FREQ=WEEKLY;BYHOUR=2;BYMINUTE=30`), `model`, `reasoning_effort`, `execution_environment`, **`cwds=[…]`**.
- Sidecars at the **job root**: `memory.md`, `last-run.md`, `priority-queue.md`, `human-approval.md`, `baseline-failures.md`, `runs/`, `.automation.lock`.
- Canonical instructions: `AGENTS.md`. Scheduler: the local Codex app picks up `automation.toml` files.

## Claude Code — `claude`  (native_file)
- Registry: `~/.claude/scheduled-tasks/<name>/SKILL.md`, run by the Claude scheduling daemon (`~/.claude/daemon`, `daemon.lock`).
- The SKILL.md **body is the prompt**; prepend the protocol block to it. Same job-root sidecar convention as Codex.
- Canonical instructions: `CLAUDE.md` (home + per-project), `.claude/rules`.
- Note: on this machine these tasks often **orchestrate headless Codex fleets** (Claude owns judgment/merge gates, Codex does labor). The protocol block applies to the orchestrator prompt.

## Gemini CLI — `gemini`  (external_cron)
- No native registry. A recurring job = a prompt file + a cron entry running `gemini -p "$(cat <prompt>)"`.
- Convention used by the scaffolder: `~/.gemini/automations/<id>/prompt.md` (protocol block + body) + job-root sidecars. The tool prints the crontab line for you to add.
- Canonical instructions: `GEMINI.md` (project) / `~/.gemini/GEMINI.md`. MCP config in `~/.gemini/settings.json`.

## Cursor — `cursor`  (cloud_api)
- Automations are cloud (dashboard/API), triggered by cron or events (GitHub/GitLab/Slack/webhooks). Not inspectable or installable locally.
- Locally only project rules exist: `.cursor/rules/*.mdc`, `.cursorrules`, `AGENTS.md`.
- The tool manages the canonical rules and **emits** the automation config for you to create in Cursor; it never creates cloud resources.

## What this means per mode
- **Discovery** (`discover_agents.py`) scans all four read-only and reports installed automations, protocol-block status, missing sidecars, orphans, and canonical files.
- **Optimizer** edits file-based registries in place (Codex now; Claude SKILL.md by the same protocol). For cron/cloud agents it hardens the prompt and reports scheduler changes for you to apply.
- **Composer/scaffolder** writes file-based jobs (`--install`) and, for cron/cloud agents, writes the prompt files and emits the exact crontab line or cloud payload.

## Canonical-instructions file by agent (used by P5 meta-learner and P8 dev-environment reflector)
`codex → AGENTS.md`, `claude → CLAUDE.md`, `gemini → GEMINI.md`, `cursor → .cursor/rules`.
P8 edits this file surgically for durable environment lessons; execution-shaping config it finds nearby (hooks, settings/permissions, CI under `.github/workflows`, lint/format config) is proposed to the approval queue, never edited directly.
