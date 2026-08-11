// Species removal (issue #216) + negatives capture (issue #281, epic #278):
// strike a misidentified species from the roster -- rows and roster both --
// WITHOUT discarding the evidence. The operator's removal is a hand-made
// "this isn't a bird" label, and until #281 it was thrown away: rows
// deleted, clips unlinked, and the same mistake free to repopulate the life
// list. Now every removal records what was heard (the negatives table) and
// keeps the audio (the quarantine shelf); only the portrait -- refetchable
// from Wikipedia -- is still deleted. Keyed by SCIENTIFIC name,
// decodeURIComponent'd, exactly like the profile page it serves.
//
// DELETE /aviary/species/<species_sci>
//   -> { species_sci, rows: {sightings, life_list, species_profile,
//        species_analysis}, negatives, quarantined, files }
//
// Rows first, then files, in one transaction: the negative evidence rows
// are INSERTed from the sightings rows (one negative per sighting; a
// sightings-less store synthesizes one per life_list row so first contact
// still leaves a trace), THEN the species' rows leave the four tables. The
// negatives DDL lives here because the removal route is the table's only
// writer -- CREATE TABLE IF NOT EXISTS inside the same transaction, so a
// pre-#281 store upgrades on its first removal and a mid-schema crash
// leaves nothing half-made. The `unmarked` column is the reversibility
// hook: un-marking (a #283 surface) sets it so a real bird never feeds
// suppression -- it does NOT restore deleted rows (the #216 contract
// stands: heard again later is a brand-new lifer).
//
// Files second, after commit: clip + -enh sibling pairs MOVE to
// quarantine/<source>/<file>.wav (removalPlan in lib/aviary.ts -- guarded,
// deduped, three segments so the clips route can't serve them until #283
// decides to); the portrait is unlinked. Missing files are not errors (the
// retention pass may have taken them first); any OTHER move failure logs
// loudly, because a quarantine that silently failed would be #216's
// data-destruction wearing #281's label. A mid-walk crash leaves an honest
// roster, negatives rows already written, and clips that age into the
// retention pass's ordinary horizon -- same story as before, now with the
// labels safe.
//
// This is the MCC's only write to earl.db, and it deliberately does NOT
// wear the readers' quiet-empty semantics: a DELETE that silently removed
// nothing while the UI reported success would be worse than an error, so an
// unopenable or unwritable store answers 500, loudly. Unset env or a store
// that doesn't exist yet answers 404 -- there is no record to remove, and
// a write-mode open would CREATE an empty earl.db as a side effect, which
// a delete must never do.

import { promises as fs } from "fs";
import path from "path";
import { DatabaseSync } from "node:sqlite";
import { clipRelPath, quarantinePath, removalPlan } from "@/lib/aviary";

const TABLES = [
  "sightings",
  "life_list",
  "species_profile",
  "species_analysis",
] as const;

// One negative per removed sighting: the evidence columns Earl recorded,
// plus where the clip lives NOW (the quarantine path -- rows point at the
// shelf, not at a file the same transaction orphaned), when the operator
// removed it, and the un-mark flag. location_id rides along because
// mistakes are per-place (#232): a Wood Duck removed at Home says nothing
// about the park.
const NEGATIVES_DDL = `
CREATE TABLE IF NOT EXISTS negatives (
    id             INTEGER PRIMARY KEY,
    species_sci    TEXT NOT NULL,
    species_common TEXT,
    ts             INTEGER NOT NULL,
    source         TEXT NOT NULL,
    confidence     REAL,
    clip           TEXT,
    wind_suspect   INTEGER NOT NULL DEFAULT 0,
    rms            REAL,
    location_id    INTEGER NOT NULL DEFAULT 1,
    removed_ts     INTEGER NOT NULL,
    unmarked       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS negatives_species
    ON negatives(species_sci, location_id);
`;

type SightingRow = {
  ts: number;
  source: string;
  species_common: string | null;
  confidence: number | null;
  clip: string | null;
  wind_suspect: number | null;
  rms: number | null;
  location_id: number | null;
};

/** The negatives row's clip column: where the file will live after the
 * move, through the same guard the plan runs -- an unguardable row-held
 * path stores NULL, never a path. */
function quarantinedClip(clip: string | null): string | null {
  if (!clip) return null;
  const rel = clipRelPath(clip.split("/"));
  return rel === null ? null : quarantinePath(rel);
}

export async function DELETE(
  _req: Request,
  ctx: { params: Promise<{ species: string }> },
) {
  const dbPath = process.env.MERLE_EARL_DB;
  if (!dbPath) return new Response(null, { status: 404 });
  try {
    await fs.access(dbPath);
  } catch {
    return new Response(null, { status: 404 }); // no store, nothing to remove
  }

  const { species } = await ctx.params;
  const sci = decodeURIComponent(species);

  let db: DatabaseSync;
  try {
    db = new DatabaseSync(dbPath); // read-write: the whole point
  } catch {
    return new Response(null, { status: 500 });
  }

  let plan: ReturnType<typeof removalPlan> = { moves: [], deletes: [] };
  let negatives = 0;
  const rows: Record<(typeof TABLES)[number], number> = {
    sightings: 0,
    life_list: 0,
    species_profile: 0,
    species_analysis: 0,
  };
  try {
    const present = new Set(
      (
        db
          .prepare("SELECT name FROM sqlite_master WHERE type = 'table'")
          .all() as { name: string }[]
      ).map((r) => r.name),
    );

    // The evidence, gathered while the rows still exist to hold it.
    const sightingRows: SightingRow[] = present.has("sightings")
      ? (db
          .prepare(
            "SELECT ts, source, species_common, confidence, clip," +
              " wind_suspect, rms, location_id" +
              " FROM sightings WHERE species_sci = ?",
          )
          .all(sci) as SightingRow[])
      : [];
    const lifeRows = present.has("life_list")
      ? (db
          .prepare(
            "SELECT species_common, first_ts, first_source, first_clip," +
              " location_id FROM life_list WHERE species_sci = ?",
          )
          .all(sci) as {
          species_common: string | null;
          first_ts: number;
          first_source: string;
          first_clip: string | null;
          location_id: number | null;
        }[])
      : [];
    let imageFile: string | null = null;
    if (present.has("species_profile")) {
      const prof = db
        .prepare("SELECT image_file FROM species_profile WHERE species_sci = ?")
        .get(sci) as { image_file: string | null } | undefined;
      imageFile = prof?.image_file ?? null;
    }
    plan = removalPlan(
      [
        ...sightingRows.map((r) => r.clip),
        ...lifeRows.map((r) => r.first_clip),
      ],
      imageFile,
    );

    // A sightings-less lifer (pre-enrichment store, or rows lost some other
    // way) still leaves one negative per life_list row -- first contact is
    // evidence too. When sightings exist, the first sighting IS the lifer's
    // first contact, so synthesizing both would double it.
    const evidence: SightingRow[] = sightingRows.length
      ? sightingRows
      : lifeRows.map((r) => ({
          ts: r.first_ts,
          source: r.first_source,
          species_common: r.species_common,
          confidence: null,
          clip: r.first_clip,
          wind_suspect: null,
          rms: null,
          location_id: r.location_id,
        }));

    const removedTs = Math.floor(Date.now() / 1000);
    db.exec("BEGIN IMMEDIATE");
    try {
      db.exec(NEGATIVES_DDL);
      const insert = db.prepare(
        "INSERT INTO negatives (species_sci, species_common, ts, source," +
          " confidence, clip, wind_suspect, rms, location_id, removed_ts)" +
          " VALUES (?,?,?,?,?,?,?,?,?,?)",
      );
      for (const r of evidence) {
        insert.run(
          sci,
          r.species_common,
          r.ts,
          r.source,
          r.confidence,
          quarantinedClip(r.clip),
          r.wind_suspect ?? 0,
          r.rms,
          r.location_id ?? 1,
          removedTs,
        );
        negatives++;
      }
      for (const t of TABLES) {
        if (!present.has(t)) continue;
        rows[t] = Number(
          db.prepare(`DELETE FROM ${t} WHERE species_sci = ?`).run(sci).changes,
        );
      }
      db.exec("COMMIT");
    } catch (e) {
      try {
        db.exec("ROLLBACK");
      } catch {}
      throw e;
    }
  } catch {
    return new Response(null, { status: 500 });
  } finally {
    db.close();
  }

  // Files second: rows are committed, the roster is already honest, and the
  // negatives rows already name the quarantine paths -- so every move that
  // succeeds makes a row true, and one that fails leaves an honest gap (the
  // pruned-clip convention). ENOENT is the designed common case (retention
  // got there first, or a sibling was never written); anything else gets
  // ink, because a quarantine failing for permissions would silently turn
  // #281 back into deletion.
  let quarantined = 0;
  let files = 0;
  const clipsDir = process.env.MERLE_EARL_CLIPS;
  if (clipsDir) {
    for (const { from, to } of plan.moves) {
      const dest = path.join(clipsDir, to);
      try {
        await fs.mkdir(path.dirname(dest), { recursive: true });
        await fs.rename(path.join(clipsDir, from), dest);
        quarantined++;
      } catch (e) {
        if ((e as NodeJS.ErrnoException).code !== "ENOENT")
          console.error(`[removal] quarantine failed for ${from}:`, e);
      }
    }
    for (const rel of plan.deletes) {
      try {
        await fs.unlink(path.join(clipsDir, rel));
        files++;
      } catch {
        // pruned or never written
      }
    }
  }

  return Response.json(
    { species_sci: sci, rows, negatives, quarantined, files },
    { headers: { "cache-control": "no-store" } },
  );
}
