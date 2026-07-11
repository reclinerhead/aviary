import { describe, expect, it } from "vitest";
import {
  WeatherPoint,
  ageText,
  compass,
  linePath,
  nearestPoint,
  nightBands,
  parseCurrent,
  parsePoints,
  parseStatus,
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
