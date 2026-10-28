// Serves the species portraits (epic #182 Phase 2, issue #184) from the
// species/ shelf under MERLE_EARL_CLIPS -- the files the enrichment pass
// (listener/species_profile.py) downloads, or Todd's own uploads in the
// feeder-cam era. The /frames/[id] shape: not proxied, quiet 404 for
// missing/unsafe, and the client renders the reserved placeholder block,
// never a broken image.
//
// GET /aviary/portrait/<species_sci>[?size=thumb][&v=<shelf version>]
//
// The URL carries the SCIENTIFIC NAME, not a filename: speciesImageName()
// re-derives the pass's scrubbed filename (the two mirror one regex), so
// the client never constructs paths and hostile input scrubs flat before it
// touches the filesystem. `size=thumb` asks for the pass's tile-sized
// -thumb sibling (#495, speciesThumbName -- the suffix is applied to the
// already-scrubbed name, so the guard's verdict is unchanged); a missing
// sibling FALLS BACK TO THE FULL FILE here, in the route, rather than via
// a client 404-retry: existence is still the only state consulted (the
// #331 idiom's honesty), but the answer costs one request instead of two
// on every un-backfilled species, and the roster's version token -- which
// covers the thumb's stats too -- is what tells caches the sibling landed.
//
// Cache (#432 / #433; versioned tier #495): portraits are a durable shelf,
// not clips. Version-less URLs keep the #432 policy -- public + day-long
// max-age + week-long SWR + mtime/size ETag -- because the same path can
// be rewritten in place. A `v` from the roster names exact bytes (the
// roster re-derives it from the files every fetch, so replacement mints a
// NEW url), which is precisely the objection #433 recorded against
// `immutable`, answered: versioned answers are immutable for a year.
// Missing portraits stay uncached 404 so a pending tile can appear after
// the pass.

import { promises as fs } from "fs";
import path from "path";
import { speciesImageName, speciesThumbName } from "@/lib/aviary";

const SPECIES_DIR = "species";

/** Long-lived browser cache; revalidate cheaply via ETag when the file changes. */
const CACHE_CONTROL =
  "public, max-age=86400, stale-while-revalidate=604800";

/** The versioned tier (#495): the URL names exact bytes, so nothing to revalidate. */
const CACHE_CONTROL_VERSIONED = "public, max-age=31536000, immutable";

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

  const query = new URL(req.url).searchParams;
  // The thumb ask tries the sibling first, then the full file (the
  // route-side fallback above); a full-size ask never sees the sibling.
  const candidates =
    query.get("size") === "thumb" ? [speciesThumbName(name), name] : [name];
  const cacheControl = query.get("v") ? CACHE_CONTROL_VERSIONED : CACHE_CONTROL;

  let filePath: string | null = null;
  let st: { mtimeMs: number; size: number } | null = null;
  for (const candidate of candidates) {
    const p = path.join(dir, SPECIES_DIR, candidate);
    try {
      st = await fs.stat(p);
      filePath = p;
      break;
    } catch {
      // sibling not written yet -- the full file answers for it
    }
  }
  if (!filePath || !st) {
    return new Response(null, { status: 404 }); // not enriched yet
  }

  const etag = portraitEtag(st.mtimeMs, st.size);
  if (req.headers.get("if-none-match") === etag) {
    return new Response(null, {
      status: 304,
      headers: {
        etag,
        "cache-control": cacheControl,
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
      "cache-control": cacheControl,
    },
  });
}
