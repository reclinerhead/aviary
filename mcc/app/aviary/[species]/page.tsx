import type { Metadata } from "next";
import { SpeciesProfile } from "@/components/Aviary";

// Profiles key on the SCIENTIFIC name (the stable identifier BirdNET gives
// us; common names are display). decodeURIComponent both ways is safe: an
// already-decoded param has no % sequences left to touch, and no taxonomy
// spells a species with a literal percent sign.
type Params = { params: Promise<{ species: string }> };

export async function generateMetadata({ params }: Params): Promise<Metadata> {
  const { species } = await params;
  return {
    title: `${decodeURIComponent(species)} — The Aviary`,
  };
}

export default async function SpeciesPage({ params }: Params) {
  const { species } = await params;
  return <SpeciesProfile sci={decodeURIComponent(species)} />;
}
