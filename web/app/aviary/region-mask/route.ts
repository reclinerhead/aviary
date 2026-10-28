// The species mask (issue #593): what Earl's geo model allows here THIS
// week, as the listener last published it on audio/mask and sightings.py
// stored it in region_mask + region_mask_meta (current state only, this box
// only). The /aviary/roster shape verbatim: node:sqlite, read-only
// per-request open, MERLE_EARL_DB with NO DEFAULT (the two-WorkingDirectories
// lesson), no-store.
//
// GET /aviary/region-mask  ->  { mask: {ts, week, status, error} | null,
//                               species: [{species_sci, species_common, geo}] }
//
// Its own segment, not /aviary/mask: the App Router refuses a route.ts
// beside a page.tsx in one segment, and /aviary/mask is the PAGE (the
// third masthead tab). Named for the table it reads.
//
// `mask: null` is "no mask recorded yet" -- unset env, no store, or a store
// the listener has never reported into (the deploy-order case: the page
// before the listener, or the listener before sightings). Two tiny queries
// merged in code, the RULES_SQL posture: a whole optional pair of tables
// asked about on their own, never a rung on the roster's cascade. A store
// without the tables answers exactly like one with them empty.

import { DatabaseSync } from "node:sqlite";
import { maskPayloadFrom, type MaskPayload, type MaskRow } from "@/lib/aviary";

const META_SQL =
  "SELECT ts, week, status, error FROM region_mask_meta WHERE id = 1";
const ROWS_SQL =
  "SELECT species_sci, species_common, geo FROM region_mask" +
  " ORDER BY species_sci";

function answer(body: MaskPayload) {
  return Response.json(body, {
    // The mask changes on the week tick and the roster lights it all day;
    // nothing here is immutable.
    headers: { "cache-control": "no-store" },
  });
}

const none = () => answer({ mask: null, species: [] });

export async function GET() {
  const path = process.env.MERLE_EARL_DB;
  if (!path) return none();

  let db: DatabaseSync;
  try {
    db = new DatabaseSync(path, { readOnly: true });
  } catch {
    return none(); // no bird record yet, or not ours to read
  }
  try {
    const meta = db.prepare(META_SQL).get() as
      | Parameters<typeof maskPayloadFrom>[0]
      | undefined;
    const rows = db.prepare(ROWS_SQL).all() as MaskRow[];
    return answer(maskPayloadFrom(meta, rows));
  } catch {
    return none(); // a store without the tables yet: no mask recorded
  } finally {
    db.close();
  }
}
