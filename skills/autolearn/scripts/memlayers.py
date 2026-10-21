"""Two-tier memory: a hard-capped, always-injected core + a searchable archive.

The registry used to be unbounded while the composer silently kept only what
fit a 3 000-char budget, ranked by retention alone. In production that meant
445 active memories with 7 ever reaching the agent: the writer never met the
budget, so it never curated. Here the writer meets it:

- ``core`` entries are always injected (memory.context.md). Each type has a
  hard character cap (``core_memory_chars`` / ``core_user_chars``) and a
  per-entry limit (``core_entry_max_chars``). A write that would overflow is
  REFUSED with the current core listed; the writer must demote, shorten or
  remove first (or write to the archive). Nothing is silently dropped.
- ``archive`` entries are not injected wholesale; they are retrieved by
  relevance (per-session slice in the plugin, ``memory search`` on demand).
- Near-duplicates (token Jaccard / containment across both layers and both
  types) are refused in favour of strengthening or editing the existing entry.
- An entry may carry a declarative ``check`` (path + regex + expect) so facts
  about code state retire themselves when the code changes. No command is
  ever executed.

Spec: docs/designs/robust-learning-loop/LLD.md (RL-MEM-001..008).
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

import registry
import storelock

DEFAULTS = {
    "core_memory_chars": 2500,
    "core_user_chars": 1500,
    "core_entry_max_chars": 400,
    "dedupe_jaccard": 0.4,
    "dedupe_containment": 0.8,
    "dedupe_min_tokens": 5,
    "archive_slice_chars": 1500,
    "archive_slice_max": 6,
    "archive_min_score": 8.0,
    "archive_min_overlap": 2,
}

ARCHIVE_INDEX = "memory.archive.json"


def load_config(persona_dir) -> dict:
    """config.yaml overrides on top of DEFAULTS (flat ``key: value`` lines)."""
    cfg = dict(DEFAULTS)
    path = Path(persona_dir) / "config.yaml"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^(\w+):\s*(.+?)\s*$", line)
            if not m or m.group(1) not in DEFAULTS:
                continue
            raw = m.group(2)
            try:
                cfg[m.group(1)] = float(raw) if "." in raw else int(raw)
            except ValueError:
                pass
    except OSError:
        pass
    return cfg


def entry_cost(text: str) -> int:
    return len(f"- {text}\n")


def cap_for(type_: str, cfg: dict) -> int:
    return int(cfg["core_user_chars"] if type_ == "user" else cfg["core_memory_chars"])


def layer_of(rec: dict) -> str:
    return rec.get("layer") or "archive"


def core_records(records: list[dict], type_: str | None = None) -> list[dict]:
    return [r for r in records if r.get("status") == "active" and layer_of(r) == "core"
            and (type_ is None or r.get("type") == type_)]


def core_used(records: list[dict], type_: str) -> int:
    return sum(entry_cost(r.get("text", "")) for r in core_records(records, type_))


# ---------------------------------------------------------------------------
# Migration (RL-MEM-008)
# ---------------------------------------------------------------------------

def _rank_key(rec: dict) -> tuple:
    score = rec.get("retention_score")
    return (bool(rec.get("pinned")), 0.5 if score is None else float(score))


def assign_layers(records: list[dict], cfg: dict) -> int:
    """Give every active record without a layer one. Returns how many changed.

    Mirrors what the old composer injected: pinned first, then by retention,
    filling each type's core cap; everything else goes to the archive.
    """
    pending = [r for r in records if r.get("status") == "active" and not r.get("layer")]
    if not pending:
        return 0
    used = {t: core_used(records, t) for t in ("memory", "user")}
    for rec in sorted(pending, key=_rank_key, reverse=True):
        t = "user" if rec.get("type") == "user" else "memory"
        cost = entry_cost(rec.get("text", ""))
        fits = (used[t] + cost <= cap_for(t, cfg)
                and len(rec.get("text", "")) <= cfg["core_entry_max_chars"])
        if rec.get("pinned") or fits:
            rec["layer"] = "core"
            used[t] += cost
        else:
            rec["layer"] = "archive"
    return len(pending)


def migrate(reg: registry.MemoryRegistry, cfg: dict | None = None) -> int:
    cfg = cfg or load_config(reg.persona_dir)
    with reg.lock():
        records = reg.load()
        n = assign_layers(records, cfg)
        if n:
            reg.save(records)
    return n


# ---------------------------------------------------------------------------
# Near-duplicate detection (RL-MEM-003)
# ---------------------------------------------------------------------------

def _tokens(text: str) -> set[str]:
    return set(registry.extract_topics(text))


def _near(ta: set[str], tb: set[str], cfg: dict) -> bool:
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    j = inter / len(ta | tb)
    c = inter / min(len(ta), len(tb))
    return j >= cfg["dedupe_jaccard"] or (c >= cfg["dedupe_containment"] and min(len(ta), len(tb)) >= cfg["dedupe_min_tokens"])


def is_near_duplicate(a: str, b: str, cfg: dict) -> bool:
    return _near(_tokens(a), _tokens(b), cfg)


def find_near_duplicates(text: str, records: list[dict], cfg: dict, exclude_id: str | None = None) -> list[dict]:
    ta = _tokens(text)
    return [r for r in records
            if r.get("status") == "active" and r.get("id") != exclude_id
            and _near(ta, _tokens(r.get("text", "")), cfg)]


def duplicate_pairs(records: list[dict], cfg: dict) -> list[tuple[str, str]]:
    """All near-duplicate pairs among active records (metrics)."""
    act = [(r["id"], _tokens(r.get("text", ""))) for r in records if r.get("status") == "active"]
    out = []
    for i, (ia, ta) in enumerate(act):
        for ib, tb in act[i + 1:]:
            if _near(ta, tb, cfg):
                out.append((ia, ib))
    return out


# ---------------------------------------------------------------------------
# Admission (RL-MEM-002)
# ---------------------------------------------------------------------------

class Refused(Exception):
    """A write the writer must resolve (exit code 2 at the CLI)."""

    def __init__(self, message: str, *, reason: str, details: list[str] | None = None):
        super().__init__(message)
        self.reason = reason
        self.details = details or []


def admit(text: str, type_: str, layer: str, records: list[dict], cfg: dict,
          *, force_duplicate: bool = False, exclude_id: str | None = None) -> None:
    """Raise Refused unless ``text`` may be written to ``layer``."""
    if not force_duplicate:
        dups = find_near_duplicates(text, records, cfg, exclude_id=exclude_id)
        if dups:
            raise Refused(
                "near-duplicate of an existing entry: strengthen or edit it instead",
                reason="duplicate",
                details=[f"{d['id']} [{layer_of(d)}/{d.get('type')}]: {d.get('text', '')[:160]}" for d in dups[:3]]
                + ["-> memory strengthen --id <id>   (same lesson seen again)",
                   "-> memory edit <id> \"<merged text>\"   (lesson refined or superseded)",
                   "-> add --force only if it is genuinely a different lesson"],
            )
    if layer != "core":
        return
    if len(text) > cfg["core_entry_max_chars"]:
        raise Refused(
            f"core entries are limited to {cfg['core_entry_max_chars']} chars ({len(text)} given)",
            reason="entry_too_long",
            details=["-> shorten it to the actionable rule, or",
                     "-> add with --archive (retrieved by relevance) and keep detail in a wiki pattern page"],
        )
    t = "user" if type_ == "user" else "memory"
    others = [r for r in core_records(records, t) if r.get("id") != exclude_id]
    used = sum(entry_cost(r.get("text", "")) for r in others)
    cap = cap_for(t, cfg)
    cost = entry_cost(text)
    if used + cost > cap:
        listing = [f"{r['id']} ({entry_cost(r.get('text', ''))} chars{', pinned' if r.get('pinned') else ''}): {r.get('text', '')[:120]}"
                   for r in others]
        raise Refused(
            f"core {t} is full: {used}/{cap} chars used, this entry needs {cost}",
            reason="core_full",
            details=listing + [
                "-> free space first: memory demote <id> (moves it to the archive) | "
                "memory edit <id> \"<shorter>\" | memory remove <keyword>",
                "-> or add with --archive if it is not needed in every session",
            ],
        )


# ---------------------------------------------------------------------------
# Declarative checks for code-state memories (RL-MEM-007)
# ---------------------------------------------------------------------------

def make_check(path: str | None, pattern: str | None, expect: str | None) -> dict | None:
    if not path and not pattern:
        return None
    if not path or not pattern:
        raise Refused("--check-path and --check-pattern go together", reason="bad_check")
    expect = (expect or "present").lower()
    if expect not in ("present", "absent"):
        raise Refused("--check-expect must be present|absent", reason="bad_check")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise Refused(f"--check-pattern is not a valid regex: {exc}", reason="bad_check")
    return {"path": str(Path(os.path.expanduser(path))), "pattern": pattern, "expect": expect}


def evaluate_check(check: dict) -> tuple[bool | None, str]:
    """(holds, detail). ``None`` = cannot evaluate here (file missing on this machine)."""
    p = Path(os.path.expanduser(check.get("path", "")))
    if not p.is_file():
        return None, f"path not found: {p}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"unreadable: {exc}"
    found = re.search(check.get("pattern", ""), text) is not None
    holds = found if check.get("expect") == "present" else not found
    return holds, f"pattern {'found' if found else 'not found'} (expect {check.get('expect')})"


def verify_checks(reg: registry.MemoryRegistry, *, dry_run: bool = False) -> dict:
    """Retire memories whose declarative check no longer holds."""
    result = {"checked": 0, "retired": [], "unknown": []}
    with reg.lock():
        records = reg.load()
        changed = False
        for r in records:
            chk = r.get("check")
            if r.get("status") != "active" or not chk:
                continue
            result["checked"] += 1
            holds, detail = evaluate_check(chk)
            if holds is None:
                result["unknown"].append(r["id"])
                continue
            r["check_last"] = {"date": date.today().isoformat(), "holds": holds}
            changed = True
            if not holds:
                result["retired"].append({"id": r["id"], "detail": detail, "text": r.get("text", "")[:120]})
                if not dry_run:
                    r["status"] = "retired"
                    r["retired_at"] = date.today().isoformat()
                    r["retired_reason"] = f"check failed: {detail}"
        if changed and not dry_run:
            reg.save(records)
    return result


# ---------------------------------------------------------------------------
# Archive retrieval (RL-MEM-005/006)
# ---------------------------------------------------------------------------

def context_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) >= 2} - registry.STOPWORDS


BM25_K1 = 1.2
BM25_B = 0.75


def _record_tokens(rec: dict) -> set[str]:
    return set(rec.get("topics") or []) or _tokens(rec.get("text", ""))


def corpus_stats(records: list[dict]) -> dict:
    """Document frequencies over the records (binary term frequency)."""
    df: dict[str, int] = {}
    total = 0
    for r in records:
        toks = _record_tokens(r)
        total += len(toks)
        for t in toks:
            df[t] = df.get(t, 0) + 1
    n = len(records)
    return {"n": n, "avgdl": (total / n) if n else 0.0, "df": df}


def bm25(rec_tokens: set[str], ctx: set[str], stats: dict) -> tuple[float, int]:
    """(score, overlap). Rare shared terms dominate; long entries are length-normalized.

    Mirrored in the plugin (autolearn-core.mjs ``archiveSlice``) using the
    df/n/avgdl exported in memory.archive.json.
    """
    overlap = rec_tokens & ctx
    if not overlap or not stats["n"]:
        return 0.0, 0
    import math
    norm = 1 - BM25_B + BM25_B * (len(rec_tokens) / (stats["avgdl"] or 1))
    tf_part = (BM25_K1 + 1) / (1 + BM25_K1 * norm)
    score = 0.0
    for t in overlap:
        df = stats["df"].get(t, 0)
        score += math.log(1 + (stats["n"] - df + 0.5) / (df + 0.5)) * tf_part
    return score, len(overlap)


def _retention(rec: dict) -> float:
    ret = rec.get("retention_score", rec.get("retention"))
    return 0.5 if ret is None else float(ret)


def search(records: list[dict], query: str, limit: int = 10, layer: str | None = "archive",
           min_overlap: int = 1) -> list[tuple[float, dict]]:
    pool = [r for r in records if r.get("status") == "active" and (not layer or layer_of(r) == layer)]
    stats = corpus_stats(pool)
    ctx = context_tokens(query)
    scored = []
    for r in pool:
        s, ov = bm25(_record_tokens(r), ctx, stats)
        if s > 0 and ov >= min_overlap:
            scored.append((s * (0.75 + 0.25 * _retention(r)), r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:limit]


def archive_slice(records: list[dict], context: str, cfg: dict) -> list[dict]:
    """The per-session slice: top archive entries above the score floor, in budget."""
    chosen, used = [], 0
    for score, r in search(records, context, limit=10 ** 6, min_overlap=int(cfg["archive_min_overlap"])):
        if score < cfg["archive_min_score"]:
            break
        cost = entry_cost(r.get("text", ""))
        if used + cost > cfg["archive_slice_chars"]:
            continue
        chosen.append(r)
        used += cost
        if len(chosen) >= cfg["archive_slice_max"]:
            break
    return chosen


def write_archive_index(persona_dir, records: list[dict], cfg: dict) -> None:
    """Export archive records + ranking params for the plugin's per-session slice."""
    pool = [r for r in records if r.get("status") == "active" and layer_of(r) == "archive"]
    stats = corpus_stats(pool)
    items = [{
        "id": r["id"],
        "text": r.get("text", ""),
        "topics": sorted(_record_tokens(r)),
        "retention": r.get("retention_score"),
        "type": r.get("type"),
    } for r in pool]
    doc = {
        "version": 2,
        "params": {
            "slice_chars": cfg["archive_slice_chars"],
            "slice_max": cfg["archive_slice_max"],
            "min_score": cfg["archive_min_score"],
            "min_overlap": cfg["archive_min_overlap"],
            "k1": BM25_K1,
            "b": BM25_B,
        },
        "stats": {"n": stats["n"], "avgdl": stats["avgdl"], "df": stats["df"]},
        "stopwords": sorted(registry.STOPWORDS),
        "records": items,
    }
    storelock.atomic_write_text(Path(persona_dir) / ARCHIVE_INDEX, json.dumps(doc, ensure_ascii=False))
