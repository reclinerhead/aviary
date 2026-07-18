// Pure shaping for the Aviary (epic #182 Phase 1, issue #183): visit
// grouping, roster/today tallies, clip-path validation, and the sort
// comparators. Everything here is Vitest-covered; the routes and components
// stay thin over it. The bus-facing halves (AudioEvent types + parsing) live
// in lib/bus.ts with the rest of the wire parsing.

import { BirdEvent } from "@/lib/bus";

// Mirror of gate.VISIT_GAP_S -- same-species detections closer than this are
// one visit. The read path applies the SAME grouping the listener applies at
// publish time (gate.VisitTracker), which is what lets pre-#175 per-window
// rows (day one: 25 rows for one singing cardinal) count as one visit
// without touching the store.
export const VISIT_GAP_S = 60;

/** Group one species' detection timestamps into visits and count them.
 * VisitTracker semantics exactly: the gap is measured against the LAST
 * detection in the visit, not its opening, and strictly-greater-than opens
 * a new one. Order-insensitive (sorts a copy). */
export function countVisits(ts: number[], gapS = VISIT_GAP_S): number {
  if (ts.length === 0) return 0;
  const sorted = [...ts].sort((a, b) => a - b);
  let visits = 1;
  for (let i = 1; i < sorted.length; i++)
    if (sorted[i] - sorted[i - 1] > gapS) visits++;
  return visits;
}

export type Tally = { visits: number; today: number };

/** Per-species visit tallies over raw sighting rows. `todayStart` (epoch, or
 * null for "don't count today") buckets a visit by its OPENING detection --
 * a visit that opened at 23:59 and ran past midnight belongs to yesterday,
 * which matches what the listener published (one opening event). */
export function tallyVisits(
  rows: { species_sci: string; ts: number }[],
  todayStart: number | null,
  gapS = VISIT_GAP_S,
): Record<string, Tally> {
  const bySpecies = new Map<string, number[]>();
  for (const r of rows) {
    const list = bySpecies.get(r.species_sci);
    if (list) list.push(r.ts);
    else bySpecies.set(r.species_sci, [r.ts]);
  }
  const out: Record<string, Tally> = {};
  for (const [sci, ts] of bySpecies) {
    ts.sort((a, b) => a - b);
    let visits = 0;
    let today = 0;
    for (let i = 0; i < ts.length; i++) {
      if (i > 0 && ts[i] - ts[i - 1] <= gapS) continue; // same visit
      visits++;
      if (todayStart !== null && ts[i] >= todayStart) today++;
    }
    out[sci] = { visits, today };
  }
  return out;
}

/** One collapsed visit for the profile's recent-visits list. */
export type Visit = {
  ts: number; // the opening detection's moment
  last_ts: number;
  windows: number;
  best: number; // best confidence across the visit's windows
  clip: string | null;
  source: string;
  wind_suspect: boolean;
};

/** Collapse one species' sighting rows (any order) into visits, newest
 * first. Post-#175 a visit is one row and this is the identity; pre-#175
 * rows collapse under the same gap rule the tallies use. The visit carries
 * its opening row's clip/source (the published ones) -- unless the opening
 * clip write failed, where a later window's clip is honestly better than
 * none. */
export function collapseVisits(
  rows: BirdEvent[],
  gapS = VISIT_GAP_S,
): Visit[] {
  const sorted = [...rows].sort((a, b) => a.ts - b.ts);
  const visits: Visit[] = [];
  for (const r of sorted) {
    const open = visits[visits.length - 1];
    if (open && r.ts - open.last_ts <= gapS) {
      open.last_ts = r.ts;
      open.windows++;
      if (r.confidence > open.best) open.best = r.confidence;
      if (open.clip === null) open.clip = r.clip;
      continue;
    }
    visits.push({
      ts: r.ts,
      last_ts: r.ts,
      windows: 1,
      best: r.confidence,
      clip: r.clip,
      source: r.source,
      wind_suspect: r.wind_suspect,
    });
  }
  return visits.reverse();
}

// --- The clips route's path guard -------------------------------------------

// Mirror of gate.clip_relpath's allowlist: Earl scrubs both derived parts to
// [A-Za-z0-9_-] before they touch a filesystem, and writes exactly
// <source>/<epoch>-<Common_name>.wav. The route's path arrives from a URL,
// not from Earl -- hence the guard (the frameFilename reasoning). No dots
// outside the one extension (kills ".."), no separators inside a segment,
// exactly two segments (anything deeper is not a clip Earl wrote).
const CLIP_DIR_SEG = /^[A-Za-z0-9_-]+$/;
const CLIP_FILE_SEG = /^[A-Za-z0-9_-]+\.wav$/;

/** The clip's relative path under MERLE_EARL_CLIPS for a catch-all route's
 * segments, or null when the path is unsafe -- the traversal guard. */
export function clipRelPath(parts: string[]): string | null {
  if (parts.length !== 2) return null;
  const [dir, file] = parts;
  if (!CLIP_DIR_SEG.test(dir) || !CLIP_FILE_SEG.test(file)) return null;
  return `${dir}/${file}`;
}

/** The ticker's URL for one event's clip. Segments are encoded even though
 * Earl's names are allowlisted -- the URL is built from wire data, and
 * encoding costs nothing (an unsafe path just 404s quietly at the route). */
export function clipUrl(rel: string): string {
  return "/clips/" + rel.split("/").map(encodeURIComponent).join("/");
}

// --- Route parameter parsing (the parseRange discipline) --------------------

/** The recent-events limit, clamped at both ends of the wire: a missing or
 * malformed value takes the default, and no request can ask the store for
 * ten thousand rows because of a typo. */
export function parseLimit(raw: string | null, fallback = 50, max = 200): number {
  if (raw === null || raw.trim() === "") return fallback;
  const n = Number(raw);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(1, Math.trunc(n)));
}

/** The roster's "today" boundary -- the client's local midnight, as an epoch.
 * The server can't know the viewer's timezone, so the client says where its
 * day began and this clamps the claim to within two days of `now`: a typo
 * can't relabel the whole archive as "today". Garbage means no today
 * counting, never an error. */
export function parseSince(raw: string | null, now: number): number | null {
  if (raw === null || raw.trim() === "") return null;
  const n = Number(raw);
  if (!Number.isFinite(n)) return null;
  return Math.min(now, Math.max(now - 2 * 86400, Math.trunc(n)));
}

// --- Roster shaping ----------------------------------------------------------

export type RosterEntry = {
  species_sci: string;
  species_common: string;
  first_ts: number;
  first_source: string;
  first_clip: string | null;
  visits: number;
  today: number;
};

/** life_list rows + raw sighting (species, ts) pairs -> the roster the grid
 * renders. Species-common order for determinism; the client owns the sort
 * control. A life_list species with no sighting rows (shouldn't happen --
 * the same insert writes both -- but a hand-edited store is a store) tallies
 * honestly at zero. */
export function shapeRoster(
  lifeRows: {
    species_sci: string;
    species_common: string;
    first_ts: number;
    first_source: string;
    first_clip: string | null;
  }[],
  sightingRows: { species_sci: string; ts: number }[],
  todayStart: number | null,
): RosterEntry[] {
  const tallies = tallyVisits(sightingRows, todayStart);
  return lifeRows
    .map((r) => ({
      ...r,
      visits: tallies[r.species_sci]?.visits ?? 0,
      today: tallies[r.species_sci]?.today ?? 0,
    }))
    .sort((a, b) => a.species_common.localeCompare(b.species_common));
}

// --- The grid's sort control -------------------------------------------------

export type SortKey = "name" | "visits";
export type SortDir = "asc" | "desc";

/** The grid order as species keys. Ties (and the name sort itself) break by
 * common name ascending regardless of direction -- flipping to "most visits
 * first" should not also flip the alphabet inside a tie. Returning keys
 * rather than entries is deliberate: the component stores this order as
 * state and only recomputes it on a sort CLICK, so live count updates land
 * in place and nothing reshuffles on its own (house rule #1). */
export function rosterOrder(
  entries: RosterEntry[],
  key: SortKey,
  dir: SortDir,
): string[] {
  const byName = (a: RosterEntry, b: RosterEntry) =>
    a.species_common.localeCompare(b.species_common, undefined, {
      sensitivity: "base",
    });
  const sorted = [...entries].sort((a, b) => {
    if (key === "name") {
      const cmp = byName(a, b);
      return dir === "asc" ? cmp : -cmp;
    }
    const cmp = a.visits - b.visits;
    if (cmp !== 0) return dir === "asc" ? cmp : -cmp;
    return byName(a, b);
  });
  return sorted.map((e) => e.species_sci);
}

/** Today's Visitors: the roster's today-count survivors, most visits first
 * (ties by name), shaped for the rail's small tiles. Sorted at load only --
 * the component appends live newcomers and bumps counts in place. */
export function todayVisitors(
  entries: RosterEntry[],
): { species_sci: string; species_common: string; count: number }[] {
  return entries
    .filter((e) => e.today > 0)
    .sort(
      (a, b) =>
        b.today - a.today || a.species_common.localeCompare(b.species_common),
    )
    .map((e) => ({
      species_sci: e.species_sci,
      species_common: e.species_common,
      count: e.today,
    }));
}

// --- The recent route's row shaping ------------------------------------------

/** One sightings row -> a wire-shaped detection event ({kind: "detection"},
 * SQLite's 0/1 back to a boolean), so /aviary/recent's body is byte-shaped
 * like a bus payload and audioEventFrom() reads both -- the /weather/history
 * idiom: hydration and the live topic can't drift. */
export function detectionFromRow(row: {
  ts: number;
  source: string;
  species_sci: string;
  species_common: string;
  confidence: number;
  clip: string | null;
  wind_suspect: number;
  rms: number | null;
}): BirdEvent {
  return {
    ts: row.ts,
    source: row.source,
    kind: "detection",
    species_sci: row.species_sci,
    species_common: row.species_common,
    confidence: row.confidence,
    clip: row.clip,
    wind_suspect: Boolean(row.wind_suspect),
    rms: row.rms,
  };
}
