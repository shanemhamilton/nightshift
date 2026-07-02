#!/usr/bin/env python3
"""
agent_materializers.py — turn one manifest [[job]] into on-disk reality for each
agent, keyed off its adapter's `scheduler` type. This is the single place that
knows how to *write*, *disable*, and *purge* a job per agent, so lifecycle.py and
scaffold_suite.py stay agent-agnostic.

Schedulers (from agent_adapters.py):
  native_file  (codex, claude) — writing the registry file IS the registration.
                 Codex has a status field (disable = set it inactive); Claude has
                 none (disable = relocate the dir out of the daemon's glob).
  external_cron (gemini)       — no native registry. We write the prompt + sidecars
                 and EMIT the crontab line; we never edit crontab.
  cloud_api     (cursor)       — automations live in the vendor cloud. We EMIT the
                 config/instructions; we never create or delete cloud resources.

The adaptive template bodies (BODIES/DEFAULTS/build_prompt) live here so both
scaffold_suite.py and lifecycle.py share one copy and never drift.
"""
from __future__ import annotations

import importlib.util
import re
import shutil
from pathlib import Path

import agent_adapters
import naming
from pattern_bodies import BODIES, DEFAULTS

# Share the managed block, sidecar templates, and fingerprint from the optimizer
# (single source of truth) by loading it by path — robust regardless of cwd.
_OPT_PATH = Path(__file__).resolve().parent / "optimize_codex_automations.py"
_spec = importlib.util.spec_from_file_location("ao_opt", _OPT_PATH)
OPT = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(OPT)  # type: ignore

DEFAULT_INTEGRATOR = "repo-hygiene"


def build_prompt(job: dict, job_dir: str) -> str:
    """Managed protocol block (with this job's absolute state paths) + adaptive body."""
    template = job["template"]
    params = dict(DEFAULTS.get(template, {}))
    params.update(job.get("params", {}) or {})
    params["integrator"] = job.get("hands_off_to") or DEFAULT_INTEGRATOR
    body = BODIES[template].format(**params)
    return OPT.managed_block(job_dir) + "\n\n" + body


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def cron_to_rrule(schedule: str) -> str:
    """Convert a simple 5-field cron (m h dom mon dow) to an iCalendar RRULE.
    Handles the common nightly/weekly cases the composer emits."""
    parts = (schedule or "").split()
    if len(parts) != 5:
        return "FREQ=DAILY;BYHOUR=3;BYMINUTE=0;BYSECOND=0"
    m, h, _dom, _mon, dow = parts
    minute = m if m.isdigit() else "0"
    hour = h if h.isdigit() else "3"
    if dow != "*":
        days = {"0": "SU", "1": "MO", "2": "TU", "3": "WE",
                "4": "TH", "5": "FR", "6": "SA"}
        byday = ",".join(days.get(d, "MO") for d in dow.split(","))
        return f"FREQ=WEEKLY;BYDAY={byday};BYHOUR={hour};BYMINUTE={minute};BYSECOND=0"
    return f"FREQ=DAILY;BYHOUR={hour};BYMINUTE={minute};BYSECOND=0"


_HAIKU_RE = re.compile(r"haiku", re.IGNORECASE)


def emit_codex_toml(job: dict, prompt: str, cwd: str, model: str | None,
                    project: str | None = None) -> str:
    """Emit a real Codex automation.toml (version/id/kind/name/prompt/status/rrule/cwds).

    model precedence:            `model` PARAMETER > job.get("model") > DEFAULTS[template].get("model")
    reasoning_effort precedence: job.get("reasoning_effort") > DEFAULTS[template].get("reasoning_effort")
    Never emits a Haiku-class model (Sonnet is the floor) — raises ValueError instead.
    """
    if "'''" in prompt:  # literal triple-quote can't contain '''
        raise ValueError(f"{job.get('id')}: prompt contains ''' and can't be emitted")
    name = naming.display_name(job, project)
    defaults = DEFAULTS.get(job["template"], {})
    resolved_model = model or job.get("model") or defaults.get("model")
    if resolved_model and _HAIKU_RE.search(resolved_model):
        raise ValueError(f"{job['id']}: refusing to emit a Haiku-class model ({resolved_model})")
    resolved_effort = job.get("reasoning_effort") or defaults.get("reasoning_effort")
    lines = [
        "version = 1",
        f"id = {_q(job['id'])}",
        'kind = "cron"',
        f"name = {_q(name)}",
        f"# template = {job['template']} | phase = {job.get('phase','?')} | "
        f"mode = {job.get('mode','active')} | merge_authority = {bool(job.get('merge_authority'))}",
        "prompt = '''",
        prompt,
        "'''",
        'status = "ACTIVE"',
        f"rrule = {_q(cron_to_rrule(job.get('schedule', '')))}",
        f"execution_environment = {_q(job.get('execution_environment', 'local'))}",
        f"cwds = [{_q(cwd)}]",
    ]
    if resolved_model:
        lines.append(f"model = {_q(resolved_model)}")
    if resolved_effort:
        lines.append(f"reasoning_effort = {_q(resolved_effort)}")
    return "\n".join(lines) + "\n"


def _skill_md(job: dict, prompt: str, project: str | None = None) -> str:
    """A Claude scheduled-task SKILL.md: frontmatter + the prompt the daemon runs."""
    name = naming.display_name(job, project)
    desc = (f"Recurring {job['template']} automation ({job.get('phase','?')}). "
            f"Cadence set via the Claude scheduler.")
    return (f"---\nname: {job['id']}\ndescription: {desc}\n---\n\n"
            f"# {name}\n\n{prompt}\n")


def _archive_and_delete(job_dir: Path, archive_root: Path, jid: str) -> str:
    """Move sidecar state into an archive, then delete the job dir. Reversible-ish:
    the archive keeps memory/ledgers; only the registry entry is gone."""
    dest = archive_root / f"{jid}-{OPT.timestamp()}"
    dest.mkdir(parents=True, exist_ok=True)
    for name in OPT.REQUIRED_SIDECARS + ["runs"]:
        src = job_dir / name
        if src.exists():
            shutil.move(str(src), str(dest / name))
    shutil.rmtree(job_dir, ignore_errors=True)
    return str(dest)


# --- public API: dispatch on the adapter's scheduler -------------------------
def _root(agent: str, home_override: str | None) -> Path:
    cfg = agent_adapters.get(agent)
    root = cfg["automations_root"]
    base = Path(home_override).expanduser() if home_override else None
    if root is None:  # cursor: no local registry
        return base or Path(cfg["home_dir"]).expanduser()
    p = Path(root).expanduser()
    if base is not None:  # redirect the agent's home for tests
        p = base / p.name
    return p


def materialize(agent: str, job: dict, *, cwd: str, model: str | None = None,
                apply: bool = False, home_override: str | None = None,
                project: str | None = None) -> dict:
    """Write/emit one job for `agent`. Returns {action, path|emit, notes}."""
    cfg = agent_adapters.get(agent)
    sched = cfg["scheduler"]
    jid = job["id"]
    root = _root(agent, home_override)

    if agent == "codex":
        job_dir = root / jid
        prompt = build_prompt(job, str(job_dir))
        text = emit_codex_toml(job, prompt, cwd, model, project)
        if apply:
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "automation.toml").write_text(text, encoding="utf-8")
            OPT.scaffold_sidecars(job_dir, [])
        return {"action": "write", "path": str(job_dir / "automation.toml"),
                "notes": ""}

    if agent == "claude":
        job_dir = root / jid
        prompt = build_prompt(job, str(job_dir))
        text = _skill_md(job, prompt, project)
        if apply:
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "SKILL.md").write_text(text, encoding="utf-8")
            OPT.scaffold_sidecars(job_dir, [])
        return {"action": "write", "path": str(job_dir / "SKILL.md"),
                "notes": "set/confirm cadence via the Claude scheduler "
                         "(mcp__scheduled-tasks__update_scheduled_task), "
                         f"schedule '{job.get('schedule','?')}'"}

    if sched == "external_cron":  # gemini
        job_dir = root / jid
        prompt = build_prompt(job, str(job_dir))
        prompt_file = job_dir / "prompt.md"
        run_cmd = cfg["run_command"].format(prompt_file=prompt_file)
        cron_line = f"{job.get('schedule','0 3 * * *')} cd {cwd} && {run_cmd}"
        if apply:
            job_dir.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text(prompt, encoding="utf-8")
            OPT.scaffold_sidecars(job_dir, [])
        return {"action": "write+emit", "path": str(prompt_file),
                "emit": f"# add to crontab (we never edit cron):\n{cron_line}",
                "notes": "crontab line emitted; apply it yourself"}

    # cloud_api: cursor — emit only, never touch the cloud
    payload = (f"# Cursor cloud automation '{jid}' ({job['template']}, "
               f"{job.get('phase','?')}), schedule '{job.get('schedule','?')}', "
               f"workspace {cwd}.\n# Create it in the Cursor dashboard/API; "
               f"add the managed protocol block to its prompt.")
    return {"action": "emit", "emit": payload,
            "notes": "cloud resource — create it yourself in Cursor"}


def disable(agent: str, job: dict, *, apply: bool = False,
            home_override: str | None = None) -> dict:
    """Stop a job scheduling without deleting it. Reversible."""
    cfg = agent_adapters.get(agent)
    jid = job["id"]
    root = _root(agent, home_override)

    if cfg.get("status_field"):  # codex: flip status inactive
        toml_path = root / jid / "automation.toml"
        if apply and toml_path.is_file():
            raw = toml_path.read_text(encoding="utf-8")
            import re
            new = re.sub(r'(?m)^status\s*=\s*".*"$', 'status = "disabled"', raw)
            toml_path.write_text(new, encoding="utf-8")
        return {"action": "disable", "path": str(toml_path),
                "notes": 'status set to "disabled"'}

    if cfg["scheduler"] == "native_file":  # claude: relocate out of the glob
        src = root / jid
        dest = root / ".disabled" / jid
        if apply and src.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        return {"action": "disable", "path": str(dest),
                "notes": "relocated under .disabled/ (daemon no longer sees it)"}

    if cfg["scheduler"] == "external_cron":  # gemini: emit removal
        return {"action": "emit",
                "emit": f"# remove the crontab line for '{jid}' "
                        f"(we never edit cron). Files kept under {root / jid}.",
                "notes": "comment/remove the crontab line yourself"}

    return {"action": "emit",  # cursor
            "emit": f"# Disable Cursor automation '{jid}' in the dashboard/API.",
            "notes": "disable it yourself in Cursor"}


def purge(agent: str, job: dict, *, apply: bool = False,
          home_override: str | None = None) -> dict:
    """Archive sidecar state, then delete the job's local registry dir."""
    cfg = agent_adapters.get(agent)
    jid = job["id"]
    root = _root(agent, home_override)

    if cfg["automations_root"] is None:  # cursor: nothing local to delete
        return {"action": "emit",
                "emit": f"# Delete Cursor automation '{jid}' in the dashboard/API.",
                "notes": "delete it yourself in Cursor"}

    job_dir = root / jid
    archive_root = root / ".archive"
    if apply and job_dir.is_dir():
        dest = _archive_and_delete(job_dir, archive_root, jid)
        return {"action": "purge", "path": dest,
                "notes": f"sidecars archived to {dest}, registry dir deleted"}
    return {"action": "purge", "path": str(job_dir),
            "notes": f"would archive sidecars to {archive_root} and delete dir"}
