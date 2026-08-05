import type { Metadata } from "next";
import { Aviary } from "@/components/Aviary";
import { stationBrandingFromEnv } from "@/lib/aviary";

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
    title: `${title} — Merle Control Center`,
    description:
      "Every bird the yard has announced, and the arrivals as they happen.",
  };
}

export default function AviaryPage() {
  const { stationName, title } = stationBrandingFromEnv();
  return <Aviary stationName={stationName} title={title} />;
}
