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

/** `last_ts` is the OPENING moment of the most recent visit (#549): a
 * continuation row inside the same visit never moves it, and it rides the
 * one walk the tallies already make. Null only when there are no rows. */
export type Tally = {
  visits: number;
  today: number;
  week: number;
  last_ts: number | null;
};

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
    let last_ts: number | null = null;
    for (let i = 0; i < ts.length; i++) {
      if (i > 0 && ts[i] - ts[i - 1] <= gapS) continue; // same visit
      visits++;
      last_ts = ts[i]; // ascending walk: the last opening seen wins
      if (todayStart !== null && ts[i] >= todayStart) today++;
      if (weekStart !== null && ts[i] >= weekStart) week++;
    }
    out[sci] = { visits, today, week, last_ts };
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

/** The playback gain steps (issue #189): unity up to the 8x rescue setting.
 * Steps, not a slider -- the nameplate idiom is discrete states at fixed
 * widths, and "normal / louder / much louder / rescue the finch" is the
 * whole decision space. Above unity is the point: many real clips are very
 * faint (measured: sighting 107 at rms 0.019), and an attenuator-only
 * volume control cannot help them. */
export const GAIN_STEPS = [1, 2, 4, 8] as const;

/** A stored or proposed gain value, snapped to the nearest legal step.
 *
 * This is the localStorage boundary: the persisted string may be garbage
 * (hand-edited, an old format, another tab's future idea of the range), and
 * whatever comes back must land on a step the control can render as
 * selected -- an off-step value would leave the nameplate showing four
 * unlit buttons, a control asserting no state at all. Unparseable or
 * non-positive input reads as unity, the do-no-harm default; a tie between
 * two steps rounds DOWN, toward the quieter one. */
export function coerceGain(raw: string | number | null): number {
  const n = typeof raw === "number" ? raw : raw === null ? NaN : Number(raw);
  if (!Number.isFinite(n) || n <= 0) return 1;
  let best: number = GAIN_STEPS[0];
  for (const step of GAIN_STEPS) {
    if (Math.abs(step - n) < Math.abs(best - n)) best = step;
  }
  return best;
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

/** The grid/profile URL for one species' portrait. The small surfaces ask
 * for the tile-sized sibling (#495, `thumb`); `v` is the shelf version the
 * roster carries -- present, the URL names exact bytes and the route
 * answers it immutable, and a replaced portrait changes the version, so
 * freshness rides the URL instead of a revalidation. A bare call keeps the
 * original URL byte-for-byte (the pre-#495 callers and the 86400 policy). */
export function portraitUrl(
  sci: string,
  opts?: { thumb?: boolean; v?: string | null },
): string {
  const base = `/aviary/portrait/${encodeURIComponent(sci)}`;
  const params = new URLSearchParams();
  if (opts?.thumb) params.set("size", "thumb");
  if (opts?.v) params.set("v", opts.v);
  const q = params.toString();
  return q ? `${base}?${q}` : base;
}

/** 'Cardinalis_cardinalis.jpg' -> 'Cardinalis_cardinalis-thumb.jpg': the
 * enrichment pass's tile-sized sibling (#495). Derived from the
 * ALREADY-SCRUBBED shelf name ('-' has always been in the allowlist), so
 * the traversal guard's verdict is unchanged by construction. Must match
 * the pass's thumb_name() exactly -- one regex, one suffix, both sides. */
export function speciesThumbName(imageName: string): string {
  return `${imageName.slice(0, -".jpg".length)}-thumb.jpg`;
}

/** The portrait shelf version the roster ships (#495): an opaque token
 * from both files' stat facts that changes whenever either byte-stream
 * does -- an enrichment refresh, an owner drop, a thumb backfill landing.
 * That last one matters: "no thumb yet" is versioned too ("0-0"), so the
 * sibling arriving busts a cached full-size fallback instead of hiding
 * behind a year of immutable. */
export function portraitVersion(
  full: { mtimeMs: number; size: number },
  thumb: { mtimeMs: number; size: number } | null,
): string {
  const t = thumb ? `${Math.round(thumb.mtimeMs)}-${thumb.size}` : "0-0";
  return `${Math.round(full.mtimeMs)}-${full.size}.${t}`;
}

// --- Species removal (issues #216 + #281) -----------------------------------

// Mirror of the pass's portrait shelf: species_profile.image_file holds a
// scrubbed <name>.jpg -- one dot, no separators -- so anything else in the
// column is a hand-edited row this must never turn into a path.
const PORTRAIT_FILE = /^[A-Za-z0-9_-]+\.jpg$/;
const SPECIES_DIR = "species";

// The quarantine shelf (issue #281): a removal MOVES clip evidence here
// instead of deleting it, so a hand-made "this isn't a bird" verdict keeps
// its audio for the false-positive loop (epic #278). Under the clips dir on
// purpose -- one tree, one disk budget -- but a quarantined path is THREE
// segments (quarantine/<source>/<file>.wav), which the clips route's
// two-segment guard rejects by construction: nothing serves quarantine
// until the review surface (#283) chooses to.
export const QUARANTINE_DIR = "quarantine";

/** A removed clip's new home on the quarantine shelf. */
export function quarantinePath(rel: string): string {
  return `${QUARANTINE_DIR}/${rel}`;
}

export type RemovalPlan = {
  /** Evidence: clip + -enh sibling pairs, original -> quarantine shelf. */
  moves: { from: string; to: string }[];
  /** Refetchable files, still deleted: the portrait (Wikipedia sells it
   * back; the audio no one can re-record is what quarantine is for). */
  deletes: string[];
};

/** The file plan for a species removal, as relative paths under
 * MERLE_EARL_CLIPS: every sighting clip and the lifer's first_clip (deduped
 * -- the first sighting's clip usually IS first_clip) MOVES to the
 * quarantine shelf, each with its -enh sibling named unconditionally (the
 * enhancement pass keeps no registry, so the filesystem is asked by moving:
 * a sibling never written is a missing file, and missing files are not
 * errors); the portrait on the species/ shelf is deleted. Every row-held
 * path re-runs the clips route's own traversal guard before it may name a
 * file -- rows normally hold exactly what Earl wrote, but this feeds the
 * MCC's only write, and a hand-edited row must skip quietly rather than
 * escape the clips dir. */
export function removalPlan(
  clips: (string | null | undefined)[],
  imageFile: string | null | undefined,
): RemovalPlan {
  const seen = new Set<string>();
  const moves: RemovalPlan["moves"] = [];
  for (const clip of clips) {
    if (!clip) continue;
    const rel = clipRelPath(clip.split("/"));
    if (rel === null) continue;
    for (const p of [rel, enhancedRelPath(rel)]) {
      if (p === null || seen.has(p)) continue;
      seen.add(p);
      moves.push({ from: p, to: quarantinePath(p) });
    }
  }
  const deletes: string[] = [];
  if (imageFile && PORTRAIT_FILE.test(imageFile)) {
    deletes.push(`${SPECIES_DIR}/${imageFile}`);
    // The tile-sized sibling goes with it (#495) -- named unconditionally,
    // the -enh precedent: the pass keeps no registry, and a sibling never
    // written is just a missing file the delete loop already forgives.
    deletes.push(`${SPECIES_DIR}/${speciesThumbName(imageFile)}`);
  }
  return { moves, deletes };
}

// --- Per-species detection rules (issue #506) --------------------------------
// The operator's standing answers to "how should Earl hear this bird":
// exclude (the veto -- future detections never publish, history untouched),
// an extra confidence floor, or a one-species hole in the region mask. Rows
// live in THIS box's earl.db (species_rules -- rules never sync across
// stations); the MCC writes them (its second earl.db write, after #216's
// removal) and earl-listener re-reads them every ~60 s.

export type SpeciesRule = {
  exclude: boolean;
  /** Extra confidence floor; null = the global MERLE_EARL_THRESHOLD. The
   * listener max()es it against the global bar, so a rule can tighten the
   * gate for one species but never loosen the yard's own. */
  threshold: number | null;
  always_include: boolean;
};

/** The table both writer routes must be able to create (the negatives DDL
 * precedent: DDL lives with the writer, CREATE IF NOT EXISTS in the same
 * transaction, so a pre-#506 store upgrades on its first rule). sightings'
 * connect() carries the same shape so a station's store grows the table on
 * the next unit restart either way -- deploy order can't strand a rule. */
export const SPECIES_RULES_DDL = `
CREATE TABLE IF NOT EXISTS species_rules (
    species_sci    TEXT PRIMARY KEY,
    exclude        INTEGER NOT NULL DEFAULT 0,
    threshold      REAL,
    always_include INTEGER NOT NULL DEFAULT 0,
    updated_ts     INTEGER NOT NULL
);
`;

/** The overlay's confidence choices (the clip-gain idiom: steps, not a
 * slider). "Default" is the absence of a step -- threshold null. */
export const RULE_THRESHOLD_STEPS = [0.8, 0.9] as const;

/** A stored rule row (sqlite integers) -> the wire/UI shape. */
export function speciesRuleFrom(row: {
  exclude: number | null;
  threshold: number | null;
  always_include: number | null;
}): SpeciesRule {
  return {
    exclude: Boolean(row.exclude),
    threshold: row.threshold ?? null,
    always_include: Boolean(row.always_include),
  };
}

/** The PUT body -> a rule, or null for anything malformed (the route
 * answers 400; never trust the wire). Strict on purpose: the overlay is
 * the only writer, so a shape it would never send is a bug worth refusing
 * rather than coercing. A threshold must be a real number strictly inside
 * (0, 1) -- 0 and 1 are not confidences BirdNET emits and a rule carrying
 * one is a typo, not a policy. */
export function parseRulePayload(body: unknown): SpeciesRule | null {
  if (typeof body !== "object" || body === null || Array.isArray(body))
    return null;
  const b = body as Record<string, unknown>;
  const threshold = b.threshold ?? null;
  if (
    threshold !== null &&
    (typeof threshold !== "number" ||
      !Number.isFinite(threshold) ||
      threshold <= 0 ||
      threshold >= 1)
  )
    return null;
  for (const flag of [b.exclude, b.always_include])
    if (flag !== undefined && typeof flag !== "boolean") return null;
  return {
    exclude: b.exclude === true,
    threshold: threshold as number | null,
    always_include: b.always_include === true,
  };
}

/** An all-defaults rule holds no policy: the route deletes the row instead
 * of storing a no-op, so "clearing" and "never ruled" are the same state
 * and the listener's map stays exactly as big as the operator's opinions. */
export function ruleIsEmpty(rule: SpeciesRule): boolean {
  return !rule.exclude && rule.threshold === null && !rule.always_include;
}

/** The hero-meta stamp's copy, one string per rule state. Always a string
 * -- "default" when no rule stands -- because the stamp is RESERVED: the
 * meta entry renders in every state so a rule landing can never pop in and
 * shift the page (house rule #1). Exclude speaks alone (it makes the other
 * fields moot, exactly as the listener treats them). */
export function ruleStamp(rule?: SpeciesRule | null): string {
  if (!rule) return "default";
  if (rule.exclude) return "ignored";
  const parts: string[] = [];
  if (rule.threshold !== null)
    parts.push(`threshold ${rule.threshold.toFixed(2)}`);
  if (rule.always_include) parts.push("map override");
  return parts.length ? parts.join(" · ") : "default";
}

/** Snap a stored threshold onto the overlay's steps (coerceGain's rule,
 * ties toward the LOWER bar): the store may hold any value -- a hand edit,
 * a future step set -- but the control renders steps, and the nearest one
 * is the honest seat for it. null stays Default. */
export function coerceRuleThreshold(
  raw: number | null | undefined,
): number | null {
  if (raw == null || !Number.isFinite(raw)) return null;
  let best: number = RULE_THRESHOLD_STEPS[0];
  for (const step of RULE_THRESHOLD_STEPS)
    if (Math.abs(step - raw) < Math.abs(best - raw)) best = step;
  return best;
}

/** The removal DELETE's URL (#216), with #506's one addition: the
 * "don't let it come back" checkbox rides as ?exclude_future=1 so the
 * route can write the standing veto in the same transaction that erases
 * the record -- after the remove there is no species page left to set it
 * from. */
export function removalUrl(sci: string, excludeFuture: boolean): string {
  return `/aviary/species/${encodeURIComponent(sci)}${
    excludeFuture ? "?exclude_future=1" : ""
  }`;
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

/** A satisfiable single byte range, inclusive on both ends (RFC 7233's own
 * convention -- `bytes 0-1/12345` is TWO bytes). */
export type ByteRange = { start: number; end: number };

/** Parse an HTTP `Range` header against a resource of `size` bytes (#377).
 *
 * This exists because WebKit refuses media served without it: iOS/iPadOS
 * Safari probes `<audio>` sources with `Range: bytes=0-1` and treats a
 * 200-with-full-body answer as a FAILED load -- which made every Aviary clip
 * on an iPhone error out, exhaust the enh->raw fallback, and wear the
 * "faded" stamp meant for pruned files. Desktop engines tolerate the bare
 * 200, which is why the bug shipped invisibly in #183.
 *
 * Three answers, mirroring the spec's three postures:
 *   null              -- no header, or nothing this parser is willing to
 *                        honor (malformed, multi-range, non-bytes units,
 *                        end before start). RFC 7233 lets a server IGNORE
 *                        such a header, so the route serves the full 200 --
 *                        always correct, never a lie.
 *   "unsatisfiable"   -- well-formed but askable of no byte this file has
 *                        (start past EOF, empty suffix, any range of an
 *                        empty file) -> 416 with `bytes *  /total`.
 *   {start, end}      -- the slice to serve as a 206. The end clamps to
 *                        EOF (asking past the end is fine; starting past
 *                        it is not) -- the spec's rule, not a kindness.
 */
export function parseRange(
  header: string | null,
  size: number,
): ByteRange | "unsatisfiable" | null {
  if (header === null) return null;
  const m = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!m) return null;
  const [, startRaw, endRaw] = m;
  if (startRaw === "" && endRaw === "") return null; // "bytes=-": nonsense
  if (startRaw === "") {
    // Suffix range: the LAST n bytes. A zero-length suffix names no byte.
    const n = Number(endRaw);
    if (n === 0 || size === 0) return "unsatisfiable";
    return { start: Math.max(0, size - n), end: size - 1 };
  }
  const start = Number(startRaw);
  if (start >= size) return "unsatisfiable"; // incl. any range of size 0
  if (endRaw === "") return { start, end: size - 1 };
  const end = Number(endRaw);
  if (end < start) return null; // malformed -> ignore, serve the 200
  return { start, end: Math.min(end, size - 1) };
}

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
  // The opening moment of the most recent visit (#549), from the same tally
  // walk. Optional: a pre-#549 payload or a hand-built entry simply has no
  // last visit to speak of, and the rare-return cut treats that as "never".
  last_ts?: number | null;
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
  // The portrait shelf version (#495) -- portraitVersion() over the full
  // file + thumb sibling's stats, attached by the roster ROUTE (it lives
  // beside the files; shapeRoster never touches the fs). Absent when the
  // route has no MERLE_EARL_CLIPS or the file is gone: consumers then emit
  // version-less URLs and simply get the older 86400 cache policy.
  image_v?: string | null;
  // What it sounds like (#315): the article's vocalization section, or NULL
  // for the species whose article has no such section -- an honest blank,
  // never a guess assembled from loose paragraphs.
  voice?: string | null;
  // Which reader produced it (#317, #318): "section" quoted from a dedicated
  // vocalization section, "sentences" gathered from an article that has
  // none, "adw" from Animal Diversity Web when Wikipedia said nothing at
  // all. NULL on a pre-#317 row, which reads as the section case.
  voice_source?: string | null;
  // Animal Diversity Web's prose and its OWN stored credit (#318). A separate
  // pair rather than more text in `voice`, because ADW is CC BY-NC-SA and
  // Wikipedia is CC BY-SA: the two are not licence-compatible and must never
  // be concatenated. `voice_source === "adw"` is what says which pair is live.
  voice_adw?: string | null;
  voice_adw_attribution?: string | null;
  // The best recorded visit (#333) -- see bestClips. NULL when the species
  // has no clip at all (every write failed, or the files were pruned before
  // this shipped), which the page renders as the same reserved slot a
  // clip-less first contact already uses.
  best_clip?: string | null;
  best_confidence?: number | null;
  best_ts?: number | null;
  // The species showcase (#551): the listener pass's three best recordings,
  // each a different call, from species_showcase. Slot 1 there IS best_clip
  // (the same rule, fixture-pinned across both stacks), so the page renders
  // slot 1 from best_clip and takes only slots 2 and 3 from here. Absent on
  // a store the pass has never run on; a species with no distinct second
  // call simply has no slot-2 entry, which the page renders as a reserved
  // empty slot.
  showcase?: ShowcaseSlot[];
  // The operator's detection rule (#506), attached by the roster ROUTE from
  // species_rules (a separate tiny query, deliberately not another rung on
  // the LEFT JOIN cascade). Absent on a pre-#506 store and on every unruled
  // species -- ruleStamp() reads both as "default".
  rule?: SpeciesRule | null;
};

/** The credit line under the "What It Sounds Like" panel (#317).
 *
 * Both readers take CC BY-SA prose from the same article, so the licence
 * credit is identical -- what differs is the claim we are making about the
 * text. A dedicated section is the article's own account of the bird, quoted
 * whole; the fallback is lines gathered from across an article that never
 * set out to describe a sound. Saying so is the same honesty the portrait
 * attribution already practises, and it is what stops a reader taking
 * assembled sentences for an encyclopedia's considered paragraph.
 *
 * An unknown or missing source reads as the section case: pre-#317 rows are
 * all sections by construction, and over-claiming "gathered" about a genuine
 * section would be its own small lie. */
export function voiceCredit(source?: string | null): string {
  return source === "sentences"
    ? "gathered from the article · via Wikipedia"
    : "via Wikipedia";
}

/** The vocalization prose to render, paired with the credit that belongs to
 * it (#318). NULL when the species has no sound description from any source.
 *
 * A PAIR, NOT A COALESCE, and that is the licensing rule made structural.
 * Wikipedia's prose is CC BY-SA and Animal Diversity Web's is CC BY-NC-SA;
 * they may not be merged, and each must appear under its own credit. Picking
 * the text and picking the credit therefore have to be ONE decision -- if
 * they were two, some future edit would eventually pair one source's prose
 * with the other's stamp, which is a licensing failure as well as a lie.
 *
 * `voice_source` is the authority on which pair is live: the pass writes it
 * as "adw" only when it actually fell through to ADW, and it never writes
 * both texts (Wikipedia answering clears the ADW columns).
 *
 * ADW's credit is READ FROM THE ROW rather than built here (#184's stored-
 * attribution rule). Wikipedia's is still derived, because both of its
 * readers quote the same article under the same licence and what differs is
 * only the claim being made about the text -- see voiceCredit. */
export function voiceProse(
  entry?: Pick<
    RosterEntry,
    "voice" | "voice_source" | "voice_adw" | "voice_adw_attribution"
  > | null,
): { text: string; credit: string } | null {
  if (!entry) return null;
  if (entry.voice_source === "adw" && entry.voice_adw) {
    return {
      text: entry.voice_adw,
      // The stored line is the honest one. The fallback exists only for a row
      // written before the attribution column was filled, and still names the
      // right source rather than defaulting to Wikipedia's.
      credit: entry.voice_adw_attribution ||
        "via Animal Diversity Web · CC BY-NC-SA 3.0",
    };
  }
  if (entry.voice) {
    return { text: entry.voice, credit: voiceCredit(entry.voice_source) };
  }
  return null;
}

/**
 * Confidence window for composite best-recording rank (#442). Among visits
 * within this many points of the species' max confidence (or at/above
 * BEST_CLIP_CONF_FLOOR), prefer the higher vocal_fill. Outside the window a
 * low-confidence long song cannot steal the crown from a 0.98 chirp.
 */
export const BEST_CLIP_CONF_WINDOW = 0.05;
/** Absolute confidence floor that always competes for best, even when below
 * cMax − window (rare when cMax is very high). */
export const BEST_CLIP_CONF_FLOOR = 0.9;

/** The best-sounding recording of each species (#333, composite rank #442).
 *
 * Earl has curated this since #175 and nothing ever showed it: a window that
 * beats its visit's best REWRITES that visit's clip in place, so every clip
 * file already holds the best moment of its visit. What was missing was a way
 * to pick the best VISIT -- and measured on pearl, for every top species the
 * best-confidence visit is not the first one (American Goldfinch 0.65-0.98
 * over 636 visits), so "first contact" was frequently a mediocre take while a
 * near-perfect one sat on disk unreachable.
 *
 * Ranked by the STORED confidence, which is the visit's OPENING window --
 * `sightings` is append-only and a visit's true peak lives only in the
 * journal (#175's accepted trade-off). So this is honestly "the best recorded
 * visit", not "the highest-confidence window ever": a visit that opened at
 * 0.80 and peaked at 0.90 can lose to one that opened at 0.85 and never
 * improved. The clip the listener actually hears is best-of-visit either way,
 * which is why option A beat adding an UPDATE path to an append-only store.
 *
 * #442 adds a second axis: `vocal_fill` (bird-band song fraction on the
 * clip, 0..1, from the listener pass). When any candidate for a species has
 * a fill, ranking is composite — maximize fill among visits near the top
 * confidence — so a fuller song beats a single high-confidence warble.
 * Unscored stores (every fill null) keep pure confidence ranking.
 *
 * A clip-less row can never win: a failed clip write is an honest gap, and
 * crowning it would hand the page a confident number attached to silence. */
export function bestClips(
  rows: {
    species_sci: string;
    ts: number;
    confidence?: number;
    clip?: string | null;
    vocal_fill?: number | null;
  }[],
): Record<string, { clip: string; confidence: number; ts: number }> {
  type Cand = {
    clip: string;
    confidence: number;
    ts: number;
    fill: number | null;
  };
  const bySci = new Map<string, Cand[]>();
  for (const r of rows) {
    if (!r.clip || typeof r.confidence !== "number") continue;
    const fill =
      typeof r.vocal_fill === "number" && Number.isFinite(r.vocal_fill)
        ? r.vocal_fill
        : null;
    const list = bySci.get(r.species_sci);
    const cand: Cand = {
      clip: r.clip,
      confidence: r.confidence,
      ts: r.ts,
      fill,
    };
    if (list) list.push(cand);
    else bySci.set(r.species_sci, [cand]);
  }

  const out: Record<string, { clip: string; confidence: number; ts: number }> =
    {};
  for (const [sci, cands] of bySci) {
    const anyFill = cands.some((c) => c.fill !== null);
    let best: Cand;
    if (!anyFill) {
      // Pure confidence (#333): strictly greater wins; equal conf keeps the
      // earlier take (older file has already survived retention).
      best = cands[0];
      for (const c of cands) {
        if (
          c.confidence > best.confidence ||
          (c.confidence === best.confidence && c.ts < best.ts)
        )
          best = c;
      }
    } else {
      const cMax = Math.max(...cands.map((c) => c.confidence));
      const eligible = cands.filter(
        (c) =>
          c.confidence >= cMax - BEST_CLIP_CONF_WINDOW ||
          c.confidence >= BEST_CLIP_CONF_FLOOR,
      );
      // Prefer scored rows; unscored fill ranks as -1 so a partial backfill
      // does not invent a champion out of thin air — re-run the pass.
      best = eligible[0];
      for (const c of eligible) {
        const f = c.fill ?? -1;
        const bf = best.fill ?? -1;
        if (f > bf) best = c;
        else if (f === bf) {
          if (
            c.confidence > best.confidence ||
            (c.confidence === best.confidence && c.ts < best.ts)
          )
            best = c;
        }
      }
    }
    out[sci] = { clip: best.clip, confidence: best.confidence, ts: best.ts };
  }
  return out;
}

/** One showcase slot (#551) as the roster carries it. */
export type ShowcaseSlot = {
  slot: number;
  clip: string;
  confidence: number | null;
  vocal_fill: number | null;
  ts: number | null;
};

/** species_showcase rows (joined to their sighting's ts by the route) ->
 * per-species slot lists, slot-ordered. Pure; a malformed row (no clip, a
 * slot outside 1..3) is dropped rather than rendered as a broken button. */
export function showcaseBySpecies(
  rows: {
    species_sci: string;
    slot: number;
    clip: string | null;
    confidence?: number | null;
    vocal_fill?: number | null;
    ts?: number | null;
  }[],
): Record<string, ShowcaseSlot[]> {
  const out: Record<string, ShowcaseSlot[]> = {};
  for (const r of rows) {
    if (!r.clip || !Number.isInteger(r.slot) || r.slot < 1 || r.slot > 3)
      continue;
    (out[r.species_sci] ??= []).push({
      slot: r.slot,
      clip: r.clip,
      confidence: typeof r.confidence === "number" ? r.confidence : null,
      vocal_fill: typeof r.vocal_fill === "number" ? r.vocal_fill : null,
      ts: typeof r.ts === "number" ? r.ts : null,
    });
  }
  for (const list of Object.values(out)) list.sort((a, b) => a.slot - b.slot);
  return out;
}

/** life_list rows + raw sighting rows -> the roster the grid renders.
 * Species-common order for determinism; the client owns the sort control. A
 * life_list species with no sighting rows (shouldn't happen -- the same
 * insert writes both -- but a hand-edited store is a store) tallies honestly
 * at zero.
 *
 * `confidence`/`clip` are optional on the row type (#333): the tally only
 * ever needed (species, ts), and keeping them optional means a caller that
 * selects the narrow shape still type-checks and simply gets no best clip. */
export function shapeRoster(
  lifeRows: {
    species_sci: string;
    species_common: string;
    first_ts: number;
    first_source: string;
    first_clip: string | null;
  }[],
  sightingRows: {
    species_sci: string;
    ts: number;
    confidence?: number;
    clip?: string | null;
    vocal_fill?: number | null;
  }[],
  todayStart: number | null,
  showcase: Record<string, ShowcaseSlot[]> = {},
): RosterEntry[] {
  const tallies = tallyVisits(sightingRows, todayStart);
  // One extra walk of rows the tally already had in hand -- no second query,
  // no second scan of the store.
  const best = bestClips(sightingRows);
  return lifeRows
    .map((r) => ({
      ...r,
      visits: tallies[r.species_sci]?.visits ?? 0,
      today: tallies[r.species_sci]?.today ?? 0,
      week: tallies[r.species_sci]?.week ?? 0,
      last_ts: tallies[r.species_sci]?.last_ts ?? null,
      best_clip: best[r.species_sci]?.clip ?? null,
      best_confidence: best[r.species_sci]?.confidence ?? null,
      best_ts: best[r.species_sci]?.ts ?? null,
      showcase: showcase[r.species_sci] ?? [],
    }))
    .sort((a, b) => a.species_common.localeCompare(b.species_common));
}

// --- The grid's sort control -------------------------------------------------

// "week" is the trailing-7-day tally (#220's window), the grid's default
// since #643 -- all-time "visits" set like concrete on an established station.
export type SortKey = "name" | "week" | "visits";
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
    const cmp = key === "week" ? a.week - b.week : a.visits - b.visits;
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

// --- Life-list search (issue #352) -------------------------------------------

/** sessionStorage key for the main-page life-list query (#410). Session-
 * scoped on purpose: survives Back / masthead dashboard / refresh in the
 * same tab; dies with the tab. Not localStorage (clip-gain is permanent). */
export const LIFE_LIST_QUERY_KEY = "aviary.life-list-query";
/** The species mask page's own search box (#593) -- its own key, so a
 * life-list drill-down and a mask browse keep separate fragments. */
export const MASK_QUERY_KEY = "aviary.mask-query";

/** Read a persisted search query under `key`. Missing key, null store, or
 * a storage throw (private mode) all return "" -- the unfiltered grid. */
export function readSessionQuery(
  store: Pick<Storage, "getItem"> | null | undefined,
  key: string,
): string {
  if (!store) return "";
  try {
    const v = store.getItem(key);
    return typeof v === "string" ? v : "";
  } catch {
    return "";
  }
}

/** Write a search query under `key`. Empty string removes the key so a
 * clear leaves no stale entry for the next mount in this session. */
export function writeSessionQuery(
  store: Pick<Storage, "setItem" | "removeItem"> | null | undefined,
  key: string,
  query: string,
): void {
  if (!store) return;
  try {
    if (query === "") store.removeItem(key);
    else store.setItem(key, query);
  } catch {
    // Quota / private mode: fail quiet; the in-memory box still works.
  }
}

/** The life-list query (#410), under its own key. */
export function readLifeListQuery(
  store: Pick<Storage, "getItem"> | null | undefined,
): string {
  return readSessionQuery(store, LIFE_LIST_QUERY_KEY);
}

export function writeLifeListQuery(
  store: Pick<Storage, "setItem" | "removeItem"> | null | undefined,
  query: string,
): void {
  writeSessionQuery(store, LIFE_LIST_QUERY_KEY, query);
}

// --- The species mask (issue #593) ------------------------------------------
//
// What Earl's geo model allows here THIS week, as the listener published it
// on audio/mask and sightings.py stored it (region_mask + region_mask_meta,
// current state only). The page merges it with the roster the dashboard
// already fetches: a mask species on the life list is HEARD (lit, a link to
// its profile), the rest are the yard's open questions (dimmed, no link).
// Everything below is pure; the route and the component stay thin over it.

export type MaskRow = {
  species_sci: string;
  species_common: string;
  /** The geo model's score for this species here this week (0..1). */
  geo: number;
};

export type MaskMeta = {
  ts: number;
  week: number;
  status: "masked" | "unmasked";
  error: string | null;
};

/** The /aviary/mask body. `mask` null = no mask recorded yet (unset env,
 * no store, a store the listener has never reported into). */
export type MaskPayload = {
  mask: MaskMeta | null;
  species: MaskRow[];
};

/** Shape the two store queries into the wire body. A missing meta row is
 * "no mask recorded yet" regardless of any species rows (the meta row is
 * what says which week they belong to); an unknown status is treated the
 * same, never guessed at. */
export function maskPayloadFrom(
  meta:
    | {
        ts: number | null;
        week: number | null;
        status: string | null;
        error: string | null;
      }
    | undefined
    | null,
  rows: MaskRow[],
): MaskPayload {
  if (
    !meta ||
    typeof meta.ts !== "number" ||
    typeof meta.week !== "number" ||
    (meta.status !== "masked" && meta.status !== "unmasked")
  ) {
    return { mask: null, species: [] };
  }
  return {
    mask: {
      ts: meta.ts,
      week: meta.week,
      status: meta.status,
      error: meta.error ?? null,
    },
    species: meta.status === "masked" ? rows : [],
  };
}

export type MaskEntry = {
  species_sci: string;
  species_common: string;
  /** Null for a species the operator let in by rule (always_include): the
   * geo model never scored it. */
  geo: number | null;
  /** On this location's life list -- heard ever, not this week. */
  heard: boolean;
  visits: number;
  rule: "always_include" | null;
};

/** Alphabetical by common name, ties by scientific name -- the rosterOrder
 * name rule. Fixed: nothing on the page ever re-sorts it. */
function byMaskName(a: MaskEntry, b: MaskEntry): number {
  return (
    a.species_common.localeCompare(b.species_common, undefined, {
      sensitivity: "base",
    }) || a.species_sci.localeCompare(b.species_sci)
  );
}

/** What Earl will actually accept, merged with what the yard has heard.
 * The geo mask minus the operator's `exclude` vetoes (never listed: the gate
 * drops them before the mask is consulted) plus `always_include` holes (the
 * roster species the operator let in, stamped by rule, no geo score). A
 * roster entry outside the mask is simply not on this page -- the list is
 * the mask's, lit by the roster, not the other way round. */
export function effectiveMask(
  rows: MaskRow[],
  roster: RosterEntry[],
): MaskEntry[] {
  const byRoster = new Map(roster.map((e) => [e.species_sci, e]));
  const out: MaskEntry[] = [];
  const seen = new Set<string>();
  for (const r of rows) {
    if (seen.has(r.species_sci)) continue;
    seen.add(r.species_sci);
    const e = byRoster.get(r.species_sci);
    if (e?.rule?.exclude) continue;
    out.push({
      species_sci: r.species_sci,
      species_common: r.species_common,
      geo: r.geo,
      heard: e !== undefined,
      visits: e?.visits ?? 0,
      rule: null,
    });
  }
  for (const e of roster) {
    if (seen.has(e.species_sci)) continue;
    if (!e.rule?.always_include || e.rule.exclude) continue;
    seen.add(e.species_sci);
    out.push({
      species_sci: e.species_sci,
      species_common: e.species_common,
      geo: null,
      heard: true,
      visits: e.visits,
      rule: "always_include",
    });
  }
  return out.sort(byMaskName);
}

/** The readout's figures: how many of the mask's species the yard has
 * heard, over how many it could. */
export function maskStats(entries: MaskEntry[]): {
  heard: number;
  total: number;
} {
  let heard = 0;
  for (const e of entries) if (e.heard) heard++;
  return { heard, total: entries.length };
}

export type MaskFilter = "all" | "unheard";

/** The grid the viewer sees: the fixed order filtered by the search box and
 * the all/unheard pill, never re-sorted by either. */
export function maskShown(
  entries: MaskEntry[],
  query: string,
  filter: MaskFilter,
): MaskEntry[] {
  return entries.filter(
    (e) => (filter === "all" || !e.heard) && speciesMatches(e, query),
  );
}

// --- Announcement strip (epic #447 Phase 1) ---------------------------------
//
// Reusable notice under the Aviary tab bar. Event kinds (rare_return,
// new_best) and ambient kinds (fun_fact) share one shell; priority + dismiss
// keys are pure so consumers only enqueue. Zero footprint when nothing is
// visible -- not a permanent empty panel (unlike New Arrivals' reserved
// frame). The notice-kind registry on epic #447 is this table's contract:
// every consumer adds its row to both.

/** Notice kinds the strip understands. More kinds are additive. */
export type AviaryNoticeKind = "rare_return" | "new_best" | "fun_fact" | "demo";

/** One message for the strip. `id` is stable for dismiss persistence. */
export type AviaryNotice = {
  id: string;
  kind: AviaryNoticeKind;
  title: string;
  body?: string;
  /** When set, the primary surface is a link (species page, etc.). */
  href?: string;
  species_sci?: string;
  species_common?: string;
  /** Portrait filename under species/ when known (roster image_file). */
  portrait_file?: string | null;
  /** A RAW clip path the strip may offer to play in place (new best). The
   * click target still owns the full page; this is the one-tap listen. */
  clip?: string | null;
  /** The moment the notice is ABOUT, epoch seconds (#602): a rare return's
   * visit opening, a new best's recording moment. The strip stamps it in
   * local date and time; null/absent renders a reserved empty line. */
  ts?: number | null;
};

/** localStorage key prefix for dismissed notice ids. */
export const NOTICE_DISMISS_PREFIX = "aviary.notice.dismissed.";

/** Event notices outrank ambient so a live moment is never buried under
 * trivia; among events the rarer moment outranks the routine one (a bird
 * heard twice ever coming back beats a better recording of a regular). The
 * numbers are the registry's total order (#447), so landing a kind never
 * renumbers its neighbours; lower shows first. */
const NOTICE_KIND_RANK: Record<AviaryNoticeKind, number> = {
  rare_return: 1,
  new_best: 3,
  demo: 8,
  fun_fact: 9,
};

/** Stable dismiss key for a notice id. */
export function noticeDismissKey(id: string): string {
  return NOTICE_DISMISS_PREFIX + id;
}

/** Id for a new-best announcement: species + clip path so a later champion
 * can re-fire while the same clip stays quiet after dismiss. */
export function newBestNoticeId(species_sci: string, clip: string): string {
  return `new_best:${species_sci}:${clip}`;
}

/** Desk-check notice for `?notice=demo` -- not a product surface. */
export function demoNotice(): AviaryNotice {
  return {
    id: "demo:announcement-strip",
    kind: "demo",
    title: "Announcement strip",
    body: "Reusable notice under the tab bar (#447). Close when you are done looking.",
    href: "/aviary",
  };
}

export function isNoticeDismissed(
  store: Pick<Storage, "getItem"> | null | undefined,
  id: string,
): boolean {
  if (!store) return false;
  try {
    return store.getItem(noticeDismissKey(id)) === "1";
  } catch {
    return false;
  }
}

export function dismissNotice(
  store: Pick<Storage, "setItem"> | null | undefined,
  id: string,
): void {
  if (!store) return;
  try {
    store.setItem(noticeDismissKey(id), "1");
  } catch {
    // private mode / quota: fail quiet
  }
}

/** Forget a dismissal -- the desk-check flag's "show me again". */
export function undismissNotice(
  store: Pick<Storage, "removeItem"> | null | undefined,
  id: string,
): void {
  if (!store) return;
  try {
    store.removeItem(noticeDismissKey(id));
  } catch {
    // private mode: fail quiet
  }
}

/**
 * Pick the single notice to show: highest priority kind among undismissed
 * candidates (stable order within a kind = first in the list wins). Empty
 * input or all dismissed → null (strip unmounts; zero footprint).
 */
export function pickVisibleNotice(
  notices: AviaryNotice[],
  isDismissed: (id: string) => boolean,
): AviaryNotice | null {
  let best: AviaryNotice | null = null;
  let bestRank = Infinity;
  for (const n of notices) {
    if (!n?.id || isDismissed(n.id)) continue;
    const rank = NOTICE_KIND_RANK[n.kind] ?? 99;
    if (rank < bestRank) {
      best = n;
      bestRank = rank;
    }
  }
  return best;
}

/** Does this roster entry answer the search box? Case-insensitive substring
 * over the common AND scientific name -- `wood` finds every woodpecker,
 * `finc` the finches, and a Latin fragment works for whoever thinks in sci
 * names. A blank (or all-whitespace) query matches everything: the empty
 * box IS the unfiltered grid. Substring, deliberately not initials or
 * edit-distance fuzz -- the search's whole job is "type a fragment of the
 * name you know", and cleverer matching surfaces birds the typist can't
 * see the reason for. */
export function speciesMatches(
  e: { species_common: string; species_sci: string } | undefined,
  query: string,
): boolean {
  if (!e) return false;
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return (
    e.species_common.toLowerCase().includes(q) ||
    e.species_sci.toLowerCase().includes(q)
  );
}

// --- The reconciliation poll (issue #352) ------------------------------------
//
// The main page re-grounds itself in the store every minute: the live bus
// ticks counts in place, but a dropped websocket replays nothing (audio/
// events is an event topic -- nothing retained), and the mount-time midnight
// boundary goes stale on a tab left open past it. These helpers fold a fresh
// roster payload into the page's existing state WITHOUT reordering anything
// the viewer is looking at -- house rule #1 is exactly why the poll can't
// just re-run the load-time shaping.

/** Fold a fresh roster into the grid's order: existing positions are kept
 * exactly (whatever the active sort was when the viewer last clicked it),
 * unseen species append at the end -- the same rule a live lifer follows.
 * Never drops a key: a species missing from one payload (a removal in
 * another tab, a store write mid-flight) keeps its tile until reload rather
 * than vanishing under the reader. Returns `prev` itself when nothing is
 * new, so a quiet poll is a no-op render. */
export function reconcileOrder(
  prev: string[],
  entries: RosterEntry[],
): string[] {
  const known = new Set(prev);
  const fresh = entries
    .filter((e) => !known.has(e.species_sci))
    .map((e) => e.species_sci);
  return fresh.length === 0 ? prev : [...prev, ...fresh];
}

/** One Today's Visitors pill. */
export type TodayTile = {
  species_sci: string;
  species_common: string;
  count: number;
};

/** Fold a fresh roster into the visitors rail: pill order is preserved,
 * counts land in place, newcomers append (most-visited first among
 * themselves -- todayVisitors' own order, the live-append rule). A pill
 * leaves only when the store explicitly says its species has no visits
 * today -- which is how a midnight rollover empties the rail -- never
 * merely because a payload omitted it (a just-minted lifer's row can trail
 * its own bus event; unknown is not zero). */
export function reconcileRail(
  prev: TodayTile[],
  entries: RosterEntry[],
): TodayTile[] {
  const bySci = new Map(entries.map((e) => [e.species_sci, e]));
  const kept = prev.flatMap((t) => {
    const e = bySci.get(t.species_sci);
    if (!e) return [t];
    if (e.today <= 0) return [];
    return [
      {
        species_sci: e.species_sci,
        species_common: e.species_common,
        count: e.today,
      },
    ];
  });
  const held = new Set(kept.map((t) => t.species_sci));
  const fresh = todayVisitors(entries.filter((e) => !held.has(e.species_sci)));
  return [...kept, ...fresh];
}

// --- The archive hero (issue #260) -------------------------------------------

export type ArchiveStats = {
  /** Distinct species on record, all time. */
  species: number;
  /** Visit total across the whole roster (the standings band's yardTotal). */
  visits: number;
  /** Distinct species heard inside the trailing local week. */
  week: number;
  /** Distinct species heard since the client's local midnight. */
  today: number;
  /** Sum of per-species `today` visit tallies -- total visit openings since
   * the client's local midnight, across every species. The hero's "detections
   * today" figure (#413); same unit Today's Visitors already shows per pill. */
  detectionsToday: number;
  /** Sum of per-species `week` visit tallies -- total visit openings inside
   * the trailing local week, across every species. Same unit as
   * detectionsToday; the readout's week cluster mirrors today's pair. */
  detectionsWeek: number;
  /** The earliest first-heard moment on record -- "listening since". Null on
   * an empty archive: no record means no since, never a guess. */
  since: number | null;
  /** Local calendar days on the air, first record's day through today
   * INCLUSIVE -- day one reads "1 day", not "0 days". */
  days: number | null;
  /** Mean visits per week over the days-on-air span (`visits * 7 / days`).
   * Null when there is no span (empty archive, or `now` unknown). The
   * hero's fourth tile leads with this so the calendar start date can
   * stay present without reading as "today's date". */
  visitsPerWeek: number | null;
  /** Mean visits per 30-day month over the same span (`visits * 30 / days`).
   * Same null rules as visitsPerWeek. */
  visitsPerMonth: number | null;
};

/** The hero band's numbers (#260, detections-today half of tile 1 in #413),
 * read entirely off the roster the page already holds -- zero fetches of its
 * own, and live roster updates flow through for free. "This week"/"today"
 * count DISTINCT SPECIES heard in the roster's own windows (week/today > 0
 * -- no third definition of either boundary); `detectionsToday` /
 * `detectionsWeek` are the SUMs of those per-species `today` / `week`
 * tallies (activity volume, not diversity). New lifers are New Arrivals'
 * job, not this band's. `now` is the page's mount-time clock, passed in
 * rather than read here (the SSR/hydration rule). The day count rounds the
 * local-midnight difference -- a DST-stretched day skews the division by at
 * most an hour -- and floors at 1: a first-ever bird landing after a
 * mounted tab's midnight is day one, not day zero. Pace rates (visits per
 * week / per month) are the same span's arithmetic -- not a rolling window
 * of their own. All zeros and nulls on an empty archive: exactly the values
 * the fresh-aviary state keys off. */
export function archiveStats(
  entries: RosterEntry[],
  now: number | null,
): ArchiveStats {
  let visits = 0;
  let week = 0;
  let today = 0;
  let detectionsToday = 0;
  let detectionsWeek = 0;
  let since: number | null = null;
  for (const e of entries) {
    visits += e.visits;
    if (e.week > 0) week++;
    if (e.today > 0) today++;
    detectionsToday += e.today;
    detectionsWeek += e.week;
    if (since === null || e.first_ts < since) since = e.first_ts;
  }
  const days =
    since !== null && now !== null
      ? Math.max(1, Math.round((dayStart(now) - dayStart(since)) / 86400) + 1)
      : null;
  // Pace over the whole record, not a rolling 7/30-day window -- the hero
  // already has species-today/week tiles for "right now"; this answers
  // "how busy has the yard been since the record began".
  const visitsPerWeek = days !== null ? (visits * 7) / days : null;
  const visitsPerMonth = days !== null ? (visits * 30) / days : null;
  return {
    species: entries.length,
    visits,
    week,
    today,
    detectionsToday,
    detectionsWeek,
    since,
    days,
    visitsPerWeek,
    visitsPerMonth,
  };
}

// --- The visits-over-time chart (issue #185) ---------------------------------

/** Overview mode's span: ~30 days of daily bars. No longer the chart's
 * opening state -- the profile now opens in DETAIL (the 48-hour rhythm),
 * the standings band above already carrying the "how often" these bars show.
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
  return `${gap === 1 ? "one visit" : `${gap.toLocaleString("en-US")} visits`} behind the ${s.leader.species_common}`;
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

// --- Rare visitor (#549, the strip's event consumer) -------------------------
//
// New Arrivals goes quiet on a mature yard: lifers are rare at 99 species.
// But the life list carries a long tail of birds heard a handful of times,
// and when one comes back nothing said so -- the ticker mentioned it once
// and a count ticked. This is that moment, as pure roster math (the #224
// precedent): no backend, no Python, no second count.

/** At most this many lifetime visits, the one that fired included. */
export const RARE_RETURN_MAX_VISITS = 5;
/** The return must have opened inside this trailing window. */
export const RARE_RETURN_WINDOW_S = ARRIVALS_24H_S;
/** The archive's "listening since" must be at least this old: a fresh
 * station has every bird at one to three visits and New Arrivals already
 * firing daily, so rare-return says nothing until the yard has a record to
 * be rare against. Derived from the roster, never a go-live constant. */
export const RARE_RETURN_MATURITY_S = 14 * 86400;

/** The rare-return cut: species with few lifetime visits whose latest visit
 * opened inside the window, first heard long enough ago that New Arrivals
 * has let go of them (the week is the only coupling -- a bird is never
 * featured on both surfaces). `since` is `archiveStats().since`; a young or
 * empty archive yields nothing. Rarest first (fewest visits), then the most
 * recent return, then name for determinism. A one-visit bird can never
 * qualify: its only visit IS its first, which the week rule excludes. */
export function rareReturns(
  entries: RosterEntry[],
  now: number,
  since: number | null,
): RosterEntry[] {
  if (since === null || now - since < RARE_RETURN_MATURITY_S) return [];
  const windowStart = now - RARE_RETURN_WINDOW_S;
  const settledBefore = now - ARRIVALS_WEEK_S;
  return entries
    .filter(
      (e) =>
        e.visits > 0 &&
        e.visits <= RARE_RETURN_MAX_VISITS &&
        e.last_ts != null &&
        e.last_ts >= windowStart &&
        e.first_ts < settledBefore,
    )
    .sort(
      (a, b) =>
        a.visits - b.visits ||
        (b.last_ts ?? 0) - (a.last_ts ?? 0) ||
        a.species_common.localeCompare(b.species_common),
    );
}

/** Notice id = species + the LOCAL day the return opened, so an owl hooting
 * on and off all night collapses to one notice, a dismiss stays quiet for
 * the rest of that day, and the same bird back next week announces again.
 * `dayStart` keys the viewer's own calendar; dismiss lives in that same
 * browser's localStorage, so the two agree by construction. */
export function rareReturnNoticeId(species_sci: string, lastTs: number): string {
  return `rare_return:${species_sci}:${dayStart(lastTs)}`;
}

/** English ordinal suffix: 2nd, 3rd, 4th, 11th, 21st. */
export function ordinal(n: number): string {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 13) return `${n}th`;
  switch (n % 10) {
    case 1:
      return `${n}st`;
    case 2:
      return `${n}nd`;
    case 3:
      return `${n}rd`;
    default:
      return `${n}th`;
  }
}

/** A moment as the viewer's day: the component owns every locale call on
 * the page (its `dayOf` family), so builders that need a date in COPY take
 * the formatter rather than importing one -- the lib stays locale-free and
 * a test hands in a plain string. */
export type DayFormatter = (ts: number) => string;

/** The strip's one line under the name: "only its 3rd visit ever, first
 * heard on Sep 2" (#603) -- the ordinal says how rare, the first-heard day
 * says over what span. `firstHeard` arrives already formatted (above). */
export function rareReturnBody(visits: number, firstHeard: string): string {
  return `only its ${ordinal(visits)} visit ever, first heard on ${firstHeard}`;
}

/** The notice for one rare return: the strip's stamp says "rare visitor",
 * the display line is the bird, the body its ordinal and first-heard day.
 * Portrait thumb, the parenthetical scientific name and the species page
 * all come from the sci name the shell already resolves. */
export function rareReturnNotice(
  e: RosterEntry,
  firstHeardOf: DayFormatter,
): AviaryNotice {
  return {
    id: rareReturnNoticeId(e.species_sci, e.last_ts ?? 0),
    kind: "rare_return",
    title: e.species_common,
    body: rareReturnBody(e.visits, firstHeardOf(e.first_ts)),
    href: `/aviary/${encodeURIComponent(e.species_sci)}`,
    species_sci: e.species_sci,
    species_common: e.species_common,
    portrait_file: e.image_file ?? null,
    // The return IS the latest visit's opening (#602): when the bird was heard.
    ts: e.last_ts ?? null,
  };
}

// --- New best recording (#447 Phase 2, the strip's second event consumer) --
//
// Earl rewrites a visit's clip in place when a window beats it, and the
// roster's three best slots per species move as the store's rankings
// change: slot 1 is the champion (`best_clip`, from bestClips), slots 2 and
// 3 the showcase's second and third calls (#551, the listener pass). Nothing
// ever said so: a new take simply replaced the old one in its slot. This
// observes those moves -- notices watch the rankings, they never invent a
// second one -- as a diff of slot clip PATHS between two roster snapshots.
// The path is the identity: an in-place rewrite that keeps its path stays
// silent in v1.

/** The three slot paths of one entry: champion, second call, third call;
 * "" where a slot is unfilled. Slot 1 reads `best_clip`, never the table's
 * own slot-1 row (the page renders it the same way, so the notice observes
 * exactly what the reader sees). */
export function slotPaths(e: RosterEntry): string[] {
  const at = (n: number) => e.showcase?.find((s) => s.slot === n)?.clip ?? "";
  return [e.best_clip ?? "", at(2), at(3)];
}

/** Slot clip paths per species, for the species that have any. */
export function championPaths(
  entries: RosterEntry[],
): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const e of entries) {
    const paths = slotPaths(e);
    if (paths.some(Boolean)) out[e.species_sci] = paths;
  }
  return out;
}

export type NewBestChange = {
  entry: RosterEntry;
  /** 1 = champion, 2 = second call, 3 = third call. */
  slot: number;
  clip: string;
  confidence: number | null;
};

/** One change per species whose slot paths moved since `prev`: the LOWEST
 * changed slot (a new champion usually bumps the old one down to slot 2,
 * and that is one piece of news, not two). A slot counts as changed only
 * when it held a clip before and holds a different one now: a species
 * earning its first clip ever is first contact (New Arrivals' moment), a
 * slot filling for the first time is the showcase growing, and a slot
 * emptying (pruned files) is a gap -- none of them news here. Name order
 * for determinism within one snapshot. */
export function newBestChanges(
  prev: Record<string, string[]>,
  entries: RosterEntry[],
): NewBestChange[] {
  const out: NewBestChange[] = [];
  for (const e of entries) {
    const was = prev[e.species_sci];
    if (!was) continue;
    const now = slotPaths(e);
    for (let i = 0; i < 3; i++) {
      if (was[i] && now[i] && now[i] !== was[i]) {
        const slot = i + 1;
        const confidence =
          slot === 1
            ? (e.best_confidence ?? null)
            : (e.showcase?.find((s) => s.slot === slot)?.confidence ?? null);
        out.push({ entry: e, slot, clip: now[i], confidence });
        break;
      }
    }
  }
  return out.sort((a, b) =>
    a.entry.species_common.localeCompare(b.entry.species_common),
  );
}

/** The strip's headline for a slot change -- the owner's ask: friendly and
 * excited, the bird named, the moment plain. */
export function newBestTitle(common: string, slot: number): string {
  return slot === 1
    ? `We've captured a new best recording of the ${common}!`
    : `We've captured a new call from the ${common}!`;
}

/** The line under it: which slot moved, how confident Earl was, and the
 * invitation to press play (the strip carries the clip). */
export function newBestBody(slot: number, confidence: number | null): string {
  const parts: string[] = [];
  if (slot === 2) parts.push("a new second call for its showcase");
  if (slot === 3) parts.push("a new third call for its showcase");
  if (confidence != null) parts.push(`confidence ${confidence.toFixed(2)}`);
  parts.push("press play to hear it");
  return parts.join(" · ");
}

/** The notice for a slot change: the stamp says "new best recording", the
 * headline is the moment, the body the slot and confidence, and `clip` is
 * the take itself for the strip's play slot. Id = species + clip path
 * (newBestNoticeId), so the same take never nags after a dismiss and a
 * later one for the same bird announces again. */
export function newBestNotice(c: NewBestChange): AviaryNotice {
  const e = c.entry;
  // The moment is the take's recording (#602): the champion's `best_ts`,
  // a showcase slot's own `ts`; null when the roster carries neither, and
  // the strip then says nothing rather than guessing at the poll clock.
  const ts =
    c.slot === 1
      ? (e.best_ts ?? null)
      : (e.showcase?.find((s) => s.slot === c.slot)?.ts ?? null);
  return {
    id: newBestNoticeId(e.species_sci, c.clip),
    kind: "new_best",
    title: newBestTitle(e.species_common, c.slot),
    body: newBestBody(c.slot, c.confidence),
    href: `/aviary/${encodeURIComponent(e.species_sci)}`,
    species_sci: e.species_sci,
    species_common: e.species_common,
    portrait_file: e.image_file ?? null,
    clip: c.clip,
    ts,
  };
}

/** Fold freshly detected notices into the session's queue: one per species,
 * the newest champion replacing an older undismissed notice for the same
 * bird (a "new best" for a clip no longer on top would be a lie), appended
 * in arrival order so the strip's within-kind order is first-come. */
export function mergeNewBestNotices(
  prev: AviaryNotice[],
  incoming: AviaryNotice[],
): AviaryNotice[] {
  if (incoming.length === 0) return prev;
  const replaced = new Set(incoming.map((n) => n.species_sci));
  return [...prev.filter((n) => !replaced.has(n.species_sci)), ...incoming];
}

// --- Desk-check samples: ?notice=<kind[,kind]|all> ---------------------------
//
// Real notices are rare by design (that is the point of them), so nobody
// should have to wait a week to see what one looks like. The flag dresses a
// REAL roster species in placeholder numbers, so the portrait thumb and the
// click-through are the genuine article; only the story is made up. Ids
// wear a `demo:` prefix so a sample never collides with a real notice's
// dismiss key, and the hook forgets a sample's dismissal on each page load
// so the flag always shows it again. `all` queues every kind, which also
// walks the queue: dismiss reveals the next by rank. Not a product surface.

/** The kinds `?notice=all` stands for, in rank order. */
export const DEMO_NOTICE_KINDS = ["rare_return", "new_best", "demo"] as const;

/** One sample notice of `kind`, dressed on `model` (a real roster entry);
 * null for an unknown kind, or for a species kind with no model to dress. */
export function sampleNotice(
  kind: string,
  model: RosterEntry | null,
  now: number,
  firstHeardOf: DayFormatter,
): AviaryNotice | null {
  // Every sample is stamped `now` (#602) so the flag desk-checks the
  // moment line too; the rare return gets it through last_ts, the others
  // directly. The rare sample keeps the model's REAL first_ts, so the
  // body's first-heard day (#603) desk-checks the year rule on a species
  // the yard actually met last year.
  if (kind === "demo") return { ...demoNotice(), ts: now };
  if (!model) return null;
  if (kind === "rare_return")
    return {
      ...rareReturnNotice({ ...model, visits: 3, last_ts: now }, firstHeardOf),
      id: "demo:rare_return",
    };
  if (kind === "new_best")
    return {
      ...newBestNotice({
        entry: model,
        slot: 1,
        clip: model.best_clip ?? "demo/sample.wav",
        confidence: model.best_confidence ?? 0.97,
      }),
      id: "demo:new_best",
      ts: now,
    };
  return null;
}

/** Parse the flag and build its samples. The model species is the first by
 * name that has a portrait (so the thumb is real), else the first by name,
 * else none -- a page with no roster (the profile) can only show `demo`. */
export function demoNoticesFor(
  flag: string | null | undefined,
  entries: RosterEntry[],
  now: number,
  firstHeardOf: DayFormatter,
): AviaryNotice[] {
  if (!flag) return [];
  const kinds =
    flag === "all" ? [...DEMO_NOTICE_KINDS] : flag.split(",").map((k) => k.trim());
  const byName = [...entries].sort((a, b) =>
    a.species_common.localeCompare(b.species_common),
  );
  const model = byName.find((e) => e.image_file) ?? byName[0] ?? null;
  const out: AviaryNotice[] = [];
  for (const k of kinds) {
    const n = sampleNotice(k, model, now, firstHeardOf);
    if (n && !out.some((o) => o.id === n.id)) out.push(n);
  }
  return out;
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

export type RhythmTier = "peak" | "second" | "rest";
export type RhythmCell = { frac: number; count: number; tier: RhythmTier };

/** How many hours the second tier may hold: the peak window is three hours
 * wide (species_analysis.peak_window's default), so its runners-up get the
 * same allowance. */
const RHYTHM_SECOND_TIER_HOURS = 3;
/** A second-tier hour must hold at least this share of the busiest hour, so
 * a flat tail never lights arbitrary hours just for being non-zero. */
const RHYTHM_SECOND_TIER_FLOOR = 0.25;

/** stats_json's 24 hour counts -> the rhythm strip's cells (#558): bar
 * fraction, the raw count for the hover bubble, and a tier. `peak` is the
 * stored peak window (wrapping midnight the way peak_window does -- an
 * owl's peak is one stretch, not two); `second` is the up-to-three busiest
 * hours OUTSIDE it, each holding at least a quarter of the busiest hour,
 * ties to the earlier hour -- non-contiguous by design, the shoulders of
 * the peak more often than not; everything else is `rest`. SERVER-local
 * hours by design: these must match the prose beside them, not the chart
 * below (see the section note). Null when the stats carry no usable
 * histogram; all-zero hours are honest flat cells, not NaN. */
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
  const second = new Set(
    hours
      .map((count, h) => ({ count, h }))
      .filter(
        ({ count, h }) =>
          !inPeak(h) && count > 0 && count >= max * RHYTHM_SECOND_TIER_FLOOR,
      )
      .sort((a, b) => b.count - a.count || a.h - b.h)
      .slice(0, RHYTHM_SECOND_TIER_HOURS)
      .map(({ h }) => h),
  );
  return hours.map((count, h) => ({
    frac: max > 0 ? count / max : 0,
    count,
    tier: inPeak(h) ? "peak" : second.has(h) ? "second" : "rest",
  }));
}

/** 0 -> "12am", 13 -> "1pm": the clock the prose speaks in
 * (species_analysis.hour_label, mirrored). */
function rhythmHourLabel(h: number): string {
  const hour = h % 12 || 12;
  return `${hour}${h < 12 ? "am" : "pm"}`;
}

/** The hour figure's caption (#626): the stored peak window in the prose's
 * clock -- "busiest 6am–9am" -- read verbatim from stats_json, never
 * recomputed from the histogram (the end hour is exclusive, as
 * species_analysis.peak_window stores it). Null without a window. */
export function rhythmCaption(stats: AnalysisStats | null): string | null {
  const peak = stats?.peak_window;
  if (!peak || !Array.isArray(stats?.hours)) return null;
  return `busiest ${rhythmHourLabel(peak.start_hour)}–${rhythmHourLabel(peak.end_hour)}`;
}

/** The rhythm strip's hover bubble, one line: the hour's span in the
 * prose's clock and its visit count -- "6am–7am · 312 visits". Singular at
 * one, "no visits" at zero, thousands separated past 999 (the house rule). */
export function rhythmCellExplain(hour: number, cell: RhythmCell): string {
  const span = `${rhythmHourLabel(hour)}–${rhythmHourLabel((hour + 1) % 24)}`;
  const visits =
    cell.count === 0
      ? "no visits"
      : cell.count === 1
        ? "1 visit"
        : `${cell.count.toLocaleString("en-US")} visits`;
  return `${span} · ${visits}`;
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

// Buckets that aren't already usable as "in ___ weather" get a grammar fix.
// Only `rain` needs it ("in rain weather" reads wrong); every temperature band
// and the other sky buckets (clear, cloudy) are adjectives already.
const WEATHER_ADJ: Record<string, string> = { rain: "rainy" };

/** One plain-language sentence decoding a weather chip for its hover tooltip
 * (issue #276) -- the figure shows "+59% MILD", this says what that means, and
 * the paragraph beneath carries the interpretation the tooltip deliberately
 * doesn't. Reads the SAME exposure-normalised effect the chip carries, never a
 * count (the section's standing rule). A −100% finding (rounded: zero visits in
 * the bucket) reads "almost never" rather than an unhedged "never"; thin
 * findings append the caveat the dashed border signals visually. */
export function weatherChipExplain(chip: StatChip): string {
  const phrase = WEATHER_ADJ[chip.label] ?? chip.label;
  let s: string;
  if (chip.pct <= -100) {
    s = `Almost never seen in ${phrase} weather.`;
  } else if (chip.pct > 0) {
    s = `Seen ${chip.pct}% more often in ${phrase} weather than its overall average.`;
  } else {
    s = `Seen ${Math.abs(chip.pct)}% less often in ${phrase} weather than its overall average.`;
  }
  return chip.thin ? `${s} Based on thin data — few visits.` : s;
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


/** The masthead's second line, station-aware (#304, epic #212): a remote
 * station says whose yard it is; at home (no name configured) the line is
 * "earl with the ears". Blank and whitespace collapse to home --
 * half-configured is not a third voice. The trailing tagline was dropped
 * so the line stays single-row on phone widths. */
export function mastheadSub(stationName?: string | null): string {
  const name = (stationName ?? "").trim();
  return name || "earl with the ears";
}

/**
 * Display title for the Aviary H1. Default "The Aviary"; stations set
 * MERLE_AVIARY_TITLE (e.g. "Karen's Aviary") in station.env.
 */
export function aviaryTitle(title?: string | null): string {
  const t = (title ?? "").trim();
  return t || "The Aviary";
}

/**
 * Runtime station branding from process.env.
 *
 * Next inlines `process.env.FOO` at **build** time when FOO is a static
 * property access. Birdhouse builds without MERLE_STATION_NAME set, so the
 * compiled page baked `undefined` forever and the masthead stayed
 * "earl with the ears" even after station.env was filled and mcc restarted.
 * Bracket access + force-dynamic pages read the live systemd EnvironmentFile.
 */
export function stationBrandingFromEnv(): {
  stationName: string | undefined;
  title: string;
} {
  const env = process.env;
  const stationName = (env["MERLE_STATION_NAME"] ?? "").trim() || undefined;
  const title = aviaryTitle(env["MERLE_AVIARY_TITLE"]);
  return { stationName, title };
}


// --- The on-demand field-notes refresh (#613) --------------------------------

/** The refresh route for one species (scientific name, URL-encoded). Who
 * writes is the unit's MERLE_LLM_CHAIN (#617): the page asks for an
 * entry, never for a writer (the second, cloud-only control of #632 was
 * retired once the chain made this one do its job). */
export function refreshUrl(sci: string): string {
  return `/aviary/analysis/${encodeURIComponent(sci)}/refresh`;
}

/** Past this the page stops waiting: the server kills the pass at five
 * minutes (lib/notesRefresh.ts), so half a minute more is only slack. */
export const REFRESH_DEADLINE_S = 330;
/** How long a failure message stays before the control is offered again. */
export const REFRESH_HOLD_S = 8;

export type RefreshUi =
  | { kind: "idle" }
  | { kind: "writing"; since: number }
  | { kind: "failed"; reason: "busy" | "model"; at: number };

export type RefreshEvent =
  | { type: "click" }
  | { type: "refused"; busy: boolean }
  | { type: "status"; state: "idle" | "running" | "done" | "failed" }
  | { type: "tick" };

/** The control's state machine, pure (times in epoch seconds). idle →
 * writing on click; writing → idle when the server says done (the caller
 * refetches the notes on exactly that transition); writing → failed when
 * the POST is refused, the server says failed, or the deadline passes;
 * failed → idle after the hold. A server status of "idle" while writing
 * (the MCC restarted mid-run) changes nothing -- the deadline settles it. */
export function refreshStep(
  s: RefreshUi,
  e: RefreshEvent,
  now: number,
): RefreshUi {
  switch (e.type) {
    case "click":
      return s.kind === "idle" ? { kind: "writing", since: now } : s;
    case "refused":
      return s.kind === "writing"
        ? { kind: "failed", reason: e.busy ? "busy" : "model", at: now }
        : s;
    case "status":
      if (s.kind !== "writing") return s;
      if (e.state === "done") return { kind: "idle" };
      if (e.state === "failed")
        return { kind: "failed", reason: "model", at: now };
      return s;
    case "tick":
      if (s.kind === "writing" && now - s.since > REFRESH_DEADLINE_S)
        return { kind: "failed", reason: "model", at: now };
      if (s.kind === "failed" && now - s.at >= REFRESH_HOLD_S)
        return { kind: "idle" };
      return s;
  }
}

/** The words in the control's one reserved line, per state. */
export function refreshLabel(s: RefreshUi): string {
  if (s.kind === "writing") return "writing… (this can take a minute)";
  if (s.kind === "failed")
    return s.reason === "busy"
      ? "another entry is being written, try again shortly"
      : "couldn't reach a model";
  return "write a fresh entry";
}


// --- The field journal (#614) -------------------------------------------------

/** One dated entry, as /aviary/analysis/[species] ships it (newest first). */
export type JournalEntry = {
  id: number;
  entry_ts: number;
  trigger: string;
  title: string;
  text: string;
  model: string | null;
  fallback: boolean;
};

export type FieldNote = {
  title: string;
  text: string;
  /** The small stamp beside the heading: why the note was written. */
  stamp: string;
  ts: number | null;
  /** Who set it down: the model, or "plain record" (#612). */
  author: string | null;
  /** A pre-#626 sketch or rhythm block carried forward on the stats row,
   * shown until the bird's first entry lands. */
  legacy: boolean;
};

/** The ONE note a bird's page shows (#626): its newest dated entry, else
 * the prose a pre-#626 row still carries (the standing sketch from version
 * 4, the rhythm and weather blocks below it -- joined, since the page has
 * one place for prose now), else nothing. The stats row never erases that
 * old prose, so a deploy can't blank a page whose first entry is still
 * queued. */
export function fieldNote(a: {
  entries?: JournalEntry[] | null;
  rhythm?: string | null;
  weather?: string | null;
  generated_ts?: number | null;
  model?: string | null;
  fallback?: boolean | null;
} | null): FieldNote | null {
  const e = a?.entries?.[0];
  if (e)
    return {
      title: e.title,
      text: e.text,
      stamp: journalStamp(e.trigger),
      ts: e.entry_ts,
      author: e.fallback ? "plain record" : e.model,
      legacy: false,
    };
  if (!a?.rhythm && !a?.weather) return null;
  return {
    title: a.weather ? "the rhythm" : "the sketch",
    text: [a.rhythm, a.weather].filter((t): t is string => !!t).join("\n"),
    stamp: "standing",
    ts: a.generated_ts ?? null,
    author: a.fallback ? "plain record" : (a.model ?? null),
    legacy: true,
  };
}

const TRIGGER_STAMPS: Record<string, string> = {
  first: "first entry",
  weekly: "this week",
  routine: "routine",
  arrival: "arrival",
  departure: "departure",
  record: "a record",
  anomaly: "a caution",
  refresh: "on request",
  compare: "second opinion",
};

/** The small stamp on an entry's heading: why it was written. */
export function journalStamp(trigger: string): string {
  return TRIGGER_STAMPS[trigger] ?? trigger;
}

/** The earlier-entries disclosure's words (zero earlier: no control). */
export function earlierLabel(count: number, open: boolean): string | null {
  if (count <= 0) return null;
  if (open) return "hide earlier entries";
  return `earlier entries (${count.toLocaleString("en-US")})`;
}
