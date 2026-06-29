#!/usr/bin/env python3
"""
lifecycle.py — manage a single automation across its whole life, on any agent.

Four verbs, one front door:
  setup   profile a project and stand up its whole suite (propose → confirm → install)
  add     add one pattern (P1..P8) to an existing suite
  remove  retire a job — DISABLE by default (reversible), --purge to archive+delete
  update  change a job's schedule / params / scope / mode / model, re-gated

Every verb is the same pipeline: mutate the suite.toml manifest → validate the
fleet → gate on approval → materialize into each target agent's registry. The
manifest is the agent-neutral source of truth; --agent {codex,claude,gemini,
cursor,all} chooses where jobs land, resolved through agent_adapters.

Default is a DRY RUN that writes nothing. --apply persists the manifest edit,
stamps the approval fingerprint for the affected job, and materializes it — the
human running --apply IS the confirmation gate.

Safety: writing active jobs requires --apply. remove never deletes by default.
Across agents the "exactly one merge authority" rule still holds: only the
designated merge agent keeps an active integrator; others get it in shadow mode.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import os
import sys
import tempfile
from pathlib import Path


def _load(mod_name: str, filename: str):
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    return mod


PP = _load("ao_profile", "profile_project.py")
OPT = _load("ao_opt", "optimize_codex_automations.py")
MAT = _load("ao_mat", "agent_materializers.py")
ADAPTERS = MAT.agent_adapters.ADAPTERS
ALL_AGENTS = list(ADAPTERS.keys())
tomllib = OPT.tomllib


# --- manifest io -------------------------------------------------------------
def load_suite(path: Path) -> tuple[dict, list[dict]]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return data.get("suite", {}), data.get("job", [])


def save_suite(path: Path, suite: dict, jobs: list[dict], header: str = "") -> None:
    if path.exists():
        backup = path.with_suffix(path.suffix + f".bak.{OPT.timestamp()}")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PP.serialize_suite(suite, jobs, header=header), encoding="utf-8")


def validate(suite: dict, jobs: list[dict]) -> int:
    """Run the real fleet validator against the proposed manifest state."""
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
        fh.write(PP.serialize_suite(suite, jobs))
        tmp = Path(fh.name)
    try:
        return OPT.fleet(tmp, require_approved=False)
    finally:
        tmp.unlink(missing_ok=True)


def stamp_approval(job: dict, by: str) -> None:
    job["approved_by"] = by
    job["approved_at"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    job["approved_fingerprint"] = PP.compute_fingerprint(job)


def find_job(jobs: list[dict], jid: str) -> dict | None:
    return next((j for j in jobs if j.get("id") == jid), None)


def integrator_id(jobs: list[dict]) -> str | None:
    j = next((j for j in jobs if j.get("phase") == "integrator"), None)
    return j.get("id") if j else None


# --- materialize across agents ----------------------------------------------
def targets(agent: str) -> list[str]:
    return ALL_AGENTS if agent == "all" else [agent]


def materialize_job(agents: list[str], job: dict, *, cwd: str, model: str | None,
                    apply: bool, home_override: str | None) -> None:
    """Materialize one job onto each agent, enforcing one real merge authority."""
    merge_agent = agents[0]  # the designated keeper of merge authority
    for agent in agents:
        j = dict(job)
        if j.get("merge_authority") and agent != merge_agent:
            j["mode"] = "shadow"  # cross-agent guard: only one active integrator
        res = MAT.materialize(agent, j, cwd=cwd, model=model, apply=apply,
                              home_override=home_override)
        _report(agent, j["id"], res)


def retire_job(agents: list[str], job: dict, *, purge: bool, apply: bool,
               home_override: str | None) -> None:
    for agent in agents:
        fn = MAT.purge if purge else MAT.disable
        res = fn(agent, job, apply=apply, home_override=home_override)
        _report(agent, job["id"], res)


def _report(agent: str, jid: str, res: dict) -> None:
    line = f"  [{agent:6}] {jid:22} {res['action']:9}"
    if res.get("path"):
        line += f" -> {res['path']}"
    print(line)
    if res.get("notes"):
        print(f"           note: {res['notes']}")
    if res.get("emit"):
        print("           " + res["emit"].replace("\n", "\n           "))


# --- param parsing -----------------------------------------------------------
def parse_value(raw: str):
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        return raw


def parse_params(pairs: list[str] | None) -> dict:
    out: dict = {}
    for p in pairs or []:
        if "=" not in p:
            sys.exit(f"bad --param '{p}', expected key=value")
        k, v = p.split("=", 1)
        out[k] = parse_value(v)
    return out


# --- verbs -------------------------------------------------------------------
def cmd_setup(args) -> int:
    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}")
        return 2
    caps = PP.detect(root)
    jobs, skipped = PP.decide(caps)
    suite = {"project": root.name, "workspace": str(root), "state_dir": "state"}
    print(PP.rationale_text(caps, jobs, skipped) + "\n")
    if validate(suite, jobs) != 0:
        print("\nProposed suite is not internally consistent; not installing.")
        return 1
    suite_path = Path(args.suite).expanduser() if args.suite else root / "suite.toml"
    if not args.apply:
        print(f"\nDry run. Re-run with --apply to write {suite_path} and install "
              f"into: {', '.join(targets(args.agent))}.")
        return 0
    for j in jobs:
        stamp_approval(j, args.by)
    save_suite(suite_path, suite, jobs, header="# Installed by lifecycle.py setup.")
    print(f"\nWrote {suite_path}. Materializing {len(jobs)} job(s):")
    for j in jobs:
        materialize_job(targets(args.agent), j, cwd=suite["workspace"],
                        model=args.model, apply=True, home_override=args.home_override)
    return 0


def cmd_add(args) -> int:
    suite_path = Path(args.suite).expanduser()
    if not suite_path.is_file():
        print(f"No such suite: {suite_path}")
        return 2
    suite, jobs = load_suite(suite_path)
    workspace = args.workspace or suite.get("workspace")
    if not workspace:
        print("No workspace: pass --workspace or set [suite].workspace.")
        return 2
    caps = PP.detect(Path(workspace).expanduser())
    proposed, skipped = PP.decide(caps)
    cand = next((j for j in proposed if j.get("template") == args.pattern), None)
    if cand is None:
        reason = next((r for pid, r in skipped if pid == args.pattern), "capability missing")
        print(f"Cannot add {args.pattern}: {reason}.")
        return 1
    if args.id:
        cand["id"] = args.id
    if find_job(jobs, cand["id"]):
        print(f"Job id '{cand['id']}' already exists in the suite.")
        return 1
    if cand.get("phase") == "producer":
        integ = integrator_id(jobs)
        if integ:
            cand["hands_off_to"] = integ
    cand.setdefault("params", {}).update(parse_params(args.param))
    jobs.append(cand)
    if validate(suite, jobs) != 0:
        print("\nAdding this job breaks the fleet; not written.")
        return 1
    if not args.apply:
        print(f"\nDry run. Re-run with --apply to add '{cand['id']}' "
              f"({args.pattern}) and install it.")
        return 0
    stamp_approval(cand, args.by)
    save_suite(suite_path, suite, jobs, header="# Job added by lifecycle.py add.")
    print(f"\nAdded '{cand['id']}'. Materializing:")
    materialize_job(targets(args.agent), cand, cwd=workspace, model=args.model,
                    apply=True, home_override=args.home_override)
    return 0


def cmd_update(args) -> int:
    suite_path = Path(args.suite).expanduser()
    if not suite_path.is_file():
        print(f"No such suite: {suite_path}")
        return 2
    suite, jobs = load_suite(suite_path)
    job = find_job(jobs, args.id)
    if job is None:
        print(f"No job '{args.id}' in {suite_path}.")
        return 1
    if args.schedule:
        job["schedule"] = args.schedule
    if args.scope:
        job["write_scope"] = args.scope
    if args.mode:
        job["mode"] = args.mode
    if args.model:
        job["model"] = args.model
    if args.param:
        job.setdefault("params", {}).update(parse_params(args.param))
    stale = job.get("approved_fingerprint") != PP.compute_fingerprint(job)
    if validate(suite, jobs) != 0:
        print("\nUpdate breaks the fleet; not written.")
        return 1
    if not args.apply:
        flag = " (safety-relevant: needs re-approval)" if stale else ""
        print(f"\nDry run{flag}. Re-run with --apply to confirm and re-install "
              f"'{args.id}'.")
        return 0
    stamp_approval(job, args.by)
    save_suite(suite_path, suite, jobs, header="# Job updated by lifecycle.py update.")
    workspace = suite.get("workspace", ".")
    print(f"\nUpdated '{args.id}'. Re-materializing:")
    materialize_job(targets(args.agent), job, cwd=workspace, model=args.model,
                    apply=True, home_override=args.home_override)
    return 0


def cmd_remove(args) -> int:
    suite_path = Path(args.suite).expanduser()
    if not suite_path.is_file():
        print(f"No such suite: {suite_path}")
        return 2
    suite, jobs = load_suite(suite_path)
    job = find_job(jobs, args.id)
    if job is None:
        print(f"No job '{args.id}' in {suite_path}.")
        return 1
    remaining = [j for j in jobs if j.get("id") != args.id]
    dependents = [j for j in remaining if j.get("phase") in ("producer", "janitor")]
    if job.get("phase") == "integrator" and dependents and not args.reassign:
        print(f"Refusing to remove integrator '{args.id}': {len(dependents)} "
              f"producer/janitor job(s) still depend on it. Add another integrator "
              f"and pass --reassign <id>, or remove the dependents first.")
        return 1
    if args.reassign:
        if not find_job(remaining, args.reassign):
            print(f"--reassign '{args.reassign}' is not an existing job.")
            return 1
        for j in remaining:
            if j.get("phase") == "producer":
                j["hands_off_to"] = args.reassign

    if args.purge:
        new_jobs = remaining
    else:
        job["mode"] = "disabled"  # reflect the disable in the manifest
        new_jobs = jobs
    if validate(suite, new_jobs) != 0:
        print("\nRemoval would break the fleet; not written.")
        return 1
    action = "purge (archive + delete)" if args.purge else "disable (reversible)"
    if not args.apply:
        print(f"\nDry run — would {action} '{args.id}'. Re-run with --apply.")
        return 0
    save_suite(suite_path, suite, new_jobs, header="# Job retired by lifecycle.py remove.")
    print(f"\n{action} '{args.id}':")
    retire_job(targets(args.agent), job, purge=args.purge, apply=True,
               home_override=args.home_override)
    return 0


# --- cli ---------------------------------------------------------------------
def _common(p, *, needs_suite=True):
    p.add_argument("--agent", default="codex", choices=ALL_AGENTS + ["all"],
                   help="target agent (default: codex)")
    p.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    p.add_argument("--by", default=os.environ.get("USER", "unknown"), help="approver name")
    p.add_argument("--model", default=None, help="model to set on the job (optional)")
    p.add_argument("--home-override", default=None,
                   help="redirect the agent home dir (testing)")
    if needs_suite:
        p.add_argument("--suite", required=True, help="path to suite.toml")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Manage an automation across its lifecycle.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="profile a project and install its suite")
    s.add_argument("path", nargs="?", default=".", help="project dir (default: .)")
    s.add_argument("--suite", default=None, help="suite.toml output (default: <repo>/suite.toml)")
    _common(s, needs_suite=False)
    s.set_defaults(func=cmd_setup)

    a = sub.add_parser("add", help="add one pattern (P1..P8) to a suite")
    a.add_argument("--pattern", required=True, choices=sorted(OPT.KNOWN_TEMPLATES))
    a.add_argument("--id", default=None, help="override the job id")
    a.add_argument("--workspace", default=None, help="project dir (default: [suite].workspace)")
    a.add_argument("--param", action="append", help="param override key=value (repeatable)")
    _common(a)
    a.set_defaults(func=cmd_add)

    u = sub.add_parser("update", help="change a job's schedule/params/scope/mode/model")
    u.add_argument("--id", required=True, help="job id to update")
    u.add_argument("--schedule", default=None, help="new 5-field cron schedule")
    u.add_argument("--scope", action="append", help="replace write_scope (repeatable)")
    u.add_argument("--mode", default=None, choices=["active", "shadow", "disabled"])
    u.add_argument("--param", action="append", help="param override key=value (repeatable)")
    _common(u)
    u.set_defaults(func=cmd_update)

    r = sub.add_parser("remove", help="disable (default) or --purge a job")
    r.add_argument("--id", required=True, help="job id to remove")
    r.add_argument("--purge", action="store_true", help="archive state then delete the dir")
    r.add_argument("--reassign", default=None, help="integrator id to hand producers to")
    _common(r)
    r.set_defaults(func=cmd_remove)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
