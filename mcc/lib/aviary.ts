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

// --- The portrait route's name guard ----------------------------------------

// Mirror of species_profile.image_filename's scrub, byte-for-byte: the pass
// writes species/<scrubbed_sci>.jpg and this route re-derives the same name
// from the URL's species. One regex both sides or the portraits go missing.
const SPECIES_UNSAFE = /[^A-Za-z0-9_-]+/g;

/** 'Cardinalis cardinalis' -> 'Cardinalis_cardinalis.jpg'; null when the
 * name scrubs to nothing. Hostile input scrubs flat -- '..' becomes '_',
 * never a path step. */
export function speciesImageName(sci: string): string | null {
  const safe = sci
    .trim()
    .replace(SPECIES_UNSAFE, "_")
    .replace(/^_+|_+$/g, "");
  return safe ? `${safe}.jpg` : null;
}

/** The grid/profile URL for one species' portrait. */
export function portraitUrl(sci: string): string {
  return `/aviary/portrait/${encodeURIComponent(sci)}`;
}

// --- Portrait framing (issue #185) ------------------------------------------

/** `object-position` for a portrait inside a FIXED box (grid tiles at 4:3,
 * ticker thumbs at 1:1), where `object-cover` must crop something.
 *
 * **Crop from the top when the image is taller than its box.** Wikipedia's
 * bird photos are shot with the bird upright and its head high in the frame,
 * so a centered crop of a portrait-orientation source cuts off the head --
 * the single most identifying part of the bird (measured on the real life
 * list: Blue Jay and Cedar Waxwing both decapitated at 4:3). Landscape and
 * square sources keep the centered crop, which is right for them: there the
 * bird is centered and the cropping happens at the sides.
 *
 * Unknown dimensions (a #184-era row awaiting backfill) take the centered
 * default -- the old behaviour, never a guess about a shape we don't know. */
export function cropPosition(
  w: number | null | undefined,
  h: number | null | undefined,
  boxAspect: number,
): string {
  if (!w || !h || w <= 0 || h <= 0) return "center";
  return h / w > 1 / boxAspect ? "top" : "center";
}

/** The aspect ratio the profile's floated figure reserves, as a CSS
 * `aspect-ratio` value. With real dimensions the figure takes the image's
 * OWN shape, so the profile crops nothing at all -- the whole bird, always
 * in frame -- and reserving it before load keeps house rule #1 (the box is
 * the right shape from first paint, so the photo landing shifts nothing).
 * Unknown dimensions fall back to the 4:3 the page has always used. */
export function portraitAspect(
  w: number | null | undefined,
  h: number | null | undefined,
): string {
  if (!w || !h || w <= 0 || h <= 0) return "4 / 3";
  return `${w} / ${h}`;
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
  // The enrichment pass's columns (#184), optional twice over: a pre-pass
  // earl.db has no species_profile table (the roster route falls back to
  // the bare life list) and an un-enriched species LEFT JOINs to NULLs.
  description?: string | null;
  image_file?: string | null;
  image_source?: string | null;
  image_attribution?: string | null;
  // The portrait's real shape (#185). NULL on #184-era rows until the
  // pass's backfill arm runs -- hence every consumer degrades to the
  // fixed-box fallback rather than assuming a ratio.
  image_w?: number | null;
  image_h?: number | null;
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

// --- The visits-over-time chart (issue #185) ---------------------------------

/** The chart's opening span: ~30 days of daily bars. Fixed forever after --
 * only the window's POSITION moves (the station chart's rule: the span is
 * the viewer's setting, never something a clamp gets to change). */
export const VISITS_SPAN_S = 30 * 86400;
/** How much record to ask for when a drag reaches past what we hold. Far
 * more generous than the station's 7 days because the payload is one
 * integer per visit, not a row per 5 minutes -- a season at a time keeps a
 * quiet winter from reading as the end of the record. */
export const VISITS_CHUNK_S = 120 * 86400;
/** A press that travels less than this is a tap, not a drag (#106's rule):
 * the crosshair placement a touchscreen can't express as hover. */
export const TAP_SLOP_PX = 4;
/** Y-axis floor: a species with a single visit a day shouldn't draw a
 * full-height bar and read as a swarm (the wind-axis-floor reasoning). */
export const VISITS_CEIL_FLOOR = 4;

export type DayBar = {
  /** Local midnight opening the day (epoch seconds). */
  ts: number;
  count: number;
};

/** Visit-opening timestamps -> counts per the VIEWER's local day.
 *
 * Bucketing is client-side deliberately, and this is a correction to what
 * #185 sketched: the server cannot know the viewer's timezone, which is the
 * exact lesson Phase 1 already encoded in `parseSince` (the client says
 * where its day begins). The server still owns the subtle half -- the 60s
 * visit grouping that must match `gate.VisitTracker` -- and ships one
 * integer per visit, so this is bucketing, not analysis.
 *
 * Days inside the window with no visits are **zero bars, honestly**: a bird
 * that didn't come is data, not a gap. Days before `since` (first-heard) are
 * omitted entirely -- that's absence of record, not absence of bird -- which
 * is what keeps a lifer's chart from claiming a year of silence it never
 * observed. `setDate`/re-floor stepping, so DST's 23/25h days can't skip or
 * double a bucket. */
export function dayBuckets(
  visitTs: number[],
  ts0: number,
  ts1: number,
  since: number | null = null,
): DayBar[] {
  if (ts1 <= ts0) return [];
  const counts = new Map<number, number>();
  for (const ts of visitTs) {
    const d = new Date(ts * 1000);
    d.setHours(0, 0, 0, 0);
    const key = Math.floor(d.getTime() / 1000);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  // The first local midnight at or before the window's left edge.
  const cursor = new Date(ts0 * 1000);
  cursor.setHours(0, 0, 0, 0);
  // Days before the record began aren't rendered at all.
  const floor = since === null ? null : dayStart(since);
  const bars: DayBar[] = [];
  while (cursor.getTime() / 1000 < ts1) {
    const ts = Math.floor(cursor.getTime() / 1000);
    if (ts >= ts0 && (floor === null || ts >= floor))
      bars.push({ ts, count: counts.get(ts) ?? 0 });
    cursor.setDate(cursor.getDate() + 1);
    cursor.setHours(0, 0, 0, 0); // re-floor: DST days are 23h or 25h
  }
  return bars;
}

/** Local midnight opening the day containing `ts`. */
export function dayStart(ts: number): number {
  const d = new Date(ts * 1000);
  d.setHours(0, 0, 0, 0);
  return Math.floor(d.getTime() / 1000);
}

/** Slide the window back inside the walls WITHOUT resizing it -- the
 * station's `clampWindow` semantics (#106), minus the forecast half: the
 * right wall is simply `newest` (today), because a bird chart has no future
 * to show. When the walls are closer together than the span the RIGHT wall
 * wins, which is the young-record's normal state, not an edge case: the
 * default window must stay exactly reachable on day one. */
export function clampVisitWindow(
  ts0: number,
  ts1: number,
  oldest: number,
  newest: number,
): { ts0: number; ts1: number } {
  const span = ts1 - ts0;
  if (newest - oldest < span) return { ts0: newest - span, ts1: newest };
  if (ts1 > newest) return { ts0: newest - span, ts1: newest };
  if (ts0 < oldest) return { ts0: oldest, ts1: oldest + span };
  return { ts0, ts1 };
}

export type VisitTick = { ts: number; frac: number; label: string };

/** Axis gridlines for the visits window: one per local week boundary
 * (Sundays) labeled by date, so a 30-day window reads as a calendar without
 * 30 labels colliding. `dayTicks`' DST-safe stepping, a week at a time. */
export function visitTicks(ts0: number, ts1: number): VisitTick[] {
  if (ts1 <= ts0) return [];
  const ticks: VisitTick[] = [];
  const d = new Date(ts0 * 1000);
  d.setHours(0, 0, 0, 0);
  // Advance to the first Sunday at or after the left edge.
  while (d.getDay() !== 0) d.setDate(d.getDate() + 1);
  while (d.getTime() / 1000 < ts1) {
    const ts = Math.floor(d.getTime() / 1000);
    if (ts > ts0)
      ticks.push({
        ts,
        frac: (ts - ts0) / (ts1 - ts0),
        label: d
          .toLocaleDateString(undefined, { month: "short", day: "numeric" })
          .toLowerCase(),
      });
    d.setDate(d.getDate() + 7);
    d.setHours(0, 0, 0, 0);
  }
  return ticks;
}

/** Y-axis ceiling: the busiest day in view, floored so a quiet species reads
 * quiet, and rounded up to a clean step for the label (the `seriesCeil`
 * recipe). */
export function visitsCeil(bars: DayBar[], floor = VISITS_CEIL_FLOOR): number {
  const max = Math.max(0, ...bars.map((b) => b.count));
  const step = max > 40 ? 10 : max > 12 ? 5 : 2;
  return Math.max(floor, Math.ceil(max / step) * step);
}

/** The bar nearest a pointer fraction across the window -- the crosshair
 * snaps to a real day, never an interpolated instant (`nearestPoint`'s
 * rule). Null when there's nothing to snap to. */
export function nearestBar(
  bars: DayBar[],
  ts: number,
): DayBar | null {
  if (bars.length === 0) return null;
  let best = bars[0];
  for (const b of bars)
    if (Math.abs(b.ts + 43200 - ts) < Math.abs(best.ts + 43200 - ts)) best = b;
  return best;
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
