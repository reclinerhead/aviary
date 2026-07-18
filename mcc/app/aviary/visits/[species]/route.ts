// The visits-over-time chart's data (epic #182 Phase 3, issue #185): one
// species' VISIT OPENINGS over a time range, from MERLE_EARL_DB. The
// /weather/history shape again -- node:sqlite, read-only per request, quiet
// empties, range clamped at both ends of the wire, no default path.
//
// GET /aviary/visits/<species_sci>?from=<epoch>&to=<epoch>
//   -> { visits: [<epoch>, ...], first_ts: <epoch>|null }
//
// WHAT THIS RETURNS AND WHY IT ISN'T PRE-BUCKETED. The issue sketched
// server-side bucketing into local days; that's wrong here for the reason
// Phase 1 already encoded in `parseSince` -- **the server cannot know the
// viewer's timezone**, so "local day" is a claim only the browser can make.
// Splitting it: the server owns the subtle half (the 60-second visit
// grouping, which must match gate.VisitTracker exactly or a pre-#175 day
// reads 25 visits instead of 1) and ships one integer per visit; the client
// buckets those into ITS local days (lib/aviary.dayBuckets). The payload is
// an array of epochs -- lighter than the day-count objects it replaces,
// and the grouping stays in one place for both the chart and the roster.
//
// `first_ts` rides along so the chart knows where the record BEGINS: days
// before a species was first heard are not drawn as zeros (absence of
// record is not absence of bird), and a chart that stopped at the fetch
// window's edge couldn't tell the difference.

import { DatabaseSync } from "node:sqlite";
import type { NextRequest } from "next/server";
import { VISIT_GAP_S } from "@/lib/aviary";

// Ordered so the grouping walk sees real gaps. Only the timestamp is
// needed -- the chart counts visits, it doesn't render their detail.
const SQL =
  "SELECT ts FROM sightings WHERE species_sci = ? AND ts >= ? AND ts <= ?" +
  " ORDER BY ts";
const FIRST_SQL = "SELECT first_ts FROM life_list WHERE species_sci = ?";

// The #105 rule, one namespace over: a typo can't ask for ten years. Anchored
// at `to`, so from=0 returns the newest span rather than scanning the table.
const MAX_SPAN_S = 400 * 86400;

function body(visits: number[], firstTs: number | null) {
  return Response.json(
    { visits, first_ts: firstTs },
    // A range's contents grow (today's visits are still arriving), so
    // nothing here is immutable -- the /weather/history reasoning.
    { headers: { "cache-control": "no-store" } },
  );
}

const empty = () => body([], null);

export async function GET(
  req: NextRequest,
  ctx: { params: Promise<{ species: string }> },
) {
  const path = process.env.MERLE_EARL_DB;
  if (!path) return empty();

  const { species } = await ctx.params;
  const sci = decodeURIComponent(species);
  const q = req.nextUrl.searchParams;
  const to = Number(q.get("to"));
  let from = Number(q.get("from"));
  if (!Number.isFinite(from) || !Number.isFinite(to)) return empty();
  if (to - from > MAX_SPAN_S) from = to - MAX_SPAN_S;

  let db: DatabaseSync;
  try {
    db = new DatabaseSync(path, { readOnly: true });
  } catch {
    return empty();
  }
  try {
    const rows = db
      .prepare(SQL)
      .all(sci, Math.trunc(from), Math.trunc(to)) as { ts: number }[];
    // The visit collapse, inline: rows within VISIT_GAP_S of the previous
    // one continue that visit, so only openings are published -- the same
    // rule tallyVisits applies for the roster's counts. Rows arrive sorted,
    // so this is a single walk.
    const visits: number[] = [];
    let last: number | null = null;
    for (const r of rows) {
      if (last === null || r.ts - last > VISIT_GAP_S) visits.push(r.ts);
      last = r.ts;
    }
    const first = db.prepare(FIRST_SQL).get(sci) as
      | { first_ts: number }
      | undefined;
    return body(visits, first?.first_ts ?? null);
  } catch {
    return empty(); // a store without the tables yet: still just no visits
  } finally {
    db.close();
  }
}
