import type { Metadata } from "next";
import WeatherStation from "@/components/WeatherStation";
import { stationBrandingFromEnv } from "@/lib/aviary";

// A page and an API child share this segment on purpose: /weather renders
// the station view, /weather/history stays the archive route the deep
// charts read. Fine in the App Router; noted so nobody is surprised (#142).
// The title bills the house the way the aviary pages do (#628 retired the
// control center it used to name): force-dynamic + stationBrandingFromEnv,
// because the branding is runtime env on a station (the aviary/page.tsx note).
export const dynamic = "force-dynamic";

export function generateMetadata(): Metadata {
  const { title } = stationBrandingFromEnv();
  return {
    title: `Weather Post — ${title}`,
    description: "The station's weather, writ large.",
  };
}

export default function WeatherPage() {
  return <WeatherStation />;
}
