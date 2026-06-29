#!/usr/bin/env python3
"""
agent_adapters.py — per-agent knowledge for the Automation Optimizer & Composer.

Each adapter describes, for one coding agent, where its recurring automations and
canonical instructions live, what format they use, and how scheduling works. This
is the single place that encodes the differences between Codex, Claude Code,
Gemini CLI, and Cursor, so the optimizer, composer, scaffolder, and discovery
tools can stay agent-agnostic.

Grounded in a real macOS install (June 2026). Paths use ~ and are expanded by the
caller. `scheduler` drives install policy:
  - "native_file": writing the registry file IS the registration (Codex, Claude).
  - "external_cron": no native registry; scheduling is cron + a headless CLI call.
    The tooling EMITS the crontab line; it never edits crontab itself.
  - "cloud_api": automations live in the vendor cloud (dashboard/API). The tooling
    EMITS the config/payload; it never creates cloud resources itself.
"""
from __future__ import annotations

ADAPTERS = {
    "codex": {
        "label": "OpenAI Codex CLI",
        # Each automation is a directory holding automation.toml + sidecars.
        "automations_glob": "~/.codex/automations/*/automation.toml",
        "automations_root": "~/.codex/automations",
        "job_layout": "dir",                 # <root>/<id>/automation.toml
        "prompt_key": "prompt",              # flat TOML string (escaped \\n)
        "schedule_field": "rrule",           # iCalendar RRULE, e.g. FREQ=WEEKLY;BYHOUR=2
        "cwd_field": "cwds",                 # array, plural
        "status_field": "status",            # "ACTIVE" / etc.
        "extra_fields": ["version", "id", "kind", "name", "model",
                         "reasoning_effort", "execution_environment"],
        # Sidecars live at the JOB ROOT (not a state/ subfolder).
        "sidecars": ["memory.md", "last-run.md", "priority-queue.md",
                     "human-approval.md", "baseline-failures.md"],
        "sidecar_dirs": ["runs"],
        "lock": ".automation.lock",
        "canonical_instructions": ["AGENTS.md"],
        "scheduler": "native_file",
        "home_dir": "~/.codex",
        "skills_dir": "~/.codex/skills",          # native skills location
        "protocol_markers": ("## Automation Optimizer Protocol",
                             "## End Automation Optimizer Protocol"),
        "protocol_version_re": r"Protocol version:\s*(\d+)",
    },
    "claude": {
        "label": "Claude Code",
        # A scheduling daemon runs tasks defined as <name>/SKILL.md directories.
        "automations_glob": "~/.claude/scheduled-tasks/*/SKILL.md",
        "automations_root": "~/.claude/scheduled-tasks",
        "job_layout": "dir",                 # <root>/<name>/SKILL.md
        "prompt_key": None,                  # the SKILL.md body IS the prompt
        "schedule_field": None,              # managed by the Claude daemon
        "cwd_field": None,
        "status_field": None,
        "extra_fields": [],
        "sidecars": ["memory.md", "last-run.md", "priority-queue.md",
                     "human-approval.md", "baseline-failures.md"],
        "sidecar_dirs": ["runs"],
        "lock": ".automation.lock",
        "canonical_instructions": ["CLAUDE.md", ".claude/rules"],
        "scheduler": "native_file",          # daemon picks up SKILL.md dirs
        "home_dir": "~/.claude",
        "skills_dir": "~/.claude/skills",         # native skills location
        "protocol_markers": ("## Automation Optimizer Protocol",
                             "## End Automation Optimizer Protocol"),
        "protocol_version_re": r"Protocol version:\s*(\d+)",
    },
    "gemini": {
        "label": "Gemini CLI",
        # No native registry; recurring runs are cron + `gemini -p <prompt>`.
        "automations_glob": None,
        "automations_root": "~/.gemini/automations",   # our convention for prompt files
        "job_layout": "dir",
        "prompt_key": None,                  # prompt stored as prompt.md
        "schedule_field": None,
        "cwd_field": None,
        "status_field": None,
        "extra_fields": [],
        "sidecars": ["memory.md", "last-run.md", "priority-queue.md",
                     "human-approval.md", "baseline-failures.md"],
        "sidecar_dirs": ["runs"],
        "lock": ".automation.lock",
        "canonical_instructions": ["GEMINI.md", "~/.gemini/GEMINI.md"],
        "scheduler": "external_cron",
        "home_dir": "~/.gemini",
        # Gemini loads context via GEMINI.md / extensions, not a skills dir; we
        # install the scripts here so the CLI commands resolve, but the agent
        # won't auto-surface it as a skill.
        "skills_dir": "~/.gemini/skills",
        "run_command": 'gemini -p "$(cat {prompt_file})"',
        "protocol_markers": ("## Automation Optimizer Protocol",
                             "## End Automation Optimizer Protocol"),
        "protocol_version_re": r"Protocol version:\s*(\d+)",
    },
    "cursor": {
        "label": "Cursor",
        # Automations are cloud (dashboard/API), triggered by cron or events.
        # Locally only project rules exist.
        "automations_glob": None,
        "automations_root": None,
        "job_layout": "cloud",
        "prompt_key": None,
        "schedule_field": None,
        "cwd_field": None,
        "status_field": None,
        "extra_fields": [],
        "sidecars": [],
        "sidecar_dirs": [],
        "lock": None,
        "canonical_instructions": [".cursor/rules", ".cursorrules", "AGENTS.md"],
        "scheduler": "cloud_api",
        "home_dir": "~/.cursor",
        "skills_dir": None,                       # no global skills dir; project .cursor/ only
        "protocol_markers": ("## Automation Optimizer Protocol",
                             "## End Automation Optimizer Protocol"),
        "protocol_version_re": r"Protocol version:\s*(\d+)",
    },
}

# Canonical-instructions filename used by the P5 meta-learner, per agent.
CANONICAL = {a: cfg["canonical_instructions"][0] for a, cfg in ADAPTERS.items()}


def get(agent: str) -> dict:
    if agent not in ADAPTERS:
        raise KeyError(f"unknown agent {agent!r}; known: {', '.join(ADAPTERS)}")
    return ADAPTERS[agent]
