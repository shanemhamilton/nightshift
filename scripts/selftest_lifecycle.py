#!/usr/bin/env python3
"""
selftest_lifecycle.py — end-to-end checks for the lifecycle verbs across agents.

Builds a throwaway workspace + agent homes under a temp dir, exercises
setup/add/remove/update for several scheduler types, and asserts the manifest and
on-disk registry end up where the design says. Pipes results through the real
fleet + strict validators. No network, no real ~/.codex touched.

Run: python3 scripts/selftest_lifecycle.py   (exit 0 = all pass)
"""
from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, fn):
    s = importlib.util.spec_from_file_location(name, HERE / fn)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


LC = _load("lc", "lifecycle.py")
OPT = _load("opt", "optimize_codex_automations.py")

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}  {detail}")


def run_lc(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = LC.main(argv)
    return rc, buf.getvalue()


def make_workspace(root: Path) -> Path:
    ws = root / "demo-app"
    ws.mkdir()
    (ws / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
    (ws / "package-lock.json").write_text("{}", encoding="utf-8")
    (ws / "README.md").write_text("# Demo\n", encoding="utf-8")  # docs surface → P10
    (ws / ".eslintrc").write_text("{}", encoding="utf-8")
    (ws / "tests").mkdir()
    (ws / "tests" / "t.test.js").write_text("test('x',()=>{})", encoding="utf-8")
    (ws / ".github" / "workflows").mkdir(parents=True)
    (ws / ".github" / "workflows" / "ci.yml").write_text("on: push", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=ws)
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=ws)
    return ws


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ws = make_workspace(root)
        codex_home = root / ".codex"
        suite_path = ws / "suite.toml"

        # --- setup (codex) -------------------------------------------------
        rc, out = run_lc(["setup", str(ws), "--suite", str(suite_path),
                          "--agent", "codex", "--apply",
                          "--home-override", str(codex_home)])
        check("setup exits 0", rc == 0, out)
        check("suite.toml written", suite_path.is_file())
        autos = codex_home / "automations"
        integ_toml = autos / "demo-app-repo-hygiene" / "automation.toml"
        check("integrator installed (project-scoped id)", integ_toml.is_file())
        check("producer sidecars created",
              (autos / "demo-app-coverage-ratchet" / "memory.md").is_file())
        docs_toml = autos / "demo-app-docs-sync" / "automation.toml"
        check("P10 docs-sync selected (README present)", docs_toml.is_file(),
              "expected demo-app-docs-sync to be installed")
        check("P10 hands off to the integrator + renders the docs-sync body",
              docs_toml.is_file()
              and "demo-app-repo-hygiene" in docs_toml.read_text()
              and "documentation-sync ratchet (P10, producer)" in docs_toml.read_text(),
              docs_toml.read_text() if docs_toml.is_file() else "missing")
        check("display name is project-prefixed",
              'name = "demo-app ' in integ_toml.read_text(),
              integ_toml.read_text() if integ_toml.is_file() else "missing")

        # fleet + strict must pass on what setup produced
        sp = OPT.fleet(suite_path, require_approved=False)
        check("fleet PASS after setup", sp == 0)
        st = OPT.strict(OPT.find_automations(codex_home), "prompt")
        check("strict PASS after setup", st == 0)

        suite0, jobs0 = LC.load_suite(suite_path)
        n0 = len(jobs0)

        # --- add a producer that is NOT auto-detected should refuse --------
        rc, out = run_lc(["add", "--suite", str(suite_path), "--pattern", "P2",
                          "--agent", "codex", "--apply",
                          "--home-override", str(codex_home)])
        check("add P2 refused (no UI driver)", rc == 1, out)

        # --- update: change schedule => stale => needs re-approval ---------
        rc, out = run_lc(["update", "--suite", str(suite_path), "--id", "demo-app-coverage-ratchet",
                          "--schedule", "30 1 * * *", "--agent", "codex"])  # dry run
        check("update dry-run flags re-approval", "re-approval" in out, out)
        rc, out = run_lc(["update", "--suite", str(suite_path), "--id", "demo-app-coverage-ratchet",
                          "--param", "coverage_floor=85", "--agent", "codex", "--apply",
                          "--home-override", str(codex_home)])
        check("update --apply exits 0", rc == 0, out)
        _, jobs1 = LC.load_suite(suite_path)
        cr = LC.find_job(jobs1, "demo-app-coverage-ratchet")
        check("update changed param", cr["params"]["coverage_floor"] == 85)
        check("update re-stamped fingerprint",
              cr.get("approved_fingerprint") == LC.PP.compute_fingerprint(cr))

        # --- remove (disable) keeps files, flips status --------------------
        rc, out = run_lc(["remove", "--suite", str(suite_path), "--id", "demo-app-code-security",
                          "--agent", "codex", "--apply",
                          "--home-override", str(codex_home)])
        check("remove(disable) exits 0", rc == 0, out)
        sec_toml = (autos / "demo-app-code-security" / "automation.toml")
        check("disabled job dir kept", sec_toml.is_file())
        check("disabled status flipped", 'status = "disabled"' in sec_toml.read_text())

        # --- refuse removing the sole integrator while producers remain ----
        rc, out = run_lc(["remove", "--suite", str(suite_path), "--id", "demo-app-repo-hygiene",
                          "--agent", "codex", "--apply",
                          "--home-override", str(codex_home)])
        check("remove integrator refused", rc == 1 and "Refusing" in out, out)

        # --- purge archives state then deletes the dir ---------------------
        rc, out = run_lc(["remove", "--suite", str(suite_path), "--id", "demo-app-code-security",
                          "--purge", "--agent", "codex", "--apply",
                          "--home-override", str(codex_home)])
        check("purge exits 0", rc == 0, out)
        check("purged dir deleted", not (autos / "demo-app-code-security").exists())
        check("purge archived sidecars",
              any((autos / ".archive").glob("demo-app-code-security-*/memory.md")))
        _, jobs2 = LC.load_suite(suite_path)
        check("purge dropped job from manifest",
              LC.find_job(jobs2, "demo-app-code-security") is None)

        # --- claude materialization (native_file, no status field) ---------
        claude_home = root / ".claude"
        rc, out = run_lc(["setup", str(ws), "--suite", str(root / "claude-suite.toml"),
                          "--agent", "claude", "--apply",
                          "--home-override", str(claude_home)])
        check("claude setup exits 0", rc == 0, out)
        check("claude SKILL.md written",
              (claude_home / "scheduled-tasks" / "demo-app-repo-hygiene" / "SKILL.md").is_file())

        # claude disable relocates under .disabled/
        rc, out = run_lc(["remove", "--suite", str(root / "claude-suite.toml"),
                          "--id", "demo-app-coverage-ratchet", "--agent", "claude", "--apply",
                          "--home-override", str(claude_home)])
        check("claude disable relocates",
              (claude_home / "scheduled-tasks" / ".disabled" / "demo-app-coverage-ratchet").is_dir(),
              out)

        # --- gemini emits a cron line, never edits cron --------------------
        rc, out = run_lc(["setup", str(ws), "--suite", str(root / "gem-suite.toml"),
                          "--agent", "gemini", "--apply",
                          "--home-override", str(root / ".gemini")])
        check("gemini emits crontab line", "crontab" in out and "gemini -p" in out, out)

        # --- reasoning_effort defaults + never-Haiku guard (C10) -----------
        # demo-app-code-security (P7) was purged above, so build the job dict
        # directly rather than looking it up in the (now-pruned) manifest.
        p7_job = {"id": "demo-app-code-security", "template": "P7", "phase": "producer",
                  "merge_authority": False, "schedule": "0 2 * * *",
                  "write_scope": ["**"], "mode": "active", "hands_off_to": "demo-app-repo-hygiene"}
        p7_prompt = LC.MAT.build_prompt(p7_job, str(autos / p7_job["id"]))
        p7_toml = LC.MAT.emit_codex_toml(p7_job, p7_prompt, str(ws), None)
        check("P7 emits reasoning_effort = high",
              'reasoning_effort = "high"' in p7_toml, p7_toml)

        p9_job = {"id": "fleet-approval-digest", "template": "P9", "phase": "reflector",
                  "merge_authority": False, "schedule": "0 6 * * *",
                  "write_scope": ["**"], "mode": "active"}
        p9_prompt = LC.MAT.build_prompt(p9_job, str(autos / p9_job["id"]))
        p9_toml = LC.MAT.emit_codex_toml(p9_job, p9_prompt, str(ws), None)
        check("P9 emits reasoning_effort = low",
              'reasoning_effort = "low"' in p9_toml, p9_toml)

        p2_job = {"id": "demo-app-product-value", "template": "P2", "phase": "producer",
                  "merge_authority": False, "schedule": "0 2 * * *",
                  "write_scope": ["**"], "mode": "active", "hands_off_to": "demo-app-repo-hygiene"}
        p2_prompt = LC.MAT.build_prompt(p2_job, str(autos / p2_job["id"]))
        p2_toml = LC.MAT.emit_codex_toml(p2_job, p2_prompt, str(ws), None)
        check("P2 (no effort default) omits reasoning_effort line",
              "reasoning_effort" not in p2_toml, p2_toml)

        haiku_job = dict(p2_job)
        haiku_job["id"] = "demo-app-haiku-guard"
        haiku_job["model"] = "claude-haiku-4-5"
        haiku_prompt = LC.MAT.build_prompt(haiku_job, str(autos / haiku_job["id"]))
        try:
            LC.MAT.emit_codex_toml(haiku_job, haiku_prompt, str(ws), None)
            check("emit_codex_toml refuses a Haiku-class model", False,
                  "expected ValueError, none raised")
        except ValueError as e:
            check("emit_codex_toml refuses a Haiku-class model",
                  "haiku" in str(e).lower(), str(e))

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
