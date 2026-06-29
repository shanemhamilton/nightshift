# Suite Manifest (`suite.toml`)

One manifest per project declares the automation suite and how its members work together. It is the single artifact the fleet validator checks. It lives at `${CODEX_HOME}/automations/suite.toml` (or per-workspace), beside the individual `automation.toml` jobs it references.

The manifest is *declarative*: it does not contain prompts. Each `[[job]]` points at a template in `pattern-library.md`; the actual prompt for that job (managed block + adaptive body) lives in its own `automation.toml` and is hardened by the optimizer helper as usual.

**Job ids are project-scoped.** Codex/Claude/Gemini registries are global (a Codex job lives at `~/.codex/automations/<id>/`), so the composer namespaces every `id` with a slug of `[suite].project` (`MySkinIQ` → `myskiniq-coverage-ratchet`) to keep two projects' suites from colliding on the same directory. Each job also carries a human `name` (`MySkinIQ Coverage Ratchet`) used as the registry display name. Both `id` and `name` are deliberately *outside* the approval fingerprint, so renaming/namespacing never re-triggers confirmation. A producer's `hands_off_to` references the integrator's namespaced id.

## Format

```toml
[suite]
project = "MySkinIQ"
state_dir = "state"               # shared run state, relative to each job dir
nightly_budget_minutes = 180      # global cap across the suite
quiet_hours = "00:00-06:00"       # optional; informational

[[job]]
id = "myskiniq-coverage-ratchet"  # project-scoped: "<slug>-<pattern>"
name = "MySkinIQ Coverage Ratchet"  # registry display name
template = "P1"                   # must be a known pattern id (P1..P8)
template_version = 2
phase = "producer"                # producer | integrator | janitor | reflector
merge_authority = false
schedule = "0 1 * * *"            # standard 5-field cron
write_scope = ["tests/**", "backend/**", "ios/**"]
hands_off_to = "myskiniq-repo-hygiene"  # required for producers: the integrator's id
mode = "active"                   # active | shadow
params = { coverage_floor = 75, quality_mode_when_met = true }
# approval record (written by the composer's confirm step)
approved_by = "shane"
approved_at = "2026-06-28T21:40:00Z"
approved_fingerprint = "ao1:9f3c1a2b4d5e"

[[job]]
id = "myskiniq-product-value-loop"
name = "MySkinIQ Product Value Loop"
template = "P2"
phase = "producer"
merge_authority = false
schedule = "0 1 * * *"
write_scope = ["ios/**"]
hands_off_to = "myskiniq-repo-hygiene"
mode = "shadow"
params = { max_loops = 10 }

[[job]]
id = "myskiniq-repo-hygiene"
name = "MySkinIQ Repo Hygiene"
template = "P3"
phase = "integrator"
merge_authority = true            # EXACTLY ONE job in the suite may set this
schedule = "0 3 * * *"
write_scope = ["**"]
params = { clean_after = true }

[[job]]
id = "myskiniq-leftover-resolver"
name = "MySkinIQ Leftover Resolver"
template = "P4"
phase = "janitor"
merge_authority = false
schedule = "0 4 * * *"
write_scope = ["**"]
params = { max_units = 5 }          # continuation-loop budget: resolve up to 5 leftovers/run

[[job]]
id = "myskiniq-collab-meta-learner"
name = "MySkinIQ Collab Meta Learner"
template = "P5"
phase = "reflector"
merge_authority = false
schedule = "0 5 * * *"
write_scope = ["AGENTS.md", "**/memory.md"]
params = { lookback_hours = 24 }

[[job]]
id = "myskiniq-devenv-reflector"
name = "MySkinIQ Devenv Reflector"
template = "P8"
phase = "reflector"
merge_authority = false
schedule = "0 5 * * *"
write_scope = ["CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursor/rules", ".claude/**", "**/memory.md"]
params = { lookback_hours = 24, max_edits = 3 }
```

## Fleet rules (enforced by `optimize_codex_automations.py --fleet`)

A manifest is valid only if all of these hold:

1. **Single merge authority.** At most one `[[job]]` has `merge_authority = true`, and it is required whenever any producer or janitor is present — that's the rule that prevents overnight merge races. More than one is always an error. Zero is allowed only for a minimal suite with no producers or janitors (e.g. a reflector-only suite on a non-git project).
2. **Merge authority is the integrator.** Any `merge_authority = true` job must have `phase = "integrator"`, and every `integrator` must hold merge authority.
3. **Producers hand off.** Every `producer` has `merge_authority = false` and a `hands_off_to` that names an existing integrator job.
4. **Consumers exist.** If any producer exists, an integrator must exist. If a janitor exists, an integrator must exist (the janitor depends on it).
5. **Phase ordering.** Schedules must run in DAG order: producer ≤ integrator ≤ janitor ≤ reflector, by cron hour. (Unparseable cron → ordering check skipped with a note.)
6. **Known templates.** Every `job.template` is one of P1..P8.
7. **Unique ids.** Job ids are unique within the suite.

## Approval / fingerprint reporting

For each active job the validator computes the expected fingerprint from its safety-relevant fields (`template`, `template_version`, `merge_authority`, sorted `write_scope`, `phase`, `schedule`, `params`) and compares it to `approved_fingerprint`:

- **approved & current** — fingerprint matches → the job may run autonomously.
- **approved but stale** — a record exists but the config changed since approval → the job must re-enter propose-confirm for the change (runtime job blocks itself; validator flags it).
- **pending** — no `approved_fingerprint` → never confirmed yet.

By default `--fleet` exits non-zero only on the structural rules (1–7) and *reports* approval status. Add `--require-approved` to also fail when any active job is pending or stale — useful as a pre-run gate so nothing unapproved or silently-changed runs unattended.

> Manifest-level fingerprints include the whole `params` table for simplicity (any change re-confirms). The finer "only a *gate-loosening* change re-confirms; tightening is free" nuance from `composer.md` is applied by the running job itself, which understands which knob loosens a gate. Manifest-level is therefore the *stricter* of the two — safe by construction.
