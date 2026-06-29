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
        check("integrator installed", (autos / "repo-hygiene" / "automation.toml").is_file())
        check("producer sidecars created",
              (autos / "coverage-ratchet" / "memory.md").is_file())

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
        rc, out = run_lc(["update", "--suite", str(suite_path), "--id", "coverage-ratchet",
                          "--schedule", "30 1 * * *", "--agent", "codex"])  # dry run
        check("update dry-run flags re-approval", "re-approval" in out, out)
        rc, out = run_lc(["update", "--suite", str(suite_path), "--id", "coverage-ratchet",
                          "--param", "coverage_floor=85", "--agent", "codex", "--apply",
                          "--home-override", str(codex_home)])
        check("update --apply exits 0", rc == 0, out)
        _, jobs1 = LC.load_suite(suite_path)
        cr = LC.find_job(jobs1, "coverage-ratchet")
        check("update changed param", cr["params"]["coverage_floor"] == 85)
        check("update re-stamped fingerprint",
              cr.get("approved_fingerprint") == LC.PP.compute_fingerprint(cr))

        # --- remove (disable) keeps files, flips status --------------------
        rc, out = run_lc(["remove", "--suite", str(suite_path), "--id", "code-security",
                          "--agent", "codex", "--apply",
                          "--home-override", str(codex_home)])
        check("remove(disable) exits 0", rc == 0, out)
        sec_toml = (autos / "code-security" / "automation.toml")
        check("disabled job dir kept", sec_toml.is_file())
        check("disabled status flipped", 'status = "disabled"' in sec_toml.read_text())

        # --- refuse removing the sole integrator while producers remain ----
        rc, out = run_lc(["remove", "--suite", str(suite_path), "--id", "repo-hygiene",
                          "--agent", "codex", "--apply",
                          "--home-override", str(codex_home)])
        check("remove integrator refused", rc == 1 and "Refusing" in out, out)

        # --- purge archives state then deletes the dir ---------------------
        rc, out = run_lc(["remove", "--suite", str(suite_path), "--id", "code-security",
                          "--purge", "--agent", "codex", "--apply",
                          "--home-override", str(codex_home)])
        check("purge exits 0", rc == 0, out)
        check("purged dir deleted", not (autos / "code-security").exists())
        check("purge archived sidecars",
              any((autos / ".archive").glob("code-security-*/memory.md")))
        _, jobs2 = LC.load_suite(suite_path)
        check("purge dropped job from manifest", LC.find_job(jobs2, "code-security") is None)

        # --- claude materialization (native_file, no status field) ---------
        claude_home = root / ".claude"
        rc, out = run_lc(["setup", str(ws), "--suite", str(root / "claude-suite.toml"),
                          "--agent", "claude", "--apply",
                          "--home-override", str(claude_home)])
        check("claude setup exits 0", rc == 0, out)
        check("claude SKILL.md written",
              (claude_home / "scheduled-tasks" / "repo-hygiene" / "SKILL.md").is_file())

        # claude disable relocates under .disabled/
        rc, out = run_lc(["remove", "--suite", str(root / "claude-suite.toml"),
                          "--id", "coverage-ratchet", "--agent", "claude", "--apply",
                          "--home-override", str(claude_home)])
        check("claude disable relocates",
              (claude_home / "scheduled-tasks" / ".disabled" / "coverage-ratchet").is_dir(),
              out)

        # --- gemini emits a cron line, never edits cron --------------------
        rc, out = run_lc(["setup", str(ws), "--suite", str(root / "gem-suite.toml"),
                          "--agent", "gemini", "--apply",
                          "--home-override", str(root / ".gemini")])
        check("gemini emits crontab line", "crontab" in out and "gemini -p" in out, out)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
