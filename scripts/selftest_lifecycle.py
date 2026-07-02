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
DA = _load("da", "discover_agents.py")
PP2 = _load("pp2", "profile_project.py")

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


def _job_toml(job: dict) -> str:
    """Render one [[job]] table from a flat dict of scalars/lists (test-only,
    compact TOML writer — enough for the minimal manifests these checks need)."""
    lines = ["[[job]]"]
    for k, v in job.items():
        if isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k} = {v}")
        elif isinstance(v, list):
            items = ", ".join(f'"{x}"' for x in v)
            lines.append(f"{k} = [{items}]")
        else:
            lines.append(f'{k} = "{v}"')
    return "\n".join(lines)


def write_manifest(path: Path, project: str, workspace: str, jobs: list[dict],
                    night_start_hour: int | None = None) -> None:
    """Write a compact-but-valid suite manifest: [suite] + one or more [[job]]."""
    header = [f'[suite]\nproject = "{project}"\nworkspace = "{workspace}"']
    if night_start_hour is not None:
        header.append(f"night_start_hour = {night_start_hour}")
    body = "\n\n".join([header[0] + ("\n" + header[1] if len(header) > 1 else "")]
                        + [_job_toml(j) for j in jobs])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body + "\n", encoding="utf-8")


def _integrator_job(jid: str, hour: int) -> dict:
    return {"id": jid, "template": "P3", "phase": "integrator", "merge_authority": True,
            "schedule": f"0 {hour} * * *", "write_scope": ["**"], "mode": "active"}


def _producer_job(jid: str, hour: int, hands_off_to: str) -> dict:
    return {"id": jid, "template": "P1", "phase": "producer", "merge_authority": False,
            "schedule": f"0 {hour} * * *", "write_scope": ["**"], "mode": "active",
            "hands_off_to": hands_off_to}


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

        # --- project-queue scaffold: setup creates it, format is correct ---
        queue_path = ws / ".codex" / "automations" / "PROJECT-QUEUE.md"
        check("PROJECT-QUEUE.md scaffolded by setup", queue_path.is_file())
        queue_text = queue_path.read_text(encoding="utf-8") if queue_path.is_file() else ""
        check("PROJECT-QUEUE.md has Objectives section", "## Objectives" in queue_text)
        check("PROJECT-QUEUE.md has Open threads section", "## Open threads" in queue_text)

        # --- project-queue scaffold: idempotent, never overwrites ----------
        sentinel = "\n<!-- sentinel: do not clobber -->\n"
        queue_path.write_text(queue_text + sentinel, encoding="utf-8")
        second = LC.PP.scaffold_project_queue(ws, "demo-app")
        check("second scaffold call returns None (already exists)", second is None)
        check("sentinel survives a second scaffold call",
              sentinel in queue_path.read_text(encoding="utf-8"))

        # --- E1: multi-suite fleet validation -------------------------------
        multi_home = root / ".codex-multi"
        suites_root = OPT.suites_dir(multi_home)

        # 1. two suites whose integrators share the SAME workspace -> FAIL,
        #    naming both integrator job ids.
        shared_ws = str(root / "shared-repo")
        write_manifest(suites_root / "alpha.toml", "Alpha", shared_ws,
                        [_integrator_job("alpha-integrator", 3),
                         _producer_job("alpha-producer", 1, "alpha-integrator")])
        write_manifest(suites_root / "beta.toml", "Beta", shared_ws,
                        [_integrator_job("beta-integrator", 4),
                         _producer_job("beta-producer", 1, "beta-integrator")])
        rc_shared = OPT.fleet_multi(multi_home, require_approved=False)
        check("multi-suite: shared-workspace merge authorities FAIL", rc_shared != 0)

        buf = io.StringIO()
        with redirect_stdout(buf):
            OPT.fleet_multi(multi_home, require_approved=False)
        shared_out = buf.getvalue()
        check("multi-suite: shared-workspace failure names both integrators",
              "alpha-integrator" in shared_out and "beta-integrator" in shared_out,
              shared_out)

        # 2. same two suites but with DISTINCT workspaces -> PASS.
        write_manifest(suites_root / "beta.toml", "Beta", str(root / "beta-repo"),
                        [_integrator_job("beta-integrator", 4),
                         _producer_job("beta-producer", 1, "beta-integrator")])
        rc_distinct = OPT.fleet_multi(multi_home, require_approved=False)
        check("multi-suite: distinct-workspace suites PASS", rc_distinct == 0)

        # 3. legacy <home>/automations/suite.toml alone is still honored, with
        #    a deprecation note printed.
        legacy_home = root / ".codex-legacy"
        legacy_path = legacy_home / "automations" / OPT.LEGACY_SUITE_NAME
        write_manifest(legacy_path, "Legacy", str(root / "legacy-repo"),
                        [_integrator_job("legacy-integrator", 3),
                         _producer_job("legacy-producer", 1, "legacy-integrator")])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc_legacy = OPT.fleet_multi(legacy_home, require_approved=False)
        legacy_out = buf.getvalue()
        check("multi-suite: legacy suite.toml alone validates and PASSes",
              rc_legacy == 0, legacy_out)
        check("multi-suite: legacy file gets a deprecation note",
              "legacy" in legacy_out.lower() and "deprecat" in legacy_out.lower(),
              legacy_out)

        # 4. E5: night_start_hour normalization for rule 5 (phase ordering).
        #    producer@23, integrator@03 — FAILS with default night_start_hour=0,
        #    PASSES once night_start_hour=20 rolls both into the same night.
        night_home = root / ".codex-night"
        night_ws = str(root / "night-repo")
        night_suite = OPT.suites_dir(night_home) / "gamma.toml"

        write_manifest(night_suite, "Gamma", night_ws,
                        [_integrator_job("gamma-integrator", 3),
                         _producer_job("gamma-producer", 23, "gamma-integrator")])
        rc_default = OPT.fleet(night_suite, require_approved=False)
        check("night_start_hour default(0): midnight-spanning pair FAILS rule 5",
              rc_default != 0)

        write_manifest(night_suite, "Gamma", night_ws,
                        [_integrator_job("gamma-integrator", 3),
                         _producer_job("gamma-producer", 23, "gamma-integrator")],
                        night_start_hour=20)
        rc_normalized = OPT.fleet(night_suite, require_approved=False)
        check("night_start_hour=20: midnight-spanning pair PASSES rule 5",
              rc_normalized == 0)

        # --- E4: agent recording + per-verb resolution ---------------------
        agent_home = root / ".codex-agents"
        claude_agent_home = root / ".claude-agents"
        agent_ws = root / "agent-app"
        agent_ws.mkdir()
        (agent_ws / "package.json").write_text('{"name":"agent-app"}', encoding="utf-8")
        (agent_ws / "package-lock.json").write_text("{}", encoding="utf-8")
        (agent_ws / "tests").mkdir()
        (agent_ws / "tests" / "t.test.js").write_text("test('x',()=>{})", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=agent_ws)
        subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=agent_ws)

        agent_suite_path = root / "agent-suite.toml"
        rc, out = run_lc(["setup", str(agent_ws), "--suite", str(agent_suite_path),
                          "--agent", "claude", "--apply",
                          "--home-override", str(claude_agent_home)])
        check("E4 setup --agent claude exits 0", rc == 0, out)
        _, agent_jobs = LC.load_suite(agent_suite_path)
        coverage_job = LC.find_job(agent_jobs, "agent-app-coverage-ratchet")
        check("E4 setup recorded agents=['claude'] on the job",
              coverage_job is not None and coverage_job.get("agents") == ["claude"],
              coverage_job)

        rc, out = run_lc(["update", "--suite", str(agent_suite_path),
                          "--id", "agent-app-coverage-ratchet",
                          "--param", "coverage_floor=90", "--apply",
                          "--home-override", str(agent_home)])
        check("E4 update with NO --agent exits 0", rc == 0, out)
        claude_skill = (claude_agent_home / "scheduled-tasks"
                        / "agent-app-coverage-ratchet" / "SKILL.md")
        check("E4 update (no --agent) touched the claude registry path",
              claude_skill.is_file(), claude_skill)
        codex_toml = (agent_home / "automations"
                      / "agent-app-coverage-ratchet" / "automation.toml")
        check("E4 update (no --agent) did NOT touch codex", not codex_toml.exists(),
              codex_toml)

        # --- E4: fingerprint stability across recording job["agents"] -------
        fp_job = {"id": "fp-check", "template": "P1", "phase": "producer",
                  "merge_authority": False, "schedule": "0 1 * * *",
                  "write_scope": ["**"], "mode": "active", "params": {"x": 1}}
        fp_before = LC.PP.compute_fingerprint(fp_job)
        fp_job["agents"] = ["claude"]
        fp_after = LC.PP.compute_fingerprint(fp_job)
        check("E4 fingerprint identical before/after recording job['agents']",
              fp_before == fp_after, (fp_before, fp_after))

        # --- E3: adopt a live codex job under manifest governance -----------
        # Adopt a janitor into a suite that already has an integrator (rule 4
        # requires an integrator whenever a janitor is present), so this
        # exercises the adopt path itself rather than an unrelated fleet rule.
        adopt_home = root / ".codex-adopt"
        adopted_dir = adopt_home / "automations" / "adopted-job"
        adopted_dir.mkdir(parents=True)
        adopted_ws = root / "adopted-ws"
        adopted_ws.mkdir()
        adopt_suites = OPT.suites_dir(adopt_home)
        write_manifest(adopt_suites / "adopted-project.toml", "adopted-project",
                       str(adopted_ws), [_integrator_job("adopted-project-integrator", 3)])
        live_prompt = "Do the thing. Task-specific instructions only."
        (adopted_dir / "automation.toml").write_text(
            "version = 1\n"
            'id = "adopted-job"\n'
            'kind = "cron"\n'
            'name = "Adopted Job"\n'
            "prompt = '''\n" + live_prompt + "\n'''\n"
            'status = "ACTIVE"\n'
            'rrule = "FREQ=DAILY;BYHOUR=4;BYMINUTE=30;BYSECOND=0"\n'
            f'cwds = ["{adopted_ws}"]\n',
            encoding="utf-8",
        )
        rc, out = run_lc(["adopt", "--job-id", "adopted-job", "--template", "P4",
                          "--phase", "janitor", "--suite", "adopted-project",
                          "--home-override", str(adopt_home), "--apply"])
        check("E3 adopt --apply exits 0", rc == 0, out)
        adopted_suite_path = OPT.suite_manifest_path(adopt_home, "adopted-project")
        check("E3 adopt wrote the resolved suites/ manifest", adopted_suite_path.is_file(),
              adopted_suite_path)
        if adopted_suite_path.is_file():
            _, adopted_jobs = LC.load_suite(adopted_suite_path)
            adopted_job = LC.find_job(adopted_jobs, "adopted-job")
            check("E3 adopt drafted a manifest entry", adopted_job is not None)
            check("E3 adopt recovered the schedule from the rrule",
                  adopted_job is not None and adopted_job.get("schedule") == "30 4 * * *",
                  adopted_job)
            check("E3 adopt set template_version for a known template",
                  adopted_job is not None and adopted_job.get("template_version") is not None,
                  adopted_job)
        live_after = (adopted_dir / "automation.toml").read_text(encoding="utf-8")
        check("E3 adopt never modified the LIVE prompt",
              live_prompt in live_after, live_after)

        # --- E3: adopt a bespoke job with --template custom ------------------
        # A reflector needs no integrator, so this isolates the custom path;
        # custom jobs track a prompt_hash instead of a template_version and must
        # pass fleet validation (template `custom` is adoptable).
        rc_c, out_c = run_lc(["adopt", "--job-id", "adopted-job", "--template",
                              "custom", "--phase", "reflector", "--suite",
                              "custom-project", "--home-override", str(adopt_home),
                              "--apply"])
        check("E3 adopt --template custom exits 0 (passes fleet validation)",
              rc_c == 0, out_c)
        custom_suite_path = OPT.suite_manifest_path(adopt_home, "custom-project")
        if custom_suite_path.is_file():
            _, custom_jobs = LC.load_suite(custom_suite_path)
            cj = LC.find_job(custom_jobs, "adopted-job")
            check("E3 custom adopt records a prompt_hash, not a template_version",
                  cj is not None and cj.get("prompt_hash")
                  and not cj.get("template_version"), cj)

        # --- E3: dual merge-authority guard on adopt -------------------------
        dual_home = root / ".codex-dual"
        dual_suites = OPT.suites_dir(dual_home)
        dual_ws = str(root / "dual-repo")
        write_manifest(dual_suites / "dual.toml", "Dual", dual_ws,
                        [_integrator_job("dual-integrator", 3)])
        dual_dir = dual_home / "automations" / "second-integrator"
        dual_dir.mkdir(parents=True)
        (dual_dir / "automation.toml").write_text(
            "version = 1\n"
            'id = "second-integrator"\n'
            'kind = "cron"\n'
            'name = "Second Integrator"\n'
            "prompt = '''\nIntegrate things.\n'''\n"
            'status = "ACTIVE"\n'
            'rrule = "FREQ=DAILY;BYHOUR=3;BYMINUTE=0;BYSECOND=0"\n'
            f'cwds = ["{dual_ws}"]\n',
            encoding="utf-8",
        )
        rc, out = run_lc(["adopt", "--job-id", "second-integrator", "--template", "P3",
                          "--phase", "integrator", "--merge-authority",
                          "--suite", str(dual_suites / "dual.toml"),
                          "--home-override", str(dual_home), "--apply"])
        check("E3 adopt: second active merge authority for same workspace FAILS",
              rc == 1, out)
        _, dual_jobs_after = LC.load_suite(dual_suites / "dual.toml")
        check("E3 adopt: nothing written on dual-authority failure",
              LC.find_job(dual_jobs_after, "second-integrator") is None)

        # --- E6: setup output location ---------------------------------------
        e6_home = root / ".codex-e6"
        e6_ws = root / "e6-app"
        e6_ws.mkdir()
        (e6_ws / "package.json").write_text('{"name":"e6-app"}', encoding="utf-8")
        (e6_ws / "package-lock.json").write_text("{}", encoding="utf-8")
        (e6_ws / "tests").mkdir()
        (e6_ws / "tests" / "t.test.js").write_text("test('x',()=>{})", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=e6_ws)
        subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=e6_ws)

        rc, out = run_lc(["setup", str(e6_ws), "--agent", "codex", "--apply",
                          "--home-override", str(e6_home)])
        check("E6 setup with neither --suite nor --local exits 0", rc == 0, out)
        expected_default = OPT.suite_manifest_path(e6_home, "e6-app")
        check("E6 setup default resolves under automations/suites/<slug>.toml",
              expected_default.is_file()
              and str(expected_default).replace("\\", "/").endswith(
                  "automations/suites/e6-app.toml"),
              expected_default)

        rc, out = run_lc(["setup", str(e6_ws), "--local", "--agent", "codex", "--apply",
                          "--home-override", str(e6_home)])
        check("E6 setup --local exits 0", rc == 0, out)
        local_path = e6_ws / ".codex" / "automations" / "suite.toml"
        check("E6 setup --local writes <root>/.codex/automations/suite.toml",
              local_path.is_file(), local_path)

        # --- rrule_to_cron round-trips cron_to_rrule --------------------------
        daily_cron = "15 4 * * *"
        check("rrule_to_cron round-trips a daily cron",
              LC.MAT.rrule_to_cron(LC.MAT.cron_to_rrule(daily_cron)) == daily_cron,
              LC.MAT.cron_to_rrule(daily_cron))

        weekly_cron = "30 2 * * 1,3"
        check("rrule_to_cron round-trips a weekly cron",
              LC.MAT.rrule_to_cron(LC.MAT.cron_to_rrule(weekly_cron)) == weekly_cron,
              LC.MAT.cron_to_rrule(weekly_cron))

        # --- F1: gemini jobs must not be falsely reported ORPHAN -----------
        f1_home = root / ".f1-gemini-home"
        f1_job_dir = f1_home / ".gemini" / "automations" / "demo-job"
        f1_job_dir.mkdir(parents=True)
        f1_prompt = (OPT.managed_block(str(f1_job_dir)) + "\n\nDo the thing.\n")
        (f1_job_dir / "prompt.md").write_text(f1_prompt, encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc_f1 = DA.main(["--home", str(f1_home), "--agent", "gemini"])
        f1_out = buf.getvalue()
        check("F1 discover gemini exits 0", rc_f1 == 0, f1_out)
        check("F1 gemini job with prompt.md is NOT reported ORPHAN",
              "ORPHAN" not in f1_out, f1_out)
        check("F1 gemini job is reflected as a real job (protocol block detected)",
              "demo-job" in f1_out and "protocol v" in f1_out, f1_out)

        # --- F3: cron_to_rrule BYMONTHDAY + unsupported-field warning ------
        monthly_rrule = LC.MAT.cron_to_rrule("30 2 5 * *")
        check("F3 numeric dom emits BYMONTHDAY=5",
              "BYMONTHDAY=5" in monthly_rrule, monthly_rrule)
        check("F3 numeric dom emits FREQ=MONTHLY",
              "FREQ=MONTHLY" in monthly_rrule, monthly_rrule)

        weekly_rrule_f3 = LC.MAT.cron_to_rrule("0 3 * * 1")
        check("F3 dow-set schedule is still WEEKLY",
              "FREQ=WEEKLY" in weekly_rrule_f3, weekly_rrule_f3)

        unsupported_job = {"id": "demo-app-unsupported-cron", "template": "P1",
                            "phase": "producer", "merge_authority": False,
                            "schedule": "*/15 * * * *", "write_scope": ["**"],
                            "mode": "active", "hands_off_to": "demo-app-repo-hygiene"}
        unsupported_prompt = LC.MAT.build_prompt(
            unsupported_job, str(autos / unsupported_job["id"]))
        unsupported_toml = LC.MAT.emit_codex_toml(
            unsupported_job, unsupported_prompt, str(ws), None)
        check("F3 unsupported cron field triggers a schedule-warning comment",
              "# schedule-warning:" in unsupported_toml, unsupported_toml)

        supported_job = dict(unsupported_job)
        supported_job["id"] = "demo-app-supported-cron"
        supported_job["schedule"] = "30 2 5 * *"
        supported_prompt = LC.MAT.build_prompt(
            supported_job, str(autos / supported_job["id"]))
        supported_toml = LC.MAT.emit_codex_toml(
            supported_job, supported_prompt, str(ws), None)
        check("F3 supported cron (numeric dom) emits no schedule-warning",
              "# schedule-warning:" not in supported_toml, supported_toml)

        # --- F4: security_scanner gated on the actual audit tool per stack -
        f4_py_root = root / "f4-py-project"
        f4_py_root.mkdir()
        (f4_py_root / "requirements.txt").write_text("flask\n", encoding="utf-8")

        orig_which = PP2.shutil.which
        try:
            PP2.shutil.which = lambda name: None  # nothing on PATH
            caps_no_tool = PP2.detect(f4_py_root)
            check("F4 python project WITHOUT pip-audit on PATH does NOT claim security_scanner",
                  caps_no_tool["security_scanner"] is False, caps_no_tool)

            PP2.shutil.which = lambda name: ("/usr/bin/pip-audit"
                                             if name == "pip-audit" else None)
            caps_with_tool = PP2.detect(f4_py_root)
            check("F4 python project WITH pip-audit on PATH claims security_scanner",
                  caps_with_tool["security_scanner"] is True, caps_with_tool)
            check("F4 evidence names pip-audit",
                  "pip-audit" in caps_with_tool["evidence"].get("security_scanner", ""),
                  caps_with_tool["evidence"])

            f4_npm_root = root / "f4-npm-project"
            f4_npm_root.mkdir()
            (f4_npm_root / "package-lock.json").write_text("{}", encoding="utf-8")
            PP2.shutil.which = lambda name: None  # no tools at all on PATH
            caps_npm = PP2.detect(f4_npm_root)
            check("F4 npm project claims security_scanner regardless of PATH",
                  caps_npm["security_scanner"] is True, caps_npm)
            check("F4 npm evidence names npm audit",
                  "npm audit" in caps_npm["evidence"].get("security_scanner", ""),
                  caps_npm["evidence"])
        finally:
            PP2.shutil.which = orig_which

        # --- v0.7.2 Fix 3: adopt wires a producer's hands_off_to to the suite's
        #     integrator (parity with `add`), so adopting a producer no longer
        #     trips fleet rule 3 ("producer missing 'hands_off_to'").
        prod_home = root / ".codex-adopt-producer"
        prod_ws = str(root / "adopt-producer-ws")
        Path(prod_ws).mkdir()
        prod_suites = OPT.suites_dir(prod_home)
        write_manifest(prod_suites / "prod-project.toml", "prod-project", prod_ws,
                        [_integrator_job("prod-project-integrator", 3)])
        live_prod_dir = prod_home / "automations" / "prod-job"
        live_prod_dir.mkdir(parents=True)
        (live_prod_dir / "automation.toml").write_text(
            "version = 1\n"
            'id = "prod-job"\n'
            'kind = "cron"\n'
            'name = "Prod Job"\n'
            "prompt = '''\nProduce things.\n'''\n"
            'status = "ACTIVE"\n'
            'rrule = "FREQ=DAILY;BYHOUR=1;BYMINUTE=0;BYSECOND=0"\n'
            f'cwds = ["{prod_ws}"]\n',
            encoding="utf-8",
        )
        rc_p, out_p = run_lc(["adopt", "--job-id", "prod-job", "--template", "P1",
                              "--phase", "producer", "--suite", "prod-project",
                              "--home-override", str(prod_home), "--apply"])
        check("Fix3 adopt producer --apply exits 0 (fleet rule 3 satisfied)",
              rc_p == 0, out_p)
        prod_suite_path = OPT.suite_manifest_path(prod_home, "prod-project")
        check("Fix3 adopt wrote the producer's suite manifest",
              prod_suite_path.is_file(), prod_suite_path)
        if prod_suite_path.is_file():
            _, prod_jobs = LC.load_suite(prod_suite_path)
            adopted_prod = LC.find_job(prod_jobs, "prod-job")
            check("Fix3 adopted producer has hands_off_to == the suite integrator",
                  adopted_prod is not None
                  and adopted_prod.get("hands_off_to") == "prod-project-integrator",
                  adopted_prod)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
