# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/shanemhamilton/nightshift/releases/tag/v0.1.0
