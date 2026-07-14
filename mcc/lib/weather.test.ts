import { describe, expect, it } from "vitest";
import {
  ARCHIVE_MAX_SPAN_S,
  FORECAST_SHADE_CEIL,
  FUTURE_S,
  RAIN_SHADE_FLOOR,
  STATION_SPAN_S,
  clampWindow,
  mergePoints,
  parseRange,
  precipFill,
  precipShade,
  tempMarks,
  windowEdgeLabel,
  BLEND_HORIZON_S,
  DEW_TREND_EPS_F,
  blendForecast,
  HUMIDITY_TREND_EPS_PCT,
  TEMP_TREND_EPS_F,
  TREND_SPAN_S,
  CurrentWeather,
  WINDY_GUST_MPH,
  WINDY_SUSTAINED_MPH,
  WeatherPoint,
  PAST_S,
  conditionIcon,
  STATION_FUTURE_S,
  ageText,
  compass,
  dayTicks,
  linePath,
  nearestPoint,
  nightBands,
  parseCurrent,
  parsePoints,
  parseReport,
  parseStatus,
  pressureRange,
  pressureTrend,
  seriesCeil,
  seriesTrend,
  snowSeason,
  tempRange,
  timeTicks,
  trendSeries,
  windCeil,
} from "./weather";

const pt = (ts: number, over: Partial<WeatherPoint> = {}): WeatherPoint => ({
  ts,
  temp_f: 70,
  wind_mph: 5,
  wind_gust_mph: null,
  condition: "Clear",
  humidity_pct: null,
  dew_point_f: null,
  pressure_rel_inhg: null,
  rain_rate_inhr: null,
  rain_day_in: null,
  solar_wm2: null,
  uv_index: null,
  pop: null,
  snow_3h_in: null,
  ...over,
});

describe("parseCurrent", () => {
  it("accepts a full report", () => {
    const got = parseCurrent(
      JSON.stringify({
        ts: 1752148800,
        temp_f: 78.3,
        feels_like_f: 79.1,
        humidity_pct: 62,
        wind_mph: 8.5,
        wind_gust_mph: 17.2,
        wind_deg: 240,
        condition: "Clouds",
        description: "broken clouds",
        sunrise: 1752116400,
        sunset: 1752170700,
      }),
    );
    expect(got?.temp_f).toBe(78.3);
    expect(got?.description).toBe("broken clouds");
    expect(got?.wind_gust_mph).toBe(17.2);
  });
  it("reads the station fields (issue #51)", () => {
    const got = parseCurrent(
      JSON.stringify({
        ts: 1752300000,
        temp_f: 78.8,
        dew_point_f: 56.3,
        uv_index: 7,
        solar_wm2: 612.4,
        raining: 1,
        rain_rate_inhr: 0.12,
        rain_day_in: 0.25,
        pressure_rel_inhg: 29.24,
        indoor_temp_f: 72.9,
        indoor_humidity_pct: 55,
        station_battery: 5,
        station_voltage: 3.2,
        station_signal: 4,
      }),
    );
    expect(got?.dew_point_f).toBe(56.3);
    expect(got?.raining).toBe(1);
    expect(got?.rain_day_in).toBe(0.25);
    expect(got?.pressure_rel_inhg).toBe(29.24);
    expect(got?.indoor_temp_f).toBe(72.9);
    expect(got?.station_signal).toBe(4);
  });
  it("nulls missing or mistyped fields but keeps the report", () => {
    const got = parseCurrent(JSON.stringify({ ts: 5, temp_f: "warm" }));
    expect(got?.ts).toBe(5);
    expect(got?.temp_f).toBeNull();
    expect(got?.condition).toBeNull();
    // a pre-#51 payload: every station field null, never undefined
    expect(got?.dew_point_f).toBeNull();
    expect(got?.station_battery).toBeNull();
  });
  it("rejects a report with no ts, junk, and non-JSON", () => {
    expect(parseCurrent(JSON.stringify({ temp_f: 70 }))).toBeNull();
    expect(parseCurrent("{not json")).toBeNull();
    expect(parseCurrent("null")).toBeNull();
  });
});

describe("parseReport", () => {
  it("accepts a segment", () => {
    const got = parseReport(
      JSON.stringify({
        ts: 1752148800,
        text: "WELL, folks, what a gorgeous afternoon!\n\nAnd tomorrow? Even better.",
        model: "gemma3:12b",
      }),
    );
    expect(got?.ts).toBe(1752148800);
    expect(got?.text).toContain("gorgeous afternoon");
    expect(got?.model).toBe("gemma3:12b");
  });
  it("tolerates a missing model", () => {
    const got = parseReport(JSON.stringify({ ts: 5, text: "Hot out there." }));
    expect(got?.model).toBeNull();
  });
  it("rejects no ts, empty or missing text, junk, and non-JSON", () => {
    expect(parseReport(JSON.stringify({ text: "no clock" }))).toBeNull();
    expect(parseReport(JSON.stringify({ ts: 5, text: "   " }))).toBeNull();
    expect(parseReport(JSON.stringify({ ts: 5 }))).toBeNull();
    expect(parseReport(JSON.stringify({ ts: 5, text: 42 }))).toBeNull();
    expect(parseReport("{not json")).toBeNull();
    expect(parseReport("null")).toBeNull();
  });
});

describe("precipShade", () => {
  it("floors the faint end so 'unlikely' never reads as 'no data'", () => {
    expect(precipShade(0, 1, RAIN_SHADE_FLOOR)).toBe(RAIN_SHADE_FLOOR);
    expect(precipShade(0.1, 1, RAIN_SHADE_FLOOR)).toBeGreaterThan(
      RAIN_SHADE_FLOOR,
    );
  });
  it("tops out at the token's full strength", () => {
    expect(precipShade(1, 1, RAIN_SHADE_FLOOR)).toBe(1);
  });
  it("respects a ceiling below full -- a forecast never shouts like a measurement", () => {
    expect(precipShade(1, 1, 0.3, FORECAST_SHADE_CEIL)).toBeCloseTo(
      FORECAST_SHADE_CEIL,
      10,
    );
  });
  it("ramps linearly between floor and ceiling", () => {
    expect(precipShade(0.5, 1, 0.4, 1)).toBeCloseTo(0.7, 10);
  });
  it("normalises against the max it is GIVEN, not a global one", () => {
    // The strip's three scales are rainMax / a fixed 100% / snowMax. Hand this
    // the wrong ceiling and every shade is wrong while still looking plausible.
    expect(precipShade(0.25, 0.25, 0.4)).toBe(1); // at rainMax -> full
    expect(precipShade(0.25, 1, 0.4)).toBeCloseTo(0.55, 10); // as a chance -> quiet
  });
  it("clamps a value past the max rather than overshooting the token", () => {
    expect(precipShade(9, 1, 0.4)).toBe(1);
  });
  it("clamps a negative value to the floor", () => {
    expect(precipShade(-5, 1, 0.4)).toBe(0.4);
  });
  it("yields the floor for a degenerate max instead of NaN or a divide by zero", () => {
    // An all-quiet strip draws faint bars, never invisible and never black.
    expect(precipShade(0, 0, 0.4)).toBe(0.4);
    expect(precipShade(1, Number.NaN, 0.4)).toBe(0.4);
    expect(precipShade(Number.NaN, 1, 0.4)).toBe(0.4);
  });
});

describe("precipFill", () => {
  it("mixes the token toward the panel in oklab", () => {
    expect(precipFill("var(--rain)", 1)).toBe(
      "color-mix(in oklab, var(--rain) 100%, var(--panel))",
    );
    expect(precipFill("var(--ink)", 0.3)).toBe(
      "color-mix(in oklab, var(--ink) 30%, var(--panel))",
    );
  });
  it("clamps out-of-range weights", () => {
    expect(precipFill("var(--rain)", 2)).toContain("100%");
    expect(precipFill("var(--rain)", -1)).toContain("0%");
  });
});

describe("tempMarks", () => {
  const H = 3600;
  /** A day of 3-hour forecast steps: cold at dawn, peak mid-afternoon. */
  const day = (d: number, low: number, high: number): WeatherPoint[] => {
    const shape = [low + 1, low, low + 4, high - 4, high, high - 2, low + 6, low + 3];
    return shape.map((t, i) => pt(d * 86_400 + i * 3 * H, { temp_f: t }));
  };

  it("marks one high and one low for a clean diurnal day", () => {
    const got = tempMarks(day(1, 50, 78));
    expect(got.map((m) => m.kind)).toEqual(["low", "high"]);
    expect(got.find((m) => m.kind === "high")!.temp_f).toBe(78);
    expect(got.find((m) => m.kind === "low")!.temp_f).toBe(50);
  });

  it("gives a valley that spans midnight exactly ONE low", () => {
    // THE case this design exists for. An evening cold front: the temperature
    // falls straight through midnight and bottoms at dawn. Bucketing by
    // calendar day would mark 23:00 (a bucket edge, still dropping) AND the
    // dawn bottom -- two labels, one valley.
    const falling = [70, 66, 62, 58, 54]; // 12:00 -> 24:00, still dropping
    const rising = [48, 52, 60, 68, 72]; // 03:00 -> 15:00 -- 48 is the bottom
    const pts = [
      ...falling.map((t, i) => pt(i * 3 * H, { temp_f: t })),
      ...rising.map((t, i) => pt((5 + i) * 3 * H, { temp_f: t })),
    ];
    const lows = tempMarks(pts).filter((m) => m.kind === "low");
    expect(lows).toHaveLength(1);
    expect(lows[0].temp_f).toBe(48); // the real bottom, not the midnight edge
  });

  it("never marks the endpoints -- the data running out is not a turning point", () => {
    // Monotonic rise then stop: the last point is the highest, but it is the
    // end of the series, not a peak.
    const pts = [50, 55, 60, 65].map((t, i) => pt(i * 3 * H, { temp_f: t }));
    expect(tempMarks(pts)).toEqual([]);
  });

  it("ignores the bridge point trendSeries prepends", () => {
    // `coming[0]` is the last OBSERVED point (the seam stitch). It is an
    // endpoint of this series and must not be labelled as a forecast peak.
    const pts = [
      pt(0, { temp_f: 99 }), // the bridge -- highest, and first
      pt(3 * H, { temp_f: 60 }),
      pt(6 * H, { temp_f: 70 }),
      pt(9 * H, { temp_f: 65 }),
    ];
    const got = tempMarks(pts);
    expect(got.map((m) => m.ts)).toEqual([3 * H, 6 * H]);
    expect(got.every((m) => m.temp_f !== 99)).toBe(true);
  });

  it("thins a shower's wiggle into the real peak", () => {
    // A 2F afternoon dip is technically a local minimum and visually nothing.
    const pts = [60, 74, 72, 78, 62].map((t, i) => pt(i * 3 * H, { temp_f: t }));
    const got = tempMarks(pts);
    expect(got.filter((m) => m.kind === "high")).toHaveLength(1);
    expect(got.find((m) => m.kind === "high")!.temp_f).toBe(78); // the real one
  });

  it("keeps genuinely separate days apart", () => {
    const got = tempMarks([...day(1, 50, 78), ...day(2, 52, 80)]);
    expect(got.filter((m) => m.kind === "high").map((m) => m.temp_f)).toEqual([
      78, 80,
    ]);
  });

  it("marks a plateau once, at its first sample", () => {
    const pts = [60, 70, 70, 70, 61].map((t, i) => pt(i * 3 * H, { temp_f: t }));
    const got = tempMarks(pts);
    expect(got).toHaveLength(1);
    expect(got[0]).toMatchObject({ kind: "high", ts: 3 * H, temp_f: 70 });
  });

  it("skips a plateau that runs off the end of the series", () => {
    const pts = [60, 70, 70].map((t, i) => pt(i * 3 * H, { temp_f: t }));
    expect(tempMarks(pts)).toEqual([]);
  });

  it("skips points with no temperature rather than crashing", () => {
    const pts = [
      pt(0, { temp_f: 60 }),
      pt(3 * H, { temp_f: null }),
      pt(6 * H, { temp_f: 78 }),
      pt(9 * H, { temp_f: 61 }),
    ];
    expect(tempMarks(pts)).toEqual([
      { ts: 6 * H, temp_f: 78, kind: "high" },
    ]);
  });

  it("is empty for a flat, empty, or one-point series", () => {
    expect(tempMarks([])).toEqual([]);
    expect(tempMarks([pt(0, { temp_f: 60 })])).toEqual([]);
    expect(
      tempMarks([60, 60, 60, 60].map((t, i) => pt(i * 3 * H, { temp_f: t }))),
    ).toEqual([]);
  });

  it("sorts an out-of-order series before reading its shape", () => {
    const pts = [
      pt(6 * H, { temp_f: 78 }),
      pt(0, { temp_f: 60 }),
      pt(9 * H, { temp_f: 61 }),
    ];
    expect(tempMarks(pts)).toEqual([{ ts: 6 * H, temp_f: 78, kind: "high" }]);
  });
});

describe("parseRange", () => {
  const now = 2_000_000_000;

  it("takes a sane range as given", () => {
    const week = 7 * 86400;
    expect(parseRange(String(now - week), String(now)))
      .toEqual({ from: now - week, to: now });
  });

  it("clamps an absurd span to the newest window, anchored at `to`", () => {
    // "from the epoch to now" is a typo, not a request for ten years.
    expect(parseRange("0", String(now)))
      .toEqual({ from: now - ARCHIVE_MAX_SPAN_S, to: now });
  });

  it("leaves a range at exactly the max span alone", () => {
    const from = now - ARCHIVE_MAX_SPAN_S;
    expect(parseRange(String(from), String(now))).toEqual({ from, to: now });
  });

  it("leaves an inverted range inverted -- it selects nothing, honestly", () => {
    expect(parseRange("900", "500")).toEqual({ from: 900, to: 500 });
  });

  it("rejects absent, blank, and non-numeric ends", () => {
    // Number(null) and Number("") are both 0, so these must be caught before
    // the arithmetic -- a missing `from` would otherwise read as the epoch.
    expect(parseRange(null, String(now))).toBeNull();
    expect(parseRange(String(now), null)).toBeNull();
    expect(parseRange("", String(now))).toBeNull();
    expect(parseRange("   ", String(now))).toBeNull();
    expect(parseRange("yesterday", String(now))).toBeNull();
    expect(parseRange(String(now), "NaN")).toBeNull();
    expect(parseRange("Infinity", String(now))).toBeNull();
  });

  it("truncates fractional seconds", () => {
    expect(parseRange("100.7", "200.9")).toEqual({ from: 100, to: 200 });
  });

  it("accepts a zero-length range", () => {
    expect(parseRange("100", "100")).toEqual({ from: 100, to: 100 });
  });
});

describe("parsePoints", () => {
  it("maps a points payload", () => {
    const got = parsePoints(
      JSON.stringify({
        points: [
          { ts: 100, temp_f: 71.5, wind_mph: 4, pressure_rel_inhg: 29.24,
            rain_rate_inhr: 0.12, solar_wm2: 610, uv_index: 6 },
        ],
      }),
    );
    expect(got).toEqual([
      pt(100, {
        temp_f: 71.5,
        wind_mph: 4,
        condition: null,
        pressure_rel_inhg: 29.24,
        rain_rate_inhr: 0.12,
        solar_wm2: 610,
        uv_index: 6,
      }),
    ]);
  });
  it("maps a forecast point's precip fields (issues #56/#65)", () => {
    const got = parsePoints(
      JSON.stringify({
        points: [
          { ts: 100, temp_f: 80, pop: 0.4, rain_rate_inhr: 0.1,
            snow_3h_in: 1.5 },
        ],
      }),
    );
    expect(got?.[0]?.pop).toBe(0.4);
    expect(got?.[0]?.rain_rate_inhr).toBe(0.1);
    expect(got?.[0]?.snow_3h_in).toBe(1.5);
  });
  it("leaves pop and snow null when absent (history, old payloads)", () => {
    const got = parsePoints(JSON.stringify({ points: [{ ts: 100 }] }));
    expect(got?.[0]?.pop).toBeNull();
    expect(got?.[0]?.snow_3h_in).toBeNull();
  });
  it("drops ts-less points, rejects payloads without a points array", () => {
    expect(
      parsePoints(JSON.stringify({ points: [{ temp_f: 70 }, { ts: 1 }] })),
    ).toHaveLength(1);
    expect(parsePoints(JSON.stringify({}))).toBeNull();
    expect(parsePoints("nope")).toBeNull();
  });
});

describe("parseStatus", () => {
  it("accepts the two presence states, whitespace-tolerant", () => {
    expect(parseStatus("online")).toBe("online");
    expect(parseStatus("offline")).toBe("offline");
    expect(parseStatus(" online\n")).toBe("online");
  });
  it("maps anything else to null (no presence info, never a fake state)", () => {
    expect(parseStatus("")).toBeNull();
    expect(parseStatus("ONLINE")).toBeNull();
    expect(parseStatus('"offline"')).toBeNull(); // JSON-quoted is not the contract
    expect(parseStatus("on coffee break")).toBeNull();
  });
});

describe("ageText", () => {
  const now = 1_000_000;
  it("buckets by coarse unit", () => {
    expect(ageText(now - 30, now)).toBe("just now");
    expect(ageText(now - 90, now)).toBe("1m ago");
    expect(ageText(now - 45 * 60, now)).toBe("45m ago");
    expect(ageText(now - 3 * 3600 - 40 * 60, now)).toBe("3h ago");
    expect(ageText(now - 50 * 3600, now)).toBe("2d ago");
  });
  it("clamps future timestamps to just now (clock skew)", () => {
    expect(ageText(now + 120, now)).toBe("just now");
  });
});

describe("trendSeries", () => {
  const now = 100_000;
  // The default live window, the shape every pre-#106 call site used: the
  // seam sits inside it and it moves with `now`.
  const liveFrom = now - PAST_S;
  const liveTo = now + FUTURE_S;
  it("clips history to the trailing window and forecast to the leading one", () => {
    const { observed, coming } = trendSeries(
      [pt(now - 90_000), pt(now - 500), pt(now + 5)], // too old / in / future
      [pt(now - 10), pt(now + 500), pt(now + 900_000)], // past / in / too far
      now,
      now - 86_400,
      now + 172_800,
    );
    expect(observed.map((p) => p.ts)).toEqual([now - 500]);
    expect(coming.map((p) => p.ts)).toEqual([now - 500, now + 500]); // bridge + 1
  });
  it("bridges the last observed point into the forecast so the lines meet", () => {
    const { coming } = trendSeries(
      [pt(now - 100)], [pt(now + 100)], now, liveFrom, liveTo,
    );
    expect(coming[0].ts).toBe(now - 100);
  });
  it("stands the forecast alone when nothing has been observed yet", () => {
    const { observed, coming } = trendSeries(
      [], [pt(now + 100)], now, liveFrom, liveTo,
    );
    expect(observed).toEqual([]);
    expect(coming.map((p) => p.ts)).toEqual([now + 100]);
  });
  it("sorts both series by ts", () => {
    const { observed } = trendSeries(
      [pt(now - 10), pt(now - 20)], [], now, liveFrom, liveTo,
    );
    expect(observed.map((p) => p.ts)).toEqual([now - 20, now - 10]);
  });

  // --- panned windows (issue #106) -----------------------------------------
  it("gives a window panned entirely into the past no forecast at all", () => {
    // Dragged back a week: everything in view is measured, and there is no
    // seam to bridge -- so no one-point `coming` series either.
    const { observed, coming } = trendSeries(
      [pt(now - 8 * 86_400), pt(now - 7 * 86_400)],
      [pt(now + 100)],
      now,
      now - 9 * 86_400,
      now - 6 * 86_400,
    );
    expect(observed.map((p) => p.ts)).toEqual([
      now - 8 * 86_400,
      now - 7 * 86_400,
    ]);
    expect(coming).toEqual([]);
  });
  it("keeps observed points out of a window that starts after them", () => {
    // ts0 is the left wall: a point older than it is not in view, even
    // though it is older than `now`.
    const { observed } = trendSeries(
      [pt(now - 5000), pt(now - 100)], [], now, now - 1000, now + FUTURE_S,
    );
    expect(observed.map((p) => p.ts)).toEqual([now - 100]);
  });
  it("bridges from the last point IN the window, not the last one that exists", () => {
    // The stitch has to start inside the frame -- otherwise the forecast line
    // reaches back to a point the viewer has panned away from.
    const { coming } = trendSeries(
      [pt(now - 5000), pt(now - 100)],
      [pt(now + 100)],
      now,
      now - 1000,
      now + FUTURE_S,
    );
    expect(coming.map((p) => p.ts)).toEqual([now - 100, now + 100]);
  });
  it("stands the forecast alone when the window opens after now", () => {
    const { observed, coming } = trendSeries(
      [pt(now - 100)], [pt(now + 5000)], now, now + 1000, now + FUTURE_S,
    );
    expect(observed).toEqual([]);
    expect(coming.map((p) => p.ts)).toEqual([now + 5000]);
  });

  it("calibrates a biased forecast to the trail, raw observed bridge intact", () => {
    const { coming } = trendSeries(
      [pt(now - 100, { temp_f: 75 })],
      [pt(now + 100, { temp_f: 70 })],
      now,
      liveFrom,
      liveTo,
    );
    expect(coming[0].temp_f).toBe(75); // the bridge is the real observed point
    expect(coming[1].temp_f).toBeCloseTo(
      70 + 5 * (1 - 200 / BLEND_HORIZON_S), // offset 5F, 200s into the decay
    );
  });
});

describe("blendForecast", () => {
  const now = 100_000;
  const H = BLEND_HORIZON_S;
  it("applies the full offset at the seam and decays it to zero", () => {
    const out = blendForecast(
      [pt(now, { temp_f: 75 })],
      [
        pt(now, { temp_f: 70 }), // at the anchor: full 5F offset
        pt(now + H / 2, { temp_f: 70 }), // halfway out: half the offset
        pt(now + H, { temp_f: 70 }), // horizon: raw again
        pt(now + H * 2, { temp_f: 70 }), // beyond: still raw, never negative
      ],
    );
    expect(out.map((p) => p.temp_f)).toEqual([75, 72.5, 70, 70]);
  });
  it("reads the forecast at the anchor's moment by interpolation", () => {
    // anchor sits 1/4 of the way between forecast points at 60F and 80F
    const out = blendForecast(
      [pt(now + 25, { temp_f: 75 })],
      [pt(now, { temp_f: 60 }), pt(now + 100, { temp_f: 80 })],
    );
    // forecast-at-anchor = 65, offset = 10, decay ~1 at these tiny spans
    expect(out[1].temp_f).toBeCloseTo(90, 1);
  });
  it("anchors on the LAST temp-bearing observed point", () => {
    const out = blendForecast(
      [pt(now - 50, { temp_f: 40 }), pt(now, { temp_f: 75 }), pt(now + 1, { temp_f: null })],
      [pt(now + 10, { temp_f: 70 })],
    );
    expect(out[0].temp_f).toBeCloseTo(75, 1);
  });
  it("returns the forecast untouched when either side lacks a temperature", () => {
    const coming = [pt(now + 10, { temp_f: 70 })];
    expect(blendForecast([], coming)).toEqual(coming);
    expect(blendForecast([pt(now, { temp_f: null })], coming)).toEqual(coming);
    const noTemps = [pt(now + 10, { temp_f: null })];
    expect(blendForecast([pt(now, { temp_f: 75 })], noTemps)).toEqual(noTemps);
  });
  it("rides null temps through and leaves every other series alone", () => {
    const out = blendForecast(
      [pt(now, { temp_f: 75 })],
      [
        pt(now + 10, { temp_f: null, wind_mph: 12 }),
        pt(now + 20, { temp_f: 70, wind_mph: 12, pop: 0.4 }),
      ],
    );
    expect(out[0].temp_f).toBeNull();
    expect(out[0].wind_mph).toBe(12);
    expect(out[1].wind_mph).toBe(12);
    expect(out[1].pop).toBe(0.4);
  });
  it("never mutates its inputs", () => {
    const observed = [pt(now, { temp_f: 75 })];
    const coming = [pt(now + 10, { temp_f: 70 })];
    blendForecast(observed, coming);
    expect(coming[0].temp_f).toBe(70);
    expect(observed[0].temp_f).toBe(75);
  });
});

describe("tempRange", () => {
  it("pads beyond the data and returns integer bounds", () => {
    const r = tempRange([pt(1, { temp_f: 40 }), pt(2, { temp_f: 80 })]);
    expect(r).toEqual({ min: 37, max: 83 });
  });
  it("forces a minimum span so a flat day reads as flat", () => {
    const r = tempRange([pt(1, { temp_f: 70 }), pt(2, { temp_f: 70 })]);
    expect(r!.max - r!.min).toBeGreaterThanOrEqual(16);
  });
  it("ignores null temps; null when nothing has one", () => {
    expect(tempRange([pt(1, { temp_f: null })])).toBeNull();
    expect(tempRange([])).toBeNull();
  });
});

describe("windCeil", () => {
  it("floors at 10 mph so calm days hug the baseline", () => {
    expect(windCeil([pt(1, { wind_mph: 2 })])).toBe(10);
    expect(windCeil([])).toBe(10);
  });
  it("rounds the strongest gust up to a multiple of 5", () => {
    expect(windCeil([pt(1, { wind_mph: 8, wind_gust_mph: 17.2 })])).toBe(20);
  });
});

describe("linePath", () => {
  it("maps ts to x and value to inverted y", () => {
    const path = linePath(
      [pt(0, { temp_f: 0 }), pt(100, { temp_f: 10 })],
      (p) => p.temp_f,
      0, 100, 0, 10, 320, 120,
    );
    expect(path).toBe("M0.0,120.0 L320.0,0.0");
  });
  it("splits the path at null values -- a gap draws as a gap", () => {
    const path = linePath(
      [pt(0), pt(50, { temp_f: null }), pt(100)],
      (p) => p.temp_f,
      0, 100, 60, 80, 100, 100,
    );
    expect(path).toBe("M0.0,50.0 M100.0,50.0");
  });
  it("returns empty for degenerate ranges", () => {
    expect(linePath([pt(1)], (p) => p.temp_f, 5, 5, 0, 10, 100, 100)).toBe("");
    expect(linePath([pt(1)], (p) => p.temp_f, 0, 10, 5, 5, 100, 100)).toBe("");
  });
});

describe("nearestPoint", () => {
  it("snaps to the closest point by timestamp", () => {
    const pts = [pt(100), pt(200), pt(300)];
    expect(nearestPoint(pts, 190)?.ts).toBe(200);
    expect(nearestPoint(pts, 200)?.ts).toBe(200); // exact hit
  });
  it("snaps to the ends beyond the data", () => {
    const pts = [pt(100), pt(200)];
    expect(nearestPoint(pts, -5_000)?.ts).toBe(100);
    expect(nearestPoint(pts, 9_000)?.ts).toBe(200);
  });
  it("breaks ties toward the earlier point", () => {
    expect(nearestPoint([pt(100), pt(200)], 150)?.ts).toBe(100);
  });
  it("is null with nothing to snap to", () => {
    expect(nearestPoint([], 100)).toBeNull();
  });
});

describe("timeTicks", () => {
  it("marks 12h steps across the default window, endpoints and now excluded", () => {
    const ticks = timeTicks();
    expect(ticks.map((t) => t.offsetS / 3600)).toEqual([-12, 12, 24, 36]);
    expect(ticks.map((t) => t.frac)).toEqual([1 / 6, 3 / 6, 4 / 6, 5 / 6]);
  });
  it("handles a past window that is not a multiple of the step", () => {
    const ticks = timeTicks(5 * 3600, 4 * 3600, 2 * 3600);
    expect(ticks.map((t) => t.offsetS / 3600)).toEqual([-4, -2, 2]);
  });
  it("is empty for degenerate inputs", () => {
    expect(timeTicks(0, 0)).toEqual([]);
    expect(timeTicks(3600, 3600, 0)).toEqual([]);
  });
});

describe("dayTicks", () => {
  // Structural assertions against the runtime's own local clock, so the
  // suite passes in any timezone (CI runs UTC, the dev box does not).
  const now = 1752408000; // 2025-07-13 12:00:00 UTC
  // The window the view opens with, which dayTicks used to derive itself.
  const dt = (from: number, to: number) => dayTicks(from, to);
  const win = () => dt(now - PAST_S, now + STATION_FUTURE_S);
  it("marks every local midnight strictly inside the station window", () => {
    const got = win();
    // 24h + 120h = 144h spans 5-7 local midnights depending on time of day
    expect(got.length).toBeGreaterThanOrEqual(5);
    expect(got.length).toBeLessThanOrEqual(7);
    for (const t of got) {
      const d = new Date(t.ts * 1000);
      expect([d.getHours(), d.getMinutes(), d.getSeconds()]).toEqual([0, 0, 0]);
    }
    const ts = got.map((t) => t.ts);
    expect([...ts].sort((a, b) => a - b)).toEqual(ts);
    expect(got.every((t) => t.frac > 0 && t.frac < 1)).toBe(true);
    expect(got[0].ts).toBeGreaterThan(now - PAST_S);
    expect(got[got.length - 1].ts).toBeLessThan(now + STATION_FUTURE_S);
  });
  it("maps ts to frac linearly across the window", () => {
    for (const t of win()) {
      expect(t.frac).toBeCloseTo(
        (t.ts - (now - PAST_S)) / (PAST_S + STATION_FUTURE_S),
        10,
      );
    }
  });
  it("labels each tick with the lowercased weekday of the day it begins", () => {
    for (const t of win()) {
      expect(t.label).toBe(
        new Date(t.ts * 1000)
          .toLocaleDateString(undefined, { weekday: "short" })
          .toLowerCase(),
      );
      expect(t.label.length).toBeGreaterThan(0);
    }
  });
  it("consecutive ticks are one calendar day apart", () => {
    const got = win();
    for (let i = 1; i < got.length; i++) {
      const gap = got[i].ts - got[i - 1].ts;
      // 23-25h covers DST spring/fall days
      expect(gap).toBeGreaterThanOrEqual(23 * 3600);
      expect(gap).toBeLessThanOrEqual(25 * 3600);
    }
  });
  it("is empty for a degenerate window", () => {
    expect(dt(now, now)).toEqual([]);
    expect(dt(now, now - 1)).toEqual([]);
  });

  // --- arbitrary windows (issue #106) --------------------------------------
  // The ticks used to be derived from `now`; panning reaches windows that
  // anchor nowhere near it, and the DST invariant has to survive there too.
  it("marks midnights in a window nowhere near now", () => {
    const from = now - 90 * 86_400;
    const got = dt(from, from + STATION_SPAN_S);
    expect(got.length).toBeGreaterThanOrEqual(5);
    for (const t of got) {
      const d = new Date(t.ts * 1000);
      expect([d.getHours(), d.getMinutes(), d.getSeconds()]).toEqual([0, 0, 0]);
      expect(t.frac).toBeGreaterThan(0);
      expect(t.frac).toBeLessThan(1);
    }
  });
  it("neither skips nor doubles a tick across a DST boundary", () => {
    // A window straddling each US transition, walked at the +36h-then-refloor
    // step. A 23h or 25h day must still produce exactly one midnight.
    for (const [y, m, d] of [
      [2026, 2, 8], // spring forward (23h day)
      [2026, 10, 1], // fall back (25h day)
    ]) {
      const from = Math.floor(new Date(y, m, d - 2, 12).getTime() / 1000);
      const got = dt(from, from + STATION_SPAN_S);
      const midnights = got.map((t) =>
        new Date(t.ts * 1000).toDateString(),
      );
      expect(new Set(midnights).size).toBe(midnights.length); // no doubles
      for (let i = 1; i < got.length; i++) {
        const gap = got[i].ts - got[i - 1].ts;
        expect(gap).toBeGreaterThanOrEqual(23 * 3600);
        expect(gap).toBeLessThanOrEqual(25 * 3600); // no skips
      }
    }
  });
});

describe("clampWindow", () => {
  const oldest = 1_000_000;
  const newest = 2_000_000;
  const span = 100_000;
  it("leaves a window between the walls alone", () => {
    expect(clampWindow(1_500_000, 1_600_000, oldest, newest)).toEqual({
      ts0: 1_500_000,
      ts1: 1_600_000,
    });
  });
  it("stops at the forecast's end rather than showing empty space", () => {
    expect(clampWindow(1_950_000, 2_050_000, oldest, newest)).toEqual({
      ts0: newest - span,
      ts1: newest,
    });
  });
  it("stops at the archive's first reading", () => {
    expect(clampWindow(950_000, 1_050_000, oldest, newest)).toEqual({
      ts0: oldest,
      ts1: oldest + span,
    });
  });
  it("preserves the span on every clamp -- a wall never resizes the window", () => {
    for (const [a, b] of [
      [1_500_000, 1_600_000],
      [-5_000_000, -4_900_000],
      [9_000_000, 9_100_000],
      [oldest, oldest + span],
      [newest - span, newest],
    ]) {
      const c = clampWindow(a, b, oldest, newest);
      expect(c.ts1 - c.ts0).toBe(b - a);
    }
  });
  it("pins right when the walls are closer together than the span", () => {
    // The young-archive case, and the reason this rule exists: on day one the
    // record is minutes old, and the default window (24h back) must still be
    // exactly reachable rather than clamped forward into the future.
    const c = clampWindow(newest - span, newest, newest - 1000, newest);
    expect(c).toEqual({ ts0: newest - span, ts1: newest });
    expect(c.ts1 - c.ts0).toBe(span);
  });
  it("an exactly-fitting window is untouched at either wall", () => {
    expect(clampWindow(oldest, newest, oldest, newest)).toEqual({
      ts0: oldest,
      ts1: newest,
    });
  });
});

describe("mergePoints", () => {
  it("merges two series into one, oldest first", () => {
    expect(mergePoints([pt(300)], [pt(100), pt(200)]).map((p) => p.ts)).toEqual(
      [100, 200, 300],
    );
  });
  it("dedupes the overlap -- the archive holds the window's own rows", () => {
    const merged = mergePoints([pt(200), pt(300)], [pt(100), pt(200)]);
    expect(merged.map((p) => p.ts)).toEqual([100, 200, 300]);
  });
  it("lets the live window win a tie", () => {
    const merged = mergePoints(
      [pt(100, { temp_f: 71 })],
      [pt(100, { temp_f: 32 })],
    );
    expect(merged).toHaveLength(1);
    expect(merged[0].temp_f).toBe(71);
  });
  it("handles either side being empty", () => {
    expect(mergePoints([], [pt(100)]).map((p) => p.ts)).toEqual([100]);
    expect(mergePoints([pt(100)], []).map((p) => p.ts)).toEqual([100]);
    expect(mergePoints([], [])).toEqual([]);
  });
});

describe("windowEdgeLabel", () => {
  it("prints the live window's corners exactly as the view always has", () => {
    expect(windowEdgeLabel(-PAST_S)).toBe("−24h");
    expect(windowEdgeLabel(STATION_FUTURE_S)).toBe("+5d");
  });
  it("switches to days once hours stop meaning anything", () => {
    expect(windowEdgeLabel(-47 * 3600)).toBe("−47h");
    expect(windowEdgeLabel(-48 * 3600)).toBe("−2d");
    expect(windowEdgeLabel(-9 * 86_400)).toBe("−9d");
  });
  it("uses a real minus sign, not a hyphen", () => {
    expect(windowEdgeLabel(-3600).startsWith("−")).toBe(true);
    expect(windowEdgeLabel(-3600).startsWith("-")).toBe(false);
  });
  it("stamps zero as a plus", () => {
    expect(windowEdgeLabel(0)).toBe("+0h");
  });
});

describe("snowSeason", () => {
  // local-date constructed timestamps, so the boundaries hold in any TZ
  const mid = (y: number, month0: number) =>
    Math.floor(new Date(y, month0, 15, 12).getTime() / 1000);
  it("is on November through March, off April through October (#69)", () => {
    expect(snowSeason(mid(2026, 10))).toBe(true); // november
    expect(snowSeason(mid(2026, 0))).toBe(true); // january
    expect(snowSeason(mid(2026, 2))).toBe(true); // march
    expect(snowSeason(mid(2026, 3))).toBe(false); // april
    expect(snowSeason(mid(2026, 6))).toBe(false); // july
    expect(snowSeason(mid(2026, 9))).toBe(false); // october
  });
});

describe("nightBands", () => {
  const D = 86_400;
  const sunrise = 6 * 3600; // 06:00 day zero
  const sunset = 21 * 3600; // 21:00 day zero
  it("repeats sunset->sunrise nights across the chart window", () => {
    const now = 12 * 3600; // noon day zero
    const bands = nightBands(sunrise, sunset, now - 24 * 3600, now + 48 * 3600);
    expect(bands).toEqual([
      { start: sunset - D, end: sunrise }, // last night
      { start: sunset, end: sunrise + D }, // tonight
      { start: sunset + D, end: sunrise + 2 * D }, // tomorrow night
    ]);
  });
  it("clamps bands to the window edges", () => {
    const bands = nightBands(sunrise, sunset, 0, D);
    expect(bands).toEqual([
      { start: 0, end: sunrise }, // last night, cut at the window start
      { start: sunset, end: D }, // tonight, cut at the window end
    ]);
  });
  it("is empty without sun times or with garbage ordering", () => {
    expect(nightBands(null, sunset, 0, D)).toEqual([]);
    expect(nightBands(sunrise, null, 0, D)).toEqual([]);
    expect(nightBands(sunset, sunrise, 0, D)).toEqual([]); // swapped
    expect(nightBands(sunrise, sunrise, 0, D)).toEqual([]);
  });
  // --- the honesty horizon (issue #106) ------------------------------------
  it("stops repeating the sun times past the horizon", () => {
    // Panned a month back: today's sunrise/sunset repeated that far would be
    // an hour off, and a confident lie is worse than no shading at all.
    const far = -30 * D;
    expect(nightBands(sunrise, sunset, far, far + 2 * D)).toEqual([]);
  });
  it("still bands the day at the horizon's edge", () => {
    // k = -7 is the last honest repetition (~15 min of drift, ~2px).
    const bands = nightBands(
      sunrise,
      sunset,
      sunset - 7 * D - 60,
      sunrise - 6 * D + 60,
    );
    expect(bands).toEqual([{ start: sunset - 7 * D, end: sunrise - 6 * D }]);
  });
  it("bands nothing one day past the horizon", () => {
    expect(
      nightBands(sunrise, sunset, sunset - 8 * D - 60, sunrise - 7 * D + 60),
    ).toEqual([]);
  });
  it("takes the horizon as a parameter", () => {
    const win: [number, number] = [sunset - 3 * D - 60, sunrise - 2 * D + 60];
    expect(nightBands(sunrise, sunset, ...win, 2)).toEqual([]);
    expect(nightBands(sunrise, sunset, ...win, 3)).toHaveLength(1);
  });
  it("never reaches the horizon from the panel's live window", () => {
    // The panel is 24h/48h, so its bands are always within two days of today
    // -- the horizon is a station-view concern and can't touch it.
    const now = 12 * 3600;
    expect(
      nightBands(sunrise, sunset, now - PAST_S, now + FUTURE_S),
    ).toHaveLength(3);
  });

  it("is empty for a degenerate window", () => {
    expect(nightBands(sunrise, sunset, D, D)).toEqual([]);
  });
});

describe("compass", () => {
  it("names the 8 points and wraps 360", () => {
    expect(compass(0)).toBe("N");
    expect(compass(45)).toBe("NE");
    expect(compass(240)).toBe("SW");
    expect(compass(359)).toBe("N");
  });
  it("is blank when the bearing is unknown", () => {
    expect(compass(null)).toBe("");
  });
});

describe("seriesCeil", () => {
  it("rounds the peak up to a clean step with a floor", () => {
    const pts = [pt(1, { rain_rate_inhr: 0.32 }), pt(2, { rain_rate_inhr: 0.1 })];
    expect(seriesCeil(pts, (p) => p.rain_rate_inhr, 0.25, 0.25)).toBe(0.5);
  });
  it("holds the floor on a quiet (or empty) series", () => {
    expect(seriesCeil([pt(1)], (p) => p.rain_rate_inhr, 0.25, 0.25)).toBe(0.25);
    expect(seriesCeil([], (p) => p.solar_wm2, 200, 100)).toBe(200);
  });
});

describe("pressureRange", () => {
  it("pads around the data", () => {
    const pts = [
      pt(1, { pressure_rel_inhg: 29.2 }),
      pt(2, { pressure_rel_inhg: 29.8 }),
    ];
    expect(pressureRange(pts)).toEqual({ min: 29.15, max: 29.85 });
  });
  it("holds a minimum span so a steady day reads as steady", () => {
    const got = pressureRange([pt(1, { pressure_rel_inhg: 29.9 })]);
    expect(got).not.toBeNull();
    expect(got!.max - got!.min).toBeCloseTo(0.3, 5);
  });
  it("is null with no pressure in view", () => {
    expect(pressureRange([pt(1)])).toBeNull();
    expect(pressureRange([])).toBeNull();
  });
});

describe("pressureTrend", () => {
  const NOW = 1_000_000;
  const trail = (deltaInhg: number) => [
    pt(NOW - TREND_SPAN_S, { pressure_rel_inhg: 29.5 }),
    pt(NOW, { pressure_rel_inhg: 29.5 + deltaInhg }),
  ];
  it("calls rising, falling, and steady past the epsilon", () => {
    expect(pressureTrend(trail(0.05), NOW)).toBe("rising");
    expect(pressureTrend(trail(-0.05), NOW)).toBe("falling");
    expect(pressureTrend(trail(0.01), NOW)).toBe("steady");
  });
  it("has no opinion on a short or pressure-less trail", () => {
    // both points too recent: the anchor sits far from the 3h target
    const short = [
      pt(NOW - 600, { pressure_rel_inhg: 29.5 }),
      pt(NOW, { pressure_rel_inhg: 29.9 }),
    ];
    expect(pressureTrend(short, NOW)).toBeNull();
    expect(pressureTrend([pt(NOW)], NOW)).toBeNull();
    expect(pressureTrend([], NOW)).toBeNull();
  });
  it("ignores forecast points ahead of now", () => {
    const withFuture = [
      ...trail(0.05),
      pt(NOW + 3600, { pressure_rel_inhg: 20 }),
    ];
    expect(pressureTrend(withFuture, NOW)).toBe("rising");
  });
});

describe("seriesTrend", () => {
  const NOW = 1_000_000;
  const trail = (over0: Partial<WeatherPoint>, over1: Partial<WeatherPoint>) =>
    [pt(NOW - TREND_SPAN_S, over0), pt(NOW, over1)];
  it("judges any series with its own epsilon (issue #67)", () => {
    expect(
      seriesTrend(
        trail({ temp_f: 70 }, { temp_f: 72 }),
        NOW,
        (p) => p.temp_f,
        TEMP_TREND_EPS_F,
      ),
    ).toBe("rising");
    // ±1.5F is steady for temperature but a fall for dew point's ±1F
    expect(
      seriesTrend(
        trail({ temp_f: 70 }, { temp_f: 68.6 }),
        NOW,
        (p) => p.temp_f,
        TEMP_TREND_EPS_F,
      ),
    ).toBe("steady");
    expect(
      seriesTrend(
        trail({ dew_point_f: 60 }, { dew_point_f: 58.6 }),
        NOW,
        (p) => p.dew_point_f,
        DEW_TREND_EPS_F,
      ),
    ).toBe("falling");
    expect(
      seriesTrend(
        trail({ humidity_pct: 60 }, { humidity_pct: 64 }),
        NOW,
        (p) => p.humidity_pct,
        HUMIDITY_TREND_EPS_PCT,
      ),
    ).toBe("rising");
  });
  it("skips points where the series is null when anchoring", () => {
    // pressure-bearing points that lack a temperature must not anchor a
    // temperature trend
    const pts = [
      pt(NOW - TREND_SPAN_S, { temp_f: null, pressure_rel_inhg: 29.5 }),
      pt(NOW, { temp_f: 80 }),
    ];
    expect(seriesTrend(pts, NOW, (p) => p.temp_f, TEMP_TREND_EPS_F)).toBeNull();
  });
  it("has no opinion on a short trail", () => {
    const short = [pt(NOW - 600, { temp_f: 70 }), pt(NOW, { temp_f: 80 })];
    expect(
      seriesTrend(short, NOW, (p) => p.temp_f, TEMP_TREND_EPS_F),
    ).toBeNull();
    expect(seriesTrend([], NOW, (p) => p.temp_f, TEMP_TREND_EPS_F)).toBeNull();
  });
});

describe("conditionIcon", () => {
  // A calm, clear report; each case overrides what its sky needs.
  const cur = (over: Partial<CurrentWeather> = {}): CurrentWeather =>
    ({
      ...(parseCurrent(
        JSON.stringify({
          ts: 1752148800,
          temp_f: 70,
          condition: "Clear",
          description: "clear sky",
          wind_mph: 5,
          wind_gust_mph: 8,
          raining: 0,
        }),
      ) as CurrentWeather),
      ...over,
    });

  it("maps each sky to its icon", () => {
    expect(conditionIcon(cur())).toBe("sunny");
    expect(
      conditionIcon(cur({ condition: "Clouds", description: "few clouds" })),
    ).toBe("mostly-sunny");
    expect(
      conditionIcon(
        cur({ condition: "Clouds", description: "scattered clouds" }),
      ),
    ).toBe("partly-cloudy");
    expect(
      conditionIcon(
        cur({ condition: "Clouds", description: "broken clouds" }),
      ),
    ).toBe("cloudy");
    expect(
      conditionIcon(
        cur({ condition: "Clouds", description: "overcast clouds" }),
      ),
    ).toBe("cloudy");
    expect(
      conditionIcon(cur({ condition: "Rain", description: "light rain" })),
    ).toBe("raining");
    expect(
      conditionIcon(cur({ condition: "Drizzle", description: "drizzle" })),
    ).toBe("raining");
    expect(
      conditionIcon(cur({ condition: "Snow", description: "light snow" })),
    ).toBe("snowing");
    expect(
      conditionIcon(
        cur({ condition: "Thunderstorm", description: "thunderstorm" }),
      ),
    ).toBe("stormy");
  });

  it("goes windy on sustained wind or gusts at the thresholds", () => {
    expect(conditionIcon(cur({ wind_mph: WINDY_SUSTAINED_MPH }))).toBe(
      "windy",
    );
    expect(conditionIcon(cur({ wind_gust_mph: WINDY_GUST_MPH }))).toBe(
      "windy",
    );
    // just under either threshold, the sky keeps the billing
    expect(
      conditionIcon(
        cur({
          wind_mph: WINDY_SUSTAINED_MPH - 1,
          wind_gust_mph: WINDY_GUST_MPH - 1,
        }),
      ),
    ).toBe("sunny");
  });

  it("lets drama outrank wind", () => {
    // a gale during a thunderstorm is still a thunderstorm
    expect(
      conditionIcon(cur({ condition: "Thunderstorm", wind_mph: 40 })),
    ).toBe("stormy");
    expect(conditionIcon(cur({ condition: "Snow", wind_gust_mph: 45 }))).toBe(
      "snowing",
    );
  });

  it("believes the piezo over OWM's word", () => {
    // the driveway instrument says water is falling; the grid cell says clouds
    expect(
      conditionIcon(
        cur({ condition: "Clouds", description: "overcast clouds", raining: 1 }),
      ),
    ).toBe("raining");
  });

  it("reads the atmosphere group as a grey sky", () => {
    expect(conditionIcon(cur({ condition: "Mist", description: "mist" }))).toBe(
      "cloudy",
    );
    expect(conditionIcon(cur({ condition: "Fog", description: "fog" }))).toBe(
      "cloudy",
    );
  });

  it("has nothing to say without a report or a sky", () => {
    expect(conditionIcon(null)).toBeNull();
    // station-only payload: no OWM garnish yet, calm and dry
    expect(conditionIcon(cur({ condition: null, description: null }))).toBeNull();
  });
});
