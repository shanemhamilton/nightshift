# Lifecycle Modes (`setup` / `add` / `remove` / `update` / `adopt`)

The optimizer hardens existing jobs and the composer stands up a whole suite. The **lifecycle**
verbs sit on top of both so a person can manage a *single* automation across its whole life — on any
agent — without hand-editing a manifest or a registry file.

All five verbs are the same pipeline:

> **mutate the `suite.toml` manifest → validate the fleet → gate on approval → materialize into each
> target agent's registry.**

The manifest is the agent-neutral source of truth. `--agent {codex,claude,gemini,cursor,all}` chooses
where jobs land, resolved through `scripts/agent_adapters.py`. Implemented in `scripts/lifecycle.py`,
validated by `scripts/optimize_codex_automations.py --fleet`, materialized by
`scripts/agent_materializers.py`.

**Default is a dry run that writes nothing.** `--apply` persists the manifest edit, stamps the
approval fingerprint for the affected job, and materializes it — running `--apply` *is* the
confirmation gate.

## The verbs

```bash
SK=~/.codex/skills/automation-optimizer/scripts

# setup — profile a project and stand up its suite
python3 $SK/lifecycle.py setup <repo> --agent codex            # dry run: propose
python3 $SK/lifecycle.py setup <repo> --agent codex --apply    # confirm + install

# add — add one pattern to an existing suite (capability-checked)
python3 $SK/lifecycle.py add --suite <suite.toml> --pattern P7 --agent codex
python3 $SK/lifecycle.py add --suite <suite.toml> --pattern P7 --agent codex --apply

# update — change schedule / params / scope / mode / model, re-gated
python3 $SK/lifecycle.py update --suite <suite.toml> --id myapp-coverage-ratchet \
        --param coverage_floor=85 --agent codex --apply

# remove — disable (reversible) by default; --purge to archive + delete
python3 $SK/lifecycle.py remove --suite <suite.toml> --id myapp-code-security --agent codex --apply
python3 $SK/lifecycle.py remove --suite <suite.toml> --id myapp-code-security --purge --agent codex --apply

# adopt — bring an existing LIVE job under manifest/governance, without touching its prompt
python3 $SK/lifecycle.py adopt --job-id myapp-nightly-cleanup --suite myapp \
        --phase janitor --template P4 --apply
python3 $SK/lifecycle.py adopt --job-id legacy-bespoke-job --suite myapp \
        --phase producer --template custom --scope 'src/**' --apply
```

Job ids passed to `update` / `remove` are **project-scoped** (`<project-slug>-<pattern>`, e.g.
`myapp-coverage-ratchet`) — `setup`/`add` namespace them automatically so the same suite in two
projects never collides in the global registries. `add` namespaces the candidate id too (override
with `--id`). Run `discover_agents.py` to list the exact installed ids, names, and workspaces.

- **`add`** runs read-only capability detection (`profile_project.detect`) on the workspace. If the
  pattern's capability is missing (e.g. P2 with no UI driver) it refuses with the reason rather than
  guessing a command. Adding a producer auto-wires `hands_off_to` to the suite's existing integrator;
  adding a second integrator is rejected by the fleet check.
- **`update`** recomputes the job's fingerprint. A safety-relevant change (scope, schedule,
  merge authority, phase, or a gate-loosening param) makes it **stale** and requires re-approval;
  `--apply` is that re-confirmation. Cosmetic changes that don't move the fingerprint don't nag.
- **`remove`** refuses to retire the sole integrator while any producer/janitor still depends on it
  (pass `--reassign <integrator-id>` after adding another). Disable is reversible; `--purge` archives
  the job's memory/ledgers to `<root>/.archive/<id>-<UTC>/` before deleting the registry dir. It never
  touches the project repo.
- **`adopt`** brings an already-running LIVE job (installed outside lifecycle.py, or predating it)
  under manifest governance without re-materializing or rewriting it. It reads the live Codex
  `automation.toml` (workspace from `cwds[0]`, schedule recovered from `rrule` via `rrule_to_cron`),
  drafts a manifest `[[job]]` entry, validates the fleet, and on `--apply` writes the manifest and
  stamps approval — the live job's `prompt` is never read for rewriting and never touched. `--suite`
  accepts either an existing manifest path or a project slug (resolved/created under
  `<codex-home>/automations/suites/<slug>.toml`). Use `--template P1`..`P10` for a known pattern (sets
  `template_version`) or `--template custom` for a bespoke job the optimizer doesn't own a body for
  (sets a `prompt_hash` drift marker instead — a sha256 prefix of the live prompt — so a later change
  to the live prompt is detectable, without lifecycle.py ever claiming to know that prompt's contents).
  Adopting a second active merge-authority job for a workspace that already has one is rejected by the
  same fleet rule `setup`/`add` are subject to. `adopt` does not accept `--scope` per template default;
  pass `--scope` explicitly (repeatable) or the job's `write_scope` stays empty.

### Agent records on jobs

Every verb that materializes a job now records which agents it targeted as `job["agents"]` in the
manifest (e.g. `["claude"]`, `["codex", "claude"]` for `--agent all`). This field is intentionally
outside `FINGERPRINT_FIELDS` — recording or changing it never invalidates a job's approval fingerprint.
It exists to fix a duplication hazard: without it, `update`/`remove` had no way to know which agent(s)
a job actually lives on, so they always defaulted to materializing/retiring against Codex — silently
re-installing (or leaving stale) a claude-only or gemini-only job's counterpart on codex.

Resolution per verb:
- **`setup` / `add`**: `--agent` defaults to `codex` when omitted; the resolved agent list is recorded
  on the job and used to materialize.
- **`update`**: pass `--agent` to retarget (records the new list and re-materializes only there);
  omit it and the job's previously recorded `agents` are reused (falling back to `["codex"]` for
  jobs that predate this field) — the manifest's `agents` field is *not* rewritten when defaulting.
- **`remove`**: same resolution as `update` — `--agent` overrides and targets are recorded jobs'
  `agents`, else `["codex"]`. Retires the job only where it was actually installed.
- **`adopt`**: records `agents = targets(--agent or "codex")` (the live job it adopts is, by
  definition, a Codex job) but does not re-materialize.

### `setup` output location

When neither `--suite` nor `--local` is given, `setup` now writes to the same suites/ directory the
composer and `adopt` use: `<codex-home>/automations/suites/<project-slug>.toml` (via
`OPT.suite_manifest_path`), not `<repo>/suite.toml`. Pass `--local` to write to
`<repo>/.codex/automations/suite.toml` instead (a per-repo manifest, useful when the repo itself should
carry its own suite file rather than the shared codex-home registry). An explicit `--suite PATH` always
wins over both. The dry-run message prints whichever path was resolved.

## Per-agent materialization

| scheduler | agents | materialize | disable (default `remove`) | purge (`--purge`) |
|---|---|---|---|---|
| `native_file` + status field | Codex | write `automation.toml` + sidecars | set `status = "disabled"` in the toml | archive sidecars → delete dir |
| `native_file`, no status field | Claude | write `<name>/SKILL.md` + sidecars | relocate `<root>/<name>` → `<root>/.disabled/<name>` (daemon globs `*/SKILL.md`; a dotted dir is skipped) | archive → delete dir |
| `external_cron` | Gemini | write `<id>/prompt.md` + sidecars; **emit** the crontab line | **emit** the removal line; keep files | archive → delete local dir |
| `cloud_api` | Cursor | **emit** the cloud config | **emit** dashboard/API disable step | **emit** delete step |

Emit-only agents return text for you to apply, preserving the never-edit-cron / never-touch-cloud
rules in `agent_adapters.py`. For Claude, cadence is owned by the scheduler daemon — the materializer
writes the task file and reports the schedule to set via `mcp__scheduled-tasks__update_scheduled_task`.

## Cross-agent merge authority

The "exactly one merge authority" fleet rule is per-manifest. Materializing a suite that contains an
integrator onto **multiple** agents for the **same** workspace would create several integrators all
merging to the default branch. The lifecycle guard keeps the integrator **active on only the
designated merge agent** (the first in `--agent all` order) and forces it to `mode = "shadow"` on every
other agent. Shadow integrators do everything except the irreversible merge — they write what they
*would* merge to the approval queue.

## Verification

`scripts/selftest_lifecycle.py` builds a throwaway workspace and agent homes under a temp dir,
exercises every verb across the scheduler types, and pipes the results through the real `--fleet` and
`--strict` validators. Run it after any change to the lifecycle or materializer code:

```bash
python3 scripts/selftest_lifecycle.py   # exit 0 = all pass
```
