#!/usr/bin/env python3
"""
scaffold_suite.py — turn an APPROVED suite.toml into runnable Codex automations.

This is the last mile of composer mode: it materializes each job in the manifest
as a real <codex-home>/automations/<id>/automation.toml whose prompt is the
shared managed block (imported from optimize_codex_automations.py, so it never
drifts) followed by that pattern's adaptive task body, and it creates the sidecar
state files. In a Codex environment, an automation.toml under the automations
directory IS the registration; its `schedule` field carries the cadence.

Safety:
  * Only jobs that are APPROVED & CURRENT (fingerprint matches) are scaffolded.
    Pending or stale jobs block the install until re-approved (override with
    --allow-unapproved, which you should not normally do).
  * Default is a DRY RUN that writes nothing. Writing active nightly automations
    into ~/.codex is a system change, so it requires the explicit --install flag.
  * Every generated file is re-parsed; the prompt must round-trip and contain all
    optimizer anchors or the job is reported as an error and not left half-written.

Usage:
  scaffold_suite.py --suite <suite.toml>                 # dry run: show the plan
  scaffold_suite.py --suite <suite.toml> --install       # write into $CODEX_HOME
  options: --codex-home PATH  --cwd PATH  --model NAME  --allow-unapproved
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

# Import the optimizer module so the managed block, fingerprint, and sidecar
# templates are shared verbatim (single source of truth, no drift).
_OPT_PATH = Path(__file__).resolve().parent / "optimize_codex_automations.py"
_spec = importlib.util.spec_from_file_location("ao_opt", _OPT_PATH)
OPT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(OPT)  # type: ignore

tomllib = OPT.tomllib
REQUIRED_SECTIONS = OPT.REQUIRED_SECTIONS

# Adaptive template bodies + Codex emitter live in agent_materializers so this
# scaffolder and lifecycle.py share one copy and never drift.
import agent_materializers as MAT

DEFAULT_INTEGRATOR = MAT.DEFAULT_INTEGRATOR
DEFAULTS = MAT.DEFAULTS
BODIES = MAT.BODIES
build_prompt = MAT.build_prompt
cron_to_rrule = MAT.cron_to_rrule
emit_job_toml = MAT.emit_codex_toml

def approval_state(job: dict) -> str:
    approved = job.get("approved_fingerprint")
    if not approved:
        return "pending"
    return "current" if approved == OPT.compute_fingerprint(job) else "stale"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Scaffold runnable automations from an "
                                             "approved suite.toml.")
    ap.add_argument("--suite", required=True, help="path to an approved suite.toml")
    ap.add_argument("--install", action="store_true",
                    help="actually write into <codex-home> (default: dry run)")
    ap.add_argument("--codex-home", default=None, help="override $CODEX_HOME / ~/.codex")
    ap.add_argument("--cwd", default=None, help="override job cwd (default: suite workspace)")
    ap.add_argument("--model", default=None, help="model name to set on each job (optional)")
    ap.add_argument("--allow-unapproved", action="store_true",
                    help="scaffold pending/stale jobs too (not recommended)")
    args = ap.parse_args(argv)

    suite_path = Path(args.suite).expanduser()
    if not suite_path.is_file():
        print(f"No such suite file: {suite_path}")
        return 2
    data = tomllib.loads(suite_path.read_text(encoding="utf-8"))
    suite = data.get("suite", {})
    jobs = data.get("job", [])
    if not jobs:
        print("Manifest has no [[job]] entries.")
        return 2

    cwd = args.cwd or suite.get("workspace")
    if not cwd:
        print("No cwd: manifest has no [suite].workspace and --cwd not given.")
        return 2

    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) \
        if not args.codex_home else Path(args.codex_home).expanduser()
    autos = home / "automations"

    # Gate on approval
    blockers = [(j.get("id"), approval_state(j)) for j in jobs
                if approval_state(j) != "current"]
    if blockers and not args.allow_unapproved:
        print("Refusing to scaffold — these jobs are not approved & current:")
        for jid, st in blockers:
            print(f"  - {jid}: {st}")
        print("\nApprove first:  profile_project.py approve --suite "
              f"{suite_path}\n(or pass --allow-unapproved to override, not recommended).")
        return 1

    action = "WRITE" if args.install else "DRY RUN (nothing written)"
    print(f"Scaffold suite '{suite.get('project','?')}' → {autos}  [{action}]\n")

    written, errors = [], []
    for j in jobs:
        jid = j.get("id")
        job_dir = autos / jid
        toml_path = job_dir / "automation.toml"
        try:
            prompt = build_prompt(j, str(job_dir))
            text = emit_job_toml(j, prompt, cwd, args.model)
        except (KeyError, ValueError) as e:
            errors.append((jid, str(e)))
            continue

        auth = " [merge authority]" if j.get("merge_authority") else ""
        print(f"  {jid:22} {j.get('phase','?'):11} mode={j.get('mode','active'):7}"
              f" -> {toml_path}{auth}")
        if not args.install:
            continue

        job_dir.mkdir(parents=True, exist_ok=True)
        toml_path.write_text(text, encoding="utf-8")
        # verify: parses, prompt round-trips, all required protocol sections present
        check = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        cp = check.get("prompt", "")
        missing = [s for s in REQUIRED_SECTIONS if s not in cp]
        if cp.strip() != prompt.strip() or missing:
            errors.append((jid, f"verification failed (missing sections: {missing})"))
            continue
        created: list[str] = []
        OPT.scaffold_sidecars(job_dir, created)
        written.append(jid)

    if args.install:
        # place the manifest alongside the jobs so --fleet can find it
        autos.mkdir(parents=True, exist_ok=True)
        (autos / "suite.toml").write_text(suite_path.read_text(encoding="utf-8"),
                                          encoding="utf-8")

    print()
    if args.install:
        print(f"Installed {len(written)} job(s): {', '.join(written)}")
        print(f"Manifest copied to {autos / 'suite.toml'}")
    if errors:
        print(f"Errors ({len(errors)}):")
        for jid, why in errors:
            print(f"  - {jid}: {why}")
    if not args.install:
        print("Dry run only. Re-run with --install to write these automations.")
    else:
        print("\nVerify:")
        print(f"  optimize_codex_automations.py --codex-home {home} --strict")
        print(f"  optimize_codex_automations.py --fleet {autos / 'suite.toml'} --require-approved")
        print("Your scheduler picks up automation.toml files under "
              f"{autos}; confirm they appear and are enabled.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
