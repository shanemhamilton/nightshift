# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.1] — 2026-07-01

### Fixed
- `discover_agents.py` and the install selftest hardcoded the installed skill
  directory name as `automation-optimizer`; it is actually the source folder name
  (`nightshift` when the public repo is cloned). Discovery now derives `SKILL_NAME`
  from the repo root, so a correctly-installed skill is no longer reported as
  `(not installed)` on a `nightshift` clone, and CI passes.

## [0.7.0] — 2026-07-01

Fleet-hardening release driven by a deep audit of the live overnight fleet. The managed
protocol advances to **v5** and is now backed by deterministic helper scripts instead of
prose the model must re-derive each run, the upgrade path preserves hand-customized blocks,
the human-decision loop closes end to end, and multiple projects are governed as one fleet.

### Added
- **Deterministic state tooling.** `run_lock.py` (atomic job + workspace locks with a
  fixed owner format and safe reclaim), `run_ledger.py` (open/close verbs writing
  `last-run.md` + per-unit `runs/` entries, a machine-owned `<!-- ao:counters -->` block in
  memory, and `ESCALATE`/exit 3 at the consecutive-failure threshold), `state_schema.py`
  (canonical `---`-frontmatter for run files plus a lenient parser that also reads the legacy
  bullet and freeform shapes on the fleet), `fingerprint.py` (repo/command change detection),
  and `fleet_health.py` (read-only scan for expired locks, overdue/empty/blank/oversized jobs).
- **Protocol v5.** The managed block wires start-of-run locking to `run_lock.py`, the
  change-detection gate to `fingerprint.py`, and closeout to `run_ledger.py`; adds a
  **Blocked-worktree recovery** section (repeatedly-blocked producers work in an isolated
  `git worktree`, never touching the user's checkout), a lease-aware continuation-loop stop
  rule, target selection from a per-project `PROJECT-QUEUE.md`, and start-of-run readback of
  decisions propagated from the daily approval digest. Every released block is preserved in
  `BLOCK_HISTORY`.
- **Human-decision loop.** The approval digest gains stable per-item ids + `- [ ] approve`
  checkboxes and a `resolve` verb that applies checked/decided items back to the source queue
  (`## Resolved`) and the job's memory (`## Stable decisions`), refusing any item whose source
  changed since the digest was generated. Queue aging (`⚠ AGED`), a tightened safe bucket with
  an always-needs-judgment deny regex, and `--emit-launchd`/`--emit-cron` delivery artifacts.
- **Fleet governance.** Per-project suites under `automations/suites/<slug>.toml`; whole-fleet
  `--fleet` validation with cross-suite checks (one active merge authority per workspace;
  same-workspace same-time collision warning); `[suite].night_start_hour` for midnight-spanning
  phase order; `lifecycle.py adopt` to bring hand-written jobs under a manifest without
  rewriting their prompt; per-job agent records; `fleet_report.py` 7/30-day rollups.
- **`PROJECT-QUEUE.md`** scaffolding (objectives + open threads), `INSTALL-INFO.json` install
  stamps with drift reporting, per-pattern model/reasoning-effort hints (never Haiku), and a
  CI workflow running all four selftest suites on Python 3.11 and 3.12.

### Changed
- Upgrades are **customization-preserving**: `status_of` reports `customized-block` (checked
  before `needs-upgrade`) and `apply` refuses to strip a hand-modified block unless
  `--migrate-custom` folds the extracted rules into the task body. Customization detection is
  path-insensitive (state-file paths never read as customization).

### Fixed
- Block integrity: `malformed-block` (missing END / version outside span), `duplicate-block`,
  and `newer-than-helper` are detected; `apply` never prepends a second block.
- Gemini jobs are no longer reported as ORPHAN (per-adapter `job_file`).
- The digest resolves a job's project from the suite manifests, not the first hyphen segment.
- `cron_to_rrule` handles day-of-month and warns on unsupported cron syntax instead of
  silently degrading to daily; `security_scanner` detection requires the audit tool on PATH
  for non-npm ecosystems.

## [0.6.0] — 2026-06-29

### Added
- **P10 — documentation-sync ratchet (per-project pattern).** A new producer that keeps
  *user-facing* documentation (README, `docs/`, API reference, CHANGELOG) in sync with the code
  that has actually landed on the default branch — behavior-neutral, so it hands doc branches to
  the single integrator (P3) and merges the same night. Built for the two properties requested:
  **efficiency** — it scopes by the change-detection window (the diff since its last successful run,
  not a whole-repo re-scan), prefers regenerating from a docs generator / API-spec pipeline over
  hand-writing, runs a bounded continuation loop (`max_docsets = 5`), and no-ops when nothing it
  covers drifted; **accuracy** — code is the source of truth (a disagreeing doc is corrected to
  match the code), every symbol/path/flag/example must be grep-verified to exist before it is
  written (no fabrication), and a detectable docs build / link-check must pass before handoff
  (`require_doc_build = true`). Capability-gated on a documentation surface (README, `docs/`, API
  spec, or a docs generator); absent → propose-only. Clear boundary with P8: P10 owns user-facing
  docs, P8 owns the agent-instruction files; P10's write scope excludes those and leaves inline
  docstrings out unless `include_inline_docstrings` is set.
  - Wired through `pattern_bodies.py` (body + defaults), `profile_project.py` (docs-surface
    detection + selection), `optimize_codex_automations.py` (`KNOWN_TEMPLATES`), the references
    (`pattern-library.md`, `composer.md`, `suite-manifest.md`), `SKILL.md`, and a new self-test
    assertion. `selftest_lifecycle.py`: 26/26 pass.

## [0.5.0] — 2026-06-29

Hardening pass driven by an overnight fleet audit (lock collisions, schedule drift, near-zero
merges to main, scattered approvals).

### Added
- **P9 — cross-project approval digest (fleet-global pattern).** A new ninth pattern plus its
  deterministic engine `scripts/approval_digest.py`. It reads every automation's
  `human-approval.md` across all agents (read-only), dedupes, ranks by age, filters self-resolved
  closure notes, and buckets items into "safe to batch-approve" vs "needs judgment" (unknown risk
  stays in needs-judgment), writing one `~/.codex/DAILY-APPROVALS.md`. Optional local macOS
  notification; external email/Slack stays OFF unless a destination is configured **and**
  `AO_DIGEST_EXTERNAL_OPTIN=1`. Installed once for the whole fleet, not per project.
- **Structured approval-queue items.** `human-approval.md` items now carry `risk` /
  `suggested_default` / `action` / `first_seen` / `evidence`, so the digest can bucket and pre-fill
  them.

### Changed
- **Atomic lock acquisition (managed protocol v3 → v4).** Start-of-run step 1 went from
  check-then-replace (which let two same-minute runs each declare the other "stale" and run in
  parallel) to **atomic acquire-or-defer**: create the lock with an operation that fails if it
  exists (`mkdir` / `O_EXCL` / noclobber), record a run token + PID + `lease_until`, **defer** (no-op)
  when the lock is held, and reclaim only a provably abandoned lock (dead PID **and** expired lease)
  with a read-back ownership check. Release only a lock that still holds this run's token.
- **Scope-gated safe-merge lane.** The integrator may auto-merge to the default branch only when
  gates pass, the diff stays within the producer's `write_scope`, and it touches no
  production-config/secrets/migration/deploy/CI/auth/billing/external surface. Agent-local tool
  metadata (`.serena/`, `.beads/issues.jsonl`) no longer counts as a dirty-worktree blocker, and
  screenshot/visual proof is required only for user-visible UI changes — removing the two false
  blockers that stranded safe work overnight.
- **Schedule de-clustering (generator).** `profile_project.py` now staggers cron minutes per job
  (`base(project) + i*13 mod 60`) so jobs touching the same repos never share a trigger minute;
  phase hours are preserved so fleet phase-ordering still holds. Existing installed jobs keep their
  schedules until regenerated (the v4 lock makes their current collisions defer safely).
- Reference docs updated in lockstep: `optimizer-contract.md` (items 4/18/20/21),
  `state-file-templates.md`, `pattern-library.md` (P9 + compose notes), `suite-manifest.md`
  (P1..P9), and `SKILL.md`.

## [0.4.1] — 2026-06-29

### Added
- **Continuation loop (managed protocol v2 → v3).** Operators finish a unit of work fast and then sat
  idle for the rest of the night. The managed block now has a `Continuation loop` section: after a unit
  closes out safely, the operator re-reads live state, fingerprint-dedupes against the work it just did,
  and picks the next highest-value unblocked target — looping until a stop rule fires (per-run unit
  budget reached, priority queue drained, two consecutive units fail/block, an approval boundary, or
  self-ping-pong detection). The start-of-run change-detection no-op gate is unchanged. Generated via
  `managed_block()` in `scripts/optimize_codex_automations.py`, so it reaches Codex, Claude, Gemini, and
  Cursor through one source; existing v2 Codex automations show `needs-upgrade` and re-injection lands v3.
- **Per-pattern run budgets.** `pattern_bodies.py` adds `max_units = 5` to P1 (coverage), P4 (leftovers),
  and P7 (safe security fixes), and reframes their bodies to loop until the budget or an early-stop
  condition. P3 (integrator) now explicitly *drains its whole handoff queue* so a long producer night
  gets integrated (or finished next integrator run). P2 (`max_loops`) and P6 (`max_changesets`) keep
  their existing caps. Reflectors P5/P8 keep their low edit caps on purpose — for them "use the night"
  means reviewing more signals, not making more edits. `profile_project.py` emits the new `max_units`
  params explicitly in generated suites.
- **Ledger fields.** `last-run.md` now records `units_completed` and `stop_reason`.

### Changed
- Reference docs updated in lockstep: `optimizer-contract.md` (AO-11/AO-12), `pattern-library.md`,
  `state-file-templates.md`, `suite-manifest.md`, `optimizer-block.md`, plus `SKILL.md`/`README.md`
  feature lists.

## [0.4.0] — 2026-06-28

### Changed
- **Project-scoped automation ids and display names.** Codex/Claude/Gemini registries are global
  (`~/.codex/automations/<id>/`), so the composer's old generic ids (`product-value-loop`) made two
  projects collide and overwrite each other. `profile_project.py` now namespaces every job id with a
  slug of `[suite].project` (`SkinCrafter` → `skincrafter-product-value-loop`) and stamps a human
  `name` (`SkinCrafter Product Value Loop`); producer `hands_off_to` is rewritten in lockstep.
  `lifecycle.py setup`/`add` apply the same namespacing. Ids/names stay outside the approval
  fingerprint, so this never re-triggers confirmation.
- **Name fallback is project-aware.** `agent_materializers.py` (used by `scaffold_suite.py` and
  `lifecycle.py`) defaults a job's registry name to `"{project} {pattern title}"` when no explicit
  `name` is set, instead of a bare id-derived title.
- **Discovery surfaces project/workspace/name.** `discover_agents.py` reads the installed
  `automation.toml` (and any co-located `suite.toml`) and prints `project=/ws=/name=` next to each
  job — flagging metadata-less generic or stale jobs with `(no suite metadata)`.
- **Install summary prints display names.** `scaffold_suite.py` leads each line with the
  user-visible name and lists names (not just ids) in its install summary.

### Added
- **`scripts/naming.py`** — single source for `slugify` / `namespace_id` / `base_title` /
  `display_name`, imported across the composer, materializer, discovery, and lifecycle scripts so the
  id/name rules never drift.

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
