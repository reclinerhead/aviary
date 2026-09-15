// The Aviary's roster (epic #182 Phase 1, issue #183): the life list joined
// with per-species visit counts, from MERLE_EARL_DB -- the SQLite file
// listener/sightings.py fills on pearl. The /weather/history shape verbatim:
// like /frames/[id], this does NOT ride the /daemon proxy -- the writer runs
// on pearl 24/7 and the reader is this route, off the same local disk.
//
// GET /aviary/roster?today=<epoch>  ->  { species: [...] }
//
// `today` is the CLIENT's local midnight (the server can't know the viewer's
// timezone); parseSince clamps it to within two days of now, so a typo can't
// relabel the whole archive as "today". Visit counts apply the same
// 60-second-gap grouping the listener applies at publish time
// (lib/aviary.tallyVisits mirrors gate.VisitTracker), which is what keeps
// the pre-#175 per-window day from overcounting -- collapsed at query time,
// never rewritten in the store.
//
// Unset env, missing DB, and a file without the tables yet all answer a
// QUIET { species: [] } -- on day one an empty aviary is the normal state,
// not a journal line. MERLE_EARL_DB has NO DEFAULT here, deliberately:
// sightings.py defaults to `earl.db` relative to its WorkingDirectory (the
// repo root), but mcc-dashboard's WorkingDirectory is the `mcc/`
// subdirectory, so the same relative default would name a DIFFERENT FILE
// and this route would quietly serve an empty aviary nothing ever writes to.
// The unit must carry an absolute path matching earl-sightings' -- the
// MERLE_WEATHER_DB coupling exactly.

import { promises as fs } from "fs";
import path from "path";
import { DatabaseSync } from "node:sqlite";
import type { NextRequest } from "next/server";
import {
  parseSince,
  portraitVersion,
  shapeRoster,
  showcaseBySpecies,
  speciesRuleFrom,
  speciesThumbName,
  type SpeciesRule,
} from "@/lib/aviary";

const LIFE_SQL =
  "SELECT species_sci, species_common, first_ts, first_source, first_clip " +
  "FROM life_list";
// The enrichment join (#184): profile prose + portrait provenance ride the
// roster so the grid and profile pages need no second fetch. LEFT JOIN --
// an un-enriched species is honest NULLs, not a missing bird. Since #185 it
// also carries the portrait's dimensions, so the browser can frame a
// portrait-orientation bird without decapitating it.
const JOIN_TAIL =
  " FROM life_list l LEFT JOIN species_profile p" +
  " ON p.species_sci = l.species_sci";
const LIFE_COLS =
  "SELECT l.species_sci, l.species_common, l.first_ts, l.first_source," +
  " l.first_clip, p.description, p.image_file, p.image_source," +
  " p.image_attribution";
// #318's columns ride as their OWN pair and are deliberately not COALESCEd
// with `voice` here: the two texts are under incompatible licences, so which
// one a row carries has to survive the wire alongside its own credit. A
// merged field would also have to be unpicked to ever show both.
const LIFE_JOINED_SQL =
  `${LIFE_COLS}, p.image_w, p.image_h, p.voice, p.voice_source,` +
  ` p.voice_adw, p.voice_adw_attribution${JOIN_TAIL}`;
// #318's rung: everything through `voice_source`, but no ADW columns.
const LIFE_JOINED_PRE318_SQL =
  `${LIFE_COLS}, p.image_w, p.image_h, p.voice, p.voice_source${JOIN_TAIL}`;
// #317's rung: `voice` but not yet `voice_source`. Without its own step a
// store one generation behind would fall past the dimensions too.
const LIFE_JOINED_PRE317_SQL =
  `${LIFE_COLS}, p.image_w, p.image_h, p.voice${JOIN_TAIL}`;
// Older store shapes: species_profile exists but predates a column this
// route selects. Selecting it would throw and drop us straight to the bare
// life list, silently losing every description and portrait -- a real
// regression during the window between deploying this code and the
// enrichment loop's next open (which is what adds the columns; the MCC
// opens read-only and never runs the pass's connect()).
//
// So the fallback is a CASCADE, and it grows a STEP PER COLUMN GENERATION
// rather than widening -- #315 added `voice`, and without its own step a
// store that already had dimensions would fall all the way to the #184
// shape and lose them. Newest shape first, each rung losing only what it
// genuinely lacks: voice+dimensions, then dimensions, then the #184 join,
// then the bare list.
const LIFE_JOINED_PRE315_SQL = `${LIFE_COLS}, p.image_w, p.image_h${JOIN_TAIL}`;
const LIFE_JOINED_PRE185_SQL = `${LIFE_COLS}${JOIN_TAIL}`;
// Every sighting -- the tally walks them all so the visit grouping sees real
// gaps, not a LIMIT's arbitrary edge. Post-debounce this table grows a few
// hundred rows a day; a full scan is milliseconds for years.
//
// `confidence` and `clip` ride along for the best-recording pick (#333);
// `vocal_fill` for the composite rank (#442). shapeRoster finds each
// species' best while walking rows it already had, so exposing the good
// take costs no second query and no second scan. conf+clip have existed
// since #172; vocal_fill needs a pre-#442 fallback rung so an un-upgraded
// store still lists birds (confidence-only ranking).
const SIGHTINGS_SQL =
  "SELECT species_sci, ts, confidence, clip, vocal_fill FROM sightings" +
  " ORDER BY ts";
const SIGHTINGS_PRE442_SQL =
  "SELECT species_sci, ts, confidence, clip FROM sightings ORDER BY ts";
// The operator's detection rules (#506) ride the roster so the profile page
// needs no second fetch -- a SEPARATE tiny query merged in code, deliberately
// not another rung on the LEFT JOIN cascade above (a new rung per column
// generation is that cascade's contract; a whole optional TABLE is cheaper
// to ask about on its own, and a pre-#506 store simply answers with none).
const RULES_SQL =
  "SELECT species_sci, exclude, threshold, always_include FROM species_rules";
// The species showcase (#551): the listener pass's three best recordings per
// species, each a different call. The RULES_SQL shape -- a whole optional
// table asked about on its own, never a cascade rung -- joined to its
// sighting for the date the page stamps beside the confidence. A store the
// pass has never run on has no table and answers with none.
const SHOWCASE_SQL =
  "SELECT s.species_sci, s.slot, s.clip, s.confidence, s.vocal_fill, g.ts" +
  " FROM species_showcase s LEFT JOIN sightings g ON g.id = s.sighting_id" +
  " ORDER BY s.species_sci, s.slot";

const empty = () => species([]);

function species(rows: unknown[]) {
  return Response.json(
    { species: rows },
    // Counts grow all day; nothing here is immutable.
    { headers: { "cache-control": "no-store" } },
  );
}

// The portrait shelf, shared with the portrait route and the pass.
const SPECIES_DIR = "species";

// The pass's scrub means a stored image_file is exactly this shape; a row
// that isn't (a hand-edited store) is skipped rather than handed to
// path.join -- the stat would only leak mtime/size into an opaque token,
// but the clips route's posture is "never walk an unguarded name" and the
// same rule is cheaper kept than argued about.
const SHELF_NAME = /^[A-Za-z0-9_-]+\.jpg$/;

async function statFacts(p: string) {
  try {
    const st = await fs.stat(p);
    return { mtimeMs: st.mtimeMs, size: st.size };
  } catch {
    return null; // shelf file gone: entry ships without a version
  }
}

/** Attach the portrait shelf version (#495) to enriched rows: an opaque
 * portraitVersion() token over the full file's and thumb sibling's stats,
 * so portraitUrl(sci, {v}) URLs can be cached immutable while a replaced
 * or newly-thumbed portrait still surfaces on the next roster fetch. The
 * fs lives HERE, not in shapeRoster -- the shaping stays pure, and ~90
 * async stats against a local SSD are microseconds on a route that already
 * opens SQLite per request. No env, no file: rows pass through untouched
 * and consumers fall back to version-less URLs (the 86400 policy). */
async function withPortraitVersions<T extends { image_file?: string | null }>(
  rows: T[],
): Promise<T[]> {
  const dir = process.env.MERLE_EARL_CLIPS;
  if (!dir) return rows;
  return Promise.all(
    rows.map(async (r) => {
      if (!r.image_file || !SHELF_NAME.test(r.image_file)) return r;
      const full = await statFacts(path.join(dir, SPECIES_DIR, r.image_file));
      if (!full) return r;
      const thumb = await statFacts(
        path.join(dir, SPECIES_DIR, speciesThumbName(r.image_file)),
      );
      return { ...r, image_v: portraitVersion(full, thumb) };
    }),
  );
}

export async function GET(req: NextRequest) {
  const path = process.env.MERLE_EARL_DB;
  if (!path) return empty();

  const today = parseSince(
    req.nextUrl.searchParams.get("today"),
    Math.floor(Date.now() / 1000),
  );

  // Opened per request, not cached in a module -- the /weather/history
  // reasoning: the store is a file the MCC doesn't own, and on a fresh pearl
  // this route can serve traffic before earl-sightings has created it.
  let db: DatabaseSync;
  try {
    db = new DatabaseSync(path, { readOnly: true });
  } catch {
    return empty(); // no bird record yet, or not ours to read
  }
  try {
    // Each older store shape loses only what it genuinely lacks: a #184-era
    // file keeps its prose and photos (just no dimensions), and a pre-#184
    // file still lists every bird. Never blank the aviary because a pass
    // hasn't run yet.
    type LifeRows = Parameters<typeof shapeRoster>[0];
    let life: LifeRows | null = null;
    for (const sql of [
      LIFE_JOINED_SQL,
      LIFE_JOINED_PRE318_SQL,
      LIFE_JOINED_PRE317_SQL,
      LIFE_JOINED_PRE315_SQL,
      LIFE_JOINED_PRE185_SQL,
    ]) {
      try {
        life = db.prepare(sql).all() as LifeRows;
        break;
      } catch {
        // this store predates that column generation; try the next rung
      }
    }
    // The bare list is the floor and is deliberately NOT caught here: a
    // store without life_list at all is "no birds yet", which the outer
    // catch already answers quietly.
    if (life === null) life = db.prepare(LIFE_SQL).all() as LifeRows;
    type SightRows = Parameters<typeof shapeRoster>[1];
    let rows: SightRows;
    try {
      rows = db.prepare(SIGHTINGS_SQL).all() as SightRows;
    } catch {
      // Store predates #442's vocal_fill column — confidence-only best.
      rows = db.prepare(SIGHTINGS_PRE442_SQL).all() as SightRows;
    }
    const rules = new Map<string, SpeciesRule>();
    try {
      for (const r of db.prepare(RULES_SQL).all() as {
        species_sci: string;
        exclude: number | null;
        threshold: number | null;
        always_include: number | null;
      }[])
        rules.set(r.species_sci, speciesRuleFrom(r));
    } catch {
      // Pre-#506 store: no species_rules table, no rules — every entry
      // ships without one and the profile stamp reads "default".
    }
    let showcase: ReturnType<typeof showcaseBySpecies> = {};
    try {
      showcase = showcaseBySpecies(
        db.prepare(SHOWCASE_SQL).all() as Parameters<
          typeof showcaseBySpecies
        >[0],
      );
    } catch {
      // Pre-#551 store: no species_showcase table -- every entry ships with
      // an empty showcase and the page reserves the slots.
    }
    const shaped = shapeRoster(life, rows, today, showcase).map((r) => {
      const rule = rules.get(r.species_sci);
      return rule ? { ...r, rule } : r;
    });
    return species(await withPortraitVersions(shaped));
  } catch {
    return empty(); // a store without the tables yet: still just no birds
  } finally {
    db.close();
  }
}
