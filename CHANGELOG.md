# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-06-28

### Added
- **Lifecycle modes** — a new `scripts/lifecycle.py` front door with four verbs so a person can
  manage a single automation across its whole life, on any agent:
  - `setup` — profile a project and stand up its whole suite (propose → confirm → install).
  - `add` — add one pattern (P1..P8) to an existing suite, capability-checked and fleet-validated.
  - `remove` — retire a job: **disable by default** (reversible), `--purge` archives sidecar state
    then deletes the registry dir. Honors the "classify, don't delete" safety posture.
  - `update` — change a job's schedule / params / scope / mode / model; a safety-relevant change
    re-enters the approval gate (the fingerprint goes stale until re-confirmed).
- **All four agents executable.** Lifecycle materializes through each adapter's `scheduler`:
  `native_file` (Codex, Claude) writes the registry files directly; `external_cron` (Gemini) writes
  the prompt and **emits** the crontab line; `cloud_api` (Cursor) **emits** the config — never
  editing cron or touching the cloud. New `scripts/agent_materializers.py` holds the per-agent
  write/disable/purge logic plus the shared template bodies (extracted from `scaffold_suite.py`, so
  the two share one copy and never drift).
- **Cross-agent merge-authority guard.** When the same suite targets multiple agents, only the
  designated merge agent keeps an active integrator; the others get it in `shadow` mode, preserving
  "exactly one merge authority" across agents, not just within one.
- **`scripts/selftest_lifecycle.py`** — end-to-end checks for every verb and scheduler type.

### Changed
- `scaffold_suite.py` now imports its template bodies / Codex emitter from `agent_materializers.py`.
  Output is byte-identical to v0.2.0 (verified by diff).

## [0.2.0] — 2026-06-28

### Added
- **P8 — dev-environment self-reflection** pattern (reflector phase). A daily routine that
  reviews the day's signals (commits, PRs, CI results, review comments, run ledgers) and keeps
  the agent's environment current: surgical edits to the canonical instruction file
  (`CLAUDE.md`/`AGENTS.md`/`GEMINI.md`/`.cursor/rules`) routed through the integrator's gate, and
  execution-shaping config (hooks, CI, lint/format, settings/permissions, new skills) proposed to
  the human-approval queue rather than edited directly. Composed automatically alongside P5; both
  reflectors bias hard toward no change. The pattern library is now 8 patterns (P1..P8).

## [0.1.0] — 2026-06-28

Initial public release.

### Added
- **Optimizer mode** — injects a versioned operating protocol into existing recurring
  automations idempotently and with backups: persistent memory, append-only run ledger,
  concurrency lock, tool preflight, change-detection short-circuit, idempotency guarantee,
  retry/backoff, cost/runtime budget, failure taxonomy, stop rules, multi-agent execution,
  human-approval queue, and verified safe-merge closeout.
- **Composer mode** — profiles a project and scaffolds a coordinated suite from an adaptive
  7-pattern library (coverage ratchet, product-value loop, repo-hygiene integrator, leftover
  resolver, collaboration meta-learner, code-simplification, code-security), wired with exactly
  one merge authority. Approval-gated install.
- **Discovery mode** — read-only cross-agent inventory of installed automations with
  protocol-compliance flags.
- Cross-agent support for Codex CLI, Claude Code, Gemini CLI, and Cursor via capability-driven
  adapters that degrade gracefully instead of guessing commands.
- Self-installer (`scripts/install_skill.py`) that detects each agent and copies the skill in.
- Reference documentation: optimizer contract, pattern library, suite manifest, agent adapters,
  state-file templates, and examples.
- Project landing page and social-preview card under `docs/`.

[0.2.0]: https://github.com/shanemhamilton/nightshift/releases/tag/v0.2.0
[0.1.0]: https://github.com/shanemhamilton/nightshift/releases/tag/v0.1.0
