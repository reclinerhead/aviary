import { describe, expect, it } from "vitest";
import { BirdEvent } from "./bus";
import {
  VISIT_GAP_S,
  clipRelPath,
  clipUrl,
  collapseVisits,
  countVisits,
  detectionFromRow,
  parseLimit,
  parseSince,
  rosterOrder,
  shapeRoster,
  tallyVisits,
  todayVisitors,
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
