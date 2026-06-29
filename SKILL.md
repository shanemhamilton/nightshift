---
name: automation-optimizer
description: Audits and hardens recurring automations so they behave like reliable operators instead of stateless prompts — and composes coherent new automation suites per project from an adaptive pattern library — across Codex, Claude Code, Gemini CLI, and Cursor. Adds persistent memory, run ledgers, concurrency locks, tool preflight, change-detection, failure taxonomy, stop rules, multi-agent execution, approval queues, and verified safe-merge closeout; wires new jobs into a coordinated fleet with one merge authority. Use when the user wants to create, review, standardize, optimize, or harden scheduled or recurring jobs — Codex automations (~/.codex/automations/*/automation.toml), Claude scheduled-tasks (~/.claude/scheduled-tasks/*/SKILL.md), Gemini cron jobs, Cursor cloud automations, nightly maintenance loops, repo-hygiene/QA/coverage/security/simplification jobs, integration-rescue jobs, or reminder/monitor prompts — or to propose what automations a project should have, see what's already installed across agents, or set them up to work together. Also triggers on requests to stop an automation duplicating work, make a job idempotent, give an automation memory, add a human-approval gate, or build a nightly automation suite.
---

# Automation Optimizer & Composer

Three jobs in one skill, across four agents. **Optimizer mode** hardens *existing* recurring automations so each behaves like a reliable operator instead of a stateless prompt. **Composer mode** proposes and scaffolds *new* automations per project — a coordinated suite drawn from an adaptive pattern library, wired so the jobs work together instead of colliding. **Discovery mode** inventories what's already installed across every agent. Composer always produces jobs that already satisfy the optimizer contract, so the modes compose cleanly.

Pick the mode from the request: "harden / review / optimize this job" → optimizer; "what automations should this project have / set up a nightly suite / add a coverage (etc.) automation" → composer; "what automations do I already have / check what's in place" → discovery.

For managing one automation across its whole life — **set up, add, remove, or update** a job on any agent — use the **lifecycle** front door (`scripts/lifecycle.py`), detailed in `reference/lifecycle.md`. The four verbs are the same pipeline (mutate `suite.toml` → validate the fleet → gate on approval → materialize per agent), and they produce jobs that already satisfy the optimizer contract.

## Installing this skill

Run the self-installer; it detects every agent on the machine and copies the skill into each one's skills directory (`~/.codex/skills`, `~/.claude/skills`, `~/.gemini/skills`; Cursor has no global skills dir and is skipped):

```
python3 scripts/install_skill.py            # install for all detected agents
python3 scripts/install_skill.py --dry-run  # preview
python3 scripts/install_skill.py --agents claude   # one agent
```

Re-running updates in place and backs up any prior install. Locations come from `scripts/agent_adapters.py`.

## Supported agents

Per-agent locations, formats, canonical-instruction files, and scheduler types are in `scripts/agent_adapters.py` (the single source of truth) and explained in `reference/agent-adapters.md`. Summary:

- **Codex CLI** — native file registry: `~/.codex/automations/<id>/automation.toml` (flat `prompt`, `rrule` schedule, `cwds[]`, `status=ACTIVE`), sidecars at the job root, canonical `AGENTS.md`. Fully installable by writing files.
- **Claude Code** — native daemon registry: `~/.claude/scheduled-tasks/<name>/SKILL.md`, canonical `CLAUDE.md`. Installable by writing the SKILL.md directory.
- **Gemini CLI** — no native registry: cron + `gemini -p`, canonical `GEMINI.md`. The tooling **emits** the crontab line; it never edits cron.
- **Cursor** — cloud automations (dashboard/API), canonical `.cursor/rules` / `AGENTS.md`. The tooling **emits** the cloud config; it never creates cloud resources.

The managed **protocol block is identical plain text across all four** (markers `## Automation Optimizer Protocol` … `## End Automation Optimizer Protocol`, `Protocol version: N`); only the container and scheduler differ per agent.

## Discovery — see what's already in place

```
python3 ~/.codex/skills/automation-optimizer/scripts/discover_agents.py
```
Read-only. Lists every agent's installed automations, whether each carries the protocol block (and version), missing sidecars, orphaned job folders, and which canonical-instruction files exist. Run this first when asked "what do I already have."

## Workflow

1. **Discover scheduled work.**
   - For Codex jobs, prefer the Codex automation tool when creating/updating active jobs; for local audits, inspect `${CODEX_HOME:-$HOME/.codex}/automations/*/automation.toml`.
   - For Claude/other, do not assume a universal registry. Search the project and `~/.claude` for scheduled-task definitions, cron runbooks, launch agents, or named automation prompts before editing. See `reference/claude-scheduled-tasks.md`.

2. **Inventory before changing.** Record id/name, schedule, status, working dirs, environment, model, prompt length, and whether optimizer controls already exist. Preserve schedule, status, model, cwd, and project-specific safety rules unless the user explicitly asks to change them. Do not edit project repos unless the automation's source of truth lives there.

3. **Apply the optimizer contract.** Every recurring job must satisfy the contract in `reference/optimizer-contract.md`. The canonical managed block that encodes it (and that the helper injects) is in `reference/optimizer-block.md`. Sidecar state-file templates are in `reference/state-file-templates.md`.

4. **For Codex TOML, use the helper.** It injects/upgrades the versioned managed block idempotently and creates sidecar state files.
   - Dry-run audit (prints a diff + status table, changes nothing):
     `python3 ~/.codex/skills/automation-optimizer/scripts/optimize_codex_automations.py`
   - Apply (backs up each file as `.bak` first):
     `python3 ~/.codex/skills/automation-optimizer/scripts/optimize_codex_automations.py --apply`
   - Validate every active job has the block + sidecars (non-zero exit on failure):
     `python3 ~/.codex/skills/automation-optimizer/scripts/optimize_codex_automations.py --strict`

5. **For Claude / non-Codex tasks.** Reuse the same contract but edit the discovered task source directly; keep sidecar state files next to the task definition. If an external scheduler manages it, update the prompt/runbook only and report any scheduler-level change that needs approval — do not rewrite the scheduler blindly.

6. **Verify and report.** Re-parse every changed config. Confirm each active automation has the current managed block and sidecar files. Report using the shape below.

## Composer mode (proposing & scaffolding new suites)

Use when the user wants new automations rather than hardening existing ones. Full detail in `reference/composer.md`; the patterns are in `reference/pattern-library.md`; the suite is declared in `reference/suite-manifest.md`.

1. **Profile (read-only).** Detect capabilities, never assume them: default branch + protection, test runners, the "gates pass" signal, the tracker, platform capabilities (e.g. iOS simulator), security scanners, complexity signals, and any existing automations. Run the optimizer audit first so you don't propose a duplicate.

2. **Propose.** From the eight patterns, select only those whose capabilities are present and draft a `suite.toml` plus a plain-English rationale: which patterns fit, which were skipped and why, the phase ordering, and which single job holds merge authority. Flag anything that can write to `main`.

3. **Confirm once.** The user approves; write an approval record (`approved_fingerprint`) per job. This is the suite's one mandatory human gate.

4. **Autonomous after.** Each run a job recomputes its fingerprint; if it matches the approved one it runs unattended. A safety-relevant change (merge authority, scope, phase, schedule, or a gate-loosening param) flips the fingerprint and re-enters propose-confirm for just that change. Cosmetic edits and gate-tightening do not.

**Executable steps** (the composer is scripted, not just prose):

```
# 1–2. profile + propose: read-only detection → draft suite.toml + rationale
python3 ~/.codex/skills/automation-optimizer/scripts/profile_project.py profile <repo> \
        --out <repo>/.codex/automations/suite.toml
# validate the proposal is internally consistent before showing the user
python3 ~/.codex/skills/automation-optimizer/scripts/optimize_codex_automations.py --fleet <suite.toml>
# 3. confirm-once: stamp approval fingerprints after the user says yes
python3 ~/.codex/skills/automation-optimizer/scripts/profile_project.py approve --suite <suite.toml> --by <name>
# 4. install: materialize each approved job as a runnable automation.toml + sidecars
#    (dry run by default; --install writes into $CODEX_HOME; only approved jobs are built)
python3 ~/.codex/skills/automation-optimizer/scripts/scaffold_suite.py --suite <suite.toml> --install
# 5. verify + gate: every job carries the block, and nothing unapproved/stale runs
python3 ~/.codex/skills/automation-optimizer/scripts/optimize_codex_automations.py --codex-home ~/.codex --strict
python3 ~/.codex/skills/automation-optimizer/scripts/optimize_codex_automations.py --fleet <installed suite.toml> --require-approved
```

`profile` is read-only on the repo (it detects capabilities by file/tool presence and a couple of read-only `git` queries; it never runs tests/scanners and writes nothing unless you pass `--out`). `scaffold_suite.py` writes each job's prompt as the shared managed block + that pattern's adaptive body, creates sidecar state, and copies the manifest beside the jobs — that is what the Codex scheduler picks up. It refuses to build any job that isn't approved & current, and requires the explicit `--install` flag to write into `~/.codex` (creating active nightly jobs is a system change). After install, confirm the automations appear and are enabled in your scheduler.

**Principle — adaptive, not targeted:** templates discover their commands at runtime and degrade to propose-only when a capability is missing; they never hardcode or guess project commands. **Fleet rule — one merge authority:** exactly one job (the integrator) merges to `main` when any producer/janitor exists; producers hand off via branches + tracker tickets. For new jobs that can merge, start in `mode = "shadow"` for the first few runs.

The eight patterns: P1 coverage-and-quality ratchet, P2 product-value explore/fix/confirm loop, P3 repo-hygiene integrator (the sole merge authority), P4 leftover resolver, P5 collaboration meta-learner, P6 code-simplification ratchet, P7 code-security sweep (escalates high-severity findings to the approval queue), P8 dev-environment self-reflection (keeps CLAUDE.md/AGENTS.md and dev tooling current — instruction edits via the integrator, hooks/CI/settings via the approval queue).

## Lifecycle modes (managing one automation over time)

Full detail in `reference/lifecycle.md`. The front door is `scripts/lifecycle.py`; every verb defaults to a dry run and only writes with `--apply` (running `--apply` is the human confirmation gate). `--agent {codex,claude,gemini,cursor,all}` picks the target; emit-only agents (Gemini cron, Cursor cloud) print the config to apply rather than editing cron or the cloud.

```
SK=~/.codex/skills/automation-optimizer/scripts
python3 $SK/lifecycle.py setup  <repo> --agent codex --apply                       # stand up a suite
python3 $SK/lifecycle.py add    --suite <suite.toml> --pattern P7 --agent codex --apply
python3 $SK/lifecycle.py update --suite <suite.toml> --id coverage-ratchet --param coverage_floor=85 --agent codex --apply
python3 $SK/lifecycle.py remove --suite <suite.toml> --id code-security --agent codex --apply           # disable (reversible)
python3 $SK/lifecycle.py remove --suite <suite.toml> --id code-security --purge --agent codex --apply   # archive + delete
```

- **add** is capability-checked: a pattern whose capability is absent is refused with the reason, never guessed. A producer auto-wires to the existing integrator; a second integrator is rejected.
- **update** re-stamps the fingerprint; a safety-relevant change goes stale and `--apply` is the re-confirmation.
- **remove** disables by default (reversible) and refuses to retire the sole integrator while producers/janitors depend on it; `--purge` archives state then deletes.

## Safety rules

- Never deploy, publish, send external messages, change secrets/security/billing, rewrite Git history, or delete project/user data while optimizing automations.
- **Lifecycle removal is reversible by default** — `remove` disables a job and keeps its files; only `--purge` deletes, and it archives the job's memory/ledgers first. Never delete a job's state without archiving it.
- **Exactly one merge authority across agents, not just within one.** When the same suite targets multiple agents, only the designated merge agent runs an active integrator; every other agent's integrator is forced to `shadow`.
- Never store secrets, full logs, screenshots, personal data, or large dumps in automation memory — store fingerprints and metrics only.
- Treat memory as a hint, not proof. Every run re-checks live repo, tracker, tool, and environment state before acting (this is enforced by the change-detection contract item).
- Prefer updating existing tracker items over creating duplicates.
- Prefer agentic execution for non-trivial work: separate inventory, implementation, verification/review, and integration responsibilities when the tool supports subagents.
- Prefer verified integration over local-only work: a completed safe fix should usually be pushed, synced, and merged to the default branch — unless project rules, deploy risk, dirty ownership, failing checks, or approval gates say otherwise.
- Keep the managed block before task-specific instructions, so the job starts with lock/preflight/memory/change-detection/integration behavior, then applies its project rules.
- If an automation is stale, orphaned, or externally managed, classify it — do not delete it.

## Final report shape (plain English)

- **Updated:** automation ids whose prompt changed
- **Already compliant:** ids that needed no change
- **State files:** memory/ledger/queue files created
- **Skipped:** stale/orphan folders and why
- **Verification:** parser / `--strict` checks run
- **Next:** exact approval or input needed, only if blocked
