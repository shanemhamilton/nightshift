#!/usr/bin/env python3
"""
selftest_digest.py — end-to-end checks for approval_digest.py (D1/D2/D4).

Builds a throwaway codex-home under a temp dir with `<home>/automations/<job>/
human-approval.md` fixtures, exercises the digest + resolve round-trip, the
D2 classify()/aging rules, and the D4 emit-only artifacts. No network, no real
~/.codex touched — every invocation passes --codex-home into the temp dir.

Run: python3 scripts/selftest_digest.py   (exit 0 = all pass)
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import io
import re
import shutil
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


AD = _load("ad", "approval_digest.py")

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


def run_main(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = AD.main(argv)
    return rc, buf.getvalue()


def make_job(autos: Path, job_id: str, body: str) -> Path:
    d = autos / job_id
    d.mkdir(parents=True, exist_ok=True)
    f = d / "human-approval.md"
    f.write_text(body, encoding="utf-8")
    return f


STRUCTURED_ITEM = """## Delete the stale feature branch after merge
- risk: low
- suggested_default: delete it
- action: git push origin --delete feature/old-thing
- first_seen: {date}
- evidence: automations/demo-app/runs/2026-06-20.md
"""


def test_round_trip(root: Path) -> None:
    codex_home = root / "rt-home"
    autos = codex_home / "automations"
    today = _dt.date.today()
    fseen = (today - _dt.timedelta(days=1)).isoformat()
    job_path = make_job(autos, "demo-app-repo-hygiene",
                        STRUCTURED_ITEM.format(date=fseen))

    rc, out = run_main(["--write", "--codex-home", str(codex_home)])
    check("round-trip: --write exits 0", rc == 0, out)
    digest_path = codex_home / "DAILY-APPROVALS.md"
    check("round-trip: digest written", digest_path.is_file())
    digest_text = digest_path.read_text(encoding="utf-8")
    check("round-trip: has approve checkbox", "- [ ] approve" in digest_text, digest_text)
    check("round-trip: has ao:item marker", "<!-- ao:item id=ao_" in digest_text, digest_text)

    marker = re.search(r"<!-- ao:item id=(ao_[0-9a-f]{8}) src=(\S+) srchash=([0-9a-f]{12}) -->",
                       digest_text)
    check("round-trip: marker parses", marker is not None, digest_text)
    if not marker:
        return
    check("round-trip: marker src matches source file",
          Path(marker.group(2)) == job_path.resolve(), marker.group(2))

    # Operator edits the digest: check the box, add a decision.
    edited = digest_text.replace("- [ ] approve", "- [x] approve", 1)
    edited = edited.replace(
        marker.group(0), f"decision: go ahead\n{marker.group(0)}", 1)
    digest_path.write_text(edited, encoding="utf-8")

    rc, out = run_main(["resolve", "--digest", str(digest_path),
                        "--codex-home", str(codex_home)])
    check("round-trip: resolve exits 0", rc == 0, out)
    check("round-trip: resolve reports 1 resolved", "1 resolved" in out, out)

    src_after = job_path.read_text(encoding="utf-8")
    check("round-trip: item moved to ## Resolved in source",
          "## Resolved" in src_after and "go ahead" in src_after, src_after)
    check("round-trip: original item heading removed from source",
          "## Delete the stale feature branch after merge" not in
          src_after.split("## Resolved")[0], src_after)

    memory_path = job_path.parent / "memory.md"
    check("round-trip: memory.md created", memory_path.is_file())
    if memory_path.is_file():
        mem_text = memory_path.read_text(encoding="utf-8")
        check("round-trip: decision landed in ## Stable decisions",
              "## Stable decisions" in mem_text and "go ahead" in mem_text, mem_text)

    # Fresh digest now shows one fewer pending item.
    rc, out2 = run_main(["--codex-home", str(codex_home)])
    check("round-trip: fresh digest drops resolved item",
          "Delete the stale feature branch after merge" not in out2, out2)


def test_stale_source_refusal(root: Path) -> None:
    codex_home = root / "stale-home"
    autos = codex_home / "automations"
    today = _dt.date.today()
    fseen = (today - _dt.timedelta(days=1)).isoformat()
    job_path = make_job(autos, "demo-app-repo-hygiene",
                        STRUCTURED_ITEM.format(date=fseen))

    rc, out = run_main(["--write", "--codex-home", str(codex_home)])
    check("stale: --write exits 0", rc == 0, out)
    digest_path = codex_home / "DAILY-APPROVALS.md"
    digest_text = digest_path.read_text(encoding="utf-8")

    # Modify the source AFTER the digest was generated (hash now stale).
    job_path.write_text(job_path.read_text(encoding="utf-8") + "\n<!-- edited -->\n",
                        encoding="utf-8")

    edited = digest_text.replace("- [ ] approve", "- [x] approve", 1)
    digest_path.write_text(edited, encoding="utf-8")

    rc, out = run_main(["resolve", "--digest", str(digest_path),
                        "--codex-home", str(codex_home)])
    check("stale: resolve exits 0", rc == 0, out)
    check("stale: resolve reports 0 resolved, 1 skipped",
          "0 resolved, 1 skipped" in out, out)
    check("stale: skip reason mentions source changed",
          "source changed since digest" in out, out)

    src_after = job_path.read_text(encoding="utf-8")
    check("stale: item NOT moved to Resolved", "## Resolved" not in src_after, src_after)


def test_classify_table(root: Path) -> None:
    cases = [
        ({"ask": "bump a dependency", "risk": "low", "_explicit_risk": True}, "safe",
         "risk: low -> safe"),
        ({"ask": "delete stale branch feature/xyz", "risk": "unknown",
          "_explicit_risk": False}, "safe",
         "unknown risk + SAFE_HINTS, no risk field -> safe"),
        ({"ask": "delete the release branch on origin/main", "risk": "unknown",
          "_explicit_risk": False}, "judgment",
         "deny regex beats SAFE_HINTS -> judgment"),
        ({"ask": "deploy the new build to prod", "risk": "high",
          "_explicit_risk": True}, "judgment",
         "risk: high deploy -> judgment"),
    ]
    for item, expected, label in cases:
        got = AD.classify(item)
        check(f"classify: {label}", got == expected, f"got {got!r}")


def test_aging(root: Path) -> None:
    today = _dt.date(2026, 7, 1)
    old_date = (today - _dt.timedelta(days=10)).isoformat()
    recent_date = (today - _dt.timedelta(days=1)).isoformat()
    items = [
        {"ask": "old risky thing", "risk": "high", "_explicit_risk": True,
         "action": "review", "first_seen": old_date, "project": "Demo",
         "agent": "codex", "source": "demo-job", "_srcpath": "/tmp/x.md",
         "_srchash": "0" * 12},
        {"ask": "recent risky thing", "risk": "high", "_explicit_risk": True,
         "action": "review", "first_seen": recent_date, "project": "Demo",
         "agent": "codex", "source": "demo-job", "_srcpath": "/tmp/x.md",
         "_srchash": "0" * 12},
    ]
    digest = AD.build_digest(items, [], today)
    check("aging: old item marked AGED", "⚠ AGED" in digest, digest)
    needs_judgment = digest.split("## Needs judgment")[1].split("## Safe")[0]
    aged_pos = needs_judgment.find("old risky thing")
    recent_pos = needs_judgment.find("recent risky thing")
    check("aging: AGED item sorts first within Needs judgment",
          0 <= aged_pos < recent_pos, needs_judgment)
    check("aging: is_aged() true at >=7 days",
          AD.is_aged(items[0], today) is True)
    check("aging: is_aged() false at 1 day",
          AD.is_aged(items[1], today) is False)


def _plutil_available() -> bool:
    return shutil.which("plutil") is not None


def test_emit_launchd_cron(root: Path) -> None:
    rc, out = run_main(["--emit-launchd"])
    check("emit-launchd: exits 0", rc == 0, out)
    check("emit-launchd: looks like a plist",
          out.strip().startswith("<?xml") and "<plist" in out, out[:200])

    if _plutil_available():
        plist_path = root / "digest.plist"
        plist_path.write_text(out, encoding="utf-8")
        proc = subprocess.run(["plutil", "-lint", str(plist_path)],
                              capture_output=True, text=True)
        check("emit-launchd: passes plutil -lint",
              proc.returncode == 0 and "OK" in proc.stdout,
              proc.stdout + proc.stderr)
    else:
        print("  SKIP  emit-launchd: plutil not available on this host")

    rc, out = run_main(["--emit-cron"])
    check("emit-cron: exits 0", rc == 0, out)
    check("emit-cron: starts with 40 6 * * *", out.startswith("40 6 * * *"), out)


def test_nonexistent_codex_home() -> None:
    rc, out = run_main(["--codex-home", "/tmp/nonexistent-xyz-selftest-digest"])
    check("nonexistent codex-home: exits 0", rc == 0, out)
    check("nonexistent codex-home: prints an empty-ish digest",
          "0 item(s)" in out, out)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        test_round_trip(root)
        test_stale_source_refusal(root)
        test_classify_table(root)
        test_aging(root)
        test_emit_launchd_cron(root)
        test_nonexistent_codex_home()

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
