import type { Metadata } from "next";
import { cookies } from "next/headers";
import { DatabaseSync } from "node:sqlite";
import { Aviary } from "@/components/Aviary";
import pool from "@/data/fun-facts.json";
import {
  FACT_DISMISS_COOKIE,
  FACT_EVENT_COOKIE,
  factsResting,
  funFactFor,
  localDayNumber,
  stationBrandingFromEnv,
  type FunFactPool,
} from "@/lib/aviary";

// The /weather page precedent (#142): a plain URL for the launchpad's tile.
// This segment holds a page AND API children (/aviary/roster, /aviary/recent)
// on purpose -- fine in the App Router, and static segments outrank the
// [species] sibling, so no bird can shadow a route (no species is named
// "roster" in any taxonomy BirdNET ships).
//
// force-dynamic + stationBrandingFromEnv: Next inlines process.env.FOO at
// build time; stations set MERLE_STATION_NAME / MERLE_AVIARY_TITLE only in
// station.env at runtime, after the bundle was built.
export const dynamic = "force-dynamic";

export function generateMetadata(): Metadata {
  const { title } = stationBrandingFromEnv();
  return {
    title,
    description:
      "Every bird the yard has announced, and the arrivals as they happen.",
  };
}

/** The box's life list, for the fun fact (#422): only a bird this yard has
 * heard may be today's fact. The roster route's posture -- no env, no file,
 * no table is an empty list, never an error page. */
function lifeListOf(path: string | undefined): Set<string> {
  if (!path) return new Set();
  let db: DatabaseSync;
  try {
    db = new DatabaseSync(path, { readOnly: true });
  } catch {
    return new Set();
  }
  try {
    const rows = db.prepare("SELECT species_sci FROM life_list").all() as {
      species_sci: string;
    }[];
    return new Set(rows.map((r) => r.species_sci));
  } catch {
    return new Set();
  } finally {
    db.close();
  }
}

/** Today's fun fact for this request (#422), or null: none while facts
 * rest after an event, none once today's was dismissed. The clock is read
 * here, outside render. */
function todaysFunFact(
  station: string,
  eventCookie: string | undefined,
  dismissedId: string | undefined,
) {
  const now = new Date();
  if (factsResting(eventCookie, Math.floor(now.getTime() / 1000))) return null;
  return funFactFor({
    pool: pool as FunFactPool,
    lifeList: lifeListOf(process.env.MERLE_EARL_DB),
    station,
    day: localDayNumber(now),
    dismissedId,
  });
}

export default async function AviaryPage() {
  const { stationName, title } = stationBrandingFromEnv();
  // The fun fact is picked HERE, not in the browser (#422): it is in the
  // first paint, so the strip never pops in after hydration, and the pool
  // never ships to the client. Cookies, because the server makes the pick.
  const jar = await cookies();
  const funFact = todaysFunFact(
    stationName ?? "house",
    jar.get(FACT_EVENT_COOKIE)?.value,
    jar.get(FACT_DISMISS_COOKIE)?.value,
  );
  return <Aviary stationName={stationName} title={title} funFact={funFact} />;
}
