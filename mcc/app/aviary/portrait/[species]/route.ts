// Serves the species portraits (epic #182 Phase 2, issue #184) from the
// species/ shelf under MERLE_EARL_CLIPS -- the files the enrichment pass
// (listener/species_profile.py) downloads, or Todd's own uploads in the
// feeder-cam era. The /frames/[id] shape: not proxied, quiet 404 for
// missing/unsafe, and the client renders the reserved placeholder block,
// never a broken image.
//
// GET /aviary/portrait/<species_sci>
//
// The URL carries the SCIENTIFIC NAME, not a filename: speciesImageName()
// re-derives the pass's scrubbed filename (the two mirror one regex), so
// the client never constructs paths and hostile input scrubs flat before it
// touches the filesystem.
//
// Cache (#432 / #433): portraits are a durable shelf, not clips. Clips stay
// no-store because best-clip rewrites the same path in place; portraits
// change only on rare enrichment refresh. no-store forced every grid revisit
// to re-download every JPEG -- free on the home LAN, painful on a station
// Wi‑Fi link. public + day-long max-age + week-long SWR, with an ETag from
// mtime+size so a replaced file revalidates to 304/200 correctly. Missing
// portraits stay uncached 404 so a pending tile can appear after the pass.

import { promises as fs } from "fs";
import path from "path";
import { speciesImageName } from "@/lib/aviary";

const SPECIES_DIR = "species";

/** Long-lived browser cache; revalidate cheaply via ETag when the file changes. */
const CACHE_CONTROL =
  "public, max-age=86400, stale-while-revalidate=604800";

/** Strong ETag from mtime (ms) + size — changes whenever enrichment rewrites the bytes. */
function portraitEtag(mtimeMs: number, size: number): string {
  return `"${Math.round(mtimeMs)}-${size}"`;
}

export async function GET(
  req: Request,
  ctx: { params: Promise<{ species: string }> },
) {
  const { species } = await ctx.params;
  const dir = process.env.MERLE_EARL_CLIPS;
  const name = speciesImageName(decodeURIComponent(species));
  if (!dir || !name) return new Response(null, { status: 404 });

  const filePath = path.join(dir, SPECIES_DIR, name);
  let st: { mtimeMs: number; size: number };
  try {
    st = await fs.stat(filePath);
  } catch {
    return new Response(null, { status: 404 }); // not enriched yet
  }

  const etag = portraitEtag(st.mtimeMs, st.size);
  if (req.headers.get("if-none-match") === etag) {
    return new Response(null, {
      status: 304,
      headers: {
        etag,
        "cache-control": CACHE_CONTROL,
      },
    });
  }

  let bytes: Buffer;
  try {
    bytes = await fs.readFile(filePath);
  } catch {
    return new Response(null, { status: 404 });
  }
  return new Response(new Uint8Array(bytes), {
    headers: {
      "content-type": "image/jpeg",
      etag,
      "cache-control": CACHE_CONTROL,
    },
  });
}
