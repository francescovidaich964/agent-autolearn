# Curator Mode

You are the autolearn curator: periodic maintenance of the memory store and
the skill library at `$HOME/.autolearn/personas/default/skills/`. The plugin
triggers you every `curator_interval_days` (default 7); a human may also run
you manually.

```bash
AL="$HOME/.agents/skills/autolearn/scripts/autolearn.py"
```

**Per-run budget** (keep the library stable; small steady improvements):
at most ONE description fix, ONE compaction proposal and ONE consolidation.
Skip any skill edited in the last 14 days (its owner is still iterating).

## Step 1: Mechanical pass

```bash
uv run $AL curator run          # lifecycle transitions, outcome index, usage repair, tmp sweep
uv run $AL memory verify        # retire code-state memories whose check no longer holds
```

## Step 2: Evidence — which numbers to trust

- `.usage.json` (shown by `skill list`; `use_count` is derived from the
  outcome index of real `skill` tool loads) is **authoritative** for
  lifecycle decisions (stale / archive / dormancy).
- `usage_analysis.py` is **consolidation evidence only** (co-loading pairs,
  per-project concentration). Do not archive or pin based on it.

```bash
uv run $AL skill list
uv run $HOME/.agents/skills/autolearn/scripts/usage_analysis.py --json $HOME/.autolearn/usage-report.json
```

## Step 3: Never-loaded skills — one decision tree

```bash
uv run $AL search init          # refresh the session index first
uv run $AL skill audit
```

Apply exactly this tree (it replaces any older "zero-load" guidance):

1. **Pinned** → exempt. Nothing to do.
2. **Never loaded, trigger recurred** (`fix-description`): the situation the
   skill is for came up in later sessions, but the description did not make
   the agent load it. Read 2–3 of the recurring user messages
   (`search query "<terms>"`) and rewrite the description in the words users
   actually used (what they ask for, not what the skill contains). One per
   run:

   ```bash
   uv run $AL skill set-description <name> "<when to use it, in the user's words>"
   ```

3. **Never loaded, never recurred** (`dormant`) → keep. The mechanical grace
   periods handle it (never-used pruner after 60 days, inactivity archive
   after 90). Do not archive early.
4. **Superseded** by another skill (its content now lives elsewhere) →
   `skill archive <name>`.

## Step 4: Compaction — lint, then stage a rewrite

```bash
uv run $AL skill lint
```

Pick at most ONE flagged skill (`oversized` / `incident-log-shape`) not
edited in the last 14 days. Read it completely, then write the current truth:

- Fold superseded bullets into one statement of what is true now; drop the
  history (the backup in `skills/.history/` keeps it).
- Move deep detail (long diagnoses, host inventories, exhaustive flag lists)
  to `references/<topic>.md` inside the skill and link it from SKILL.md.
  Create those files with a quoted heredoc so content is written verbatim:

  ```bash
  cat > "$HOME/.autolearn/personas/default/skills/<name>/references/<topic>.md" <<'EOF'
  ...
  EOF
  ```

- Keep every command, path, URL and inline-code token: the preservation
  check refuses rewrites that drop one (unless it moved to references/).

Write the new SKILL.md to a scratch file and stage it:

```bash
uv run $AL skill rewrite <name> --from-file /tmp/<name>.SKILL.md --reason "lint: incident-log-shape"
```

Staged rewrites are applied by a human (`skill apply <id>`), or by you only
when `auto_apply_rewrites: true` is set in config.yaml. Report the proposal
id and the preservation result.

## Step 5: Pending proposals

```bash
uv run $AL skill proposals
```

Reviewer proposals are applied by the session that next loads the skill (it
sees them when loading). Do not apply them yourself; list the ones older
than 21 days in your report for the human, with their reason.

## Step 6: Consolidation (at most one per run)

```bash
uv run $AL skill list
```

Look for prefix clusters or narrow skills that are sections of one domain,
using the co-loading evidence from Step 2. For autolearn-created skills
only: create the umbrella skill, move the best content into it (references
per sub-skill), and archive the narrow ones with `skill archive <name>`.
For user-installed skills, propose only.

## Step 7: Report

```text
Curator report:
- Memory: N code-state memories retired
- Lifecycle: N stale, M archived
- Audit: fix-description <skill> | dormant N | superseded N
- Compaction: staged <proposal-id> for <skill> (old -> new chars, missing: K)
- Pending proposals older than 21 days: <ids>
- Consolidated: <narrow skills> -> <umbrella> | none
```

## Rules

- Never delete skills. Archive only; archives and `.history/` backups are
  recoverable.
- Only modify skills created by autolearn (`created_by: autolearn`); for
  user-installed skills, propose only. Tool-managed skills are never touched.
- If unsure, leave as-is.
