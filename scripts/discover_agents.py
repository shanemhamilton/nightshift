#!/usr/bin/env python3
"""
discover_agents.py — read-only cross-agent inventory of recurring automations.

Scans the registries of every supported agent (Codex, Claude Code, Gemini CLI,
Cursor) and reports what's installed, whether each automation carries the
Automation Optimizer protocol block, which canonical-instruction files exist, and
where the scheduler is native vs external/cloud. Writes nothing.

Run on the machine that hosts the agents:
    python3 discover_agents.py
    python3 discover_agents.py --home /path/to/fake/home   # for testing
    python3 discover_agents.py --agent codex,claude         # subset
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("agent_adapters", _HERE / "agent_adapters.py")
AA = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AA)  # type: ignore

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None


def expand(path: str | None, home: Path) -> Path | None:
    if not path:
        return None
    if path.startswith("~"):
        return Path(str(home) + path[1:])
    return Path(path)


def detect_block(text: str, cfg: dict) -> tuple[bool, int | None]:
    begin, _end = cfg["protocol_markers"]
    if begin not in text:
        return (False, None)
    m = re.search(cfg["protocol_version_re"], text)
    return (True, int(m.group(1)) if m else 0)


def suite_index(root: Path) -> dict:
    """Read <root>/suite.toml if present → {job_id: {project, workspace}}.

    The scaffolder copies the manifest next to the jobs, so this lets discovery
    label each job with the project/workspace it belongs to — the quickest way to
    spot a generic or stale job that drifted in from another suite.
    """
    if tomllib is None:
        return {}
    sp = root / "suite.toml"
    if not sp.is_file():
        return {}
    try:
        data = tomllib.loads(sp.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    suite = data.get("suite", {})
    meta = {"project": suite.get("project"), "workspace": suite.get("workspace")}
    return {j.get("id"): meta for j in data.get("job", []) if j.get("id")}


def codex_meta(job_file: Path) -> dict:
    """Pull (name, workspace) straight from a Codex automation.toml when possible."""
    if tomllib is None or job_file.suffix != ".toml":
        return {}
    try:
        data = tomllib.loads(job_file.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}
    cwds = data.get("cwds")
    workspace = cwds[0] if isinstance(cwds, list) and cwds else None
    return {"name": data.get("name"), "workspace": workspace}


def prompt_text_for(job_file: Path, cfg: dict) -> str:
    """Return the text to scan for a protocol block, per agent format."""
    raw = job_file.read_text(encoding="utf-8", errors="replace")
    key = cfg["prompt_key"]
    if key and tomllib and job_file.suffix == ".toml":
        try:
            val = tomllib.loads(raw).get(key)
            if isinstance(val, str):
                return val
        except Exception:
            return raw
    return raw  # SKILL.md / prompt.md: the body itself


def scan_agent(agent: str, cfg: dict, home: Path) -> dict:
    out = {"agent": agent, "label": cfg["label"], "scheduler": cfg["scheduler"],
           "jobs": [], "canonical": [], "note": ""}

    # canonical instruction files present
    for c in cfg["canonical_instructions"]:
        p = expand(c, home) if c.startswith("~") else (home / c)
        # bare names like AGENTS.md/CLAUDE.md are checked at home root
        if p.exists():
            out["canonical"].append(str(p))

    root = expand(cfg["automations_root"], home)
    if cfg["job_layout"] == "cloud":
        out["note"] = ("cloud automations (dashboard/API) — not inspectable locally; "
                       "local artifacts are project rules only")
        return out
    if root is None or not root.is_dir():
        out["note"] = (f"no local registry at {root} "
                       f"({'cron-driven' if cfg['scheduler']=='external_cron' else 'none found'})")
        return out

    idx = suite_index(root)
    job_file_name = Path(cfg["automations_glob"]).name if cfg["automations_glob"] else None
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        jf = d / job_file_name if job_file_name else None
        if not jf or not jf.is_file():
            # directory without the expected job file = orphan
            out["jobs"].append({"id": d.name, "block": None, "version": None,
                                "orphan": True})
            continue
        has, ver = detect_block(prompt_text_for(jf, cfg), cfg)
        missing_sidecars = [s for s in cfg["sidecars"] if not (d / s).is_file()]
        meta = idx.get(d.name, {})
        cmeta = codex_meta(jf) if agent == "codex" else {}
        out["jobs"].append({
            "id": d.name, "block": has, "version": ver, "orphan": False,
            "missing_sidecars": missing_sidecars,
            "project": meta.get("project"),
            "workspace": cmeta.get("workspace") or meta.get("workspace"),
            "name": cmeta.get("name"),
        })
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Cross-agent automation inventory (read-only).")
    ap.add_argument("--home", default=None, help="override home dir (for testing)")
    ap.add_argument("--agent", default=None, help="comma-separated subset of agents")
    args = ap.parse_args(argv)
    home = Path(args.home).expanduser() if args.home else Path.home()
    agents = args.agent.split(",") if args.agent else list(AA.ADAPTERS)

    print(f"Cross-agent automation inventory — home={home}\n")
    grand = 0
    for agent in agents:
        cfg = AA.ADAPTERS.get(agent)
        if not cfg:
            print(f"[{agent}] unknown agent\n")
            continue
        r = scan_agent(agent, cfg, home)
        print(f"=== {r['label']} ({agent}) — scheduler: {r['scheduler']} ===")
        if r["note"]:
            print(f"  {r['note']}")
        if r["jobs"]:
            for j in r["jobs"]:
                grand += 1
                if j["orphan"]:
                    print(f"  - {j['id']:42} ORPHAN (no job file)")
                else:
                    blk = (f"protocol v{j['version']}" if j["block"] else "NO protocol block")
                    sc = "" if not j["missing_sidecars"] else \
                        f"  missing: {', '.join(j['missing_sidecars'])}"
                    print(f"  - {j['id']:42} {blk}{sc}")
                    meta = []
                    if j.get("project"):
                        meta.append(f"project={j['project']}")
                    if j.get("workspace"):
                        meta.append(f"ws={j['workspace']}")
                    if j.get("name") and j["name"] != j["id"]:
                        meta.append(f"name={j['name']}")
                    if meta:
                        print(f"      {'  '.join(meta)}")
                    else:
                        print("      (no suite metadata — generic/standalone job)")
        elif not r["note"]:
            print("  (none)")
        if r["canonical"]:
            print(f"  canonical instructions: {', '.join(r['canonical'])}")
        print()
    print(f"Total automations found across agents: {grand}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
