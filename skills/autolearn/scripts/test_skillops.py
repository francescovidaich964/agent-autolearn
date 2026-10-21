"""Tests for skill operations (RL-SKL-001..007, RL-INP-004)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import skillops  # noqa: E402

SKILL = """---
name: demo
description: |
  Deploy the demo service to the lab cluster.
created_by: autolearn
created_at: "2026-09-01"
---

# Demo

## Steps

- Build with `make build`.
- Copy with `scp build/demo.tar lab:/srv/demo/` (2026-09-02).
- Restart with `systemctl restart demo`.
"""


@pytest.fixture
def skills(tmp_path):
    root = tmp_path / "skills"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Exact edits
# ---------------------------------------------------------------------------

def test_exact_replace_requires_unique_match():
    assert skillops.exact_replace("a b c", "b", "X") == "a X c"
    with pytest.raises(skillops.SkillOpError):
        skillops.exact_replace("a b c", "zzz", "X")
    with pytest.raises(skillops.SkillOpError):
        skillops.exact_replace("b b", "b", "X")


def test_apply_edit_backs_up_and_replaces(skills):
    res = skillops.apply_edit(skills, "demo", "Restart with `systemctl restart demo`.",
                              "Restart with `systemctl restart demo --now`.")
    text = (skills / "demo" / "SKILL.md").read_text(encoding="utf-8")
    assert "--now" in text and text.count("Restart") == 1          # replaced, not appended
    assert Path(res["backup"]).read_text(encoding="utf-8") == SKILL


def test_resolve_file_stays_inside_skill(skills):
    with pytest.raises(skillops.SkillOpError):
        skillops.resolve_file(skills, "demo", "../../etc/passwd")


# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------

def test_set_description_replaces_block_scalar():
    new = skillops.set_frontmatter_block(SKILL, "description", skillops.description_block("Use when shipping the demo service"))
    assert skillops.frontmatter_value(new, "description") == "Use when shipping the demo service"
    assert "Deploy the demo service" not in new.split("---")[1]
    assert "# Demo" in new
    skillops.validate_frontmatter(new)


def test_set_verify_adds_then_replaces():
    once = skillops.set_frontmatter_block(SKILL, "verify", skillops.verify_block("make check", 0))
    twice = skillops.set_frontmatter_block(once, "verify", skillops.verify_block("make test", 1))
    fm = twice.split("---")[1]
    assert fm.count("verify:") == 1 and '"make test"' in fm and "expect_exit: 1" in fm
    skillops.validate_frontmatter(twice)


# ---------------------------------------------------------------------------
# Lint + preservation
# ---------------------------------------------------------------------------

def test_lint_flags_incident_log_shape_and_size(tmp_path):
    p = tmp_path / "SKILL.md"
    body = "\n".join(f"- fix {i} (2026-09-{i:02d}) supersedes the previous bullet" for i in range(1, 12))
    p.write_text(SKILL + body + "\n" + "x" * 13000, encoding="utf-8")
    rules = {f["rule"] for f in skillops.lint_skill(p)}
    assert rules == {"oversized", "incident-log-shape"}
    (tmp_path / "ok").mkdir()
    q = tmp_path / "ok" / "SKILL.md"
    q.write_text(SKILL, encoding="utf-8")
    assert skillops.lint_skill(q) == []


def test_preservation_report_catches_dropped_commands():
    rewritten = SKILL.replace("- Restart with `systemctl restart demo`.\n", "")
    rep = skillops.preservation_report(SKILL, [rewritten])
    assert "systemctl restart demo" in rep["missing"]
    moved = skillops.preservation_report(SKILL, [rewritten, "ref: run `systemctl restart demo`"])
    assert moved["missing"] == []


# ---------------------------------------------------------------------------
# Audit (session index)
# ---------------------------------------------------------------------------

def _search_db(path: Path, rows):
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE session_text_content(rowid INTEGER PRIMARY KEY, session_id TEXT, message_id TEXT,
            role TEXT, text TEXT, project TEXT DEFAULT '', timestamp INTEGER);
        CREATE VIRTUAL TABLE session_text USING fts5(session_id, message_id, role, text, project, timestamp,
            content=session_text_content, content_rowid=rowid);
    """)
    for i, (sid, role, text, ts) in enumerate(rows, 1):
        con.execute("INSERT INTO session_text_content VALUES (?,?,?,?,?,?,?)", (i, sid, f"m{i}", role, text, "", ts))
        con.execute("INSERT INTO session_text(rowid, session_id, message_id, role, text, project, timestamp) VALUES (?,?,?,?,?,?,?)",
                    (i, sid, f"m{i}", role, text, "", ts))
    con.commit()
    con.close()


def test_audit_separates_failed_trigger_from_dormant(skills, tmp_path):
    (skills / "quiet").mkdir()
    (skills / "quiet" / "SKILL.md").write_text(SKILL.replace("name: demo", "name: quiet").replace(
        "Deploy the demo service to the lab cluster.", "Calibrate the oscilloscope probes before captures."), encoding="utf-8")
    created = date.today() - timedelta(days=40)
    later = int((datetime.now() - timedelta(days=10)).timestamp() * 1000)
    db = tmp_path / "search.db"
    _search_db(db, [
        ("s1", "user", "can you deploy the demo service to the lab cluster again?", later),
        ("s2", "assistant", "deploy demo service lab cluster", later),        # assistant: ignored
        ("s3", "user", "what is the weather", later),
    ])
    usage = {n: {"created_by": "autolearn", "created_at": created.isoformat(), "use_count": 0, "state": "active"}
             for n in ("demo", "quiet")}
    rows = {r["skill"]: r for r in skillops.audit_never_loaded(skills, usage, db)}
    assert rows["demo"]["verdict"] == "fix-description" and rows["demo"]["recurred_sessions"] == 1
    assert rows["quiet"]["verdict"] == "dormant"
    usage["demo"]["pinned"] = True
    assert "demo" not in {r["skill"] for r in skillops.audit_never_loaded(skills, usage, db)}


# ---------------------------------------------------------------------------
# CLI: reviewer gate, propose -> apply, rewrite, pin cap
# ---------------------------------------------------------------------------

def _cli(home: Path, *args, role: str | None = None):
    env = dict(os.environ, AUTOLEARN_HOME=str(home), AGENTS_SKILLS_DIR=str(home / "agents-skills"),
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    for k in ("AUTOLEARN_REVIEWER", "AUTOLEARN_CURATOR", "AUTOLEARN_SESSION_ID"):
        env.pop(k, None)
    if role == "reviewer":
        env["AUTOLEARN_REVIEWER"] = "1"
    if role == "curator":
        env["AUTOLEARN_CURATOR"] = "1"
    if role == "session":
        env["AUTOLEARN_SESSION_ID"] = "ses_test"
    return subprocess.run([sys.executable, str(HERE / "autolearn.py"), *args],
                          capture_output=True, text=True, env=env, timeout=120)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "al"
    p = h / "personas" / "default"
    (p / "skills" / "demo").mkdir(parents=True)
    (p / "skills" / "demo" / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (p / "skills" / ".usage.json").write_text(json.dumps({"demo": {
        "created_by": "autolearn", "created_at": "2026-09-01", "use_count": 0, "patch_count": 0,
        "last_activity_at": "2026-09-01", "state": "active"}}), encoding="utf-8")
    (p / "memories.jsonl").write_text("")
    return h


def _skill_text(home):
    return (home / "personas" / "default" / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8")


def test_reviewer_cannot_edit_or_patch(home):
    r = _cli(home, "skill", "edit", "demo", "--old", "make build", "--new", "make all", role="reviewer")
    assert r.returncode == 4 and "propose" in r.stderr
    r = _cli(home, "skill", "patch", "demo", "Steps", "extra bullet", role="reviewer")
    assert r.returncode == 4
    assert _skill_text(home) == SKILL


def test_session_edit_records_session_and_patch_count(home):
    r = _cli(home, "skill", "edit", "demo", "--old", "make build", "--new", "make all", role="session")
    assert r.returncode == 0, r.stderr
    assert "make all" in _skill_text(home)
    usage = json.loads((home / "personas" / "default" / "skills" / ".usage.json").read_text(encoding="utf-8"))
    assert usage["demo"]["patch_count"] == 1 and usage["demo"]["last_edit_session"] == "ses_test"
    obs = (home / "personas" / "default" / "observations.jsonl").read_text(encoding="utf-8")
    assert '"skill_edited"' in obs and '"role": "session"' in obs


def test_propose_then_apply_by_session(home):
    r = _cli(home, "skill", "propose", "demo", "--old", "systemctl restart demo", "--new",
             "systemctl restart demo --now", "--reason", "user: restart needs --now", role="reviewer")
    assert r.returncode == 0, r.stderr
    props = json.loads((home / "personas" / "default" / "skills" / ".proposals.json").read_text(encoding="utf-8"))
    pid = props[0]["id"]
    assert props[0]["source"] == "reviewer" and _skill_text(home) == SKILL
    assert _cli(home, "skill", "apply", pid, role="reviewer").returncode == 4
    assert _cli(home, "skill", "apply", pid, role="session").returncode == 0
    assert "--now" in _skill_text(home)
    props = json.loads((home / "personas" / "default" / "skills" / ".proposals.json").read_text(encoding="utf-8"))
    assert props[0]["status"] == "applied"


def test_propose_with_stale_old_text_is_refused(home):
    r = _cli(home, "skill", "propose", "demo", "--old", "not in the skill", "--new", "x", "--reason", "r", role="reviewer")
    assert r.returncode == 2


def test_rewrite_refused_when_it_drops_commands_then_accepted_when_moved(home, tmp_path):
    lossy = tmp_path / "new.md"
    lossy.write_text(SKILL.replace("- Restart with `systemctl restart demo`.\n", ""), encoding="utf-8")
    out = _cli(home, "skill", "rewrite", "demo", "--from-file", str(lossy), "--reason", "lint", role="curator")
    assert out.returncode == 0 and "1 missing" in out.stdout
    pid = json.loads((home / "personas" / "default" / "skills" / ".proposals.json").read_text(encoding="utf-8"))[0]["id"]
    assert _cli(home, "skill", "apply", pid, role="curator").returncode == 4       # curator: auto-apply off
    refused = _cli(home, "skill", "apply", pid, role="session")
    assert refused.returncode == 2 and "systemctl restart demo" in refused.stderr
    refs = home / "personas" / "default" / "skills" / "demo" / "references"
    refs.mkdir()
    (refs / "ops.md").write_text("Restart: `systemctl restart demo`\n", encoding="utf-8")
    assert _cli(home, "skill", "apply", pid, role="session").returncode == 0
    assert "Restart with" not in _skill_text(home)


def test_pin_requires_reason_and_is_capped(home):
    p = home / "personas" / "default"
    usage = json.loads((p / "skills" / ".usage.json").read_text(encoding="utf-8"))
    for i in range(5):
        usage[f"s{i}"] = {"created_by": "autolearn", "pinned": True, "state": "active"}
    (p / "skills" / ".usage.json").write_text(json.dumps(usage), encoding="utf-8")
    assert _cli(home, "skill", "pin", "demo").returncode == 2                     # argparse: --reason required
    capped = _cli(home, "skill", "pin", "demo", "--reason", "incident runbook")
    assert capped.returncode == 2 and "already pinned" in capped.stderr


def test_set_description_and_verify_via_cli(home):
    assert _cli(home, "skill", "set-description", "demo", "Use when the user asks to ship or redeploy the demo service",
                role="reviewer").returncode == 4
    assert _cli(home, "skill", "set-description", "demo", "Use when the user asks to ship or redeploy the demo service",
                role="curator").returncode == 0
    assert _cli(home, "skill", "set-verify", "demo", "make check", "--expect-exit", "0").returncode == 0
    text = _skill_text(home)
    assert skillops.frontmatter_value(text, "description").startswith("Use when the user asks")
    assert '"make check"' in text
