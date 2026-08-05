import type { Metadata } from "next";
import { SpeciesProfile } from "@/components/Aviary";
import { stationBrandingFromEnv } from "@/lib/aviary";

// Profiles key on the SCIENTIFIC name (the stable identifier BirdNET gives
// us; common names are display). decodeURIComponent both ways is safe: an
// already-decoded param has no % sequences left to touch, and no taxonomy
// spells a species with a literal percent sign.
type Params = { params: Promise<{ species: string }> };

export const dynamic = "force-dynamic";

export async function generateMetadata({ params }: Params): Promise<Metadata> {
  const { species } = await params;
  const { title } = stationBrandingFromEnv();
  return {
    title: `${decodeURIComponent(species)} — ${title}`,
  };
}

export default async function SpeciesPage({ params }: Params) {
  const { species } = await params;
  const { stationName, title } = stationBrandingFromEnv();
  return (
    <SpeciesProfile
      sci={decodeURIComponent(species)}
      stationName={stationName}
      title={title}
    />
  );
}
