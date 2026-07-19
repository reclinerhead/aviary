import type { Metadata } from "next";
import { AviaryEvents } from "@/components/Aviary";

// The event archive (issue #211): the browsable, filterable record the
// Latest Events ticker links to. A page rather than an overlay -- the
// /weather reasoning (#142): filters and the jumped-to day live in the
// query string, so a view is a shareable URL and the back button works.
// Static segment beside [species] -- the roster/recent precedent: statics
// outrank the dynamic sibling, and no bird is named "events".
export const metadata: Metadata = {
  title: "The Full Record — The Aviary — Merle Control Center",
  description:
    "Every arrival the yard has announced, browsable by day and by bird.",
};

export default function AviaryEventsPage() {
  return <AviaryEvents />;
}
