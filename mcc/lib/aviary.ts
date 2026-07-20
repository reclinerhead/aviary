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

export type Tally = { visits: number; today: number; week: number };

/** The standings' trailing week (issue #220): the 7 local days ending today,
 * derived from the client's own midnight rather than a second timezone
 * param. Subtracting fixed days from a LOCAL midnight is the arithmetic the
 * chart code refuses to do -- across a DST change the boundary lands an hour
 * off the client's actual midnight. Accepted here, stated plainly: this
 * feeds a leaderboard, the skew touches one boundary hour two nights a year,
 * and the alternative is new timezone plumbing for a rank. The chart's
 * bucketing rules are untouched. */
export function weekWindowStart(todayStart: number): number {
  return todayStart - 6 * 86400;
}

/** Per-species visit tallies over raw sighting rows. `todayStart` (epoch, or
 * null for "don't count today") buckets a visit by its OPENING detection --
 * a visit that opened at 23:59 and ran past midnight belongs to yesterday,
 * which matches what the listener published (one opening event). The week
 * tally (#220) rides the same walk, windowed by weekWindowStart. */
export function tallyVisits(
  rows: { species_sci: string; ts: number }[],
  todayStart: number | null,
  gapS = VISIT_GAP_S,
): Record<string, Tally> {
  const weekStart = todayStart === null ? null : weekWindowStart(todayStart);
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
    let week = 0;
    for (let i = 0; i < ts.length; i++) {
      if (i > 0 && ts[i] - ts[i - 1] <= gapS) continue; // same visit
      visits++;
      if (todayStart !== null && ts[i] >= todayStart) today++;
      if (weekStart !== null && ts[i] >= weekStart) week++;
    }
    out[sci] = { visits, today, week };
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

// Mirror of clip_enhance.ENH_SUFFIX (issue #190): the pass writes a sibling
// <stem>-enh.wav beside every clip, never in place.
const ENH_SUFFIX = "-enh.wav";

/** The enhanced sibling's path for a clip, or null when the argument is
 * already one (there is no -enh-enh).
 *
 * Note what does NOT change for this: the route's guard. `CLIP_FILE_SEG`
 * already admits a `-enh` stem, because `-` has been in the allowlist since
 * day one -- the sibling is an ordinary clip name by construction, not an
 * exception carved into the guard. The traversal rules are untouched, which
 * is exactly the property #190 asked for. */
export function enhancedRelPath(rel: string): string | null {
  if (rel.endsWith(ENH_SUFFIX) || !rel.endsWith(".wav")) return null;
  return rel.slice(0, -".wav".length) + ENH_SUFFIX;
}

/** The enhanced sibling's URL, or null when there can't be one. Whether the
 * file actually EXISTS is not knowable from the path -- the player asks for
 * it and falls back to the raw clip if the route 404s, which is the same
 * "file existence is the source of truth" rule the pass itself runs on. */
export function enhancedClipUrl(rel: string): string | null {
  const enh = enhancedRelPath(rel);
  return enh === null ? null : clipUrl(enh);
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

// --- Species removal (issue #216) -------------------------------------------

// Mirror of the pass's portrait shelf: species_profile.image_file holds a
// scrubbed <name>.jpg -- one dot, no separators -- so anything else in the
// column is a hand-edited row this must never turn into a path.
const PORTRAIT_FILE = /^[A-Za-z0-9_-]+\.jpg$/;
const SPECIES_DIR = "species";

/** The files a species removal dooms, as relative paths under
 * MERLE_EARL_CLIPS: every sighting clip and the lifer's first_clip (deduped
 * -- the first sighting's clip usually IS first_clip), each with its -enh
 * sibling named unconditionally (the enhancement pass keeps no registry, so
 * the filesystem is asked by deleting: a sibling never written is a missing
 * file, and missing files are not errors), plus the portrait on the
 * species/ shelf. Every row-held path re-runs the clips route's own
 * traversal guard before it may name a file -- rows normally hold exactly
 * what Earl wrote, but this feeds the MCC's first delete, and a hand-edited
 * row must skip quietly rather than escape the clips dir. */
export function doomedFiles(
  clips: (string | null | undefined)[],
  imageFile: string | null | undefined,
): string[] {
  const out = new Set<string>();
  for (const clip of clips) {
    if (!clip) continue;
    const rel = clipRelPath(clip.split("/"));
    if (rel === null) continue;
    out.add(rel);
    const enh = enhancedRelPath(rel);
    if (enh !== null) out.add(enh);
  }
  if (imageFile && PORTRAIT_FILE.test(imageFile))
    out.add(`${SPECIES_DIR}/${imageFile}`);
  return [...out];
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

/** The archive's species filter (#211): a comma-separated list of exact
 * species_sci values -- scientific names never contain commas, which is what
 * makes the encoding safe. Trimmed, de-duplicated, capped (a URL can't ask
 * the store to build a 500-placeholder IN clause); empty means "no filter",
 * never an error. The names stay exact-match parameters downstream, so they
 * need no scrubbing here -- the same reasoning as the single `species` param. */
export function parseSpeciesFilter(raw: string | null, max = 40): string[] {
  if (raw === null) return [];
  const seen = new Set<string>();
  for (const part of raw.split(",")) {
    const name = part.trim();
    if (name !== "") seen.add(name);
    if (seen.size >= max) break;
  }
  return [...seen];
}

/** The archive's pagination cursor (#211): "rows at or before this epoch".
 * INCLUSIVE on purpose -- the client re-requests its own oldest row and
 * dedupes by audioEventKey, so a same-second sighting straddling a page
 * boundary is a no-op instead of a dropped bird (the hydration/live merge
 * trick, reused as overlap tolerance). Garbage or a non-positive value means
 * "no cursor" (newest first, exactly as before); a far-future value clamps
 * to now plus a day, which is the same thing said politely. */
export function parseBefore(raw: string | null, now: number): number | null {
  if (raw === null || raw.trim() === "") return null;
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0) return null;
  return Math.min(now + 86400, Math.trunc(n));
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
  week: number;
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
      week: tallies[r.species_sci]?.week ?? 0,
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

/** Overview mode's span: ~30 days of daily bars, the chart's opening state.
 *
 * This constant once read "fixed forever after -- only the window's POSITION
 * moves." #204 made that half-true and the correction is worth stating: the
 * span is still never something a CLAMP gets to change (the station chart's
 * rule, and `clampVisitWindow` still takes the span as given), but it IS now
 * something the VIEWER gets to change, by switching modes. Two spans, each
 * fixed within its mode; a clamp may only slide a window, never resize it. */
export const VISITS_SPAN_S = 30 * 86400;
/** Detail mode's span (#204): 48 hours of hourly counts. Two days rather
 * than one so a dawn/dusk rhythm shows up as a repeating shape -- a single
 * day's curve is an anecdote, two is the beginning of a pattern. */
export const DETAIL_SPAN_S = 48 * 3600;
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

/** One bucket of the visits chart: the local instant opening it, and how
 * many visits opened inside it. Named for the mark, not the unit -- #204
 * added hourly buckets and the shape didn't change, only its width. */
export type VisitBar = {
  /** Local start of the bucket (epoch seconds): midnight for a day bar,
   * the top of the hour for an hour bar. */
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
): VisitBar[] {
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
  const bars: VisitBar[] = [];
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

/** Top of the local hour containing `ts`.
 *
 * Not `Math.floor(ts / 3600) * 3600`, and the difference is not pedantry:
 * India (+05:30) and Newfoundland (-03:30) put their hour boundaries on the
 * half-hour in UTC, so the arithmetic version would draw every bucket 30
 * minutes off the viewer's own clock -- the exact class of bug the
 * client-buckets rule exists to prevent. */
export function hourStart(ts: number): number {
  const d = new Date(ts * 1000);
  d.setMinutes(0, 0, 0);
  return Math.floor(d.getTime() / 1000);
}

/** Visit-opening timestamps -> counts per the VIEWER's local hour (#204).
 *
 * `dayBuckets`' rules exactly, one unit finer, and deliberately the same
 * function shape: empty hours inside the window are honest zeros, hours
 * before `since` (first-heard) are omitted entirely, and a visit is bucketed
 * by its OPENING detection -- so a visit that opened at 6:59 and ran past
 * seven belongs to the six o'clock hour, matching what the listener
 * published and what the day bars already claim.
 *
 * DST needs no special case here, unlike the day stepping: advancing the
 * cursor by 3600 REAL seconds and re-flooring lands on every hour the
 * viewer's clock actually experienced. Spring forward yields 23 buckets
 * because 2am never happened; fall back yields 25, the repeated 1am hours
 * landing in distinct buckets because they are distinct absolute hours. */
export function hourBuckets(
  visitTs: number[],
  ts0: number,
  ts1: number,
  since: number | null = null,
): VisitBar[] {
  if (ts1 <= ts0) return [];
  const counts = new Map<number, number>();
  for (const ts of visitTs) {
    const key = hourStart(ts);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  const floor = since === null ? null : hourStart(since);
  const bars: VisitBar[] = [];
  let cursor = hourStart(ts0);
  while (cursor < ts1) {
    if (cursor >= ts0 && (floor === null || cursor >= floor))
      bars.push({ ts: cursor, count: counts.get(cursor) ?? 0 });
    cursor = hourStart(cursor + 3600);
  }
  return bars;
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

/** Axis gridlines for detail mode (#204): one every six local hours, so 48
 * hours reads as morning/noon/evening/midnight without 48 labels colliding.
 * Stepping walks REAL hours and re-floors for the same reason `hourBuckets`
 * does -- a spring-forward day has no 6am-to-noon of the usual length, and
 * the gridline has to land where the clock actually said six. */
export function visitHourTicks(ts0: number, ts1: number): VisitTick[] {
  if (ts1 <= ts0) return [];
  const ticks: VisitTick[] = [];
  let cursor = hourStart(ts0);
  // Walked an hour at a time rather than six at a stride: a spring-forward
  // day is 23 hours long, so a six-hour stride would land on 7am and stay
  // off the six-hour grid for the rest of the window.
  while (cursor < ts1) {
    const d = new Date(cursor * 1000);
    const h = d.getHours();
    if (cursor > ts0 && h % 6 === 0) {
      ticks.push({
        ts: cursor,
        frac: (cursor - ts0) / (ts1 - ts0),
        // Midnight names its day; the rest name their hour. A 48-hour
        // window otherwise reads as eight anonymous times with no way to
        // tell which of the two days you're looking at.
        label:
          h === 0
            ? d
                .toLocaleDateString(undefined, {
                  month: "short",
                  day: "numeric",
                })
                .toLowerCase()
            : d
                .toLocaleTimeString(undefined, { hour: "numeric" })
                .toLowerCase()
                .replace(/\s+/g, ""),
      });
    }
    cursor = hourStart(cursor + 3600);
  }
  return ticks;
}

/** Y-axis ceiling: the busiest bucket in view, floored so a quiet species
 * reads quiet, and rounded up to a clean step for the label (the `seriesCeil`
 * recipe). */
export function visitsCeil(bars: VisitBar[], floor = VISITS_CEIL_FLOOR): number {
  const max = Math.max(0, ...bars.map((b) => b.count));
  const step = max > 40 ? 10 : max > 12 ? 5 : 2;
  return Math.max(floor, Math.ceil(max / step) * step);
}

/** The bar nearest a pointer instant -- the crosshair snaps to a real
 * bucket, never an interpolated moment (`nearestPoint`'s rule). Distance is
 * measured from each bucket's MIDDLE, hence `halfWidth`: half a day for the
 * day bars it was written for, half an hour for #204's hourly ones. Null
 * when there's nothing to snap to. */
export function nearestBar(
  bars: VisitBar[],
  ts: number,
  halfWidth = 43200,
): VisitBar | null {
  if (bars.length === 0) return null;
  let best = bars[0];
  for (const b of bars)
    if (Math.abs(b.ts + halfWidth - ts) < Math.abs(best.ts + halfWidth - ts))
      best = b;
  return best;
}

// --- Detail mode's curve (#204) ---------------------------------------------

export type Pt = { x: number; y: number };
/** One cubic Bezier segment: from `p0` to `p1`, bent by `c1`/`c2`. */
export type Seg = { p0: Pt; c1: Pt; c2: Pt; p1: Pt };

/** Hourly counts -> monotone cubic Bezier segments, the smooth line detail
 * mode draws.
 *
 * Fritsch-Carlson monotone Hermite interpolation, and the choice is about
 * honesty rather than looks. A plain Catmull-Rom or natural spline through
 * sparse counts OVERSHOOTS: a quiet 3am between two busy hours dips the
 * curve below the baseline and the chart shows negative visits, while an
 * isolated peak rings above the count that produced it. Fritsch-Carlson
 * zeroes the tangent at every local extremum and clamps the rest to three
 * times the local secant, which bounds each control point inside its own
 * segment's y-range -- and a cubic Bezier never leaves the convex hull of
 * its control points, so the drawn curve cannot exceed the counts it
 * interpolates in either direction. That is a property the tests assert
 * directly, not a hope about how it renders.
 *
 * Y is passed in whatever space the caller draws in; the guarantee is about
 * neighbouring values, so it holds for SVG's inverted axis unchanged. */
export function smoothSegments(pts: Pt[]): Seg[] {
  const n = pts.length;
  if (n < 2) return [];
  // Secant slopes between neighbours.
  const d: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    const h = pts[i + 1].x - pts[i].x;
    d.push(h === 0 ? 0 : (pts[i + 1].y - pts[i].y) / h);
  }
  // Tangents: one-sided at the ends, averaged inside.
  const m: number[] = [d[0]];
  for (let i = 1; i < n - 1; i++) m.push((d[i - 1] + d[i]) / 2);
  m.push(d[n - 2]);
  // The clamp that buys the no-overshoot guarantee.
  for (let i = 0; i < n - 1; i++) {
    if (d[i] === 0) {
      // A flat run: both ends go flat, or the curve would bulge across it.
      m[i] = 0;
      m[i + 1] = 0;
      continue;
    }
    const a = m[i] / d[i];
    const b = m[i + 1] / d[i];
    // A tangent pointing against its secant would create a local wobble.
    if (a < 0) m[i] = 0;
    if (b < 0) m[i + 1] = 0;
    const s = a * a + b * b;
    if (s > 9) {
      const t = 3 / Math.sqrt(s);
      m[i] = t * a * d[i];
      m[i + 1] = t * b * d[i];
    }
  }
  const segs: Seg[] = [];
  for (let i = 0; i < n - 1; i++) {
    const h = pts[i + 1].x - pts[i].x;
    segs.push({
      p0: pts[i],
      c1: { x: pts[i].x + h / 3, y: pts[i].y + (m[i] * h) / 3 },
      c2: { x: pts[i + 1].x - h / 3, y: pts[i + 1].y - (m[i + 1] * h) / 3 },
      p1: pts[i + 1],
    });
  }
  return segs;
}

/** The segments as an SVG path. Empty string for nothing to draw, which is
 * a valid `d` and renders as no ink -- the caller doesn't need a branch. */
export function smoothPath(pts: Pt[]): string {
  const segs = smoothSegments(pts);
  if (segs.length === 0) return "";
  const r = (v: number) => Math.round(v * 100) / 100;
  let out = `M${r(segs[0].p0.x)},${r(segs[0].p0.y)}`;
  for (const s of segs)
    out += `C${r(s.c1.x)},${r(s.c1.y)} ${r(s.c2.x)},${r(s.c2.y)} ${r(s.p1.x)},${r(s.p1.y)}`;
  return out;
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

// --- The event archive (issue #211) ------------------------------------------

export type DayGroup<T extends { ts: number }> = {
  /** Local midnight opening this group's day -- the header's identity. */
  day: number;
  rows: T[];
};

/** Newest-first rows -> day sections for the archive's sticky headers, split
 * at the VIEWER's local midnight (`dayStart` -- the client-buckets rule; the
 * server never sees a day boundary). Grouping is by consecutive runs, which
 * on sorted input is exact and on anything else refuses to invent an order:
 * this function groups, it does not sort. DST needs no case here at all --
 * whatever length the local day was, `dayStart` names it once. */
export function dayGroups<T extends { ts: number }>(rows: T[]): DayGroup<T>[] {
  const groups: DayGroup<T>[] = [];
  for (const row of rows) {
    const day = dayStart(row.ts);
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.rows.push(row);
    else groups.push({ day, rows: [row] });
  }
  return groups;
}

/** The next page's cursor from the oldest loaded row. Ordinarily that IS the
 * cursor (inclusive query + key dedupe, see `parseBefore`); the guard handles
 * the pathological page whose rows all share one second -- re-asking at the
 * same cursor would loop forever, so it steps past it and accepts the
 * theoretical loss over the certain hang. */
export function nextBefore(oldestTs: number, prev: number | null): number {
  return prev !== null && oldestTs >= prev ? prev - 1 : oldestTs;
}

// --- The field desk (issue #220) ----------------------------------------------
//
// Standings, records, and margin-figure shaping for the species profile's
// bottom half. Two data regimes, deliberately distinct: standings and yard
// records derive CLIENT-SIDE from payloads the page already holds (roster
// entries, visit openings) using the viewer-local day rules above, while the
// margin figures render the pass's stored stats_json VERBATIM -- the same
// numbers the prose was written from, server-local hours and all, so figure
// and prose cannot disagree (#186's auditability design paying rent).

export type Standing = {
  /** Competition ranking: ties share a rank, the next rank skips. */
  rank: number;
  of: number;
  count: number;
  /** Whether another species shares this exact count (the "tied" phrasing). */
  tied: boolean;
  /** The nearest species strictly ahead -- the rival line's target. Null
   * when leading. Ties among the ahead break alphabetically, determinism
   * over drama. */
  leader: { species_common: string; count: number } | null;
};

/** One species' standing in the yard by any count the roster carries.
 * Computed at load, never on live events -- ranks reshuffling under the
 * reader is house rule #1 broken with a scoreboard. */
export function standingFor<
  T extends { species_sci: string; species_common: string },
>(entries: T[], sci: string, count: (e: T) => number): Standing | null {
  const me = entries.find((e) => e.species_sci === sci);
  if (!me) return null;
  const mine = count(me);
  const ahead = entries.filter((e) => count(e) > mine);
  let leader: Standing["leader"] = null;
  if (ahead.length > 0) {
    const nearest = Math.min(...ahead.map(count));
    const holder = ahead
      .filter((e) => count(e) === nearest)
      .sort((a, b) => a.species_common.localeCompare(b.species_common))[0];
    leader = { species_common: holder.species_common, count: nearest };
  }
  return {
    rank: 1 + ahead.length,
    of: entries.length,
    count: mine,
    tied: entries.some((e) => e.species_sci !== sci && count(e) === mine),
    leader,
  };
}

/** The rivalry line under a rank: what stands between this bird and the next
 * rung. `quiet` names the zero-count state ("no visits this week" for the
 * week tile, never a rank nobody earned). */
export function rivalLine(s: Standing, quiet: string): string {
  if (s.count === 0) return quiet;
  if (s.leader === null)
    return s.tied ? "tied for the lead" : "leading the yard";
  const gap = s.leader.count - s.count;
  return `${gap === 1 ? "one visit" : `${gap} visits`} behind the ${s.leader.species_common}`;
}

/** "1 in 6 of everything Earl hears" -- the nearest whole ratio of the
 * yard's total visits to this species'. Null when either side is zero
 * (nothing honest to say); a species that IS the majority says so instead
 * of claiming a silly "1 in 1". */
export function shareOfYard(totalVisits: number, mine: number): string | null {
  if (mine <= 0 || totalVisits <= 0) return null;
  const n = Math.round(totalVisits / mine);
  if (n <= 1) return "most of everything Earl hears";
  return `1 in ${n} of everything Earl hears`;
}

export type YardRecords = {
  busiestDay: { day: number; count: number } | null;
  /** Consecutive local days heard, ending today -- or yesterday, because an
   * unfinished day hasn't broken anything yet. 0 when the streak is over. */
  streak: number;
  /** The longest stretch without a visit, in whole days -- including the
   * silence running right now, which for a bird gone a month IS the record. */
  longestSilenceDays: number;
  /** Seconds into the local day of the earliest/latest opening ever. */
  earliest: number | null;
  latest: number | null;
};

/** Step one local day back from a local midnight, DST-safe (the re-floor
 * rule -- a 23/25h day is one day, not an hour's drift). */
function prevDay(dayTs: number): number {
  const d = new Date(dayTs * 1000);
  d.setDate(d.getDate() - 1);
  d.setHours(0, 0, 0, 0);
  return Math.floor(d.getTime() / 1000);
}

/** Seconds into the viewer's local day -- `Date` getters, never `ts % 86400`
 * (which is a UTC claim wearing a local costume). */
export function secondsIntoDay(ts: number): number {
  const d = new Date(ts * 1000);
  return d.getHours() * 3600 + d.getMinutes() * 60 + d.getSeconds();
}

/** The records panel, from one species' visit openings (any order) in the
 * viewer's local calendar. Empty input is all-nulls-and-zeros -- the
 * placeholders' state, never NaN. */
export function yardRecords(visitTs: number[], now: number): YardRecords {
  if (visitTs.length === 0)
    return {
      busiestDay: null,
      streak: 0,
      longestSilenceDays: 0,
      earliest: null,
      latest: null,
    };
  const sorted = [...visitTs].sort((a, b) => a - b);

  const perDay = new Map<number, number>();
  for (const ts of sorted) {
    const day = dayStart(ts);
    perDay.set(day, (perDay.get(day) ?? 0) + 1);
  }
  // Busiest day: highest count; ties go to the most RECENT day -- "again
  // yesterday" is the interesting claim, and determinism either way.
  let busiestDay: YardRecords["busiestDay"] = null;
  for (const [day, count] of perDay)
    if (
      busiestDay === null ||
      count > busiestDay.count ||
      (count === busiestDay.count && day > busiestDay.day)
    )
      busiestDay = { day, count };

  // The current streak: walk back from today (or yesterday -- today merely
  // being unfinished doesn't break a streak) through consecutive heard days.
  const today = dayStart(now);
  let cursor = perDay.has(today) ? today : prevDay(today);
  let streak = 0;
  while (perDay.has(cursor)) {
    streak++;
    cursor = prevDay(cursor);
  }

  let longestGapS = Math.max(0, now - sorted[sorted.length - 1]);
  for (let i = 1; i < sorted.length; i++)
    longestGapS = Math.max(longestGapS, sorted[i] - sorted[i - 1]);

  let earliest: number | null = null;
  let latest: number | null = null;
  for (const ts of sorted) {
    const s = secondsIntoDay(ts);
    if (earliest === null || s < earliest) earliest = s;
    if (latest === null || s > latest) latest = s;
  }

  return {
    busiestDay,
    streak,
    longestSilenceDays: Math.floor(longestGapS / 86400),
    earliest,
    latest,
  };
}

/** Lifer number: where this species falls in first-heard order. Ties (a
 * hand-edited store) break by scientific name -- a number, deterministically,
 * never two birds both claiming № 12. */
export function liferNumber(
  entries: { species_sci: string; first_ts: number }[],
  sci: string,
): { n: number; of: number } | null {
  const order = [...entries].sort(
    (a, b) => a.first_ts - b.first_ts || a.species_sci.localeCompare(b.species_sci),
  );
  const i = order.findIndex((e) => e.species_sci === sci);
  return i === -1 ? null : { n: i + 1, of: order.length };
}

/** New Arrivals' windows (#224; the short one tightened to a day in #226):
 * the last 24 hours by default, the last week on the toggle. Plain trailing
 * spans off a mount-time `now` -- the cards never silently drop out
 * mid-session as the clock advances; the window moves on reload or toggle
 * (the midnight-state rule). */
export const ARRIVALS_24H_S = 24 * 3600;
export const ARRIVALS_WEEK_S = 7 * 86400;

/** The New Arrivals cut (#224): species first heard at or after `sinceTs`,
 * newest first (ties by name, determinism over drama). Pure filter over the
 * roster the page already holds -- a lifer is an event the moment its
 * first_ts says so, and nothing else needs asking. */
export function newArrivals(
  entries: RosterEntry[],
  sinceTs: number,
): RosterEntry[] {
  return entries
    .filter((e) => e.first_ts >= sinceTs)
    .sort(
      (a, b) =>
        b.first_ts - a.first_ts ||
        a.species_common.localeCompare(b.species_common),
    );
}

/** The slice of stats_json the margin figures read. Everything optional:
 * the stats are whatever the pass stored, and a missing piece renders a
 * reserved placeholder, never a crash. */
export type AnalysisStats = {
  total_visits?: number;
  hours?: number[] | null;
  peak_window?: {
    start_hour: number;
    end_hour: number;
  } | null;
  weather?: {
    visits_matched?: number;
    enough?: boolean;
    conditions?: StatsFinding[] | null;
    temperature?: StatsFinding[] | null;
  } | null;
};

export type StatsFinding = {
  bucket: string;
  effect: number | null;
  thin: boolean;
};

export type RhythmCell = { frac: number; peak: boolean };

/** stats_json's 24 hour counts -> bar fractions for the rhythm strip, peak
 * window flagged (wrapping midnight the way peak_window does -- an owl's
 * peak is one stretch, not two). SERVER-local hours by design: these must
 * match the prose beside them, not the chart below (see the section note).
 * Null when the stats carry no usable histogram; all-zero hours are honest
 * flat cells, not NaN. */
export function rhythmStrip(stats: AnalysisStats | null): RhythmCell[] | null {
  const hours = stats?.hours;
  if (!Array.isArray(hours) || hours.length !== 24) return null;
  const max = Math.max(...hours);
  const peak = stats?.peak_window ?? null;
  const inPeak = (h: number): boolean => {
    if (!peak) return false;
    const { start_hour: a, end_hour: b } = peak;
    return a < b ? h >= a && h < b : h >= a || h < b;
  };
  return hours.map((count, h) => ({
    frac: max > 0 ? count / max : 0,
    peak: inPeak(h),
  }));
}

export type StatChip = { label: string; pct: number; thin: boolean };

/** stats_json's weather findings -> the weather page's margin chips.
 * Rendered only when the pass itself judged the sample sufficient
 * (`weather.enough` -- the prose hedges below that line, and a confident
 * chip over hedged prose would be the figure contradicting the writing).
 * Skips the unknown bucket, null effects, and about-average findings
 * (|effect| < 10%, the prose's own threshold); strongest first, capped --
 * a margin is a margin. Thin findings KEEP their flag and render hedged,
 * the show-with-hedging rule in pixels. */
export function weatherChips(stats: AnalysisStats | null, cap = 4): StatChip[] {
  const w = stats?.weather;
  if (!w?.enough) return [];
  const findings = [...(w.conditions ?? []), ...(w.temperature ?? [])];
  return findings
    .filter(
      (f): f is StatsFinding & { effect: number } =>
        f.bucket !== "unknown" &&
        typeof f.effect === "number" &&
        Math.abs(Math.round(f.effect * 100)) >= 10,
    )
    .map((f) => ({
      label: f.bucket,
      pct: Math.round(f.effect * 100),
      thin: f.thin,
    }))
    .sort((a, b) => Math.abs(b.pct) - Math.abs(a.pct) || a.label.localeCompare(b.label))
    .slice(0, cap);
}

/** A date input's "yyyy-mm-dd" -> the last second of that LOCAL day: the
 * jump-to-date cursor (#211). Parsed by hand because `new Date(string)`
 * reads a bare date as UTC midnight -- off by a whole day for every viewer
 * west of Greenwich. End-of-day is found by stepping to the NEXT local
 * midnight and backing off one second, so a 23- or 25-hour DST day lands
 * exactly (the re-floor rule, `dayBuckets`' comment). Null for anything
 * that isn't a real calendar date -- a typo shows the latest record,
 * never an error. */
export function dayAnchor(value: string): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim());
  if (!m) return null;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const date = new Date(y, mo - 1, d, 12, 0, 0, 0);
  // Reject rollovers (Feb 30 becomes Mar 2): a rolled date is a typo, and
  // jumping somewhere the viewer didn't name would be worse than ignoring it.
  if (
    date.getFullYear() !== y ||
    date.getMonth() !== mo - 1 ||
    date.getDate() !== d
  )
    return null;
  date.setHours(0, 0, 0, 0);
  date.setDate(date.getDate() + 1);
  date.setHours(0, 0, 0, 0); // re-floor: DST days are 23h or 25h
  return Math.floor(date.getTime() / 1000) - 1;
}
