/**
 * Autolearn Plugin — shared core for all harness adapters.
 *
 * All harness-independent logic lives here: store management, config,
 * redaction, memory composition, sync, the review wrapper script, review
 * formatting, throttles, and detached subprocess spawning. The enforced
 * methodology (review every N user messages, idle + exit reviews, one
 * skill change per review, multi-layer throttle) is defined HERE so every
 * harness adapter gets identical behavior.
 *
 * Consumers (shells — each ~300 lines of harness-specific event plumbing):
 *   plugin/autolearn.js     — OpenCode v1 shell (function default export)
 *   plugin/autolearn-v2.js  — OpenCode v2 shell (plain-object plugin)
 *   plugin/autolearn-pi.ts  — pi shell (ExtensionAPI)
 *
 * No imports outside Node/Bun builtins so the module resolves under both
 * plugin loaders without a package.json or node_modules.
 *
 * Environment variables:
 *   AUTOLEARN_HOME     - Base directory (default: ~/.autolearn)
 *   AUTOLEARN_DISABLED - Set to "1" to disable
 *   AUTOLEARN_DEBUG    - Set to "1" for debug logging
 */

import {
  appendFileSync,
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "fs"
import { spawn as nodeSpawn } from "child_process"
import { homedir } from "os"
import { join } from "path"

export const AL_HOME = process.env.AUTOLEARN_HOME || join(homedir(), ".autolearn")
export const PERSONAS_DIR = join(AL_HOME, "personas")
export const DEFAULT_PERSONA_DIR = join(PERSONAS_DIR, "default")
export const CONFIG_FILE = join(DEFAULT_PERSONA_DIR, "config.yaml")
export const MEMORY_FILE = join(DEFAULT_PERSONA_DIR, "memory.context.md")
export const LEGACY_MEMORY_FILE = join(DEFAULT_PERSONA_DIR, "memory.md")
export const USER_FILE = join(DEFAULT_PERSONA_DIR, "user-profile.md")
export const OBS_FILE = join(DEFAULT_PERSONA_DIR, "observations.jsonl")
export const BIN_DIR = join(DEFAULT_PERSONA_DIR, "bin")
export const REVIEWS_DIR = join(DEFAULT_PERSONA_DIR, "reviews")
export const SKILLS_DIR = join(DEFAULT_PERSONA_DIR, "skills")
export const ARCHIVE_DIR = join(SKILLS_DIR, ".archive")
export const WRAPPER_SCRIPT = join(BIN_DIR, "review-runner.sh")
// win32 hidden spawn launcher (issue #32): wscript runs the command line
// carried in SPAWN_CMD_ENV with a hidden window, preserving detached
// semantics (env values are not parsed, so quoted paths survive exactly).
export const LAUNCHER_SCRIPT = join(BIN_DIR, "spawn-hidden.vbs")
export const SPAWN_CMD_ENV = "AUTOLEARN_SPAWN_CMD"
// Activity-coupled curator trigger (issue #23). The state file is written by
// the Python CLI (`curator run` saves last_run on EVERY run, including
// no-transition runs); the JS side only reads it.
export const CURATOR_STATE_FILE = join(DEFAULT_PERSONA_DIR, ".curator_state.json")
// Machine-wide (like THROTTLE_FILE): written at spawn time so a curator run
// that dies before saving state cannot trigger a retry storm on every
// subsequent review.
export const CURATOR_COOLDOWN_FILE = join(AL_HOME, ".curator_cooldown")
export const CURATOR_COOLDOWN_MS = 3600000 // 1h
export const SYNC_CONFIG_FILE = join(AL_HOME, "sync.yaml")
export const SALT_FILE = join(AL_HOME, ".encryption_salt")
// Skills consolidated into a single `autolearn` skill (scripts/ moved from
// autolearn-reviewer). Resolve the new path, fall back to the legacy one for
// installs that predate the consolidation.
function resolveCli() {
  // AUTOLEARN_CLI pins the CLI (tests, A/B replays of a working tree).
  if (process.env.AUTOLEARN_CLI) return process.env.AUTOLEARN_CLI
  const fresh = join(homedir(), ".agents", "skills", "autolearn", "scripts", "autolearn.py")
  try { if (existsSync(fresh)) return fresh } catch {}
  return join(homedir(), ".agents", "skills", "autolearn-reviewer", "scripts", "autolearn.py")
}
export const AUTOLEARN_CLI = resolveCli()
export const THRESHOLD_DEFAULT = 5 // in USER messages (exchanges), not assistant turns
export const STALE_DAYS_DEFAULT = 30
export const IDLE_COOLDOWN_MS = 300000
export const MIN_INTERVAL_DEFAULT_MS = 180000 // 3 min between review starts, global (serializes bursts; dedupe handles same-content)
export const MAX_REVIEWS_PER_DAY_DEFAULT = 60 // hard ceiling across all projects (~2.5/hr sustained)
export const REVIEW_HEADING = "# Autolearn Review"
// Stamped on observations and review files so metrics can split the
// before/after windows of a design change (eval/store_report.py).
export const DESIGN_VERSION = "rl-2026.10"
export const DEBUG = process.env.AUTOLEARN_DEBUG === "1"
export const DBG_FILE = join(AL_HOME, "debug.log")

export function dbg(...args) {
  if (!DEBUG) return
  const msg = args.map(a => typeof a === "string" ? a : JSON.stringify(a)).join(" ")
  try { appendFileSync(DBG_FILE, `[${new Date().toISOString()}] ${msg}\n`) } catch {}
}

// @spec CM-TC-006
export const SECRET_RE =
  /(api[_-]?key|token|secret|password|authorization|credentials?|auth)(["\s:=]+)([A-Za-z]+\s+)?([A-Za-z0-9_\-/.+=]{8,})/gi

// @spec CM-TC-006
export function redact(str) {
  if (!str) return str
  return str.replace(SECRET_RE, "$1$2$3[REDACTED]")
}

// ---------------------------------------------------------------------------
// Cross-process store lock (RL-INT-001). Same protocol as storelock.py: an
// atomic mkdir of <persona>/.store.lock/, broken when older than 120 s. The
// plugin runs inside the harness process, so it never waits long: after
// `timeoutMs` it logs and proceeds unlocked rather than stall the host.
export const STORE_LOCK_DIR = join(DEFAULT_PERSONA_DIR, ".store.lock")
export const STORE_LOCK_STALE_MS = 120000

function sleepSync(ms) {
  try { Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms) } catch {}
}

export function withStoreLock(fn, { timeoutMs = 2000, lockDir = STORE_LOCK_DIR } = {}) {
  const deadline = Date.now() + timeoutMs
  let delay = 5
  let held = false
  for (;;) {
    try {
      mkdirSync(lockDir)
      held = true
      break
    } catch (err) {
      if (err.code !== "EEXIST" && err.code !== "EPERM") {
        if (err.code === "ENOENT") { try { mkdirSync(join(lockDir, ".."), { recursive: true }) } catch {} ; continue }
        break
      }
      try {
        if (Date.now() - statSync(lockDir).mtimeMs > STORE_LOCK_STALE_MS) {
          const grave = `${lockDir}.stale.${process.pid}.${Date.now()}`
          try { renameSync(lockDir, grave); rmSync(grave, { recursive: true, force: true }) } catch {}
          continue
        }
      } catch {}
      if (Date.now() >= deadline) { dbg("store lock busy, proceeding unlocked"); break }
      sleepSync(delay)
      delay = Math.min(delay * 2, 100)
    }
  }
  if (held) {
    try { writeFileSync(join(lockDir, "owner"), `${process.pid} ${Math.floor(Date.now() / 1000)} js\n`) } catch {}
  }
  try {
    return fn()
  } finally {
    if (held) { try { rmSync(lockDir, { recursive: true, force: true }) } catch {} }
  }
}

// RL-INT-003: writers killed between write and rename leave pid-suffixed
// *.tmp files; sweep the ones older than an hour on every plugin load.
export function sweepTmp(dirs = [DEFAULT_PERSONA_DIR, SKILLS_DIR], maxAgeMs = 3600000) {
  const removed = []
  const now = Date.now()
  for (const dir of dirs) {
    let names = []
    try { names = readdirSync(dir) } catch { continue }
    for (const f of names) {
      if (!f.endsWith(".tmp")) continue
      const p = join(dir, f)
      try {
        if (now - statSync(p).mtimeMs > maxAgeMs) { unlinkSync(p); removed.push(f) }
      } catch {}
    }
  }
  if (removed.length) dbg("SWEPT orphaned tmp files", removed)
  return removed
}

// RL-INT-004: project label from a working directory on any platform. The
// shells used `.split("/")`, which returns the whole path on Windows and
// broke every per-project scope.
export function projectLabel(dir) {
  const parts = String(dir || "").replace(/[\\/]+$/, "").split(/[\\/]/)
  return parts.pop() || "unknown"
}

// @spec KS-MEM-001 (ensures directory tree exists before any operation)
export function ensureStore() {
  migrateToPersonas()
  mkdirSync(DEFAULT_PERSONA_DIR, { recursive: true })
  mkdirSync(BIN_DIR, { recursive: true })
  mkdirSync(SKILLS_DIR, { recursive: true })
  mkdirSync(ARCHIVE_DIR, { recursive: true })
  mkdirSync(REVIEWS_DIR, { recursive: true })
  if (!existsSync(MEMORY_FILE)) {
    writeFileSync(MEMORY_FILE, "# Autolearn Memory\n\n<!-- Managed by autolearn. -->\n\n")
  }
  if (!existsSync(USER_FILE)) {
    writeFileSync(USER_FILE, "# User Profile\n\n<!-- Managed by autolearn. -->\n\n")
  }
  if (!existsSync(CONFIG_FILE)) {
    writeFileSync(CONFIG_FILE, `review_threshold: ${THRESHOLD_DEFAULT}\nsession_review_on_idle: true\nmax_conversation_buffer: 50\nmin_interval_ms: ${MIN_INTERVAL_DEFAULT_MS}\ncurator_interval_days: 7\nstale_after_days: 30\narchive_after_days: 90\n`)
  }
  ensureWrapper()
  ensureLauncher()
  sweepTmp()
}

// Phase 3 migration: move flat ~/.autolearn/ files to personas/default/
function migrateToPersonas() {
  if (existsSync(PERSONAS_DIR)) return
  const flatFiles = ["memory.md", "user-profile.md", "config.yaml", "observations.jsonl", "strengths.json", ".curator_state.json"]
  const hasFlat = flatFiles.some(f => { try { return existsSync(join(AL_HOME, f)) } catch { return false } })
  const hasSkills = existsSync(join(AL_HOME, "skills"))
  if (!hasFlat && !hasSkills) return

  mkdirSync(DEFAULT_PERSONA_DIR, { recursive: true })
  for (const f of flatFiles) {
    const src = join(AL_HOME, f)
    try {
      if (existsSync(src) && !statSync(src).isDirectory()) {
        renameSync(src, join(DEFAULT_PERSONA_DIR, f))
      }
    } catch {}
  }
  for (const d of ["skills", "reviews", "bin"]) {
    const srcDir = join(AL_HOME, d)
    try {
      if (existsSync(srcDir) && statSync(srcDir).isDirectory() && !existsSync(join(DEFAULT_PERSONA_DIR, d))) {
        renameSync(srcDir, join(DEFAULT_PERSONA_DIR, d))
      }
    } catch {}
  }
  dbg("MIGRATED flat layout to", DEFAULT_PERSONA_DIR)
}

// @spec SYNC-PROTO-012, SYNC-PROTO-013
export function syncBackground(command) {
  if (!process.env.AUTOLEARN_SYNC_API_KEY) return
  if (!existsSync(SYNC_CONFIG_FILE)) return
  if (!existsSync(SALT_FILE)) return
  if (!existsSync(AUTOLEARN_CLI)) return

  // Honor sync_on_start / sync_after_review config flags
  try {
    const syncYaml = readFileSync(SYNC_CONFIG_FILE, "utf-8")
    if (command === "pull" && /sync_on_start:\s*false/.test(syncYaml)) return
    if (command === "push" && /sync_after_review:\s*false/.test(syncYaml)) return
  } catch {}

  try {
    spawnDetached(["uv", "run", AUTOLEARN_CLI, "sync", command], { cwd: process.cwd() })
    dbg(`SYNC ${command} spawned in background`)
  } catch (err) {
    dbg(`SYNC ${command} failed to spawn:`, err.message)
  }
}

// Detached subprocess spawn that works under both OpenCode v1 (Bun runtime)
// and v2 (Bun-implemented Node compat). Bun.spawn is preferred where
// available to preserve v1 behavior exactly; child_process elsewhere.
// Pass { unref: true } for fire-and-forget children that must not keep the
// host's event loop alive (e.g. memory compose).
export function spawnDetached(cmd, opts = {}) {
  const { unref, ...spawnOpts } = opts
  let argv = cmd
  let env = spawnOpts.env || { ...process.env }
  // win32: `detached: true` (DETACHED_PROCESS) makes the child auto-allocate
  // a VISIBLE console window, and windowsHide cannot stop it (CREATE_NO_WINDOW
  // is ignored when DETACHED_PROCESS is set — issue #32). Route the spawn
  // through a hidden launcher instead; fall back to the direct spawn (with a
  // window) if the launcher cannot be installed.
  if (process.platform === "win32") {
    try {
      // Self-heal: ensureStore() writes the launcher at plugin load, but a
      // spawn must never silently no-op when it is missing (stale install,
      // manual delete, first spawn before load).
      if (!existsSync(LAUNCHER_SCRIPT)) ensureLauncher()
      if (!existsSync(LAUNCHER_SCRIPT)) throw new Error("launcher unavailable: " + LAUNCHER_SCRIPT)
      // The command line travels in an env var: values are not parsed, so
      // quoted paths survive exactly (wscript argv cannot round-trip them)
      // and concurrent spawns cannot collide.
      env = { ...env, [SPAWN_CMD_ENV]: winCommandLine(cmd) }
      argv = hiddenSpawnCommand()
    } catch (err) {
      dbg("hidden spawn setup failed, spawning directly:", err.message)
    }
  }
  if (typeof Bun !== "undefined" && typeof Bun.spawn === "function") {
    const proc = Bun.spawn(argv, {
      stdout: "ignore",
      stderr: "ignore",
      stdin: "ignore",
      detached: true,
      ...spawnOpts,
      env,
    })
    try { unref ? proc.unref() : proc.ref() } catch {}
    return proc
  }
  const proc = nodeSpawn(argv[0], argv.slice(1), {
    stdio: "ignore",
    detached: true,
    ...spawnOpts,
    env,
  })
  try { proc.unref() } catch {}
  return proc
}

// Windows shim: the review wrapper is a POSIX sh script, which Bun.spawn
// cannot execute directly on win32. Run it via Git Bash instead.
function findGitBash() {
  const candidates = [
    process.env.OPENCODE_GIT_BASH_PATH,
    "C:/Program Files/Git/bin/bash.exe",
    "C:/Program Files (x86)/Git/bin/bash.exe",
  ].filter(Boolean)
  for (const c of candidates) {
    try { if (existsSync(c)) return c } catch {}
  }
  return "bash"
}

// The wrapper is a POSIX sh script; win32 cannot exec it directly, so route
// it through Git Bash. Returns the argv to spawn for the given platform.
export function wrapperCommand(args, platform = process.platform) {
  return platform === "win32" ? [findGitBash(), WRAPPER_SCRIPT, ...args] : [WRAPPER_SCRIPT, ...args]
}

// The wrapper is harness-aware: it runs the review under the binary named by
// AUTOLEARN_HARNESS_BIN (set by each shell: v1 pins `opencode`, v2 pins
// `opencode2`, the pi shell pins `pi`), falling back to
// AUTOLEARN_OPENCODE_BIN (legacy name, still honored), then to detection.
// pi runs reviews as one-shot print-mode processes with --no-session, so no
// session cleanup is needed; opencode keeps the run/delete dance (v2 via the
// HTTP API, which has no `session delete` CLI subcommand).
const WRAPPER_CONTENT = `#!/bin/sh
# Autolearn review runner - runs an opencode review, deletes the session,
# then pushes the updated store via sync (if configured).
# Works with OpenCode v1 (opencode) and v2 beta (opencode2).
# Args: $1 = path to the review markdown file (streamed to the harness on
# stdin); remaining args pass through to \`<binary> run --format json\`.
#
# Wrapper-side throttle (defense in depth): plugin instances already in
# memory predate the in-plugin throttle and keep calling this script, so
# gate HERE as well. Three gates, keyed on wrapper-owned state that is
# independent of the plugin's .last_review_lock (so the two layers never
# cancel each other out):
#   1. one review executing at a time (atomic mkdir claim)
#   2. min_interval_ms between review STARTS (default 3 min, config-overridable)
#   3. an identical conversation section never runs twice
# ensureWrapper() rewrites this file on every plugin load, so a manual triage
# edit to the wrapper no longer silently reverts (2026-09-04: the un-gated
# wrapper was restored by a fresh plugin load mid-incident and the fleet
# respawned).
AL="\${AUTOLEARN_HOME:-\$HOME/.autolearn}"
GATE="\$AL/.review_gate"
LOCK="\$AL/.last_wrapper_review"
# Default 3 min: serialize bursts machine-wide without starving cadence.
# (The in-plugin dedupe gate handles the same-conversation case.)
MIN_INTERVAL_MS=180000
CFG="\$AL/personas/default/config.yaml"
if [ -f "\$CFG" ]; then
  MI=\$(sed -n 's/^min_interval_ms:[[:space:]]*//p' "\$CFG" | head -1 | tr -d '[:space:]')
  case "\$MI" in ''|*[!0-9]*) ;; *) MIN_INTERVAL="\$MI" ;; esac
fi
# Convert ms -> seconds for the shell arithmetic.
MIN_INTERVAL_S=\$(( \${MIN_INTERVAL:-\$MIN_INTERVAL_MS} / 1000 ))
NOW=\$(date +%s)
# ---- Curator mode (activity-coupled trigger, issue #23) ----
# Invoked as: review-runner.sh --curator
# MUST run before the review gates below: it has its OWN single-flight gate
# and never touches GATE/LOCK, so a curator run neither consumes a review
# interval nor serializes against reviews. Stale reclaim is 1h (a
# consolidation session can legitimately run long) vs the review gate's 15m.
if [ "\$1" = "--curator" ]; then
  CGATE="\$AL/.curator_gate"
  if ! mkdir "\$CGATE" 2>/dev/null; then
    CGAGE=\$(cat "\$CGATE/ts" 2>/dev/null || echo 0)
    case "\$CGAGE" in ''|*[!0-9]*) CGAGE=0 ;; esac
    if [ \$((NOW - CGAGE)) -gt 3600 ]; then
      rm -rf "\$CGATE"
      mkdir "\$CGATE" 2>/dev/null || exit 0
    else
      exit 0
    fi
  fi
  printf '%s\\n' "\$NOW" > "\$CGATE/ts" 2>/dev/null
  trap 'rm -rf "\$CGATE" 2>/dev/null' EXIT
  OC="\${AUTOLEARN_HARNESS_BIN:-\${AUTOLEARN_OPENCODE_BIN:-}}"
  if [ -z "\$OC" ]; then
    if command -v pi >/dev/null 2>&1; then OC=pi
    elif command -v opencode2 >/dev/null 2>&1; then OC=opencode2
    else OC=opencode; fi
  fi
  CPROMPT="Load the autolearn skill and follow references/curator.md to run the curator."
  if [ "\$(basename "\$OC")" = "pi" ]; then
    printf '%s' "\$CPROMPT" | "\$OC" -p --no-session -na >/dev/null 2>&1
  else
    COUT=\$(mktemp "\${TMPDIR:-/tmp}/alcurator.XXXXXX")
    "\$OC" run --format json --agent "\${AUTOLEARN_CURATOR_AGENT:-autolearn-reviewer}" "\$CPROMPT" > "\$COUT" 2>/dev/null
    CSID=\$(sed -n 's/.*"sessionID"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p' "\$COUT" | head -1)
    rm -f "\$COUT"
    if [ -n "\$CSID" ]; then
      case "\$(basename "\$OC")" in
        opencode2) "\$OC" api delete "/api/session/\$CSID" >/dev/null 2>&1 ;;
        *)         "\$OC" session delete "\$CSID" >/dev/null 2>&1 ;;
      esac
    fi
  fi
  # Same aftercare as the review path: push post-curator state when sync is
  # configured; stay silent otherwise.
  AL_CLI="\$HOME/.agents/skills/autolearn/scripts/autolearn.py"
  [ -f "\$AL_CLI" ] || AL_CLI="\$HOME/.agents/skills/autolearn-reviewer/scripts/autolearn.py"
  if [ -n "\${AUTOLEARN_SYNC_API_KEY}" ] && [ -f "\${HOME}/.autolearn/.encryption_salt" ] && [ -f "\$AL_CLI" ]; then
    uv run "\$AL_CLI" sync push >/dev/null 2>&1
  fi
  exit 0
fi
# $1 is the review markdown PATH (not its content): the file is streamed to
# the harness on stdin, keeping the command line small. Windows caps a
# process command line at 32,767 chars; content-as-argv broke reviews over
# ~32 KB with ENAMETOOLONG (issue #21).
REVIEW_FILE="\$1"
shift
PDIR="\$AL/personas/default"
REVIEWS="\$PDIR/reviews"
PENDING="\$REVIEWS/pending"
RUNLOG="\$PDIR/review-runs.jsonl"
DRAIN_MAX=3
PENDING_MAX_AGE_S=259200
# One JSON line per wrapper decision (RL-OPS-001). Every value is a plain
# token (review basename, integer, bareword), so no JSON escaping is needed;
# extra fields are passed pre-formatted in \$3 using sh quote concatenation
# (no backslashes: this script lives inside a JS template literal).
runlog() {
  printf '{"ts":%s,"event":"%s","review":"%s","design":"${DESIGN_VERSION}"%s}\\n' "\$(date +%s)" "\$1" "\$(basename "\$2" .md)" "\$3" >> "\$RUNLOG" 2>/dev/null
}
# A review the gates cannot run NOW is queued, never dropped (RL-OPS-002):
# the plugin cleared its buffer when it handed us this file, so exiting here
# would lose the conversation. The current gate holder drains the queue.
defer() {
  mkdir -p "\$PENDING" 2>/dev/null
  if mv "\$REVIEW_FILE" "\$PENDING/" 2>/dev/null; then
    runlog deferred "\$REVIEW_FILE" ',"reason":"'"\$1"'"'
  else
    runlog lost "\$REVIEW_FILE" ',"reason":"'"\$1"'"'
  fi
  exit 0
}
# Gate 3 first (cheap, no state change): identical conversation → skip.
CONV=\$(sed -n '/## Conversation/,\$p' "\$REVIEW_FILE" 2>/dev/null)
HASH=""
if [ -n "\$CONV" ]; then
  HASH=\$(printf '%s' "\$CONV" | md5 -q 2>/dev/null || printf '%s' "\$CONV" | md5sum | cut -d' ' -f1)
  OLDHASH=\$(cut -d: -f2 "\$LOCK" 2>/dev/null)
  if [ -n "\$HASH" ] && [ -n "\$OLDHASH" ] && [ "\$HASH" = "\$OLDHASH" ]; then
    runlog skipped_duplicate "\$REVIEW_FILE" ""
    exit 0
  fi
fi
# Gate 2: start-to-start spacing (seconds). Too soon → queue, not drop.
LAST=\$(cut -d: -f1 "\$LOCK" 2>/dev/null)
case "\$LAST" in ''|*[!0-9]*) LAST=0 ;; esac
if [ \$(( NOW - LAST )) -lt "\$MIN_INTERVAL_S" ]; then
  defer interval
fi
# Gate 1: atomic claim. Losers queue their review; a gate older than 15 min
# is stale (crashed run) and gets reclaimed. The holder refreshes ts before
# each run it drains, so a long drain is never mistaken for a crash.
if ! mkdir "\$GATE" 2>/dev/null; then
  GAGE=\$(cat "\$GATE/ts" 2>/dev/null || echo 0)
  case "\$GAGE" in ''|*[!0-9]*) GAGE=0 ;; esac
  if [ \$((NOW - GAGE)) -gt 900 ]; then
    rm -rf "\$GATE"
    mkdir "\$GATE" 2>/dev/null || defer busy
  else
    defer busy
  fi
fi
printf '%s\\n' "\$NOW" > "\$GATE/ts" 2>/dev/null
trap 'rm -rf "\$GATE" 2>/dev/null' EXIT
# Record start BEFORE running so a killed review still consumes the interval
# (fail-safe: a broken binary must not cause an endless retry loop).
printf '%s:%s\\n' "\$NOW" "\$HASH" > "\$LOCK" 2>/dev/null
OC="\${AUTOLEARN_HARNESS_BIN:-\${AUTOLEARN_OPENCODE_BIN:-}}"
if [ -z "\$OC" ]; then
  if command -v pi >/dev/null 2>&1; then OC=pi
  elif command -v opencode2 >/dev/null 2>&1; then OC=opencode2
  else OC=opencode; fi
fi
# run_review FILE [harness args...]: run one review and keep its per-step
# accounting (<review>.run.jsonl) and stderr tail (<review>.run.err) next to
# the review so completion, steps and cost are measurable offline
# (RL-OPS-005). AUTOLEARN_REVIEW_ID reaches the reviewer's shell, so its
# "log review-complete" joins this run exactly (RL-OPS-004).
run_review() {
  RF="\$1"
  shift
  BASE="\${RF%.md}"
  RID=\$(basename "\$BASE")
  T0=\$(date +%s)
  printf '%s\\n' "\$T0" > "\$GATE/ts" 2>/dev/null
  runlog started "\$RF" ""
  SID=""
  if [ "\$(basename "\$OC")" = "pi" ]; then
    # pi branch: one-shot print-mode review, ephemeral (--no-session),
    # project-local resources ignored (-na), review md piped on stdin.
    AUTOLEARN_REVIEW_ID="\$RID" "\$OC" -p --no-session -na < "\$RF" >/dev/null 2> "\$BASE.run.err"
    RC=\$?
  else
    OUT=\$(mktemp "\${TMPDIR:-/tmp}/alreview.XXXXXX")
    AUTOLEARN_REVIEW_ID="\$RID" "\$OC" run --format json "\$@" < "\$RF" > "\$OUT" 2> "\$BASE.run.err"
    RC=\$?
    # BRE backslashes below are DOUBLED (\\\\, \\1) because this script lives
    # inside a JS template literal — single backslashes get eaten by the escape
    # evaluation (\\( -> ( , \\1 -> 0x01 control char under Bun) and the sed
    # silently extracts nothing (issue #15).
    # Output contract (verified against opencode v1.0.142..v1.18.31 and v2.0.3):
    # both binaries emit "run --format json" as JSONL with a top-level
    # "sessionID" on every line, so line-by-line sed extraction is safe.
    SID=\$(sed -n 's/.*"sessionID"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p' "\$OUT" | head -1)
    # Keep only step accounting and errors: full tool output would add
    # megabytes per review to the store.
    grep -E '"type" *: *"(step_finish|step-finish|error)"' "\$OUT" > "\$BASE.run.jsonl" 2>/dev/null
    rm -f "\$OUT"
    if [ -n "\$SID" ]; then
      case "\$(basename "\$OC")" in
        opencode2) "\$OC" api delete "/api/session/\$SID" >/dev/null 2>&1 ;;
        *)         "\$OC" session delete "\$SID" >/dev/null 2>&1 ;;
      esac
    fi
  fi
  if [ -s "\$BASE.run.err" ]; then
    tail -c 4000 "\$BASE.run.err" > "\$BASE.run.err.t" 2>/dev/null && mv "\$BASE.run.err.t" "\$BASE.run.err" 2>/dev/null
  else
    rm -f "\$BASE.run.err"
  fi
  HAS_SID=false
  [ -n "\$SID" ] && HAS_SID=true
  runlog finished "\$RF" ',"rc":'"\$RC"',"duration_s":'"\$(( \$(date +%s) - T0 ))"',"session":'"\$HAS_SID"
}
run_review "\$REVIEW_FILE" "\$@"
# Drain reviews other wrappers queued while we held the gate (RL-OPS-002).
DRAINED=0
for P in "\$PENDING"/*.md; do
  [ -f "\$P" ] || continue
  [ "\$DRAINED" -lt "\$DRAIN_MAX" ] || break
  N=\$(basename "\$P" .md)
  TSMS=\${N##*-}
  case "\$TSMS" in ''|*[!0-9]*) TSMS=0 ;; esac
  if [ \$(( \$(date +%s) - TSMS / 1000 )) -gt "\$PENDING_MAX_AGE_S" ]; then
    rm -f "\$P"
    runlog expired "\$P" ""
    continue
  fi
  mv "\$P" "\$REVIEWS/" 2>/dev/null || continue
  runlog drained "\$P" ""
  run_review "\$REVIEWS/\$N.md" "\$@"
  DRAINED=\$((DRAINED + 1))
done
# Push after review completes so reviewer-written changes are included.
# Stays silent when sync isn't configured (no API key or no salt).
AL_CLI="\$HOME/.agents/skills/autolearn/scripts/autolearn.py"
[ -f "\$AL_CLI" ] || AL_CLI="\$HOME/.agents/skills/autolearn-reviewer/scripts/autolearn.py"
if [ -n "\${AUTOLEARN_SYNC_API_KEY}" ] && [ -f "\$HOME/.autolearn/.encryption_salt" ] && [ -f "\$AL_CLI" ]; then
  uv run "\$AL_CLI" sync push >/dev/null 2>&1
fi
`

function ensureWrapper() {
  try {
    // The wrapper is the last line of defense for plugin instances still in
    // memory from before an upgrade (their OLD ensureWrapper would otherwise
    // restore an un-gated wrapper — observed 2026-09-04). Keeping the file
    // read-only between updates makes stale instances' writes fail silently
    // while fresh code chmods, writes, and re-locks it.
    try { chmodSync(WRAPPER_SCRIPT, 0o644) } catch {}
    writeFileSync(WRAPPER_SCRIPT, WRAPPER_CONTENT)
    chmodSync(WRAPPER_SCRIPT, 0o544) // r-x: executable, NOT writable
  } catch (err) {
    dbg("ensureWrapper failed:", err.message)
  }
}

// ---------------------------------------------------------------------------
// win32 hidden spawn launcher (issue #32).
//
// libuv maps `detached: true` to DETACHED_PROCESS, so a console app starts
// with no console and auto-allocates a VISIBLE one on first console API use.
// windowsHide cannot prevent it: CREATE_NO_WINDOW is ignored when
// DETACHED_PROCESS is set. The only reliable way to keep detached spawns
// windowless is a GUI-subsystem launcher: wscript.exe (no console of its
// own) runs the command line with WshShell.Run(..., 0, False) — SW_HIDE,
// async — and the child keeps detached survival semantics.
const LAUNCHER_CONTENT = `Option Explicit
' Autolearn hidden spawn launcher (win32).
' Runs the command line from the ${SPAWN_CMD_ENV} environment variable with
' a hidden window (style 0), without waiting. wscript.exe is a GUI-subsystem
' host (no console of its own); the cmd /c wrap with stdin from NUL keeps the
' old stdio:"ignore" semantics for the child (a console stdin would hang
' stdin-reading children). See plugin/autolearn-core.mjs (issue #32).
Dim sh, cmdLine
Set sh = CreateObject("WScript.Shell")
cmdLine = sh.Environment("PROCESS")("${SPAWN_CMD_ENV}")
sh.Run "cmd /c """ & cmdLine & " <nul""", 0, False
`

export function ensureLauncher() {
  if (process.platform !== "win32") return
  try {
    // Same read-only dance as ensureWrapper: a stale in-memory plugin must
    // not be able to restore an old launcher over a fresh one.
    try { chmodSync(LAUNCHER_SCRIPT, 0o644) } catch {}
    writeFileSync(LAUNCHER_SCRIPT, LAUNCHER_CONTENT)
    chmodSync(LAUNCHER_SCRIPT, 0o544)
  } catch (err) {
    dbg("ensureLauncher failed:", err.message)
  }
}

// Windows command-line quoting (the CRT rule set): quote when the argument
// contains whitespace or quotes; escape embedded quotes with backslashes;
// double trailing backslashes before a closing quote. Port of Python's
// subprocess.list2cmdline, which follows the same rules.
export function winCommandLine(args) {
  let out = ""
  for (const arg of args) {
    if (out) out += " "
    if (arg === "" || /[\s"]/.test(arg)) {
      out += '"'
      let backslashes = 0
      for (const ch of arg) {
        if (ch === "\\") { backslashes++; out += ch; continue }
        if (ch === '"') { out += "\\".repeat(backslashes + 1) + '"'; backslashes = 0; continue }
        backslashes = 0
        out += ch
      }
      out += "\\".repeat(backslashes) + '"'
    } else {
      out += arg
    }
  }
  return out
}

function wscriptPath() {
  const root = process.env.SystemRoot || "C:\\Windows"
  const p = join(root, "System32", "wscript.exe")
  try { if (existsSync(p)) return p } catch {}
  return "wscript.exe"
}

// The argv to spawn on win32: wscript runs the hidden launcher, which reads
// the command line from SPAWN_CMD_ENV. Exported for the platform tests.
export function hiddenSpawnCommand({ wscript = wscriptPath(), launcher = LAUNCHER_SCRIPT } = {}) {
  return [wscript, "//B", "//Nologo", launcher]
}

export function parseConfig() {
  try {
    const content = readFileSync(CONFIG_FILE, "utf-8")
    const config = {}
    for (const line of content.split("\n")) {
      const match = line.match(/^(\w+):\s*(.+)/)
      if (match) {
        const [, key, raw] = match
        const val = raw.trim()
        config[key] = val === "true" ? true : val === "false" ? false : isNaN(val) ? val : Number(val)
      }
    }
    return config
  } catch {
    return { review_threshold: THRESHOLD_DEFAULT, session_review_on_idle: true, max_conversation_buffer: 50 }
  }
}

export function truncate(text, maxLen) {
  if (!text || text.length <= maxLen) return text || ""
  return text.slice(0, maxLen - 3) + "..."
}

// @spec MI-CMP-009, MI-CMP-010
function instructionKey(p) {
  const s = String(p).replaceAll("\\", "/")
  return process.platform === "win32" ? s.toLowerCase() : s
}

export function injectInstructions() {
  try {
    // AUTOLEARN_OPENCODE_CONFIG redirects the patched config (tests, sandboxes).
    const configPath = process.env.AUTOLEARN_OPENCODE_CONFIG || join(homedir(), ".config", "opencode", "opencode.json")
    if (!existsSync(configPath)) return
    const raw = readFileSync(configPath, "utf-8")
    const data = JSON.parse(raw)
    if (!data.instructions) data.instructions = []

    // Remove superseded memory instruction paths (flat-layout + persona memory.md).
    // Memory Insight loads the generated memory.context.md instead. Compare by
    // normalized key so slash style and Windows case do not defeat the match.
    const oldMemoryFile = join(AL_HOME, "memory.md")
    const legacyKeys = new Set([oldMemoryFile, LEGACY_MEMORY_FILE].map(instructionKey))
    try {
      legacyKeys.add(instructionKey(join(homedir(), ".autolearn", "memory.md").replace(homedir(), "~")))
      legacyKeys.add(instructionKey(LEGACY_MEMORY_FILE.replace(homedir(), "~")))
    } catch {}
    const hadSuperseded = data.instructions.some(p => legacyKeys.has(instructionKey(p)))
    let changed = hadSuperseded
    if (hadSuperseded) {
      data.instructions = data.instructions.filter(p => !legacyKeys.has(instructionKey(p)))
    }

    // Collapse equivalent entries (forward vs back slashes, Windows case).
    const beforeDedupe = data.instructions.length
    const seen = new Set()
    data.instructions = data.instructions.filter((p) => {
      const key = instructionKey(p)
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })
    if (data.instructions.length !== beforeDedupe) changed = true

    const memoryKey = instructionKey(MEMORY_FILE)
    if (!data.instructions.some(p => instructionKey(p) === memoryKey)) {
      data.instructions.push(MEMORY_FILE)
      changed = true
    }
    if (changed) {
      writeFileSync(configPath, JSON.stringify(data, null, 2) + "\n")
    }
  } catch (err) {
    dbg("injectInstructions failed:", err.message)
  }
}

// @spec MI-CMP-009 — regenerate memory.context.md (runs migration on first call)
export function composeContext() {
  try {
    spawnDetached(["uv", "run", AUTOLEARN_CLI, "memory", "compose"], { unref: true })
    dbg("memory compose spawned")
  } catch (err) {
    dbg("memory compose failed to spawn:", err.message)
  }
}

// ---------------------------------------------------------------------------
// Per-session archive slice (RL-MEM-005). memory.context.md carries only the
// hard-capped core (every harness injects it via `instructions`). The v1
// shell adds the archive entries relevant to THIS session through
// experimental.chat.system.transform, computed once and frozen per session
// so the system prompt stays byte-stable (prefix cache). Python owns the
// policy and exports records + params to memory.archive.json; the ranking
// below mirrors memlayers.rank_score / archive_slice.
export const ARCHIVE_INDEX_FILE = join(DEFAULT_PERSONA_DIR, "memory.archive.json")
const archiveCache = { path: null, mtimeMs: -1, doc: null }

export function loadArchiveIndex(path = ARCHIVE_INDEX_FILE) {
  try {
    const st = statSync(path)
    if (archiveCache.path !== path || archiveCache.mtimeMs !== st.mtimeMs) {
      archiveCache.doc = JSON.parse(readFileSync(path, "utf-8"))
      archiveCache.mtimeMs = st.mtimeMs
      archiveCache.path = path
    }
    return archiveCache.doc
  } catch {
    return null
  }
}

export function contextTokens(text, stopwords = []) {
  const stop = new Set(stopwords)
  const out = new Set()
  for (const t of String(text || "").toLowerCase().match(/[a-z0-9]+/g) || []) {
    if (t.length >= 2 && !stop.has(t)) out.add(t)
  }
  return out
}

// BM25 over the archive (binary tf), mirroring memlayers.bm25 / search /
// archive_slice: rare shared terms dominate, long entries are normalized.
export function archiveSlice(contextText, doc = loadArchiveIndex()) {
  if (!doc || !Array.isArray(doc.records) || !doc.stats) return []
  const p = doc.params || {}
  const sliceChars = p.slice_chars ?? 1500
  const sliceMax = p.slice_max ?? 6
  const minScore = p.min_score ?? 8
  const minOverlap = p.min_overlap ?? 2
  const k1 = p.k1 ?? 1.2
  const b = p.b ?? 0.75
  const { n, avgdl, df } = doc.stats
  const ctx = contextTokens(contextText, doc.stopwords || [])
  if (!ctx.size || !n) return []
  const scored = []
  for (const rec of doc.records) {
    const toks = rec.topics || []
    if (!toks.length) continue
    const norm = 1 - b + b * (toks.length / (avgdl || 1))
    const tfPart = (k1 + 1) / (1 + k1 * norm)
    let score = 0
    let overlap = 0
    for (const t of toks) {
      if (!ctx.has(t)) continue
      overlap++
      const d = df[t] || 0
      score += Math.log(1 + (n - d + 0.5) / (d + 0.5)) * tfPart
    }
    if (overlap < minOverlap || score <= 0) continue
    const ret = rec.retention == null ? 0.5 : Number(rec.retention)
    scored.push({ rec, score: score * (0.75 + 0.25 * ret) })
  }
  scored.sort((a, c) => c.score - a.score)
  const chosen = []
  let used = 0
  for (const { rec, score } of scored) {
    if (score < minScore) break
    const cost = `- ${rec.text}\n`.length
    if (used + cost > sliceChars) continue
    chosen.push(rec)
    used += cost
    if (chosen.length >= sliceMax) break
  }
  return chosen
}

export function formatArchiveSlice(records) {
  if (!records || !records.length) return ""
  let out = "# Autolearn: memories relevant to this session\n\n"
  out += "<!-- Retrieved from the autolearn archive for this project/request. -->\n\n"
  for (const r of records) out += `- ${r.text}\n`
  return out
}

// Session context for relevance: project label, directory segments, the git
// origin URL (repo name is often the best signal), and the first user message.
export function sessionContextText({ directory, firstMessage } = {}) {
  // The home directory's own name (the username) matches nothing useful and
  // its path segments only add noise, so it contributes no project label.
  const label = projectLabel(directory)
  const parts = [label.toLowerCase() === projectLabel(homedir()).toLowerCase() ? "" : label]
  try {
    const cfg = readFileSync(join(directory, ".git", "config"), "utf-8")
    const m = cfg.match(/\[remote "origin"\][^[]*?url\s*=\s*(\S+)/)
    if (m) parts.push(m[1].replace(/[/:.]+/g, " "))
  } catch {}
  if (firstMessage) parts.push(String(firstMessage).slice(0, 2000))
  return parts.join(" ")
}

// ---------------------------------------------------------------------------
// Tool notes (RL-INP-002, RL-SKL-007). Built from tool.execute.after: the
// strongest learning signal is "a call failed -> the user redirected", and
// the reviewer only ever saw the prose around it. Notes are compact and
// redacted; successful calls produce none except skill loads/edits.
const SKILL_FILE_RE = /[\\/]skills[\\/]([^\\/]+)[\\/](?:SKILL\.md|references[\\/][^\\/]+)$/i

function lastLines(text, n = 6) {
  return String(text || "").trim().split(/\r?\n/).slice(-n).join(" | ")
}

export function toolNote(input = {}, output = {}) {
  const tool = String(input.tool || "")
  const args = input.args || {}
  const meta = output.metadata || {}
  if (tool === "skill") {
    const name = args.name || args.id
    return name ? { kind: "skill_load", skill: String(name), text: `[skill] loaded ${name}` } : null
  }
  const filePath = args.filePath || args.file_path || args.path || ""
  const m = typeof filePath === "string" ? filePath.match(SKILL_FILE_RE) : null
  if (m && /^(edit|write|patch|multiedit|apply_patch)$/i.test(tool)) {
    return { kind: "skill_edit", skill: m[1], text: `[skill-edit] ${m[1]} via ${tool}` }
  }
  const out = typeof output.output === "string" ? output.output : ""
  const exit = meta.exit ?? meta.exitCode
  const failed = (exit != null && Number(exit) !== 0) || /^\s*error\b/i.test(out)
  if (!failed) return null
  const what = tool === "bash" ? (args.command || "") : (filePath || args.pattern || args.url || JSON.stringify(args))
  const status = exit != null ? `exit ${exit}` : "error"
  return {
    kind: "tool_error",
    text: `[tool error] ${tool}: ${redact(truncate(String(what), 200))} -> ${status}: ${redact(truncate(lastLines(out), 300))}`,
  }
}

// Pending skill proposals (staged by the reviewer, which may not edit skills
// itself) surface at the moment a session LOADS that skill: the agent that
// is using it has the evidence to apply or reject them (RL-SKL-002).
export const SKILL_PROPOSALS_FILE = join(SKILLS_DIR, ".proposals.json")

export function pendingProposalsNote(skill, { file = SKILL_PROPOSALS_FILE, max = 3 } = {}) {
  let items = []
  try { items = JSON.parse(readFileSync(file, "utf-8")) } catch { return "" }
  const mine = (Array.isArray(items) ? items : []).filter(p => p && p.status === "pending" && p.skill === skill)
  if (!mine.length) return ""
  let out = `\n\n<autolearn-proposals skill="${skill}">\n`
  out += `${mine.length} pending improvement(s) for this skill were staged from earlier sessions. `
  out += "If what you observe while using the skill confirms one, apply it; if it is wrong, reject it with the reason:\n"
  for (const p of mine.slice(0, max)) {
    if (p.kind === "edit") {
      out += `- ${p.id} (${p.file || "SKILL.md"}): replace "${truncate(String(p.old || ""), 160)}" with "${truncate(String(p.new || ""), 240)}" — reason: ${truncate(String(p.reason || ""), 200)}\n`
    } else {
      out += `- ${p.id}: full rewrite staged (${p.old_chars} -> ${p.new_chars} chars) — reason: ${truncate(String(p.reason || ""), 200)}\n`
    }
  }
  out += `Apply: uv run "${AUTOLEARN_CLI}" skill apply <id>   Reject: uv run "${AUTOLEARN_CLI}" skill reject <id> --reason "<why>"\n</autolearn-proposals>`
  return out
}

export const MAX_OBS_LINES = 1000

// @spec KS-OBS-001, KS-OBS-002, KS-OBS-003
export function logObs(obs) {
  obs.timestamp = new Date().toISOString()
  if (!obs.design) obs.design = DESIGN_VERSION
  try {
    // Append + trim under the store lock: the Python CLI appends and trims
    // the same file, and an unlocked trim silently drops concurrent appends.
    withStoreLock(() => {
      // @spec KS-OBS-005
      appendFileSync(OBS_FILE, JSON.stringify(obs) + "\n")
      // @spec KS-OBS-004
      trimFile(OBS_FILE, MAX_OBS_LINES)
    })
  } catch {
    // @spec KS-OBS-006
  }
}

// @spec KS-OBS-004
export function trimFile(filePath, maxLines) {
  try {
    const content = readFileSync(filePath, "utf-8")
    const lines = content.split("\n")
    if (lines.length <= maxLines) return
    const trimmed = lines.slice(-maxLines).join("\n")
    const tmp = `${filePath}.${process.pid}.tmp`
    writeFileSync(tmp, trimmed)
    renameSync(tmp, filePath)
    dbg("TRIMMED", filePath, "from", lines.length, "to", maxLines, "lines")
  } catch {}
}

// @spec CM-RS-006
export function formatReview(messages, meta = {}) {
  const { project = "unknown", trigger = "exit" } = meta
  let md = "# Autolearn Review\n\n"
  md += "## Context\n\n"
  md += `- Project: ${project}\n`
  md += `- Date: ${new Date().toISOString()}\n`
  md += `- Turns in this review: ${messages.length}\n`
  md += `- Trigger: ${trigger}\n`
  md += `- Design: ${DESIGN_VERSION}\n`
  // RL-INP-002: the reviewer must know which skills were actually used — it
  // stages proposals only for those, and never patches blind.
  const loaded = [...(meta.loadedSkills || [])]
  md += `- Skills loaded in this conversation: ${loaded.length ? loaded.join(", ") : "none"}\n\n`
  md += "## Instructions\n\n"
  md += "Review the conversation below for learning opportunities.\n"
  md += "Load the autolearn skill and follow references/reviewer.md for the\n"
  md += "full signal taxonomy and action protocol.\n\n"
  md += "Focus on:\n\n"
  md += "1. User corrections (style, approach, tools) — \"don't do X\", \"use Y instead\"\n"
  md += "2. User preferences AND declarative workflow specs — not just corrections.\n"
  md += "   The user may describe how they want something done without a mistake\n"
  md += "   being made first. Capture these too:\n"
  md += "   - \"they should be one post one week\" (cadence spec)\n"
  md += "   - \"we don't use global pip anywhere here\" (system-wide tool rule)\n"
  md += "   - \"LinkedIn should follow Bluesky schedule\" (sync rule)\n"
  md += "   - \"use PEP 723 inline metadata for Python scripts\" (convention)\n"
  md += "3. Workarounds or techniques that worked\n"
  md += "4. Skills that were wrong, incomplete, or outdated\n"
  md += "5. Repeated patterns worth capturing\n\n"
  md += "IMPORTANT: Preferences are not always corrections. Scan every user message\n"
  md += "for declarative specs (\"should be\", \"we use\", \"we don't\", \"I want\") even\n"
  md += "when no error occurred. Record general rules, not narrow instances.\n\n"
  md += "## Conversation\n\n"

  for (const msg of messages) {
    const label = msg.role === "user" ? "User" : msg.role === "tool" ? "Tool" : "Assistant"
    md += `### ${label}\n\n${msg.content}\n\n`
  }

  md += "---\n\nTake action now.\n"
  return md
}

// @spec CM-RS-014
export function cleanStaleReviews(config) {
  try {
    const staleMs = (config.stale_after_days || STALE_DAYS_DEFAULT) * 86400000
    const now = Date.now()
    // The review markdown plus its run sidecars (<id>.run.jsonl / .run.err,
    // RL-OPS-005), in the reviews dir and the pending queue.
    for (const dir of [REVIEWS_DIR, join(REVIEWS_DIR, "pending")]) {
      let files = []
      try { files = readdirSync(dir) } catch { continue }
      for (const f of files) {
        if (!f.startsWith("review-")) continue
        const match = f.match(/^review-(?:exit-)?(\d+)\.(?:md|run\.jsonl|run\.err)$/)
        if (!match) continue
        const fileTime = parseInt(match[1], 10)
        if (now - fileTime > staleMs) {
          try { unlinkSync(join(dir, f)); dbg("CLEANED STALE REVIEW", f) } catch {}
        }
      }
    }
  } catch (err) {
    dbg("CLEAN STALE FAILED", err.message)
  }
}

/**
 * Cross-process review throttle: multiple plugin instances (one per OpenCode
 * project/directory) share one LLM provider budget, so a global lock file
 * gates spawns across all of them.
 *
 * Returns true when a spawn is allowed; when `commit` is true (default) it
 * also bumps the last-review timestamp so the NEXT allowed spawn is delayed
 * by min_interval_ms. Pass `commit: false` to PEEK without writing the lock —
 * the shells use this to speculate before clearing their buffer, while
 * `runReviewSubprocess` remains the single committing gate (a committing
 * pre-check would write the lock and the authoritative check would then
 * match its own hash and suppress the spawn — issue #14).
 * Three layers (checked in order, cheapest first):
 *   1. Content (all projects): a hash of the review's Conversation section;
 *      the identical conversation snapshot (e.g. the same idle moment seen
 *      by every instance of the same event stream) never reviews twice.
 *   2. Global spacing (all projects): min_interval_ms between any two
 *      reviews (default 3 min). Prevents the N-project parallel burst.
 *   3. Daily cap (all projects): max_reviews_per_day reviews per calendar
 *      day, counted from review file timestamps. Default 24. Hard ceiling
 *      on provider spend even if other gates misbehave.
 */
export const THROTTLE_FILE = join(AL_HOME, ".last_review_lock")

// FNV-1a over the review text — fast, no deps, stable across runtimes.
export function contentHash(str) {
  let h = 0x811c9dc5
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return (h >>> 0).toString(16)
}

// The wrapper's single-flight claim. A fresh claim means a review is running
// right now (median ~5 min, p90 ~17 min in production) — handing the wrapper
// another review would only get it queued, so the peek keeps the buffer
// instead and the content rides the next trigger (RL-OPS-003).
export const WRAPPER_GATE_DIR = join(AL_HOME, ".review_gate")
export const WRAPPER_GATE_STALE_MS = 900000

export function wrapperBusy(now = Date.now(), gateDir = WRAPPER_GATE_DIR) {
  try {
    if (!existsSync(gateDir)) return false
    const ts = parseInt(readFileSync(join(gateDir, "ts"), "utf-8").trim(), 10) || 0
    return now - ts * 1000 < WRAPPER_GATE_STALE_MS
  } catch {
    return false
  }
}

export function throttleCheck(reviewMd, commit = true) {
  return throttleVerdict(reviewMd, { commit }).ok
}

/**
 * Like throttleCheck, but returns `{ ok, reason }` so callers can tell a
 * permanent refusal (duplicate content, daily cap) from a transient one
 * (busy wrapper, interval) that should be retried or queued.
 * `checkBusy` makes the peek treat a running review as transient-busy.
 */
export function throttleVerdict(reviewMd, { commit = true, checkBusy = false } = {}) {
  // Hash the CONVERSATION section only, not the whole md: the Context header
  // (project name, timestamp) varies between plugin instances that observed
  // the same conversation, and those variants must still dedupe — that is
  // exactly the multi-project burst pattern this throttle exists to stop.
  const convIdx = reviewMd.indexOf("## Conversation")
  const dedupeKey = convIdx >= 0 ? reviewMd.slice(convIdx) : reviewMd

  let lastMs = 0
  let lastHash = ""
  try {
    const raw = readFileSync(THROTTLE_FILE, "utf-8")
    const [ms, hash] = raw.trim().split(":")
    lastMs = parseInt(ms, 10) || 0
    lastHash = hash || ""
  } catch {}

  if (contentHash(dedupeKey) === lastHash) {
    dbg("THROTTLED: identical review content already spawned")
    return { ok: false, reason: "duplicate" }
  }

  if (checkBusy && wrapperBusy()) {
    dbg("THROTTLED: a review is running (wrapper gate held)")
    return { ok: false, reason: "busy" }
  }

  const config = parseConfig()
  const minInterval = config.min_interval_ms ?? MIN_INTERVAL_DEFAULT_MS
  const now = Date.now()
  const gap = now - lastMs
  if (gap < minInterval) {
    dbg("THROTTLED: global min_interval_ms", gap, "<", minInterval)
    return { ok: false, reason: "interval" }
  }

  // Daily cap across all projects: reviews spawned today (file timestamps)
  // must not exceed max_reviews_per_day. Bounds worst-case provider spend
  // even if every other gate fails.
  const dailyCap = config.max_reviews_per_day ?? MAX_REVIEWS_PER_DAY_DEFAULT
  if (dailyCap > 0) {
    const dayStart = new Date(); dayStart.setHours(0, 0, 0, 0)
    let today = 0
    try {
      for (const f of readdirSync(REVIEWS_DIR)) {
        const m = f.match(/review-(?:exit-)?(\d+)\.md/)
        if (m && parseInt(m[1], 10) >= dayStart.getTime()) today++
      }
    } catch {}
    if (today >= dailyCap) {
      dbg("THROTTLED: daily cap reached", today, ">=", dailyCap)
      return { ok: false, reason: "daily_cap" }
    }
  }

  // Committing calls record the lock; peeking calls (commit: false) leave
  // throttle state untouched so the authoritative gate in
  // runReviewSubprocess can still admit this spawn (issue #14).
  if (commit) {
    try { writeFileSync(THROTTLE_FILE, `${now}:${contentHash(dedupeKey)}`) } catch {}
  }
  return { ok: true, reason: null }
}

// Transient refusals: the content is new, just not runnable right now.
const TRANSIENT_THROTTLE = new Set(["busy", "interval"])

/**
 * Activity-coupled curator trigger (issue #23): decide whether the curator is
 * due. Replaces the never-shipped `opencode schedule` documented path.
 *
 * Due when `curator_interval_days` has elapsed since the curator's last run
 * (never-run counts as due) AND the spawn cooldown has expired. Set
 * `curator_interval_days: 0` to disable the automatic trigger entirely.
 * All inputs are injectable for testing.
 */
export function curatorDue({ config = parseConfig(), now = Date.now(), stateFile = CURATOR_STATE_FILE, cooldownFile = CURATOR_COOLDOWN_FILE } = {}) {
  const rawInterval = config.curator_interval_days
  if (rawInterval === 0 || rawInterval === "0") return { due: false, reason: "disabled" }
  const intervalDays = Number(rawInterval) > 0 ? Number(rawInterval) : 7
  let lastRunMs = 0
  try {
    const state = JSON.parse(readFileSync(stateFile, "utf-8"))
    // Python stores last_run as a date string ("YYYY-MM-DD"); Date.parse
    // gives UTC midnight — fine at day granularity.
    if (state.last_run) lastRunMs = Date.parse(state.last_run) || 0
  } catch {}
  if (now - lastRunMs < intervalDays * 86400000) {
    return { due: false, reason: "interval", intervalDays, lastRunMs }
  }
  try {
    const cd = parseInt(readFileSync(cooldownFile, "utf-8").trim(), 10) || 0
    if (now - cd < CURATOR_COOLDOWN_MS) return { due: false, reason: "cooldown" }
  } catch {}
  return { due: true, intervalDays, lastRunMs }
}

/**
 * Fire the curator via the wrapper's --curator mode when it is due.
 * Called from runReviewSubprocess after every successful review spawn, so
 * all three harness shells (v1, v2, pi) get identical behavior. The wrapper
 * enforces the real single-flight gate; this side adds the spawn cooldown
 * so a dead curator run is not re-attempted on every review.
 */
// The curator gets its own agent (larger step budget) when the installer
// defined one; otherwise the wrapper falls back to the reviewer agent.
export function curatorAgentName() {
  const cfg = parseConfig()
  if (cfg.curator_agent) return String(cfg.curator_agent)
  try {
    const configPath = process.env.AUTOLEARN_OPENCODE_CONFIG || join(homedir(), ".config", "opencode", "opencode.json")
    const data = JSON.parse(readFileSync(configPath, "utf-8"))
    if (data?.agent?.["autolearn-curator"]) return "autolearn-curator"
  } catch {}
  return null
}

export function maybeSpawnCurator({ cwd, project, env, spawnFn, now = Date.now(), config, stateFile, cooldownFile } = {}) {
  try {
    const verdict = curatorDue({ ...(config ? { config } : {}), now, ...(stateFile ? { stateFile } : {}), ...(cooldownFile ? { cooldownFile } : {}) })
    if (!verdict.due) {
      dbg("CURATOR not due:", verdict.reason)
      return { ok: false, ...verdict }
    }
    // Record the cooldown BEFORE spawning (fail-safe: a killed run still
    // consumes it, mirroring the wrapper's record-start-before-run rule).
    try { writeFileSync(cooldownFile || CURATOR_COOLDOWN_FILE, String(now)) } catch {}
    const spawn = spawnFn || ((cmd, opts) => spawnDetached(cmd, opts))
    // Forward the caller's env UNCHANGED: the shells pin AUTOLEARN_HARNESS_BIN
    // per harness family, and dropping the pin would let the wrapper's PATH
    // fallback route a v1-pinned curator to opencode2 (or pi) — the binary-
    // shadowing failure mode. AUTOLEARN_CURATOR marks the run for debugging.
    const curatorAgent = curatorAgentName()
    spawn(wrapperCommand(["--curator"]), {
      cwd: cwd || process.cwd(),
      env: { ...process.env, AUTOLEARN_CURATOR: "1", ...(curatorAgent ? { AUTOLEARN_CURATOR_AGENT: curatorAgent } : {}), ...(env || {}) },
    })
    const obs = { type: "curator_triggered", project: project || "unknown", interval_days: verdict.intervalDays }
    if (verdict.lastRunMs) obs.last_run = new Date(verdict.lastRunMs).toISOString().slice(0, 10)
    logObs(obs)
    dbg("CURATOR TRIGGERED (activity-coupled)", project || "unknown")
    return { ok: true }
  } catch (err) {
    dbg("maybeSpawnCurator failed:", err.message)
    return { ok: false, reason: "error", error: err.message }
  }
}

/**
 * Spawn a review subprocess via the wrapper script.
 * `messageCount`, `project`, and `trigger` are recorded in the observation
 * log (spec CM-RS-013); pass `log: false` to skip observation logging
 * (the v1 exit-review path stays silent, matching original behavior).
 * Returns { ok, reviewFile, reviewMd } — reviewMd is set even on failure so
 * callers can write the fallback file.
 */
// @spec CM-RS-007..CM-RS-013
export const PENDING_DIR = join(REVIEWS_DIR, "pending")

// Extra harness args for the review run from config (RL-INP-003):
// reviewer_model -> --model, reviewer_variant -> --variant.
export function reviewerArgs(config = parseConfig()) {
  const out = []
  if (config.reviewer_model) out.push("--model", String(config.reviewer_model))
  if (config.reviewer_variant) out.push("--variant", String(config.reviewer_variant))
  return out
}

export function runReviewSubprocess({ reviewMd, filePrefix = "review", title, cwd, env, messageCount, project, trigger, log = true, queueIfThrottled = false }) {
  // Cross-process throttle first: identical content never reviews twice, any
  // two reviews are separated by min_interval_ms (default 3 min), and a
  // daily cap bounds total provider spend.
  // @spec CM-RS-020
  const verdict = throttleVerdict(reviewMd)
  if (!verdict.ok) {
    // Callers that cannot retry later (the exit review: the session is
    // ending and its buffer dies with it) queue transient refusals for the
    // next wrapper run to drain instead of dropping them (RL-OPS-002).
    if (queueIfThrottled && TRANSIENT_THROTTLE.has(verdict.reason)) {
      try {
        mkdirSync(PENDING_DIR, { recursive: true })
        const queued = join(PENDING_DIR, `${filePrefix}-${Date.now()}.md`)
        writeFileSync(queued, reviewMd)
        logObs({ type: "review_deferred", review_id: queued.split(/[\\/]/).pop().replace(/\.md$/, ""), reason: verdict.reason, project: project || "unknown", trigger: trigger || "unknown" })
        dbg("REVIEW QUEUED (throttled:", verdict.reason + ")", queued)
        return { ok: false, reviewFile: queued, reviewMd, throttled: true, queued: true, reason: verdict.reason }
      } catch (err) {
        dbg("REVIEW QUEUE FAILED", err.message)
      }
    }
    dbg("REVIEW SUPPRESSED by throttle", { project, trigger, reason: verdict.reason })
    if (log) {
      logObs({ type: "review_throttled", reason: verdict.reason, project: project || "unknown", trigger: trigger || "unknown" })
    }
    return { ok: false, reviewFile: null, reviewMd, throttled: true, reason: verdict.reason }
  }

  // @spec CM-RS-007
  const reviewFile = join(REVIEWS_DIR, `${filePrefix}-${Date.now()}.md`)
  writeFileSync(reviewFile, reviewMd)
  dbg("REVIEW FILE WRITTEN", reviewFile)
  const reviewId = reviewFile.split(/[\\/]/).pop().replace(/\.md$/, "")

  // @spec CM-RS-008, CM-RS-009, CM-RS-010
  // Pass the review FILE PATH, not the content: the wrapper streams the file
  // to the harness on stdin. Content-as-argv exceeds the Windows command-line
  // limit (32,767 chars) once the review passes ~32 KB and the spawn dies
  // with ENAMETOOLONG (issue #21).
  const args = [reviewFile, "--agent", "autolearn-reviewer", "--title", title, ...reviewerArgs()]
  spawnDetached(wrapperCommand(args), {
    cwd: cwd || process.cwd(),
    env: { ...process.env, AUTOLEARN_REVIEWER: "1", ...(env || {}) },
  })

  // @spec CM-RS-013
  if (log) {
    const obs = { type: "review_spawned", review_file: reviewFile, review_id: reviewId }
    if (typeof messageCount === "number") obs.message_count = messageCount
    obs.project = project || "unknown"
    if (trigger) obs.trigger = trigger
    logObs(obs)
  }
  dbg("REVIEW SPAWNED OK via wrapper", reviewFile)

  // Activity-coupled curator trigger (issue #23): after every successful
  // review spawn, fire the curator if curator_interval_days has elapsed.
  // `env` is forwarded so the harness-binary pin reaches the curator too.
  maybeSpawnCurator({ cwd, project, env })

  // Note: sync push happens in the wrapper script AFTER the review
  // completes, not here — otherwise we'd push pre-review state.
  return { ok: true, reviewFile, reviewMd }
}
