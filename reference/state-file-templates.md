> **Superseded — read this first.** The live convention places sidecars at the **job root**, not in a `state/` subfolder, with these names: `memory.md`, `last-run.md`, `priority-queue.md`, `human-approval.md`, `baseline-failures.md`, `runs/`, and lock `.automation.lock`. The optimizer (`scripts/optimize_codex_automations.py`, `SIDECARS`/`SIDECAR_DIRS`) creates exactly these. The `state/`-subfolder layout and the names `known-failures.md` / `approval-queue.md` below are the earlier design and are kept only for rationale.

# Sidecar State-File Templates

State lives in a `state/` folder next to each automation definition, so one job's state never collides with another's. The helper creates these on `--apply` if missing and never overwrites an existing one.

```
<automation-dir>/
  automation.toml          # (Codex) or the Claude task / runbook file
  state/
    .lock                  # presence = a run is in progress (AO-04)
    memory.md              # fingerprints + prior decisions (AO-01)
    last-run.md            # latest run summary (AO-02/03)
    known-failures.md      # baseline failures to ignore (AO-09)
    approval-queue.md      # unsafe actions awaiting a human (AO-18)
    runs/                  # dated per-run ledger entries (AO-02)
      2026-06-28T0600.md
```

---

## memory.md
```markdown
# Memory — <automation id>
Treat as a hint, never as proof. Re-check live state every run.

## Watched fingerprints (updated each run)
- repo_head: <sha>
- open_tracker_items: <count or id-hash>
- inputs_hash: <hash of relevant inputs>
- last_success: <ISO timestamp>

## Stable decisions
- <date> — <decision and why> (e.g., "ignore lint rule X in vendor/ — owner approved")

## Consecutive failures
- count: 0
```

## last-run.md
Canonical shape is a `---`-delimited front-matter block, then free prose notes. Parsed by `scripts/state_schema.py` (`parse_last_run`), which also degrades gracefully across two older on-disk shapes still found on the live fleet (see below) — schema="frontmatter" | "template" | "legacy".

```markdown
---
when: <ISO timestamp>
outcome: success | no-op | partial | failed
units_completed: <int>              # bounded units finished this run (continuation loop)
stop_reason: <budget | queue-drained | repeated-failure | approval-boundary | no-op>
failure_class: <none | transient | config | baseline | blocked | needs-human>
runtime_s: <int>
merged_shas: [<sha>, <sha>]
branches: [<branch>]
tracker_ids: [<id>]
---
<free prose notes>
```

Field list (`state_schema.SCHEMA_FIELDS`): `when`, `outcome`, `units_completed` (int), `stop_reason`, `failure_class`, `runtime_s` (int), `merged_shas` (list), `branches` (list), `tracker_ids` (list).

Older shapes the parser also reads (never write these, only read):
- **template** — the earlier `SIDECARS["last-run.md"]` bullet form: `# Last run` header then `- key: value` bullets (e.g. `- outcome: success`, `- rollback: n/a`). Recognized fields map onto the same names; unknown bullets (like `rollback`) are ignored; `notes:` bullets become prose.
- **legacy** — freeform prose with no recognizable fields at all. The parser returns `{schema: "legacy", raw: <full text>, ...blank fields}` and never raises.

`parse_last_run(path)` returns `None` only if the file does not exist; otherwise it always returns a dict. `render_frontmatter(record, prose)` writes the canonical NEW shape back out.

## runs/<timestamp>.md
Same front-matter shape and parser (`parse_run_entry`) as `last-run.md` above — one entry per dated run.

## known-failures.md
```markdown
# Known / baseline failures — <automation id>
Do not re-report or auto-fix these.
- <test or check id> — <why it's expected> — <date noted>
```

## human-approval.md  (live name; was `approval-queue.md`)
```markdown
# Human approval queue — <automation id>
Unsafe actions awaiting a human. Nothing here is auto-executed.

## <one-line ask>
- risk: low|medium|high
- suggested_default: <what you would do absent other input>
- action: <exact command / branch / ticket id to act on>
- first_seen: <ISO date>
- evidence: <ids / paths, never secrets>
```
The structured fields let the cross-project daily digest (pattern **P9**, `approval_digest.py`) aggregate every project's pending decisions into one `~/.codex/DAILY-APPROVALS.md`, bucketed safe-to-batch vs needs-judgment.
