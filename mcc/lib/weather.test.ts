import { describe, expect, it } from "vitest";
import {
  PRESSURE_TREND_SPAN_S,
  WeatherPoint,
  PAST_S,
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
  it("clips history to the trailing window and forecast to the leading one", () => {
    const { observed, coming } = trendSeries(
      [pt(now - 90_000), pt(now - 500), pt(now + 5)], // too old / in / future
      [pt(now - 10), pt(now + 500), pt(now + 900_000)], // past / in / too far
      now,
      86_400,
      172_800,
    );
    expect(observed.map((p) => p.ts)).toEqual([now - 500]);
    expect(coming.map((p) => p.ts)).toEqual([now - 500, now + 500]); // bridge + 1
  });
  it("bridges the last observed point into the forecast so the lines meet", () => {
    const { coming } = trendSeries([pt(now - 100)], [pt(now + 100)], now);
    expect(coming[0].ts).toBe(now - 100);
  });
  it("stands the forecast alone when nothing has been observed yet", () => {
    const { observed, coming } = trendSeries([], [pt(now + 100)], now);
    expect(observed).toEqual([]);
    expect(coming.map((p) => p.ts)).toEqual([now + 100]);
  });
  it("sorts both series by ts", () => {
    const { observed } = trendSeries([pt(now - 10), pt(now - 20)], [], now);
    expect(observed.map((p) => p.ts)).toEqual([now - 20, now - 10]);
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
  it("marks every local midnight strictly inside the station window", () => {
    const got = dayTicks(now);
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
    for (const t of dayTicks(now)) {
      expect(t.frac).toBeCloseTo(
        (t.ts - (now - PAST_S)) / (PAST_S + STATION_FUTURE_S),
        10,
      );
    }
  });
  it("labels each tick with the lowercased weekday of the day it begins", () => {
    for (const t of dayTicks(now)) {
      expect(t.label).toBe(
        new Date(t.ts * 1000)
          .toLocaleDateString(undefined, { weekday: "short" })
          .toLowerCase(),
      );
      expect(t.label.length).toBeGreaterThan(0);
    }
  });
  it("consecutive ticks are one calendar day apart", () => {
    const got = dayTicks(now);
    for (let i = 1; i < got.length; i++) {
      const gap = got[i].ts - got[i - 1].ts;
      // 23-25h covers DST spring/fall days
      expect(gap).toBeGreaterThanOrEqual(23 * 3600);
      expect(gap).toBeLessThanOrEqual(25 * 3600);
    }
  });
  it("is empty for a degenerate window", () => {
    expect(dayTicks(now, 0, 0)).toEqual([]);
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
    pt(NOW - PRESSURE_TREND_SPAN_S, { pressure_rel_inhg: 29.5 }),
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
