// The on-demand field-notes refresh (#613), server side: which boxes may
// offer it, the one job that may run at a time, and the subprocess that
// runs it. Two callers -- the refresh route (start + status) and the
// analysis route (whether the page should show the control at all).
//
// THE PASS STILL OWNS THE WRITE. This module never opens earl.db for
// writing: it spawns the pass's own CLI, `python -m listener.species_analysis
// --refresh <sci>`, exactly the hand recipe in the orchid runbook, and reads
// `generated_ts` read-only before and after to decide whether a new note
// actually landed. #206's "no generate-now route" line is superseded in that
// one respect only -- generation is still the pass's, never render time's.
//
// ONE AT A TIME, PER BOX. The model behind this is a workstation GPU, and a
// refresh racing the enrichment loop is benign (WAL + INSERT OR REPLACE, last
// writer wins) but two hand refreshes racing each other only spend the GPU
// twice. A second request answers "busy" rather than queueing.
//
// The registry lives on globalThis: Next bundles each route separately, and
// a module-level Map imported by two routes is not guaranteed to be one Map.

import { spawn } from "node:child_process";
import { DatabaseSync } from "node:sqlite";

/** A refresh that hasn't finished by now is killed: model load + two blocks
 * + one retry each is ~30 s warm on gemma3:12b, so five minutes only ever
 * means a hung host. */
export const REFRESH_TIMEOUT_MS = 5 * 60 * 1000;

export type RefreshJobState = "idle" | "running" | "done" | "failed";

export type RefreshJob = {
  state: RefreshJobState;
  /** Why a failed job failed, in words the page can say. */
  reason: string | null;
  finished_ts: number | null;
};

type Job = RefreshJob & { started_ts: number };
type Registry = { jobs: Map<string, Job>; running: string | null };

const g = globalThis as typeof globalThis & {
  __aviaryNotesRefresh?: Registry;
};
const registry: Registry = (g.__aviaryNotesRefresh ??= {
  jobs: new Map(),
  running: null,
});

export type RefreshConfig = { db: string; python: string; root: string };

/** The box may offer a refresh only when every piece is configured: the
 * store, the interpreter that can run the pass (the enrichment venv), the
 * checkout to run it from, and a model to write with. A station without
 * MERLE_OLLAMA answers null, and the page shows no control at all. */
export function refreshConfig(): RefreshConfig | null {
  // Bracket access on purpose: Next can inline a static `process.env.FOO`
  // at build time (stationBrandingFromEnv in lib/aviary.ts has the story),
  // and these must be the unit's live environment.
  const env = process.env;
  const db = env["MERLE_EARL_DB"]?.trim();
  const python = env["MERLE_ENRICH_PYTHON"]?.trim();
  const root = env["MERLE_REPO_ROOT"]?.trim();
  const ollama = env["MERLE_OLLAMA"]?.trim();
  if (!db || !python || !root || !ollama) return null;
  return { db, python, root };
}

function readOne<T>(db: string, sql: string, arg: string): T | undefined {
  let conn: DatabaseSync;
  try {
    conn = new DatabaseSync(db, { readOnly: true });
  } catch {
    return undefined;
  }
  try {
    return conn.prepare(sql).get(arg) as T | undefined;
  } catch {
    return undefined; // a store without the table yet
  } finally {
    conn.close();
  }
}

/** Is this bird in the record? (Only a life-list species can be refreshed --
 * the pass would write nothing for anything else.) */
export function onLifeList(db: string, sci: string): boolean {
  return (
    readOne<{ one: number }>(
      db,
      "SELECT 1 AS one FROM life_list WHERE species_sci = ? LIMIT 1",
      sci,
    ) !== undefined
  );
}

/** The newest write the pass has made for this bird: its sketch's
 * generated_ts or its latest journal entry (#614), whichever is later. A
 * refresh writes both; either moving is "a note landed". */
function generatedTs(db: string, sci: string): number | null {
  const sketch = readOne<{ generated_ts: number }>(
    db,
    "SELECT generated_ts FROM species_analysis WHERE species_sci = ?",
    sci,
  )?.generated_ts;
  const entry = readOne<{ ts: number | null }>(
    db,
    "SELECT MAX(entry_ts) AS ts FROM species_journal WHERE species_sci = ?",
    sci,
  )?.ts;
  const both = [sketch, entry].filter((t): t is number => typeof t === "number");
  return both.length ? Math.max(...both) : null;
}

/** This species' job as the page polls it. A species never refreshed in
 * this server's life is idle. */
export function jobStatus(sci: string): RefreshJob {
  const job = registry.jobs.get(sci);
  if (!job) return { state: "idle", reason: null, finished_ts: null };
  return { state: job.state, reason: job.reason, finished_ts: job.finished_ts };
}

/** Start a refresh, or say why not: "busy" when any refresh is running on
 * this box (including this species' own). */
export function startRefresh(
  cfg: RefreshConfig,
  sci: string,
): "started" | "busy" {
  if (registry.running !== null) return "busy";
  const before = generatedTs(cfg.db, sci);
  const job: Job = {
    state: "running",
    reason: null,
    finished_ts: null,
    started_ts: Date.now(),
  };
  registry.jobs.set(sci, job);
  registry.running = sci;

  let done = false;
  let timedOut = false;
  const finish = (state: "done" | "failed", reason: string | null) => {
    if (done) return; // 'error' and 'close' can both fire for one child
    done = true;
    clearTimeout(timer);
    job.state = state;
    job.reason = reason;
    job.finished_ts = Math.floor(Date.now() / 1000);
    registry.running = null;
    console.log(
      `[refresh] ${sci}: ${state}${reason ? ` (${reason})` : ""} after ` +
        `${Math.round((Date.now() - job.started_ts) / 1000)}s`,
    );
  };

  // One argv element per argument and no shell: the scientific name comes
  // off a URL, and it is never interpreted by anything but argparse.
  const child = spawn(
    cfg.python,
    ["-m", "listener.species_analysis", "--refresh", sci],
    { cwd: cfg.root, env: process.env, stdio: ["ignore", "pipe", "pipe"] },
  );
  const relay = (buf: Buffer) => {
    for (const line of buf.toString("utf8").split(/\r?\n/)) {
      if (line.trim()) console.log(`[refresh] ${line}`);
    }
  };
  child.stdout?.on("data", relay);
  child.stderr?.on("data", relay);
  const timer = setTimeout(() => {
    timedOut = true;
    child.kill("SIGTERM");
  }, REFRESH_TIMEOUT_MS);

  child.on("error", (e) => finish("failed", `could not start: ${e.message}`));
  child.on("close", (code) => {
    if (timedOut) return finish("failed", "timed out");
    if (code !== 0) return finish("failed", `the pass exited ${code}`);
    // Exit 0 also covers "llm-down" (the pass leaves the row alone and
    // says so), so success is judged by the store: a new note landed.
    const after = generatedTs(cfg.db, sci);
    if (after !== null && after !== before) return finish("done", null);
    finish("failed", "no model answered");
  });
  console.log(`[refresh] ${sci}: started`);
  return "started";
}
