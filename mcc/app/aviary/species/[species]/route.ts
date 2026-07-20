// Species removal (issue #216): strike a misidentified species from the
// record entirely -- rows and files both -- so a rover motor logged as a
// Wood Duck doesn't skew the tallies forever. Keyed by SCIENTIFIC name,
// decodeURIComponent'd, exactly like the profile page it serves.
//
// DELETE /aviary/species/<species_sci>
//   -> { species_sci, rows: {sightings, life_list, species_profile,
//        species_analysis}, files }
//
// Rows first, then files: every row for the species from the four tables
// (one transaction), then the doomed file set computed BEFORE the rows
// vanished -- each sighting clip, its -enh sibling, the lifer's first_clip,
// and the portrait. Missing files are not errors (the retention pass may
// have taken them first); missing TABLES are not errors either (a pre-
// enrichment store simply has nothing there to delete). The species heard
// again later is a brand-new lifer -- intended, the listener is untouched.
//
// This is the MCC's FIRST write to earl.db, and it deliberately does NOT
// wear the readers' quiet-empty semantics: a DELETE that silently removed
// nothing while the UI reported success would be worse than an error, so an
// unopenable or unwritable store answers 500, loudly. Unset env or a store
// that doesn't exist yet answers 404 -- there is no record to remove, and
// a write-mode open would CREATE an empty earl.db as a side effect, which
// a delete must never do.

import { promises as fs } from "fs";
import path from "path";
import { DatabaseSync } from "node:sqlite";
import { doomedFiles } from "@/lib/aviary";

const TABLES = [
  "sightings",
  "life_list",
  "species_profile",
  "species_analysis",
] as const;

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

  let doomed: string[] = [];
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

    // The doomed files, gathered while the rows still exist to name them.
    const clips: (string | null)[] = present.has("sightings")
      ? (
          db
            .prepare("SELECT clip FROM sightings WHERE species_sci = ?")
            .all(sci) as { clip: string | null }[]
        ).map((r) => r.clip)
      : [];
    if (present.has("life_list")) {
      const first = db
        .prepare("SELECT first_clip FROM life_list WHERE species_sci = ?")
        .get(sci) as { first_clip: string | null } | undefined;
      if (first) clips.push(first.first_clip);
    }
    let imageFile: string | null = null;
    if (present.has("species_profile")) {
      const prof = db
        .prepare("SELECT image_file FROM species_profile WHERE species_sci = ?")
        .get(sci) as { image_file: string | null } | undefined;
      imageFile = prof?.image_file ?? null;
    }
    doomed = doomedFiles(clips, imageFile);

    db.exec("BEGIN IMMEDIATE");
    try {
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

  // Files second: if this crashes mid-walk the rows are already gone, the
  // roster is already honest, and any orphaned clip ages into the retention
  // pass's ordinary horizon. Every failure here is "already missing".
  let files = 0;
  const clipsDir = process.env.MERLE_EARL_CLIPS;
  if (clipsDir) {
    for (const rel of doomed) {
      try {
        await fs.unlink(path.join(clipsDir, rel));
        files++;
      } catch {
        // pruned, never written, or a not-yet-enhanced sibling
      }
    }
  }

  return Response.json(
    { species_sci: sci, rows, files },
    { headers: { "cache-control": "no-store" } },
  );
}
