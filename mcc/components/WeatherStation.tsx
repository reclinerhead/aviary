"use client";

// The standalone station view (issue #142, epic #110 Phase 2):
// WeatherStationView mounted as a page so the launchpad's weather tile has a
// plain URL to point at. Until #628 the MCC dashboard opened the same
// component as an overlay; this page is its only caller now. The feed hook
// runs its own bus client, and every weather topic is retained, so a fresh
// tab gets the latest report straight from the broker — no HTTP, no
// spinner-then-jump: the view reserves its footprint with em-dash
// placeholders until the retained messages land (milliseconds later).
import { useRouter } from "next/navigation";
import { WeatherStationView, useWeatherFeed } from "@/components/WeatherStationView";

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
      // detour to `/`, which is the Aviary since #628. The weather tab
      // (#652) retires the control along with the overlay contract.
      onClose={() => router.push("/")}
    />
  );
}
