"""Skill operations: exact edits, staged proposals, rewrites, lint, audit.

`skill patch` could only APPEND a bullet to a section, so every correction
layered on top of the text it corrected ("supersedes the bullet above...").
Combined with a reviewer that patched skills it never saw loaded, the
production library grew to 15-32 KB skills carrying 10-23 dated bullets
each, with patch counts of 15-24 and zero recorded loads.

This module provides the replacements:

- ``exact_replace``: replace exactly one occurrence of an exact string
  (refuses 0 or >1 matches), with a backup in ``skills/.history/``.
- proposals (``skills/.proposals.json``): edits and rewrites staged by the
  reviewer / curator, applied by the agent that has the evidence, a human,
  or the curator when ``auto_apply_rewrites`` allows it.
- ``preservation_report``: a rewrite may only drop text that is not a
  command, path, URL or inline-code token unless explicitly accepted.
- ``lint``: oversized / incident-log-shape / references-sprawl.
- frontmatter helpers for ``set-description`` and ``set-verify``.
- ``audit_never_loaded``: did the trigger situation of a never-loaded skill
  recur in past sessions (description failed to fire) or not (dormant)?

Spec: docs/designs/robust-learning-loop/LLD.md (RL-SKL-001..007).
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import storelock

PROPOSALS_FILE = ".proposals.json"
HISTORY_DIR = ".history"
LINT_DEFAULTS = {
    "skill_max_chars": 12000,
    "skill_dated_bullets_max": 8,
    "skill_supersede_markers_max": 3,
    "skill_references_max": 20,
}


class SkillOpError(Exception):
    def __init__(self, message: str, *, details: list[str] | None = None):
        super().__init__(message)
        self.details = details or []


# ---------------------------------------------------------------------------
# Paths, backups
# ---------------------------------------------------------------------------

def skill_dir(skills_root: Path, name: str) -> Path:
    return Path(skills_root) / name


def resolve_file(skills_root: Path, name: str, rel: str | None = None) -> Path:
    base = skill_dir(skills_root, name)
    if not (base / "SKILL.md").is_file():
        raise SkillOpError(f"Skill not found: {name}")
    target = (base / (rel or "SKILL.md")).resolve()
    if base.resolve() not in target.parents and target != (base / "SKILL.md").resolve():
        raise SkillOpError(f"--file must stay inside the skill directory: {rel}")
    return target


def backup(skills_root: Path, name: str, path: Path) -> Path | None:
    if not path.exists():
        return None
    hist = Path(skills_root) / HISTORY_DIR / name
    hist.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    dest = hist / f"{stamp}-{path.name}"
    dest.write_bytes(path.read_bytes())
    return dest


# ---------------------------------------------------------------------------
# Exact edits (RL-SKL-001)
# ---------------------------------------------------------------------------

def exact_replace(text: str, old: str, new: str) -> str:
    if not old:
        raise SkillOpError("--old must not be empty")
    n = text.count(old)
    if n == 0:
        raise SkillOpError("--old text not found in the file (it must match exactly, including whitespace)",
                           details=["Read the file and copy the exact passage to replace."])
    if n > 1:
        raise SkillOpError(f"--old text occurs {n} times; include more surrounding context so it is unique")
    return text.replace(old, new, 1)


def apply_edit(skills_root: Path, name: str, old: str, new: str, rel: str | None = None) -> dict:
    path = resolve_file(skills_root, name, rel)
    raw = path.read_bytes().decode("utf-8")
    crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")
    updated = exact_replace(text, old.replace("\r\n", "\n"), new.replace("\r\n", "\n"))
    bak = backup(skills_root, name, path)
    storelock.atomic_write_text(path, updated.replace("\n", "\r\n") if crlf else updated)
    return {"file": str(path), "backup": str(bak) if bak else None,
            "delta_chars": len(updated) - len(text)}


# ---------------------------------------------------------------------------
# Frontmatter (set-description / set-verify)
# ---------------------------------------------------------------------------

def split_frontmatter(text: str) -> tuple[list[str], str]:
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        raise SkillOpError("SKILL.md has no YAML frontmatter")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i], "\n".join(lines[i + 1:])
    raise SkillOpError("SKILL.md frontmatter is not terminated")


def _key_span(fm: list[str], key: str) -> tuple[int, int] | None:
    start = next((i for i, l in enumerate(fm) if re.match(rf"^{re.escape(key)}\s*:", l)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(fm) and (fm[end].startswith((" ", "\t")) or fm[end].strip() == ""):
        end += 1
    return start, end


def set_frontmatter_block(text: str, key: str, block: list[str]) -> str:
    """Replace (or append) the top-level ``key`` in the frontmatter with ``block`` lines."""
    fm, body = split_frontmatter(text)
    span = _key_span(fm, key)
    if span:
        fm = fm[:span[0]] + block + fm[span[1]:]
    else:
        fm = fm + block
    return "---\n" + "\n".join(fm) + "\n---\n" + body


def frontmatter_value(text: str, key: str) -> str:
    fm, _ = split_frontmatter(text)
    span = _key_span(fm, key)
    if not span:
        return ""
    first = fm[span[0]].split(":", 1)[1].strip()
    rest = [l.strip() for l in fm[span[0] + 1:span[1]] if l.strip()]
    if first in ("|", ">", "|-", ">-"):
        return " ".join(rest)
    return " ".join([first.strip("\"'")] + rest).strip()


def description_block(desc: str) -> list[str]:
    lines = [l.rstrip() for l in desc.strip().splitlines()] or [""]
    return ["description: |"] + [f"  {l}" for l in lines]


def verify_block(command: str, expect_exit: int) -> list[str]:
    return ["verify:", f"  command: {json.dumps(command)}", f"  expect_exit: {int(expect_exit)}"]


def validate_frontmatter(text: str) -> None:
    try:
        import yaml
    except ImportError:
        return
    fm, _ = split_frontmatter(text)
    try:
        data = yaml.safe_load("\n".join(fm))
    except Exception as exc:
        raise SkillOpError(f"frontmatter would not parse after the edit: {exc}")
    if not isinstance(data, dict) or not data.get("name"):
        raise SkillOpError("frontmatter lost its name after the edit")


# ---------------------------------------------------------------------------
# Lint (RL-SKL-003)
# ---------------------------------------------------------------------------

DATED_BULLET_RE = re.compile(r"(?m)^\s*[-*].*\b20\d\d-\d\d-\d\d\b")
SUPERSEDE_RE = re.compile(r"(?i)supersed|\bupdated?\b\s*\(?20\d\d|\bUPDATE:|no longer (?:true|valid|applies)|was wrong|correction:")


def lint_skill(path: Path, cfg: dict | None = None) -> list[dict]:
    cfg = {**LINT_DEFAULTS, **(cfg or {})}
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    findings = []
    if len(text) > cfg["skill_max_chars"]:
        findings.append({"rule": "oversized", "value": len(text), "limit": cfg["skill_max_chars"],
                         "hint": "move deep detail into references/<topic>.md; keep SKILL.md to the current procedure"})
    dated = len(DATED_BULLET_RE.findall(text))
    sup = len(SUPERSEDE_RE.findall(text))
    if dated > cfg["skill_dated_bullets_max"] or sup >= cfg["skill_supersede_markers_max"]:
        findings.append({"rule": "incident-log-shape", "value": {"dated_bullets": dated, "supersede_markers": sup},
                         "hint": "fold superseded bullets into the current truth; skills hold lessons, not logs"})
    refs = Path(path).parent / "references"
    if refs.is_dir():
        n = sum(1 for _ in refs.iterdir())
        if n > cfg["skill_references_max"]:
            findings.append({"rule": "references-sprawl", "value": n, "limit": cfg["skill_references_max"],
                             "hint": "merge reference files by topic"})
    return findings


# ---------------------------------------------------------------------------
# Preservation check for rewrites (RL-SKL-004)
# ---------------------------------------------------------------------------

INLINE_CODE_RE = re.compile(r"`([^`\n]{2,200})`")
URL_RE = re.compile(r"https?://[^\s)>\]`'\"]+")
PATH_RE = re.compile(r"(?<![\w/])(?:~|\.{1,2})?[/\\][\w.\-~/\\]+\.[A-Za-z0-9]{1,6}\b|[\w.-]+/[\w./-]+\.[A-Za-z0-9]{1,6}\b")


def critical_tokens(text: str) -> set[str]:
    """Strings a rewrite must not lose: inline code, code-block lines, URLs, paths."""
    toks: set[str] = set()
    in_block = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.strip().startswith("```"):
            in_block = not in_block
            continue
        if in_block:
            s = line.strip()
            if len(s) >= 3:
                toks.add(s)
            continue
        toks.update(m.strip() for m in INLINE_CODE_RE.findall(line) if m.strip())
        toks.update(URL_RE.findall(line))
        toks.update(PATH_RE.findall(line))
    return toks


def _norm(s: str) -> str:
    return " ".join(s.split())


def preservation_report(old_text: str, new_texts: list[str]) -> dict:
    corpus = _norm("\n".join(new_texts))
    crit = critical_tokens(old_text)
    missing = sorted(t for t in crit if _norm(t) not in corpus)
    return {"critical": len(crit), "missing": missing}


# ---------------------------------------------------------------------------
# Proposals (RL-SKL-002/004)
# ---------------------------------------------------------------------------

def proposals_path(skills_root: Path) -> Path:
    return Path(skills_root) / PROPOSALS_FILE


def load_proposals(skills_root: Path) -> list[dict]:
    p = proposals_path(skills_root)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def save_proposals(skills_root: Path, items: list[dict]) -> None:
    storelock.atomic_write_text(proposals_path(skills_root), json.dumps(items, indent=2, ensure_ascii=False) + "\n")


def add_proposal(skills_root: Path, **fields) -> dict:
    items = load_proposals(skills_root)
    prop = {"id": uuid.uuid4().hex[:8], "status": "pending",
            "created_at": datetime.now().isoformat(timespec="seconds"), **fields}
    items.append(prop)
    save_proposals(skills_root, items)
    return prop


def get_proposal(skills_root: Path, pid: str) -> dict:
    prop = next((p for p in load_proposals(skills_root) if p.get("id") == pid), None)
    if prop is None:
        raise SkillOpError(f"No proposal with id {pid}")
    return prop


def set_proposal_status(skills_root: Path, pid: str, status: str, **extra) -> dict:
    items = load_proposals(skills_root)
    for p in items:
        if p.get("id") == pid:
            p["status"] = status
            p["resolved_at"] = datetime.now().isoformat(timespec="seconds")
            p.update(extra)
            save_proposals(skills_root, items)
            return p
    raise SkillOpError(f"No proposal with id {pid}")


def supersede_pending_rewrites(skills_root: Path, skill: str) -> list[str]:
    """Mark pending rewrites of ``skill`` superseded; drop their staged files.

    A rewrite is built from the current file, so the latest one supersedes any
    pending one for the same skill (merging two full-file rewrites is not
    meaningful). Called before staging a new rewrite.
    """
    items = load_proposals(skills_root)
    superseded = []
    for p in items:
        if p.get("kind") == "rewrite" and p.get("skill") == skill and p.get("status") == "pending":
            p["status"] = "superseded"
            p["resolved_at"] = datetime.now().isoformat(timespec="seconds")
            superseded.append(p["id"])
            try:
                (Path(skills_root) / ".proposals" / f"{p['id']}.md").unlink()
            except OSError:
                pass
    if superseded:
        save_proposals(skills_root, items)
    return superseded


def reject_conflicting_edits(skills_root: Path, skill: str, rewrite_id: str) -> list[str]:
    """Reject pending edits of ``skill`` whose anchor text no longer exists.

    Called after a rewrite is applied: an edit whose ``old`` text is gone from
    the (new) file can never apply - its content is folded into the rewrite or
    obsolete - so it is rejected with the rewrite as the reason.
    """
    items = load_proposals(skills_root)
    rejected = []
    for p in items:
        if p.get("kind") != "edit" or p.get("skill") != skill or p.get("status") != "pending":
            continue
        try:
            path = resolve_file(skills_root, skill, p.get("file"))
            text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        except (SkillOpError, OSError):
            text = ""
        if str(p.get("old") or "").replace("\r\n", "\n") not in text:
            p["status"] = "rejected"
            p["resolved_at"] = datetime.now().isoformat(timespec="seconds")
            p["reject_reason"] = f"superseded by rewrite {rewrite_id}"
            rejected.append(p["id"])
    if rejected:
        save_proposals(skills_root, items)
    return rejected


def expire_stale_proposals(skills_root: Path, days: int) -> list[str]:
    """Expire pending proposals older than ``days``; drop their staged files.

    Dead proposals (a skill that never loads, an agent that never applies)
    must not accumulate indefinitely.
    """
    items = load_proposals(skills_root)
    cutoff = datetime.now() - timedelta(days=days)
    expired = []
    for p in items:
        if p.get("status") != "pending":
            continue
        try:
            created = datetime.fromisoformat(str(p.get("created_at")))
        except ValueError:
            continue
        if created < cutoff:
            p["status"] = "expired"
            p["resolved_at"] = datetime.now().isoformat(timespec="seconds")
            expired.append(p["id"])
            try:
                (Path(skills_root) / ".proposals" / f"{p['id']}.md").unlink()
            except OSError:
                pass
    if expired:
        save_proposals(skills_root, items)
    return expired


# ---------------------------------------------------------------------------
# Audit of never-loaded skills (RL-SKL-006)
# ---------------------------------------------------------------------------

GENERIC = {
    "use", "using", "used", "when", "skill", "skills", "user", "users", "wants", "want", "work", "works",
    "file", "files", "run", "runs", "make", "new", "one", "also", "via", "any", "all", "each", "only",
    "after", "before", "this", "that", "with", "from", "into", "how", "what", "why", "which", "who",
    "guide", "procedure", "steps", "task", "tasks", "session", "sessions", "agent", "load", "loads",
    "ask", "asks", "asked", "need", "needs", "needed", "help", "please", "thing", "things",
}


def trigger_terms(description: str, limit: int = 8) -> list[str]:
    import registry
    toks = [t for t in registry.extract_topics(description) if t not in GENERIC and len(t) > 2 and not t.isdigit()]
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:limit]


def recurring_sessions(search_db: Path, terms: list[str], since_ms: int = 0, min_terms: int = 2,
                       exclude_sessions: set[str] | None = None) -> list[str]:
    """User messages after ``since_ms`` where >= ``min_terms`` trigger terms co-occur."""
    if not terms or not Path(search_db).exists():
        return []
    q = " OR ".join(f'"{t}"' for t in terms)
    con = sqlite3.connect(f"file:{Path(search_db).as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT c.session_id, c.text FROM session_text s JOIN session_text_content c ON c.rowid = s.rowid "
            "WHERE session_text MATCH ? AND c.role = 'user' AND c.timestamp > ? LIMIT 5000",
            [f"text : ({q})", since_ms],
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    hits: set[str] = set()
    for sid, text in rows:
        if exclude_sessions and sid in exclude_sessions:
            continue
        low = (text or "").lower()
        if sum(1 for t in terms if re.search(rf"\b{re.escape(t)}\b", low)) >= min_terms:
            hits.add(sid)
    return sorted(hits)


def audit_never_loaded(skills_root: Path, usage: dict, search_db: Path, *, min_age_days: int = 21,
                       today: date | None = None) -> list[dict]:
    today = today or date.today()
    out = []
    for name, meta in sorted(usage.items()):
        if meta.get("state") == "archived" or meta.get("pinned") or int(meta.get("use_count") or 0) > 0:
            continue
        try:
            created = date.fromisoformat(meta.get("created_at") or "")
        except ValueError:
            continue
        age = (today - created).days
        if age < min_age_days:
            continue
        sk = Path(skills_root) / name / "SKILL.md"
        if not sk.is_file():
            continue
        desc = frontmatter_value(sk.read_text(encoding="utf-8", errors="replace"), "description")
        terms = trigger_terms(desc)
        since_ms = int(datetime(created.year, created.month, created.day).timestamp() * 1000) + 86400000
        sessions = recurring_sessions(search_db, terms, since_ms=since_ms)
        out.append({
            "skill": name, "age_days": age, "terms": terms, "recurred_sessions": len(sessions),
            "sample_sessions": sessions[:5],
            "verdict": "fix-description" if sessions else "dormant",
        })
    return out
