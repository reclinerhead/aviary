import type { Metadata } from "next";
import WeatherStation from "@/components/WeatherStation";

// A page and an API child share this segment on purpose: /weather renders
// the station view, /weather/history stays the archive route the deep
// charts read. Fine in the App Router; noted so nobody is surprised (#142).
export const metadata: Metadata = {
  title: "Weather Post — Merle Control Center",
  description: "The driveway weather station, writ large.",
};

export default function WeatherPage() {
  return <WeatherStation />;
}
