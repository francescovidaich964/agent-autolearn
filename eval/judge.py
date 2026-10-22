#!/usr/bin/env python3
"""Blind pairwise judgment of two replayed reviewer designs.

For every case both variants completed, an LLM judge sees the conversation
and the two variants' changes to the store (what they would make future
sessions know), labelled only "A" and "B" in a seeded random order, and
scores them on a fixed rubric. Results go to <run>/judge.jsonl and are
aggregated by `replay.py report`.

    uv run eval/judge.py --run ~/.autolearn-eval/runs/<id> [--model provider/model] [--both-orders]

`--both-orders` judges every case twice with A/B swapped and keeps the
verdict only when both orders agree (otherwise: tie). It doubles the cost
but removes the position bias LLM judges are known for.

The judge runs with every tool denied, plugins off, and no instructions
(sandbox XDG config), so it sees nothing but the prompt.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import replay  # noqa: E402

CRITERIA = ["recall", "precision", "curation", "correctness", "skill_handling"]

RUBRIC = """You are evaluating two automated "learning reviewers". Each read the same conversation between a
user and a coding agent, and wrote durable knowledge (memories, user preferences, skill changes) that will
be fed to FUTURE agent sessions. Judge which reviewer would make future sessions better.

You are also shown what the store ALREADY contained before the review. Both reviewers started from that
same store.

Score each reviewer from 1 (poor) to 5 (excellent) on:
- recall: did it capture the conversation's real durable signals that were NOT already in the store (user
  corrections, stated preferences or workflow specs, workarounds that worked, root-caused failures)?
  Missing a clear NEW correction is serious. Not re-recording something already stored is NOT a miss.
- precision: did it avoid noise (one-off task details, transient errors, restating the conversation,
  speculative or over-generalized rules)? Writing nothing when nothing new and durable happened is CORRECT.
- curation: did it fold knowledge into existing entries (strengthen / edit) instead of adding
  near-duplicates of what the store already held, or "supersedes" layers? Are entries concise and
  actionable? Re-adding a known fact as a new entry is a curation flaw.
- correctness: is what it wrote true to the conversation and safe to act on?
- skill_handling: were skill changes justified by evidence in the conversation and minimal? Editing a
  skill that the conversation never used, or appending history instead of fixing the text, is poor.
  If neither touched skills and none was warranted, score 5.

More output is NOT better. Judge the effect on future sessions.

Answer with ONLY a JSON object, no prose before or after:
{"A": {"recall": n, "precision": n, "curation": n, "correctness": n, "skill_handling": n},
 "B": {"recall": n, "precision": n, "curation": n, "correctness": n, "skill_handling": n},
 "preferred": "A" | "B" | "tie",
 "rationale": "<two sentences>"}
"""


def build_prompt(conversation: str, changes_a: str, changes_b: str, max_conv: int, known: str = "") -> str:
    conv = conversation if len(conversation) <= max_conv else conversation[:max_conv // 2] + "\n\n[... middle truncated ...]\n\n" + conversation[-max_conv // 2:]
    return (RUBRIC + "\n\n# Already in the store before the review (shared by both)\n\n" + (known or "(not available)") +
            "\n\n# Conversation\n\n" + conv + "\n\n# Reviewer A wrote\n\n" + changes_a +
            "\n\n# Reviewer B wrote\n\n" + changes_b + "\n")


def parse_verdict(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if d.get("preferred") not in ("A", "B", "tie"):
        return None
    for side in ("A", "B"):
        if not isinstance(d.get(side), dict):
            return None
    return d


def judge_once(prompt: str, model: str | None, workdir: Path, timeout: int) -> tuple[dict | None, str]:
    xdg = workdir / "xdg"
    cfg = replay.provider_subset(model)
    cfg["instructions"] = []
    deny = {k: "deny" for k in ("bash", "read", "glob", "grep", "write", "edit", "webfetch", "task", "skill", "list", "todowrite")}
    cfg["agent"] = {"autolearn-judge": {"description": "judge", "hidden": True, "steps": 2,
                                        "prompt": "You are a careful, impartial evaluator. Reply with JSON only.",
                                        "permission": deny}}
    (xdg / "opencode").mkdir(parents=True, exist_ok=True)
    (xdg / "opencode" / "opencode.json").write_text(json.dumps(cfg), encoding="utf-8")
    env = dict(os.environ, XDG_CONFIG_HOME=str(xdg), AUTOLEARN_REVIEWER="1")
    env.pop("OPENCODE_CONFIG", None)
    cmd = [replay.OPENCODE, "run", "--pure", "--format", "json", "--agent", "autolearn-judge", "--title", "autolearn judge"]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(cmd, input=prompt.encode("utf-8"), capture_output=True, env=env,
                              cwd=str(workdir), timeout=timeout)
        out = proc.stdout.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return None, "timeout"
    finally:
        shutil.rmtree(xdg, ignore_errors=True)
    evs = [json.loads(l) for l in out.splitlines() if l.strip().startswith("{")]
    sid = next((e.get("sessionID") for e in evs if e.get("sessionID")), None)
    if sid:
        subprocess.run([replay.OPENCODE, "session", "delete", sid], capture_output=True, env=env, timeout=120)
    text = "".join((e.get("part") or {}).get("text", "") for e in evs if e.get("type") == "text")
    return parse_verdict(text), text[-800:]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--both-orders", action="store_true")
    ap.add_argument("--max-conversation-chars", type=int, default=30000)
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    run_dir = Path(args.run).expanduser()
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    variants = list(meta["variants"])
    if len(variants) != 2:
        sys.exit("judge compares exactly two variants")
    corpus = Path(meta["corpus"])
    done = {r["case"] for r in replay.read_jsonl(run_dir / "judge.jsonl")}
    rng = random.Random(args.seed)
    workdir = Path(tempfile.mkdtemp(prefix="al-judge-", dir=str(run_dir)))
    try:
        for case in meta["cases"]:
            if case in done:
                continue
            paths = [run_dir / v / case / "changes.md" for v in variants]
            if not all(p.exists() and (p.parent / "result.json").exists() for p in paths):
                continue
            conv = replay.conversation_of((corpus / "cases" / f"{case}.md").read_text(encoding="utf-8", errors="replace"))
            changes = {v: p.read_text(encoding="utf-8") for v, p in zip(variants, paths)}
            known_f = paths[0].parent / "known.md"
            known = known_f.read_text(encoding="utf-8") if known_f.exists() else ""
            orders = [variants[:], variants[::-1]] if args.both_orders else [rng.sample(variants, 2)]
            verdicts = []
            for a, b in orders:
                v, raw = judge_once(build_prompt(conv, changes[a], changes[b], args.max_conversation_chars, known),
                                    args.model, workdir, args.timeout)
                if v is None:
                    verdicts.append({"order": [a, b], "error": raw})
                    continue
                win = {"A": a, "B": b}.get(v["preferred"], "tie")
                verdicts.append({"order": [a, b], "winner": win,
                                 "scores": {a: v["A"], b: v["B"]}, "rationale": v.get("rationale", "")})
            ok = [x for x in verdicts if "winner" in x]
            if not ok:
                row = {"case": case, "error": verdicts[0].get("error", "")[:300]}
            else:
                winners = {x["winner"] for x in ok}
                winner = ok[0]["winner"] if len(winners) == 1 else "tie"
                scores = {v: {k: round(sum(x["scores"][v].get(k, 0) for x in ok) / len(ok), 2) for k in CRITERIA} for v in variants}
                row = {"case": case, "winner": winner, "scores": scores, "orders": len(ok),
                       "rationale": ok[0].get("rationale", "")}
            with (run_dir / "judge.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"  {case}: {row.get('winner', 'ERROR')}  {row.get('rationale', row.get('error', ''))[:140]}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
