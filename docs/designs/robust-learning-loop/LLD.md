# Robust Learning Loop - Low-Level Design

**Created**: 2026-10-13
**Branch**: `feat/robust-learning-loop`
**Design version tag**: `rl-2026.10` (stamped on observations so before/after windows separate)

## Why

Evidence from a production store (3 weeks, Windows, OpenCode v1, 2026-09-21 → 2026-10-13):

| Symptom | Measured | Root cause |
|---|---|---|
| Reviews lost | 293 `review_spawned`, 102 `review_complete`; ~50 spawns started while the previous review (median 322 s, p90 1002 s) was still running | wrapper busy-gate exits silently **after** the plugin already cleared its buffer; no-op reviews mostly never log |
| Learned memory invisible | 445 active memories, 7 injected | 3 000-char compose budget, retention-only ranking (`refresh_context()` passes an empty context) and an unbounded writer that never meets the budget |
| Skill accretion | top skills 15–32 KB, the most-patched skill: 24 patches / 0 recorded loads | `skill patch` can only **append** a bullet; the reviewer patches skills it never saw loaded |
| Lost / clobbered writes | 14 orphaned `*.tmp` next to `memories.jsonl` / `memory.context.md` | unlocked read-modify-write from concurrent reviewer/curator/compose processes; `os.replace` fails on Windows when the target is open |
| Stale facts injected | "popup fix NOT implemented" still injected after the fix merged | no invalidation for memories describing code state |
| No project scoping on Windows | `project: C:\Users\...` | `projectName()` splits on `/` only |
| Frozen telemetry | outcome index mark frozen; curator scan crashes on `session_v2` | `_set_mark` never committed; usage scan assumes the v2 schema |
| Persistent prompt-injection path | transcripts include web/tool output → reviewer → memory injected into every session | only secret redaction, no injection scan |

Design principle: **write less, deliver what is written, let the actor with the
evidence make the edit, and measure every change.**

## Components

### RL-OPS — Review pipeline observability and no-loss queue (Phase 0)

- RL-OPS-001: The wrapper SHALL append one JSON line per decision to
  `personas/default/review-runs.jsonl`: `skipped_duplicate`, `deferred`
  (busy or interval gate), `started`, `finished` (exit code, duration,
  step count, token totals, cost when the harness reports them),
  `drained`, `expired`.
- RL-OPS-002: A review blocked by the busy or interval gate SHALL be moved to
  `reviews/pending/` instead of being dropped. The gate holder drains up to
  `pending_drain_max` (default 3) pending reviews before releasing the gate.
  Pending reviews older than 24 h are expired (logged).
- RL-OPS-003: The plugin's peek throttle SHALL treat a fresh wrapper gate
  (`.review_gate` younger than 15 min) as busy, so the buffer is retained and
  rides the next trigger rather than being handed to a busy wrapper.
- RL-OPS-004: Every review run SHALL carry `AUTOLEARN_REVIEW_ID` (review file
  basename). `log review-complete` records it so runs and outcomes join
  exactly. A run that finishes without a matching `review_complete` is
  reported as `unlogged` by the metrics report.
- RL-OPS-005: The wrapper SHALL keep the harness JSON output next to the
  review (`<review>.run.jsonl`) so cost/steps can be recomputed offline; the
  stale-review cleaner removes both.
- RL-OPS-006: `outcomes._set_mark` SHALL commit. `usage_analysis` SHALL fall
  back to the v1 `session`/`part` tables when `session_v2` is absent.

### RL-INT — Store integrity (Phase 1)

- RL-INT-001: All store mutations (Python and JS) SHALL hold the cross-process
  store lock: an atomic `mkdir` of `personas/<p>/.store.lock/` with an owner
  file; stale after 120 s; re-entrant within a Python process.
- RL-INT-002: Atomic writes SHALL retry `os.replace` on `PermissionError`
  (Windows sharing violation) with backoff and remove their tmp file on final
  failure.
- RL-INT-003: Startup (plugin load, curator run) SHALL sweep `*.tmp` older
  than 1 h from the persona dir.
- RL-INT-004: `projectName()` SHALL return the basename for both `/` and `\`
  separated paths in every shell.
- RL-INT-005: Memory, user-profile, skill and proposal writes SHALL pass a
  content scan (invisible/bidi/tag Unicode, instruction-override phrases,
  exfiltration patterns, credential shapes). `block` findings reject the write
  (exit 3, observation `write_blocked`); `warn` findings are logged.

### RL-MEM — Two-tier memory (Phase 2)

- RL-MEM-001: Each registry record carries `layer: core | archive`. Core is
  hard-capped per type: `core_memory_chars` (default 2200) and
  `core_user_chars` (default 1300).
- RL-MEM-002: `memory add` / `user add` place new entries in core. If the cap
  would be exceeded the command fails (exit 2) and prints the core entries
  with ids and usage; the writer must `memory demote <id>`,
  `memory edit <id> "<shorter>"` or `memory remove` first, or add with
  `--archive`. Nothing is silently dropped.
- RL-MEM-003: Before adding, the CLI SHALL check near-duplicates (token
  Jaccard over topics ≥ `dedupe_threshold`, default 0.6, or containment
  ≥ 0.8) across core+archive; a match fails with the existing id and the
  suggested `memory strengthen --id <id>`. `--force` overrides.
- RL-MEM-004: `memory.context.md` (injected by the `instructions` entry for
  every harness) SHALL contain the core layer only (pinned first).
- RL-MEM-005: The v1 shell SHALL add a per-session, project-relevant slice of
  the archive through `experimental.chat.system.transform`, composed once per
  session from project name, directory segments, git remote and the first user
  message, then frozen for the session (prefix-cache friendly). Python writes
  `memory.archive.json` (records + ranking params) on every mutation; the JS
  ranking mirrors `composer.relevance`.
- RL-MEM-006: `memory search "<terms>"` exposes the archive on demand.
- RL-MEM-007: A memory MAY carry a declarative check
  `{path, pattern, expect: present|absent}` (no command execution).
  `memory verify` (run by the curator) retires memories whose check no longer
  holds (`status: retired`, `retired_reason`).
- RL-MEM-008: Migration is lazy and idempotent: records without `layer` are
  assigned by the previous composer ranking until the core caps fill; the rest
  go to archive.

### RL-SKL — Skills edited where the evidence is (Phase 3)

- RL-SKL-001: `skill edit <name> --old <exact> --new <text> [--file <rel>]`
  replaces exactly one occurrence (fails on 0 or >1 matches), backs up the
  previous file to `skills/.history/<name>/`, bumps `patch_count`, writes a
  ledger entry.
- RL-SKL-002: When `AUTOLEARN_REVIEWER=1`, `skill edit|patch|rewrite` SHALL
  refuse; the reviewer stages `skill propose <name> --old --new --reason`
  instead. In-session agents edit skills they loaded when they turn out wrong.
- RL-SKL-003: `skill lint` flags `oversized` (SKILL.md > `skill_max_chars`,
  default 12 000), `incident-log-shape` (≥ 3 supersede/dated-update markers)
  and `references-sprawl` (> 20 reference files).
- RL-SKL-004: `skill rewrite <name> --from-file <f>` stages a rewrite
  proposal; `skill apply <proposal-id>` applies it only if every backticked
  token, path and command line in the old version survives in the new
  SKILL.md or its references (preservation check), backing up the old file.
- RL-SKL-005: `skill pin <name> --reason "<why>"` (cap `max_pinned_skills`,
  default 5), `skill unpin`, pinned + reason shown in `skill list`.
- RL-SKL-006: `skill set-description <name> "<desc>"` edits the frontmatter
  only. `skill audit` classifies never-loaded skills (use_count 0, age ≥ 21 d,
  unpinned) by whether their trigger terms recur in the session index.
- RL-SKL-007: The v1 shell records skill loads and skill-file edits seen in
  `tool.execute.after` and exports `AUTOLEARN_SESSION_ID` via `shell.env`.

### RL-INP — Reviewer input and cost (Phase 4)

- RL-INP-001: `max_user_chars` (default 4000) and `max_assistant_chars`
  (default 2000) config keys.
- RL-INP-002: Tool notes: failed tool calls (tool, input summary ≤ 200 chars,
  error ≤ 300 chars) and skill loads are buffered as `Tool` entries (cap 20 per
  review); the review header lists skills loaded in the conversation.
- RL-INP-003: `reviewer_model` and `reviewer_steps` config keys are passed to
  the harness run.
- RL-INP-004: `skill set-verify <name> "<cmd>" [--expect-exit N]` writes the
  `verify:` frontmatter consumed by `falsify`.

## Evaluation (how we know it helped)

`eval/` holds two complementary instruments:

1. **Store health report** (`eval/store_report.py`) — deterministic metrics
   for any store: pipeline (spawned/started/finished/deferred/lost/unlogged,
   completion rate, duration, steps, tokens), memory (active, core usage,
   injected share, near-duplicate pairs, failing checks), skills (size
   distribution, accretion markers, patches per load, never-loaded, pins),
   integrity (orphan tmp, corrupt lines), safety findings in injected
   content. `snapshot` writes JSON; `compare` renders a before/after table.
   Snapshots of the live store before deployment and weekly after give the
   longitudinal comparison; observations are split by `design` tag.
2. **A/B replay** (`eval/replay.py`) — runs the old and new reviewer
   protocols over the same frozen corpus of real review files, each case in an
   isolated sandbox seeded from the same store snapshot (`AUTOLEARN_HOME`,
   `AGENTS_SKILLS_DIR`, `AGENT_IMPROVEMENT_HOME`, `AGENT_*_AGENTS_MD` all
   redirected; harness run with `--pure` and a replay agent with `skill` and
   `edit` denied, so no live state is touched). Per case it records the store
   diff (memories added/reinforced/demoted, core cap respected, duplicates
   introduced, skills created/edited/proposed, blocked writes), completion
   and run cost. `eval/judge.py` adds a blind pairwise LLM judgment of which
   variant captured the conversation's real signals with less noise.
   `eval/compare.py` aggregates both into one report.
