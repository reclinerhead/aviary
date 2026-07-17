"use client";

// The standalone station view (issue #142, epic #110 Phase 2): the same
// WeatherStationView the dashboard opens as an overlay, mounted as a page so
// the launchpad's weather tile has a plain URL to point at. The feed hook
// runs its own bus client, and every weather topic is retained, so a fresh
// tab gets the latest report straight from the broker — no HTTP, no
// spinner-then-jump: the view reserves its footprint with em-dash
// placeholders until the retained messages land (milliseconds later).
import { useRouter } from "next/navigation";
import { WeatherStationView, useWeatherFeed } from "@/components/Dashboard";

export default function WeatherStation() {
  const router = useRouter();
  const { current, history, forecast, report, now, offline, reporting, onAir, baroTrend } =
    useWeatherFeed();

  return (
    <WeatherStationView
      current={current}
      history={history}
      forecast={forecast}
      report={report}
      now={now}
      reporting={reporting}
      offline={offline}
      onAir={onAir}
      baroTrend={baroTrend}
      // Standing alone there is no overlay to dismiss — close (and Escape)
      // detour to the full dashboard instead.
      onClose={() => router.push("/")}
    />
  );
}
