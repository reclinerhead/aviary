import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { BirdEvent } from "./bus";
import {
  ARRIVALS_24H_S,
  ARRIVALS_WEEK_S,
  AnalysisStats,
  DETAIL_SPAN_S,
  VISITS_SPAN_S,
  VISIT_GAP_S,
  archiveStats,
  bestClips,
  showcaseBySpecies,
  clampVisitWindow,
  clipRelPath,
  clipUrl,
  coerceGain,
  demoNotice,
  dismissNotice,
  enhancedClipUrl,
  enhancedRelPath,
  collapseVisits,
  countVisits,
  cropPosition,
  dayAnchor,
  dayBuckets,
  mastheadSub,
  aviaryTitle,
  dayGroups,
  dayStart,
  detectionFromRow,
  removalPlan,
  hourBuckets,
  hourStart,
  isNoticeDismissed,
  liferNumber,
  newArrivals,
  nearestBar,
  newBestNoticeId,
  nextBefore,
  noticeDismissKey,
  parseBefore,
  parseLimit,
  parseRange,
  parseSince,
  parseRulePayload,
  parseSpeciesFilter,
  pickVisibleNotice,
  championPaths,
  DEMO_NOTICE_KINDS,
  demoNoticesFor,
  mergeNewBestNotices,
  newBestChanges,
  newBestNotice,
  sampleNotice,
  undismissNotice,
  RARE_RETURN_MATURITY_S,
  RARE_RETURN_MAX_VISITS,
  RARE_RETURN_WINDOW_S,
  type RosterEntry,
  ordinal,
  rareReturnBody,
  rareReturnNotice,
  rareReturnNoticeId,
  rareReturns,
  portraitAspect,
  portraitUrl,
  portraitVersion,
  reconcileOrder,
  reconcileRail,
  removalUrl,
  rhythmCellExplain,
  rhythmStrip,
  rivalLine,
  rosterOrder,
  ruleIsEmpty,
  ruleStamp,
  coerceRuleThreshold,
  speciesRuleFrom,
  type SpeciesRule,
  secondsIntoDay,
  shapeRoster,
  voiceCredit,
  voiceProse,
  shareOfYard,
  smoothPath,
  smoothSegments,
  speciesImageName,
  speciesThumbName,
  LIFE_LIST_QUERY_KEY,
  readLifeListQuery,
  speciesMatches,
  standingFor,
  writeLifeListQuery,
  tallyVisits,
  todayVisitors,
  visitHourTicks,
  visitTicks,
  visitsCeil,
  weatherChips,
  weatherChipExplain,
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
      A: { visits: 2, today: 0, week: 0, last_ts: 400 },
      B: { visits: 1, today: 0, week: 0, last_ts: 110 },
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
      A: { visits: 2, today: 1, week: 2, last_ts: 2000 },
    });
  });
  it("counts no today buckets when todayStart is null", () => {
    expect(tallyVisits([{ species_sci: "A", ts: 5 }], null)).toEqual({
      A: { visits: 1, today: 0, week: 0, last_ts: 5 },
    });
  });
  it("last_ts is the OPENING of the latest visit, whatever the row order (#549)", () => {
    // A continuation row inside the same visit never moves it...
    expect(
      tallyVisits(
        [
          { species_sci: "A", ts: 1000 },
          { species_sci: "A", ts: 1030 },
          { species_sci: "A", ts: 1055 },
        ],
        null,
      ).A.last_ts,
    ).toBe(1000);
    // ...and an out-of-order row set still yields the latest opening.
    expect(
      tallyVisits(
        [
          { species_sci: "A", ts: 5000 },
          { species_sci: "A", ts: 100 },
          { species_sci: "A", ts: 5020 },
          { species_sci: "A", ts: 2000 },
        ],
        null,
      ).A.last_ts,
    ).toBe(5000);
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
      A: { visits: 3, today: 1, week: 2, last_ts: midnight + 100 },
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
      A: { visits: 1, today: 0, week: 0, last_ts: weekStart - 1 },
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

describe("coerceGain", () => {
  it("passes legal steps through untouched", () => {
    expect(coerceGain(1)).toBe(1);
    expect(coerceGain(2)).toBe(2);
    expect(coerceGain("4")).toBe(4);
    expect(coerceGain("8")).toBe(8);
  });
  it("reads garbage as unity -- the do-no-harm default", () => {
    expect(coerceGain(null)).toBe(1);
    expect(coerceGain("")).toBe(1);
    expect(coerceGain("loud")).toBe(1);
    expect(coerceGain("NaN")).toBe(1);
    expect(coerceGain(0)).toBe(1);
    expect(coerceGain(-3)).toBe(1);
  });
  it("snaps off-step values to the nearest step, ties toward quieter", () => {
    expect(coerceGain("1.4")).toBe(1);
    expect(coerceGain(3)).toBe(2); // equidistant from 2 and 4 -- round down
    expect(coerceGain(5)).toBe(4);
    expect(coerceGain(6.5)).toBe(8);
    expect(coerceGain("999")).toBe(8); // an old wider range can only clamp in
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
  it("a bare call keeps the pre-#495 URL byte-for-byte", () => {
    expect(portraitUrl("Cardinalis cardinalis", {})).toBe(
      "/aviary/portrait/Cardinalis%20cardinalis",
    );
  });
  it("asks for the tile sibling and carries the shelf version (#495)", () => {
    expect(portraitUrl("Cardinalis cardinalis", { thumb: true })).toBe(
      "/aviary/portrait/Cardinalis%20cardinalis?size=thumb",
    );
    expect(
      portraitUrl("Cardinalis cardinalis", { thumb: true, v: "1-2.3-4" }),
    ).toBe("/aviary/portrait/Cardinalis%20cardinalis?size=thumb&v=1-2.3-4");
    expect(portraitUrl("Cardinalis cardinalis", { v: "1-2.3-4" })).toBe(
      "/aviary/portrait/Cardinalis%20cardinalis?v=1-2.3-4",
    );
  });
  it("a null/absent version emits a version-less URL, never v=null", () => {
    expect(portraitUrl("X y", { thumb: true, v: null })).toBe(
      "/aviary/portrait/X%20y?size=thumb",
    );
  });
});

describe("speciesThumbName", () => {
  it("mirrors the pass's thumb_name exactly", () => {
    expect(speciesThumbName("Cardinalis_cardinalis.jpg")).toBe(
      "Cardinalis_cardinalis-thumb.jpg",
    );
  });
  it("stays inside the shelf allowlist by construction", () => {
    // The input is the ALREADY-SCRUBBED name; appending -thumb cannot add a
    // character the allowlist rejects, so the guard's verdict is unchanged.
    expect(speciesThumbName("a_b-c.jpg")).toMatch(/^[A-Za-z0-9_-]+\.jpg$/);
  });
});

describe("portraitVersion", () => {
  const full = { mtimeMs: 1000.4, size: 200 };
  it("tokens both files' stat facts, mtime rounded", () => {
    expect(portraitVersion(full, { mtimeMs: 2000.6, size: 50 })).toBe(
      "1000-200.2001-50",
    );
  });
  it("versions the thumb's ABSENCE too -- the backfill landing is a change", () => {
    const before = portraitVersion(full, null);
    expect(before).toBe("1000-200.0-0");
    expect(before).not.toBe(portraitVersion(full, { mtimeMs: 0, size: 1 }));
  });
  it("changes when either file's bytes do", () => {
    const t = { mtimeMs: 2000, size: 50 };
    expect(portraitVersion(full, t)).not.toBe(
      portraitVersion({ mtimeMs: 1000.4, size: 201 }, t),
    );
    expect(portraitVersion(full, t)).not.toBe(
      portraitVersion(full, { mtimeMs: 2000, size: 51 }),
    );
  });
});

describe("removalPlan", () => {
  it("moves sighting clips with their -enh siblings to quarantine", () => {
    expect(
      removalPlan(
        ["amcrest/1000-Wood_Duck.wav", "rover/2000-Wood_Duck.wav"],
        null,
      ),
    ).toEqual({
      moves: [
        {
          from: "amcrest/1000-Wood_Duck.wav",
          to: "quarantine/amcrest/1000-Wood_Duck.wav",
        },
        {
          from: "amcrest/1000-Wood_Duck-enh.wav",
          to: "quarantine/amcrest/1000-Wood_Duck-enh.wav",
        },
        {
          from: "rover/2000-Wood_Duck.wav",
          to: "quarantine/rover/2000-Wood_Duck.wav",
        },
        {
          from: "rover/2000-Wood_Duck-enh.wav",
          to: "quarantine/rover/2000-Wood_Duck-enh.wav",
        },
      ],
      deletes: [],
    });
  });
  it("dedupes first_clip against the sighting row that made it", () => {
    // The lifer's opening sighting and life_list.first_clip name the same
    // file -- the caller appends first_clip to the row clips, and the plan
    // must not move it twice.
    expect(
      removalPlan(
        ["amcrest/1000-Wood_Duck.wav", "amcrest/1000-Wood_Duck.wav"],
        null,
      ).moves,
    ).toEqual([
      {
        from: "amcrest/1000-Wood_Duck.wav",
        to: "quarantine/amcrest/1000-Wood_Duck.wav",
      },
      {
        from: "amcrest/1000-Wood_Duck-enh.wav",
        to: "quarantine/amcrest/1000-Wood_Duck-enh.wav",
      },
    ]);
  });
  it("skips null clips (pre-clip-era rows) quietly", () => {
    expect(
      removalPlan([null, undefined, "amcrest/1000-Blue_Jay.wav"], null).moves,
    ).toEqual([
      {
        from: "amcrest/1000-Blue_Jay.wav",
        to: "quarantine/amcrest/1000-Blue_Jay.wav",
      },
      {
        from: "amcrest/1000-Blue_Jay-enh.wav",
        to: "quarantine/amcrest/1000-Blue_Jay-enh.wav",
      },
    ]);
    expect(removalPlan([], null)).toEqual({ moves: [], deletes: [] });
  });
  it("re-runs the clips route's guard: a hostile row skips, never escapes", () => {
    expect(
      removalPlan(
        ["../../../etc/passwd", "a/b/c.wav", "amcrest/a.b.wav", "amcrest/x.mp3"],
        null,
      ),
    ).toEqual({ moves: [], deletes: [] });
  });
  it("does not name an -enh-enh for a row already holding a sibling", () => {
    // Sightings never store the sibling, but a hand-edited row is a row:
    // enhancedRelPath's no-double-enhance rule carries over unchanged.
    expect(removalPlan(["amcrest/1000-Blue_Jay-enh.wav"], null).moves).toEqual([
      {
        from: "amcrest/1000-Blue_Jay-enh.wav",
        to: "quarantine/amcrest/1000-Blue_Jay-enh.wav",
      },
    ]);
  });
  it("deletes the portrait AND its thumb sibling -- refetchable, so not quarantined", () => {
    // The sibling is named unconditionally (#495, the -enh precedent): a
    // thumb never written is a missing file the delete loop forgives.
    expect(removalPlan([], "Aix_sparsa.jpg")).toEqual({
      moves: [],
      deletes: ["species/Aix_sparsa.jpg", "species/Aix_sparsa-thumb.jpg"],
    });
  });
  it("refuses a portrait filename that could step off the shelf", () => {
    expect(removalPlan([], "../evil.jpg").deletes).toEqual([]);
    expect(removalPlan([], "x/y.jpg").deletes).toEqual([]);
    expect(removalPlan([], "a.b.jpg").deletes).toEqual([]);
    expect(removalPlan([], "portrait.png").deletes).toEqual([]);
    expect(removalPlan([], null).deletes).toEqual([]);
    expect(removalPlan([], "").deletes).toEqual([]);
  });
});

describe("per-species detection rules (#506)", () => {
  const rule = (over: Partial<SpeciesRule> = {}): SpeciesRule => ({
    exclude: false,
    threshold: null,
    always_include: false,
    ...over,
  });

  describe("ruleStamp", () => {
    // The hero-meta stamp is RESERVED (house rule #1): every state answers a
    // string, so the entry renders on every species and never pops in.
    it("reads default with no rule at all", () => {
      expect(ruleStamp(null)).toBe("default");
      expect(ruleStamp(undefined)).toBe("default");
      expect(ruleStamp(rule())).toBe("default");
    });
    it("speaks the threshold in the overlay's own step copy", () => {
      expect(ruleStamp(rule({ threshold: 0.8 }))).toBe("threshold 0.80");
      expect(ruleStamp(rule({ threshold: 0.9 }))).toBe("threshold 0.90");
    });
    it("reads ignored, alone, when the veto stands", () => {
      // Exclude makes the other fields moot -- exactly as the listener
      // treats them -- so the stamp says only the thing that governs.
      expect(ruleStamp(rule({ exclude: true }))).toBe("ignored");
      expect(
        ruleStamp(rule({ exclude: true, threshold: 0.8, always_include: true })),
      ).toBe("ignored");
    });
    it("reads map override, and joins it with a threshold", () => {
      expect(ruleStamp(rule({ always_include: true }))).toBe("map override");
      expect(ruleStamp(rule({ threshold: 0.9, always_include: true }))).toBe(
        "threshold 0.90 · map override",
      );
    });
  });

  describe("parseRulePayload", () => {
    it("shapes the overlay's body", () => {
      expect(
        parseRulePayload({ exclude: false, threshold: 0.8, always_include: true }),
      ).toEqual(rule({ threshold: 0.8, always_include: true }));
    });
    it("treats absent fields as defaults", () => {
      expect(parseRulePayload({})).toEqual(rule());
      expect(parseRulePayload({ exclude: true })).toEqual(
        rule({ exclude: true }),
      );
    });
    it("refuses anything that is not a rule-shaped object", () => {
      expect(parseRulePayload(null)).toBeNull();
      expect(parseRulePayload("exclude")).toBeNull();
      expect(parseRulePayload([0.8])).toBeNull();
      expect(parseRulePayload({ threshold: "0.8" })).toBeNull();
      expect(parseRulePayload({ threshold: 0 })).toBeNull();
      expect(parseRulePayload({ threshold: 1 })).toBeNull();
      expect(parseRulePayload({ threshold: NaN })).toBeNull();
      expect(parseRulePayload({ exclude: 1 })).toBeNull();
      expect(parseRulePayload({ always_include: "yes" })).toBeNull();
    });
  });

  describe("ruleIsEmpty", () => {
    it("an all-defaults rule holds no policy -- the route deletes the row", () => {
      expect(ruleIsEmpty(rule())).toBe(true);
      expect(ruleIsEmpty(rule({ exclude: true }))).toBe(false);
      expect(ruleIsEmpty(rule({ threshold: 0.8 }))).toBe(false);
      expect(ruleIsEmpty(rule({ always_include: true }))).toBe(false);
    });
  });

  describe("speciesRuleFrom", () => {
    it("turns sqlite integers into the wire shape", () => {
      expect(
        speciesRuleFrom({ exclude: 1, threshold: null, always_include: 0 }),
      ).toEqual(rule({ exclude: true }));
      expect(
        speciesRuleFrom({ exclude: 0, threshold: 0.9, always_include: 1 }),
      ).toEqual(rule({ threshold: 0.9, always_include: true }));
      expect(
        speciesRuleFrom({ exclude: null, threshold: null, always_include: null }),
      ).toEqual(rule());
    });
  });

  describe("coerceRuleThreshold", () => {
    it("keeps Default and lands stored values on a step, ties toward lower", () => {
      expect(coerceRuleThreshold(null)).toBeNull();
      expect(coerceRuleThreshold(undefined)).toBeNull();
      expect(coerceRuleThreshold(0.8)).toBe(0.8);
      expect(coerceRuleThreshold(0.9)).toBe(0.9);
      expect(coerceRuleThreshold(0.7)).toBe(0.8); // below the steps
      expect(coerceRuleThreshold(0.85)).toBe(0.8); // the tie, toward lower
      expect(coerceRuleThreshold(0.95)).toBe(0.9); // above the steps
    });
  });

  describe("removalUrl", () => {
    it("carries the don't-let-it-come-back checkbox as a query flag", () => {
      expect(removalUrl("Aix sponsa", false)).toBe(
        "/aviary/species/Aix%20sponsa",
      );
      expect(removalUrl("Aix sponsa", true)).toBe(
        "/aviary/species/Aix%20sponsa?exclude_future=1",
      );
    });
  });
});

describe("voiceCredit", () => {
  it("credits Wikipedia either way -- same article, same licence", () => {
    expect(voiceCredit("section")).toContain("via Wikipedia");
    expect(voiceCredit("sentences")).toContain("via Wikipedia");
  });

  it("says so when the prose was gathered rather than quoted", () => {
    expect(voiceCredit("sentences")).toBe(
      "gathered from the article · via Wikipedia",
    );
    expect(voiceCredit("section")).toBe("via Wikipedia");
  });

  it("treats an unknown or missing source as a section", () => {
    // Pre-#317 rows are all sections by construction, and over-claiming
    // "gathered" about a real section would be its own small lie.
    expect(voiceCredit(null)).toBe("via Wikipedia");
    expect(voiceCredit(undefined)).toBe("via Wikipedia");
    expect(voiceCredit("something-new")).toBe("via Wikipedia");
  });
});

describe("voiceProse", () => {
  const wiki = {
    voice: "It goes teakettle-teakettle.",
    voice_source: "section",
    voice_adw: null,
    voice_adw_attribution: null,
  };
  const adw = {
    voice: null,
    voice_source: "adw",
    voice_adw: "Their song is a long, rich warble.",
    voice_adw_attribution: "via Animal Diversity Web · CC BY-NC-SA 3.0",
  };

  it("pairs Wikipedia prose with a Wikipedia credit", () => {
    expect(voiceProse(wiki)).toEqual({
      text: "It goes teakettle-teakettle.",
      credit: "via Wikipedia",
    });
    expect(voiceProse({ ...wiki, voice_source: "sentences" })?.credit).toBe(
      "gathered from the article · via Wikipedia",
    );
  });

  it("pairs ADW prose with ADW's own stored credit", () => {
    // The licensing rule the whole pair exists for: ADW prose under a "via
    // Wikipedia" stamp would be a licensing failure as well as a lie.
    expect(voiceProse(adw)).toEqual({
      text: "Their song is a long, rich warble.",
      credit: "via Animal Diversity Web · CC BY-NC-SA 3.0",
    });
    expect(voiceProse(adw)?.credit).not.toContain("Wikipedia");
    expect(voiceProse(adw)?.credit).toContain("CC BY-NC-SA");
  });

  it("never serves one source's text under the other's credit", () => {
    // The two texts are never both present (the pass clears the ADW columns
    // when Wikipedia answers), but if a hand-edited row ever carried both,
    // the source column decides -- and the credit follows the text, always.
    const both = { ...wiki, voice_adw: "ADW text", voice_adw_attribution: "x" };
    expect(voiceProse(both)).toEqual({
      text: "It goes teakettle-teakettle.",
      credit: "via Wikipedia",
    });
    expect(voiceProse({ ...both, voice_source: "adw" })).toEqual({
      text: "ADW text",
      credit: "x",
    });
  });

  it("names ADW even on a row whose stored attribution is missing", () => {
    expect(
      voiceProse({ ...adw, voice_adw_attribution: null })?.credit,
    ).toContain("Animal Diversity Web");
  });

  it("is null when no source has anything -- the honest blank", () => {
    expect(voiceProse(null)).toBeNull();
    expect(voiceProse(undefined)).toBeNull();
    expect(
      voiceProse({
        voice: null,
        voice_source: null,
        voice_adw: null,
        voice_adw_attribution: null,
      }),
    ).toBeNull();
    // "adw" claimed but no prose stored: still nothing, never an empty panel.
    expect(voiceProse({ ...adw, voice_adw: null })).toBeNull();
  });

  it("reads a pre-#318 row exactly as before", () => {
    // A store one generation behind LEFT JOINs the ADW columns to undefined.
    expect(
      voiceProse({ voice: "It sings.", voice_source: "section" }),
    ).toEqual({ text: "It sings.", credit: "via Wikipedia" });
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

describe("parseRange (#377)", () => {
  // A 100-byte file; ranges are INCLUSIVE both ends (RFC 7233).
  const SIZE = 100;

  it("absence means no range -- the plain 200 path", () => {
    expect(parseRange(null, SIZE)).toBeNull();
  });
  it("parses the WebKit probe that motivated the whole function", () => {
    expect(parseRange("bytes=0-1", SIZE)).toEqual({ start: 0, end: 1 });
  });
  it("open-ended start-to-EOF", () => {
    expect(parseRange("bytes=40-", SIZE)).toEqual({ start: 40, end: 99 });
  });
  it("suffix range takes the LAST n bytes", () => {
    expect(parseRange("bytes=-30", SIZE)).toEqual({ start: 70, end: 99 });
    // A suffix longer than the file is the whole file, not an error.
    expect(parseRange("bytes=-500", SIZE)).toEqual({ start: 0, end: 99 });
  });
  it("clamps an end past EOF -- asking long is fine", () => {
    expect(parseRange("bytes=90-150", SIZE)).toEqual({ start: 90, end: 99 });
  });
  it("the exact last byte", () => {
    expect(parseRange("bytes=99-99", SIZE)).toEqual({ start: 99, end: 99 });
  });
  it("start at or past EOF is unsatisfiable -> 416", () => {
    expect(parseRange("bytes=100-", SIZE)).toBe("unsatisfiable");
    expect(parseRange("bytes=500-600", SIZE)).toBe("unsatisfiable");
  });
  it("an empty file satisfies nothing", () => {
    expect(parseRange("bytes=0-0", 0)).toBe("unsatisfiable");
    expect(parseRange("bytes=-5", 0)).toBe("unsatisfiable");
  });
  it("a zero-length suffix names no byte -> 416", () => {
    expect(parseRange("bytes=-0", SIZE)).toBe("unsatisfiable");
  });
  it("malformed or exotic asks are ignored, never honored wrong", () => {
    expect(parseRange("bytes=-", SIZE)).toBeNull(); // both ends empty
    expect(parseRange("bytes=5-2", SIZE)).toBeNull(); // end before start
    expect(parseRange("bytes=0-1,5-6", SIZE)).toBeNull(); // multi-range
    expect(parseRange("items=0-1", SIZE)).toBeNull(); // non-byte units
    expect(parseRange("bytes=one-two", SIZE)).toBeNull();
    expect(parseRange("", SIZE)).toBeNull();
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

describe("bestClips (#333 / #442)", () => {
  const row = (
    ts: number,
    confidence: number,
    clip: string | null,
    sci = "Cardinalis cardinalis",
    vocal_fill: number | null = null,
  ) => ({ species_sci: sci, ts, confidence, clip, vocal_fill });

  it("picks the highest-confidence recording per species when unscored", () => {
    expect(
      bestClips([row(100, 0.7, "a.wav"), row(200, 0.95, "b.wav"), row(300, 0.8, "c.wav")]),
    ).toEqual({
      "Cardinalis cardinalis": { clip: "b.wav", confidence: 0.95, ts: 200 },
    });
  });

  it("never crowns a clip-less sighting", () => {
    // A failed clip write is an honest gap; a confident number attached to
    // silence would be the page's worst possible offer.
    expect(bestClips([row(100, 0.99, null), row(200, 0.6, "b.wav")])).toEqual({
      "Cardinalis cardinalis": { clip: "b.wav", confidence: 0.6, ts: 200 },
    });
    expect(bestClips([row(100, 0.99, null)])).toEqual({});
  });

  it("keeps the earlier take on a confidence tie", () => {
    // Two equally good recordings: the older one has already survived a
    // retention sweep the newer one hasn't faced.
    expect(
      bestClips([row(100, 0.9, "old.wav"), row(200, 0.9, "new.wav")]),
    ).toMatchObject({
      "Cardinalis cardinalis": { clip: "old.wav", ts: 100 },
    });
  });

  it("keeps species independent", () => {
    const out = bestClips([
      row(100, 0.9, "card.wav"),
      row(200, 0.5, "robin.wav", "Turdus migratorius"),
    ]);
    expect(out["Cardinalis cardinalis"].clip).toBe("card.wav");
    expect(out["Turdus migratorius"].clip).toBe("robin.wav");
  });

  it("ignores rows with no confidence (the tally's narrow shape)", () => {
    expect(bestClips([{ species_sci: "Cardinalis cardinalis", ts: 100 }])).toEqual(
      {},
    );
  });

  it("prefers fuller song inside the confidence window (#442)", () => {
    // 0.98 chirp vs 0.94 full song — both eligible; fill decides.
    expect(
      bestClips([
        row(100, 0.98, "chirp.wav", "Cardinalis cardinalis", 0.08),
        row(200, 0.94, "song.wav", "Cardinalis cardinalis", 0.72),
      ]),
    ).toMatchObject({
      "Cardinalis cardinalis": { clip: "song.wav", confidence: 0.94 },
    });
  });

  it("does not let a low-confidence full song steal the crown", () => {
    expect(
      bestClips([
        row(100, 0.98, "chirp.wav", "Cardinalis cardinalis", 0.05),
        row(200, 0.55, "long.wav", "Cardinalis cardinalis", 0.95),
      ]),
    ).toMatchObject({
      "Cardinalis cardinalis": { clip: "chirp.wav", confidence: 0.98 },
    });
  });

  it("matches the shared fixture the Python showcase pass reads (#551)", () => {
    // fixtures/best_clips.json is read by BOTH suites: slot 1 of the species
    // showcase (listener/clip_showcase.py, retention-protected) must be the
    // same clip this function crowns on the page. Add cases there, not here.
    const fixture = JSON.parse(
      readFileSync(
        fileURLToPath(new URL("../../fixtures/best_clips.json", import.meta.url)),
        "utf8",
      ),
    ) as {
      cases: {
        name: string;
        rows: Parameters<typeof bestClips>[0];
        expected: ReturnType<typeof bestClips>;
      }[];
    };
    expect(fixture.cases.length).toBeGreaterThan(5);
    for (const c of fixture.cases) {
      expect(bestClips(c.rows), c.name).toEqual(c.expected);
    }
  });

  it("on equal fill, prefers higher confidence then earlier ts", () => {
    expect(
      bestClips([
        row(100, 0.92, "a.wav", "Cardinalis cardinalis", 0.5),
        row(200, 0.96, "b.wav", "Cardinalis cardinalis", 0.5),
      ]),
    ).toMatchObject({
      "Cardinalis cardinalis": { clip: "b.wav", confidence: 0.96 },
    });
    expect(
      bestClips([
        row(100, 0.95, "old.wav", "Cardinalis cardinalis", 0.5),
        row(200, 0.95, "new.wav", "Cardinalis cardinalis", 0.5),
      ]),
    ).toMatchObject({
      "Cardinalis cardinalis": { clip: "old.wav", ts: 100 },
    });
  });
});

describe("showcaseBySpecies (#551)", () => {
  it("groups rows per species in slot order and drops malformed ones", () => {
    const out = showcaseBySpecies([
      { species_sci: "A", slot: 3, clip: "a3.wav", confidence: 0.8, vocal_fill: 0.3, ts: 300 },
      { species_sci: "A", slot: 1, clip: "a1.wav", confidence: 0.95, vocal_fill: 0.6, ts: 100 },
      { species_sci: "B", slot: 1, clip: "b1.wav", confidence: null, vocal_fill: null, ts: null },
      { species_sci: "A", slot: 2, clip: null, confidence: 0.9, vocal_fill: 0.5, ts: 200 },
      { species_sci: "A", slot: 4, clip: "a4.wav", confidence: 0.9, vocal_fill: 0.5, ts: 400 },
    ]);
    expect(out.A.map((s) => s.slot)).toEqual([1, 3]);
    expect(out.A[0]).toEqual({
      slot: 1,
      clip: "a1.wav",
      confidence: 0.95,
      vocal_fill: 0.6,
      ts: 100,
    });
    // A pre-#551-sighting join miss (no ts) and NULL numbers are honest nulls.
    expect(out.B).toEqual([
      { slot: 1, clip: "b1.wav", confidence: null, vocal_fill: null, ts: null },
    ]);
    expect(showcaseBySpecies([])).toEqual({});
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
    expect(roster[1]).toMatchObject({ visits: 2, today: 0, last_ts: 500 });
    expect(roster[0]).toMatchObject({ visits: 1, today: 1, last_ts: 600 });
  });
  it("tallies a lifer with no sighting rows honestly at zero", () => {
    const roster = shapeRoster(life.slice(0, 1), [], null);
    expect(roster[0]).toMatchObject({ visits: 0, today: 0, last_ts: null });
  });
  it("carries the best recording onto the entry (#333)", () => {
    const roster = shapeRoster(
      life.slice(0, 1),
      [
        {
          species_sci: "Cardinalis cardinalis",
          ts: 100,
          confidence: 0.7,
          clip: "amcrest/100-Northern_Cardinal.wav",
        },
        {
          species_sci: "Cardinalis cardinalis",
          ts: 500,
          confidence: 0.95,
          clip: "amcrest/500-Northern_Cardinal.wav",
        },
      ],
      null,
    );
    expect(roster[0]).toMatchObject({
      best_clip: "amcrest/500-Northern_Cardinal.wav",
      best_confidence: 0.95,
      best_ts: 500,
      // First contact is untouched -- the historical record keeps its place.
      first_clip: "amcrest/100-Northern_Cardinal.wav",
    });
  });
  it("carries the showcase onto the entry, empty when the pass never ran (#551)", () => {
    const withShowcase = shapeRoster(life.slice(0, 1), [], null, {
      "Cardinalis cardinalis": [
        { slot: 1, clip: "a.wav", confidence: 0.95, vocal_fill: 0.6, ts: 100 },
        { slot: 2, clip: "b.wav", confidence: 0.9, vocal_fill: 0.5, ts: 200 },
      ],
    });
    expect(withShowcase[0].showcase).toEqual([
      { slot: 1, clip: "a.wav", confidence: 0.95, vocal_fill: 0.6, ts: 100 },
      { slot: 2, clip: "b.wav", confidence: 0.9, vocal_fill: 0.5, ts: 200 },
    ]);
    expect(shapeRoster(life.slice(0, 1), [], null)[0].showcase).toEqual([]);
  });
  it("leaves the best null when no sighting carries a clip", () => {
    const roster = shapeRoster(
      life.slice(0, 1),
      [{ species_sci: "Cardinalis cardinalis", ts: 100, confidence: 0.9 }],
      null,
    );
    expect(roster[0].best_clip).toBeNull();
    expect(roster[0].best_confidence).toBeNull();
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

// --- Life-list search (#352) -------------------------------------------------

describe("speciesMatches", () => {
  const downy = entry("Dryobates pubescens", "Downy Woodpecker", 3);
  it("matches a case-insensitive substring of the common name", () => {
    expect(speciesMatches(downy, "wood")).toBe(true);
    expect(speciesMatches(downy, "WOOD")).toBe(true);
    expect(speciesMatches(downy, "downy wood")).toBe(true);
    expect(speciesMatches(downy, "finch")).toBe(false);
  });
  it("matches the scientific name too", () => {
    expect(speciesMatches(downy, "dryobates")).toBe(true);
    expect(speciesMatches(downy, "pubescens")).toBe(true);
  });
  it("a fragment mid-word still hits -- substring, not prefix", () => {
    expect(speciesMatches(downy, "pecker")).toBe(true);
  });
  it("blank and all-whitespace queries match everything", () => {
    expect(speciesMatches(downy, "")).toBe(true);
    expect(speciesMatches(downy, "   ")).toBe(true);
  });
  it("a missing entry matches nothing, even on a blank query", () => {
    expect(speciesMatches(undefined, "")).toBe(false);
    expect(speciesMatches(undefined, "wood")).toBe(false);
  });
  it("trims the query -- ' wood ' is 'wood'", () => {
    expect(speciesMatches(downy, "  wood  ")).toBe(true);
  });
});

// --- Life-list query session persistence (#410) ------------------------------

function memoryStore(seed: Record<string, string> = {}) {
  const map = new Map(Object.entries(seed));
  return {
    getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
    setItem: (k: string, v: string) => {
      map.set(k, v);
    },
    removeItem: (k: string) => {
      map.delete(k);
    },
    // Test introspection only.
    _map: map,
  };
}

describe("life-list query session storage (#410)", () => {
  it("reads empty when the key is missing or the store is null", () => {
    expect(readLifeListQuery(null)).toBe("");
    expect(readLifeListQuery(memoryStore())).toBe("");
  });
  it("round-trips a query through the fixed key", () => {
    const store = memoryStore();
    writeLifeListQuery(store, "finch");
    expect(store.getItem(LIFE_LIST_QUERY_KEY)).toBe("finch");
    expect(readLifeListQuery(store)).toBe("finch");
  });
  it("clear removes the key rather than leaving an empty string", () => {
    const store = memoryStore({ [LIFE_LIST_QUERY_KEY]: "finch" });
    writeLifeListQuery(store, "");
    expect(store.getItem(LIFE_LIST_QUERY_KEY)).toBeNull();
    expect(readLifeListQuery(store)).toBe("");
  });
  it("preserves whitespace as typed -- matching still trims at filter time", () => {
    const store = memoryStore();
    writeLifeListQuery(store, "  wood  ");
    expect(readLifeListQuery(store)).toBe("  wood  ");
  });
  it("survives a throwing store without raising", () => {
    const broken = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => {
        throw new Error("denied");
      },
    };
    expect(readLifeListQuery(broken)).toBe("");
    expect(() => writeLifeListQuery(broken, "finch")).not.toThrow();
  });
});

// --- The reconciliation poll (#352) ------------------------------------------

describe("reconcileOrder", () => {
  it("preserves the existing order exactly and appends the unseen at the end", () => {
    const next = reconcileOrder(
      ["B", "A"],
      [
        entry("A", "American Robin", 9),
        entry("B", "Blue Jay", 5),
        entry("C", "Carolina Wren", 1),
        entry("D", "Downy Woodpecker", 2),
      ],
    );
    expect(next).toEqual(["B", "A", "C", "D"]);
  });
  it("never drops a key the payload omitted -- a tile can't vanish mid-read", () => {
    const prev = ["A", "B"];
    const next = reconcileOrder(prev, [entry("A", "American Robin", 9)]);
    expect(next).toEqual(["A", "B"]);
    // And it is prev ITSELF when nothing is new, so a quiet poll re-renders
    // nothing.
    expect(next).toBe(prev);
  });
});

describe("reconcileRail", () => {
  const tile = (sci: string, common: string, count: number) => ({
    species_sci: sci,
    species_common: common,
    count,
  });
  it("preserves pill order, lands counts in place, appends newcomers", () => {
    const next = reconcileRail(
      [tile("C", "Carolina Wren", 3), tile("A", "American Robin", 1)],
      [
        entry("A", "American Robin", 9, 2),
        entry("B", "Blue Jay", 5, 1),
        entry("C", "Carolina Wren", 5, 4),
        entry("D", "Downy Woodpecker", 1, 2),
      ],
    );
    expect(next).toEqual([
      // Existing pills keep their seats, counts refreshed from the store.
      tile("C", "Carolina Wren", 4),
      tile("A", "American Robin", 2),
      // Newcomers append, most-visited-today first among themselves.
      tile("D", "Downy Woodpecker", 2),
      tile("B", "Blue Jay", 1),
    ]);
  });
  it("empties on a midnight rollover: an explicit today=0 retires a pill", () => {
    const next = reconcileRail(
      [tile("A", "American Robin", 7), tile("B", "Blue Jay", 2)],
      [entry("A", "American Robin", 9, 0), entry("B", "Blue Jay", 5, 0)],
    );
    expect(next).toEqual([]);
  });
  it("keeps a pill the payload omitted -- unknown is not zero", () => {
    // A just-minted lifer's row can trail its own bus event; the pill the
    // live merge showed must not flicker out and back.
    const next = reconcileRail(
      [tile("A", "American Robin", 1)],
      [entry("B", "Blue Jay", 5, 1)],
    );
    expect(next).toEqual([tile("A", "American Robin", 1), tile("B", "Blue Jay", 1)]);
  });
  it("picks up a common-name correction from the store", () => {
    const next = reconcileRail(
      [tile("A", "Amercan Robin", 1)],
      [entry("A", "American Robin", 9, 1)],
    );
    expect(next).toEqual([tile("A", "American Robin", 1)]);
  });
});

// --- The archive hero (#260) -------------------------------------------------

describe("archiveStats", () => {
  // Mid-July local noon: away from any DST transition, so adding exact
  // multiples of 86400 keeps the local time of day (and CI's UTC has no
  // transitions at all).
  const JUL_18_NOON = 1784376000; // 2026-07-18 12:00 UTC
  it("is all zeros and nulls on an empty archive -- the fresh-aviary values", () => {
    expect(archiveStats([], JUL_18_NOON)).toEqual({
      species: 0,
      visits: 0,
      week: 0,
      today: 0,
      detectionsToday: 0,
      detectionsWeek: 0,
      since: null,
      days: null,
      visitsPerWeek: null,
      visitsPerMonth: null,
    });
  });
  it("counts distinct species per window and sums visits", () => {
    const stats = archiveStats(
      [
        // entry(sci, common, visits, today, week, first_ts)
        entry("A", "American Robin", 40, 2, 9, 5000),
        entry("B", "Blue Jay", 25, 0, 3, 2000),
        entry("C", "Carolina Wren", 1, 0, 0, 9000),
      ],
      JUL_18_NOON,
    );
    // A species is one species however many visits it logged.
    expect(stats.species).toBe(3);
    expect(stats.visits).toBe(66);
    // Window membership is week/today > 0, the roster's own tallies.
    expect(stats.week).toBe(2);
    expect(stats.today).toBe(1);
    // detectionsToday sums the per-species today tallies (volume, not
    // diversity) -- robin's 2, jay and wren contribute 0 (#413).
    expect(stats.detectionsToday).toBe(2);
    // detectionsWeek sums the per-species week tallies the same way:
    // robin 9 + jay 3 + wren 0.
    expect(stats.detectionsWeek).toBe(12);
  });
  it("sums detectionsToday across every species with activity today", () => {
    const stats = archiveStats(
      [
        entry("A", "American Robin", 10, 3, 5, 5000),
        entry("B", "Blue Jay", 8, 7, 8, 2000),
        entry("C", "Carolina Wren", 1, 0, 1, 9000),
      ],
      JUL_18_NOON,
    );
    // Distinct species today is 2; activity volume is 3+7.
    expect(stats.today).toBe(2);
    expect(stats.detectionsToday).toBe(10);
  });
  it("sums detectionsWeek across every species with activity this week", () => {
    const stats = archiveStats(
      [
        entry("A", "American Robin", 10, 3, 5, 5000),
        entry("B", "Blue Jay", 8, 7, 8, 2000),
        entry("C", "Carolina Wren", 1, 0, 1, 9000),
        entry("D", "Downy Woodpecker", 4, 0, 0, 1000),
      ],
      JUL_18_NOON,
    );
    // Distinct species this week is 3; volume is 5+8+1 (woodpecker at 0).
    expect(stats.week).toBe(3);
    expect(stats.detectionsWeek).toBe(14);
  });
  it("takes the EARLIEST first_ts as since, wherever it sits in the list", () => {
    const stats = archiveStats(
      [
        entry("A", "American Robin", 1, 0, 0, 5000),
        entry("B", "Blue Jay", 1, 0, 0, 2000),
        entry("C", "Carolina Wren", 1, 0, 0, 9000),
      ],
      JUL_18_NOON,
    );
    expect(stats.since).toBe(2000);
  });
  it("counts days on the air inclusively -- first day through today", () => {
    const roster = (first_ts: number) => [
      entry("A", "American Robin", 1, 0, 0, first_ts),
    ];
    // Same local day: day one reads "1 day", not zero.
    expect(archiveStats(roster(JUL_18_NOON - 3600), JUL_18_NOON).days).toBe(1);
    // Four days later (plus an odd hour): five calendar days inclusive.
    expect(
      archiveStats(roster(JUL_18_NOON), JUL_18_NOON + 4 * 86400 + 3600).days,
    ).toBe(5);
  });
  it("floors the day count at 1 when since lands past the mount-time now", () => {
    // A tab open past midnight when the first-ever bird arrives: the mount
    // clock says yesterday, the record says today. Day one, not day zero.
    expect(
      archiveStats(
        [entry("A", "American Robin", 1, 0, 0, JUL_18_NOON + 86400)],
        JUL_18_NOON,
      ).days,
    ).toBe(1);
  });
  it("skips the day count when now is unknown, keeping since", () => {
    const stats = archiveStats(
      [entry("A", "American Robin", 1, 0, 0, 5000)],
      null,
    );
    expect(stats.since).toBe(5000);
    expect(stats.days).toBeNull();
    expect(stats.visitsPerWeek).toBeNull();
    expect(stats.visitsPerMonth).toBeNull();
  });
  it("rates visits per week and per month over the days-on-air span", () => {
    // 70 visits across exactly 7 inclusive calendar days -> 70/wk, 300/mo.
    const first = JUL_18_NOON;
    const now = JUL_18_NOON + 6 * 86400;
    const stats = archiveStats(
      [entry("A", "American Robin", 70, 1, 5, first)],
      now,
    );
    expect(stats.days).toBe(7);
    expect(stats.visitsPerWeek).toBe(70);
    expect(stats.visitsPerMonth).toBe(300);
  });
  it("scales pace with the archive span, not a rolling window", () => {
    // Same 70 visits over 14 days: half the weekly pace.
    const first = JUL_18_NOON;
    const now = JUL_18_NOON + 13 * 86400;
    const stats = archiveStats(
      [entry("A", "American Robin", 70, 0, 0, first)],
      now,
    );
    expect(stats.days).toBe(14);
    expect(stats.visitsPerWeek).toBe(35);
    expect(stats.visitsPerMonth).toBe(150);
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
  it("separates thousands in a four-digit gap (#558)", () => {
    const far = [entry("a", "A Bird", 3003), entry("b", "B Bird", 1)];
    expect(rivalLine(standingFor(far, "b", (e) => e.visits)!, "quiet")).toBe(
      "3,002 visits behind the A Bird",
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
  it("normalizes the stored histogram to the busiest hour and carries the count", () => {
    const hours = Array.from({ length: 24 }, () => 0);
    hours[7] = 20;
    hours[8] = 10;
    const cells = rhythmStrip({
      hours,
      peak_window: { start_hour: 7, end_hour: 10 },
    })!;
    expect(cells[7]).toEqual({ frac: 1, count: 20, tier: "peak" });
    expect(cells[8]).toEqual({ frac: 0.5, count: 10, tier: "peak" });
    expect(cells[12]).toEqual({ frac: 0, count: 0, tier: "rest" });
    expect(cells[6].tier).toBe("rest");
  });
  it("wraps a midnight-spanning peak -- an owl's peak is one stretch", () => {
    const hours = Array.from({ length: 24 }, () => 1);
    const cells = rhythmStrip({
      hours,
      peak_window: { start_hour: 23, end_hour: 2 },
    })!;
    expect(cells[23].tier).toBe("peak");
    expect(cells[0].tier).toBe("peak");
    expect(cells[1].tier).toBe("peak");
    expect(cells[2].tier).not.toBe("peak");
  });
  it("lights the three busiest non-peak hours as the second tier, non-contiguous (#558)", () => {
    const hours = Array.from({ length: 24 }, () => 2);
    hours[12] = 40; // peak noon-3
    hours[13] = 38;
    hours[14] = 35;
    hours[11] = 30; // the shoulders...
    hours[15] = 28;
    hours[6] = 25; // ...and a dawn burst far from the peak
    hours[16] = 24; // fourth-busiest outside: cut by the cap of three
    const cells = rhythmStrip({
      hours,
      peak_window: { start_hour: 12, end_hour: 15 },
    })!;
    const second = cells.flatMap((c, h) => (c.tier === "second" ? [h] : []));
    expect(second).toEqual([6, 11, 15]);
    expect(cells[16].tier).toBe("rest");
  });
  it("breaks second-tier ties toward the earlier hour", () => {
    const hours = Array.from({ length: 24 }, () => 0);
    hours[9] = 10; // peak 9-12
    for (const h of [5, 7, 14, 20]) hours[h] = 6; // four tied candidates
    const cells = rhythmStrip({
      hours,
      peak_window: { start_hour: 9, end_hour: 12 },
    })!;
    const second = cells.flatMap((c, h) => (c.tier === "second" ? [h] : []));
    expect(second).toEqual([5, 7, 14]);
  });
  it("never lights a flat tail: second tier needs a quarter of the busiest hour", () => {
    const hours = Array.from({ length: 24 }, () => 2);
    hours[7] = 100;
    const cells = rhythmStrip({
      hours,
      peak_window: { start_hour: 7, end_hour: 10 },
    })!;
    expect(cells.some((c) => c.tier === "second")).toBe(false);
    // Exactly a quarter qualifies; just under does not.
    hours[18] = 25;
    hours[19] = 24;
    const again = rhythmStrip({
      hours,
      peak_window: { start_hour: 7, end_hour: 10 },
    })!;
    expect(again[18].tier).toBe("second");
    expect(again[19].tier).toBe("rest");
  });
  it("never demotes a peak hour, however quiet it is", () => {
    const hours = Array.from({ length: 24 }, () => 0);
    hours[7] = 50;
    hours[8] = 0; // a hollow inside the stored window stays peak
    hours[9] = 1;
    hours[20] = 50; // busier than most of the window, but outside it
    const cells = rhythmStrip({
      hours,
      peak_window: { start_hour: 7, end_hour: 10 },
    })!;
    expect(cells[8].tier).toBe("peak");
    expect(cells[20].tier).toBe("second");
  });
  it("keeps all-zero hours honest flat rest cells, never NaN", () => {
    const cells = rhythmStrip({ hours: Array.from({ length: 24 }, () => 0) })!;
    expect(cells.every((c) => c.frac === 0 && c.count === 0)).toBe(true);
    expect(cells.every((c) => c.tier === "rest")).toBe(true);
  });
  it("returns null without a usable histogram -- the reserved placeholder", () => {
    expect(rhythmStrip(null)).toBeNull();
    expect(rhythmStrip({})).toBeNull();
    expect(rhythmStrip({ hours: [1, 2, 3] })).toBeNull();
  });
});

describe("rhythmCellExplain", () => {
  const cell = (count: number) => ({ frac: 0.5, count, tier: "rest" as const });
  it("names the hour span in the prose's clock and the count", () => {
    expect(rhythmCellExplain(6, cell(312))).toBe("6am–7am · 312 visits");
    expect(rhythmCellExplain(13, cell(2))).toBe("1pm–2pm · 2 visits");
  });
  it("wraps noon and midnight the way the strip's axis does", () => {
    expect(rhythmCellExplain(11, cell(5))).toBe("11am–12pm · 5 visits");
    expect(rhythmCellExplain(23, cell(5))).toBe("11pm–12am · 5 visits");
    expect(rhythmCellExplain(0, cell(5))).toBe("12am–1am · 5 visits");
  });
  it("speaks singular at one and 'no visits' at zero", () => {
    expect(rhythmCellExplain(6, cell(1))).toBe("6am–7am · 1 visit");
    expect(rhythmCellExplain(6, cell(0))).toBe("6am–7am · no visits");
  });
  it("separates thousands -- the house rule for every count (#558)", () => {
    expect(rhythmCellExplain(6, cell(1234))).toBe("6am–7am · 1,234 visits");
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

describe("weatherChipExplain", () => {
  it("reads a positive effect as 'more often' against the baseline", () => {
    expect(weatherChipExplain({ label: "mild", pct: 59, thin: false })).toBe(
      "Seen 59% more often in mild weather than its overall average.",
    );
  });
  it("reads a negative effect as 'less often'", () => {
    expect(weatherChipExplain({ label: "warm", pct: -18, thin: false })).toBe(
      "Seen 18% less often in warm weather than its overall average.",
    );
  });
  it("softens a −100% finding to 'almost never', not an unhedged 'never'", () => {
    expect(weatherChipExplain({ label: "clear", pct: -100, thin: false })).toBe(
      "Almost never seen in clear weather.",
    );
  });
  it("fixes 'rain' to read grammatically as 'rainy weather'", () => {
    expect(weatherChipExplain({ label: "rain", pct: 24, thin: false })).toBe(
      "Seen 24% more often in rainy weather than its overall average.",
    );
  });
  it("appends the thin-data caveat the dashed border signals", () => {
    expect(weatherChipExplain({ label: "warm", pct: 18, thin: true })).toBe(
      "Seen 18% more often in warm weather than its overall average. Based on thin data — few visits.",
    );
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

describe("rareReturns (#549)", () => {
  const DAY = 86400;
  const now = 1_000 * DAY + 12 * 3600; // noon, some day
  const since = now - 100 * DAY; // a mature archive
  // A rare bird back this morning: 3 visits ever, first heard a month ago.
  const rare = (sci: string, common: string, visits: number, lastTs: number) =>
    ({
      ...entry(sci, common, visits, 0, 1, now - 30 * DAY),
      last_ts: lastTs,
    }) as RosterEntry;

  it("features a seldom-heard species that came back inside the window", () => {
    const hit = rare("pw", "Pileated Woodpecker", 3, now - 3600);
    expect(rareReturns([hit], now, since).map((e) => e.species_sci)).toEqual([
      "pw",
    ]);
  });
  it("the visit cap is inclusive at five", () => {
    const five = rare("five", "Five Timer", 5, now - 3600);
    const six = rare("six", "Six Timer", 6, now - 3600);
    expect(rareReturns([five, six], now, since).map((e) => e.species_sci)).toEqual(
      ["five"],
    );
  });
  it("a return older than the window is no longer news", () => {
    const stale = rare("stale", "Stale", 2, now - RARE_RETURN_WINDOW_S - 1);
    const edge = rare("edge", "Edge", 2, now - RARE_RETURN_WINDOW_S);
    expect(rareReturns([stale, edge], now, since).map((e) => e.species_sci)).toEqual(
      ["edge"],
    );
  });
  it("hands lifers and week-old newcomers to New Arrivals", () => {
    const newcomer = {
      ...entry("new", "Newcomer", 2, 1, 2, now - 3 * DAY),
      last_ts: now - 3600,
    } as RosterEntry;
    // Exactly a week old is still New Arrivals' (its 7d cut is inclusive).
    const boundary = {
      ...entry("bdry", "Boundary", 2, 1, 2, now - ARRIVALS_WEEK_S),
      last_ts: now - 3600,
    } as RosterEntry;
    const settled = {
      ...entry("old", "Settled", 2, 1, 2, now - ARRIVALS_WEEK_S - 1),
      last_ts: now - 3600,
    } as RosterEntry;
    expect(
      rareReturns([newcomer, boundary, settled], now, since).map(
        (e) => e.species_sci,
      ),
    ).toEqual(["old"]);
  });
  it("says nothing on a young archive, or with no since at all", () => {
    const hit = rare("pw", "Pileated Woodpecker", 3, now - 3600);
    expect(rareReturns([hit], now, now - 13 * DAY)).toEqual([]);
    expect(rareReturns([hit], now, null)).toEqual([]);
    // Fourteen days exactly: mature.
    expect(rareReturns([hit], now, now - RARE_RETURN_MATURITY_S)).toHaveLength(1);
  });
  it("ignores an entry with no last visit on record", () => {
    const bare = entry("bare", "Bare", 2, 0, 0, now - 30 * DAY) as RosterEntry;
    expect(rareReturns([bare], now, since)).toEqual([]);
  });
  it("orders rarest first, then the most recent return, then name", () => {
    const yard = [
      rare("b4", "B Four", 4, now - 100),
      rare("a2late", "Zed Two", 2, now - 5000),
      rare("a2early", "Alpha Two", 2, now - 5000),
      rare("c2", "Mid Two", 2, now - 100),
    ];
    expect(rareReturns(yard, now, since).map((e) => e.species_sci)).toEqual([
      "c2", // two visits, back most recently
      "a2early", // two visits, same moment: by name
      "a2late",
      "b4",
    ]);
  });
  it("constants say what they claim", () => {
    expect(RARE_RETURN_MAX_VISITS).toBe(5);
    expect(RARE_RETURN_WINDOW_S).toBe(ARRIVALS_24H_S);
    expect(RARE_RETURN_MATURITY_S).toBe(14 * DAY);
  });
});

describe("rare-return notice helpers (#549)", () => {
  it("the id is stable within a local day and differs across days", () => {
    const morning = Date.UTC(2026, 8, 6, 14, 0, 0) / 1000; // local day A
    const sameDay = morning + 2 * 3600;
    const d = new Date(morning * 1000);
    d.setDate(d.getDate() + 1);
    const nextDay = Math.floor(d.getTime() / 1000);
    const a = rareReturnNoticeId("Strix varia", morning);
    expect(rareReturnNoticeId("Strix varia", sameDay)).toBe(a);
    expect(rareReturnNoticeId("Strix varia", nextDay)).not.toBe(a);
    expect(a.startsWith("rare_return:Strix varia:")).toBe(true);
  });
  it("ordinal copy reads naturally", () => {
    expect(rareReturnBody(2)).toBe("only its 2nd visit ever");
    expect(rareReturnBody(3)).toBe("only its 3rd visit ever");
    expect(rareReturnBody(4)).toBe("only its 4th visit ever");
    expect(rareReturnBody(5)).toBe("only its 5th visit ever");
    expect(ordinal(1)).toBe("1st");
    expect(ordinal(11)).toBe("11th");
    expect(ordinal(12)).toBe("12th");
    expect(ordinal(13)).toBe("13th");
    expect(ordinal(21)).toBe("21st");
    expect(ordinal(22)).toBe("22nd");
    expect(ordinal(103)).toBe("103rd");
  });
  it("builds the strip notice: the bird is the title, the stamp is the kind", () => {
    const e = {
      ...entry("Dryocopus pileatus", "Pileated Woodpecker", 3, 1, 1, 0),
      last_ts: 1_700_000_000,
      image_file: "Dryocopus_pileatus.jpg",
    } as RosterEntry;
    expect(rareReturnNotice(e)).toEqual({
      id: rareReturnNoticeId("Dryocopus pileatus", 1_700_000_000),
      kind: "rare_return",
      title: "Pileated Woodpecker",
      body: "only its 3rd visit ever",
      href: "/aviary/Dryocopus%20pileatus",
      species_sci: "Dryocopus pileatus",
      species_common: "Pileated Woodpecker",
      portrait_file: "Dryocopus_pileatus.jpg",
    });
  });
});

describe("new best recording (#447 Phase 2)", () => {
  const champ = (
    sci: string,
    common: string,
    clip: string | null,
    confidence: number | null = 0.9,
  ) =>
    ({
      ...entry(sci, common, 10, 0, 1, 0),
      best_clip: clip,
      best_confidence: confidence,
      best_ts: 1000,
    }) as RosterEntry;
  const hydrated = [
    champ("Cardinalis cardinalis", "Northern Cardinal", "amcrest/100-NC.wav"),
    champ("Turdus migratorius", "American Robin", "amcrest/200-AR.wav"),
    champ("Strix varia", "Barred Owl", null, null), // no clip yet
  ];

  // A showcase: slot 2 and 3 takes beside the champion (#551).
  const show = (e: RosterEntry, s2: string | null, s3: string | null) =>
    ({
      ...e,
      showcase: [
        ...(s2 ? [{ slot: 2, clip: s2, confidence: 0.88, vocal_fill: 0.5, ts: 1 }] : []),
        ...(s3 ? [{ slot: 3, clip: s3, confidence: 0.81, vocal_fill: 0.4, ts: 2 }] : []),
      ],
    }) as RosterEntry;
  const change = (slot: number, sci: string) =>
    ({ entry: expect.objectContaining({ species_sci: sci }), slot }) as unknown;

  it("championPaths carries the three slot paths for species with any clip", () => {
    const withShow = [
      show(hydrated[0], "amcrest/150-NC.wav", null),
      hydrated[1],
      hydrated[2],
    ];
    expect(championPaths(withShow)).toEqual({
      "Cardinalis cardinalis": ["amcrest/100-NC.wav", "amcrest/150-NC.wav", ""],
      "Turdus migratorius": ["amcrest/200-AR.wav", "", ""],
    });
  });

  it("first paint never floods: a snapshot diffed against itself is silent", () => {
    expect(newBestChanges(championPaths(hydrated), hydrated)).toEqual([]);
  });

  it("a new champion path fires; a same-path rewrite stays silent", () => {
    const seed = championPaths(hydrated);
    const later = [
      champ("Cardinalis cardinalis", "Northern Cardinal", "amcrest/900-NC.wav", 0.97),
      // Same path, better number: the in-place rewrite case, quiet in v1.
      champ("Turdus migratorius", "American Robin", "amcrest/200-AR.wav", 0.99),
      champ("Strix varia", "Barred Owl", null, null),
    ];
    expect(newBestChanges(seed, later)).toMatchObject([
      change(1, "Cardinalis cardinalis"),
    ]);
    expect(newBestChanges(seed, later)[0]).toMatchObject({
      clip: "amcrest/900-NC.wav",
      confidence: 0.97,
    });
  });

  it("a species' first clip ever is first contact, not a new best; a lost clip is a gap", () => {
    const seed = championPaths(hydrated);
    const later = [
      champ("Cardinalis cardinalis", "Northern Cardinal", null, null), // pruned
      champ("Turdus migratorius", "American Robin", "amcrest/200-AR.wav"),
      champ("Strix varia", "Barred Owl", "amcrest/300-BO.wav", 0.8), // first clip
      champ("Sitta carolinensis", "White-breasted Nuthatch", "x.wav"), // lifer
    ];
    expect(newBestChanges(seed, later)).toEqual([]);
  });

  it("a second or third call changing fires too, with its own slot and confidence", () => {
    const seeded = [
      show(hydrated[0], "amcrest/150-NC.wav", "amcrest/160-NC.wav"),
      show(hydrated[1], "amcrest/250-AR.wav", null),
    ];
    const seed = championPaths(seeded);
    const later = [
      show(hydrated[0], "amcrest/150-NC.wav", "amcrest/960-NC.wav"), // slot 3 moved
      show(hydrated[1], "amcrest/950-AR.wav", null), // slot 2 moved
    ];
    const got = newBestChanges(seed, later);
    expect(got).toMatchObject([
      change(2, "Turdus migratorius"),
      change(3, "Cardinalis cardinalis"),
    ]);
    expect(got[0]).toMatchObject({ clip: "amcrest/950-AR.wav", confidence: 0.88 });
    expect(got[1]).toMatchObject({ clip: "amcrest/960-NC.wav", confidence: 0.81 });
  });

  it("a slot filling for the first time is the showcase growing, not news", () => {
    const seed = championPaths([show(hydrated[0], null, null)]);
    expect(
      newBestChanges(seed, [show(hydrated[0], "amcrest/150-NC.wav", null)]),
    ).toEqual([]);
  });

  it("a new champion that bumps the old one to slot 2 is ONE piece of news, the champion's", () => {
    const seed = championPaths([show(hydrated[0], "amcrest/050-NC.wav", null)]);
    const later = [
      show(
        champ("Cardinalis cardinalis", "Northern Cardinal", "amcrest/900-NC.wav", 0.97),
        "amcrest/100-NC.wav", // yesterday's champion, demoted
        null,
      ),
    ];
    expect(newBestChanges(seed, later)).toMatchObject([
      change(1, "Cardinalis cardinalis"),
    ]);
  });

  it("orders several changes by name within one snapshot", () => {
    const seed = championPaths(hydrated);
    const later = [
      champ("Turdus migratorius", "American Robin", "amcrest/901-AR.wav"),
      champ("Cardinalis cardinalis", "Northern Cardinal", "amcrest/900-NC.wav"),
    ];
    expect(
      newBestChanges(seed, later).map((c) => c.entry.species_common),
    ).toEqual(["American Robin", "Northern Cardinal"]);
  });

  it("builds the strip notice keyed by species + clip path, carrying the clip", () => {
    const e = {
      ...champ("Cardinalis cardinalis", "Northern Cardinal", "amcrest/900-NC.wav", 0.973),
      image_file: "Cardinalis_cardinalis.jpg",
    } as RosterEntry;
    expect(
      newBestNotice({ entry: e, slot: 1, clip: "amcrest/900-NC.wav", confidence: 0.973 }),
    ).toEqual({
      id: newBestNoticeId("Cardinalis cardinalis", "amcrest/900-NC.wav"),
      kind: "new_best",
      title: "We've captured a new best recording of the Northern Cardinal!",
      body: "confidence 0.97 · press play to hear it",
      href: "/aviary/Cardinalis%20cardinalis",
      species_sci: "Cardinalis cardinalis",
      species_common: "Northern Cardinal",
      portrait_file: "Cardinalis_cardinalis.jpg",
      clip: "amcrest/900-NC.wav",
    });
    const owl = champ("Strix varia", "Barred Owl", "b.wav", null);
    expect(newBestNotice({ entry: owl, slot: 2, clip: "b2.wav", confidence: null })).toMatchObject({
      title: "We've captured a new call from the Barred Owl!",
      body: "a new second call for its showcase · press play to hear it",
      clip: "b2.wav",
    });
    expect(newBestNotice({ entry: owl, slot: 3, clip: "b3.wav", confidence: 0.8 }).body).toBe(
      "a new third call for its showcase · confidence 0.80 · press play to hear it",
    );
  });

  it("merging keeps one notice per species, newest champion winning, arrival order kept", () => {
    const nb = (sci: string, common: string, clip: string) =>
      newBestNotice({ entry: champ(sci, common, clip), slot: 1, clip, confidence: 0.9 });
    const a1 = nb("A", "Alpha", "a1.wav");
    const b1 = nb("B", "Beta", "b1.wav");
    const a2 = nb("A", "Alpha", "a2.wav");
    const queue = mergeNewBestNotices(mergeNewBestNotices([], [a1, b1]), [a2]);
    expect(queue.map((n) => n.id)).toEqual([b1.id, a2.id]);
    // A later champion is a new id: the dismissed a1 never blocks a2.
    expect(a2.id).not.toBe(a1.id);
    // Nothing incoming: the same array back, no churn for the strip hook.
    expect(mergeNewBestNotices(queue, [])).toBe(queue);
  });
});

describe("desk-check samples: ?notice=<kinds> (#447)", () => {
  const now = 1_700_000_000;
  const yard = [
    { ...entry("Zz", "Zebra Finch", 40, 0, 1, 0), image_file: null },
    { ...entry("Cc", "Carolina Wren", 300, 0, 1, 0), image_file: "Cc.jpg", best_clip: "c.wav", best_confidence: 0.91 },
    { ...entry("Aa", "Acadian Flycatcher", 1, 0, 0, 0), image_file: null },
  ] as RosterEntry[];

  it("no flag, no samples", () => {
    expect(demoNoticesFor(null, yard, now)).toEqual([]);
    expect(demoNoticesFor("", yard, now)).toEqual([]);
  });
  it("dresses the first species BY NAME that has a portrait", () => {
    const [n] = demoNoticesFor("rare_return", yard, now);
    expect(n).toMatchObject({
      id: "demo:rare_return",
      kind: "rare_return",
      title: "Carolina Wren",
      body: "only its 3rd visit ever",
      species_sci: "Cc",
      portrait_file: "Cc.jpg",
    });
  });
  it("falls back to the first by name when no portrait exists, and to nothing on an empty roster", () => {
    const bare = yard.map((e) => ({ ...e, image_file: null }));
    expect(demoNoticesFor("new_best", bare, now)[0]?.species_common).toBe(
      "Acadian Flycatcher",
    );
    expect(demoNoticesFor("new_best", [], now)).toEqual([]);
    // The bare shell needs no species: the profile page can still show it.
    expect(demoNoticesFor("demo", [], now).map((n) => n.kind)).toEqual(["demo"]);
  });
  it("new_best borrows the real champion when there is one, invents one otherwise", () => {
    expect(sampleNotice("new_best", yard[1], now)).toMatchObject({
      id: "demo:new_best",
      kind: "new_best",
      title: "We've captured a new best recording of the Carolina Wren!",
      body: "confidence 0.91 · press play to hear it",
      clip: "c.wav",
    });
    expect(sampleNotice("new_best", yard[0], now)?.body).toBe(
      "confidence 0.97 · press play to hear it",
    );
    expect(sampleNotice("bogus", yard[0], now)).toBeNull();
    expect(sampleNotice("rare_return", null, now)).toBeNull();
  });
  it("`all` is every kind in rank order, and a list dedupes", () => {
    expect(demoNoticesFor("all", yard, now).map((n) => n.kind)).toEqual([
      ...DEMO_NOTICE_KINDS,
    ]);
    expect(
      demoNoticesFor("new_best, new_best,demo", yard, now).map((n) => n.id),
    ).toEqual(["demo:new_best", "demo:announcement-strip"]);
  });
  it("sample ids never collide with a real notice's dismiss key", () => {
    const real = newBestNotice({ entry: yard[1], slot: 1, clip: "c.wav", confidence: 0.91 });
    const sample = sampleNotice("new_best", yard[1], now);
    expect(sample?.id).not.toBe(real.id);
  });
  it("undismissNotice forgets a dismissal", () => {
    const mem = new Map<string, string>();
    const store = {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => {
        mem.set(k, v);
      },
      removeItem: (k: string) => {
        mem.delete(k);
      },
    };
    dismissNotice(store, "demo:new_best");
    expect(isNoticeDismissed(store, "demo:new_best")).toBe(true);
    undismissNotice(store, "demo:new_best");
    expect(isNoticeDismissed(store, "demo:new_best")).toBe(false);
  });
});

describe("mastheadSub (#304, the station masthead)", () => {
  it("home wears the station-less line", () => {
    expect(mastheadSub()).toBe("earl with the ears");
    expect(mastheadSub(null)).toBe("earl with the ears");
  });
  it("a station says whose yard it is", () => {
    expect(mastheadSub("Karens Yard")).toBe("Karens Yard");
    expect(mastheadSub("Karen's Yard")).toBe("Karen's Yard");
  });
  it("blank collapses to home, never a dangling separator", () => {
    expect(mastheadSub("   ")).toBe("earl with the ears");
  });
});

describe("aviaryTitle (station H1)", () => {
  it("defaults to The Aviary", () => {
    expect(aviaryTitle()).toBe("The Aviary");
    expect(aviaryTitle(null)).toBe("The Aviary");
    expect(aviaryTitle("  ")).toBe("The Aviary");
  });
  it("uses a configured title", () => {
    expect(aviaryTitle("Karen's Aviary")).toBe("Karen's Aviary");
  });
});

describe("announcement strip helpers (#447 Phase 1)", () => {
  it("builds stable new-best and dismiss keys", () => {
    expect(newBestNoticeId("Cardinalis cardinalis", "house/1-Cardinal.wav")).toBe(
      "new_best:Cardinalis cardinalis:house/1-Cardinal.wav",
    );
    expect(noticeDismissKey("new_best:x:y")).toBe(
      "aviary.notice.dismissed.new_best:x:y",
    );
  });

  it("pickVisibleNotice prefers new_best over fun_fact and demo", () => {
    const fun = {
      id: "f1",
      kind: "fun_fact" as const,
      title: "fun",
    };
    const best = {
      id: "b1",
      kind: "new_best" as const,
      title: "best",
    };
    const demo = demoNotice();
    expect(pickVisibleNotice([fun, best, demo], () => false)?.id).toBe("b1");
    expect(pickVisibleNotice([fun, demo], () => false)?.kind).toBe("demo");
    expect(pickVisibleNotice([fun], () => false)?.kind).toBe("fun_fact");
  });

  it("rare_return outranks new_best, and both outrank fun_fact (#549)", () => {
    const fun = { id: "f1", kind: "fun_fact" as const, title: "fun" };
    const best = { id: "b1", kind: "new_best" as const, title: "best" };
    const rare = { id: "r1", kind: "rare_return" as const, title: "rare" };
    expect(pickVisibleNotice([fun, best, rare], () => false)?.id).toBe("r1");
    expect(pickVisibleNotice([fun, best], () => false)?.id).toBe("b1");
    // Dismissing the visible one reveals the next by rank.
    expect(pickVisibleNotice([fun, best, rare], (id) => id === "r1")?.id).toBe(
      "b1",
    );
  });

  it("pickVisibleNotice skips dismissed and returns null when empty", () => {
    const best = {
      id: "b1",
      kind: "new_best" as const,
      title: "best",
    };
    expect(pickVisibleNotice([best], (id) => id === "b1")).toBeNull();
    expect(pickVisibleNotice([], () => false)).toBeNull();
  });

  it("dismiss storage marks a notice read", () => {
    const mem = new Map<string, string>();
    const store = {
      getItem: (k: string) => mem.get(k) ?? null,
      setItem: (k: string, v: string) => {
        mem.set(k, v);
      },
    };
    expect(isNoticeDismissed(store, "b1")).toBe(false);
    dismissNotice(store, "b1");
    expect(isNoticeDismissed(store, "b1")).toBe(true);
  });
});
