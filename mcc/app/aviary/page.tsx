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
  // The station's display name (#304, epic #212), read server-side at
  // request time -- a client component reading runtime env would hydrate
  // to a mismatch. Absent at home; the masthead falls back to its old self.
  return <Aviary stationName={process.env.MERLE_STATION_NAME} />;
}
