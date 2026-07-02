# Composer Mode

The optimizer *hardens existing* automations. The composer *proposes and scaffolds new* ones — a coherent, coordinated suite per project, drawn from the adaptive `pattern-library.md`, wired together by a `suite-manifest.md`. New jobs are born carrying the managed block, so they are compliant from day one.

Composer mode runs when the user asks to add automations, set up a nightly suite, or "what automations should this project have." It never edits a repo's code; it writes automation definitions + sidecar state + a suite manifest.

The lifecycle below is implemented by `scripts/profile_project.py` (`profile` and `approve` subcommands), validated by `scripts/optimize_codex_automations.py --fleet`. The script is the source of truth for the mechanics; this doc explains the intent.

> To manage a single job after a suite exists — add one pattern, update a knob, or retire a job on any agent — use the lifecycle verbs in `reference/lifecycle.md` (`scripts/lifecycle.py setup|add|remove|update`). `setup` and `add` are the composer's propose→confirm→install lifecycle wrapped as one command per scope.

`setup`/`profile` also scaffold a per-project `PROJECT-QUEUE.md` the first time they materialize a suite into a workspace — see `reference/project-queue.md` for the format and ownership rules.

## Principle: adaptive, not targeted

Templates do **not** hardcode project commands. Each job discovers what it needs at runtime and adapts scope to what it finds. A template that can't find a required capability degrades to **propose-only** and records why — it never guesses a command. This keeps one template working across a backend repo, an iOS app, and a mixed workspace without per-project rewrites. The hardcoded specifics a job *learns* over time live in its memory and (durably) in the project's `AGENTS.md`, not in the template.

## Lifecycle: profile → propose → confirm-once → autonomous

### 1. Profile (capability detection, read-only)
Detect, never assume. Record findings as evidence in the proposal:
- **Default branch + protection** — `git symbolic-ref refs/remotes/origin/HEAD`; note required reviews/checks.
- **Test runners** — presence of `package.json`/`Package.swift`/`pyproject.toml`/`Gemfile`/`pom.xml`/`go.mod`; map to the runner each implies.
- **CI gate** — `.github/workflows/*`, required status checks; capture the check name(s) that mean "gates pass."
- **Tracker** — `bd`/Beads CLI on PATH, or other configured tracker; this is the handoff currency.
- **Platform capabilities** — `xcrun simctl list` for an iOS simulator; container/build tooling, etc.
- **Security scanners (for P7)** — dependency audit (`npm audit`/`pip-audit`/`bundler-audit`/`govulncheck`), secret scan (`gitleaks`/`trufflehog`), SAST (`semgrep`/CodeQL). No scanner → P7 is proposed in propose-only mode with a recommendation to enable one.
- **Complexity signals (for P6)** — linters, duplication/dead-code reports, large-function metrics; plus the test safety net P6 needs to refactor behavior-preservingly.
- **Instruction files + dev-env config (for P8)** — the canonical instruction file for this agent (`CLAUDE.md`/`AGENTS.md`/`GEMINI.md`/`.cursor/rules`) and nearby execution-shaping config (hooks, `.github/workflows/*`, lint/format/editorconfig, settings/permissions). P8 is always proposable; instruction edits route through the integrator, config changes through the approval queue.
- **Documentation surface (for P10)** — a `README`, a `docs/` tree, an API spec (`openapi`/`swagger`), or a docs generator (`mkdocs`/`docusaurus`/`typedoc`/sphinx/`mdbook`). Present → P10 keeps it in sync with merged code (change-scoped, generator-first, verify-before-write). Absent → P10 is propose-only with a recommendation to add a docs surface. P10 owns *user-facing* docs; the agent-instruction files stay P8's.
- **Existing automations** — run the optimizer audit so the composer doesn't propose a duplicate of something already present.

### 2. Propose (evidence-based, no writes to active config)
Emit a draft `suite.toml` plus a plain-English rationale: which patterns fit, which were skipped and why (e.g., "product-value-loop skipped — no iOS simulator detected"), the phase ordering, and which single job holds merge authority. Anything proposed that can write to `main` is flagged.

Every proposed job id is **project-scoped** — `profile_project.py` prefixes it with a slug of `[suite].project` (`SkinCrafter` → `skincrafter-product-value-loop`) and stamps a human `name` (`SkinCrafter Product Value Loop`) — because Codex/Claude/Gemini automation registries are global and generic ids would collide or overwrite across projects. Producer `hands_off_to` is namespaced in lockstep; id/name stay outside the approval fingerprint so this never re-triggers confirmation.

### 3. Confirm-once (human gate, first time only)
The user approves the proposal. On approval, write an **approval record** into the manifest for each job:
```toml
approved_by = "shane"
approved_at = "2026-06-28T21:40:00Z"
approved_fingerprint = "ao1:9f3c…"   # see fingerprint rule below
```
This is the only mandatory human gate for the suite as a whole.

### 4. Autonomous (every run after)
On each run a job recomputes its fingerprint and compares to `approved_fingerprint`:
- **Match** → run autonomously, no prompt.
- **No record or changed fingerprint** → do not run autonomously. Emit a proposal to `state/approval-queue.md` describing exactly what changed, and wait for re-confirmation of *that change only*.

## Fingerprint rule (what forces re-confirmation)

The fingerprint is a hash of the **safety-relevant, normalized** fields of a job — not its full prose. It covers:
- template id + template version
- `merge_authority` (true/false)
- `write_scope` (paths/areas the job may modify)
- `phase` (producer / integrator / janitor / reflector)
- schedule phase/window
- any threshold that **loosens** a gate (e.g., lowering a required coverage floor, raising max loops, widening merge conditions)

It deliberately ignores cosmetic edits (wording, comments, tightening a gate) so routine upkeep doesn't nag. Net effect: "confirmed = autonomous" holds, but a job can never silently acquire merge power, wider scope, or a looser gate than the human approved. Tightening is always allowed without re-confirmation.

## Optional: shadow rollout

For any job that can merge to `main`, the composer can set `mode = "shadow"` for the first N runs. In shadow mode the job does everything except the irreversible step — it writes what it *would* merge/push to the approval queue. Promote to `mode = "active"` after the shadow runs look right. Recommended default for new integrators; optional for read-only producers.

## Safety posture (composer-specific)

- Profiling is read-only; proposing writes only draft automation/manifest files, never repo code.
- The composer never grants merge authority to more than one job (the fleet check enforces this).
- A capability the profiler can't confirm becomes a *skipped* pattern with a reason, not a guessed command.
- Re-confirmation is required for any safety-relevant change, per the fingerprint rule above.
