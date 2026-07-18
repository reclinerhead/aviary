// Serves Earl's visit clips (epic #182 Phase 1, issue #183) from
// MERLE_EARL_CLIPS -- the dir listener/earl.py fills on pearl
// (/srv/media-cache/earl in production). The /frames/[id] shape, one sense
// over: does NOT ride the /daemon proxy (both ends of this live on pearl),
// quiet 404 for anything missing or unsafe, and the client renders a pruned
// clip as a "faded" stamp in the reserved slot, never a broken player.
//
// GET /clips/<source>/<epoch>-<Common_name>.wav
//
// The path guard (clipRelPath in lib/aviary.ts) mirrors gate.clip_relpath's
// allowlist: Earl scrubs both derived parts to [A-Za-z0-9_-] before writing,
// so exactly two segments, no dots outside the one .wav extension, nothing a
// filesystem could interpret. The guard exists because the route's path
// arrives from a URL, not from Earl.
//
// Cache: NO-STORE, decided here (#183 left it open) -- a clip path's bytes
// DO change: a window beating the visit's best confidence rewrites the
// visit's clip in place (#175), so a play during an open visit followed by
// an immutable-cached replay would pin the opening take and silently hide
// the bird's best moment -- the one thing best-clip exists to keep. Clips
// are ~9s WAVs fetched by hand on a LAN; re-reading them costs nothing worth
// lying to the browser over.

import { promises as fs } from "fs";
import path from "path";
import { clipRelPath } from "@/lib/aviary";

export async function GET(
  _req: Request,
  ctx: { params: Promise<{ path: string[] }> },
) {
  const { path: parts } = await ctx.params;
  const dir = process.env.MERLE_EARL_CLIPS;
  const rel = clipRelPath(parts ?? []);
  if (!dir || !rel) return new Response(null, { status: 404 });

  let bytes: Buffer;
  try {
    bytes = await fs.readFile(path.join(dir, rel));
  } catch {
    return new Response(null, { status: 404 }); // pruned, or never written
  }
  return new Response(new Uint8Array(bytes), {
    headers: {
      "content-type": "audio/wav",
      "cache-control": "no-store",
    },
  });
}
