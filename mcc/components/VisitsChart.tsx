"use client";

// The visits-over-time chart (epic #182 Phase 3, issue #185): daily visit
// bars for one species, pannable back through the whole record. The station
// chart's INTERACTION IDIOM (#106) deliberately copied and its code
// deliberately not -- pointer events throughout, setPointerCapture so a drag
// leaving the chart doesn't strand, touch-action: pan-y so a horizontal drag
// pans while a vertical one still scrolls the page, axes frozen during the
// gesture and settled once, a left wall discovered rather than announced,
// and a "now" control that is always rendered and merely disabled while live.
//
// What's simpler here than at the weather desk: there is no forecast, so the
// right wall is just today and the seam logic disappears entirely. What's
// different: bars, not a trace -- a visit is a countable event, and 30 daily
// counts read as bars where a line would imply continuity between days that
// the data doesn't have.

import { useEffect, useRef, useState } from "react";
import {
  DayBar,
  TAP_SLOP_PX,
  VISITS_CHUNK_S,
  VISITS_SPAN_S,
  clampVisitWindow,
  dayBuckets,
  dayStart,
  nearestBar,
  visitTicks,
  visitsCeil,
} from "@/lib/aviary";

// The stretched viewBox the station charts use: pointer math runs against
// the container rect and every label is an HTML overlay, never SVG text
// (which would squash under preserveAspectRatio="none").
const VW = 1000;
const VH = 220;

type Fetched = { visits: number[]; first_ts: number | null };

export function VisitsChart({ sci }: { sci: string }) {
  // null means LIVE: the window ends at today and tracks it. Once panned it
  // holds an ABSOLUTE right edge -- a window dragged to last April must stay
  // on last April, not creep as today ticks under it. It doubles as the
  // snap-back control's state: home is null, not a timestamp to recompute.
  const [windowEnd, setWindowEnd] = useState<number | null>(null);
  const [visits, setVisits] = useState<number[]>([]);
  const [firstTs, setFirstTs] = useState<number | null>(null);
  const [today, setToday] = useState<number | null>(null);
  const [loaded, setLoaded] = useState(false);
  // The wall: the record's start is known (first_ts), so unlike the weather
  // archive this one IS announced by the data -- but the FETCHED floor still
  // has to be discovered chunk by chunk, and `exhausted` stops the asking
  // once we've reached first_ts (or the route stopped answering).
  const [exhausted, setExhausted] = useState(false);
  const [frozenCeil, setFrozenCeil] = useState<number | null>(null);
  const [hoverFrac, setHoverFrac] = useState<number | null>(null);

  const drag = useRef<{ x: number; ts1: number; moved: boolean } | null>(null);
  const dragging = useRef(false);
  const inFlight = useRef(0);
  // The floor we last asked below. A ref for the station chart's exact
  // reason: a chunk resolving doesn't re-render synchronously, so a
  // pointermove landing between the fetch settling and React catching up
  // would ask for the same range twice. inFlight can't catch that.
  const askedBelow = useRef<number | null>(null);
  const fetchedFrom = useRef<number | null>(null);

  const load = (from: number, to: number, replace: boolean) => {
    inFlight.current += 1;
    return fetch(
      `/aviary/visits/${encodeURIComponent(sci)}?from=${Math.trunc(from)}&to=${Math.trunc(to)}`,
      { cache: "no-store" },
    )
      .then((r) => (r.ok ? r.json() : { visits: [], first_ts: null }))
      .then((b: Fetched) => {
        const got = Array.isArray(b.visits) ? b.visits : [];
        setFirstTs((prev) => prev ?? b.first_ts);
        // Ranges are fetched once and merged additively -- history is
        // immutable once past. Deduped by ts because adjacent chunks share
        // their boundary second.
        setVisits((prev) =>
          replace ? got : [...new Set([...prev, ...got])].sort((a, b2) => a - b2),
        );
        // Reached the beginning of the record: stop asking. An empty answer
        // means the same thing (nothing older exists) -- both are the wall.
        if (b.first_ts !== null && from <= b.first_ts) setExhausted(true);
        else if (!replace && got.length === 0) setExhausted(true);
      })
      .catch(() => setExhausted(true))
      .finally(() => {
        inFlight.current -= 1;
        if (inFlight.current === 0 && !dragging.current) setFrozenCeil(null);
      });
  };

  useEffect(() => {
    // Today's local midnight, computed client-side (never Date.now() in
    // render -- the SSR/hydration rule) and the right wall of the chart.
    const t = dayStart(Math.floor(Date.now() / 1000));
    setToday(t);
    const to = t + 86400; // through the end of today
    const from = to - VISITS_SPAN_S - VISITS_CHUNK_S;
    fetchedFrom.current = from;
    load(from, to, true).finally(() => setLoaded(true));
    // Species changes mean a different bird entirely: reset, don't merge.
    return () => {
      setVisits([]);
      setFirstTs(null);
      setExhausted(false);
      setWindowEnd(null);
      askedBelow.current = null;
      fetchedFrom.current = null;
    };
  }, [sci]);

  if (today === null) {
    // Reserve the footprint before the clock is known (house rule #1).
    return <ChartFrame sci={sci} />;
  }

  const newest = today + 86400;
  const ts1 = windowEnd ?? newest;
  const ts0 = ts1 - VISITS_SPAN_S;
  const live = windowEnd === null;
  // The left wall: where the record begins, or -- until first_ts is known --
  // as far back as we've fetched.
  const oldest = firstTs ?? fetchedFrom.current ?? ts0;

  const bars = dayBuckets(visits, ts0, ts1, firstTs);
  const liveCeil = visitsCeil(bars);
  // Frozen during a gesture so the axis can't rescale every frame (the
  // no-layout-shift rule, applied inside the chart's own frame).
  const ceil = frozenCeil ?? liveCeil;
  const ticks = visitTicks(ts0, ts1);
  const hovered =
    hoverFrac !== null ? nearestBar(bars, ts0 + hoverFrac * (ts1 - ts0)) : null;

  /** Ask for the chunk older than everything we hold. Keyed off what the
   * viewer ASKED for, never the clamped result: the clamp pins ts0 at the
   * wall, so waiting for the clamped window to cross it would mean the
   * request never fires at all (#106's hard-won detail). */
  const askOlder = (wantTs0: number) => {
    if (exhausted || inFlight.current > 0) return;
    const floor = fetchedFrom.current;
    if (floor === null || wantTs0 >= floor) return;
    if (askedBelow.current !== null && floor >= askedBelow.current) return;
    askedBelow.current = floor;
    const from = floor - VISITS_CHUNK_S;
    fetchedFrom.current = from;
    load(from, floor - 1, false);
  };

  const panTo = (wantTs1: number) => {
    const c = clampVisitWindow(
      wantTs1 - VISITS_SPAN_S,
      wantTs1,
      oldest,
      newest,
    );
    // Dragged back to the right edge: return to LIVE rather than freezing an
    // absolute end that today would immediately outrun.
    setWindowEnd(c.ts1 >= newest ? null : c.ts1);
    askOlder(wantTs1 - VISITS_SPAN_S);
  };

  const fracAt = (e: React.PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return r.width > 0
      ? Math.min(1, Math.max(0, (e.clientX - r.left) / r.width))
      : null;
  };

  const dayW = (VW / (ts1 - ts0)) * 86400;
  const empty = loaded && bars.every((b) => b.count === 0);

  return (
    <ChartFrame
      sci={sci}
      right={
        <div className="flex items-center gap-2">
          <span className="stamp text-[10px] text-inkfaint">
            {rangeLabel(ts0, ts1)}
          </span>
          {/* Always rendered, merely disabled while live -- a control that
              appeared on pan would shove this line sideways (house rule #1).
              The station chart's vocabulary, not a new one. */}
          <button
            type="button"
            onClick={() => setWindowEnd(null)}
            disabled={live}
            aria-label="Return the chart to today"
            className="stamp shrink-0 rounded-sm border border-line px-2 py-0.5 text-[10px] text-inkdim transition-colors hover:border-linebright hover:text-wing disabled:pointer-events-none disabled:opacity-40"
          >
            now
          </button>
        </div>
      }
    >
      <div
        className="relative cursor-grab active:cursor-grabbing"
        // pan-y: a horizontal drag is ours, a vertical one still scrolls the
        // page (the browser fires pointercancel when it claims the gesture).
        style={{ touchAction: "pan-y" }}
        onPointerDown={(e) => {
          e.currentTarget.setPointerCapture(e.pointerId);
          drag.current = { x: e.clientX, ts1, moved: false };
          dragging.current = true;
          setFrozenCeil(frozenCeil ?? liveCeil);
          // A finger on glass isn't hovering; wait to see tap or drag.
          if (e.pointerType !== "mouse") setHoverFrac(null);
        }}
        onPointerMove={(e) => {
          const d = drag.current;
          if (d) {
            const r = e.currentTarget.getBoundingClientRect();
            if (r.width <= 0) return;
            const dx = e.clientX - d.x;
            if (!d.moved && Math.abs(dx) > TAP_SLOP_PX) d.moved = true;
            if (d.moved) {
              setHoverFrac(null);
              // Drag right and the chart follows your hand, which means
              // walking backwards in time.
              panTo(d.ts1 - (dx / r.width) * VISITS_SPAN_S);
            }
            return;
          }
          if (e.pointerType === "mouse") setHoverFrac(fracAt(e));
        }}
        onPointerUp={(e) => {
          const d = drag.current;
          drag.current = null;
          dragging.current = false;
          if (inFlight.current === 0) setFrozenCeil(null);
          if (!d) return;
          // A press that never travelled is a tap: place the crosshair.
          if (!d.moved || e.pointerType === "mouse") setHoverFrac(fracAt(e));
        }}
        onPointerLeave={() => {
          if (!drag.current) setHoverFrac(null);
        }}
        onPointerCancel={() => {
          drag.current = null;
          dragging.current = false;
          if (inFlight.current === 0) setFrozenCeil(null);
          setHoverFrac(null);
        }}
      >
        <div className="relative h-56 w-full">
          <svg
            viewBox={`0 0 ${VW} ${VH}`}
            preserveAspectRatio="none"
            className="h-full w-full"
            role="img"
            aria-label={`Daily visits over ${rangeLabel(ts0, ts1)}`}
          >
            {/* Week gridlines, recessive behind the data. */}
            {ticks.map((t) => (
              <line
                key={t.ts}
                x1={t.frac * VW}
                y1={0}
                x2={t.frac * VW}
                y2={VH}
                stroke="var(--line)"
                vectorEffect="non-scaling-stroke"
              />
            ))}
            {/* Baseline: where a zero-visit day honestly sits. */}
            <line
              x1={0}
              y1={VH}
              x2={VW}
              y2={VH}
              stroke="var(--line-bright)"
              vectorEffect="non-scaling-stroke"
            />
            {bars.map((b) => {
              const x = ((b.ts - ts0) / (ts1 - ts0)) * VW;
              // 2px surface gap between adjacent bars (the mark spec), taken
              // in viewBox units off a day's width.
              const w = Math.max(1, dayW * 0.72);
              const h = b.count > 0 ? (b.count / ceil) * (VH - 4) : 0;
              if (b.count === 0) return null;
              return (
                <rect
                  key={b.ts}
                  x={x + (dayW - w) / 2}
                  y={VH - h}
                  width={w}
                  height={h}
                  fill="var(--wing)"
                  opacity={hovered && hovered.ts === b.ts ? 1 : 0.85}
                />
              );
            })}
          </svg>

          {/* Y ceiling whisper, absolute so it can never shift the chart. */}
          <span className="pointer-events-none absolute left-1 top-0.5 text-[9px] tabular-nums text-inkfaint">
            {ceil}
          </span>
          {empty && (
            <span className="stamp pointer-events-none absolute inset-0 flex items-center justify-center text-[10px] text-inkfaint">
              no visits in this stretch
            </span>
          )}

          {/* Crosshair + readout: snapped to a real day, HTML overlay (the
              viewBox is stretched, so SVG text would squash), riding the
              roomier side so it can't run off the edge. */}
          {hovered && (
            <>
              <span
                className="pointer-events-none absolute top-0 h-full w-px bg-linebright"
                style={{
                  left: `${((hovered.ts + 43200 - ts0) / (ts1 - ts0)) * 100}%`,
                }}
              />
              <span
                className={`pointer-events-none absolute top-1 rounded-sm border border-line bg-panel2 px-1.5 py-1 text-[10px] leading-tight text-inkdim ${
                  (hovered.ts - ts0) / (ts1 - ts0) > 0.5
                    ? "-translate-x-full"
                    : ""
                }`}
                style={{
                  left: `${((hovered.ts + 43200 - ts0) / (ts1 - ts0)) * 100}%`,
                }}
              >
                <span className="block text-ink">
                  {new Date(hovered.ts * 1000).toLocaleDateString(undefined, {
                    weekday: "short",
                    month: "short",
                    day: "numeric",
                  })}
                </span>
                <span className="tabular-nums">
                  {hovered.count === 1 ? "1 visit" : `${hovered.count} visits`}
                </span>
              </span>
            </>
          )}
        </div>

        {/* Week labels under the axis. */}
        <div className="relative mt-1 h-3">
          {ticks.map((t) => (
            <span
              key={t.ts}
              className="stamp pointer-events-none absolute -translate-x-1/2 text-[9px] text-inkfaint"
              style={{ left: `${t.frac * 100}%` }}
            >
              {t.label}
            </span>
          ))}
        </div>
      </div>
    </ChartFrame>
  );
}

/** The panel shell, rendered identically before and after data lands so the
 * chart's footprint is reserved from first paint (house rule #1). */
function ChartFrame({
  right,
  children,
}: {
  sci: string;
  right?: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <section className="panel mt-4 rounded-sm border border-line bg-panel">
      <div className="flex items-baseline justify-between gap-3 px-4 pb-2 pt-3">
        <h2
          className="text-lg text-ink"
          style={{ fontFamily: "var(--font-display)" }}
        >
          Visits over time
        </h2>
        {right}
      </div>
      <div className="px-4 pb-4">
        {children ?? <div className="h-56 w-full" />}
        <p className="stamp mt-1 text-[9px] text-inkfaint">
          one bar per day · drag to travel back through the record
        </p>
      </div>
    </section>
  );
}

/** "jun 18 — jul 18", the window's own honest label. */
function rangeLabel(ts0: number, ts1: number): string {
  const fmt = (ts: number) =>
    new Date(ts * 1000)
      .toLocaleDateString(undefined, { month: "short", day: "numeric" })
      .toLowerCase();
  return `${fmt(ts0)} — ${fmt(ts1 - 86400)}`;
}
