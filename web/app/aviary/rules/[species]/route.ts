// Per-species detection rules (issue #506): the operator's standing answers
// to "how should Earl hear this bird" -- an extra confidence floor, the
// ignore-from-now-on veto, or a one-species hole in the region mask. Rows
// live in species_rules in THIS box's earl.db (rules never sync across
// stations); earl-listener re-reads the table every ~60 s and hands
// gate.decide() the map, so a rule saved here is live within a minute with
// no restart. Keyed by SCIENTIFIC name, decodeURIComponent'd, exactly like
// the profile page and the removal route.
//
// PUT /aviary/rules/<species_sci>   body {exclude, threshold, always_include}
//   -> { species_sci, rule }        rule null when the save cleared the row
//
// An all-defaults body DELETES the row rather than storing a no-op --
// "cleared" and "never ruled" are deliberately the same state, so the
// listener's map stays exactly as big as the operator's opinions and the
// profile stamp reads "default" either way. The DDL rides the write in the
// same transaction (the negatives precedent: a pre-#506 store upgrades on
// its first rule; sightings' connect() carries the same shape for the
// stations' deploy-order case).
//
// This is the MCC's second write to earl.db (after #216's removal), and it
// wears the same posture: unset env or a store that doesn't exist yet
// answers 404 (a write-mode open would CREATE an empty earl.db, which a
// settings save must never do), an unopenable or unwritable store answers
// 500 loudly, and a malformed body 400 -- a rule that silently didn't land
// while the overlay reported success would suppress birds nobody chose to
// suppress, or keep publishing ones somebody vetoed.

import { promises as fs } from "fs";
import { DatabaseSync } from "node:sqlite";
import {
  parseRulePayload,
  ruleIsEmpty,
  SPECIES_RULES_DDL,
} from "@/lib/aviary";

export async function PUT(
  req: Request,
  ctx: { params: Promise<{ species: string }> },
) {
  const dbPath = process.env.MERLE_EARL_DB;
  if (!dbPath) return new Response(null, { status: 404 });
  try {
    await fs.access(dbPath);
  } catch {
    return new Response(null, { status: 404 }); // no store, nothing to rule
  }

  const { species } = await ctx.params;
  const sci = decodeURIComponent(species);

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return new Response(null, { status: 400 });
  }
  const rule = parseRulePayload(body);
  if (rule === null) return new Response(null, { status: 400 });

  let db: DatabaseSync;
  try {
    db = new DatabaseSync(dbPath); // read-write: the whole point
  } catch {
    return new Response(null, { status: 500 });
  }
  try {
    db.exec("BEGIN IMMEDIATE");
    try {
      db.exec(SPECIES_RULES_DDL);
      if (ruleIsEmpty(rule)) {
        db.prepare("DELETE FROM species_rules WHERE species_sci = ?").run(sci);
      } else {
        db.prepare(
          "INSERT INTO species_rules" +
            " (species_sci, exclude, threshold, always_include, updated_ts)" +
            " VALUES (?,?,?,?,?)" +
            " ON CONFLICT(species_sci) DO UPDATE SET" +
            " exclude=excluded.exclude, threshold=excluded.threshold," +
            " always_include=excluded.always_include," +
            " updated_ts=excluded.updated_ts",
        ).run(
          sci,
          rule.exclude ? 1 : 0,
          rule.threshold,
          rule.always_include ? 1 : 0,
          Math.floor(Date.now() / 1000),
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

  return Response.json(
    { species_sci: sci, rule: ruleIsEmpty(rule) ? null : rule },
    { headers: { "cache-control": "no-store" } },
  );
}
