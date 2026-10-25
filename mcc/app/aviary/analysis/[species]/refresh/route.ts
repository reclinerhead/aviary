// The on-demand field-notes refresh (#613): the species page's "write a
// fresh entry" control lands here. It never writes earl.db -- it starts the
// pass's own --refresh CLI as a subprocess (lib/notesRefresh.ts says why
// and how) and reports on it.
//
// POST /aviary/analysis/<species_sci>/refresh[?writer=gateway]
//   202 {started: true}       the pass is running
//   409 {busy: true}          a refresh is already running on this box
//   404                       this box can't refresh (no model / no venv /
//                             no store), the bird isn't in the record, or
//                             the cloud writer was asked for on a box whose
//                             unit carries no AI_GATEWAY_API_KEY (#632)
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
  req: Request,
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
  // The cloud writer (#632) only where this unit's env says it may be
  // offered; anything else on the param is the house writer.
  const writer =
    new URL(req.url).searchParams.get("writer") === "gateway"
      ? "gateway"
      : "ollama";
  if (writer === "gateway" && !cfg.cloud)
    return new Response(null, { status: 404 });

  if (startRefresh(cfg, sci, writer) === "busy")
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
