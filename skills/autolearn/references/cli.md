# CLI Reference

Two scripts power every mode. Paths assume the standard install; both honor
`AUTOLEARN_HOME` for the store root.

```bash
AL="$HOME/.agents/skills/autolearn/scripts"
uv run $AL/autolearn.py <command>     # store, memory, wiki, skills, sync
uv run $AL/improve.py <command>       # behavioral rules + AGENTS.md escalation
```

## autolearn.py — store & memory

| Command | Purpose |
|---------|---------|
| `init` | Initialize the autolearn store |
| `memory add "<lesson>" [--archive] [--force] [--pin] [--core-only]` | Add a lesson. Core by default (hard-capped, injected every session); a full core redirects to the archive with a notice, `--core-only` refuses with exit 2; near-duplicates refuse |
| `memory add ... --check-path P --check-pattern R --check-expect present\|absent` | Code-state fact that retires itself when P stops matching |
| `memory search "<terms>" [--all-layers]` | BM25 search (archive by default) — use instead of `memory list` |
| `memory strengthen --id <id>` (or a unique keyword) | Reinforce an existing entry (same lesson seen again) |
| `memory edit <id> "<text>"` | Rewrite an entry in place (merge / shorten / supersede) |
| `memory demote <id>` / `memory promote <id>` | Move between core and archive (promote is cap-checked) |
| `memory remove --id <id>` (or a unique keyword; `--all` for every match) | Remove an entry |
| `memory verify [--dry-run]` | Retire code-state memories whose check no longer holds |
| `memory list [--layer core\|archive]` | List entries with ids, layers and core usage |
| `memory weaken "<keyword>"` / `memory strengths` | Reinforcement tools |
| `memory compose` | Regenerate `memory.context.md` (core) + `memory.archive.json` |
| `user add "<preference>" [--archive] [--force] [--core-only]` | Add a user-profile preference (`type="user"`, own core cap; a full core redirects to the archive) |
| `user list` / `user remove --id <id>` | List / remove profile entries |
| `retention score` | Recompute Ebbinghaus retention scores & tiers |
| `retention evict` | Evict memories past the cold grace period |
| `persona create/list/switch/archive/rename` | Manage knowledge personas |

## autolearn.py — skills & wiki

| Command | Purpose |
|---------|---------|
| `skill create <name> "<desc>" --patterns "<slugs>" [--verify-command C --verify-exit N]` | Create a skill (recurrence gate REQUIRED — reviewer.md Step 5) |
| `skill edit <name> --old "<exact>" --new "<text>" [--file F]` | Replace exactly one occurrence (in-session agents; refused for the reviewer) |
| `skill propose <name> --old "<exact>" --new "<text>" --reason "<evidence>"` | Stage an edit; surfaced to the next session that loads the skill |
| `skill rewrite <name> --from-file F --reason R` | Stage a full rewrite (preservation-checked) |
| `skill proposals [--all]` / `skill apply <id>` / `skill reject <id> --reason R` | Review staged proposals |
| `skill lint [name]` | oversized / incident-log-shape / references-sprawl |
| `skill audit` | Never-loaded skills: `fix-description` (trigger recurred) vs `dormant` |
| `skill set-description <name> "<desc>"` / `skill set-verify <name> "<cmd>" [--expect-exit N]` | Frontmatter-only edits |
| `skill pin <name> --reason R` / `skill unpin <name>` | Exempt from lifecycle rules (capped by `max_pinned_skills`) |
| `skill patch <name> "<section>" "<content>"` | Legacy: APPENDS a bullet (prefer `skill edit`; refused for the reviewer) |
| `skill archive <name>` | Archive a skill (never delete) |
| `skill list` / `skill usage` | List skills (size, pins, pending proposals) / show usage telemetry |
| `curator run` / `curator status` | Auto stale/archive transitions / state |
| `wiki compose` | Render `wiki/context.md` (reviewer pre-flight, MANDATORY) |
| `wiki read "<terms>"` | Lexically search the pattern index |
| `wiki show <slug>` | Print one pattern page |
| `wiki list` / `wiki init` / `wiki backfill-purposes` | Index / scaffold / backfill PURPOSE.md |
| `logs append "<summary>"` | Append a review summary to wiki/logs.md |
| `impact append` | Skill-impact ledger entry (harness/debug) |

## autolearn.py — search, logging, proposals, extras

| Command | Purpose |
|---------|---------|
| `search init` / `search query "<terms>"` / `search sessions "<terms>"` / `search status` | FTS5 search over past sessions |
| `log review-complete --observations N [--memory-updated] [--user-profile-updated] [--skills-created N] [--skills-proposed N] --topics "<t1,t2>"` | Log review outcome (joins the run via `AUTOLEARN_REVIEW_ID`) |
| `log review-complete --nothing --topics "<t1,t2>"` | Log an empty-handed review |
| `proposals recurrence "<terms>"` | Recurrence gate for skill creation |
| `proposals scan` | Long-horizon scan → verify → promote (scheduled tick) |
| `topics scan` / `topics candidates` | Recurring-preference (shift) detector |
| `outcomes init` / `outcomes status` | Tool-call outcome index (Certified Procedures spine) |
| `falsify run` / `falsify verdicts` | Verify skills against their claims |
| `sync login/logout/export-key/push/pull/status` | Cross-machine E2E-encrypted sync |
| `ui` | Launch the inspector UI |

## improve.py — behavioral rules

| Command | When | Purpose |
|---------|------|---------|
| `observe "<rule>" [--project P] [--domain D] [--context C] [--explicit-scope global\|local]` | After a correction/preference (observer Tier 1) | Record an observation |
| `status` | Session start | Show all rules and counts |
| `due` | Session end or `/improve` | Show pending escalations |
| `escalate --dry-run` | Escalation due | Preview what would be written where |
| `escalate --apply` | Escalation due | Write rules to the right AGENTS.md + confirm in one step |
| `confirm <id> --scope global\|local` | After a manual AGENTS.md write | Mark rule as written |
| `history <id>` | Debugging | Full observation history |
| `domains` | Reference | List the domain taxonomy |
| `stale [--days N]` | Periodic check | Rules not reinforced in N days (default 90) |
| `seed --from-agents-md` | First-time setup | Import rules from existing AGENTS.md |
| `init [--force]` | First-time setup | Initialize rules.yaml |

See `references/observer.md` for the escalation decision tree and AGENTS.md
writing protocol.
