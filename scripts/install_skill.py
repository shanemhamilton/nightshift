#!/usr/bin/env python3
"""
install_skill.py — install this skill into each coding agent's skills directory.

Copies (or symlinks) the whole automation-optimizer skill folder into the right
location for every agent detected on this machine:
    Codex      -> ~/.codex/skills/automation-optimizer
    Claude Code-> ~/.claude/skills/automation-optimizer
    Gemini CLI -> ~/.gemini/skills/automation-optimizer   (scripts only; see note)
    Cursor     -> (no global skills dir; skipped)

It only installs for agents whose home dir exists, is idempotent (re-running
updates in place), and backs up any existing install first. Locations come from
scripts/agent_adapters.py, so there's one source of truth.

Usage:
    python3 install_skill.py                 # install for all detected agents
    python3 install_skill.py --dry-run       # show what would happen, write nothing
    python3 install_skill.py --agents claude,codex
    python3 install_skill.py --link          # symlink instead of copy (dev mode)
    python3 install_skill.py --home /tmp/h   # override home (testing)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
SKILL_SRC = _HERE.parent                       # the automation-optimizer/ folder
SKILL_NAME = SKILL_SRC.name

_spec = importlib.util.spec_from_file_location("agent_adapters", _HERE / "agent_adapters.py")
AA = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AA)  # type: ignore

IGNORE = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*.bak.*",
                                ".DS_Store", "*.egg-info")


def expand(path: str, home: Path) -> Path:
    return Path(str(home) + path[1:]) if path.startswith("~") else Path(path)


def _repo_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(SKILL_SRC), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def write_install_info(dest: Path) -> None:
    """Record what was installed (copy mode only) so discovery can spot drift."""
    version_file = SKILL_SRC / "VERSION"
    version = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else "unknown"
    info = {
        "version": version,
        "commit": _repo_commit(),
        "installed_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (dest / "INSTALL-INFO.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")


def install_one(agent: str, cfg: dict, home: Path, *, link: bool, dry: bool) -> dict:
    res = {"agent": agent, "label": cfg["label"], "action": "", "dest": "", "note": ""}
    skills_dir = cfg.get("skills_dir")
    if not skills_dir:
        res["action"] = "skip"
        res["note"] = "no global skills directory for this agent"
        return res
    home_marker = expand(cfg.get("home_dir", skills_dir.rsplit("/", 1)[0]), home)
    if not home_marker.exists():
        res["action"] = "skip"
        res["note"] = f"agent not installed ({home_marker} missing)"
        return res

    dest_root = expand(skills_dir, home)
    dest = dest_root / SKILL_NAME
    res["dest"] = str(dest)

    if dry:
        res["action"] = "would-link" if link else "would-install"
        return res

    dest_root.mkdir(parents=True, exist_ok=True)
    # Back up an existing install (unless it's our own symlink we're refreshing).
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink():
            dest.unlink()
        else:
            bak = dest.with_name(dest.name + ".bak." +
                                 _dt.datetime.now().strftime("%Y%m%dT%H%M%S"))
            shutil.move(str(dest), str(bak))
            res["note"] = f"backed up prior install -> {bak.name}"

    if link:
        # Symlink mode: dest points INTO the live repo, so writing
        # INSTALL-INFO.json there would pollute the source tree itself.
        # Discovery falls back to reading dest/VERSION (which is live) instead.
        dest.symlink_to(SKILL_SRC, target_is_directory=True)
        res["action"] = "linked"
    else:
        shutil.copytree(SKILL_SRC, dest, ignore=IGNORE)
        res["action"] = "installed"
        write_install_info(dest)

    # verify key files landed
    must = ["SKILL.md", "scripts/discover_agents.py",
            "scripts/optimize_codex_automations.py", "scripts/agent_adapters.py"]
    if not link:
        must = must + ["INSTALL-INFO.json"]
    missing = [m for m in must if not (dest / m).exists()]
    if missing:
        res["action"] = "error"
        res["note"] = f"missing after install: {', '.join(missing)}"
    return res


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Install the automation-optimizer skill "
                                             "into each coding agent.")
    ap.add_argument("--agents", default=None, help="comma-separated subset (default: all)")
    ap.add_argument("--dry-run", action="store_true", help="show plan, write nothing")
    ap.add_argument("--link", action="store_true", help="symlink instead of copy (dev)")
    ap.add_argument("--home", default=None, help="override home dir (testing)")
    args = ap.parse_args(argv)

    home = Path(args.home).expanduser() if args.home else Path.home()
    agents = args.agents.split(",") if args.agents else list(AA.ADAPTERS)

    mode = "DRY RUN" if args.dry_run else ("LINK" if args.link else "COPY")
    print(f"Installing '{SKILL_NAME}' from {SKILL_SRC}\n  home={home}  mode={mode}\n")

    results = [install_one(a, AA.ADAPTERS[a], home, link=args.link, dry=args.dry_run)
               for a in agents if a in AA.ADAPTERS]

    installed = [r for r in results if r["action"] in ("installed", "linked",
                                                        "would-install", "would-link")]
    skipped = [r for r in results if r["action"] == "skip"]
    errors = [r for r in results if r["action"] == "error"]

    for r in results:
        line = f"  [{r['action']:13}] {r['label']:16}"
        if r["dest"]:
            line += f" -> {r['dest']}"
        if r["note"]:
            line += f"   ({r['note']})"
        print(line)

    print(f"\n{len(installed)} target(s), {len(skipped)} skipped, {len(errors)} error(s).")
    if not args.dry_run and installed and not errors:
        print("\nTry it:")
        for r in installed:
            print(f"  python3 {r['dest']}/scripts/discover_agents.py")
            break
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
