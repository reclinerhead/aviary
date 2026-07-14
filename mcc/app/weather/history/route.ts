// Serves the seasonal weather archive (issue #105) from MERLE_WEATHER_DB --
// the SQLite file weather_archive.py fills on pearl. Like /frames/[id], and
// unlike everything else the dashboard fetches, this does NOT ride the
// /daemon proxy: merle.db lives on bluejay, and daemon-down is the steady
// state for the 24/7 MCC, so a weather archive behind the daemon's HTTP
// surface would have holes exactly when bluejay naps. The writer runs on
// pearl 24/7 and the reader is this route, off the same local disk.
//
// GET /weather/history?from=<epoch>&to=<epoch>  ->  { points: [...] }
//
// The body is byte-shaped like a weather/history bus payload, so lib/weather's
// parsePoints() reads both and the chart can't tell an archived point from a
// retained one. Epoch seconds, not ISO -- the weather bus is epoch end to end.
//
// Unset env, missing DB, and a bad or empty range all answer a QUIET
// { points: [] } (the /frames reasoning: an empty archive is the normal state
// on day one, not a journal line). Not a 404 -- unlike a pruned frame, "no
// data here" is a chartable answer, and the chart already draws gaps honestly.
//
// MERLE_WEATHER_DB has NO DEFAULT here, deliberately: weather.py defaults it
// to `weather.db` relative to its WorkingDirectory, but the mcc-dashboard
// unit's WorkingDirectory is the `mcc/` subdirectory, so the same relative
// default would name a DIFFERENT FILE and this route would quietly serve an
// empty archive that nothing ever writes to. The unit must carry an absolute
// path matching willard-weather's -- the MERLE_FRAMES_DIR coupling exactly.

import { DatabaseSync } from "node:sqlite";
import type { NextRequest } from "next/server";
import { parseRange } from "@/lib/weather";

// The columns of weather_archive.SCHEMA, in HISTORY_FIELDS order. Named
// explicitly rather than SELECT *: the row shape the client parses is a
// contract, not whatever the table happens to hold.
const COLUMNS =
  "ts, temp_f, wind_mph, wind_gust_mph, condition, humidity_pct, " +
  "dew_point_f, pressure_rel_inhg, rain_rate_inhr, rain_day_in, " +
  "solar_wm2, uv_index";

// Both ends inclusive, oldest first -- weather_archive.observations().
const SQL = `SELECT ${COLUMNS} FROM observations
             WHERE ts >= ? AND ts <= ? ORDER BY ts`;

const empty = () => points([]);

function points(rows: unknown[]) {
  return Response.json({ points: rows }, {
    // A time range's contents grow, so nothing here is immutable -- the
    // /frames route's `immutable` header would be a lie at this address.
    headers: { "cache-control": "no-store" },
  });
}

export async function GET(req: NextRequest) {
  const path = process.env.MERLE_WEATHER_DB;
  if (!path) return empty();

  const q = req.nextUrl.searchParams;
  const range = parseRange(q.get("from"), q.get("to"));
  if (!range) return empty();

  // Opened per request, not cached in a module: the archive is a file the MCC
  // doesn't own, and on a fresh pearl this route can easily serve traffic
  // before willard-weather has created it. A cached handle would pin that
  // day-one miss (or a replaced file) until the next deploy, and a SQLite
  // open against a local file is microseconds -- this is a hand-fetched
  // panel, not the 1s /state poll.
  let db: DatabaseSync;
  try {
    db = new DatabaseSync(path, { readOnly: true });
  } catch {
    return empty(); // no archive yet, or not ours to read
  }
  try {
    return points(db.prepare(SQL).all(range.from, range.to));
  } catch {
    return empty(); // an archive without the table yet: still just no data
  } finally {
    db.close();
  }
}
