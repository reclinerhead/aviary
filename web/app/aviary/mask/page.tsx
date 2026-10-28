import type { Metadata } from "next";
import { AviaryMask } from "@/components/Aviary";
import { stationBrandingFromEnv } from "@/lib/aviary";

// The species mask (issue #593): every species Earl's geo model allows here
// this week, lit by what the yard has heard. The third masthead tab, a
// page beside the dashboard and the archive. Static segment beside
// [species] -- the events precedent: statics outrank the dynamic sibling,
// and no bird is named "mask". force-dynamic + stationBrandingFromEnv: the
// branding is runtime env on stations (the aviary/page.tsx note).
export const dynamic = "force-dynamic";

export function generateMetadata(): Metadata {
  const { title } = stationBrandingFromEnv();
  return {
    title: `The Species Mask — ${title}`,
    description:
      "Every bird that could be here this week, and which ones the yard has heard.",
  };
}

export default function AviaryMaskPage() {
  const { stationName, title } = stationBrandingFromEnv();
  return <AviaryMask stationName={stationName} title={title} />;
}
