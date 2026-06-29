#!/usr/bin/env python3
"""
approval_digest.py — collapse every automation's pending human decisions into ONE
low-cognitive-load morning inbox (pattern P9's deterministic engine).

READ-ONLY over the fleet: it scans each agent's automations (via agent_adapters),
parses every `human-approval.md`, dedupes, ranks by age, and buckets items into
"safe to batch-approve" vs "needs judgment". It NEVER mutates a project's queue.

Output: ~/.codex/DAILY-APPROVALS.md (or --root/<...>). Channels: --notify-macos
(local osascript), --channel email|slack (OFF unless explicitly configured AND
opted in — sending crosses the external boundary, so it refuses without both).

Usage:
  approval_digest.py                       # print digest to stdout
  approval_digest.py --write               # also write DAILY-APPROVALS.md
  approval_digest.py --write --notify-macos
  approval_digest.py --json                # machine-readable
  options: --codex-home PATH  --channel email|slack
"""
from __future__ import annotations

import argparse
import datetime as _dt
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

APPROVAL_FILE = "human-approval.md"
SAFE_RISKS = {"low"}
JUDGMENT_RISKS = {"medium", "high"}
# Unknown-risk items only count as "safe to batch" when they clearly match a
# low-stakes housekeeping decision; everything else defaults to needs-judgment.
SAFE_HINTS = re.compile(
    r"(gitignore|\.serena|ignore[- ]policy|stale branch|delete[^.]*branch|"
    r"prune|branch cleanup|untracked.*(metadata|tool)|tool metadata)", re.I)
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
            items.append({"ask": cb.group(1).strip(), "risk": "unknown"})
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
                items.append({"ask": txt, "risk": "unknown"})
    flush()
    return items, malformed


def _infer_risk(item: dict) -> str:
    cls = (item.get("classification") or "").lower()
    if "high" in cls or "secret" in cls or "deploy" in cls:
        return "high"
    return "unknown"


def classify(item: dict) -> str:
    risk = (item.get("risk") or "unknown").lower()
    if risk in JUDGMENT_RISKS:
        return "judgment"
    blob = " ".join(str(item.get(k, "")) for k in ("ask", "classification", "action"))
    if risk == "low" or SAFE_HINTS.search(blob):
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


def _fmt_item(it: dict, today: _dt.date) -> str:
    age = age_days(it, today)
    age_s = f"{age}d old" if age is not None else "age unknown"
    fs = f", first seen {it['first_seen']}" if it.get("first_seen") else ""
    lines = [f"### {it['project']} — {it['ask']}",
             f"- risk: {it.get('risk', 'unknown')}  ({age_s}{fs})"]
    if it.get("suggested_default"):
        lines.append(f"- suggested default: {it['suggested_default']}")
    if it.get("action"):
        lines.append(f"- action: {it['action']}")
    if it.get("evidence"):
        lines.append(f"- evidence: {it['evidence']}")
    lines.append(f"- source: {it['source']} ({it['agent']})")
    return "\n".join(lines)


def build_digest(items: list[dict], malformed: list[tuple[str, str]],
                 today: _dt.date, resolved: int = 0) -> str:
    judgment = [i for i in items if classify(i) == "judgment"]
    safe = [i for i in items if classify(i) == "safe"]
    key = lambda i: (age_days(i, today) is None, -(age_days(i, today) or 0))
    judgment.sort(key=key)
    safe.sort(key=key)
    projects = sorted({i["project"] for i in items})
    note = f" ({resolved} resolved note(s) filtered)" if resolved else ""
    out = [f"# Daily approvals — {today.isoformat()}",
           f"Read-only digest of pending human decisions. "
           f"{len(items)} item(s) across {len(projects)} project(s): "
           f"{len(judgment)} need judgment, {len(safe)} safe to batch-approve.{note}",
           ""]
    out.append(f"## Needs judgment ({len(judgment)})")
    out += [_fmt_item(i, today) + "\n" for i in judgment] or ["_none_\n"]
    out.append(f"## Safe to batch-approve ({len(safe)})")
    out += [_fmt_item(i, today) + "\n" for i in safe] or ["_none_\n"]
    if malformed:
        out.append(f"## Needs cleanup ({len(malformed)})")
        out += [f"- {src}: {why}" for src, why in malformed]
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


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Cross-project approval digest (P9).")
    ap.add_argument("--write", action="store_true",
                    help="write DAILY-APPROVALS.md (default: print only)")
    ap.add_argument("--notify-macos", action="store_true",
                    help="post a local macOS notification with the counts")
    ap.add_argument("--channel", choices=["email", "slack"], default=None,
                    help="external delivery (OFF unless configured + opted in)")
    ap.add_argument("--codex-home", default=None, help="override ~/.codex")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    today = _dt.date.today()
    items: list[dict] = []
    malformed: list[tuple[str, str]] = []
    for agent, project, job_id, path in iter_queue_files(args.codex_home):
        try:
            parsed, bad = parse_queue(path.read_text(encoding="utf-8"))
        except OSError as e:
            malformed.append((job_id, f"unreadable: {e}"))
            continue
        for it in parsed:
            it.update({"project": project, "agent": agent, "source": job_id})
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
            for k in ("_ask", "_raw_heading", "_fields"):
                it.pop(k, None)
        print(json.dumps({"date": today.isoformat(), "items": items,
                          "malformed": malformed}, indent=2))
        return 0

    digest = build_digest(items, malformed, today, resolved)
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
