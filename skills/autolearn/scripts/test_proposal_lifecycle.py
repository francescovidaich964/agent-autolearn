"""Tests for the proposal lifecycle (RL-SKL-002/004 extensions).

- supersede-on-restage: a new rewrite supersedes pending rewrites of the
  same skill (latest wins; staged file dropped).
- conflict auto-reject: after a rewrite is applied, pending edits whose
  anchor text no longer exists are rejected with the rewrite as the reason.
- stale-proposal expiry: pending proposals older than N days are expired
  and their staged files dropped.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skillops  # noqa: E402


def _make_skill(root: Path, name: str = "demo", text: str = "line A\nline B\n") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return d


def _stage_file(root: Path, pid: str, content: str = "staged") -> Path:
    d = root / ".proposals"
    d.mkdir(exist_ok=True)
    p = d / f"{pid}.md"
    p.write_text(content, encoding="utf-8")
    return p


def test_supersede_pending_rewrites(tmp_path):
    _make_skill(tmp_path)
    p1 = skillops.add_proposal(tmp_path, kind="rewrite", skill="demo", file="SKILL.md", reason="first")
    staged1 = _stage_file(tmp_path, p1["id"])
    superseded = skillops.supersede_pending_rewrites(tmp_path, "demo")
    assert superseded == [p1["id"]]
    assert not staged1.exists()
    assert skillops.load_proposals(tmp_path)[0]["status"] == "superseded"
    # only pending rewrites are superseded
    p2 = skillops.add_proposal(tmp_path, kind="rewrite", skill="demo", file="SKILL.md", reason="second")
    p3 = skillops.add_proposal(tmp_path, kind="edit", skill="demo", file="SKILL.md",
                               old="line A", new="line Z", reason="edit stays")
    assert skillops.supersede_pending_rewrites(tmp_path, "demo") == [p2["id"]]
    statuses = {p["id"]: p["status"] for p in skillops.load_proposals(tmp_path)}
    assert statuses[p1["id"]] == "superseded"
    assert statuses[p3["id"]] == "pending"


def test_reject_conflicting_edits(tmp_path):
    _make_skill(tmp_path, text="alpha\nbeta\n")
    ok = skillops.add_proposal(tmp_path, kind="edit", skill="demo", file="SKILL.md",
                               old="alpha", new="ALPHA", reason="anchor survives")
    gone = skillops.add_proposal(tmp_path, kind="edit", skill="demo", file="SKILL.md",
                                 old="gamma", new="GAMMA", reason="anchor gone")
    rejected = skillops.reject_conflicting_edits(tmp_path, "demo", "rw123")
    assert rejected == [gone["id"]]
    items = {p["id"]: p for p in skillops.load_proposals(tmp_path)}
    assert items[gone["id"]]["status"] == "rejected"
    assert "rw123" in items[gone["id"]]["reject_reason"]
    assert items[ok["id"]]["status"] == "pending"


def test_expire_stale_proposals(tmp_path):
    _make_skill(tmp_path)
    old = skillops.add_proposal(tmp_path, kind="edit", skill="demo", file="SKILL.md",
                                old="line A", new="old edit", reason="old")
    fresh = skillops.add_proposal(tmp_path, kind="edit", skill="demo", file="SKILL.md",
                                  old="line A", new="fresh edit", reason="fresh")
    staged = _stage_file(tmp_path, old["id"])
    items = skillops.load_proposals(tmp_path)
    for p in items:
        if p["id"] == old["id"]:
            p["created_at"] = (datetime.now() - timedelta(days=31)).isoformat(timespec="seconds")
    skillops.save_proposals(tmp_path, items)
    expired = skillops.expire_stale_proposals(tmp_path, days=30)
    assert expired == [old["id"]]
    assert not staged.exists()
    statuses = {p["id"]: p["status"] for p in skillops.load_proposals(tmp_path)}
    assert statuses[old["id"]] == "expired"
    assert statuses[fresh["id"]] == "pending"
