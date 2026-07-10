import { describe, expect, it } from "vitest";
import {
  WeatherPoint,
  compass,
  linePath,
  parseCurrent,
  parsePoints,
  tempRange,
  trendSeries,
  windCeil,
} from "./weather";

const pt = (ts: number, over: Partial<WeatherPoint> = {}): WeatherPoint => ({
  ts,
  temp_f: 70,
  wind_mph: 5,
  wind_gust_mph: null,
  condition: "Clear",
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
  it("nulls missing or mistyped fields but keeps the report", () => {
    const got = parseCurrent(JSON.stringify({ ts: 5, temp_f: "warm" }));
    expect(got?.ts).toBe(5);
    expect(got?.temp_f).toBeNull();
    expect(got?.condition).toBeNull();
  });
  it("rejects a report with no ts, junk, and non-JSON", () => {
    expect(parseCurrent(JSON.stringify({ temp_f: 70 }))).toBeNull();
    expect(parseCurrent("{not json")).toBeNull();
    expect(parseCurrent("null")).toBeNull();
  });
});

describe("parsePoints", () => {
  it("maps a points payload", () => {
    const got = parsePoints(
      JSON.stringify({ points: [{ ts: 100, temp_f: 71.5, wind_mph: 4 }] }),
    );
    expect(got).toEqual([
      { ts: 100, temp_f: 71.5, wind_mph: 4, wind_gust_mph: null, condition: null },
    ]);
  });
  it("drops ts-less points, rejects payloads without a points array", () => {
    expect(
      parsePoints(JSON.stringify({ points: [{ temp_f: 70 }, { ts: 1 }] })),
    ).toHaveLength(1);
    expect(parsePoints(JSON.stringify({}))).toBeNull();
    expect(parsePoints("nope")).toBeNull();
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
