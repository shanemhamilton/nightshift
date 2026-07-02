# Project Queue

`PROJECT-QUEUE.md` is the cross-run home for a project's objectives and open threads —
the thing automations read at start-of-run and update at closeout, so intent and
in-flight work survive between runs even when a job's own memory doesn't cover it.
Protocol v5 reads this file **before** the private priority queue, so it's the first
place a job looks to understand what this project's automations should be driving
toward and what's already being tracked.

**Location:** `<workspace>/.codex/automations/PROJECT-QUEUE.md` — one per project,
scoped by workspace like the rest of `.codex/automations/`, not per-job.

## The two sections

### Objectives
Human-editable. What this project's automations should drive toward, one bullet
each. Automations read this for context but do not rewrite it — it's the human's
steering wheel, not a job's scratchpad.

### Open threads
Machine-maintained by automations (escalation/closeout paths write here, not
humans). One `### <thread-id>` block per open thread, with fields:
- `owner_job` — the job id responsible for this thread
- `status` — `open` | `parked` | `resolved`
- `next_action` — the concrete next step
- `first_seen` — ISO date the thread was opened

## Ownership

Objectives = human. Open threads = machine. Don't cross the streams: a job
proposing a new objective belongs in the approval queue (`reference/state-file-templates.md`),
not a direct edit here; a human closing out a thread should flip its `status`
rather than deleting the block, so the history stays legible.

## Scaffolding

`profile_project.py profile --out <suite.toml>` and `lifecycle.py setup --apply`
both scaffold `PROJECT-QUEUE.md` the first time they materialize a suite into a
workspace (`profile_project.scaffold_project_queue`). Scaffolding **never
overwrites an existing file** — if `PROJECT-QUEUE.md` is already there, re-running
setup/profile leaves it untouched, so accumulated objectives and open threads are
safe across re-profiles.
