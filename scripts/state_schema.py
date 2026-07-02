#!/usr/bin/env python3
"""
state_schema.py — canonical schema + lenient parser for the `last-run.md` /
`runs/<timestamp>.md` sidecar files.

Three on-disk shapes are handled, oldest fleet reality first:
  (a) "frontmatter" — the canonical NEW format: a `---`-delimited YAML-ish
      block at the top (see `render_frontmatter`), then free prose.
  (b) "template"     — the legacy `SIDECARS["last-run.md"]` shape: a
      `# Last run` header followed by `- key: value` bullets.
  (c) "legacy"        — freeform prose with no recognizable fields at all.

`parse_last_run` / `parse_run_entry` NEVER raise and return `dict | None`
(`None` only when the file does not exist). Python 3.11+ stdlib only — no
third-party YAML; the front-matter block is hand-rolled and intentionally
small (scalars + flat lists only).
"""
from __future__ import annotations

from pathlib import Path

# --- Canonical schema ---------------------------------------------------------
# Scalar fields (order matters for render_frontmatter output).
SCALAR_FIELDS = (
    "when",
    "outcome",
    "units_completed",
    "stop_reason",
    "failure_class",
    "runtime_s",
)
# List fields (rendered as inline `[a, b]`, also accept block `- a` / `- b`).
LIST_FIELDS = (
    "merged_shas",
    "branches",
    "tracker_ids",
)
SCHEMA_FIELDS = SCALAR_FIELDS + LIST_FIELDS
INT_FIELDS = {"units_completed", "runtime_s"}

TEMPLATE_HEADER = "# Last run"
FRONTMATTER_DELIM = "---"


def blank_record() -> dict:
    """A fresh record: every scalar field None, every list field []."""
    record: dict = {field: None for field in SCALAR_FIELDS}
    for field in LIST_FIELDS:
        record[field] = []
    return record


# --- Writer --------------------------------------------------------------------
def render_frontmatter(record: dict, prose: str = "") -> str:
    """Render `record` (missing keys treated as blank/empty) as the canonical
    NEW `---`-delimited front-matter block, followed by `prose`."""
    lines = [FRONTMATTER_DELIM]
    for field in SCALAR_FIELDS:
        value = record.get(field)
        lines.append(f"{field}: {'' if value is None else value}")
    for field in LIST_FIELDS:
        values = record.get(field) or []
        lines.append(f"{field}: [{', '.join(str(v) for v in values)}]")
    lines.append(FRONTMATTER_DELIM)
    body = "\n".join(lines) + "\n"
    if prose:
        body += prose if prose.endswith("\n") else prose + "\n"
    return body


# --- Small parsing helpers -------------------------------------------------
def _coerce_scalar(field: str, raw: str) -> object:
    raw = raw.strip()
    if not raw:
        return None
    if field in INT_FIELDS:
        try:
            return int(raw)
        except ValueError:
            return None
    return raw


def _parse_inline_list(raw: str) -> list[str]:
    inner = raw.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    items = [item.strip() for item in inner.split(",")]
    return [item for item in items if item]


def _looks_like_inline_list(raw: str) -> bool:
    return raw.strip().startswith("[")


# --- Shape (a): NEW frontmatter -----------------------------------------------
def _parse_frontmatter(text: str) -> dict | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        return None
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONTMATTER_DELIM:
            end_idx = i
            break
    if end_idx is None:
        return None

    record = blank_record()
    field_lines = lines[1:end_idx]
    i = 0
    while i < len(field_lines):
        line = field_lines[i]
        if ":" not in line:
            i += 1
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if key in LIST_FIELDS:
            if _looks_like_inline_list(rest) or rest == "":
                if rest:
                    record[key] = _parse_inline_list(rest)
                else:
                    # possible block list on following lines: `  - item`
                    block_items = []
                    j = i + 1
                    while j < len(field_lines) and field_lines[j].lstrip().startswith("-"):
                        block_items.append(field_lines[j].lstrip()[1:].strip())
                        j += 1
                    if block_items:
                        record[key] = block_items
                        i = j - 1
                    else:
                        record[key] = []
            else:
                record[key] = _parse_inline_list(rest)
        elif key in SCALAR_FIELDS:
            record[key] = _coerce_scalar(key, rest)
        # unknown keys ignored
        i += 1

    prose = "\n".join(lines[end_idx + 1:]).strip("\n")
    record["schema"] = "frontmatter"
    record["raw"] = text
    record["prose"] = prose
    return record


# --- Shape (b): legacy `# Last run` bullet template ---------------------------
def _parse_template(text: str) -> dict | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != TEMPLATE_HEADER:
        return None

    record = blank_record()
    prose_lines: list[str] = []
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.startswith("- ") and ":" in stripped:
            body = stripped[2:]
            key, _, rest = body.partition(":")
            key = key.strip()
            rest = rest.strip()
            if key in SCALAR_FIELDS:
                record[key] = _coerce_scalar(key, rest)
            elif key in LIST_FIELDS:
                record[key] = _parse_inline_list(rest) if rest else []
            elif key == "notes" and rest:
                prose_lines.append(rest)
            # any other unknown bullet (e.g. "rollback") is ignored
        elif stripped:
            prose_lines.append(stripped)

    record["schema"] = "template"
    record["raw"] = text
    record["prose"] = "\n".join(prose_lines).strip()
    return record


# --- Shape (c): freeform prose, opportunistic scavenging ----------------------
def _parse_legacy(text: str) -> dict:
    record = blank_record()
    record["schema"] = "legacy"
    record["raw"] = text
    return record


def _parse_text(text: str) -> dict:
    for parser in (_parse_frontmatter, _parse_template):
        try:
            result = parser(text)
        except Exception:
            result = None
        if result is not None:
            return result
    return _parse_legacy(text)


# --- Public API ------------------------------------------------------------
def parse_last_run(path: str | Path) -> dict | None:
    """Parse a `last-run.md` sidecar. Returns None only if `path` does not
    exist; otherwise ALWAYS returns a dict (never raises)."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return {**blank_record(), "schema": "legacy", "raw": ""}
    return _parse_text(text)


def parse_run_entry(path: str | Path) -> dict | None:
    """Parse a `runs/<timestamp>.md` entry. Same contract as parse_last_run."""
    return parse_last_run(path)


if __name__ == "__main__":  # pragma: no cover
    import sys as _sys
    for arg in _sys.argv[1:]:
        print(arg, "->", parse_last_run(arg))
