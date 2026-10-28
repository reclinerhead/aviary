import type { Metadata } from "next";
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

export default function AviaryWeatherPage() {
  const { stationName, title } = stationBrandingFromEnv();
  return <AviaryWeather stationName={stationName} title={title} />;
}
