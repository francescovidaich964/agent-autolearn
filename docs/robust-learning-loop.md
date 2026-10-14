# The robust learning loop: strategy and evaluation

This document explains what changed in autolearn's design (branch
`feat/robust-learning-loop`, design tag `rl-2026.10`), why, and how the change
was evaluated before deployment. The specification lives in
[`designs/robust-learning-loop/LLD.md`](designs/robust-learning-loop/LLD.md);
the evaluation tooling in [`../eval/`](../eval/README.md).

## Summary

The old loop recorded a lot and delivered little: most reviews never finished,
most learned memories never reached a session, and skills grew by appending
corrections on top of the text they corrected. The new loop is built on one
principle — **write less, deliver what is written, let the actor with the
evidence make the edit, and measure every change** — and was evaluated by
replaying 20 real reviews through both designs in sealed sandboxes and having
a blind judge compare what each wrote.

| On 20 real reviews (time-travelled seeds, blind judge in both orders) | Old | New |
|---|---|---|
| Judge verdict | 2 wins | **16 wins** (2 ties) |
| Recall / precision / curation / correctness / skill handling (1–5) | 2.98 / 3.77 / 3.33 / 4.20 / 3.83 | **4.33 / 4.28 / 4.38 / 4.60 / 4.58** |
| Reviews that logged their outcome | 4 / 20 | **19 / 20** |
| Reviews that hit the 20-step cap | 19 / 20 | **0 / 20** |
| Steps · duration · input tokens per review (mean) | 19.7 · 270 s · 1.92 M | **7.0 · 140 s · 0.27 M** |
| Direct skill modifications (to skills the conversation never used) | 2 (2) | **0 (0)** — 3 proposals instead |
| Writes into the live store during the replay | 0 | 0 |

## 1. What was wrong

Measured on a production store after three weeks of daily use (one Windows
machine, OpenCode v1):

| Symptom | Evidence | Root cause |
|---|---|---|
| Reviews lost or unfinished | 293 reviews spawned, 102 logged an outcome (34.8 %). 50 spawns started while the previous review (median 322 s, p90 1 002 s) was still running | The wrapper's busy/interval gates exited silently **after** the plugin had cleared its buffer, so the conversation was dropped. Reviewers ran out of their 20 steps before logging (the replay later reproduced this: 19 of 20 old reviews hit the cap) |
| Learned memory never delivered | 445 active memories, 7 injected into sessions (1.6 %) | Unbounded registry + a 3 000-char compose budget ranked by retention only; the writer never met the budget, so it never curated |
| Duplicates | 15 near-duplicate memory pairs; 208 of 445 entries over 400 chars | The same signal recorded as both a memory and a user preference; no duplicate check at write time |
| Skill accretion | Skills up to 32 KB, 141 dated bullets across the library, 6.1 patches per recorded load, 10 never-loaded skills patched anyway | `skill patch` could only **append** a bullet; the reviewer patched skills it never saw loaded |
| Lost / clobbered writes | 14 orphaned `*.tmp` files; a 4-process reproduction kept 16 of 60 writes and crashed two writers | Unlocked read-modify-write across concurrent processes; `os.replace` fails on Windows while a reader holds the file |
| Stale facts injected | A "fix not implemented" memory kept being injected after the fix merged | No invalidation for memories about the state of code |
| Frozen telemetry | Outcome-index high-water mark never advanced; usage scan crashed on v1 databases | Uncommitted mark; v2-only schema assumption |
| No project scoping on Windows | Every review tagged with the full home path | `split("/")` on a backslash path |
| Persistence channel for prompt injection | Transcripts (with web/tool output) feed memory that is injected into every session | Secret redaction only |

## 2. The new strategy

### 2.1 Principles

1. **Never lose a review, and know what happened to each one.** Every
   pipeline decision is logged; a review that cannot run now is queued.
2. **Write less, deliver what is written.** A small, hard-capped core goes
   into every session; everything else is retrieved by relevance. The writer
   meets the budget at write time and must curate.
3. **The actor with the evidence makes the edit.** The agent that loaded a
   skill and saw it fail fixes it in place; the reviewer, which only sees a
   truncated transcript, can only propose.
4. **Replace, don't append.** Corrections overwrite the stale text (exact
   replacement), so a skill states the current truth once.
5. **Measure every change.** Same metrics before and after; controlled A/B
   replays before deploying.

### 2.2 Components

**Review pipeline (no-loss queue).** A review refused by the wrapper's busy or
interval gate moves to `reviews/pending/` and is drained by the current gate
holder; the plugin's peek treats a running review as "not now" and keeps its
buffer; throttled exit reviews are queued. Every decision (`deferred`,
`started`, `finished` with exit code and duration, `drained`, `expired`, …) is
appended to `review-runs.jsonl`, each run keeps its step/token accounting next
to the review, and `AUTOLEARN_REVIEW_ID` joins a run to the reviewer's logged
outcome exactly.

**Store integrity.** One cross-process lock (`mkdir`-based, shared by the
Python CLI and the JS plugin) around every read-modify-write; atomic writes
that retry Windows sharing violations and never leave a temp file behind; a
sweep of stale temp files at startup; a write-time scan that refuses
instruction-override payloads, special tokens, invisible Unicode, exfiltration
commands and credentials in anything written to memory or skills.

**Two-tier memory.**

```text
             write (CLI)                                   read (every session)
  reviewer ──► admission ──► core (hard cap: 2 500 + 1 500 chars, ≤ 400/entry) ──► memory.context.md
             │  · near-duplicate? → refuse: strengthen / edit instead
             │  · core full?      → refuse: demote / shorten / --archive
             └──────────────────► archive ──► BM25 slice per session (project, git remote,
                                              first message; frozen for the session)
```

Code-state facts carry a declarative check (file + pattern + present/absent);
the curator retires a memory whose check stops holding. `memory search`
exposes the archive on demand. Existing stores migrate lazily: the entries the
old composer would have injected fill the core, the rest go to the archive.

**Skills edited where the evidence is.** `skill edit --old/--new` replaces
exactly one occurrence (backup kept). The reviewer is refused any direct
skill edit and stages `skill propose` instead; when a session loads that
skill, the pending proposals are appended to the skill text, and the agent
that is using it applies or rejects them. Full rewrites are staged and
applied only if every command, path, URL and inline-code token survives
(or moved to `references/`). `skill lint` flags oversized and
incident-log-shaped skills; `skill audit` separates never-loaded skills whose
trigger recurred (fix the description) from dormant ones; pins need a reason
and are capped.

**Reviewer and curator protocols.** The reviewer works in ~12 tool calls and
always logs last; it searches before writing, records each signal once (memory
*or* user preference), chooses core vs archive, attaches checks to code-state
facts, and evaluates both the user's messages and the agent's verified work.
The curator follows one decision tree for never-loaded skills, stages at most
one compaction per run, and runs under its own agent with a larger step
budget.

**Reviewer input.** User messages are kept up to 4 000 chars (corrections live
there); failed tool calls and skill loads become compact, redacted `Tool`
notes; the review header lists the skills loaded in the conversation.

## 3. How it was evaluated

### 3.1 Instruments

| Instrument | Question | How |
|---|---|---|
| `eval/store_report.py` | Is the live store healthier? | Deterministic metrics (pipeline, delivery, duplicates, skill shape, integrity, safety) for any store; `snapshot`, `compare`, `track` |
| `eval/replay.py` | On the same conversations, what does each design write? | Replays real review files through both designs in sealed sandboxes and diffs the store |
| `eval/judge.py` | Which writes make future sessions better? | Blind pairwise LLM judgment on a fixed rubric, both A/B orders |

### 3.2 Replay method

- **Corpus**: 20 real review files from the production store, stratified by
  size and trigger (10 idle, 10 threshold; 3.5–68 KB).
- **Variants**: `old` = `main`, `new` = this branch. Each variant's scripts,
  `SKILL.md` and reviewer protocol come from its git ref.
- **Seed**: one frozen copy of the production store, **rolled back per case to
  the date of that review** (memories and reinforcements dated on or after it,
  skills created after it, and later sessions in the search index removed),
  then "deployed" with the variant's own migration.
- **Isolation**: store, skills dir, escalation rules and AGENTS.md targets
  point into the sandbox; OpenCode runs with `--pure` and a sandbox config
  (only the provider the model needs, the sandbox's own memory as
  instructions, a reviewer agent with skill loading and file editing denied
  and external directories limited to the sandbox and the variant source);
  live paths in the prompt are rewritten; runs are tagged so any write into
  the live store would be reported. Same model and the same 20-step budget as
  production.
- **Judge**: sees the conversation, what the store already contained before
  the review, and the two variants' writes labelled A/B; scores recall (new
  durable signals), precision, curation, correctness and skill handling;
  every case is judged in both orders and a winner is kept only if both
  orders agree (otherwise a tie).

### 3.3 Iterations — what the evaluation caught

1. **Pilot (3 cases)** validated the harness and caught a regression in the new
   reviewer prompt: it scanned only user messages, so a session whose only user
   message was a status question lost a root-caused bug and a process lesson
   the agent had established. The protocol now evaluates the agent's verified
   work as a second source.
2. **First 20-case run** (seed = today's store): new 12 wins, old 6, ties 2.
   Inspecting the old wins showed the new reviewer *had* seen the "missed"
   signals and skipped them because they were already stored — the seed held
   what production had recorded from these same conversations. The judge,
   seeing only writes, penalized correct deduplication. Fix: per-case time
   travel and a judge that sees what was already known.
3. **Corrected 20-case run**: the results in the summary table. Recall flipped
   in favour of the new design (2.98 → 4.33 vs old) once deduplication stopped
   being scored as a miss; the old design's misses are mostly reviews that ran
   out of steps before writing or logging.

### 3.4 Full results of the corrected run

| Metric | Old | New |
|---|---|---|
| Runs completed | 20/20 | 20/20 |
| Outcome logged | 4/20 | 19/20 |
| Hit the step limit | 19 | 0 |
| Steps · duration (s) · input tokens (mean) | 19.65 · 269.9 · 1 916 483 | 6.95 · 140.3 · 267 521 |
| Memories added (core / archive) | 17 (no layers) | 39 (3 / 36) |
| … over 400 chars | 8 | 12 |
| … with a code-state check | 0 | 2 |
| Memories strengthened / edited in place | 1 / 0 | 2 / 3 |
| Skills created | 6 | 2 |
| Skills modified directly (not mentioned in the conversation) | 2 (2) | 0 (0) |
| Skill bytes appended | 2 077 | 0 |
| Skill proposals staged | 0 | 3 |
| Writes refused by the CLI and resolved (duplicate / core full / too long) | 0 | 8 |
| Reviews recording nothing | 8 | 1 |
| Judge wins (both orders agreeing) | 2 | 16 (2 ties) |

The one new-design review without an outcome was a model failure: the model
spent its entire reasoning budget on an 84 KB review and stopped on the output
limit without acting. In production the runner now records such a run as
finished without an outcome; `reviewer_variant` can cap reasoning effort.

### 3.5 Threats to validity

- One judge model and 20 cases: strong directional evidence, not proof.
  Both-order agreement removes position bias but not the judge's own taste.
- Time travel cannot roll back edits to skill and wiki *content* (no history
  existed); that residue is identical for both variants.
- The protocol is inlined in the prompt instead of being loaded through the
  skill tool; identical for both variants.
- Production review files predate the new review header, so the replay's new
  reviewer never saw a "skills loaded" list or tool notes; in production it
  will, which should help skill proposals.
- The per-session archive slice and in-session skill editing are plugin-side
  behaviours that the reviewer replay does not exercise; they are covered by
  unit/integration tests and will show up in the production metrics.

## 4. Deployment and production measurement

1. Take a baseline: `eval/store_report.py snapshot --out <baseline.json>`.
2. Install from the branch (plugin files, skill, reviewer prompt, curator
   agent), then run `init`, `retention score` and `memory compose`; the last
   step migrates the registry into core and archive. Restart OpenCode so the
   new plugin hooks load.
3. Track weekly: `eval/store_report.py track --baseline <baseline.json>
   --since <deploy date> --out-dir <dir>` writes a dated comparison
   (completion rate by exact review id, lost/unlogged reviews, durations,
   injected share, duplicates, skill shape, archive slices served, proposals
   applied, refused/blocked writes, orphaned temp files).

Rollback: restore the backed-up store directory, plugin files, skill and
`opencode.json`; the new registry fields (`layer`, `check`, `edits`) are
ignored by the old code.

New configuration keys (all optional): `core_memory_chars`,
`core_user_chars`, `core_entry_max_chars`, `max_user_chars`,
`max_assistant_chars`, `max_pinned_skills`, `auto_apply_rewrites`,
`reviewer_model`, `reviewer_variant`, `curator_agent`,
`session_archive_slice`, `tool_notes`, `max_tool_notes`.
