#!/usr/bin/env python3
"""
profile_project.py — the executable composer side of the Automation Optimizer.

Two subcommands:

  profile  Read-only capability detection on a project, then emit a draft
           suite.toml (the coordinated automation suite) plus a plain-English
           rationale. Picks only the patterns whose capabilities are present and
           degrades the rest to "skipped, with reason" — never guesses commands.
           The suite is always structurally valid for the fleet validator
           (exactly one merge authority, ordered phases, producers hand off).

  approve  Stamp approval fingerprints into a suite.toml after a human has
           confirmed it. Implements the "confirm-once, then autonomous" gate.

Safety: `profile` does not modify the repo. It only reads files, checks which
tools are on PATH, and runs a couple of read-only `git` queries. It writes a
suite.toml only when you pass --out. `approve` writes only to the manifest.

Detection is by capability presence (files, config, tools on PATH) — it never
executes test/scan commands; running those is the automation's own runtime job.

Pairs with optimize_codex_automations.py (same fingerprint definition; keep the
FINGERPRINT_FIELDS list and BLOCK-independent format in lockstep across both).
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import naming

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        sys.exit("Needs Python 3.11+ (tomllib) or `pip install tomli`.")

TEMPLATE_VERSION = 2

# Phase schedules (cron) — keep producer < integrator < janitor < reflector.
PHASE_SCHEDULE = {
    "producer": "0 1 * * *",
    "integrator": "0 3 * * *",
    "janitor": "0 4 * * *",
    "reflector": "0 5 * * *",
}

# Must match optimize_codex_automations.py exactly.
FINGERPRINT_FIELDS = (
    "template", "template_version", "merge_authority",
    "write_scope", "phase", "schedule", "params",
)

INTEGRATOR_ID = "repo-hygiene"

# Per-stack source scopes used to give producers a meaningful write_scope.
STACK_SCOPES = {
    "node": ["src/**", "lib/**", "test/**", "tests/**", "__tests__/**"],
    "swift/ios": ["Sources/**", "Tests/**", "**/*.swift"],
    "python": ["src/**", "tests/**", "**/*.py"],
    "ruby": ["app/**", "lib/**", "spec/**"],
    "go": ["**/*.go"],
    "rust": ["src/**", "tests/**"],
    "java/kotlin": ["src/**"],
}


# --- small read-only helpers -------------------------------------------------
def _exists(root: Path, *names: str) -> str | None:
    for n in names:
        if (root / n).exists():
            return n
    return None


def _glob_any(root: Path, *patterns: str) -> str | None:
    for pat in patterns:
        for m in root.glob(pat):
            return m.name
    return None


def _which(name: str) -> bool:
    return shutil.which(name) is not None


def _git(root: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


# --- detection ---------------------------------------------------------------
def detect(root: Path) -> dict:
    caps: dict = {"evidence": {}}

    # git + default branch
    is_git = (root / ".git").exists() or _git(root, "rev-parse", "--is-inside-work-tree") == "true"
    caps["git"] = is_git
    branch = None
    if is_git:
        head = _git(root, "symbolic-ref", "refs/remotes/origin/HEAD")
        if head:
            branch = head.rsplit("/", 1)[-1]
            caps["evidence"]["default_branch"] = f"origin/HEAD → {branch}"
        if not branch:
            branches = (_git(root, "branch", "--format=%(refname:short)") or "").split()
            for cand in ("main", "master"):
                if cand in branches:
                    branch = cand
                    caps["evidence"]["default_branch"] = f"local branch '{cand}'"
                    break
        if not branch:
            branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD") or "main"
            caps["evidence"]["default_branch"] = f"current branch '{branch}'"
    else:
        branch = "main"
        caps["evidence"]["default_branch"] = "no git repo; assumed 'main'"
    caps["default_branch"] = branch

    # stacks
    stacks = []
    stack_checks = [
        ("node", lambda: _exists(root, "package.json")),
        ("swift/ios", lambda: _exists(root, "Package.swift") or _glob_any(root, "*.xcodeproj", "*.xcworkspace")),
        ("python", lambda: _exists(root, "pyproject.toml", "setup.py", "requirements.txt")),
        ("ruby", lambda: _exists(root, "Gemfile")),
        ("go", lambda: _exists(root, "go.mod")),
        ("rust", lambda: _exists(root, "Cargo.toml")),
        ("java/kotlin", lambda: _exists(root, "pom.xml", "build.gradle", "build.gradle.kts")),
    ]
    for name, check in stack_checks:
        ev = check()
        if ev:
            stacks.append(name)
            caps["evidence"][f"stack:{name}"] = ev
    caps["stacks"] = stacks

    # test safety net
    test_dir = _exists(root, "tests", "test", "Tests", "spec", "__tests__")
    caps["test_net"] = bool(test_dir and stacks)
    if test_dir:
        caps["evidence"]["test_net"] = f"{test_dir}/ present"

    # CI gate
    ci = _glob_any(root, ".github/workflows/*.yml", ".github/workflows/*.yaml") \
        or _exists(root, ".gitlab-ci.yml", ".circleci/config.yml")
    caps["ci_gate"] = bool(ci)
    if ci:
        caps["evidence"]["ci_gate"] = ci

    # tracker
    tracker = _which("bd") or bool(_exists(root, ".beads"))
    caps["tracker"] = tracker
    if tracker:
        caps["evidence"]["tracker"] = "beads (bd)"

    # UI driver for P2
    ios_sim = ("swift/ios" in stacks) and _which("xcrun")
    web_e2e = _glob_any(root, "playwright.config.*", "cypress.config.*", "cypress.json") \
        or _exists(root, "e2e")
    caps["ui_driver"] = bool(ios_sim or web_e2e)
    if ios_sim:
        caps["evidence"]["ui_driver"] = "iOS simulator (xcrun + Xcode project)"
    elif web_e2e:
        caps["evidence"]["ui_driver"] = f"web e2e harness ({web_e2e})"

    # security scanner for P7
    lockfile = _exists(root, "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                       "Gemfile.lock", "poetry.lock", "go.sum", "Cargo.lock")
    secret_scan = _which("gitleaks") or _which("trufflehog") or _exists(root, ".gitleaks.toml")
    sast = _which("semgrep") or _exists(root, ".semgrep.yml", ".semgrep")
    scanner_ev = lockfile and f"dependency audit ({lockfile})" or \
        (secret_scan and "secret scanner") or (sast and "SAST (semgrep)")
    caps["security_scanner"] = bool(lockfile or secret_scan or sast)
    if scanner_ev:
        caps["evidence"]["security_scanner"] = scanner_ev

    # linter / complexity signal for P6
    linter = _exists(root, ".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml",
                     "eslint.config.js", "eslint.config.mjs", ".flake8", "ruff.toml",
                     ".ruff.toml", ".rubocop.yml", ".swiftlint.yml", ".golangci.yml",
                     ".golangci.yaml")
    caps["linter"] = bool(linter)
    if linter:
        caps["evidence"]["linter"] = linter

    # source scopes
    scopes: list[str] = []
    for s in stacks:
        scopes.extend(STACK_SCOPES.get(s, []))
    caps["source_scopes"] = sorted(set(scopes)) or ["**"]
    return caps


# --- decision ----------------------------------------------------------------
def _job(jid, template, phase, *, merge_authority=False, write_scope=None,
         hands_off_to=None, mode="active", params=None) -> dict:
    j = {
        "id": jid,
        "template": template,
        "template_version": TEMPLATE_VERSION,
        "phase": phase,
        "merge_authority": merge_authority,
        "schedule": PHASE_SCHEDULE[phase],
        "write_scope": write_scope or ["**"],
    }
    if hands_off_to:
        j["hands_off_to"] = hands_off_to
    j["mode"] = mode
    if params:
        j["params"] = params
    return j


def decide(caps: dict) -> tuple[list[dict], list[tuple[str, str]]]:
    jobs: list[dict] = []
    skipped: list[tuple[str, str]] = []
    src = caps["source_scopes"]

    if not caps["git"]:
        # Without git there is no branch to merge/clean; only the reflectors are safe.
        jobs.append(_job("collab-meta-learner", "P5", "reflector",
                         write_scope=["AGENTS.md", "**/memory.md"],
                         params={"lookback_hours": 24}))
        jobs.append(_job("devenv-reflector", "P8", "reflector",
                         write_scope=["CLAUDE.md", "AGENTS.md", "GEMINI.md",
                                      ".cursor/rules", ".claude/**", "**/memory.md"],
                         params={"lookback_hours": 24, "max_edits": 3}))
        for pid, name in [("P1", "coverage-and-quality ratchet"),
                          ("P2", "product-value loop"), ("P3", "repo-hygiene integrator"),
                          ("P4", "leftover resolver"), ("P6", "code-simplification"),
                          ("P7", "code-security sweep")]:
            skipped.append((pid, f"{name}: not a git repo — nothing to branch/merge/clean"))
        return jobs, skipped

    # producers
    if caps["test_net"]:
        jobs.append(_job("coverage-ratchet", "P1", "producer", write_scope=src,
                         hands_off_to=INTEGRATOR_ID, params={"coverage_floor": 75}))
    else:
        skipped.append(("P1", "coverage-and-quality ratchet: no test runner / coverage "
                              "capability detected"))

    if caps["ui_driver"]:
        jobs.append(_job("product-value-loop", "P2", "producer", write_scope=src,
                         hands_off_to=INTEGRATOR_ID, params={"max_loops": 10}))
    else:
        skipped.append(("P2", "product-value loop: no runnable UI driver (iOS simulator "
                              "or web e2e harness) detected"))

    if caps["test_net"] and caps["linter"]:
        jobs.append(_job("code-simplification", "P6", "producer", write_scope=src,
                         hands_off_to=INTEGRATOR_ID, params={"max_changesets": 5}))
    elif caps["linter"]:
        skipped.append(("P6", "code-simplification: linter present but no test safety net "
                              "to prove behavior-preserving refactors"))
    else:
        skipped.append(("P6", "code-simplification: no complexity signal / linter detected"))

    if caps["security_scanner"]:
        jobs.append(_job("code-security", "P7", "producer", write_scope=["**"],
                         hands_off_to=INTEGRATOR_ID,
                         params={"auto_fix_max_severity": "low",
                                 "escalate_at_or_above": "high"}))
    else:
        skipped.append(("P7", "code-security sweep: no scanner detected — enable npm audit / "
                              "pip-audit / gitleaks / semgrep, then re-profile"))

    has_producers = any(j["phase"] == "producer" for j in jobs)

    # integrator (sole merge authority); shadow until a gate is known
    integ_mode = "active" if caps["ci_gate"] else "shadow"
    jobs.append(_job(INTEGRATOR_ID, "P3", "integrator", merge_authority=True,
                     write_scope=["**"], mode=integ_mode,
                     params={"default_branch": caps["default_branch"], "clean_after": True}))
    if integ_mode == "shadow":
        skipped.append(("note", "repo-hygiene starts in shadow mode: no CI 'gates pass' "
                                "signal detected — it will report would-merge actions until "
                                "you configure the gate"))

    # janitor only if there is produced work to clean up after
    if has_producers:
        jobs.append(_job("leftover-resolver", "P4", "janitor", write_scope=["**"],
                         params={"safe_fix_only": True}))
    else:
        skipped.append(("P4", "leftover resolver: no producers in this suite, so there are "
                              "no producer leftovers to resolve yet"))

    # reflectors always: P5 tunes collaboration → memory; P8 keeps the
    # instruction files + dev tooling current → instructions / approval-queued config.
    jobs.append(_job("collab-meta-learner", "P5", "reflector",
                     write_scope=["AGENTS.md", "**/memory.md"],
                     params={"lookback_hours": 24}))
    jobs.append(_job("devenv-reflector", "P8", "reflector",
                     write_scope=["CLAUDE.md", "AGENTS.md", "GEMINI.md",
                                  ".cursor/rules", ".claude/**", "**/memory.md"],
                     params={"lookback_hours": 24, "max_edits": 3}))
    return jobs, skipped


# --- project-scope ids + display names ---------------------------------------
def localize(jobs: list[dict], project: str) -> list[dict]:
    """Project-scope every job id and stamp a human display name, in place.

    Codex/Claude/Gemini registries are global, so generic ids would collide
    across projects; namespacing keeps each suite in its own directory. Producer
    `hands_off_to` is rewritten with the same rule so it still points at the
    namespaced integrator. Id/name are outside FINGERPRINT_FIELDS, so this never
    changes a job's approval fingerprint.
    """
    for j in jobs:
        j["id"] = naming.namespace_id(project, j["id"])
        if j.get("hands_off_to"):
            j["hands_off_to"] = naming.namespace_id(project, j["hands_off_to"])
        j["name"] = naming.display_name(j, project)
    return jobs


# --- fingerprint (must match optimizer) --------------------------------------
def compute_fingerprint(job: dict) -> str:
    norm = {}
    for f in FINGERPRINT_FIELDS:
        v = job.get(f)
        if f == "write_scope" and isinstance(v, list):
            v = sorted(v)
        if f == "params" and isinstance(v, dict):
            v = {k: v[k] for k in sorted(v)}
        norm[f] = v
    blob = json.dumps(norm, sort_keys=True, separators=(",", ":"))
    return "ao1:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# --- minimal TOML emitter (for our known structure) --------------------------
def _toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_toml_val(val)}" for k, val in v.items()) + " }"
    raise TypeError(f"unserializable: {v!r}")


JOB_KEY_ORDER = ["id", "name", "template", "template_version", "phase", "merge_authority",
                 "schedule", "write_scope", "hands_off_to", "mode", "params",
                 "approved_by", "approved_at", "approved_fingerprint"]


def serialize_suite(suite: dict, jobs: list[dict], header: str = "") -> str:
    out = []
    if header:
        out.append(header.rstrip() + "\n")
    out.append("[suite]")
    for k, v in suite.items():
        out.append(f"{k} = {_toml_val(v)}")
    out.append("")
    for job in jobs:
        out.append("[[job]]")
        for k in JOB_KEY_ORDER:
            if k in job:
                out.append(f"{k} = {_toml_val(job[k])}")
        for k, v in job.items():  # any unexpected extras, deterministically
            if k not in JOB_KEY_ORDER:
                out.append(f"{k} = {_toml_val(v)}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def rationale_text(caps: dict, jobs: list[dict], skipped: list[tuple[str, str]]) -> str:
    lines = ["Capability evidence:"]
    for k, v in caps["evidence"].items():
        lines.append(f"  - {k}: {v}")
    lines.append("")
    lines.append("Included automations:")
    for j in jobs:
        auth = " [SOLE MERGE AUTHORITY]" if j.get("merge_authority") else ""
        label = f"{j['name']} [{j['id']}]" if j.get("name") else j["id"]
        lines.append(f"  - {label} ({j['template']}, {j['phase']}, mode={j.get('mode','active')}){auth}")
    lines.append("")
    lines.append("Skipped / notes:")
    for pid, reason in skipped:
        lines.append(f"  - {pid}: {reason}")
    return "\n".join(lines)


# --- subcommands -------------------------------------------------------------
def cmd_profile(args) -> int:
    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}")
        return 2
    caps = detect(root)
    jobs, skipped = decide(caps)
    localize(jobs, root.name)  # project-scope ids + stamp display names
    suite = {"project": root.name, "workspace": str(root), "state_dir": "state"}
    rationale = rationale_text(caps, jobs, skipped)
    header = ("# Generated by profile_project.py — DRAFT, pending confirmation.\n"
              "# Review, then approve with:  profile_project.py approve --suite <this file>\n#\n"
              + "\n".join("# " + ln for ln in rationale.splitlines()))
    toml_text = serialize_suite(suite, jobs, header=header)

    # self-check: must round-trip
    try:
        tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError as e:
        print(f"internal error: emitted invalid TOML: {e}", file=sys.stderr)
        return 3

    if args.out:
        outp = Path(args.out).expanduser()
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(toml_text, encoding="utf-8")
        print(f"Wrote draft suite → {outp}\n")
        print(rationale)
        print(f"\nNext: validate with  optimize_codex_automations.py --fleet {outp}")
        print(f"Then: confirm with   profile_project.py approve --suite {outp}")
    else:
        print(toml_text)
        print("# ---- rationale ----")
        print("\n".join("# " + ln for ln in rationale.splitlines()))
        print("#\n# (no --out given; nothing written. Re-run with --out PATH to save.)")
    return 0


def cmd_approve(args) -> int:
    suite_path = Path(args.suite).expanduser()
    if not suite_path.is_file():
        print(f"No such suite file: {suite_path}")
        return 2
    data = tomllib.loads(suite_path.read_text(encoding="utf-8"))
    suite = data.get("suite", {})
    jobs = data.get("job", [])
    targets = set(args.jobs.split(",")) if args.jobs else None
    by = args.by or os.environ.get("USER", "unknown")
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamped = []
    for j in jobs:
        if targets and j.get("id") not in targets:
            continue
        j["approved_by"] = by
        j["approved_at"] = now
        j["approved_fingerprint"] = compute_fingerprint(j)
        stamped.append(j.get("id"))
    text = serialize_suite(suite, jobs,
                           header="# Approval stamped by profile_project.py approve.")
    suite_path.write_text(text, encoding="utf-8")
    print(f"Approved {len(stamped)} job(s) by '{by}': {', '.join(stamped)}")
    print(f"Wrote → {suite_path}")
    print("Gate a run with:  optimize_codex_automations.py --fleet "
          f"{suite_path} --require-approved")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Composer: profile a project and "
                                             "propose/approve an automation suite.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("profile", help="detect capabilities and emit a draft suite.toml")
    p.add_argument("path", nargs="?", default=".", help="project directory (default: .)")
    p.add_argument("--out", default=None, help="write suite.toml here (default: print only)")
    p.set_defaults(func=cmd_profile)

    a = sub.add_parser("approve", help="stamp approval fingerprints into a suite.toml")
    a.add_argument("--suite", required=True, help="path to suite.toml")
    a.add_argument("--jobs", default=None, help="comma-separated job ids (default: all)")
    a.add_argument("--by", default=None, help="approver name (default: $USER)")
    a.set_defaults(func=cmd_approve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
