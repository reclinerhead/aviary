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

import { DatabaseSync } from "node:sqlite";
import type { NextRequest } from "next/server";
import { parseSince, shapeRoster } from "@/lib/aviary";

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
const LIFE_JOINED_SQL =
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
// Every (species, ts) pair -- the tally walks them all so the visit grouping
// sees real gaps, not a LIMIT's arbitrary edge. Post-debounce this table
// grows a few hundred rows a day; a full scan is milliseconds for years.
const SIGHTINGS_SQL = "SELECT species_sci, ts FROM sightings ORDER BY ts";

const empty = () => species([]);

function species(rows: unknown[]) {
  return Response.json(
    { species: rows },
    // Counts grow all day; nothing here is immutable.
    { headers: { "cache-control": "no-store" } },
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
    const rows = db.prepare(SIGHTINGS_SQL).all() as Parameters<
      typeof shapeRoster
    >[1];
    return species(shapeRoster(life, rows, today));
  } catch {
    return empty(); // a store without the tables yet: still just no birds
  } finally {
    db.close();
  }
}
