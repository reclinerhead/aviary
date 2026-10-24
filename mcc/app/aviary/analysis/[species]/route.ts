// The field-naturalist blocks (epic #182 Phase 4, issue #186), read from
// earl.db's species_analysis table -- written by listener/species_analysis.py
// on pearl, never by this app. The /weather/history idiom throughout:
// node:sqlite, read-only per request, quiet empties, no default path.
//
// GET /aviary/analysis/<species_sci>
//   -> { rhythm, weather, stats, generated_ts, model, visits_watermark,
//        fallback } | nulls
//
// `fallback` (#612) is true when the pass stored the plain record because
// the model's drafts broke the validator twice. Read with SELECT * rather
// than by name so a store whose pass hasn't added the column yet still
// answers (deploy order: the MCC may rebuild before the loop's next tick
// upgrades the table) -- a missing column reads as false, not as an error
// that would blank every note on the page.
//
// `stats` (#220) is the pass's stored stats_json, parsed -- the exact
// numbers the prose was written from, shipped so the margin figures and the
// writing can never disagree. Parsed here rather than in the browser so a
// corrupt blob degrades to null once, at the wire, not in a component.
//
// Its own route rather than more columns on /aviary/roster: the grid asks
// for the roster and would carry two paragraphs per species it never
// renders. The profile is the only reader, and it wants exactly one bird's.
//
// **Nothing here generates anything.** The prose is produced by a pass, on
// a schedule a human chooses; this route serves whatever the store holds and
// answers a quiet empty when the pass hasn't run for this species yet (the
// honest day-one state, which the page renders as its own sentence).

import { DatabaseSync } from "node:sqlite";

const SQL = "SELECT * FROM species_analysis WHERE species_sci = ?";

type Body = {
  rhythm: string | null;
  weather: string | null;
  stats: unknown | null;
  model: string | null;
  visits_watermark: number | null;
  generated_ts: number | null;
  fallback: boolean;
};

const EMPTY: Body = {
  rhythm: null,
  weather: null,
  stats: null,
  model: null,
  visits_watermark: null,
  generated_ts: null,
  fallback: false,
};

/** The stored blob, or null -- a row predating stats, or a corrupt one,
 * just has no figures. */
function parseStats(raw: string | null): unknown | null {
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function body(b: Body) {
  return Response.json(b, {
    // Regenerated whenever the pass next runs, so never immutable.
    headers: { "cache-control": "no-store" },
  });
}

export async function GET(
  _req: Request,
  ctx: { params: Promise<{ species: string }> },
) {
  const path = process.env.MERLE_EARL_DB;
  if (!path) return body(EMPTY);

  const { species } = await ctx.params;
  let db: DatabaseSync;
  try {
    db = new DatabaseSync(path, { readOnly: true });
  } catch {
    return body(EMPTY);
  }
  try {
    const row = db.prepare(SQL).get(decodeURIComponent(species)) as
      | {
          rhythm_text: string | null;
          weather_text: string | null;
          stats_json: string | null;
          model: string | null;
          visits_watermark: number;
          generated_ts: number;
          fallback?: number | null; // absent on a pre-#612 table
        }
      | undefined;
    if (!row) return body(EMPTY);
    return body({
      rhythm: row.rhythm_text,
      weather: row.weather_text,
      stats: parseStats(row.stats_json),
      model: row.model,
      visits_watermark: row.visits_watermark,
      generated_ts: row.generated_ts,
      fallback: row.fallback === 1,
    });
  } catch {
    // A store predating the analysis pass has no such table -- the same
    // quiet empty, not an error at a viewer.
    return body(EMPTY);
  } finally {
    db.close();
  }
}
