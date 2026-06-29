# Automation Optimizer & Composer

A skill that makes recurring AI-agent automations behave like reliable operators instead of stateless prompts — and composes coherent new automation suites per project — across **Codex CLI, Claude Code, Gemini CLI, and Cursor**.

It does three things:

- **Optimize** existing recurring automations: inject a versioned operating protocol (persistent memory, run ledger, concurrency lock, tool preflight, change-detection, failure taxonomy, stop rules, multi-agent execution, human-approval queue, verified safe-merge closeout), idempotently and with backups.
- **Compose** new automation suites from an adaptive 8-pattern library (coverage ratchet, product-value loop, repo-hygiene integrator, leftover resolver, collaboration meta-learner, code-simplification, code-security, dev-environment self-reflection), wired into a coordinated fleet with exactly one merge authority. Templates are capability-driven: they detect what a project supports and degrade gracefully, never guessing commands.
- **Discover** what automations are already installed across every agent.

## Install

```bash
git clone https://github.com/shanemhamilton/nightshift.git
cd nightshift
python3 scripts/install_skill.py            # installs into every detected agent
python3 scripts/install_skill.py --dry-run  # preview first
```

The installer copies the skill into each agent's skills directory it finds:

| Agent | Install location | Scheduler |
|---|---|---|
| Codex CLI | `~/.codex/skills/automation-optimizer` | native file (`automation.toml`, `rrule`) |
| Claude Code | `~/.claude/skills/automation-optimizer` | native daemon (`scheduled-tasks/*/SKILL.md`) |
| Gemini CLI | `~/.gemini/skills/automation-optimizer` | external cron + `gemini -p` |
| Cursor | (no global skills dir — project `.cursor/` only) | cloud (dashboard/API) |

Re-running updates in place and backs up any previous install. Use `--link` to symlink instead of copy (dev mode), `--agents claude,codex` to scope.

## Use

```bash
SK=~/.codex/skills/automation-optimizer/scripts
python3 $SK/discover_agents.py                              # inventory all agents (read-only)
python3 $SK/optimize_codex_automations.py                   # audit existing Codex jobs (dry run)
python3 $SK/optimize_codex_automations.py --apply           # harden them (backs up first)
python3 $SK/profile_project.py profile <repo> --out suite.toml   # propose a suite for a project
python3 $SK/profile_project.py approve --suite suite.toml        # confirm once
python3 $SK/scaffold_suite.py --suite suite.toml --install       # materialize runnable jobs
python3 $SK/optimize_codex_automations.py --fleet suite.toml --require-approved
```

### Lifecycle — manage one automation over time, on any agent

```bash
python3 $SK/lifecycle.py setup  <repo> --agent codex --apply                 # stand up a suite
python3 $SK/lifecycle.py add    --suite suite.toml --pattern P7 --agent codex --apply   # add one job
python3 $SK/lifecycle.py update --suite suite.toml --id coverage-ratchet --param coverage_floor=85 --agent codex --apply
python3 $SK/lifecycle.py remove --suite suite.toml --id code-security --agent codex --apply           # disable (reversible)
python3 $SK/lifecycle.py remove --suite suite.toml --id code-security --purge --agent codex --apply   # archive + delete
```

`setup` / `add` / `remove` / `update` all follow one pipeline — mutate the manifest → validate the
fleet → gate on approval → materialize per agent. Every verb defaults to a dry run; `--apply` is the
confirmation. `--agent all` targets Codex, Claude, Gemini, and Cursor (cron/cloud agents get the
config emitted, never applied for you). See [`reference/lifecycle.md`](reference/lifecycle.md).

## Layout

```
SKILL.md                     # the skill entry point (loaded by the agent)
LICENSE                      # MIT
scripts/
  agent_adapters.py          # per-agent locations/formats/schedulers (source of truth)
  install_skill.py           # self-installer into each agent
  discover_agents.py         # read-only cross-agent inventory
  optimize_codex_automations.py  # optimizer + fleet validator
  profile_project.py         # composer: profile + approve
  scaffold_suite.py          # composer: materialize approved jobs (Codex)
  lifecycle.py               # lifecycle front door: setup / add / remove / update
  agent_materializers.py     # per-agent write/disable/purge + shared template bodies
  selftest_lifecycle.py      # end-to-end lifecycle checks
reference/                   # contract, pattern library, suite manifest, lifecycle, agent adapters, examples
```

## Safety

Never deploys, publishes, sends external messages, changes secrets/billing, rewrites git history, or deletes data while optimizing. Composer install is approval-gated; writing into `~/.<agent>` requires an explicit flag. For cron (Gemini) and cloud (Cursor) schedulers the tooling emits the command/config for you to apply — it never edits cron or creates cloud resources itself.

## License

MIT — see [LICENSE](LICENSE).
