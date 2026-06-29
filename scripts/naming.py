#!/usr/bin/env python3
"""
naming.py — project-scoped automation IDs and human display names (single source).

Codex/Claude/Gemini automation registries are GLOBAL: a Codex automation lives at
`~/.codex/automations/<id>/`, a Claude scheduled task at
`~/.claude/scheduled-tasks/<name>/`. Two projects that install the same suite with
generic ids like `product-value-loop` would collide on the same directory and
silently overwrite each other.

These pure helpers namespace every job id by a slug of the project
(`skincrafter-product-value-loop`) and derive a human display name
(`SkinCrafter Product Value Loop`). Stdlib only, no side effects — imported by
profile_project.py, agent_materializers.py, discover_agents.py, and lifecycle.py
so the rules live in exactly one place and never drift (same pattern as
pattern_bodies.py).

IDs and names are intentionally OUTSIDE the approval fingerprint
(FINGERPRINT_FIELDS), so renaming/namespacing never invalidates an approval.
"""
from __future__ import annotations

import re


def slugify(s: str) -> str:
    """Lowercase, collapse any non-alphanumeric run to a single '-', trim edges.

    "SkinCrafter" -> "skincrafter"; "Overnight Automation" -> "overnight-automation".
    Falls back to "project" so an empty/symbol-only name still yields a usable slug.
    """
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "project"


def namespace_id(project: str, base_id: str) -> str:
    """Prefix `base_id` with the project slug, idempotently.

    `namespace_id("SkinCrafter", "product-value-loop")` -> "skincrafter-product-value-loop".
    Already-prefixed ids pass through unchanged, so this is safe to apply twice.
    """
    slug = slugify(project)
    if base_id == slug or base_id.startswith(slug + "-"):
        return base_id
    return f"{slug}-{base_id}"


def base_title(job_id: str, project: str) -> str:
    """Strip the project slug prefix, then turn the remainder into a Title Case label.

    `base_title("skincrafter-product-value-loop", "SkinCrafter")` -> "Product Value Loop".
    """
    slug = slugify(project)
    core = job_id[len(slug) + 1:] if job_id.startswith(slug + "-") else job_id
    return core.replace("-", " ").title()


def display_name(job: dict, project: str | None = None) -> str:
    """Resolve a job's user-visible name.

    Order of preference:
      1. an explicit `job["name"]` (composer stamps this);
      2. "<project> <base_title>" when a project is known (the default fallback);
      3. an id-derived title when no project is available (legacy behavior).
    """
    if job.get("name"):
        return job["name"]
    if project:
        return f"{project} {base_title(job['id'], project)}"
    return job["id"].replace("-", " ").title()
