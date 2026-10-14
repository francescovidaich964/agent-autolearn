#!/usr/bin/env python3
"""Store health report: deterministic metrics for an autolearn store.

Works on stores written by the old and the new design, so the same report
measures a production store before deployment (baseline) and after.

    uv run --with python-slugify eval/store_report.py snapshot --home ~/.autolearn --out baseline.json
    uv run --with python-slugify eval/store_report.py snapshot --home ~/.autolearn --since 2026-10-14 --out week1.json
    uv run --with python-slugify eval/store_report.py compare baseline.json week1.json

Never writes to the store. Reads observations.jsonl, review-runs.jsonl,
reviews/*.run.jsonl, memories.jsonl, memory.context.md, config.yaml and the
skills directory.

Metric groups (docs/designs/robust-learning-loop/LLD.md "Evaluation"):
  pipeline   review completion, losses, durations, steps, tokens
  memory     delivery (injected share), duplicates, core usage, code checks
  skills     size / accretion, patches per load, proposals, in-session edits
  integrity  orphan tmp files, corrupt lines, stale locks
  safety     injection/secret findings in content injected into sessions
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import os  # noqa: E402

# AUTOLEARN_SCRIPTS lets a copy of this file outside the repo (e.g. a
# scheduled weekly report) use the installed skill's modules.
SCRIPTS = Path(os.environ.get("AUTOLEARN_SCRIPTS") or
               Path(__file__).resolve().parents[1] / "skills" / "autolearn" / "scripts")
sys.path.insert(0, str(SCRIPTS))
import registry  # noqa: E402  (tokenization shared by both designs)
import safety  # noqa: E402
import skillops  # noqa: E402

DUP_CFG = {"dedupe_jaccard": 0.4, "dedupe_containment": 0.8, "dedupe_min_tokens": 5}


# ---------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------

def read_jsonl(path: Path) -> tuple[list[dict], int]:
    rows, bad = [], 0
    if not path.exists():
        return rows, bad
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    return rows, bad


def ts_of(row: dict) -> float | None:
    t = row.get("timestamp") or row.get("ts")
    if t is None:
        return None
    if isinstance(t, (int, float)):
        return float(t) / (1000.0 if t > 1e11 else 1.0)
    try:
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(q * len(v)))], 1)


def ratio(a: float, b: float) -> float | None:
    return round(a / b, 3) if b else None


# ---------------------------------------------------------------------------
# metric groups
# ---------------------------------------------------------------------------

def pipeline_metrics(pdir: Path, since: float | None) -> dict:
    obs, bad_obs = read_jsonl(pdir / "observations.jsonl")
    obs = [o for o in obs if since is None or (ts_of(o) or 0) >= since]
    types = Counter(o.get("type") for o in obs)
    spawned = [o for o in obs if o.get("type") == "review_spawned"]
    completes = [o for o in obs if o.get("type") == "review_complete"]
    out: dict = {
        "reviews_spawned": len(spawned),
        "reviews_by_trigger": dict(Counter(o.get("trigger", "?") for o in spawned)),
        "reviews_completed": len(completes),
        "completion_rate": ratio(len(completes), len(spawned)),
        "nothing_recorded": sum(1 for c in completes if c.get("nothing_recorded")),
        "throttled": dict(Counter(o.get("reason", "?") for o in obs if o.get("type") == "review_throttled")),
        "deferred_by_plugin": types.get("review_deferred", 0),
        "design_tags": dict(Counter(o.get("design", "legacy") for o in obs)),
        "events": dict(types),
        "corrupt_observation_lines": bad_obs,
    }
    # Exact join (new design): spawned review ids vs completes carrying them.
    ids = {o.get("review_id") for o in spawned if o.get("review_id")}
    done = {c.get("review_id") for c in completes if c.get("review_id")}
    if ids:
        out["completion_rate_exact"] = ratio(len(ids & done), len(ids))
        out["unlogged_reviews"] = len(ids - done)
    # Legacy proxy: first completion within 20 min after each spawn.
    comp_ts = sorted(t for t in (ts_of(c) for c in completes) if t)
    used, durs = set(), []
    for s in sorted(t for t in (ts_of(o) for o in spawned) if t):
        for i, t in enumerate(comp_ts):
            if i not in used and s < t < s + 1200:
                used.add(i)
                durs.append(t - s)
                break
    out["duration_s_proxy"] = {"p50": pct(durs, 0.5), "p90": pct(durs, 0.9)}
    gaps = []
    st = sorted(t for t in (ts_of(o) for o in spawned) if t)
    for a, b in zip(st, st[1:]):
        gaps.append(b - a)
    p50 = pct(durs, 0.5)
    out["spawns_into_running_review"] = sum(1 for g in gaps if p50 and g < p50)

    runs, bad_runs = read_jsonl(pdir / "review-runs.jsonl")
    runs = [r for r in runs if since is None or (ts_of(r) or 0) >= since]
    if runs:
        ev = Counter(r.get("event") for r in runs)
        fin = [r for r in runs if r.get("event") == "finished"]
        out["wrapper"] = {
            "events": dict(ev),
            "deferred_by_reason": dict(Counter(r.get("reason", "?") for r in runs if r.get("event") == "deferred")),
            "lost": ev.get("lost", 0) + ev.get("expired", 0),
            "finished_rc_nonzero": sum(1 for r in fin if r.get("rc") not in (0, None)),
            "duration_s": {"p50": pct([r.get("duration_s", 0) for r in fin], 0.5),
                           "p90": pct([r.get("duration_s", 0) for r in fin], 0.9)},
            "corrupt_lines": bad_runs,
        }
    # Per-run accounting sidecars (new design).
    steps, tok_in, tok_out, cost = [], [], [], []
    for side in (pdir / "reviews").glob("*.run.jsonl"):
        if since and side.stat().st_mtime < since:
            continue
        rows, _ = read_jsonl(side)
        fins = [r for r in rows if "step" in str(r.get("type", "")) and "finish" in str(r.get("type", ""))]
        if not fins:
            continue
        steps.append(len(fins))
        ti = to = c = 0.0
        for r in fins:
            part = r.get("part") or r
            tk = part.get("tokens") or {}
            ti += float(tk.get("input") or 0)
            to += float(tk.get("output") or 0)
            c += float(part.get("cost") or 0)
        tok_in.append(ti)
        tok_out.append(to)
        cost.append(c)
    if steps:
        out["runs"] = {"n": len(steps), "steps_p50": pct(steps, 0.5), "steps_p90": pct(steps, 0.9),
                       "tokens_in_p50": pct(tok_in, 0.5), "tokens_out_p50": pct(tok_out, 0.5),
                       "cost_total": round(sum(cost), 4)}
    return out


def _tokens(text: str) -> set[str]:
    return set(registry.extract_topics(text))


def duplicate_pairs(records: list[dict]) -> int:
    toks = [_tokens(r.get("text", "")) for r in records]
    n = 0
    for i, a in enumerate(toks):
        if not a:
            continue
        for b in toks[i + 1:]:
            if not b:
                continue
            inter = len(a & b)
            j = inter / len(a | b)
            c = inter / min(len(a), len(b))
            if j >= DUP_CFG["dedupe_jaccard"] or (c >= DUP_CFG["dedupe_containment"] and min(len(a), len(b)) >= DUP_CFG["dedupe_min_tokens"]):
                n += 1
    return n


def memory_metrics(pdir: Path) -> dict:
    recs, bad = read_jsonl(pdir / "memories.jsonl")
    active = [r for r in recs if r.get("status") == "active"]
    ctx = (pdir / "memory.context.md")
    ctx_text = ctx.read_text(encoding="utf-8", errors="replace") if ctx.exists() else ""
    injected = [l for l in ctx_text.splitlines() if l.startswith("- ")]
    lens = [len(r.get("text", "")) for r in active]
    layers = Counter(r.get("layer", "none") for r in active)
    core = [r for r in active if r.get("layer") == "core"]
    out = {
        "active": len(active),
        "by_type": dict(Counter(r.get("type") for r in active)),
        "by_layer": dict(layers),
        "retired": sum(1 for r in recs if r.get("status") == "retired"),
        "evicted": sum(1 for r in recs if r.get("status") == "evicted"),
        "injected_entries": len(injected),
        "injected_chars": len(ctx_text),
        "injected_share": ratio(len(injected), len(active)),
        "core_chars": {t: sum(len(f"- {r.get('text', '')}\n") for r in core if r.get("type") == t) for t in ("memory", "user")},
        "entry_chars_p50": pct(lens, 0.5),
        "entry_chars_p90": pct(lens, 0.9),
        "entries_over_400": sum(1 for l in lens if l > 400),
        "reinforced_share": ratio(sum(1 for r in active if r.get("reinforcements")), len(active)),
        "near_duplicate_pairs": duplicate_pairs(active),
        "with_code_check": sum(1 for r in active if r.get("check")),
        "corrupt_lines": bad,
    }
    arch = pdir / "memory.archive.json"
    if arch.exists():
        try:
            out["archive_index_records"] = len(json.loads(arch.read_text(encoding="utf-8")).get("records", []))
        except (OSError, json.JSONDecodeError):
            out["archive_index_records"] = None
    return out


def skill_metrics(pdir: Path) -> dict:
    sdir = pdir / "skills"
    usage = {}
    try:
        usage = json.loads((sdir / ".usage.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    files = sorted(sdir.glob("*/SKILL.md"))
    sizes = [f.stat().st_size for f in files]
    lint = Counter()
    dated = 0
    for f in files:
        for finding in skillops.lint_skill(f):
            lint[finding["rule"]] += 1
        dated += len(skillops.DATED_BULLET_RE.findall(f.read_text(encoding="utf-8", errors="replace")))
    tracked = {n: m for n, m in usage.items() if m.get("state") != "archived"}
    uses = sum(int(m.get("use_count") or 0) for m in tracked.values())
    patches = sum(int(m.get("patch_count") or 0) for m in tracked.values())
    props = skillops.load_proposals(sdir)
    return {
        "skills": len(files),
        "archived": sum(1 for m in usage.values() if m.get("state") == "archived"),
        "size_p50": pct(sizes, 0.5), "size_p90": pct(sizes, 0.9), "size_max": max(sizes) if sizes else None,
        "size_total": sum(sizes),
        "lint": dict(lint),
        "dated_bullets_total": dated,
        "loaded_share": ratio(sum(1 for m in tracked.values() if int(m.get("use_count") or 0) > 0), len(tracked)),
        "patches_total": patches,
        "patches_per_load": ratio(patches, uses) if uses else None,
        "never_loaded_patched": sum(1 for m in tracked.values()
                                    if int(m.get("use_count") or 0) == 0 and int(m.get("patch_count") or 0) > 0),
        "pinned": sum(1 for m in tracked.values() if m.get("pinned")),
        "proposals": dict(Counter(p.get("status") for p in props)),
    }


def integrity_metrics(pdir: Path) -> dict:
    now = time.time()
    tmps = list(pdir.glob("*.tmp")) + list((pdir / "skills").glob("*.tmp"))
    lock = pdir / ".store.lock"
    return {
        "orphan_tmp_files": sum(1 for f in tmps if now - f.stat().st_mtime > 3600),
        "stale_store_lock": bool(lock.exists() and now - lock.stat().st_mtime > 120),
        "pending_reviews": len(list((pdir / "reviews" / "pending").glob("*.md"))) if (pdir / "reviews" / "pending").exists() else 0,
    }


def safety_metrics(pdir: Path) -> dict:
    findings = Counter()
    ctx = pdir / "memory.context.md"
    sources = [ctx] if ctx.exists() else []
    sources += sorted((pdir / "skills").glob("*/SKILL.md"))
    for src in sources:
        for f in safety.scan(src.read_text(encoding="utf-8", errors="replace")):
            findings[f"{f['rule']}:{f['verdict']}"] += 1
    return {"findings": dict(findings), "blocked": sum(v for k, v in findings.items() if k.endswith(":block"))}


def snapshot(home: Path, since: str | None) -> dict:
    pdir = Path(home).expanduser() / "personas" / "default"
    since_ts = datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp() if since else None
    return {
        "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "home": str(home),
        "since": since,
        "pipeline": pipeline_metrics(pdir, since_ts),
        "memory": memory_metrics(pdir),
        "skills": skill_metrics(pdir),
        "integrity": integrity_metrics(pdir),
        "safety": safety_metrics(pdir),
    }


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------

# (path, label, better) — better: "up" | "down" | None (informational)
HEADLINE = [
    ("pipeline.completion_rate", "Review completion rate (logged / spawned)", "up"),
    ("pipeline.completion_rate_exact", "Review completion rate (exact id join)", "up"),
    ("pipeline.unlogged_reviews", "Reviews that never logged an outcome", "down"),
    ("pipeline.spawns_into_running_review", "Spawns while a review was running", None),
    ("pipeline.wrapper.lost", "Reviews lost (lost + expired)", "down"),
    ("pipeline.duration_s_proxy.p50", "Review duration p50 (s, proxy)", "down"),
    ("pipeline.wrapper.duration_s.p50", "Review duration p50 (s, wrapper)", "down"),
    ("pipeline.runs.steps_p50", "Reviewer steps p50", "down"),
    ("pipeline.nothing_recorded", "Reviews that recorded nothing", None),
    ("memory.active", "Active memories", None),
    ("memory.injected_entries", "Entries injected every session", None),
    ("memory.injected_share", "Share of active memories injected", "up"),
    ("memory.near_duplicate_pairs", "Near-duplicate memory pairs", "down"),
    ("memory.entries_over_400", "Memories over 400 chars", "down"),
    ("memory.with_code_check", "Code-state memories with a check", "up"),
    ("memory.retired", "Retired (stale code-state) memories", None),
    ("skills.size_p90", "Skill size p90 (bytes)", "down"),
    ("skills.size_max", "Largest skill (bytes)", "down"),
    ("skills.dated_bullets_total", "Dated bullets across skills", "down"),
    ("skills.lint.incident-log-shape", "Skills with incident-log shape", "down"),
    ("skills.lint.oversized", "Oversized skills", "down"),
    ("skills.loaded_share", "Skills ever loaded (share)", "up"),
    ("skills.never_loaded_patched", "Never-loaded skills that got patched", "down"),
    ("pipeline.events.archive_slice", "Sessions given a relevant archive slice", None),
    ("pipeline.events.skill_edited", "Skill edits via `skill edit` (CLI)", None),
    ("pipeline.events.skill_edited_in_session", "Skill-file edits seen in sessions", None),
    ("pipeline.events.skill_proposed", "Skill proposals staged", None),
    ("pipeline.events.skill_proposals_surfaced", "Proposals surfaced at skill load", None),
    ("pipeline.events.skill_proposal_applied", "Proposals applied", None),
    ("pipeline.events.write_refused", "Writes refused (duplicate / core full)", None),
    ("pipeline.events.write_blocked", "Writes blocked by the safety scan", None),
    ("pipeline.events.memories_retired", "Code-state retirement runs", None),
    ("integrity.orphan_tmp_files", "Orphaned tmp files", "down"),
    ("safety.blocked", "Blocking safety findings in injected content", "down"),
]


def get(d: dict, path: str):
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def compare(a: dict, b: dict) -> str:
    lines = [f"| Metric | Before ({a.get('taken_at', '?')[:10]}) | After ({b.get('taken_at', '?')[:10]}) | Verdict |",
             "|---|---|---|---|"]
    for path, label, better in HEADLINE:
        va, vb = get(a, path), get(b, path)
        if va is None and vb is None:
            continue
        verdict = ""
        if better and isinstance(va, (int, float)) and isinstance(vb, (int, float)) and va != vb:
            verdict = "better" if (vb > va) == (better == "up") else "worse"
        lines.append(f"| {label} | {va if va is not None else '–'} | {vb if vb is not None else '–'} | {verdict} |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--home", default=str(Path.home() / ".autolearn"))
    s.add_argument("--since", default=None, help="ISO date: only pipeline events from this date")
    s.add_argument("--out", default=None)
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    t = sub.add_parser("track", help="Snapshot since a deploy date and write a dated comparison vs a baseline")
    t.add_argument("--home", default=str(Path.home() / ".autolearn"))
    t.add_argument("--baseline", required=True, help="Pre-deploy snapshot JSON")
    t.add_argument("--since", required=True, help="Deploy date (ISO); only pipeline events from then on")
    t.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    if args.cmd == "track":
        out_dir = Path(args.out_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
        snap = snapshot(Path(args.home), args.since)
        stamp = datetime.now().strftime("%Y-%m-%d")
        (out_dir / f"snapshot-{stamp}.json").write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        base = json.loads(Path(args.baseline).expanduser().read_text(encoding="utf-8"))
        md = (f"# Autolearn production metrics — {stamp}\n\n"
              f"Baseline: `{args.baseline}` (pre-deploy) · after: events since {args.since}\n\n"
              + compare(base, snap) + "\n")
        (out_dir / f"report-{stamp}.md").write_text(md, encoding="utf-8")
        print(md)
        return
    if args.cmd == "snapshot":
        snap = snapshot(Path(args.home), args.since)
        text = json.dumps(snap, indent=2, ensure_ascii=False)
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
            print(f"wrote {args.out}")
        else:
            print(text)
    else:
        a = json.loads(Path(args.before).read_text(encoding="utf-8"))
        b = json.loads(Path(args.after).read_text(encoding="utf-8"))
        print(compare(a, b))


if __name__ == "__main__":
    main()
