# Evaluating a design change

Three instruments answer "is the new design better?" from different angles.
All of them are read-only on the live store; outputs go to `~/.autolearn-eval/`
(personal data, never committed).

| Instrument | Question it answers | Cost |
|---|---|---|
| `store_report.py` | Is the live store healthier than before? (longitudinal, real usage) | free, seconds |
| `replay.py` | On the SAME conversations, what does each design write? (controlled A/B) | 2 reviewer runs per case |
| `judge.py` | Which design's writes would make future sessions better? (quality) | 1–2 LLM calls per case |

## 1. Baseline before deploying

```bash
uv run --with python-slugify eval/store_report.py snapshot --home ~/.autolearn \
  --out ~/.autolearn-eval/baselines/live-$(date +%F).json
```

## 2. Controlled A/B replay

```bash
# Freeze 20 real reviews (stratified by size x trigger) + a copy of the store.
uv run --with python-slugify eval/replay.py prepare --n 20 --out ~/.autolearn-eval/corpus-$(date +%F)

# Replay both designs. Each case x variant runs in its own sandbox.
uv run --with python-slugify --with pyyaml eval/replay.py run \
  --corpus ~/.autolearn-eval/corpus-<date> \
  --variant old=main --variant new=feat/robust-learning-loop --jobs 2

uv run eval/judge.py --run ~/.autolearn-eval/runs/<id> --both-orders
uv run --with python-slugify eval/replay.py report --run ~/.autolearn-eval/runs/<id>
```

`report.md` compares, per variant: runs completed, outcome logged, step-limit
hits, steps / duration / tokens, memories added (and how many duplicate an
existing entry, exceed 400 chars, land in core vs archive), strengthened and
edited entries, entries injected every session, skills created / modified /
modified-without-being-discussed, skill bytes added, proposals staged, CLI
refusals and safety blocks, empty reviews, live-store leaks, and the judge's
win rate and per-criterion scores (recall, precision, curation, correctness,
skill handling). Every case directory keeps `message.md`, `events.jsonl`,
`changes.md` and `result.json` for inspection; runs resume with `--run-id`.

What "better" means for the new design:

- **more** outcomes logged, fewer step-limit hits;
- **fewer** duplicate / over-long memories, more strengthen/edit;
- **no** direct skill modifications, proposals only for discussed skills;
- **equal or better** judge recall with better precision and curation.

## 3. After deploying: weekly snapshots

```bash
uv run --with python-slugify eval/store_report.py snapshot --home ~/.autolearn --since <deploy-date> \
  --out ~/.autolearn-eval/baselines/live-week1.json
uv run --with python-slugify eval/store_report.py compare \
  ~/.autolearn-eval/baselines/live-<before>.json ~/.autolearn-eval/baselines/live-week1.json
```

Headline metrics (direction marked better/worse in the table): completion
rate (exact review-id join after deploy), unlogged and lost reviews, review
duration and steps, share of active memories injected, near-duplicate pairs,
over-long entries, code-state checks, skill size p90 / max, dated bullets,
incident-log-shaped skills, loaded share, never-loaded-but-patched skills,
orphan tmp files, safety findings in injected content. Observations carry
`design: rl-2026.10`, so pre/post windows never mix.

## Isolation guarantees of the replay

- Store, skills dir, escalation rules and AGENTS.md targets are sandbox copies
  (`AUTOLEARN_HOME`, `AGENTS_SKILLS_DIR`, `AGENT_IMPROVEMENT_HOME`,
  `AGENT_GLOBAL_AGENTS_MD`, `AGENT_PROJECT_AGENTS_MD`).
- OpenCode runs with `--pure` and `XDG_CONFIG_HOME` = a sandbox config holding
  only the provider the model needs, the sandbox's own `memory.context.md` as
  instructions, and a replay agent with `skill`/`edit` denied and external
  directories limited to the sandbox and the variant source. The config is
  deleted when the case finishes; sessions are deleted after each run.
- Live paths in the inlined protocol and the AGENTS.md copy are rewritten to
  the sandbox and the variant's scripts.
- Runs are tagged `AUTOLEARN_REVIEW_ID=replay-<case>`; a live observation with
  that tag is reported as a LEAK.

Known, symmetric caveats: the session-search snapshot includes sessions newer
than the replayed review; the protocol is inlined rather than loaded via the
skill tool; one LLM judge (use `--both-orders` against position bias).
