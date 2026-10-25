// The on-demand field-notes refresh (#613): the species page's "write a
// fresh entry" control lands here. It never writes earl.db -- it starts the
// pass's own --refresh CLI as a subprocess (lib/notesRefresh.ts says why
// and how) and reports on it.
//
// POST /aviary/analysis/<species_sci>/refresh
//   202 {started: true}       the pass is running (with the writer the
//                             unit's MERLE_LLM_CHAIN names first, #617)
//   409 {busy: true}          a refresh is already running on this box
//   404                       this box can't refresh (no writer / no venv /
//                             no store), or the bird isn't in the record
// GET  /aviary/analysis/<species_sci>/refresh
//   -> {state: idle|running|done|failed, reason, finished_ts}
//
// The page polls GET after a 202 and refetches the notes on "done". The
// write routes' posture where it applies: an unconfigured box answers 404,
// never a write-mode open that would create an empty store.

import { promises as fs } from "fs";
import {
  jobStatus,
  onLifeList,
  refreshConfig,
  startRefresh,
} from "@/lib/notesRefresh";

export async function POST(
  _req: Request,
  ctx: { params: Promise<{ species: string }> },
) {
  const cfg = refreshConfig();
  if (!cfg) return new Response(null, { status: 404 });
  try {
    await fs.access(cfg.db);
  } catch {
    return new Response(null, { status: 404 });
  }
  const { species } = await ctx.params;
  const sci = decodeURIComponent(species);
  if (!onLifeList(cfg.db, sci)) return new Response(null, { status: 404 });

  if (startRefresh(cfg, sci) === "busy")
    return Response.json({ busy: true }, { status: 409 });
  return Response.json({ started: true }, { status: 202 });
}

export async function GET(
  _req: Request,
  ctx: { params: Promise<{ species: string }> },
) {
  const { species } = await ctx.params;
  return Response.json(jobStatus(decodeURIComponent(species)), {
    headers: { "cache-control": "no-store" },
  });
}
