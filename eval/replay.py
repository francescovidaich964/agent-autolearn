#!/usr/bin/env python3
"""A/B replay: run two reviewer designs over the same real review files.

Each (case, variant) runs in a sealed sandbox seeded from ONE frozen copy of
the production store, so both designs start from identical state and see
identical input; the only difference is the design under test (its scripts,
its SKILL.md + reviewer protocol, its store migration). Afterwards the
sandbox store is diffed against the seed: exactly what each design wrote.

    # 1. freeze a corpus + seed store (read-only on the live store)
    uv run --with python-slugify eval/replay.py prepare --n 20 --out ~/.autolearn-eval/corpus-2026-10-13

    # 2. replay both designs (git refs of this repo)
    uv run --with python-slugify --with pyyaml eval/replay.py run \
        --corpus ~/.autolearn-eval/corpus-2026-10-13 \
        --variant old=main --variant new=feat/robust-learning-loop --jobs 2

    # 3. optional blind pairwise judgment, then the report
    uv run eval/judge.py --run <run dir>
    uv run --with python-slugify eval/replay.py report --run <run dir>

Isolation (nothing live is written):
  AUTOLEARN_HOME, AGENTS_SKILLS_DIR, AGENT_IMPROVEMENT_HOME, AGENT_*_AGENTS_MD
  point into the sandbox; XDG_CONFIG_HOME points at a sandbox OpenCode config
  (only the provider the model needs, plugins off, instructions = the
  sandbox's own memory.context.md, a replay agent with `skill`/`edit` denied
  and external directories limited to the sandbox + variant source); the run
  uses `--pure`. Paths to the live store / installed skill inside the inlined
  protocol and the AGENTS.md copy are rewritten to the sandbox and variant.
  Every run is tagged AUTOLEARN_REVIEW_ID=replay-<case>; a live observation
  carrying that tag is reported as a LEAK.

Caveats (equal for both variants, so the comparison stays fair): the session
search index is a snapshot that includes sessions after the review was
written; the protocol is inlined instead of loaded through the skill tool.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "skills" / "autolearn" / "scripts"))
import registry  # noqa: E402

LIVE_HOME = Path.home() / ".autolearn"
LIVE_PDIR = LIVE_HOME / "personas" / "default"
LIVE_IMPROVEMENT = Path(os.environ.get("AGENT_IMPROVEMENT_HOME", Path.home() / ".agent-improvement"))
LIVE_OC_CONFIG = Path.home() / ".config" / "opencode" / "opencode.json"
LIVE_AGENTS_MD = Path.home() / ".config" / "opencode" / "AGENTS.md"
EVAL_ROOT = Path.home() / ".autolearn-eval"

SEED_FILES = ["memories.jsonl", "config.yaml", "proposals.json", "observations.jsonl",
              "user-profile.md", "memory.context.md", "search.db", "verdicts.json"]
SEED_DIRS = ["skills", "wiki"]
DUP_CFG = {"dedupe_jaccard": 0.4, "dedupe_containment": 0.8, "dedupe_min_tokens": 5}
OPENCODE = os.environ.get("AUTOLEARN_REPLAY_OPENCODE", "opencode")

REPLAY_AGENT_PROMPT = (
    "You are the autolearn review agent running in a sandbox. Follow the reviewer protocol appended "
    "to the message; it replaces loading the autolearn skill (do not load or read any installed "
    "autolearn skill). Use only the CLI paths given in the protocol."
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def posix(p: Path) -> str:
    return str(p).replace("\\", "/")


def sha(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest() if path.exists() else ""


def read_jsonl(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def copy_tree(src: Path, dst: Path) -> None:
    if src.is_dir():
        # Skill dirs in the persona are real dirs; ~/.agents/skills holds the links.
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=False,
                        ignore=shutil.ignore_patterns("*.tmp", ".store.lock"))
    elif src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def review_trigger(text: str) -> str:
    m = re.search(r"^- Trigger:\s*(\S+)", text, re.M)
    return m.group(1) if m else "unknown"


def conversation_of(text: str) -> str:
    i = text.find("## Conversation")
    return text[i:] if i >= 0 else text


# ---------------------------------------------------------------------------
# prepare: corpus + seed
# ---------------------------------------------------------------------------

def cmd_prepare(args) -> None:
    out = Path(args.out).expanduser()
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} exists and is not empty")
    reviews = Path(args.reviews).expanduser()
    files = sorted(f for f in reviews.glob("review-*.md") if f.stat().st_size >= args.min_bytes)
    rng = random.Random(args.seed)
    # Stratify by size tercile x trigger so the corpus resembles production.
    sizes = sorted(f.stat().st_size for f in files)
    cut = [sizes[len(sizes) // 3], sizes[2 * len(sizes) // 3]] if sizes else [0, 0]
    strata: dict[tuple, list[Path]] = {}
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        if text.count("# Autolearn Review") > 1:   # a review of a review (depth guard)
            continue
        band = 0 if f.stat().st_size < cut[0] else 1 if f.stat().st_size < cut[1] else 2
        strata.setdefault((band, review_trigger(text)), []).append(f)
    for v in strata.values():
        rng.shuffle(v)
    picked: list[Path] = []
    keys = sorted(strata)
    while len(picked) < args.n and any(strata.values()):
        for k in keys:
            if strata[k] and len(picked) < args.n:
                picked.append(strata[k].pop())
    (out / "cases").mkdir(parents=True)
    manifest = []
    for f in sorted(picked):
        case = f.stem
        shutil.copy2(f, out / "cases" / f.name)
        text = f.read_text(encoding="utf-8", errors="replace")
        manifest.append({"case": case, "bytes": f.stat().st_size, "trigger": review_trigger(text)})
    seed = out / "seed"
    pd = seed / "al" / "personas" / "default"
    pd.mkdir(parents=True)
    for name in SEED_FILES:
        copy_tree(LIVE_PDIR / name, pd / name)
    for name in SEED_DIRS:
        copy_tree(LIVE_PDIR / name, pd / name)
    copy_tree(LIVE_IMPROVEMENT, seed / "agent-improvement")
    copy_tree(LIVE_AGENTS_MD, seed / "AGENTS.md")
    (out / "manifest.json").write_text(json.dumps({
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_reviews": str(reviews), "seed_from": str(LIVE_PDIR), "rng_seed": args.seed,
        "cases": manifest}, indent=2), encoding="utf-8")
    print(f"corpus: {len(manifest)} cases -> {out}")
    print(f"triggers: {sorted({m['trigger'] for m in manifest})}; seed store copied (read-only on live)")


# ---------------------------------------------------------------------------
# variants
# ---------------------------------------------------------------------------

def materialize(ref: str, dest: Path) -> Path:
    """Extract skills/autolearn of git ``ref`` into ``dest``; returns the skill dir."""
    if dest.exists():
        return dest / "skills" / "autolearn"
    if ref == "WORKTREE":
        shutil.copytree(REPO / "skills" / "autolearn", dest / "skills" / "autolearn",
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    else:
        data = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", ref, "skills/autolearn"],
                              capture_output=True, check=True).stdout
        dest.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            tf.extractall(dest, filter="data")
    commit = "WORKTREE" if ref == "WORKTREE" else subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", ref], capture_output=True, text=True, check=True).stdout.strip()
    (dest / "REF").write_text(f"{ref} {commit}\n", encoding="utf-8")
    return dest / "skills" / "autolearn"


_HOME_ALTS = [re.escape("$HOME"), re.escape("${HOME}"), "~"]


def _home_prefix_re(suffix: str) -> re.Pattern:
    homes = _HOME_ALTS + [re.escape(posix(Path.home())), re.escape(str(Path.home()))]
    # The lookahead keeps `.autolearn` from matching inside `.autolearn-eval`.
    return re.compile(r"(?:" + "|".join(homes) + r")[/\\]" + suffix + r"(?![\w-])")


SKILL_PATH_RE = _home_prefix_re(r"\.agents[/\\]skills[/\\]autolearn")
STORE_PATH_RE = _home_prefix_re(r"\.autolearn")


def rewrite_paths(text: str, skill_dir: Path, home: Path) -> str:
    """Point the protocol at the variant's code and the sandbox store, never the live ones.

    Placeholders first, targets last: the targets live under ~/.autolearn-eval,
    so a sequential replace would re-match its own output.
    """
    text = SKILL_PATH_RE.sub("\x00SKILL\x00", text)
    text = STORE_PATH_RE.sub("\x00STORE\x00", text)
    # Bare relative mentions only (already-absolute ones were rewritten above).
    text = re.sub(r"(?<![/\\\w\x00])references/observer\.md", "\x00SKILL\x00/references/observer.md", text)
    return text.replace("\x00SKILL\x00", posix(skill_dir)).replace("\x00STORE\x00", posix(home))


def build_message(review_text: str, skill_dir: Path, home: Path) -> str:
    body = re.sub(
        r"Load the autolearn skill and follow references/reviewer\.md for the\s+full signal taxonomy and action protocol\.",
        "Follow the reviewer protocol appended at the end of this message (it replaces loading the autolearn skill).",
        review_text)
    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    skill_body = skill_md.split("---", 2)[2] if skill_md.startswith("---") else skill_md
    reviewer = (skill_dir / "references" / "reviewer.md").read_text(encoding="utf-8")
    proto = ("\n\n---\n\n# Appended: autolearn reviewer protocol\n\n"
             "## Shared skill reference (signal taxonomy, hard gates)\n\n" + skill_body +
             "\n\n## Reviewer mode (references/reviewer.md)\n\n" + reviewer)
    return body + rewrite_paths(proto, skill_dir, home)


def provider_subset(model: str | None) -> dict:
    live = json.loads(LIVE_OC_CONFIG.read_text(encoding="utf-8"))
    model = model or live.get("model")
    prov = (model or "").split("/", 1)[0]
    cfg = {"$schema": "https://opencode.ai/config.json", "model": model}
    if prov in (live.get("provider") or {}):
        cfg["provider"] = {prov: live["provider"][prov]}
    return cfg


def external_dirs(*dirs: Path) -> dict:
    """external_directory rules: allow these trees, deny everything else.

    OpenCode matches native paths (backslashes on Windows); forward-slash
    patterns silently never match, which turned every read into an error in
    the first pilot. Emit both forms. Order matters: last match wins, so the
    catch-all deny comes first.
    """
    rules = {"*": "deny"}
    for d in dirs:
        rules[str(d) + os.sep + "*"] = "allow"
        rules[posix(d) + "/*"] = "allow"
    return rules


def write_sandbox_config(xdg: Path, sb: Path, skill_dir: Path, model: str | None, agents_md: str | None) -> None:
    cfg = provider_subset(model)
    cfg["small_model"] = cfg["model"]
    cfg["instructions"] = [str(sb / "al" / "personas" / "default" / "memory.context.md")]
    allowed = external_dirs(sb, skill_dir.parents[1])
    cfg["permission"] = {"external_directory": allowed}
    cfg["agent"] = {"autolearn-replay": {
        "description": "Replays an autolearn review in a sandbox", "hidden": True, "steps": 20,
        "prompt": REPLAY_AGENT_PROMPT,
        "permission": {"bash": "allow", "read": "allow", "glob": "allow", "grep": "allow", "write": "allow",
                       "edit": "deny", "webfetch": "deny", "task": "deny", "skill": "deny",
                       "external_directory": allowed},
    }}
    oc = xdg / "opencode"
    oc.mkdir(parents=True, exist_ok=True)
    (oc / "opencode.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    if agents_md is not None:
        (oc / "AGENTS.md").write_text(agents_md, encoding="utf-8")


# ---------------------------------------------------------------------------
# store state + diff
# ---------------------------------------------------------------------------

def store_state(pdir: Path, improvement: Path) -> dict:
    mems = {r["id"]: r for r in read_jsonl(pdir / "memories.jsonl") if r.get("id")}
    skills = {}
    for f in sorted((pdir / "skills").glob("*/SKILL.md")):
        skills[f.parent.name] = f.read_text(encoding="utf-8", errors="replace")
    wiki = {posix(p.relative_to(pdir / "wiki")): sha(p) for p in (pdir / "wiki").rglob("*") if p.is_file()} if (pdir / "wiki").exists() else {}
    props = []
    try:
        props = json.loads((pdir / "skills" / ".proposals.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    ctx = (pdir / "memory.context.md")
    ctx_text = ctx.read_text(encoding="utf-8", errors="replace") if ctx.exists() else ""
    rules = (improvement / "rules.yaml")
    return {
        "memories": mems, "skills": skills, "wiki": wiki, "proposals": props,
        "observations": len(read_jsonl(pdir / "observations.jsonl")),
        "context_entries": sum(1 for l in ctx_text.splitlines() if l.startswith("- ")),
        "context_chars": len(ctx_text),
        "rules_sha": sha(rules), "rules_text": rules.read_text(encoding="utf-8", errors="replace") if rules.exists() else "",
    }


def _near(a: str, b: str) -> bool:
    ta, tb = set(registry.extract_topics(a)), set(registry.extract_topics(b))
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    return (inter / len(ta | tb) >= DUP_CFG["dedupe_jaccard"]
            or (inter / min(len(ta), len(tb)) >= DUP_CFG["dedupe_containment"] and min(len(ta), len(tb)) >= DUP_CFG["dedupe_min_tokens"]))


def diff_states(before: dict, after: dict, pdir: Path, conversation: str) -> dict:
    bm, am = before["memories"], after["memories"]
    act = lambda r: r.get("status") == "active"  # noqa: E731
    added = [am[i] for i in am if i not in bm and act(am[i])]
    removed = [bm[i] for i in bm if act(bm[i]) and (i not in am or not act(am[i]))]
    edited = [am[i] for i in am if i in bm and act(am[i]) and am[i].get("text") != bm[i].get("text")]
    reinforced = [am[i] for i in am if i in bm and len(am[i].get("reinforcements") or []) > len(bm[i].get("reinforcements") or [])]
    relayered = [am[i] for i in am if i in bm and am[i].get("layer") != bm[i].get("layer") and bm[i].get("layer")]
    pre_active = [r for r in bm.values() if act(r)]
    dup_added = [r for r in added if any(_near(r.get("text", ""), p.get("text", "")) for p in pre_active)]
    conv_low = conversation.lower()
    skills_created = sorted(set(after["skills"]) - set(before["skills"]))
    skills_modified = sorted(n for n in after["skills"] if n in before["skills"] and after["skills"][n] != before["skills"][n])
    new_props = [p for p in after["proposals"] if p.get("id") not in {q.get("id") for q in before["proposals"]}]
    obs = read_jsonl(pdir / "observations.jsonl")[before["observations"]:]
    otypes: dict[str, int] = {}
    for o in obs:
        otypes[o.get("type", "?")] = otypes.get(o.get("type", "?"), 0) + 1
    return {
        "memories_added": len(added),
        "memories_added_by_type": {t: sum(1 for r in added if r.get("type") == t) for t in ("memory", "user")},
        "memories_added_by_layer": {l: sum(1 for r in added if (r.get("layer") or "none") == l) for l in ("core", "archive", "none")},
        "memories_added_dup_of_existing": len(dup_added),
        "memories_added_over_400": sum(1 for r in added if len(r.get("text", "")) > 400),
        "memories_added_with_check": sum(1 for r in added if r.get("check")),
        "memories_removed": len(removed),
        "memories_edited": len(edited),
        "memories_reinforced": len(reinforced),
        "memories_relayered": len(relayered),
        "context_entries_after": after["context_entries"],
        "context_chars_after": after["context_chars"],
        "skills_created": skills_created,
        "skills_modified": skills_modified,
        "skills_modified_not_in_conversation": [n for n in skills_modified if n.lower() not in conv_low],
        "skill_bytes_added": sum(len(after["skills"][n]) - len(before["skills"][n]) for n in skills_modified),
        "skill_proposals": len(new_props),
        "wiki_files_changed": sorted(k for k in after["wiki"] if before["wiki"].get(k) != after["wiki"][k]),
        "rules_changed": before["rules_sha"] != after["rules_sha"],
        "review_complete_logged": otypes.get("review_complete", 0) > 0,
        "nothing_recorded_logged": any(o.get("type") == "review_complete" and o.get("nothing_recorded") for o in obs),
        "write_refused": otypes.get("write_refused", 0),
        "write_blocked": otypes.get("write_blocked", 0),
        "skill_edit_denied": otypes.get("skill_edit_denied", 0),
        "observation_types": otypes,
        "_texts": {
            "added": [f"[{r.get('layer', '-')}/{r.get('type')}] {r.get('text')}" for r in added],
            "edited": [f"[{r.get('layer', '-')}/{r.get('type')}] {bm[r['id']].get('text')}  =>  {r.get('text')}" for r in edited],
            "reinforced": [r.get("text", "")[:200] for r in reinforced],
            "removed": [r.get("text", "")[:200] for r in removed],
            "proposals": [f"{p.get('skill')}: replace {p.get('old', '')[:200]!r} with {p.get('new', '')[:300]!r} ({p.get('reason', '')[:200]})" for p in new_props],
        },
    }


def changes_markdown(diff: dict, before: dict, after: dict) -> str:
    """Human/judge-readable account of what a variant wrote."""
    t = diff["_texts"]
    out = []
    def section(title, items):
        out.append(f"### {title} ({len(items)})")
        out.extend(f"- {i}" for i in items) if items else out.append("- none")
        out.append("")
    section("Memories added", t["added"])
    section("Memories edited (old => new)", t["edited"])
    section("Memories reinforced (same lesson seen again)", t["reinforced"])
    section("Memories removed", t["removed"])
    section("Skill proposals staged", t["proposals"])
    import difflib
    skill_items = []
    for n in diff["skills_created"]:
        skill_items.append(f"CREATED {n}:\n```\n{after['skills'][n][:2500]}\n```")
    for n in diff["skills_modified"]:
        d = "\n".join(difflib.unified_diff(before["skills"][n].splitlines(), after["skills"][n].splitlines(), lineterm="", n=1))
        skill_items.append(f"MODIFIED {n}:\n```diff\n{d[:2500]}\n```")
    section("Skills created/modified", skill_items)
    section("Wiki files written", diff["wiki_files_changed"])
    out.append(f"Escalation rules store changed: {'yes' if diff['rules_changed'] else 'no'}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def run_events_stats(events_path: Path) -> dict:
    evs = read_jsonl(events_path)
    fins = [e for e in evs if e.get("type") == "step_finish"]
    tin = sum(float(((e.get("part") or {}).get("tokens") or {}).get("input") or 0) for e in fins)
    tout = sum(float(((e.get("part") or {}).get("tokens") or {}).get("output") or 0) for e in fins)
    cost = sum(float((e.get("part") or {}).get("cost") or 0) for e in fins)
    texts = [(e.get("part") or {}).get("text", "") for e in evs if e.get("type") == "text"]
    sid = next((e.get("sessionID") for e in evs if e.get("sessionID")), None)
    return {"steps": len(fins), "tool_calls": sum(1 for e in evs if e.get("type") == "tool_use"),
            "errors": sum(1 for e in evs if e.get("type") == "error"),
            "tokens_in": tin, "tokens_out": tout, "cost": round(cost, 6),
            "final_text": (texts[-1] if texts else "")[:1500], "session_id": sid,
            "hit_step_limit": bool(fins) and (fins[-1].get("part") or {}).get("reason") not in ("stop", "end_turn", None)}


def live_leak_check() -> bool:
    for o in read_jsonl(LIVE_PDIR / "observations.jsonl")[-400:]:
        if str(o.get("review_id", "")).startswith("replay-"):
            return True
    return False


def run_case(case_file: Path, variant: str, skill_dir: Path, seed: Path, run_dir: Path,
             model: str | None, timeout_s: int, dry_run: bool) -> dict:
    case = case_file.stem
    sb = run_dir / variant / case
    if (sb / "result.json").exists():
        return json.loads((sb / "result.json").read_text(encoding="utf-8"))
    if sb.exists():
        shutil.rmtree(sb)
    home = sb / "al"
    pdir = home / "personas" / "default"
    copy_tree(seed / "al", home)
    copy_tree(seed / "agent-improvement", sb / "agent-improvement")
    (sb / "project").mkdir(parents=True, exist_ok=True)
    agents_md = None
    if (seed / "AGENTS.md").exists():
        agents_md = rewrite_paths((seed / "AGENTS.md").read_text(encoding="utf-8"), skill_dir, home)
    env = dict(os.environ)
    for k in ("AUTOLEARN_CURATOR", "AUTOLEARN_SESSION_ID", "AUTOLEARN_DISABLED", "OPENCODE_CONFIG"):
        env.pop(k, None)
    env.update({
        "AUTOLEARN_HOME": str(home), "AGENTS_SKILLS_DIR": str(sb / "agents-skills"),
        "AGENT_IMPROVEMENT_HOME": str(sb / "agent-improvement"),
        "AGENT_GLOBAL_AGENTS_MD": str(sb / "xdg-config" / "opencode" / "AGENTS.md"),
        "AGENT_PROJECT_AGENTS_MD": str(sb / "project" / "AGENTS.md"),
        "AGENT_PROJECT_NAME": "replay",
        "AUTOLEARN_REVIEWER": "1", "AUTOLEARN_REVIEW_ID": f"replay-{case}",
        "XDG_CONFIG_HOME": str(sb / "xdg-config"),
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
    })
    # Deploy the variant onto the seed exactly as an upgrade would (the new
    # design migrates layers and writes the archive index here).
    prep = subprocess.run(["uv", "run", "-q", str(skill_dir / "scripts" / "autolearn.py"), "memory", "compose"],
                          capture_output=True, text=True, env=env, cwd=str(sb / "project"), timeout=600)
    before = store_state(pdir, sb / "agent-improvement")
    review_text = case_file.read_text(encoding="utf-8", errors="replace")
    msg = build_message(review_text, skill_dir, home)
    (sb / "message.md").write_text(msg, encoding="utf-8")
    write_sandbox_config(sb / "xdg-config", sb, skill_dir, model, agents_md)
    result = {"case": case, "variant": variant, "prep_rc": prep.returncode, "prep_err": prep.stderr[-500:]}
    t0 = time.time()
    rc, timed_out = None, False
    if not dry_run:
        cmd = [OPENCODE, "run", "--pure", "--format", "json", "--agent", "autolearn-replay",
               "--title", f"autolearn replay {variant} {case}"]
        if model:
            cmd += ["--model", model]
        with (sb / "message.md").open("rb") as stdin, (sb / "events.jsonl").open("wb") as out, \
                (sb / "stderr.txt").open("wb") as err:
            try:
                proc = subprocess.run(cmd, stdin=stdin, stdout=out, stderr=err, env=env,
                                      cwd=str(sb / "project"), timeout=timeout_s)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                timed_out = True
    result.update({"rc": rc, "timed_out": timed_out, "duration_s": round(time.time() - t0, 1)})
    if not dry_run:
        stats = run_events_stats(sb / "events.jsonl")
        if stats.get("session_id"):
            subprocess.run([OPENCODE, "session", "delete", stats["session_id"]], capture_output=True,
                           env=env, cwd=str(sb / "project"), timeout=120)
        result.update(stats)
    after = store_state(pdir, sb / "agent-improvement")
    conv = conversation_of(review_text)
    diff = diff_states(before, after, pdir, conv)
    (sb / "changes.md").write_text(changes_markdown(diff, before, after), encoding="utf-8")
    texts = diff.pop("_texts")
    (sb / "texts.json").write_text(json.dumps(texts, indent=2, ensure_ascii=False), encoding="utf-8")
    result["diff"] = diff
    result["leak"] = live_leak_check()
    # The sandbox config holds provider settings: never leave it behind.
    shutil.rmtree(sb / "xdg-config", ignore_errors=True)
    if not dry_run:
        (sb / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def cmd_run(args) -> None:
    corpus = Path(args.corpus).expanduser()
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = Path(args.out).expanduser() / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    variants = {}
    for spec in args.variant:
        name, _, ref = spec.partition("=")
        variants[name] = materialize(ref, run_dir / f"src-{name}")
    cases = [m["case"] for m in manifest["cases"]]
    if args.cases:
        cases = [c for c in cases if c in set(args.cases.split(","))]
    meta = {"corpus": str(corpus), "variants": {n: (run_dir / f"src-{n}" / "REF").read_text().strip() for n in variants},
            "model": args.model, "cases": cases, "started_at": datetime.now().isoformat(timespec="seconds")}
    (run_dir / "run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    jobs = [(c, v) for c in cases for v in variants]
    print(f"run {run_id}: {len(cases)} cases x {len(variants)} variants = {len(jobs)} reviewer runs -> {run_dir}")
    with cf.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futs = {pool.submit(run_case, corpus / "cases" / f"{c}.md", v, variants[v], corpus / "seed", run_dir,
                            args.model, args.timeout, args.dry_run): (c, v) for c, v in jobs}
        for fut in cf.as_completed(futs):
            c, v = futs[fut]
            try:
                r = fut.result()
                d = r["diff"]
                flag = " LEAK!" if r.get("leak") else ""
                print(f"  {v:>6} {c}: rc={r.get('rc')} steps={r.get('steps')} {r.get('duration_s')}s "
                      f"mem+{d['memories_added']} edit{d['memories_edited']} reinf{d['memories_reinforced']} "
                      f"skills+{len(d['skills_created'])}/~{len(d['skills_modified'])} props{d['skill_proposals']} "
                      f"logged={d['review_complete_logged']}{flag}")
            except Exception as exc:  # keep the batch going; the report shows the gap
                print(f"  {v:>6} {c}: FAILED {exc}")
    print(f"done. next: uv run eval/judge.py --run {run_dir}  |  eval/replay.py report --run {run_dir}")


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def _mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(sum(xs) / len(xs), 2) if xs else None


def cmd_report(args) -> None:
    run_dir = Path(args.run).expanduser()
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    variants = list(meta["variants"])
    res = {v: {} for v in variants}
    for v in variants:
        for f in (run_dir / v).glob("*/result.json"):
            r = json.loads(f.read_text(encoding="utf-8"))
            res[v][r["case"]] = r
    judge = {}
    jf = run_dir / "judge.jsonl"
    if jf.exists():
        for row in read_jsonl(jf):
            judge[row["case"]] = row
    rows = [
        ("Runs completed", lambda rs: f"{sum(1 for r in rs if r.get('rc') == 0)}/{len(rs)}"),
        ("Outcome logged (review_complete)", lambda rs: f"{sum(1 for r in rs if r['diff']['review_complete_logged'])}/{len(rs)}"),
        ("Hit the step limit", lambda rs: sum(1 for r in rs if r.get("hit_step_limit"))),
        ("Steps (mean)", lambda rs: _mean([r.get("steps") for r in rs])),
        ("Duration s (mean)", lambda rs: _mean([r.get("duration_s") for r in rs])),
        ("Input tokens (mean)", lambda rs: _mean([r.get("tokens_in") for r in rs])),
        ("Memories added (total)", lambda rs: sum(r["diff"]["memories_added"] for r in rs)),
        ("  ... duplicates of existing entries", lambda rs: sum(r["diff"]["memories_added_dup_of_existing"] for r in rs)),
        ("  ... over 400 chars", lambda rs: sum(r["diff"]["memories_added_over_400"] for r in rs)),
        ("  ... into core / archive", lambda rs: f"{sum(r['diff']['memories_added_by_layer']['core'] for r in rs)} / {sum(r['diff']['memories_added_by_layer']['archive'] for r in rs)}"),
        ("  ... with a code-state check", lambda rs: sum(r["diff"]["memories_added_with_check"] for r in rs)),
        ("Memories strengthened", lambda rs: sum(r["diff"]["memories_reinforced"] for r in rs)),
        ("Memories edited in place", lambda rs: sum(r["diff"]["memories_edited"] for r in rs)),
        ("Entries injected every session (after, mean)", lambda rs: _mean([r["diff"]["context_entries_after"] for r in rs])),
        ("Skills created", lambda rs: sum(len(r["diff"]["skills_created"]) for r in rs)),
        ("Skills modified directly", lambda rs: sum(len(r["diff"]["skills_modified"]) for r in rs)),
        ("  ... skill not mentioned in conversation", lambda rs: sum(len(r["diff"]["skills_modified_not_in_conversation"]) for r in rs)),
        ("Skill bytes added", lambda rs: sum(r["diff"]["skill_bytes_added"] for r in rs)),
        ("Skill proposals staged", lambda rs: sum(r["diff"]["skill_proposals"] for r in rs)),
        ("Writes refused by the CLI (dup/cap)", lambda rs: sum(r["diff"]["write_refused"] for r in rs)),
        ("Writes blocked by the safety scan", lambda rs: sum(r["diff"]["write_blocked"] for r in rs)),
        ("Reviews recording nothing", lambda rs: sum(1 for r in rs if r["diff"]["memories_added"] + r["diff"]["memories_edited"] + r["diff"]["memories_reinforced"] + len(r["diff"]["skills_created"]) + len(r["diff"]["skills_modified"]) + r["diff"]["skill_proposals"] == 0)),
        ("LEAKS into the live store", lambda rs: sum(1 for r in rs if r.get("leak"))),
    ]
    common = sorted(set.intersection(*[set(res[v]) for v in variants])) if variants else []
    lines = [f"# Replay report — {run_dir.name}", "",
             f"Corpus: `{meta['corpus']}` · model: `{meta.get('model') or 'default'}` · cases compared: {len(common)}", ""]
    for v in variants:
        lines.append(f"- **{v}**: `{meta['variants'][v]}`")
    lines += ["", "| Metric | " + " | ".join(variants) + " |", "|---|" + "---|" * len(variants)]
    for label, fn in rows:
        lines.append(f"| {label} | " + " | ".join(str(fn([res[v][c] for c in common])) for v in variants) + " |")
    if judge:
        lines += ["", "## Blind pairwise judgment", ""]
        wins = {v: 0 for v in variants}
        ties = 0
        scores = {v: {} for v in variants}
        for c in common:
            j = judge.get(c)
            if not j:
                continue
            if j.get("winner") in wins:
                wins[j["winner"]] += 1
            else:
                ties += 1
            for v in variants:
                for k, val in (j.get("scores", {}).get(v) or {}).items():
                    scores[v].setdefault(k, []).append(val)
        judged = sum(1 for c in common if c in judge)
        lines.append(f"Judged cases: {judged} · " + " · ".join(f"{v} wins {wins[v]}" for v in variants) + f" · ties {ties}")
        crit = sorted({k for v in variants for k in scores[v]})
        if crit:
            lines += ["", "| Criterion (1-5, mean) | " + " | ".join(variants) + " |", "|---|" + "---|" * len(variants)]
            for k in crit:
                lines.append(f"| {k} | " + " | ".join(str(_mean(scores[v].get(k, []))) for v in variants) + " |")
    lines += ["", "## Per case", "", "| Case | trigger | " + " | ".join(f"{v}: mem+/edit/reinf/skill/prop" for v in variants) + (" | judge |" if judge else " |"),
              "|---|---|" + "---|" * len(variants) + ("---|" if judge else "")]
    trig = {m["case"]: m["trigger"] for m in json.loads((Path(meta["corpus"]) / "manifest.json").read_text(encoding="utf-8"))["cases"]}
    for c in common:
        cells = []
        for v in variants:
            d = res[v][c]["diff"]
            cells.append(f"{d['memories_added']}/{d['memories_edited']}/{d['memories_reinforced']}/"
                         f"{len(d['skills_created']) + len(d['skills_modified'])}/{d['skill_proposals']}"
                         + ("" if d["review_complete_logged"] else " (unlogged)"))
        jcell = f" {judge[c].get('winner', '?')} |" if judge and c in judge else (" – |" if judge else "")
        lines.append(f"| {c} | {trig.get(c, '?')} | " + " | ".join(cells) + " |" + jcell)
    text = "\n".join(lines) + "\n"
    (run_dir / "report.md").write_text(text, encoding="utf-8")
    print(text)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="Freeze a stratified corpus of real reviews + a seed store copy")
    p.add_argument("--reviews", default=str(LIVE_PDIR / "reviews"))
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--min-bytes", type=int, default=3000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", required=True)
    r = sub.add_parser("run", help="Replay variants over the corpus in sandboxes")
    r.add_argument("--corpus", required=True)
    r.add_argument("--variant", action="append", required=True, help="name=gitref (or name=WORKTREE)")
    r.add_argument("--model", default=None, help="provider/model (default: your opencode default)")
    r.add_argument("--jobs", type=int, default=2)
    r.add_argument("--timeout", type=int, default=1500, help="seconds per reviewer run")
    r.add_argument("--cases", default=None, help="comma-separated subset of case ids")
    r.add_argument("--run-id", default=None, help="resume / name a run (completed cases are skipped)")
    r.add_argument("--out", default=str(EVAL_ROOT / "runs"))
    r.add_argument("--dry-run", action="store_true", help="build sandboxes + prompts, do not call the model")
    rep = sub.add_parser("report", help="Aggregate a run (and judge results) into report.md")
    rep.add_argument("--run", required=True)
    args = ap.parse_args()
    {"prepare": cmd_prepare, "run": cmd_run, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
