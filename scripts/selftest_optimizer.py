#!/usr/bin/env python3
"""
selftest_optimizer.py — checks for block-integrity validation (A1),
customization-preserving upgrades (A2), and never-downgrade (A3) in
optimize_codex_automations.py.

Builds throwaway automation.toml fixtures under a tempfile.TemporaryDirectory
and drives status_of()/apply()/strict() directly against them. No network, no
real ~/.codex touched.

Run: python3 scripts/selftest_optimizer.py   (exit 0 = all pass)
"""
from __future__ import annotations

import importlib.util
import io
import json
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name, fn):
    s = importlib.util.spec_from_file_location(name, HERE / fn)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


OPT = _load("opt", "optimize_codex_automations.py")
INSTALL = _load("install_skill", "install_skill.py")
DISCOVER = _load("discover_agents", "discover_agents.py")
REPO_VERSION = (HERE.parent / "VERSION").read_text(encoding="utf-8").strip()

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


def _write_job(root: Path, job_id: str, prompt: str) -> Path:
    """Write a minimal automation.toml with the given prompt body."""
    job_dir = root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    toml_path = job_dir / "automation.toml"
    quoted = prompt.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    toml_path.write_text(
        f'id = "{job_id}"\nprompt = """\n{quoted}\n"""\n', encoding="utf-8"
    )
    return toml_path


def _quiet(fn, *args, **kwargs):
    """Run fn, discarding stdout, and return (result, printed_text)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(*args, **kwargs)
    return rc, buf.getvalue()


def check_managed_block_shape(root: Path) -> None:
    job_dir = root / "shape-check"
    job_dir.mkdir()
    block = OPT.managed_block(str(job_dir))
    check(f"managed_block contains 'Protocol version: {OPT.PROTOCOL_VERSION}'",
          f"Protocol version: {OPT.PROTOCOL_VERSION}" in block)
    missing = [s for s in OPT.REQUIRED_SECTIONS if s not in block]
    check("managed_block contains all REQUIRED_SECTIONS", not missing, missing)
    # The prior canonical version stays reproducible from BLOCK_HISTORY, so
    # customization-preserving upgrades of older jobs still work.
    v4 = OPT.managed_block(str(job_dir), 4)
    check("BLOCK_HISTORY[4] still reproduces 'Protocol version: 4'",
          "Protocol version: 4" in v4)


def check_wellformed_v4(root: Path) -> None:
    job_dir = root / "wellformed"
    job_dir.mkdir()
    prompt = OPT.managed_block(str(job_dir)) + "\n\nTask body here.\n"
    p = _write_job(root, "wellformed", prompt)
    code, detail = OPT.status_of(p, "prompt")
    check("well-formed v4 block recognized (needs-sidecars or compliant)",
          code in ("needs-sidecars", "compliant"), f"got {code}: {detail}")
    check("well-formed v4 block is NOT malformed/duplicate",
          code not in ("malformed-block", "duplicate-block"), code)
    OPT.scaffold_sidecars(job_dir, [])
    code2, detail2 = OPT.status_of(p, "prompt")
    check("well-formed v4 block compliant after sidecars", code2 == "compliant",
          f"got {code2}: {detail2}")


def check_missing_end_marker(root: Path) -> None:
    job_dir = root / "no-end"
    job_dir.mkdir()
    block = OPT.managed_block(str(job_dir))
    broken = block.replace(OPT.END_MARKER, "")  # BEGIN present, END removed
    prompt = broken + "\n\nTask body.\n"
    p = _write_job(root, "no-end", prompt)
    code, _ = OPT.status_of(p, "prompt")
    check("BEGIN without END -> malformed-block", code == "malformed-block", code)

    raw_before = p.read_bytes()
    _quiet(OPT.apply, [p], "prompt")
    check("apply() leaves malformed-block file byte-identical",
          p.read_bytes() == raw_before)


def check_duplicate_block(root: Path) -> None:
    job_dir = root / "dup"
    job_dir.mkdir()
    block = OPT.managed_block(str(job_dir))
    prompt = block + "\n\n" + block + "\n\nTask body.\n"
    p = _write_job(root, "dup", prompt)
    code, _ = OPT.status_of(p, "prompt")
    check("two BEGIN..END blocks -> duplicate-block", code == "duplicate-block", code)

    raw_before = p.read_bytes()
    _quiet(OPT.apply, [p], "prompt")
    check("apply() leaves duplicate-block file byte-identical",
          p.read_bytes() == raw_before)


def check_version_line_outside_span(root: Path) -> None:
    job_dir = root / "outside-version"
    job_dir.mkdir()
    block = OPT.managed_block(str(job_dir))
    # Strip the version line from inside the span, so the ONLY version line in
    # the whole prompt sits outside it, in the task body.
    block_no_version = OPT.PROTOCOL_VERSION_RE.sub("(version line removed)", block, count=1)
    prompt = block_no_version + "\n\nTask body.\nProtocol version: 4\n"
    p = _write_job(root, "outside-version", prompt)
    code, _ = OPT.status_of(p, "prompt")
    check("version line only in task body -> malformed-block",
          code == "malformed-block", code)

    raw_before = p.read_bytes()
    _quiet(OPT.apply, [p], "prompt")
    check("apply() leaves that malformed-block file byte-identical",
          p.read_bytes() == raw_before)


def check_newer_than_helper(root: Path) -> None:
    job_dir = root / "newer"
    job_dir.mkdir()
    block = OPT.managed_block(str(job_dir)).replace(
        f"Protocol version: {OPT.PROTOCOL_VERSION}", "Protocol version: 99"
    )
    prompt = block + "\n\nTask body.\n"
    p = _write_job(root, "newer", prompt)
    code, _ = OPT.status_of(p, "prompt")
    check("protocol v99 -> newer-than-helper", code == "newer-than-helper", code)

    raw_before = p.read_bytes()
    _quiet(OPT.apply, [p], "prompt")
    check("apply() leaves newer-than-helper file byte-identical",
          p.read_bytes() == raw_before)


def check_canonical_not_customized(root: Path) -> None:
    job_dir = root / "canonical"
    job_dir.mkdir()
    prompt = OPT.managed_block(str(job_dir)) + "\n\nTask body.\n"
    p = _write_job(root, "canonical", prompt)
    customized, extra = OPT.is_block_customized(prompt, str(job_dir), OPT.PROTOCOL_VERSION)
    check("unmodified canonical v4 block -> NOT customized", not customized, extra)
    code, _ = OPT.status_of(p, "prompt")
    check("unmodified canonical block status != customized-block",
          code != "customized-block", code)


def check_customized_block_detection_and_refusal(root: Path) -> Path:
    job_dir = root / "customized"
    job_dir.mkdir()
    canonical_block = OPT.managed_block(str(job_dir))
    injected_line = "- Custom rule: always ping #eng-oncall before merging."
    span_end = canonical_block.index(OPT.END_MARKER)
    customized_block = (
        canonical_block[:span_end] + injected_line + "\n" + canonical_block[span_end:]
    )
    prompt = customized_block + "\n\nTask body.\n"
    p = _write_job(root, "customized", prompt)

    customized, extra = OPT.is_block_customized(prompt, str(job_dir), OPT.PROTOCOL_VERSION)
    check("block with injected line -> customized", customized, extra)
    check("injected line captured verbatim in the diff", injected_line in extra, extra)

    code, _ = OPT.status_of(p, "prompt")
    check("customized block status == customized-block", code == "customized-block", code)

    raw_before = p.read_bytes()
    _quiet(OPT.apply, [p], "prompt", migrate_custom=False)
    check("apply() WITHOUT --migrate-custom leaves file byte-identical",
          p.read_bytes() == raw_before)
    extract = job_dir / "custom-protocol-extract.md"
    check("custom-protocol-extract.md written", extract.is_file())
    check("extract contains the injected line",
          extract.is_file() and injected_line in extract.read_text(encoding="utf-8"))
    return p


def check_migrate_custom(root: Path, p: Path, injected_line: str) -> None:
    rc, _ = _quiet(OPT.apply, [p], "prompt", migrate_custom=True)
    new_prompt = OPT.parse_prompt(p.read_text(encoding="utf-8"), "prompt")
    check("apply() --migrate-custom exits without file errors", rc == 0, rc)
    check("migrated prompt contains the injected custom line verbatim",
          new_prompt is not None and injected_line in new_prompt,
          new_prompt)
    check("migrated prompt has the extraction heading",
          new_prompt is not None
          and "## Project-specific rules (extracted from protocol block v"
          in new_prompt)
    begin_count = (new_prompt or "").count(OPT.BEGIN_MARKER)
    check("exactly one BEGIN marker after migration", begin_count == 1, begin_count)
    code, detail = OPT.status_of(p, "prompt")
    check("status compliant after migration (post-sidecar-scaffold)",
          code == "compliant", f"got {code}: {detail}")


def check_customization_is_path_insensitive(root: Path) -> None:
    """A canonical block whose state-file paths point at a DIFFERENT dir (e.g.
    when auditing a copy, or after a job dir is renamed) must NOT read as
    customized — only genuine prose changes count, never path differences."""
    canonical_at_a = OPT.managed_block("/home/orig/jobA")
    customized, extra = OPT.is_block_customized(
        canonical_at_a + "\n\nTask body.\n", "/tmp/copy/jobA", OPT.PROTOCOL_VERSION)
    check("canonical block at a different path is NOT customized",
          not customized, extra)
    # But a genuine prose change is still caught even across differing paths.
    injected = "- Custom rule: page oncall before merge."
    end = canonical_at_a.index(OPT.END_MARKER)
    modified = canonical_at_a[:end] + injected + "\n" + canonical_at_a[end:]
    cust2, extra2 = OPT.is_block_customized(
        modified + "\n\nx\n", "/tmp/copy/jobA", OPT.PROTOCOL_VERSION)
    check("genuine customization still detected across differing paths", cust2)
    check("path lines are not mistaken for custom lines in the extract",
          extra2 == [injected], extra2)


def check_v4_upgrades_to_v5(root: Path) -> None:
    """A canonical older-version block is recognized as needs-upgrade and apply()
    rewrites it to the current version, preserving the task body, one block only."""
    job_dir = root / "v4-upgrade"
    job_dir.mkdir()
    body = "Task body that must survive the upgrade.\n"
    prompt = OPT.managed_block(str(job_dir), 4) + "\n\n" + body
    p = _write_job(root, "v4-upgrade", prompt)
    code, detail = OPT.status_of(p, "prompt")
    check("canonical v4 block -> needs-upgrade", code == "needs-upgrade",
          f"got {code}: {detail}")
    _quiet(OPT.apply, [p], "prompt")
    new_prompt = OPT.parse_prompt(p.read_text(encoding="utf-8"), "prompt") or ""
    check("upgraded prompt now carries the current version",
          f"Protocol version: {OPT.PROTOCOL_VERSION}" in new_prompt)
    check("exactly one BEGIN marker after v4->v5 upgrade",
          new_prompt.count(OPT.BEGIN_MARKER) == 1)
    check("task body survived the v4->v5 upgrade",
          "Task body that must survive the upgrade." in new_prompt)
    code2, detail2 = OPT.status_of(p, "prompt")
    check("upgraded v4 job is compliant", code2 == "compliant",
          f"got {code2}: {detail2}")


def check_customized_v4_migrates_to_v5(root: Path) -> None:
    """A hand-customized OLDER block refuses auto-upgrade, but --migrate-custom
    upgrades it to the current version AND preserves the custom line verbatim."""
    job_dir = root / "v4-custom"
    job_dir.mkdir()
    v4_block = OPT.managed_block(str(job_dir), 4)
    injected = "- Custom rule: never merge on Fridays."
    span_end = v4_block.index(OPT.END_MARKER)
    customized = v4_block[:span_end] + injected + "\n" + v4_block[span_end:]
    prompt = customized + "\n\nTask body.\n"
    p = _write_job(root, "v4-custom", prompt)
    code, detail = OPT.status_of(p, "prompt")
    check("customized v4 block -> customized-block", code == "customized-block",
          f"got {code}: {detail}")
    raw_before = p.read_bytes()
    _quiet(OPT.apply, [p], "prompt", migrate_custom=False)
    check("customized v4 refuses upgrade without --migrate-custom (byte-identical)",
          p.read_bytes() == raw_before)
    _quiet(OPT.apply, [p], "prompt", migrate_custom=True)
    migrated = OPT.parse_prompt(p.read_text(encoding="utf-8"), "prompt") or ""
    check("migrated v4 job now carries the current version",
          f"Protocol version: {OPT.PROTOCOL_VERSION}" in migrated)
    check("migrated v4 job preserves the custom line verbatim", injected in migrated)
    check("exactly one BEGIN marker after v4 migrate", migrated.count(OPT.BEGIN_MARKER) == 1)


def check_install_writes_install_info(home: Path) -> Path:
    fake_codex = home / ".codex"
    fake_codex.mkdir(parents=True, exist_ok=True)
    rc, _ = _quiet(INSTALL.main, ["--home", str(home), "--agents", "codex"])
    check("install_skill.main() copy install exits 0", rc == 0, rc)

    # The installed dir is named after the skill source folder (repo root name),
    # which is "automation-optimizer" in dev but "nightshift" in CI (the public
    # repo checkout) — derive it rather than hardcoding.
    info_path = (home / ".codex" / "skills" / INSTALL.SKILL_NAME / "INSTALL-INFO.json")
    check("INSTALL-INFO.json created by copy install", info_path.is_file())
    if info_path.is_file():
        info = json.loads(info_path.read_text(encoding="utf-8"))
        check("INSTALL-INFO.json version matches repo VERSION",
              info.get("version") == REPO_VERSION,
              f"got {info.get('version')!r}, expected {REPO_VERSION!r}")
    return info_path


def check_discover_reports_drift(home: Path, info_path: Path) -> None:
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["version"] = "0.0.1"
    info_path.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")

    _, out = _quiet(DISCOVER.main, ["--home", str(home), "--agent", "codex"])
    check("discover_agents reports DRIFT after version mismatch", "DRIFT" in out, out)
    check("drift output shows the stale installed version", "0.0.1" in out, out)
    check("drift output shows the repo version", REPO_VERSION in out, out)

    # Matching case: restore the real version and confirm drift clears.
    info["version"] = REPO_VERSION
    info_path.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    _, out2 = _quiet(DISCOVER.main, ["--home", str(home), "--agent", "codex"])
    check("discover_agents reports match after version restored",
          "matches repo" in out2, out2)
    check("no DRIFT flagged once versions match", "DRIFT" not in out2, out2)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        check_managed_block_shape(root)
        check_wellformed_v4(root)
        check_missing_end_marker(root)
        check_duplicate_block(root)
        check_version_line_outside_span(root)
        check_newer_than_helper(root)
        check_canonical_not_customized(root)
        check_customization_is_path_insensitive(root)
        customized_path = check_customized_block_detection_and_refusal(root)
        check_migrate_custom(
            root, customized_path,
            "- Custom rule: always ping #eng-oncall before merging.",
        )
        check_v4_upgrades_to_v5(root)
        check_customized_v4_migrates_to_v5(root)

    with tempfile.TemporaryDirectory() as td2:
        install_home = Path(td2)
        info_path = check_install_writes_install_info(install_home)
        if info_path.is_file():
            check_discover_reports_drift(install_home, info_path)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
