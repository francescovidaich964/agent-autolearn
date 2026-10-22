"""Regression tests for the telemetry fixes (RL-OPS-006)."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import outcomes  # noqa: E402
import usage_analysis  # noqa: E402
from test_outcomes import _make_opencode_db, _tool_part  # noqa: E402


def test_index_mark_survives_close_and_advances(tmp_path):
    ocdb = tmp_path / "opencode.db"
    _make_opencode_db(ocdb, [
        _tool_part("p1", "s1", 1000, "bash", input_={"command": "ls"}),
        _tool_part("p2", "s1", 2000, "skill", input_={"name": "demo"}),
    ])
    db = tmp_path / "outcomes.db"
    idx = outcomes.OutcomeIndex(db, ocdb)
    first = idx.index()
    idx.close()
    assert first["tool_parts"] == 2

    # Re-open: the mark must have been committed, so nothing is re-indexed.
    idx = outcomes.OutcomeIndex(db, ocdb)
    assert idx._last_mark() == (2000, "p2")
    again = idx.index()
    idx.close()
    assert again["tool_parts"] == 0, "frozen mark: the tail was re-indexed"


def _make_v1_db(path: Path) -> None:
    _make_opencode_db(path, [
        _tool_part("p1", "s1", 1_760_000_000_000, "skill", input_={"name": "demo-project"}),
        _tool_part("p2", "s1", 1_760_000_000_100, "read",
                   input_={"filePath": "/home/u/.agents/skills/offline-runner/SKILL.md"}),
        _tool_part("p3", "s2", 1_760_000_000_200, "bash", input_={"command": "ls"}),
    ])
    con = sqlite3.connect(str(path))
    con.executescript("CREATE TABLE session (id text PRIMARY KEY, directory text, time_created integer);")
    con.execute("INSERT INTO session VALUES ('s1', '/home/u/repos/moe', 1760000000000)")
    con.execute("INSERT INTO session VALUES ('s2', '/home/u/repos/other', 1760000000000)")
    con.commit()
    con.close()


def test_usage_scan_falls_back_to_v1_tables(tmp_path, monkeypatch):
    db = tmp_path / "opencode.db"
    _make_v1_db(db)
    monkeypatch.setattr(usage_analysis, "OC_DB", str(db))
    out = usage_analysis.scan_opencode_db()  # used to raise: no such table session_v2
    assert list(out) == [("ocdb", "s1")]
    assert out[("ocdb", "s1")]["skills"] == {"demo-project", "offline-runner"}
    assert out[("ocdb", "s1")]["proj"] == "moe"
