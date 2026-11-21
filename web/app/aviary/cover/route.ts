// The porch's cover bird (#697): the photo the station launcher (porch/,
// #696) shows on the Aviary's tile, changing once a week.
//
// GET /aviary/cover  ->  { src, caption, credit }
//
// `src` is a same-origin path to the species' full-size portrait, `caption`
// its common name, `credit` the portrait's stored attribution. Who
// qualifies: species on the life list that have a portrait on the shelf and
// were heard in the last seven days; if there are none, every species on
// the life list with a portrait. The pick is lib/aviary.pickCover: a
// function of the week and the candidates, never stored.
//
// The roster route's rule holds: unset MERLE_EARL_DB (no default, for the
// reason that route gives), a missing file, or a store without the tables
// all answer a QUIET three nulls with a 200. The porch keeps its default
// photo for anything but a usable `src`.

import { promises as fs } from "fs";
import path from "path";
import { DatabaseSync } from "node:sqlite";
import {
  pickCover,
  portraitUrl,
  portraitVersion,
  speciesThumbName,
} from "@/lib/aviary";

export const dynamic = "force-dynamic";

const WEEK_S = 7 * 24 * 60 * 60;

// The portrait shelf, shared with the portrait route and the roster.
const SPECIES_DIR = "species";
const SHELF_NAME = /^[A-Za-z0-9_-]+\.jpg$/;

const COVER_SQL =
  "SELECT l.species_sci, l.species_common, p.image_file," +
  " p.image_attribution," +
  " (SELECT MAX(s.ts) FROM sightings s" +
  "   WHERE s.species_sci = l.species_sci) AS last_ts" +
  " FROM life_list l JOIN species_profile p" +
  " ON p.species_sci = l.species_sci" +
  " WHERE p.image_file IS NOT NULL";

type Row = {
  species_sci: string;
  species_common: string | null;
  image_file: string;
  image_attribution: string | null;
  last_ts: number | null;
};

function cover(
  src: string | null = null,
  caption: string | null = null,
  credit: string | null = null,
) {
  return Response.json(
    { src, caption, credit },
    { headers: { "cache-control": "no-store" } },
  );
}

async function statFacts(p: string) {
  try {
    const st = await fs.stat(p);
    return { mtimeMs: st.mtimeMs, size: st.size };
  } catch {
    return null;
  }
}

export async function GET() {
  // Bracket access: a static process.env.FOO is inlined at build time.
  const dbPath = process.env["MERLE_EARL_DB"];
  const clips = process.env["MERLE_EARL_CLIPS"];
  // No shelf, no portrait the portrait route could serve.
  if (!dbPath || !clips) return cover();

  let rows: Row[];
  try {
    const db = new DatabaseSync(dbPath, { readOnly: true });
    try {
      rows = db.prepare(COVER_SQL).all() as Row[];
    } finally {
      db.close();
    }
  } catch {
    return cover(); // no bird record yet, or a store without the tables
  }

  // "Has a portrait" means the file is on the shelf, not that a row names
  // one. The stats also make the version the portrait URL carries.
  const shelf = path.join(clips, SPECIES_DIR);
  const withPortrait = (
    await Promise.all(
      rows
        .filter((r) => SHELF_NAME.test(r.image_file))
        .map(async (r) => {
          const full = await statFacts(path.join(shelf, r.image_file));
          return full ? { ...r, id: r.species_sci, full } : null;
        }),
    )
  ).filter((r) => r !== null);

  const now = new Date();
  const since = Math.floor(now.getTime() / 1000) - WEEK_S;
  const thisWeek = withPortrait.filter(
    (r) => r.last_ts !== null && r.last_ts >= since,
  );
  const pick = pickCover(thisWeek.length > 0 ? thisWeek : withPortrait, now);
  if (!pick) return cover();

  const thumb = await statFacts(
    path.join(shelf, speciesThumbName(pick.image_file)),
  );
  return cover(
    portraitUrl(pick.species_sci, { v: portraitVersion(pick.full, thumb) }),
    pick.species_common ?? pick.species_sci,
    pick.image_attribution,
  );
}
