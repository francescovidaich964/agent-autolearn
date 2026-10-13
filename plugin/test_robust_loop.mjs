#!/usr/bin/env node
/**
 * Tests for the robust-learning-loop changes in the shared core
 * (docs/designs/robust-learning-loop/LLD.md).
 *
 * Run: node plugin/test_robust_loop.mjs   (also: bun plugin/test_robust_loop.mjs)
 *
 * All state lives under a temp AUTOLEARN_HOME; the real ~/.autolearn is
 * never touched. Sections are appended per phase.
 */

import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, utimesSync, readdirSync } from "fs"
import { tmpdir } from "os"
import { join } from "path"

const HOME = mkdtempSync(join(tmpdir(), "al-robust-test-"))
process.env.AUTOLEARN_HOME = HOME
// The v1 shell patches opencode.json and spawns `memory compose` on load:
// point both at sandbox paths so the real config and CLI are never touched.
process.env.AUTOLEARN_OPENCODE_CONFIG = join(HOME, "opencode.json")
process.env.AUTOLEARN_CLI = join(HOME, "no-such-cli.py")
delete process.env.AUTOLEARN_REVIEWER
delete process.env.AUTOLEARN_DISABLED

const core = await import("./autolearn-core.mjs")
core.ensureStore()

let passed = 0
let failed = 0
function ok(cond, name) {
  if (cond) { passed++; console.log(`  ok - ${name}`) }
  else { failed++; console.error(`  FAIL - ${name}`) }
}

// ---------------------------------------------------------------------------
console.log("A. Store integrity (RL-INT)")

ok(core.projectLabel("C:\\Users\\me\\repos\\demo-project") === "demo-project", "projectLabel: Windows path -> basename")
ok(core.projectLabel("C:\\Users\\someuser") === "someuser", "projectLabel: Windows home dir")
ok(core.projectLabel("/home/me/repos/demo-project/") === "demo-project", "projectLabel: POSIX path with trailing slash")
ok(core.projectLabel("") === "unknown", "projectLabel: empty -> unknown")

let ran = false
core.withStoreLock(() => {
  ran = true
  ok(existsSync(core.STORE_LOCK_DIR), "withStoreLock: lock dir held during fn")
})
ok(ran && !existsSync(core.STORE_LOCK_DIR), "withStoreLock: released after fn")

// A stale lock (dead holder) is broken instead of blocking forever.
mkdirSync(core.STORE_LOCK_DIR)
const old = new Date(Date.now() - core.STORE_LOCK_STALE_MS - 10000)
utimesSync(core.STORE_LOCK_DIR, old, old)
let ranStale = false
core.withStoreLock(() => { ranStale = true })
ok(ranStale && !existsSync(core.STORE_LOCK_DIR), "withStoreLock: stale lock broken")

// A fresh foreign lock: the plugin must not stall the host — proceed after
// the timeout, and leave the foreign holder's lock in place.
mkdirSync(core.STORE_LOCK_DIR)
const t0 = Date.now()
let ranBusy = false
core.withStoreLock(() => { ranBusy = true }, { timeoutMs: 150 })
ok(ranBusy && Date.now() - t0 < 2000, "withStoreLock: busy lock -> proceeds after timeout")
ok(existsSync(core.STORE_LOCK_DIR), "withStoreLock: does not delete a lock it does not hold")
rmSync(core.STORE_LOCK_DIR, { recursive: true, force: true })

// Orphaned tmp sweep.
const oldTmp = join(core.DEFAULT_PERSONA_DIR, "memories.jsonl.999.tmp")
const newTmp = join(core.DEFAULT_PERSONA_DIR, "memories.jsonl.998.tmp")
writeFileSync(oldTmp, "x")
writeFileSync(newTmp, "y")
const twoHoursAgo = new Date(Date.now() - 2 * 3600000)
utimesSync(oldTmp, twoHoursAgo, twoHoursAgo)
const swept = core.sweepTmp()
ok(swept.includes("memories.jsonl.999.tmp") && !existsSync(oldTmp), "sweepTmp: removes old tmp")
ok(existsSync(newTmp), "sweepTmp: keeps fresh tmp (writer may be mid-write)")

// Observations carry the design tag; append+trim does not lose lines.
core.logObs({ type: "probe" })
const lastObs = JSON.parse(readFileSync(core.OBS_FILE, "utf-8").trim().split("\n").pop())
ok(lastObs.design === core.DESIGN_VERSION, "logObs: stamps design version")
ok(!existsSync(core.STORE_LOCK_DIR), "logObs: lock released")

// ---------------------------------------------------------------------------
console.log("B. Review pipeline: no-loss queue + run log (RL-OPS)")

const { spawnSync } = await import("child_process")
function findGitBash() {
  for (const c of [process.env.OPENCODE_GIT_BASH_PATH, "C:/Program Files/Git/bin/bash.exe", "C:/Program Files (x86)/Git/bin/bash.exe"].filter(Boolean)) {
    try { if (existsSync(c)) return c } catch {}
  }
  return "bash"
}
const runWrapper = (args, opts) => process.platform === "win32"
  ? spawnSync(findGitBash(), [core.WRAPPER_SCRIPT, ...args], opts)
  : spawnSync(core.WRAPPER_SCRIPT, args, opts)

// Stub harness: records argv + the review id it was given, emits one
// sessionID line, two step_finish events and one noisy tool event.
const FAKE_LOG = join(HOME, "fake-oc.log")
const fakeOc = join(HOME, "fake-oc")
writeFileSync(fakeOc, `#!/bin/sh
printf '%s|%s\\n' "$AUTOLEARN_REVIEW_ID" "$*" >> "$FAKE_OC_LOG"
if [ "$1" = "run" ]; then
  cat > /dev/null
  echo '{"type":"step_start","sessionID":"ses_fake"}'
  echo '{"type":"tool_use","sessionID":"ses_fake","part":{"state":{"output":"HUGE TOOL OUTPUT"}}}'
  echo '{"type":"step_finish","sessionID":"ses_fake","part":{"tokens":{"input":100,"output":20},"cost":0}}'
  echo '{"type":"step_finish","sessionID":"ses_fake","part":{"tokens":{"input":50,"output":5},"cost":0}}'
  echo "warning from harness" >&2
fi
exit 0
`)
const { chmodSync } = await import("fs")
chmodSync(fakeOc, 0o755)
const wenv = { ...process.env, AUTOLEARN_HOME: HOME, AUTOLEARN_HARNESS_BIN: fakeOc, FAKE_OC_LOG: FAKE_LOG, AUTOLEARN_SYNC_API_KEY: "" }
const RUNLOG = join(core.DEFAULT_PERSONA_DIR, "review-runs.jsonl")
const runEvents = () => existsSync(RUNLOG) ? readFileSync(RUNLOG, "utf-8").trim().split("\n").filter(Boolean).map(l => JSON.parse(l)) : []
const fakeCalls = () => existsSync(FAKE_LOG) ? readFileSync(FAKE_LOG, "utf-8").trim().split("\n").filter(Boolean) : []
const GATE = join(HOME, ".review_gate")
const WLOCK = join(HOME, ".last_wrapper_review")
const mkReview = (ms, body) => {
  const f = join(core.REVIEWS_DIR, `review-${ms}.md`)
  writeFileSync(f, `# Autolearn Review\n\n## Conversation\n\n### User\n\n${body}\n`)
  return f
}

// 1. Busy gate: the review is queued, not dropped, and the harness is not run.
mkdirSync(GATE, { recursive: true })
writeFileSync(join(GATE, "ts"), String(Math.floor(Date.now() / 1000)))
const t1 = Date.now()
const busyFile = mkReview(t1, "conversation one")
let wr = runWrapper([busyFile, "--agent", "autolearn-reviewer", "--title", "autolearn review"], { env: wenv, encoding: "utf-8", timeout: 20000 })
ok(wr.status === 0, "busy: wrapper exits 0")
ok(!existsSync(busyFile) && existsSync(join(core.PENDING_DIR, `review-${t1}.md`)), "busy: review moved to pending/ (not dropped)")
ok(runEvents().some(e => e.event === "deferred" && e.reason === "busy" && e.review === `review-${t1}`), "busy: run log records deferred/busy")
ok(fakeCalls().length === 0, "busy: harness not invoked")
ok(core.wrapperBusy(), "wrapperBusy(): fresh gate -> busy")
rmSync(GATE, { recursive: true, force: true })
ok(!core.wrapperBusy(), "wrapperBusy(): no gate -> idle")

// 2. Free gate: the review runs, then the queued one is drained in the
// same invocation. Both carry their review id into the harness env.
const t2 = t1 + 1
const runFile = mkReview(t2, "conversation two")
wr = runWrapper([runFile, "--agent", "autolearn-reviewer", "--title", "autolearn review"], { env: wenv, encoding: "utf-8", timeout: 30000 })
ok(wr.status === 0, "run: wrapper exits 0")
const calls = fakeCalls()
ok(calls.some(c => c.startsWith(`review-${t2}|run --format json --agent autolearn-reviewer`)), "run: harness got the review id + args")
ok(calls.some(c => c.startsWith(`review-${t1}|run --format json`)), "drain: queued review ran in the same invocation")
ok(calls.filter(c => c.includes("session delete ses_fake")).length === 2, "run+drain: both sessions deleted")
ok(!existsSync(join(core.PENDING_DIR, `review-${t1}.md`)), "drain: pending queue emptied")
const evs = runEvents()
const fin = evs.find(e => e.event === "finished" && e.review === `review-${t2}`)
ok(fin && fin.rc === 0 && typeof fin.duration_s === "number" && fin.session === true && fin.design === core.DESIGN_VERSION, "run log: finished with rc, duration, session, design")
ok(evs.some(e => e.event === "drained" && e.review === `review-${t1}`), "run log: drained event")
const side = readFileSync(join(core.REVIEWS_DIR, `review-${t2}.run.jsonl`), "utf-8").trim().split("\n")
ok(side.length === 2 && side.every(l => l.includes("step_finish")), "sidecar keeps only step_finish events (no tool output)")
ok(readFileSync(join(core.REVIEWS_DIR, `review-${t2}.run.err`), "utf-8").includes("warning from harness"), "sidecar keeps the stderr tail")
ok(!existsSync(GATE), "gate released after run + drain")

// 3. Interval gate: a second review right after is queued with reason=interval.
const t3 = t2 + 1
const soonFile = mkReview(t3, "conversation three")
wr = runWrapper([soonFile, "--agent", "autolearn-reviewer", "--title", "autolearn review"], { env: wenv, encoding: "utf-8", timeout: 20000 })
ok(runEvents().some(e => e.event === "deferred" && e.reason === "interval" && e.review === `review-${t3}`), "interval: queued, not dropped")

// 4. Duplicate content is skipped (and logged), not queued.
writeFileSync(WLOCK, "0:" + readFileSync(WLOCK, "utf-8").trim().split(":")[1])
const t4 = t3 + 1
const dupFile = mkReview(t4, "conversation two")
wr = runWrapper([dupFile, "--agent", "autolearn-reviewer"], { env: wenv, encoding: "utf-8", timeout: 20000 })
ok(runEvents().some(e => e.event === "skipped_duplicate" && e.review === `review-${t4}`), "duplicate: skipped and logged")

// 5. Expired pending reviews are removed and logged by the next drain.
rmSync(join(core.PENDING_DIR, `review-${t3}.md`), { force: true })
const ancient = Date.now() - 4 * 86400000
writeFileSync(join(core.PENDING_DIR, `review-${ancient}.md`), "# Autolearn Review\n\n## Conversation\n\nold\n")
writeFileSync(WLOCK, "0:x")
const t5 = Date.now() + 5
const freshFile = mkReview(t5, "conversation five")
runWrapper([freshFile, "--agent", "autolearn-reviewer"], { env: wenv, encoding: "utf-8", timeout: 30000 })
ok(!existsSync(join(core.PENDING_DIR, `review-${ancient}.md`)) && runEvents().some(e => e.event === "expired" && e.review === `review-${ancient}`), "expired pending review removed + logged")

// 6. Plugin peek: a busy wrapper keeps the buffer (transient), and the exit
// review is queued instead of dropped when throttled.
mkdirSync(GATE, { recursive: true })
writeFileSync(join(GATE, "ts"), String(Math.floor(Date.now() / 1000)))
const md = core.formatReview([{ role: "user", content: "brand new topic " + Date.now() }], { project: "p", trigger: "idle" })
const v = core.throttleVerdict(md, { commit: false, checkBusy: true })
ok(!v.ok && v.reason === "busy", "peek: busy wrapper -> transient refusal")
ok(md.includes(`- Design: ${core.DESIGN_VERSION}`), "review header carries the design tag")
writeFileSync(core.THROTTLE_FILE, `${Date.now()}:other`)
const exitRes = core.runReviewSubprocess({ reviewMd: md, filePrefix: "review-exit", title: "autolearn exit review", trigger: "exit", project: "p", queueIfThrottled: true })
ok(exitRes.queued && exitRes.reason === "interval" && existsSync(exitRes.reviewFile) && exitRes.reviewFile.includes("pending"), "exit review throttled -> queued to pending/")
const lastO = JSON.parse(readFileSync(core.OBS_FILE, "utf-8").trim().split("\n").pop())
ok(lastO.type === "review_deferred" && lastO.reason === "interval", "exit review deferral is observable")
rmSync(GATE, { recursive: true, force: true })

// 7. Reviewer model/variant from config reach the harness args.
ok(JSON.stringify(core.reviewerArgs({ reviewer_model: "prov/small", reviewer_variant: "minimal" })) === JSON.stringify(["--model", "prov/small", "--variant", "minimal"]), "reviewerArgs: model + variant")
ok(core.reviewerArgs({}).length === 0, "reviewerArgs: none by default")

// ---------------------------------------------------------------------------
console.log("C. Per-session archive slice (RL-MEM-005)")

function buildIndex(records, params = {}) {
  const df = {}
  let total = 0
  for (const r of records) { total += r.topics.length; for (const t of r.topics) df[t] = (df[t] || 0) + 1 }
  return { version: 2, params: { slice_chars: 1500, slice_max: 6, min_score: 1, min_overlap: 2, k1: 1.2, b: 0.75, ...params },
    stats: { n: records.length, avgdl: total / records.length, df }, stopwords: ["the", "to", "with"], records }
}
const archive = [
  { id: "gpg-cache", text: "GPG passphrase prompts: gpg-agent caches the passphrase", topics: ["gpg", "passphrase", "prompts", "agent", "caches"], retention: 0.9 },
  { id: "hosta-gpg", text: "hosta commits stay unsigned: no GPG key", topics: ["hosta", "commits", "stay", "unsigned", "gpg", "key"], retention: 0.5 },
  { id: "pandas", text: "pandas dtype coercion turns ints into floats", topics: ["pandas", "dtype", "coercion", "turns", "ints", "floats"], retention: 0.9 },
]
const sl = core.archiveSlice("how to sign commits with gpg passphrase", buildIndex(archive))
ok(sl.map(r => r.id).includes("gpg-cache") && sl.map(r => r.id).includes("hosta-gpg") && !sl.some(r => r.id === "pandas"), "archiveSlice: relevant entries in, unrelated out")
ok(core.archiveSlice("gpg", buildIndex(archive)).length === 0, "archiveSlice: single shared term is below the overlap floor")
ok(core.archiveSlice("sign commits gpg passphrase", buildIndex(archive, { min_score: 99 })).length === 0, "archiveSlice: score floor applies")
ok(core.formatArchiveSlice([]) === "" && core.formatArchiveSlice(sl).includes("- GPG passphrase"), "formatArchiveSlice")

const projDir = join(HOME, "demo-project")
mkdirSync(join(projDir, ".git"), { recursive: true })
writeFileSync(join(projDir, ".git", "config"), '[remote "origin"]\n\turl = git@github.com:acme/demo-research.git\n')
const ctxText = core.sessionContextText({ directory: projDir, firstMessage: "fix the placement config" })
ok(ctxText.includes("demo-project") && ctxText.includes("research") && ctxText.includes("placement"), "sessionContextText: project + git remote + first message")
ok(!core.sessionContextText({ directory: (await import("os")).homedir() }).toLowerCase().includes((await import("os")).userInfo().username.toLowerCase()), "sessionContextText: home dir contributes no username token")

// Drive the real v1 shell hooks: first message -> slice -> frozen per session.
writeFileSync(core.ARCHIVE_INDEX_FILE, JSON.stringify(buildIndex(archive)))
writeFileSync(join(HOME, "opencode.json"), "{}\n")
const shell = (await import("./autolearn.js")).default
const hooks = await shell({ client: {}, directory: projDir, worktree: projDir })
ok(typeof hooks["experimental.chat.system.transform"] === "function" && typeof hooks["chat.message"] === "function", "v1 shell exposes chat.message + system.transform hooks")
await hooks["chat.message"]({ sessionID: "ses_a" }, { parts: [{ type: "text", text: "sign my commits with gpg without the passphrase prompt" }] })
const out1 = { system: ["base prompt"] }
await hooks["experimental.chat.system.transform"]({ sessionID: "ses_a" }, out1)
ok(out1.system.length === 2 && out1.system[1].includes("gpg-agent caches"), "system.transform appends the relevant archive slice")
writeFileSync(core.ARCHIVE_INDEX_FILE, JSON.stringify(buildIndex([archive[2]])))
const out2 = { system: ["base prompt"] }
await hooks["experimental.chat.system.transform"]({ sessionID: "ses_a" }, out2)
ok(out2.system[1] === out1.system[1], "slice is frozen for the session (byte-stable system prompt)")
const out3 = { system: ["base prompt"] }
await hooks["experimental.chat.system.transform"]({ sessionID: "ses_b" }, out3)
ok(out3.system.length === 1, "a session with nothing relevant gets no slice")
const injected = JSON.parse(readFileSync(join(HOME, "opencode.json"), "utf-8"))
ok(Array.isArray(injected.instructions) && injected.instructions[0] === core.MEMORY_FILE, "injectInstructions honours AUTOLEARN_OPENCODE_CONFIG (sandboxed)")

// ---------------------------------------------------------------------------
export function done() {
  console.log(`\n${passed} passed, ${failed} failed`)
  rmSync(HOME, { recursive: true, force: true })
  process.exit(failed ? 1 : 0)
}
done()
