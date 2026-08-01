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
// RANGE REQUESTS ARE LOAD-BEARING (#377): WebKit probes <audio> sources with
// `Range: bytes=0-1` and refuses to play anything that answers 200-with-full-
// body -- which made every clip on an iPhone/iPad "faded" while desktop
// engines shrugged. So this route speaks RFC 7233: single byte ranges get a
// 206 + Content-Range, unsatisfiable ones a 416, everything else the full
// 200 -- now with Content-Length and Accept-Ranges, so the engine can learn
// the media's size instead of a chunked stream of unknown length. The
// parsing lives in lib/aviary.ts (parseRange) with the tests; this stays a
// thin file-server over it.
//
// Known race, accepted: a visit-best rewrite (#175) can replace the file
// between two range requests of one playback and mix takes. The old chunked
// streaming had the same window mid-stream; clips are ~9 s and rewrites are
// rare. If-Range/ETag is the escalation if it ever audibly bites.
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
import { clipRelPath, parseRange } from "@/lib/aviary";

const BASE_HEADERS = {
  "content-type": "audio/wav",
  "cache-control": "no-store",
  "accept-ranges": "bytes",
};

export async function GET(
  req: Request,
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

  const total = bytes.byteLength;
  const range = parseRange(req.headers.get("range"), total);

  if (range === "unsatisfiable")
    return new Response(null, {
      status: 416,
      headers: { ...BASE_HEADERS, "content-range": `bytes */${total}` },
    });

  if (range === null)
    return new Response(new Uint8Array(bytes), {
      headers: { ...BASE_HEADERS, "content-length": String(total) },
    });

  // A view, not a copy -- byteOffset because fs may hand back a Buffer into
  // a shared pool whose view doesn't start at 0.
  const length = range.end - range.start + 1;
  const body = new Uint8Array(
    bytes.buffer,
    bytes.byteOffset + range.start,
    length,
  );
  return new Response(body, {
    status: 206,
    headers: {
      ...BASE_HEADERS,
      "content-length": String(length),
      "content-range": `bytes ${range.start}-${range.end}/${total}`,
    },
  });
}
