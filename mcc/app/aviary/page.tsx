import type { Metadata } from "next";
import { Aviary } from "@/components/Aviary";

// The /weather page precedent (#142): a plain URL for the launchpad's tile.
// This segment holds a page AND API children (/aviary/roster, /aviary/recent)
// on purpose -- fine in the App Router, and static segments outrank the
// [species] sibling, so no bird can shadow a route (no species is named
// "roster" in any taxonomy BirdNET ships).
export const metadata: Metadata = {
  title: "The Aviary — Merle Control Center",
  description: "Every bird the yard has announced, and the arrivals as they happen.",
};

export default function AviaryPage() {
  return <Aviary />;
}
