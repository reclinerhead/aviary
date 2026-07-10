// Client types + pure shaping for the Weather Post panel (issue #25). The
// weather service on pearl publishes RETAINED bus messages, so the browser
// gets the latest report + forecast + 48h observed window the instant it
// subscribes -- there is no HTTP path for weather, only the bus (lib/bus.ts).
// Timestamps are unix epoch SECONDS (OpenWeather's native clock).

export const WEATHER_CURRENT_TOPIC = "weather/current";
export const WEATHER_FORECAST_TOPIC = "weather/forecast";
export const WEATHER_HISTORY_TOPIC = "weather/history";
export const WEATHER_STATUS_TOPIC = "weather/status";

export type CurrentWeather = {
  ts: number;
  temp_f: number | null;
  feels_like_f: number | null;
  humidity_pct: number | null;
  wind_mph: number | null;
  wind_gust_mph: number | null;
  wind_deg: number | null;
  condition: string | null;
  description: string | null;
  sunrise: number | null;
  sunset: number | null;
};

/** One point of the trend chart -- the shape shared by weather/forecast and
 * weather/history payloads ({points: [...]}) . */
export type WeatherPoint = {
  ts: number;
  temp_f: number | null;
  wind_mph: number | null;
  wind_gust_mph: number | null;
  condition: string | null;
};

// A report older than this is treated as no report: the panel goes stale
// rather than presenting yesterday's weather as now (3 missed 10-min polls).
export const STALE_AFTER_S = 30 * 60;

// The chart window: observed trail behind "now", forecast ahead of it.
// 24h back + 48h forward puts "now" at the 1/3 mark -- enough trail to see
// where the day came from, enough forecast to plan the next feeding.
export const PAST_S = 24 * 3600;
export const FUTURE_S = 48 * 3600;

// --- Pure payload parsing (unit-tested in weather.test.ts) -------------------

const num = (v: unknown): number | null => (typeof v === "number" ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" ? v : null);

export type WeatherStatus = "online" | "offline";

/** Parse a weather/status payload. Raw strings, not JSON -- the status topics
 * follow the narrator presence convention (issue #31). Anything else maps to
 * null ("no presence info"), so a stray payload can only ever demote the
 * masthead to the freshness-based judgement, never fake a state. */
export function parseStatus(payload: string): WeatherStatus | null {
  const s = payload.trim();
  return s === "online" || s === "offline" ? s : null;
}

/** Relative age for "last checked in" -- coarse on purpose (a field log, not
 * a stopwatch). Future timestamps clamp to "just now" rather than counting
 * negative (clock skew between pearl and the viewer is not the reader's
 * problem). */
export function ageText(ts: number, now: number): string {
  const s = Math.max(0, now - ts);
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

/** Parse a weather/current payload; null for anything malformed (the bus is a
 * shared room -- a stray message must never crash the panel). */
export function parseCurrent(payload: string): CurrentWeather | null {
  try {
    const o = JSON.parse(payload);
    if (typeof o?.ts !== "number") return null;
    return {
      ts: o.ts,
      temp_f: num(o.temp_f),
      feels_like_f: num(o.feels_like_f),
      humidity_pct: num(o.humidity_pct),
      wind_mph: num(o.wind_mph),
      wind_gust_mph: num(o.wind_gust_mph),
      wind_deg: num(o.wind_deg),
      condition: str(o.condition),
      description: str(o.description),
      sunrise: num(o.sunrise),
      sunset: num(o.sunset),
    };
  } catch {
    return null;
  }
}

/** Parse a weather/forecast or weather/history payload into chart points.
 * Silently drops ts-less points; null for a malformed payload. */
export function parsePoints(payload: string): WeatherPoint[] | null {
  try {
    const o = JSON.parse(payload);
    if (!Array.isArray(o?.points)) return null;
    return (o.points as unknown[])
      .filter((p): p is Record<string, unknown> =>
        typeof p === "object" && p !== null &&
        typeof (p as Record<string, unknown>).ts === "number",
      )
      .map((p) => ({
        ts: p.ts as number,
        temp_f: num(p.temp_f),
        wind_mph: num(p.wind_mph),
        wind_gust_mph: num(p.wind_gust_mph),
        condition: str(p.condition),
      }));
  } catch {
    return null;
  }
}

// --- Pure chart shaping -------------------------------------------------------

export type Trend = { observed: WeatherPoint[]; coming: WeatherPoint[] };

/** Clip history to the trailing window and forecast to the leading one, both
 * sorted by ts. The last observed point is PREPENDED to `coming` so the two
 * polylines meet at "now" instead of leaving a gap. */
export function trendSeries(
  history: WeatherPoint[],
  forecast: WeatherPoint[],
  now: number,
  pastS = PAST_S,
  futureS = FUTURE_S,
): Trend {
  const byTs = (a: WeatherPoint, b: WeatherPoint) => a.ts - b.ts;
  const observed = history
    .filter((p) => p.ts >= now - pastS && p.ts <= now)
    .sort(byTs);
  const coming = forecast
    .filter((p) => p.ts > now && p.ts <= now + futureS)
    .sort(byTs);
  const last = observed[observed.length - 1];
  return { observed, coming: last ? [last, ...coming] : coming };
}

/** Temperature axis for the whole trend, padded so the line never kisses the
 * frame and a flat day still reads as flat (minimum 16F of span). Integer
 * bounds so the axis labels are honest. Null when nothing has a temperature. */
export function tempRange(
  pts: WeatherPoint[],
): { min: number; max: number } | null {
  const temps = pts.map((p) => p.temp_f).filter((t): t is number => t !== null);
  if (temps.length === 0) return null;
  let min = Math.min(...temps) - 3;
  let max = Math.max(...temps) + 3;
  const shortfall = 16 - (max - min);
  if (shortfall > 0) {
    min -= shortfall / 2;
    max += shortfall / 2;
  }
  return { min: Math.floor(min), max: Math.ceil(max) };
}

/** Wind axis ceiling: the strongest wind or gust in view, floored at 10 mph
 * (a calm day should hug the baseline, not fill the chart with noise) and
 * rounded up to a clean multiple of 5 for the label. */
export function windCeil(pts: WeatherPoint[]): number {
  const winds = pts
    .flatMap((p) => [p.wind_mph, p.wind_gust_mph])
    .filter((w): w is number => w !== null);
  return Math.max(10, Math.ceil(Math.max(0, ...winds) / 5) * 5);
}

/** SVG path for one series: ts -> x across [ts0, ts1], value -> y (inverted,
 * SVG grows downward) across [vMin, vMax]. Points with a null value split the
 * path (a fresh "M"), so a data gap draws as a gap, never as a fake line. */
export function linePath(
  pts: WeatherPoint[],
  value: (p: WeatherPoint) => number | null,
  ts0: number,
  ts1: number,
  vMin: number,
  vMax: number,
  width: number,
  height: number,
): string {
  if (ts1 <= ts0 || vMax <= vMin) return "";
  const parts: string[] = [];
  let pen = false;
  for (const p of pts) {
    const v = value(p);
    if (v === null) {
      pen = false;
      continue;
    }
    const x = ((p.ts - ts0) / (ts1 - ts0)) * width;
    const y = height - ((v - vMin) / (vMax - vMin)) * height;
    parts.push(`${pen ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`);
    pen = true;
  }
  return parts.join(" ");
}

/** Wind bearing -> the 8-point compass a field notebook would use.
 * OpenWeather reports the direction the wind comes FROM. */
export function compass(deg: number | null): string {
  if (deg === null) return "";
  const points = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  return points[Math.round(((deg % 360) + 360) % 360 / 45) % 8];
}
