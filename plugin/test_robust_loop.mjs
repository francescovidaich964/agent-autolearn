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
export function done() {
  console.log(`\n${passed} passed, ${failed} failed`)
  rmSync(HOME, { recursive: true, force: true })
  process.exit(failed ? 1 : 0)
}
done()
