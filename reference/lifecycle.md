# Lifecycle Modes (`setup` / `add` / `remove` / `update`)

The optimizer hardens existing jobs and the composer stands up a whole suite. The **lifecycle**
verbs sit on top of both so a person can manage a *single* automation across its whole life — on any
agent — without hand-editing a manifest or a registry file.

All four verbs are the same pipeline:

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
