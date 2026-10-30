import type { Metadata } from "next";
import { DatabaseSync } from "node:sqlite";
import { AviaryWeather } from "@/components/Aviary";
import { stationBrandingFromEnv } from "@/lib/aviary";

// The weather tab (issue #652): the Weather Post station view as the fourth
// masthead tab, beside the dashboard, the archive and the mask. Static
// segment beside [species] -- the mask precedent: statics outrank the
// dynamic sibling, and no bird is named "weather". force-dynamic +
// stationBrandingFromEnv: the branding is runtime env on stations (the
// aviary/page.tsx note). The old /weather page is a permanent redirect here.
export const dynamic = "force-dynamic";

export function generateMetadata(): Metadata {
  const { title } = stationBrandingFromEnv();
  return {
    title: `Weather Post — ${title}`,
    description: "The station's weather, writ large.",
  };
}

/** Does this box's field journal carry weather? (#655) The "why weather?"
 * note's journal sentence is true only then: the house writes notes with a
 * weather paragraph, and the stations, with no writer, have none. Read from
 * the store rather than the writer's env, so a box that starts writing notes
 * gains the sentence on its own. The lifeListOf posture (aviary/page.tsx):
 * no env, no file, no table is false, never an error page. */
function hasWeatherJournal(path: string | undefined): boolean {
  if (!path) return false;
  let db: DatabaseSync;
  try {
    db = new DatabaseSync(path, { readOnly: true });
  } catch {
    return false;
  }
  try {
    const row = db
      .prepare(
        "SELECT 1 FROM species_analysis WHERE trim(coalesce(weather_text, '')) != '' LIMIT 1",
      )
      .get();
    return row !== undefined;
  } catch {
    return false;
  } finally {
    db.close();
  }
}

export default function AviaryWeatherPage() {
  const { stationName, title } = stationBrandingFromEnv();
  return (
    <AviaryWeather
      stationName={stationName}
      title={title}
      // Bracket access: a static process.env.FOO is inlined at build time
      // (stationBrandingFromEnv's story); this must be the unit's live env.
      weatherJournal={hasWeatherJournal(process.env["MERLE_EARL_DB"])}
    />
  );
}
