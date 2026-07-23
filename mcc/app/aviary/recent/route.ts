// The Latest Events ticker's hydration (epic #182 Phase 1, issue #183) and
// the event archive's pages (#211): the newest sightings from MERLE_EARL_DB,
// shaped like audio/events bus payloads -- the /weather/history idiom, one
// namespace over: the body is byte-shaped like the wire, so lib/bus's
// audioEventFrom() reads both and the ticker can't tell a hydrated bird from
// a live one. Sound events (kind:"sound") never appear here because
// sightings.py deliberately doesn't persist them -- bus-only, gone on reload
// (accepted in #182).
//
// GET /aviary/recent?limit=<n>              ->  { events: [...] }  newest first
// GET /aviary/recent?species=<sci>&since=<epoch>&limit=<n>
//                                               the profile's per-species cut
// GET /aviary/recent?species=<sci>,<sci>&before=<epoch>&limit=<n>
//                                               the archive's filtered page
//
// The limit is clamped at both ends of the wire (parseLimit here mirrors the
// range clamps of #105); `species` is one or more exact species_sci matches
// (comma-separated -- scientific names never contain commas), parameterized,
// so it needs no scrubbing. `before` (#211) is an INCLUSIVE ts ceiling: the
// archive re-requests its own oldest row and dedupes by audioEventKey, so a
// same-second sighting straddling a page boundary is a no-op, never a
// dropped bird. `since` (#276) is an INCLUSIVE ts floor -- the profile's
// Recent Visits asks only for the last 24 hours (parseSince clamps a bogus
// value to a recent window, never letting it scan the whole store); the full
// archive is a click away via the page's "browse the full record" link, which
// carries no `since`. Unset env, missing DB, and a store without the table yet
// all answer a QUIET { events: [] } -- day one's normal state. No default
// path -- the roster route's WorkingDirectory reasoning, verbatim.

import { DatabaseSync } from "node:sqlite";
import type { NextRequest } from "next/server";
import {
  detectionFromRow,
  parseBefore,
  parseLimit,
  parseSince,
  parseSpeciesFilter,
} from "@/lib/aviary";

// Named columns, not SELECT * -- the row shape the client parses is a
// contract, not whatever the table happens to hold. ORDER BY ts DESC with id
// as the tiebreak (#211): the cursor paginates on ts, and cursor and order
// MUST ride the same axis -- the previous bare `id DESC` matched ts order
// only by the coincidence of sequential insertion, and a store with any
// out-of-order timestamps (measured on the dev copy) jumbled pages and broke
// the inclusive-overlap contract. id still breaks same-second ties, which
// keeps two species sharing a window stable.
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
  const species = parseSpeciesFilter(q.get("species"));
  const now = Math.floor(Date.now() / 1000);
  const before = parseBefore(q.get("before"), now);
  const since = parseSince(q.get("since"), now);

  // The WHERE assembles from whichever filters arrived; placeholders only,
  // so the species list rides IN (?, ...) at exactly its parsed length.
  const where: string[] = [];
  const params: (string | number)[] = [];
  if (species.length > 0) {
    where.push(
      `species_sci IN (${species.map(() => "?").join(", ")})`,
    );
    params.push(...species);
  }
  if (before !== null) {
    where.push("ts <= ?");
    params.push(before);
  }
  if (since !== null) {
    where.push("ts >= ?");
    params.push(since);
  }
  const sql =
    `SELECT ${COLUMNS} FROM sightings` +
    (where.length ? ` WHERE ${where.join(" AND ")}` : "") +
    " ORDER BY ts DESC, id DESC LIMIT ?";

  let db: DatabaseSync;
  try {
    db = new DatabaseSync(path, { readOnly: true });
  } catch {
    return empty();
  }
  try {
    const rows = db
      .prepare(sql)
      .all(...params, limit) as Parameters<typeof detectionFromRow>[0][];
    return events(rows.map(detectionFromRow));
  } catch {
    return empty();
  } finally {
    db.close();
  }
}
