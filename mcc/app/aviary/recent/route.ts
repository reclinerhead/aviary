// The Latest Events ticker's hydration (epic #182 Phase 1, issue #183):
// the newest sightings from MERLE_EARL_DB, shaped like audio/events bus
// payloads -- the /weather/history idiom, one namespace over: the body is
// byte-shaped like the wire, so lib/bus's audioEventFrom() reads both and
// the ticker can't tell a hydrated bird from a live one. Sound events
// (kind:"sound") never appear here because sightings.py deliberately
// doesn't persist them -- bus-only, gone on reload (accepted in #182).
//
// GET /aviary/recent?limit=<n>              ->  { events: [...] }  newest first
// GET /aviary/recent?species=<sci>&limit=<n>    the profile's per-species cut
//
// The limit is clamped at both ends of the wire (parseLimit here mirrors the
// range clamps of #105); `species` is an exact species_sci match,
// parameterized, so it needs no scrubbing. Unset env, missing DB, and a
// store without the table yet all answer a QUIET { events: [] } -- day one's
// normal state. No default path -- the roster route's WorkingDirectory
// reasoning, verbatim.

import { DatabaseSync } from "node:sqlite";
import type { NextRequest } from "next/server";
import { detectionFromRow, parseLimit } from "@/lib/aviary";

// Named columns, not SELECT * -- the row shape the client parses is a
// contract, not whatever the table happens to hold. ORDER BY id: insertion
// order, which keeps two species sharing a window's ts stable.
const COLUMNS =
  "ts, source, species_sci, species_common, confidence, clip, " +
  "wind_suspect, rms";

const empty = () => events([]);

function events(rows: unknown[]) {
  return Response.json(
    { events: rows },
    { headers: { "cache-control": "no-store" } },
  );
}

export async function GET(req: NextRequest) {
  const path = process.env.MERLE_EARL_DB;
  if (!path) return empty();

  const q = req.nextUrl.searchParams;
  const limit = parseLimit(q.get("limit"));
  const sci = q.get("species");

  let db: DatabaseSync;
  try {
    db = new DatabaseSync(path, { readOnly: true });
  } catch {
    return empty();
  }
  try {
    const rows = (
      sci
        ? db
            .prepare(
              `SELECT ${COLUMNS} FROM sightings WHERE species_sci = ?
               ORDER BY id DESC LIMIT ?`,
            )
            .all(sci, limit)
        : db
            .prepare(
              `SELECT ${COLUMNS} FROM sightings ORDER BY id DESC LIMIT ?`,
            )
            .all(limit)
    ) as Parameters<typeof detectionFromRow>[0][];
    return events(rows.map(detectionFromRow));
  } catch {
    return empty();
  } finally {
    db.close();
  }
}
