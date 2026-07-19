import { describe, expect, it } from "vitest";
import { BirdEvent } from "./bus";
import {
  DETAIL_SPAN_S,
  VISITS_SPAN_S,
  VISIT_GAP_S,
  clampVisitWindow,
  clipRelPath,
  clipUrl,
  enhancedClipUrl,
  enhancedRelPath,
  collapseVisits,
  countVisits,
  cropPosition,
  dayBuckets,
  dayStart,
  detectionFromRow,
  hourBuckets,
  hourStart,
  nearestBar,
  parseLimit,
  parseSince,
  portraitAspect,
  portraitUrl,
  rosterOrder,
  shapeRoster,
  smoothPath,
  smoothSegments,
  speciesImageName,
  tallyVisits,
  todayVisitors,
  visitHourTicks,
  visitTicks,
  visitsCeil,
} from "./aviary";

const bird = (over: Partial<BirdEvent> = {}): BirdEvent => ({
  ts: 1000,
  source: "amcrest",
  kind: "detection",
  species_sci: "Cardinalis cardinalis",
  species_common: "Northern Cardinal",
  confidence: 0.87,
  clip: "amcrest/1000-Northern_Cardinal.wav",
  wind_suspect: false,
  rms: 0.01,
  ...over,
});

describe("countVisits", () => {
  it("collapses pre-#175 per-window rows: one singing cardinal is one visit", () => {
    // Day one's measured shape: ~25 windows a few seconds apart over ~96s.
    const ts = Array.from({ length: 25 }, (_, i) => 1000 + i * 4);
    expect(countVisits(ts)).toBe(1);
  });
  it("opens a new visit strictly past the gap (VisitTracker's > rule)", () => {
    expect(countVisits([1000, 1000 + VISIT_GAP_S])).toBe(1); // exactly at gap: same
    expect(countVisits([1000, 1000 + VISIT_GAP_S + 1])).toBe(2);
  });
  it("measures the gap against the LAST detection, not the opening", () => {
    // Each step is inside the gap, but the chain runs far past 60s total.
    expect(countVisits([0, 50, 100, 150, 200])).toBe(1);
  });
  it("is order-insensitive", () => {
    expect(countVisits([500, 0, 1000])).toBe(3);
  });
  it("counts nothing for no rows", () => {
    expect(countVisits([])).toBe(0);
  });
});

describe("tallyVisits", () => {
  it("tallies species independently, interleaved rows and all", () => {
    const rows = [
      { species_sci: "A", ts: 100 },
      { species_sci: "B", ts: 110 },
      { species_sci: "A", ts: 130 }, // same A visit
      { species_sci: "A", ts: 400 }, // new A visit
    ];
    expect(tallyVisits(rows, null)).toEqual({
      A: { visits: 2, today: 0 },
      B: { visits: 1, today: 0 },
    });
  });
  it("buckets today by the visit's OPENING detection", () => {
    const midnight = 1000;
    const rows = [
      { species_sci: "A", ts: 970 }, // opens before midnight...
      { species_sci: "A", ts: 1020 }, // ...continues past it: yesterday's visit
      { species_sci: "A", ts: 2000 }, // today's visit
    ];
    expect(tallyVisits(rows, midnight)).toEqual({
      A: { visits: 2, today: 1 },
    });
  });
  it("counts no today buckets when todayStart is null", () => {
    expect(tallyVisits([{ species_sci: "A", ts: 5 }], null)).toEqual({
      A: { visits: 1, today: 0 },
    });
  });
});

describe("collapseVisits", () => {
  it("returns one visit per gap group, newest first", () => {
    const visits = collapseVisits([
      bird({ ts: 1000, confidence: 0.7 }),
      bird({ ts: 1030, confidence: 0.9 }),
      bird({ ts: 5000, confidence: 0.8 }),
    ]);
    expect(visits.map((v) => v.ts)).toEqual([5000, 1000]);
    expect(visits[1]).toMatchObject({
      windows: 2,
      last_ts: 1030,
      best: 0.9,
    });
  });
  it("keeps the opening row's clip and source", () => {
    const [v] = collapseVisits([
      bird({ ts: 1000, clip: "amcrest/1000-a.wav", source: "amcrest" }),
      bird({ ts: 1010, clip: "rover/1010-a.wav", source: "rover" }),
    ]);
    expect(v.clip).toBe("amcrest/1000-a.wav");
    expect(v.source).toBe("amcrest");
  });
  it("adopts a later clip only when the opening had none", () => {
    const [v] = collapseVisits([
      bird({ ts: 1000, clip: null }),
      bird({ ts: 1010, clip: "amcrest/1010-a.wav" }),
    ]);
    expect(v.clip).toBe("amcrest/1010-a.wav");
  });
  it("sorts unordered input before grouping", () => {
    const visits = collapseVisits([bird({ ts: 5000 }), bird({ ts: 1000 })]);
    expect(visits.map((v) => v.ts)).toEqual([5000, 1000]);
  });
});

describe("clipRelPath", () => {
  it("accepts Earl's exact layout: <source>/<epoch>-<Common_name>.wav", () => {
    expect(clipRelPath(["amcrest", "1752861234-Northern_Cardinal.wav"])).toBe(
      "amcrest/1752861234-Northern_Cardinal.wav",
    );
  });
  it("rejects traversal in either segment", () => {
    expect(clipRelPath(["..", "x.wav"])).toBeNull();
    expect(clipRelPath(["amcrest", "..%2Fx.wav"])).toBeNull();
    expect(clipRelPath(["amcrest", "..\\x.wav"])).toBeNull();
  });
  it("rejects dots anywhere but the one .wav extension", () => {
    expect(clipRelPath(["amcrest", "a.b.wav"])).toBeNull();
    expect(clipRelPath(["am.crest", "a.wav"])).toBeNull();
  });
  it("rejects non-wav extensions", () => {
    expect(clipRelPath(["amcrest", "a.mp3"])).toBeNull();
    expect(clipRelPath(["amcrest", "a.wav.exe"])).toBeNull();
  });
  it("rejects any depth but exactly two segments", () => {
    expect(clipRelPath(["a.wav"])).toBeNull();
    expect(clipRelPath(["a", "b", "c.wav"])).toBeNull();
    expect(clipRelPath([])).toBeNull();
  });
  it("rejects empty segments", () => {
    expect(clipRelPath(["", "a.wav"])).toBeNull();
    expect(clipRelPath(["amcrest", ".wav"])).toBeNull();
  });
});

describe("clipUrl", () => {
  it("routes a relative clip path under /clips", () => {
    expect(clipUrl("amcrest/1000-Northern_Cardinal.wav")).toBe(
      "/clips/amcrest/1000-Northern_Cardinal.wav",
    );
  });
  it("encodes hostile characters -- transport only; the route's guard is what rejects them", () => {
    expect(clipUrl("a b/c?.wav")).toBe("/clips/a%20b/c%3F.wav");
  });
});

describe("enhancedRelPath / enhancedClipUrl", () => {
  it("names the sibling the pass writes (clip_enhance.enhanced_relpath)", () => {
    expect(enhancedRelPath("amcrest/1000-Blue_Jay.wav")).toBe(
      "amcrest/1000-Blue_Jay-enh.wav",
    );
    expect(enhancedClipUrl("amcrest/1000-Blue_Jay.wav")).toBe(
      "/clips/amcrest/1000-Blue_Jay-enh.wav",
    );
  });
  it("refuses to double-enhance or to touch a non-wav", () => {
    expect(enhancedRelPath("amcrest/1000-Blue_Jay-enh.wav")).toBeNull();
    expect(enhancedClipUrl("species/Cyanocitta_cristata.jpg")).toBeNull();
  });
  it("the sibling passes the EXISTING route guard, unloosened (#190)", () => {
    // The point of the naming choice: '-' was already in the allowlist, so a
    // sibling is an ordinary clip name and the traversal guard needed no
    // exception carved into it. If this ever fails, the guard was changed --
    // and it should not have been.
    expect(clipRelPath(["amcrest", "1000-Blue_Jay-enh.wav"])).toBe(
      "amcrest/1000-Blue_Jay-enh.wav",
    );
    expect(clipRelPath(["..", "x-enh.wav"])).toBeNull();
  });
});

describe("speciesImageName", () => {
  it("mirrors the pass's scrub exactly (species_profile.image_filename)", () => {
    expect(speciesImageName("Cardinalis cardinalis")).toBe(
      "Cardinalis_cardinalis.jpg",
    );
  });
  it("scrubs hostile input flat -- never a path step", () => {
    expect(speciesImageName("../x/../y")).toBe("x_y.jpg");
    expect(speciesImageName("a\\b")).toBe("a_b.jpg");
  });
  it("returns null for a name that scrubs to nothing", () => {
    expect(speciesImageName("   ")).toBeNull();
    expect(speciesImageName("...")).toBeNull();
  });
});

describe("portraitUrl", () => {
  it("routes by encoded scientific name", () => {
    expect(portraitUrl("Cardinalis cardinalis")).toBe(
      "/aviary/portrait/Cardinalis%20cardinalis",
    );
  });
});

describe("parseLimit", () => {
  it("defaults on absence or garbage", () => {
    expect(parseLimit(null)).toBe(50);
    expect(parseLimit("")).toBe(50);
    expect(parseLimit("many")).toBe(50);
  });
  it("clamps both ends", () => {
    expect(parseLimit("0")).toBe(1);
    expect(parseLimit("-5")).toBe(1);
    expect(parseLimit("10000")).toBe(200);
  });
  it("truncates a fractional ask", () => {
    expect(parseLimit("25.9")).toBe(25);
  });
});

describe("parseSince", () => {
  const now = 1_000_000;
  it("passes a plausible local midnight through", () => {
    expect(parseSince(String(now - 30_000), now)).toBe(now - 30_000);
  });
  it("returns null (no today counting) for absence or garbage", () => {
    expect(parseSince(null, now)).toBeNull();
    expect(parseSince("", now)).toBeNull();
    expect(parseSince("midnight", now)).toBeNull();
  });
  it("clamps a typo to within two days -- it can't relabel the archive", () => {
    expect(parseSince("0", now)).toBe(now - 2 * 86400);
    expect(parseSince(String(now + 999), now)).toBe(now);
  });
});

describe("shapeRoster", () => {
  const life = [
    {
      species_sci: "Cardinalis cardinalis",
      species_common: "Northern Cardinal",
      first_ts: 100,
      first_source: "amcrest",
      first_clip: "amcrest/100-Northern_Cardinal.wav",
    },
    {
      species_sci: "Turdus migratorius",
      species_common: "American Robin",
      first_ts: 200,
      first_source: "rover",
      first_clip: null,
    },
  ];
  it("joins tallies onto the life list, common-name ordered", () => {
    const roster = shapeRoster(
      life,
      [
        { species_sci: "Cardinalis cardinalis", ts: 100 },
        { species_sci: "Cardinalis cardinalis", ts: 500 },
        { species_sci: "Turdus migratorius", ts: 600 },
      ],
      550,
    );
    expect(roster.map((r) => r.species_common)).toEqual([
      "American Robin",
      "Northern Cardinal",
    ]);
    expect(roster[1]).toMatchObject({ visits: 2, today: 0 });
    expect(roster[0]).toMatchObject({ visits: 1, today: 1 });
  });
  it("tallies a lifer with no sighting rows honestly at zero", () => {
    const roster = shapeRoster(life.slice(0, 1), [], null);
    expect(roster[0]).toMatchObject({ visits: 0, today: 0 });
  });
  it("passes the enrichment columns (#184) through untouched", () => {
    const enriched = [
      {
        ...life[0],
        description: "A stout red songbird.",
        image_file: "Cardinalis_cardinalis.jpg",
        image_source: "wikipedia",
        image_attribution: "photo: J · CC BY-SA 4.0 · via Wikipedia",
      },
    ];
    const roster = shapeRoster(enriched, [], null);
    expect(roster[0]).toMatchObject({
      description: "A stout red songbird.",
      image_file: "Cardinalis_cardinalis.jpg",
      image_source: "wikipedia",
      image_attribution: "photo: J · CC BY-SA 4.0 · via Wikipedia",
    });
    // A pre-pass row simply lacks the keys -- optional both ways.
    expect(shapeRoster(life.slice(0, 1), [], null)[0].description).toBe(
      undefined,
    );
  });
});

const entry = (
  sci: string,
  common: string,
  visits: number,
  today = 0,
) => ({
  species_sci: sci,
  species_common: common,
  first_ts: 0,
  first_source: "amcrest",
  first_clip: null,
  visits,
  today,
});

describe("rosterOrder", () => {
  const roster = [
    entry("B sci", "Blue Jay", 5),
    entry("A sci", "American Robin", 9),
    entry("C sci", "Carolina Wren", 5),
  ];
  it("sorts by name, both directions", () => {
    expect(rosterOrder(roster, "name", "asc")).toEqual([
      "A sci",
      "B sci",
      "C sci",
    ]);
    expect(rosterOrder(roster, "name", "desc")).toEqual([
      "C sci",
      "B sci",
      "A sci",
    ]);
  });
  it("sorts by visits with name-ascending ties in BOTH directions", () => {
    expect(rosterOrder(roster, "visits", "desc")).toEqual([
      "A sci",
      "B sci",
      "C sci",
    ]);
    expect(rosterOrder(roster, "visits", "asc")).toEqual([
      "B sci",
      "C sci",
      "A sci",
    ]);
  });
});

describe("todayVisitors", () => {
  it("keeps only today's species, most visits first, ties by name", () => {
    const rail = todayVisitors([
      entry("A", "American Robin", 9, 2),
      entry("B", "Blue Jay", 5, 0),
      entry("C", "Carolina Wren", 5, 4),
      entry("D", "Downy Woodpecker", 1, 2),
    ]);
    expect(rail).toEqual([
      { species_sci: "C", species_common: "Carolina Wren", count: 4 },
      { species_sci: "A", species_common: "American Robin", count: 2 },
      { species_sci: "D", species_common: "Downy Woodpecker", count: 2 },
    ]);
  });
});

// --- Portrait framing (#185) -------------------------------------------------

describe("cropPosition", () => {
  it("crops a portrait-orientation photo from the top -- the head stays", () => {
    // The real case that motivated the fix: a 675x900 Blue Jay in a 4:3 box.
    expect(cropPosition(675, 900, 4 / 3)).toBe("top");
    expect(cropPosition(675, 900, 1)).toBe("top"); // and in a square thumb
  });
  it("leaves landscape sources centered -- they crop at the sides", () => {
    expect(cropPosition(900, 600, 4 / 3)).toBe("center");
    expect(cropPosition(1600, 900, 4 / 3)).toBe("center");
  });
  it("top-crops a SQUARE photo in a landscape box (it crops vertically)", () => {
    // object-cover fills the width, so a 1:1 source overflows a 4:3 box top
    // and bottom -- the same head-losing crop, so the same rule applies.
    expect(cropPosition(900, 900, 4 / 3)).toBe("top");
  });
  it("centers exactly at the box ratio (no crop to make)", () => {
    expect(cropPosition(400, 300, 4 / 3)).toBe("center");
  });
  it("treats a square photo in a square box as centered", () => {
    expect(cropPosition(500, 500, 1)).toBe("center");
  });
  it("falls back to centered when dimensions are unknown", () => {
    // A #184-era row awaiting backfill: never guess a shape we don't have.
    expect(cropPosition(null, null, 4 / 3)).toBe("center");
    expect(cropPosition(undefined, undefined, 1)).toBe("center");
    expect(cropPosition(0, 900, 4 / 3)).toBe("center");
  });
});

describe("portraitAspect", () => {
  it("gives the photo its own shape so the profile crops nothing", () => {
    expect(portraitAspect(675, 900)).toBe("675 / 900");
    expect(portraitAspect(900, 600)).toBe("900 / 600");
  });
  it("falls back to the page's original 4:3 when dimensions are unknown", () => {
    expect(portraitAspect(null, null)).toBe("4 / 3");
    expect(portraitAspect(900, 0)).toBe("4 / 3");
  });
});

// --- The visits chart (#185) -------------------------------------------------

// Fixed local noon anchors, so these tests read the same in any timezone the
// suite runs in (CI is UTC, the desk is Eastern).
const noon = (y: number, m: number, d: number) =>
  Math.floor(new Date(y, m, d, 12, 0, 0, 0).getTime() / 1000);

describe("dayStart", () => {
  it("floors to the viewer's local midnight", () => {
    const ts = dayStart(noon(2026, 6, 18));
    const d = new Date(ts * 1000);
    expect([d.getHours(), d.getMinutes(), d.getDate()]).toEqual([0, 0, 18]);
  });
});

describe("dayBuckets", () => {
  const ts0 = dayStart(noon(2026, 6, 15));
  const ts1 = dayStart(noon(2026, 6, 20));

  it("counts visits into the viewer's local days", () => {
    const bars = dayBuckets(
      [noon(2026, 6, 16), noon(2026, 6, 16) + 3600, noon(2026, 6, 18)],
      ts0,
      ts1,
    );
    expect(bars.map((b) => b.count)).toEqual([0, 2, 0, 1, 0]);
  });
  it("draws an empty day as an honest zero, not a gap", () => {
    const bars = dayBuckets([], ts0, ts1);
    expect(bars).toHaveLength(5);
    expect(bars.every((b) => b.count === 0)).toBe(true);
  });
  it("omits days before first-heard -- absence of record, not of bird", () => {
    const bars = dayBuckets([noon(2026, 6, 18)], ts0, ts1, noon(2026, 6, 17));
    // The window opens the 15th, but the record starts the 17th.
    expect(bars).toHaveLength(3);
    expect(bars[0].ts).toBe(dayStart(noon(2026, 6, 17)));
  });
  it("steps by calendar days, so a DST change can't skip or double one", () => {
    // US spring-forward 2026: March 8. The 23-hour day must still be one bar.
    const from = dayStart(noon(2026, 2, 6));
    const to = dayStart(noon(2026, 2, 11));
    const bars = dayBuckets([noon(2026, 2, 8)], from, to);
    expect(bars).toHaveLength(5);
    expect(bars.map((b) => new Date(b.ts * 1000).getDate())).toEqual([
      6, 7, 8, 9, 10,
    ]);
    expect(bars.find((b) => new Date(b.ts * 1000).getDate() === 8)?.count).toBe(
      1,
    );
  });
  it("steps cleanly across a fall-back day too", () => {
    // US fall-back 2026: November 1, a 25-hour day.
    const from = dayStart(noon(2026, 9, 30));
    const to = dayStart(noon(2026, 10, 4));
    const bars = dayBuckets([noon(2026, 10, 1)], from, to);
    expect(bars.map((b) => new Date(b.ts * 1000).getDate())).toEqual([
      30, 31, 1, 2, 3,
    ]);
  });
  it("is empty for a degenerate window", () => {
    expect(dayBuckets([1000], 500, 500)).toEqual([]);
  });
});

describe("clampVisitWindow", () => {
  const span = VISITS_SPAN_S;
  it("preserves the span on every clamp -- position moves, size never", () => {
    const past = clampVisitWindow(0, span, 5000, 1_000_000);
    expect(past.ts1 - past.ts0).toBe(span);
    const future = clampVisitWindow(2_000_000, 2_000_000 + span, 0, 1_000_000);
    expect(future.ts1 - future.ts0).toBe(span);
  });
  it("pins to today when dragged past the right edge", () => {
    const c = clampVisitWindow(9_000_000, 9_000_000 + span, 0, 1_000_000);
    expect(c.ts1).toBe(1_000_000);
  });
  it("pins to the record's start when dragged past the left", () => {
    const c = clampVisitWindow(0, span, 500_000, 9_000_000);
    expect(c.ts0).toBe(500_000);
  });
  it("lets the RIGHT wall win on a young record -- the day-one normal", () => {
    // Record shorter than the span: the default window must stay reachable.
    const c = clampVisitWindow(0, span, 900_000, 1_000_000);
    expect(c.ts1).toBe(1_000_000);
    expect(c.ts1 - c.ts0).toBe(span);
  });
  it("leaves a window already inside the walls alone", () => {
    const c = clampVisitWindow(600_000, 600_000 + span, 0, 9_000_000);
    expect(c).toEqual({ ts0: 600_000, ts1: 600_000 + span });
  });
});

describe("visitTicks", () => {
  it("marks week boundaries strictly inside the window", () => {
    const ts0 = dayStart(noon(2026, 6, 1));
    const ts1 = dayStart(noon(2026, 6, 29));
    const ticks = visitTicks(ts0, ts1);
    // Every tick is a Sunday, inside, with a sane fraction.
    expect(ticks.length).toBeGreaterThan(0);
    for (const t of ticks) {
      expect(new Date(t.ts * 1000).getDay()).toBe(0);
      expect(t.ts).toBeGreaterThan(ts0);
      expect(t.ts).toBeLessThan(ts1);
      expect(t.frac).toBeGreaterThan(0);
      expect(t.frac).toBeLessThan(1);
    }
  });
  it("is empty for a degenerate window", () => {
    expect(visitTicks(500, 500)).toEqual([]);
  });
});

describe("visitsCeil", () => {
  const bars = (...counts: number[]) =>
    counts.map((count, i) => ({ ts: i * 86400, count }));
  it("floors so a quiet species reads quiet", () => {
    expect(visitsCeil(bars(0, 1, 0))).toBe(4);
    expect(visitsCeil(bars())).toBe(4);
  });
  it("rounds up to a clean step above the busiest day", () => {
    expect(visitsCeil(bars(5, 9))).toBe(10);
    expect(visitsCeil(bars(14))).toBe(15);
    expect(visitsCeil(bars(42))).toBe(50);
  });
});

describe("nearestBar", () => {
  const bars = [
    { ts: 0, count: 1 },
    { ts: 86400, count: 5 },
    { ts: 172800, count: 2 },
  ];
  it("snaps to the day containing the pointer, never between days", () => {
    expect(nearestBar(bars, 86400 + 43200)?.ts).toBe(86400);
    expect(nearestBar(bars, 1000)?.ts).toBe(0);
  });
  it("clamps to the ends rather than returning nothing", () => {
    expect(nearestBar(bars, -99999)?.ts).toBe(0);
    expect(nearestBar(bars, 999999)?.ts).toBe(172800);
  });
  it("is null when there are no bars", () => {
    expect(nearestBar([], 0)).toBeNull();
  });
  it("snaps by the bucket's own width, not always a day (#204)", () => {
    // Hour bars: the pointer 40 minutes into the 1:00 hour belongs to 1:00.
    const hours = [
      { ts: 0, count: 1 },
      { ts: 3600, count: 5 },
      { ts: 7200, count: 2 },
    ];
    expect(nearestBar(hours, 3600 + 2400, 1800)?.ts).toBe(3600);
    // The same instant under the day-wide default snaps to the first bar,
    // which is exactly why the width had to become a parameter.
    expect(nearestBar(hours, 3600 + 2400)?.ts).toBe(0);
  });
});

// --- Detail mode (#204) ------------------------------------------------------

describe("hourStart", () => {
  it("floors to the viewer's local top-of-hour", () => {
    const d = new Date(hourStart(noon(2026, 6, 18) + 2000) * 1000);
    expect([d.getMinutes(), d.getSeconds()]).toEqual([0, 0]);
  });
  it("is idempotent", () => {
    const h = hourStart(noon(2026, 6, 18) + 2000);
    expect(hourStart(h)).toBe(h);
  });
});

describe("hourBuckets", () => {
  const ts0 = hourStart(noon(2026, 6, 18));
  const ts1 = ts0 + 6 * 3600;

  it("counts visits into the viewer's local hours", () => {
    const bars = hourBuckets(
      [ts0 + 60, ts0 + 120, ts0 + 2 * 3600 + 30, ts0 + 5 * 3600],
      ts0,
      ts1,
    );
    expect(bars.map((b) => b.count)).toEqual([2, 0, 1, 0, 0, 1]);
  });
  it("draws a quiet hour as an honest zero, not a gap", () => {
    const bars = hourBuckets([], ts0, ts1);
    expect(bars).toHaveLength(6);
    expect(bars.every((b) => b.count === 0)).toBe(true);
  });
  it("buckets a visit by its OPENING, so 6:59 is the six o'clock hour", () => {
    // The rule the day bars already claim, one unit finer: a visit that
    // opened at 6:59 and ran past seven counts once, at six.
    const bars = hourBuckets([ts0 + 3599], ts0, ts1);
    expect(bars[0].count).toBe(1);
    expect(bars[1].count).toBe(0);
  });
  it("omits hours before first-heard -- absence of record, not of bird", () => {
    const bars = hourBuckets([ts0 + 4 * 3600], ts0, ts1, ts0 + 3 * 3600 + 900);
    expect(bars).toHaveLength(3);
    expect(bars[0].ts).toBe(ts0 + 3 * 3600);
  });
  it("is empty for a degenerate window", () => {
    expect(hourBuckets([1000], 500, 500)).toEqual([]);
  });

  // The DST pair. Written to hold in ANY zone the suite runs in (CI is UTC,
  // Todd's machine is not): rather than asserting "23 buckets", they assert
  // that the buckets exactly tile the real seconds between two local
  // midnights and every one lands on a real local hour. In a DST zone that
  // IS 23 or 25; in UTC it's 24; the invariant is the same either way, and
  // a stepper that skipped or doubled an hour fails all three.
  const tiles = (from: number, to: number) => {
    const bars = hourBuckets([], from, to);
    expect(bars).toHaveLength(Math.round((to - from) / 3600));
    for (let i = 1; i < bars.length; i++)
      expect(bars[i].ts).toBeGreaterThan(bars[i - 1].ts);
    for (const b of bars)
      expect(new Date(b.ts * 1000).getMinutes()).toBe(0);
  };
  it("tiles a spring-forward day without skipping an hour", () => {
    // US spring-forward 2026: March 8.
    tiles(dayStart(noon(2026, 2, 8)), dayStart(noon(2026, 2, 9)));
  });
  it("tiles a fall-back day without doubling one", () => {
    // US fall-back 2026: November 1. The repeated 1am hours are distinct
    // absolute hours and must land in distinct buckets.
    tiles(dayStart(noon(2026, 9, 1)), dayStart(noon(2026, 9, 2)));
  });
});

describe("visitHourTicks", () => {
  // Anchored at noon rather than midnight, which is both the realistic live
  // case (the window ends at the hour in progress) and the one that puts two
  // midnights inside it.
  const ts0 = hourStart(noon(2026, 6, 18));
  const ts1 = ts0 + DETAIL_SPAN_S;

  it("marks every sixth local hour across the window", () => {
    const ticks = visitHourTicks(ts0, ts1);
    for (const t of ticks)
      expect(new Date(t.ts * 1000).getHours() % 6).toBe(0);
    // Both edges excluded -- a gridline on the frame is just the frame --
    // so 48 hours from noon gives 18/00/06/12/18/00/06, not nine.
    expect(ticks).toHaveLength(7);
  });
  it("places every tick inside the window", () => {
    for (const t of visitHourTicks(ts0, ts1)) {
      expect(t.frac).toBeGreaterThan(0);
      expect(t.frac).toBeLessThanOrEqual(1);
    }
  });
  it("names midnight by its date so the two days can be told apart", () => {
    const ticks = visitHourTicks(ts0, ts1);
    const midnights = ticks.filter(
      (t) => new Date(t.ts * 1000).getHours() === 0,
    );
    expect(midnights).toHaveLength(2);
    // A date, not a time: digits without an am/pm.
    for (const m of midnights) expect(m.label).not.toMatch(/[ap]m/);
  });
  it("is empty for a degenerate window", () => {
    expect(visitHourTicks(500, 500)).toEqual([]);
  });
});

describe("smoothSegments", () => {
  const pts = (ys: number[]) => ys.map((y, i) => ({ x: i * 10, y }));

  it("needs two points to make a segment", () => {
    expect(smoothSegments([])).toEqual([]);
    expect(smoothSegments([{ x: 0, y: 1 }])).toEqual([]);
    expect(smoothSegments(pts([1, 2]))).toHaveLength(1);
  });
  it("passes exactly through every data point", () => {
    const segs = smoothSegments(pts([0, 9, 2, 7]));
    expect(segs.map((s) => s.p0.y)).toEqual([0, 9, 2]);
    expect(segs[segs.length - 1].p1.y).toBe(7);
  });

  // THE guarantee, and the reason this is Fritsch-Carlson rather than a
  // plain spline: a cubic Bezier never leaves the convex hull of its four
  // control points, so control points penned inside each segment's own
  // y-range prove the drawn curve can't dip below a quiet hour or ring
  // above a busy one. A natural spline through this series fails it.
  const penned = (ys: number[]) => {
    for (const s of smoothSegments(pts(ys))) {
      const lo = Math.min(s.p0.y, s.p1.y);
      const hi = Math.max(s.p0.y, s.p1.y);
      for (const c of [s.c1, s.c2]) {
        expect(c.y).toBeGreaterThanOrEqual(lo - 1e-9);
        expect(c.y).toBeLessThanOrEqual(hi + 1e-9);
      }
    }
  };
  it("never overshoots a spike or undershoots the quiet around it", () => {
    // A dawn/dusk day: the 3am zeros must not be dragged negative by the
    // peaks on either side of them.
    penned([0, 0, 9, 1, 0, 0, 0, 4, 12, 2, 0]);
  });
  it("holds the guarantee for a monotone climb and a lone spike", () => {
    penned([0, 1, 2, 3, 4, 5]);
    penned([0, 0, 0, 20, 0, 0, 0]);
  });
  it("keeps a flat run flat instead of bulging across it", () => {
    for (const s of smoothSegments(pts([3, 3, 3, 3]))) {
      expect(s.c1.y).toBe(3);
      expect(s.c2.y).toBe(3);
    }
  });
  it("survives repeated x without dividing by zero", () => {
    for (const s of smoothSegments([
      { x: 0, y: 1 },
      { x: 0, y: 5 },
      { x: 10, y: 2 },
    ]))
      for (const c of [s.c1, s.c2]) expect(Number.isFinite(c.y)).toBe(true);
  });
});

describe("smoothPath", () => {
  it("is an empty (valid, inkless) path when there's nothing to draw", () => {
    expect(smoothPath([])).toBe("");
    expect(smoothPath([{ x: 0, y: 1 }])).toBe("");
  });
  it("opens with a move and carries one cubic per segment", () => {
    const d = smoothPath([
      { x: 0, y: 4 },
      { x: 10, y: 8 },
      { x: 20, y: 2 },
    ]);
    expect(d.startsWith("M0,4")).toBe(true);
    expect(d.match(/C/g)).toHaveLength(2);
  });
});

describe("detectionFromRow", () => {
  it("shapes a store row like a bus payload", () => {
    expect(
      detectionFromRow({
        ts: 1000,
        source: "amcrest",
        species_sci: "Cardinalis cardinalis",
        species_common: "Northern Cardinal",
        confidence: 0.87,
        clip: "amcrest/1000-Northern_Cardinal.wav",
        wind_suspect: 1,
        rms: 0.013,
      }),
    ).toEqual(
      bird({ wind_suspect: true, rms: 0.013 }),
    );
  });
  it("keeps a pruned-era NULL rms and clip honest", () => {
    const e = detectionFromRow({
      ts: 1000,
      source: "amcrest",
      species_sci: "A",
      species_common: "a",
      confidence: 0.7,
      clip: null,
      wind_suspect: 0,
      rms: null,
    });
    expect(e.rms).toBeNull();
    expect(e.clip).toBeNull();
    expect(e.wind_suspect).toBe(false);
  });
});
