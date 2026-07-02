#!/usr/bin/env python3
"""
approval_digest.py — collapse every automation's pending human decisions into ONE
low-cognitive-load morning inbox (pattern P9's deterministic engine).

READ-ONLY over the fleet by default: it scans each agent's automations (via
agent_adapters), parses every `human-approval.md`, dedupes, ranks by age, and
buckets items into "safe to batch-approve" vs "needs judgment". The default
(no subcommand) invocation NEVER mutates a project's queue.

The `resolve` subcommand is the one exception: it reads an OPERATOR-EDITED
DAILY-APPROVALS.md (checked boxes / decision notes) and writes those decisions
back to the originating human-approval.md + memory.md — see `cmd_resolve`.

Output: ~/.codex/DAILY-APPROVALS.md (or --root/<...>). Channels: --notify-macos
(local osascript), --channel email|slack (OFF unless explicitly configured AND
opted in — sending crosses the external boundary, so it refuses without both).

Usage:
  approval_digest.py                       # print digest to stdout
  approval_digest.py --write               # also write DAILY-APPROVALS.md
  approval_digest.py --write --notify-macos
  approval_digest.py --json                # machine-readable
  approval_digest.py resolve [--digest PATH] [--codex-home P]
                                            # apply operator decisions back
  approval_digest.py --emit-launchd        # print a launchd plist (emit-only)
  approval_digest.py --emit-cron           # print a crontab line (emit-only)
  options: --codex-home PATH  --channel email|slack
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# Share the adapter registry (agent → automations_root, sidecar names) by path so
# this runs regardless of cwd, exactly like the other skill scripts.
_AA_PATH = Path(__file__).resolve().parent / "agent_adapters.py"
_spec = importlib.util.spec_from_file_location("ao_adapters", _AA_PATH)
AA = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AA)  # type: ignore

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - dev fallback
    tomllib = None

APPROVAL_FILE = "human-approval.md"
SAFE_RISKS = {"low"}
JUDGMENT_RISKS = {"medium", "high"}
# Unknown-risk items only count as "safe to batch" when they clearly match a
# low-stakes housekeeping decision; everything else defaults to needs-judgment.
SAFE_HINTS = re.compile(
    r"(gitignore|\.serena|ignore[- ]policy|stale branch|delete[^.]*branch|"
    r"prune|branch cleanup|untracked.*(metadata|tool)|tool metadata)", re.I)
# Always-needs-judgment regardless of SAFE_HINTS: high-blast-radius verbs/targets.
DENY_JUDGMENT = re.compile(
    r"(deploy|force|secret|prod|origin/main|release|rotate)", re.I)
AGE_THRESHOLD_DAYS = 7
_TS_HEADING = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][\d:.\-+Z]+)?")
_FIELD = re.compile(r"^[-*]\s*([a-z_]+)\s*:\s*(.*)$", re.I)
_CHECKBOX = re.compile(r"^[-*]\s*\[ \]\s*(.+)$")
_BULLET = re.compile(r"^[-*]\s+(.+)$")
# A "container" heading (e.g. `## Pending Decisions`) is a section whose BULLETS
# are the decisions — not itself an item. Real asks are full sentences, so we only
# treat a short heading that names a queue as a container.
_CONTAINER_WORD = re.compile(r"(pending|decision|approval|awaiting|to approve|"
                             r"needs?[- ]?human|open question)", re.I)
_NONE_TOKENS = {"none", "n/a", "na", "(none)", "nothing", "—", "-"}
# Closure notes a job leaves in its own queue once an item is handled — surfaced
# as a count, not as live decisions (keeps the inbox to things that still need you).
_RESOLVED = re.compile(r"no (human )?approval needed|no action needed|"
                       r"already (merged|owned|resolved|landed)|resolved this run|"
                       r"no longer needed|nothing to approve", re.I)


def _is_none(txt: str) -> bool:
    return txt.strip().strip(".").lower() in _NONE_TOKENS


def _roots(codex_home: str | None) -> list[tuple[str, Path]]:
    """(agent, automations_root) for every adapter that has a local registry."""
    out: list[tuple[str, Path]] = []
    for agent, cfg in AA.ADAPTERS.items():
        root = cfg.get("automations_root")
        if not root:
            continue
        p = Path(root).expanduser()
        if agent == "codex" and codex_home:
            p = Path(codex_home).expanduser() / "automations"
        if p.is_dir():
            out.append((agent, p))
    return out


def _project_of(job_id: str) -> str:
    """Project label = the namespaced id's first slug segment (skincrafter-... )."""
    head = job_id.split("-", 1)[0]
    return head.replace("_", " ").title() if head else "(unknown)"


def iter_queue_files(codex_home: str | None):
    """Yield (agent, project, job_id, path) for each human-approval.md present."""
    for agent, root in _roots(codex_home):
        for job_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if job_dir.name.startswith("."):  # .archive/.disabled
                continue
            f = job_dir / APPROVAL_FILE
            if f.is_file():
                yield agent, _project_of(job_dir.name), job_dir.name, f


def _is_container(heading: str) -> bool:
    """A short heading that names a queue ('Pending Decisions') is a container; a
    full-sentence ask ('Decide whether ...') is not."""
    return len(heading.split()) <= 4 and bool(_CONTAINER_WORD.search(heading))


def _heading_ask(heading: str) -> tuple[str, str | None]:
    """For a structured-item heading return (ask, first_seen). A timestamp/date
    heading contributes its date and any trailing ' - <text>' as the ask."""
    if _TS_HEADING.match(heading):
        tail = heading.split(" - ", 1)
        return (tail[1].strip() if len(tail) > 1 else ""), heading[:10]
    return heading, None


def parse_queue(text: str) -> tuple[list[dict], list[str]]:
    """Parse one human-approval.md into (items, malformed). Tolerant of three
    shapes: the structured format (`## ask` + risk/action/... fields), legacy
    `## <timestamp>` blocks, and bullets under a `## Pending Decisions` container
    (or `- [ ]` checkboxes). Container headings and bare dates are NOT items."""
    items: list[dict] = []
    malformed: list[str] = []
    cur: dict | None = None
    in_container = False

    def flush():
        nonlocal cur
        if cur is None:
            return
        ask = (cur.get("item") or cur.get("candidate") or cur.get("_ask") or "").strip()
        if ask and not _is_none(ask):
            cur["ask"] = ask
            cur["_explicit_risk"] = "risk" in cur
            cur.setdefault("risk", _infer_risk(cur))
            items.append(cur)
        elif cur.get("_fields"):
            malformed.append(cur.get("_raw_heading", "<no ask>"))
        cur = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("# ") or line.lower().startswith("format per item"):
            continue
        if line.startswith("## "):
            flush()
            heading = line[3:].strip()
            if _is_container(heading):
                in_container = True
                continue
            in_container = False
            ask, first_seen = _heading_ask(heading)
            cur = {"_ask": ask, "_raw_heading": heading, "_fields": False}
            if first_seen:
                cur["first_seen"] = first_seen
            continue
        cb = _CHECKBOX.match(line)
        if cb:
            flush()
            items.append({"ask": cb.group(1).strip(), "risk": "unknown",
                          "_explicit_risk": False})
            continue
        fm = _FIELD.match(line)
        if fm and cur is not None:
            cur[fm.group(1).lower()] = fm.group(2).strip()
            cur["_fields"] = True
            continue
        bullet = _BULLET.match(line)
        if bullet and in_container:
            txt = bullet.group(1).strip()
            if txt and not _is_none(txt):
                items.append({"ask": txt, "risk": "unknown",
                             "_explicit_risk": False})
    flush()
    return items, malformed


def _infer_risk(item: dict) -> str:
    cls = (item.get("classification") or "").lower()
    if "high" in cls or "secret" in cls or "deploy" in cls:
        return "high"
    return "unknown"


def classify(item: dict) -> str:
    """Deterministic, total: every item maps to exactly "safe" or "judgment".

    - risk: low            -> safe (explicit operator/agent signal wins).
    - risk: medium/high     -> judgment.
    - unknown risk          -> safe ONLY when (a) there is no explicit `risk:`
      field at all AND (b) the ask/action text matches SAFE_HINTS (purely
      local/metadata surfaces). DENY_JUDGMENT (deploy/force/secret/prod/
      origin-main/release/rotate) always overrides SAFE_HINTS back to judgment,
      even when a risk field is present and says "low" — high-blast-radius
      verbs are never auto-batched.
    """
    blob = " ".join(str(item.get(k, "")) for k in ("ask", "classification", "action"))
    if DENY_JUDGMENT.search(blob):
        return "judgment"
    risk = (item.get("risk") or "unknown").lower()
    if risk in JUDGMENT_RISKS:
        return "judgment"
    if risk == "low":
        return "safe"
    has_explicit_risk = bool(item.get("_explicit_risk"))
    if not has_explicit_risk and SAFE_HINTS.search(blob):
        return "safe"
    return "judgment"  # conservative: unknown stays in front of the human


def age_days(item: dict, today: _dt.date) -> int | None:
    fs = item.get("first_seen")
    if not fs:
        return None
    try:
        return (today - _dt.date.fromisoformat(str(fs)[:10])).days
    except ValueError:
        return None


def is_aged(item: dict, today: _dt.date) -> bool:
    age = age_days(item, today)
    return age is not None and age >= AGE_THRESHOLD_DAYS


def item_id(project: str, ask: str) -> str:
    """Stable id: ao_ + first 8 hex of sha256(project\\nnormalized_ask). Stable
    across re-digests as long as (project, ask) don't change, matching the
    dedupe key so an item keeps its id run to run."""
    normalized_ask = re.sub(r"\s+", " ", ask.lower()).strip()
    digest = hashlib.sha256(f"{project}\n{normalized_ask}".encode("utf-8")).hexdigest()
    return f"ao_{digest[:8]}"


def _is_incomplete(item: dict) -> bool:
    """True when an item is missing first_seen, risk, or action — i.e. it
    parsed but doesn't carry the fields resolve() and aging need."""
    if not item.get("first_seen"):
        return True
    if not item.get("_explicit_risk"):
        return True
    if not item.get("action"):
        return True
    return False


def _fmt_item(it: dict, today: _dt.date) -> str:
    aged = is_aged(it, today)
    age = age_days(it, today)
    age_s = f"{age}d old" if age is not None else "age unknown"
    fs = f", first seen {it['first_seen']}" if it.get("first_seen") else ""
    prefix = "⚠ AGED " if aged else ""
    iid = item_id(it["project"], it["ask"])
    lines = [f"### {prefix}{it['project']} — {it['ask']}  `{iid}`",
             f"- risk: {it.get('risk', 'unknown')}  ({age_s}{fs})"]
    if it.get("suggested_default"):
        lines.append(f"- suggested default: {it['suggested_default']}")
    if it.get("action"):
        lines.append(f"- action: {it['action']}")
    if it.get("evidence"):
        lines.append(f"- evidence: {it['evidence']}")
    if it.get("_objectives"):
        lines.append(f"- {it['_objectives']}")
    lines.append(f"- source: {it['source']} ({it['agent']})")
    lines.append("- [ ] approve")
    srcpath = it.get("_srcpath", "")
    srchash = it.get("_srchash", "")
    lines.append(f"<!-- ao:item id={iid} src={srcpath} srchash={srchash} -->")
    return "\n".join(lines)


def _fleet_summary_line(codex_home: str | None) -> str | None:
    """Best-effort first summary line of <codex-home>/FLEET-REPORT.md (written
    by fleet_report.py), for the digest header context. Missing/unreadable
    file -> None; NEVER raises. Does not import fleet_report — reads the file
    text only, matching this module's existing best-effort-hook style (see
    objectives_line)."""
    try:
        home = Path(codex_home).expanduser() if codex_home \
            else Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        report_path = home / "FLEET-REPORT.md"
        if not report_path.is_file():
            return None
        text = report_path.read_text(encoding="utf-8", errors="replace")
        for raw in text.splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                return line
        return None
    except OSError:
        return None


def build_digest(items: list[dict], malformed: list[tuple[str, str]],
                 today: _dt.date, resolved: int = 0,
                 fleet_summary: str | None = None) -> str:
    # Structurally-incomplete items (missing first_seen/risk/action) join the
    # truly-unparseable ones under "Needs cleanup" rather than being digested
    # with unreliable data.
    incomplete = [i for i in items if _is_incomplete(i)]
    items = [i for i in items if not _is_incomplete(i)]
    cleanup: list[tuple[str, str]] = list(malformed)
    for i in incomplete:
        missing = []
        if not i.get("first_seen"):
            missing.append("first_seen")
        if not i.get("_explicit_risk"):
            missing.append("risk")
        if not i.get("action"):
            missing.append("action")
        heading = i.get("_raw_heading") or i.get("ask", "<no ask>")
        cleanup.append((i.get("source", "?"),
                        f"## {heading}  (missing: {', '.join(missing)}) "
                        f"— {i.get('_srcpath', i.get('source', '?'))}"))

    judgment = [i for i in items if classify(i) == "judgment"]
    safe = [i for i in items if classify(i) == "safe"]
    # AGED items sort first within "Needs judgment"; otherwise oldest-first.
    key = lambda i: (not is_aged(i, today), age_days(i, today) is None,
                     -(age_days(i, today) or 0))
    judgment.sort(key=key)
    safe.sort(key=lambda i: (age_days(i, today) is None, -(age_days(i, today) or 0)))
    projects = sorted({i["project"] for i in items})
    note = f" ({resolved} resolved note(s) filtered)" if resolved else ""
    out = [f"# Daily approvals — {today.isoformat()}",
           f"Read-only digest of pending human decisions. "
           f"{len(items)} item(s) across {len(projects)} project(s): "
           f"{len(judgment)} need judgment, {len(safe)} safe to batch-approve.{note}"]
    if fleet_summary:
        out.append(f"Fleet: {fleet_summary}")
    out.append("")
    out.append(f"## Needs judgment ({len(judgment)})")
    out += [_fmt_item(i, today) + "\n" for i in judgment] or ["_none_\n"]
    out.append(f"## Safe to batch-approve ({len(safe)})")
    out += [_fmt_item(i, today) + "\n" for i in safe] or ["_none_\n"]
    if cleanup:
        out.append(f"## Needs cleanup ({len(cleanup)})")
        out += [f"- {src}: {why}" for src, why in cleanup]
    return "\n".join(out).rstrip() + "\n"


def notify_macos(judgment: int, safe: int) -> str:
    if sys.platform != "darwin":
        return "skipped (not macOS)"
    msg = f"{judgment} decisions need judgment, {safe} safe to batch-approve"
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "NightShift approvals"'],
            check=True, capture_output=True, timeout=10)
        return "sent"
    except (OSError, subprocess.SubprocessError) as e:
        return f"failed ({e})"


def deliver_external(channel: str, digest: str) -> str:
    """Send the digest to email/Slack — but ONLY when both a destination is
    configured AND the operator opted in. Otherwise refuse (no external send)."""
    optin = os.environ.get("AO_DIGEST_EXTERNAL_OPTIN") == "1"
    dest = os.environ.get(f"AO_DIGEST_{channel.upper()}")  # webhook / address
    if not (optin and dest):
        return (f"refused: {channel} delivery is OFF. Set AO_DIGEST_EXTERNAL_OPTIN=1 "
                f"and AO_DIGEST_{channel.upper()}=<destination> to enable, then "
                f"approve it in the human-approval queue.")
    # Configured + opted in: a real integration would POST/SMTP here. Kept as an
    # explicit stub so no message leaves the machine without a deliberate wiring.
    return (f"configured for {channel} → {dest[:24]}…, but sending is not wired in "
            f"this build; deliver {len(digest)} chars yourself or wire the adapter.")


def _sha256_file(path: Path) -> str:
    """First 12 hex chars of sha256 of a file's current bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


# --- D3: best-effort project-objectives hook ---------------------------------

def _codex_workspace(job_toml: Path) -> str | None:
    """Read a codex automation.toml's cwds[0] as the job's workspace. Degrades
    silently (missing tomllib / bad file) to None."""
    if tomllib is None or not job_toml.is_file():
        return None
    try:
        data = tomllib.loads(job_toml.read_text(encoding="utf-8", errors="replace"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    cwds = data.get("cwds")
    return cwds[0] if isinstance(cwds, list) and cwds else None


def _first_bullet(text: str, section: str) -> str | None:
    """First non-empty `- ` bullet under a `## <section>` heading, or None."""
    in_section = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            in_section = line[3:].strip().lower() == section.lower()
            continue
        if in_section:
            m = _BULLET.match(line)
            if m and m.group(1).strip():
                return m.group(1).strip()
    return None


def _count_bullets(text: str, section: str) -> int:
    in_section = False
    n = 0
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            in_section = line[3:].strip().lower() == section.lower()
            continue
        if in_section and _BULLET.match(line):
            n += 1
    return n


def objectives_line(agent: str, job_id: str, job_path: Path) -> str | None:
    """Best-effort `Objectives: ...` line for a job's project. NEVER raises —
    any lookup failure (missing automation.toml, missing PROJECT-QUEUE.md,
    unreadable file) degrades to None so digest-building never hard-depends on
    this file existing."""
    try:
        if agent != "codex":
            return None
        job_toml = job_path.parent / "automation.toml"
        workspace = _codex_workspace(job_toml)
        if not workspace:
            return None
        pq = Path(workspace).expanduser() / ".codex" / "automations" / "PROJECT-QUEUE.md"
        if not pq.is_file():
            return None
        text = pq.read_text(encoding="utf-8", errors="replace")
        first_obj = _first_bullet(text, "Objectives")
        if first_obj:
            return f"Objectives: {first_obj}"
        n_obj = _count_bullets(text, "Objectives")
        n_threads = _count_bullets(text, "Open threads")
        if n_obj or n_threads:
            return f"Objectives: {n_obj} objective(s)/{n_threads} open thread(s)"
        return None
    except OSError:
        return None


# --- D4: emit-only delivery artifacts -----------------------------------------

def emit_launchd(script_path: Path) -> str:
    """Ready-to-install launchd plist that runs this script daily at 06:40.
    EMIT-ONLY: prints XML to stdout; never touches ~/Library/LaunchAgents."""
    python3 = sys.executable or "python3"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.nightshift.approval-digest</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python3}</string>
        <string>{script_path}</string>
        <string>--write</string>
        <string>--notify-macos</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>6</integer>
        <key>Minute</key>
        <integer>40</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/nightshift-approval-digest.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/nightshift-approval-digest.err</string>
</dict>
</plist>
"""


def emit_cron(script_path: Path) -> str:
    """Crontab line for the same schedule. EMIT-ONLY: prints to stdout; never
    edits crontab (mirrors the gemini adapter's never-edit-cron policy)."""
    python3 = sys.executable or "python3"
    return f"40 6 * * * {python3} {script_path} --write --notify-macos\n"


# --- D1: resolve — apply operator-edited decisions back to source ------------

_ITEM_HEADING = re.compile(r"^### (?:⚠ AGED )?(.+?)  `(ao_[0-9a-f]{8})`\s*$")
_MARKER = re.compile(
    r"<!--\s*ao:item\s+id=(?P<id>ao_[0-9a-f]{8})\s+src=(?P<src>\S+)\s+"
    r"srchash=(?P<hash>[0-9a-f]{12})\s*-->")
_CHECKED = re.compile(r"^- \[[xX]\]\s*approve\s*$")
_DECISION_LINE = re.compile(r"^decision:\s*(.+)$", re.I)


def _parse_digest_blocks(text: str) -> list[dict]:
    """Split an (operator-edited) DAILY-APPROVALS.md into item blocks, keyed by
    the embedded marker. Each block records whether the checkbox is checked and
    any operator `decision: ...` free-text line."""
    blocks: list[dict] = []
    cur: dict | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("### "):
            if cur:
                blocks.append(cur)
            cur = {"checked": False, "decision": None, "marker": None}
            continue
        if line.startswith("## ") or line.startswith("# "):
            if cur:
                blocks.append(cur)
            cur = None
            continue
        if cur is None:
            continue
        if _CHECKED.match(line):
            cur["checked"] = True
            continue
        dm = _DECISION_LINE.match(line.strip())
        if dm:
            cur["decision"] = dm.group(1).strip()
            continue
        mm = _MARKER.search(line)
        if mm:
            cur["marker"] = mm.groupdict()
    if cur:
        blocks.append(cur)
    return blocks


def _append_resolved(src_text: str, block_heading: str, decision: str, today: _dt.date) -> str:
    """Remove the resolved item's `### ...` block from src_text and append it
    under `## Resolved` with the decision + today's date. Best-effort match on
    the ask text embedded in the heading; if the exact block can't be located,
    the resolution is still appended (never silently dropped)."""
    lines = src_text.splitlines()
    out_lines: list[str] = []
    removed_block: list[str] = []
    i = 0
    found = False
    while i < len(lines):
        line = lines[i]
        if line.startswith("## ") and block_heading and block_heading in line:
            found = True
            removed_block.append(line)
            i += 1
            while i < len(lines) and not lines[i].startswith("## "):
                removed_block.append(lines[i])
                i += 1
            continue
        out_lines.append(line)
        i += 1
    new_text = "\n".join(out_lines).rstrip() + "\n"
    resolved_entry = "\n".join(removed_block).strip() if found else block_heading
    resolved_section = (
        f"\n## Resolved\n- {today.isoformat()} — {resolved_entry}\n"
        f"  decision: {decision}\n")
    if "## Resolved" in new_text:
        new_text = new_text.rstrip() + "\n" + resolved_section.lstrip("\n")
    else:
        new_text = new_text.rstrip() + "\n" + resolved_section
    return new_text


def _append_stable_decision(memory_path: Path, ask: str, decision: str, today: _dt.date) -> None:
    """Read-modify-write memory.md's `## Stable decisions` section, creating the
    file/section if missing, preserving all existing content."""
    entry = f"- {today.isoformat()} — {ask}: {decision}\n"
    if not memory_path.is_file():
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text(f"## Stable decisions\n{entry}", encoding="utf-8")
        return
    text = memory_path.read_text(encoding="utf-8")
    marker = "## Stable decisions"
    if marker not in text:
        sep = "" if text.endswith("\n") else "\n"
        memory_path.write_text(text + sep + f"\n{marker}\n{entry}", encoding="utf-8")
        return
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    i = 0
    inserted = False
    while i < len(lines):
        out.append(lines[i])
        if lines[i].rstrip("\n") == marker:
            # Insert the new entry as the first item under this section.
            out.append(entry)
            inserted = True
            i += 1
            break
        i += 1
    out.extend(lines[i:])
    if not inserted:
        out.append(f"\n{marker}\n{entry}")
    memory_path.write_text("".join(out), encoding="utf-8")


def cmd_resolve(args: argparse.Namespace) -> int:
    home = Path(args.codex_home).expanduser() if args.codex_home \
        else Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    digest_path = Path(args.digest).expanduser() if args.digest else home / "DAILY-APPROVALS.md"
    if not digest_path.is_file():
        print(f"No digest found at {digest_path}", file=sys.stderr)
        return 1

    text = digest_path.read_text(encoding="utf-8")
    # Recover the heading line for each block so we can locate the source block.
    raw_lines = text.splitlines()
    headings: list[str] = []
    cur_heading = None
    heading_by_marker: dict[str, str] = {}
    for i, line in enumerate(raw_lines):
        if line.startswith("### "):
            cur_heading = line[4:].split("`ao_", 1)[0].strip()
            hm = _ITEM_HEADING.match(line)
            ask_only = hm.group(1).split(" — ", 1)[-1] if hm else cur_heading
            cur_heading = f"## {ask_only}"
        m = _MARKER.search(line)
        if m and cur_heading:
            heading_by_marker[m.group("id")] = cur_heading

    blocks = _parse_digest_blocks(text)
    today = _dt.date.today()
    resolved_n = 0
    skipped: list[tuple[str, str]] = []

    for block in blocks:
        marker = block.get("marker")
        if not marker:
            continue
        checked = block.get("checked")
        decision = block.get("decision")
        if not checked and not decision:
            continue  # never act on an unchecked item with no decision
        item_id_ = marker["id"]
        src_path = Path(marker["src"])
        expected_hash = marker["hash"]

        if not src_path.is_file():
            skipped.append((item_id_, f"source missing: {src_path}"))
            continue
        current_hash = _sha256_file(src_path)
        if current_hash != expected_hash:
            skipped.append((item_id_, f"source changed since digest: {src_path}"))
            continue

        decision_text = decision or "approved"
        block_heading = heading_by_marker.get(item_id_, "")
        ask_text = block_heading[3:].strip() if block_heading.startswith("## ") else block_heading

        src_text = src_path.read_text(encoding="utf-8")
        new_src_text = _append_resolved(src_text, block_heading, decision_text, today)
        src_path.write_text(new_src_text, encoding="utf-8")

        memory_path = src_path.parent / "memory.md"
        _append_stable_decision(memory_path, ask_text, decision_text, today)

        resolved_n += 1

    print(f"resolve: {resolved_n} resolved, {len(skipped)} skipped")
    if skipped:
        print("\nSkipped (source changed since digest):")
        for item_id_, reason in skipped:
            print(f"  - {item_id_}: {reason}")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Cross-project approval digest (P9).")
    ap.add_argument("--write", action="store_true",
                    help="write DAILY-APPROVALS.md (default: print only)")
    ap.add_argument("--notify-macos", action="store_true",
                    help="post a local macOS notification with the counts")
    ap.add_argument("--channel", choices=["email", "slack"], default=None,
                    help="external delivery (OFF unless configured + opted in)")
    ap.add_argument("--codex-home", default=None, help="override ~/.codex")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--emit-launchd", action="store_true",
                    help="print a launchd plist that runs --write --notify-macos "
                         "daily at 06:40 (emit-only; never installs)")
    ap.add_argument("--emit-cron", action="store_true",
                    help="print a crontab line for the same schedule (emit-only; "
                         "never edits crontab)")

    sub = ap.add_subparsers(dest="subcommand")
    rp = sub.add_parser("resolve", help="apply operator-edited digest decisions "
                                        "back to their source human-approval.md")
    rp.add_argument("--digest", default=None,
                    help="path to the edited digest (default: <codex-home>/DAILY-APPROVALS.md)")
    rp.add_argument("--codex-home", default=None, help="override ~/.codex")
    rp.set_defaults(func=cmd_resolve)
    return ap


def main(argv: list[str]) -> int:
    ap = build_arg_parser()
    args = ap.parse_args(argv)

    if getattr(args, "subcommand", None) == "resolve":
        return args.func(args)

    if args.emit_launchd:
        print(emit_launchd(Path(__file__).resolve()), end="")
        return 0
    if args.emit_cron:
        print(emit_cron(Path(__file__).resolve()), end="")
        return 0

    today = _dt.date.today()
    items: list[dict] = []
    malformed: list[tuple[str, str]] = []
    for agent, project, job_id, path in iter_queue_files(args.codex_home):
        try:
            parsed, bad = parse_queue(path.read_text(encoding="utf-8"))
        except OSError as e:
            malformed.append((job_id, f"unreadable: {e}"))
            continue
        srchash = _sha256_file(path)
        obj_line = objectives_line(agent, job_id, path)
        for it in parsed:
            it.update({"project": project, "agent": agent, "source": job_id,
                      "_srcpath": str(path.resolve()), "_srchash": srchash})
            if obj_line:
                it["_objectives"] = obj_line
            items.append(it)
        for h in bad:
            malformed.append((job_id, f"unparseable item heading {h!r}"))

    # Dedupe by (project, normalized ask): the same decision re-queued across runs
    # appears once. Keep the earliest first_seen.
    dedup: dict[tuple[str, str], dict] = {}
    for it in items:
        k = (it["project"], re.sub(r"\s+", " ", it["ask"].lower())[:120])
        if k not in dedup or str(it.get("first_seen", "9")) < str(
                dedup[k].get("first_seen", "9")):
            dedup[k] = it
    items = list(dedup.values())

    # Drop self-resolved closure notes a job left in its own queue; surface the count.
    resolved = sum(1 for i in items if _RESOLVED.search(i["ask"]))
    items = [i for i in items if not _RESOLVED.search(i["ask"])]

    if args.json:
        for it in items:
            for k in ("_ask", "_raw_heading", "_fields", "_srcpath", "_srchash",
                     "_explicit_risk", "_objectives"):
                it.pop(k, None)
        print(json.dumps({"date": today.isoformat(), "items": items,
                          "malformed": malformed}, indent=2))
        return 0

    fleet_summary = _fleet_summary_line(args.codex_home)
    digest = build_digest(items, malformed, today, resolved, fleet_summary)
    print(digest)

    judgment = sum(1 for i in items if classify(i) == "judgment")
    safe = len(items) - judgment
    if args.write:
        home = Path(args.codex_home).expanduser() if args.codex_home \
            else Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        home.mkdir(parents=True, exist_ok=True)
        out = home / "DAILY-APPROVALS.md"
        out.write_text(digest, encoding="utf-8")
        print(f"\n[wrote {out}]", file=sys.stderr)
    if args.notify_macos:
        print(f"[notify: {notify_macos(judgment, safe)}]", file=sys.stderr)
    if args.channel:
        print(f"[{args.channel}: {deliver_external(args.channel, digest)}]",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
