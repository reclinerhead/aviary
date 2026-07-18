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
// Cache: no-store, the clips route's reasoning one shelf over -- a
// portrait's bytes change on --refresh and will change again when an owner
// photo replaces a fetched one; ~17 LAN-local images per grid view is
// nothing worth lying to the browser over. Revalidation caching is a noted
// follow-up if the life list ever makes this chatty.

import { promises as fs } from "fs";
import path from "path";
import { speciesImageName } from "@/lib/aviary";

const SPECIES_DIR = "species";

export async function GET(
  _req: Request,
  ctx: { params: Promise<{ species: string }> },
) {
  const { species } = await ctx.params;
  const dir = process.env.MERLE_EARL_CLIPS;
  const name = speciesImageName(decodeURIComponent(species));
  if (!dir || !name) return new Response(null, { status: 404 });

  let bytes: Buffer;
  try {
    bytes = await fs.readFile(path.join(dir, SPECIES_DIR, name));
  } catch {
    return new Response(null, { status: 404 }); // not enriched yet
  }
  return new Response(new Uint8Array(bytes), {
    headers: {
      "content-type": "image/jpeg",
      "cache-control": "no-store",
    },
  });
}
