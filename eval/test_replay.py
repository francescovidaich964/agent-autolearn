"""Offline tests for the replay harness (no model calls)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import judge  # noqa: E402
import replay  # noqa: E402

HOME = str(Path.home()).replace("\\", "/")


def test_rewrite_paths_redirects_every_live_form(tmp_path):
    skill = Path(HOME) / ".autolearn-eval" / "runs" / "r1" / "src-new" / "skills" / "autolearn"
    store = Path(HOME) / ".autolearn-eval" / "runs" / "r1" / "new" / "case" / "al"
    text = ("uv run $HOME/.agents/skills/autolearn/scripts/autolearn.py memory add x\n"
            "uv run ~/.agents/skills/autolearn/scripts/improve.py observe y\n"
            "Store root: `~/.autolearn/personas/default/` and ${HOME}/.autolearn/wiki\n"
            f"absolute {HOME}/.autolearn/personas/default/skills/demo/SKILL.md\n"
            "follow references/observer.md for escalation\n"
            "keep ~/.autolearn-eval/notes untouched\n")
    out = replay.rewrite_paths(text, skill, store)
    s, h = replay.posix(skill), replay.posix(store)
    assert f"{s}/scripts/autolearn.py" in out and f"{s}/scripts/improve.py" in out
    assert f"`{h}/personas/default/`" in out and f"{h}/wiki" in out
    assert f"{h}/personas/default/skills/demo/SKILL.md" in out
    assert f"{s}/references/observer.md" in out and out.count("references/observer.md") == 1
    assert "~/.autolearn-eval/notes" in out                       # not a store path
    assert "$HOME/.agents" not in out and "~/.autolearn/" not in out
    assert "al-eval" not in out                                   # the old double-substitution bug


def test_build_message_replaces_skill_load_instruction(tmp_path):
    skill = tmp_path / "skills" / "autolearn"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: autolearn\n---\nTAXONOMY uses ~/.autolearn", encoding="utf-8")
    (skill / "references" / "reviewer.md").write_text("PROTOCOL $HOME/.agents/skills/autolearn/scripts/autolearn.py", encoding="utf-8")
    review = ("# Autolearn Review\n\n## Instructions\n\nReview the conversation below for learning opportunities.\n"
              "Load the autolearn skill and follow references/reviewer.md for the\nfull signal taxonomy and action protocol.\n\n"
              "## Conversation\n\n### User\n\nhi\n")
    msg = replay.build_message(review, skill, tmp_path / "al")
    assert "Load the autolearn skill" not in msg
    assert "TAXONOMY" in msg and "PROTOCOL" in msg and "name: autolearn" not in msg
    assert replay.posix(skill) + "/scripts/autolearn.py" in msg


def _rec(id_, text, **kw):
    return {"id": id_, "text": text, "type": kw.get("type", "memory"), "status": kw.get("status", "active"),
            "reinforcements": kw.get("reinforcements", []), "layer": kw.get("layer")}


def test_diff_states_classifies_changes(tmp_path):
    pdir = tmp_path
    (pdir / "observations.jsonl").write_text('{"type":"x"}\n{"type":"review_complete"}\n{"type":"write_refused"}\n', encoding="utf-8")
    before = {"memories": {"a": _rec("a", "scp from hosta to hostb fails relay via relay101"),
                           "b": _rec("b", "old text about pytest"),
                           "c": _rec("c", "reinforce me about uv run", reinforcements=[])},
              "skills": {"demo": "v1", "other": "x"}, "wiki": {"p.md": "1"}, "proposals": [],
              "observations": 1, "context_entries": 3, "context_chars": 100, "rules_sha": "r1", "rules_text": ""}
    after = {"memories": {**before["memories"],
                          "b": _rec("b", "new text about pytest -x"),
                          "c": _rec("c", "reinforce me about uv run", reinforcements=["2026-10-13"]),
                          "d": _rec("d", "hosta to hostb scp fails; relay through relay101", layer="archive"),
                          "e": _rec("e", "brand new lesson about kubernetes probes", layer="core")},
             "skills": {"demo": "v1 plus appended bullet", "other": "x", "fresh": "new skill"},
             "wiki": {"p.md": "2", "q.md": "1"}, "proposals": [{"id": "p1", "skill": "other"}],
             "observations": 3, "context_entries": 4, "context_chars": 140, "rules_sha": "r2", "rules_text": ""}
    d = replay.diff_states(before, after, pdir, "we talked about other things")
    assert d["memories_added"] == 2 and d["memories_added_dup_of_existing"] == 1
    assert d["memories_added_by_layer"] == {"core": 1, "archive": 1, "none": 0}
    assert d["memories_edited"] == 1 and d["memories_reinforced"] == 1
    assert d["skills_created"] == ["fresh"] and d["skills_modified"] == ["demo"]
    assert d["skills_modified_not_in_conversation"] == ["demo"]
    assert d["skill_proposals"] == 1 and d["wiki_files_changed"] == ["p.md", "q.md"]
    assert d["rules_changed"] and d["review_complete_logged"] and d["write_refused"] == 1
    md = replay.changes_markdown(d, before, after)
    assert "kubernetes probes" in md and "MODIFIED demo" in md


def test_run_events_stats(tmp_path):
    ev = tmp_path / "events.jsonl"
    ev.write_text("\n".join(json.dumps(e) for e in [
        {"type": "step_start", "sessionID": "ses_1", "part": {}},
        {"type": "tool_use", "sessionID": "ses_1", "part": {}},
        {"type": "step_finish", "sessionID": "ses_1", "part": {"reason": "tool-calls", "tokens": {"input": 100, "output": 10}, "cost": 0.01}},
        {"type": "text", "sessionID": "ses_1", "part": {"text": "Autolearn review complete: nothing to record."}},
        {"type": "step_finish", "sessionID": "ses_1", "part": {"reason": "stop", "tokens": {"input": 50, "output": 5}, "cost": 0.0}},
    ]), encoding="utf-8")
    s = replay.run_events_stats(ev)
    assert s["steps"] == 2 and s["tool_calls"] == 1 and s["tokens_in"] == 150 and s["session_id"] == "ses_1"
    assert not s["hit_step_limit"] and "nothing to record" in s["final_text"]
    capped = tmp_path / "capped.jsonl"
    capped.write_text("\n".join(json.dumps({"type": "step_finish", "part": {"reason": "stop"}})
                                for _ in range(replay.REPLAY_STEPS)), encoding="utf-8")
    assert replay.run_events_stats(capped)["hit_step_limit"]


@pytest.mark.parametrize("text,ok", [
    ('{"A": {"recall": 4}, "B": {"recall": 2}, "preferred": "A", "rationale": "x"}', True),
    ('noise before {"A": {}, "B": {}, "preferred": "tie"} noise after', True),
    ('{"A": {}, "B": {}, "preferred": "C"}', False),
    ("no json at all", False),
])
def test_parse_verdict(text, ok):
    assert (judge.parse_verdict(text) is not None) == ok


def test_prompt_truncates_long_conversations():
    p = judge.build_prompt("x" * 50000, "A changes", "B changes", 1000)
    assert "middle truncated" in p and len(p) < 6000


def test_external_dirs_emits_native_and_posix_patterns(tmp_path):
    rules = replay.external_dirs(tmp_path)
    assert list(rules)[0] == "*" and rules["*"] == "deny"          # catch-all first: last match wins
    assert rules[str(tmp_path) + replay.os.sep + "*"] == "allow"
    assert rules[replay.posix(tmp_path) + "/*"] == "allow"


def test_time_travel_rolls_seed_back(tmp_path):
    import sqlite3
    from datetime import datetime as dt
    pdir = tmp_path
    (pdir / "skills" / "old-skill").mkdir(parents=True)
    (pdir / "skills" / "new-skill").mkdir(parents=True)
    (pdir / "skills" / ".usage.json").write_text(json.dumps({
        "old-skill": {"created_at": "2026-09-01"}, "new-skill": {"created_at": "2026-10-05"}}), encoding="utf-8")
    (pdir / "memories.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"id": "a", "text": "before", "created_at": "2026-09-20", "reinforcements": ["2026-09-25", "2026-10-02"], "status": "active"},
        {"id": "b", "text": "same day", "created_at": "2026-10-01", "reinforcements": [], "status": "active"},
        {"id": "c", "text": "after", "created_at": "2026-10-07", "reinforcements": [], "status": "active"},
    ]) + "\n", encoding="utf-8")
    con = sqlite3.connect(str(pdir / "search.db"))
    con.executescript("""
        CREATE TABLE session_text_content(rowid INTEGER PRIMARY KEY, session_id TEXT, message_id TEXT, role TEXT,
            text TEXT, project TEXT DEFAULT '', timestamp INTEGER);
        CREATE VIRTUAL TABLE session_text USING fts5(session_id, message_id, role, text, project, timestamp,
            content=session_text_content, content_rowid=rowid);
        CREATE TABLE indexed_session(session_id TEXT PRIMARY KEY, title TEXT, project TEXT, message_count INTEGER,
            time_created INTEGER, time_indexed INTEGER);
    """)
    cutoff = int(dt(2026, 10, 1, 12, 0).timestamp() * 1000)
    for i, ts in enumerate([cutoff - 10_000, cutoff + 10_000], 1):
        con.execute("INSERT INTO session_text_content VALUES (?,?,?,?,?,?,?)", (i, f"s{i}", "m", "user", "gpg passphrase", "", ts))
    con.execute("INSERT INTO session_text(session_text) VALUES('rebuild')")
    con.commit()
    con.close()
    info = replay.time_travel(pdir, cutoff)
    kept = [json.loads(l) for l in (pdir / "memories.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in kept] == ["a"] and kept[0]["reinforcements"] == ["2026-09-25"]
    assert info["skills_dropped"] == ["new-skill"] and not (pdir / "skills" / "new-skill").exists()
    con = sqlite3.connect(str(pdir / "search.db"))
    hits = con.execute("SELECT c.session_id FROM session_text s JOIN session_text_content c ON c.rowid=s.rowid "
                       "WHERE session_text MATCH 'gpg'").fetchall()
    con.close()
    assert hits == [("s1",)] and info["search_rows_dropped"] == 1
