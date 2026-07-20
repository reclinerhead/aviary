import { describe, expect, it } from "vitest";
import { BirdEvent } from "./bus";
import {
  ARRIVALS_24H_S,
  ARRIVALS_WEEK_S,
  AnalysisStats,
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
  dayAnchor,
  dayBuckets,
  dayGroups,
  dayStart,
  detectionFromRow,
  doomedFiles,
  hourBuckets,
  hourStart,
  liferNumber,
  newArrivals,
  nearestBar,
  nextBefore,
  parseBefore,
  parseLimit,
  parseSince,
  parseSpeciesFilter,
  portraitAspect,
  portraitUrl,
  rhythmStrip,
  rivalLine,
  rosterOrder,
  secondsIntoDay,
  shapeRoster,
  shareOfYard,
  smoothPath,
  smoothSegments,
  speciesImageName,
  standingFor,
  tallyVisits,
  todayVisitors,
  visitHourTicks,
  visitTicks,
  visitsCeil,
  weatherChips,
  weekWindowStart,
  yardRecords,
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
      A: { visits: 2, today: 0, week: 0 },
      B: { visits: 1, today: 0, week: 0 },
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
      A: { visits: 2, today: 1, week: 2 },
    });
  });
  it("counts no today buckets when todayStart is null", () => {
    expect(tallyVisits([{ species_sci: "A", ts: 5 }], null)).toEqual({
      A: { visits: 1, today: 0, week: 0 },
    });
  });
  it("windows the week tally to 7 local days back from today (#220)", () => {
    const midnight = 100 * 86400;
    const weekStart = midnight - 6 * 86400;
    const rows = [
      { species_sci: "A", ts: weekStart }, // the boundary itself: inclusive
      { species_sci: "A", ts: weekStart - 100 }, // its own visit, outside
      { species_sci: "A", ts: midnight + 100 }, // today counts twice over
    ];
    expect(tallyVisits(rows, midnight)).toEqual({
      A: { visits: 3, today: 1, week: 2 },
    });
  });
  it("attributes a boundary-straddling visit by its OPENING (#220)", () => {
    // Opens one second before the window, runs into it: last week's visit,
    // the same opening-attribution rule `today` has always used.
    const midnight = 100 * 86400;
    const weekStart = midnight - 6 * 86400;
    const rows = [
      { species_sci: "A", ts: weekStart - 1 },
      { species_sci: "A", ts: weekStart + 30 },
    ];
    expect(tallyVisits(rows, midnight)).toEqual({
      A: { visits: 1, today: 0, week: 0 },
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

describe("doomedFiles", () => {
  it("collects sighting clips with their -enh siblings", () => {
    expect(
      doomedFiles(["amcrest/1000-Wood_Duck.wav", "rover/2000-Wood_Duck.wav"], null),
    ).toEqual([
      "amcrest/1000-Wood_Duck.wav",
      "amcrest/1000-Wood_Duck-enh.wav",
      "rover/2000-Wood_Duck.wav",
      "rover/2000-Wood_Duck-enh.wav",
    ]);
  });
  it("dedupes first_clip against the sighting row that made it", () => {
    // The lifer's opening sighting and life_list.first_clip name the same
    // file -- the caller appends first_clip to the row clips, and the set
    // must not doom it twice.
    expect(
      doomedFiles(
        ["amcrest/1000-Wood_Duck.wav", "amcrest/1000-Wood_Duck.wav"],
        null,
      ),
    ).toEqual(["amcrest/1000-Wood_Duck.wav", "amcrest/1000-Wood_Duck-enh.wav"]);
  });
  it("skips null clips (pre-clip-era rows) quietly", () => {
    expect(doomedFiles([null, undefined, "amcrest/1000-Blue_Jay.wav"], null))
      .toEqual(["amcrest/1000-Blue_Jay.wav", "amcrest/1000-Blue_Jay-enh.wav"]);
    expect(doomedFiles([], null)).toEqual([]);
  });
  it("re-runs the clips route's guard: a hostile row skips, never escapes", () => {
    expect(
      doomedFiles(
        ["../../../etc/passwd", "a/b/c.wav", "amcrest/a.b.wav", "amcrest/x.mp3"],
        null,
      ),
    ).toEqual([]);
  });
  it("does not name an -enh-enh for a row already holding a sibling", () => {
    // Sightings never store the sibling, but a hand-edited row is a row:
    // enhancedRelPath's no-double-enhance rule carries over unchanged.
    expect(doomedFiles(["amcrest/1000-Blue_Jay-enh.wav"], null)).toEqual([
      "amcrest/1000-Blue_Jay-enh.wav",
    ]);
  });
  it("adds the portrait on the species/ shelf", () => {
    expect(doomedFiles([], "Aix_sparsa.jpg")).toEqual(["species/Aix_sparsa.jpg"]);
  });
  it("refuses a portrait filename that could step off the shelf", () => {
    expect(doomedFiles([], "../evil.jpg")).toEqual([]);
    expect(doomedFiles([], "x/y.jpg")).toEqual([]);
    expect(doomedFiles([], "a.b.jpg")).toEqual([]);
    expect(doomedFiles([], "portrait.png")).toEqual([]);
    expect(doomedFiles([], null)).toEqual([]);
    expect(doomedFiles([], "")).toEqual([]);
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
  week = 0,
  first_ts = 0,
) => ({
  species_sci: sci,
  species_common: common,
  first_ts,
  first_source: "amcrest",
  first_clip: null,
  visits,
  today,
  week,
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

// --- The event archive (#211) ------------------------------------------------

describe("parseSpeciesFilter", () => {
  it("splits a comma list into exact names", () => {
    expect(
      parseSpeciesFilter("Cardinalis cardinalis,Cyanocitta cristata"),
    ).toEqual(["Cardinalis cardinalis", "Cyanocitta cristata"]);
  });
  it("trims, drops empties, and de-duplicates", () => {
    expect(parseSpeciesFilter(" A a , , A a ,B b ")).toEqual(["A a", "B b"]);
  });
  it("null or blank means no filter, never an error", () => {
    expect(parseSpeciesFilter(null)).toEqual([]);
    expect(parseSpeciesFilter("  ")).toEqual([]);
    expect(parseSpeciesFilter(",,,")).toEqual([]);
  });
  it("caps the count -- a URL can't demand a 500-placeholder IN clause", () => {
    const raw = Array.from({ length: 60 }, (_, i) => `Species ${i}`).join(",");
    expect(parseSpeciesFilter(raw)).toHaveLength(40);
  });
});

describe("parseBefore", () => {
  const now = 1_800_000_000;
  it("passes an ordinary cursor through truncated", () => {
    expect(parseBefore("1799990000.7", now)).toBe(1799990000);
  });
  it("garbage and non-positive mean no cursor (newest first)", () => {
    expect(parseBefore(null, now)).toBeNull();
    expect(parseBefore("", now)).toBeNull();
    expect(parseBefore("birds", now)).toBeNull();
    expect(parseBefore("0", now)).toBeNull();
    expect(parseBefore("-5", now)).toBeNull();
  });
  it("clamps the far future to now plus a day", () => {
    expect(parseBefore("9999999999", now)).toBe(now + 86400);
  });
});

describe("dayGroups", () => {
  it("splits newest-first rows at the viewer's local midnight", () => {
    const rows = [
      { ts: noon(2026, 6, 19) + 3600 },
      { ts: noon(2026, 6, 19) },
      { ts: noon(2026, 6, 18) },
      { ts: noon(2026, 6, 15) },
    ];
    const groups = dayGroups(rows);
    expect(groups.map((g) => g.rows.length)).toEqual([2, 1, 1]);
    expect(groups.map((g) => new Date(g.day * 1000).getDate())).toEqual([
      19, 18, 15,
    ]);
    // Every group's day is a true local midnight.
    for (const g of groups) {
      const d = new Date(g.day * 1000);
      expect([d.getHours(), d.getMinutes()]).toEqual([0, 0]);
    }
  });
  it("keeps row order within a group -- it groups, it does not sort", () => {
    const rows = [
      { ts: noon(2026, 6, 19) + 60, who: "a" },
      { ts: noon(2026, 6, 19), who: "b" },
    ];
    expect(dayGroups(rows)[0].rows.map((r) => r.who)).toEqual(["a", "b"]);
  });
  it("groups a DST-transition day once, whatever its length", () => {
    // US fall-back 2026: November 1 is a 25-hour local day; both of that
    // day's rows land in one group, flanked by its neighbours.
    const rows = [
      { ts: noon(2026, 10, 2) },
      { ts: noon(2026, 10, 1) + 3600 },
      { ts: noon(2026, 10, 1) - 3600 },
      { ts: noon(2026, 9, 31) },
    ];
    const groups = dayGroups(rows);
    expect(groups.map((g) => g.rows.length)).toEqual([1, 2, 1]);
  });
  it("is empty for no rows", () => {
    expect(dayGroups([])).toEqual([]);
  });
});

describe("nextBefore", () => {
  it("advances to the oldest loaded row (inclusive query, key dedupe)", () => {
    expect(nextBefore(1000, null)).toBe(1000);
    expect(nextBefore(1000, 2000)).toBe(1000);
  });
  it("steps past a page that made no progress rather than looping", () => {
    expect(nextBefore(2000, 2000)).toBe(1999);
    expect(nextBefore(2500, 2000)).toBe(1999); // never moves forward either
  });
});

describe("dayAnchor", () => {
  it("lands on the last second of the named LOCAL day", () => {
    const end = dayAnchor("2026-07-18");
    expect(end).not.toBeNull();
    const d = new Date((end as number) * 1000);
    expect([d.getDate(), d.getHours(), d.getMinutes(), d.getSeconds()]).toEqual(
      [18, 23, 59, 59],
    );
    // One second later is the 19th -- the boundary is exact.
    expect(new Date(((end as number) + 1) * 1000).getDate()).toBe(19);
  });
  it("handles the 25-hour fall-back day by re-flooring", () => {
    const end = dayAnchor("2026-11-01");
    const d = new Date((end as number) * 1000);
    expect([d.getDate(), d.getHours()]).toEqual([1, 23]);
    expect(new Date(((end as number) + 1) * 1000).getDate()).toBe(2);
  });
  it("rejects garbage and rolled-over dates", () => {
    expect(dayAnchor("")).toBeNull();
    expect(dayAnchor("last tuesday")).toBeNull();
    expect(dayAnchor("2026-2-30")).toBeNull(); // wrong shape
    expect(dayAnchor("2026-02-30")).toBeNull(); // right shape, not a date
    expect(dayAnchor("2026-13-01")).toBeNull();
  });
});

// --- The field desk (issue #220) ----------------------------------------------

/** A local epoch at an hour of the named calendar day (1-indexed month --
 * unlike the chart section's `noon` above, which speaks Date's 0-indexed
 * one), so day-math tests read the same in any timezone. */
const at = (year: number, month: number, day: number, hour = 12) =>
  Math.floor(new Date(year, month - 1, day, hour, 0, 0).getTime() / 1000);

describe("standingFor", () => {
  const yard = [
    entry("jay", "Blue Jay", 40, 0, 9),
    entry("robin", "American Robin", 25, 0, 7),
    entry("finch", "American Goldfinch", 25, 0, 7),
    entry("wren", "Carolina Wren", 10, 0, 0),
  ];
  it("ranks by the given count, competition style", () => {
    expect(standingFor(yard, "jay", (e) => e.visits)).toMatchObject({
      rank: 1,
      of: 4,
      count: 40,
      tied: false,
      leader: null,
    });
    expect(standingFor(yard, "wren", (e) => e.visits)).toMatchObject({
      rank: 4,
      count: 10,
    });
  });
  it("shares a rank on ties and skips the next", () => {
    const robin = standingFor(yard, "robin", (e) => e.visits);
    const finch = standingFor(yard, "finch", (e) => e.visits);
    expect(robin?.rank).toBe(2);
    expect(finch?.rank).toBe(2);
    expect(robin?.tied).toBe(true);
    // The species below the tie is 4th -- 3rd was consumed by the pair.
    expect(standingFor(yard, "wren", (e) => e.visits)?.rank).toBe(4);
  });
  it("names the NEAREST species strictly ahead as the rival", () => {
    expect(standingFor(yard, "wren", (e) => e.visits)?.leader).toEqual({
      species_common: "American Goldfinch", // alphabetical among the tied 25s
      count: 25,
    });
  });
  it("returns null for a species not in the roster", () => {
    expect(standingFor(yard, "ghost", (e) => e.visits)).toBeNull();
  });
});

describe("rivalLine", () => {
  const yard = [
    entry("jay", "Blue Jay", 40),
    entry("finch", "American Goldfinch", 38),
    entry("wren", "Carolina Wren", 0),
  ];
  const s = (sci: string) => standingFor(yard, sci, (e) => e.visits)!;
  it("counts the gap to the next rung", () => {
    expect(rivalLine(s("finch"), "quiet")).toBe("2 visits behind the Blue Jay");
  });
  it("says so at the top", () => {
    expect(rivalLine(s("jay"), "quiet")).toBe("leading the yard");
  });
  it("phrases a one-visit gap in words", () => {
    const close = [entry("a", "A Bird", 5), entry("b", "B Bird", 4)];
    expect(rivalLine(standingFor(close, "b", (e) => e.visits)!, "quiet")).toBe(
      "one visit behind the A Bird",
    );
  });
  it("calls a shared lead a tie", () => {
    const tied = [entry("a", "A Bird", 5), entry("b", "B Bird", 5)];
    expect(rivalLine(standingFor(tied, "a", (e) => e.visits)!, "quiet")).toBe(
      "tied for the lead",
    );
  });
  it("hands zero counts the caller's quiet phrase, never a rank", () => {
    expect(rivalLine(s("wren"), "no visits this week")).toBe(
      "no visits this week",
    );
  });
});

describe("shareOfYard", () => {
  it("rounds to the nearest whole ratio", () => {
    expect(shareOfYard(300, 50)).toBe("1 in 6 of everything Earl hears");
    expect(shareOfYard(100, 32)).toBe("1 in 3 of everything Earl hears");
  });
  it("says nothing over zeros", () => {
    expect(shareOfYard(0, 0)).toBeNull();
    expect(shareOfYard(100, 0)).toBeNull();
  });
  it("calls a majority a majority, not '1 in 1'", () => {
    expect(shareOfYard(100, 80)).toBe("most of everything Earl hears");
  });
});

describe("yardRecords", () => {
  it("is all nulls and zeros on an empty record -- the placeholder state", () => {
    expect(yardRecords([], at(2026, 7, 20))).toEqual({
      busiestDay: null,
      streak: 0,
      longestSilenceDays: 0,
      earliest: null,
      latest: null,
    });
  });
  it("finds the busiest local day, ties to the most recent", () => {
    const r = yardRecords(
      [
        at(2026, 7, 18, 7),
        at(2026, 7, 18, 9),
        at(2026, 7, 19, 7),
        at(2026, 7, 19, 9),
      ],
      at(2026, 7, 20),
    );
    expect(r.busiestDay).toEqual({ day: dayStart(at(2026, 7, 19)), count: 2 });
  });
  it("streak ends today or survives an unfinished today", () => {
    const now = at(2026, 7, 20);
    const heardToday = [at(2026, 7, 18, 7), at(2026, 7, 19, 7), at(2026, 7, 20, 7)];
    expect(yardRecords(heardToday, now).streak).toBe(3);
    // Not heard *yet* today: the streak through yesterday still stands.
    const throughYesterday = [at(2026, 7, 18, 7), at(2026, 7, 19, 7)];
    expect(yardRecords(throughYesterday, now).streak).toBe(2);
    // A day's gap before yesterday ends it.
    const broken = [at(2026, 7, 16, 7), at(2026, 7, 19, 7)];
    expect(yardRecords(broken, now).streak).toBe(1);
    // Last heard before yesterday: no current streak at all.
    const over = [at(2026, 7, 17, 7)];
    expect(yardRecords(over, now).streak).toBe(0);
  });
  it("counts the longest silence in whole days, the ongoing one included", () => {
    const now = at(2026, 7, 20);
    const gapInside = [at(2026, 7, 1, 7), at(2026, 7, 11, 7), at(2026, 7, 20, 7)];
    expect(yardRecords(gapInside, now).longestSilenceDays).toBe(10);
    // A bird gone twelve days: the silence running NOW is the record.
    const goneQuiet = [at(2026, 7, 1, 7), at(2026, 7, 8, 12)];
    expect(yardRecords(goneQuiet, now).longestSilenceDays).toBe(12);
    // Same-day visits: under a day reads as zero whole days.
    const busy = [at(2026, 7, 20, 7), at(2026, 7, 20, 9)];
    expect(yardRecords(busy, at(2026, 7, 20, 10)).longestSilenceDays).toBe(0);
  });
  it("takes earliest and latest as local time-of-day across the record", () => {
    const r = yardRecords(
      [at(2026, 7, 18, 5) + 41 * 60, at(2026, 7, 19, 20)],
      at(2026, 7, 20),
    );
    expect(r.earliest).toBe(5 * 3600 + 41 * 60);
    expect(r.latest).toBe(20 * 3600);
  });
});

describe("secondsIntoDay", () => {
  it("reads the viewer's local clock, not UTC arithmetic", () => {
    expect(secondsIntoDay(at(2026, 7, 18, 5) + 41 * 60)).toBe(
      5 * 3600 + 41 * 60,
    );
    expect(secondsIntoDay(dayStart(at(2026, 7, 18)))).toBe(0);
  });
});

describe("liferNumber", () => {
  const yard = [
    entry("wren", "Carolina Wren", 1, 0, 0, 300),
    entry("jay", "Blue Jay", 1, 0, 0, 100),
    entry("robin", "American Robin", 1, 0, 0, 200),
  ];
  it("numbers by first-heard order", () => {
    expect(liferNumber(yard, "jay")).toEqual({ n: 1, of: 3 });
    expect(liferNumber(yard, "wren")).toEqual({ n: 3, of: 3 });
  });
  it("breaks a first_ts tie by scientific name, one number each", () => {
    const tied = [
      entry("b sci", "B Bird", 1, 0, 0, 100),
      entry("a sci", "A Bird", 1, 0, 0, 100),
    ];
    expect(liferNumber(tied, "a sci")).toEqual({ n: 1, of: 2 });
    expect(liferNumber(tied, "b sci")).toEqual({ n: 2, of: 2 });
  });
  it("returns null off the roster", () => {
    expect(liferNumber(yard, "ghost")).toBeNull();
  });
});

describe("rhythmStrip", () => {
  it("normalizes the stored histogram to the busiest hour", () => {
    const hours = Array.from({ length: 24 }, () => 0);
    hours[7] = 20;
    hours[8] = 10;
    const cells = rhythmStrip({
      hours,
      peak_window: { start_hour: 7, end_hour: 10 },
    })!;
    expect(cells[7]).toEqual({ frac: 1, peak: true });
    expect(cells[8]).toEqual({ frac: 0.5, peak: true });
    expect(cells[12]).toEqual({ frac: 0, peak: false });
    expect(cells[6].peak).toBe(false);
  });
  it("wraps a midnight-spanning peak -- an owl's peak is one stretch", () => {
    const hours = Array.from({ length: 24 }, () => 1);
    const cells = rhythmStrip({
      hours,
      peak_window: { start_hour: 23, end_hour: 2 },
    })!;
    expect(cells[23].peak).toBe(true);
    expect(cells[0].peak).toBe(true);
    expect(cells[1].peak).toBe(true);
    expect(cells[2].peak).toBe(false);
  });
  it("keeps all-zero hours honest flat cells, never NaN", () => {
    const cells = rhythmStrip({ hours: Array.from({ length: 24 }, () => 0) })!;
    expect(cells.every((c) => c.frac === 0)).toBe(true);
  });
  it("returns null without a usable histogram -- the reserved placeholder", () => {
    expect(rhythmStrip(null)).toBeNull();
    expect(rhythmStrip({})).toBeNull();
    expect(rhythmStrip({ hours: [1, 2, 3] })).toBeNull();
  });
});

describe("weatherChips", () => {
  const stats = (over: Partial<NonNullable<AnalysisStats["weather"]>> = {}) => ({
    weather: {
      enough: true,
      conditions: [
        { bucket: "clear", effect: 0.37, thin: false },
        { bucket: "cloudy", effect: -0.22, thin: false },
        { bucket: "unknown", effect: 0.9, thin: false },
        { bucket: "rain", effect: null, thin: true },
      ],
      temperature: [
        { bucket: "warm", effect: 0.18, thin: true },
        { bucket: "mild", effect: 0.04, thin: false }, // about average
      ],
      ...over,
    },
  });
  it("keeps real findings, strongest first, thin flags intact", () => {
    expect(weatherChips(stats())).toEqual([
      { label: "clear", pct: 37, thin: false },
      { label: "cloudy", pct: -22, thin: false },
      { label: "warm", pct: 18, thin: true },
    ]);
  });
  it("skips the unknown bucket, null effects, and about-average findings", () => {
    const labels = weatherChips(stats()).map((c) => c.label);
    expect(labels).not.toContain("unknown");
    expect(labels).not.toContain("rain");
    expect(labels).not.toContain("mild");
  });
  it("renders nothing when the pass itself hedged the whole sample", () => {
    expect(weatherChips(stats({ enough: false }))).toEqual([]);
    expect(weatherChips(null)).toEqual([]);
  });
  it("caps the margin -- a margin is a margin", () => {
    const many = {
      weather: {
        enough: true,
        conditions: Array.from({ length: 8 }, (_, i) => ({
          bucket: `c${i}`,
          effect: 0.5 + i / 100,
          thin: false,
        })),
        temperature: [],
      },
    };
    expect(weatherChips(many)).toHaveLength(4);
  });
});

describe("weekWindowStart", () => {
  it("is six days before the client's midnight -- seven local days inclusive", () => {
    expect(weekWindowStart(700 * 86400)).toBe(694 * 86400);
  });
});

describe("newArrivals", () => {
  const yard = [
    entry("old", "Old Timer", 9, 0, 0, 1000),
    entry("fresh", "Fresh Face", 2, 0, 2, 5000),
    entry("freshest", "Freshest Face", 1, 0, 1, 6000),
    entry("edge", "Edge Case", 1, 0, 1, 3000),
  ];
  it("cuts at sinceTs inclusive, newest first", () => {
    expect(newArrivals(yard, 3000).map((e) => e.species_sci)).toEqual([
      "freshest",
      "fresh",
      "edge", // exactly at the boundary: still an arrival
    ]);
  });
  it("is empty when nothing is new -- the panel's normal day", () => {
    expect(newArrivals(yard, 7000)).toEqual([]);
    expect(newArrivals([], 0)).toEqual([]);
  });
  it("breaks a same-moment tie by name, determinism over drama", () => {
    const twins = [
      entry("b", "B Bird", 1, 0, 1, 5000),
      entry("a", "A Bird", 1, 0, 1, 5000),
    ];
    expect(newArrivals(twins, 0).map((e) => e.species_sci)).toEqual(["a", "b"]);
  });
  it("window constants say what they claim", () => {
    expect(ARRIVALS_24H_S).toBe(86400);
    expect(ARRIVALS_WEEK_S).toBe(7 * 86400);
  });
});
