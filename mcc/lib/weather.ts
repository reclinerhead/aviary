// Client types + pure shaping for the Weather Post panel (issue #25). The
// weather service on pearl publishes RETAINED bus messages, so the browser
// gets the latest report + forecast + 48h observed window the instant it
// subscribes -- the LIVE panel needs no HTTP at all, only the bus (lib/bus.ts).
// Timestamps are unix epoch SECONDS (OpenWeather's native clock).
//
// The one HTTP path is the seasonal archive (issue #105): anything older than
// the retained 48h window can't ride a bus payload that gets republished whole
// on every append, so the deep past is fetched on demand from
// /weather/history -- same-origin off pearl's local disk, never the /daemon
// proxy. It answers in the same `{points: [...]}` shape the bus topic carries,
// so parsePoints() reads both and the chart can't tell them apart.

export const WEATHER_CURRENT_TOPIC = "weather/current";
export const WEATHER_FORECAST_TOPIC = "weather/forecast";
export const WEATHER_HISTORY_TOPIC = "weather/history";
export const WEATHER_REPORT_TOPIC = "weather/report";
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
  // The station's own instruments (issue #51). Every field null on a
  // pre-#51 payload -- the panel renders the em-dash placeholder, never NaN.
  dew_point_f: number | null;
  vpd_inhg: number | null;
  wind_max_daily_gust_mph: number | null;
  solar_wm2: number | null;
  uv_index: number | null;
  raining: number | null; // 0/1 -- the piezo's "falling right now" bit
  rain_rate_inhr: number | null;
  rain_event_in: number | null;
  rain_day_in: number | null;
  rain_week_in: number | null;
  rain_month_in: number | null;
  rain_year_in: number | null;
  pressure_abs_inhg: number | null;
  pressure_rel_inhg: number | null;
  indoor_temp_f: number | null;
  indoor_humidity_pct: number | null;
  station_battery: number | null; // 0-5, the WH90's own scale
  station_voltage: number | null;
  station_signal: number | null; // 0-4 radio bars, from the sensor roster
};

/** One point of the trend chart -- the shape shared by weather/forecast and
 * weather/history payloads ({points: [...]}). History points carry the
 * station's extra series (issue #51); forecast points leave them null --
 * except rain_rate_inhr, which forecast points also carry since issue #56
 * (the 3-hour step's precip volume as an average rate), plus pop, which is
 * forecast-only (the station doesn't deal in probabilities). */
export type WeatherPoint = {
  ts: number;
  temp_f: number | null;
  wind_mph: number | null;
  wind_gust_mph: number | null;
  condition: string | null;
  humidity_pct: number | null;
  dew_point_f: number | null;
  pressure_rel_inhg: number | null;
  rain_rate_inhr: number | null;
  rain_day_in: number | null;
  solar_wm2: number | null;
  uv_index: number | null;
  pop: number | null; // precipitation probability 0..1, forecast points only
  snow_3h_in: number | null; // the step's snow, inches -- forecast only (#65)
};

// A report older than this is treated as no report: the panel goes stale
// rather than presenting yesterday's weather as now (3 missed 10-min polls).
export const STALE_AFTER_S = 30 * 60;

/** Willard's on-air segment (issue #45), retained on weather/report. */
export type WeatherReport = {
  ts: number;
  text: string;
  model: string | null;
};

// Willard broadcasts every ~30 minutes; a retained segment older than three
// missed broadcasts is history, not news -- the panel shows the between-
// broadcasts state rather than presenting yesterday's showmanship as current.
export const REPORT_STALE_S = 90 * 60;

// The chart window: observed trail behind "now", forecast ahead of it.
// 24h back + 48h forward puts "now" at the 1/3 mark -- enough trail to see
// where the day came from, enough forecast to plan the next feeding.
export const PAST_S = 24 * 3600;
export const FUTURE_S = 48 * 3600;
// The station view stretches to everything the free API publishes (issue
// #60): OWM's classic /forecast runs 5 days at 3-hour steps, so 120h ahead
// with "now" at the 1/6 mark. Fixed, never sized to the payload -- a short
// payload leaves the far end honestly blank instead of reflowing the axis
// (the no-layout-shift rule). The panel chart keeps the 48h window above:
// six days in ~400px would be a smear, not a chart.
export const STATION_FUTURE_S = 120 * 3600;

// The station window's SPAN (issue #106). Panning slides this window along
// the archive; it never resizes it -- span is fixed, only position moves.
// (A zoom/span control is the acknowledged follow-up once the archive holds
// enough months to make dragging 144h at a time tedious.)
export const STATION_SPAN_S = PAST_S + STATION_FUTURE_S;

/** Slide a window back inside the walls WITHOUT resizing it (issue #106):
 * the span is the viewer's setting, never something a clamp gets to change.
 * `newest` is the forecast's end -- panning right past it would show space
 * with no data behind it -- and `oldest` is the earliest reading that exists.
 *
 * When the walls are closer together than the span, the RIGHT wall wins and
 * the window overhangs the left one. That case is the normal state, not an
 * edge case: a young archive holds less than 144h, and the default window
 * (24h back, 120h ahead) must stay exactly reachable from day one. */
export function clampWindow(
  ts0: number,
  ts1: number,
  oldest: number,
  newest: number,
): { ts0: number; ts1: number } {
  const span = ts1 - ts0;
  if (newest - oldest < span) return { ts0: newest - span, ts1: newest };
  if (ts1 > newest) return { ts0: newest - span, ts1: newest };
  if (ts0 < oldest) return { ts0: oldest, ts1: oldest + span };
  return { ts0, ts1 };
}

/** Merge two point series by ts, oldest first (issue #106): the retained bus
 * window and whatever the archive has handed over. Deduped because the two
 * genuinely overlap -- the archive holds the same rows the window does, and a
 * duplicate ts would draw a zero-length segment and double-count in the axis
 * scan. `a` wins a tie: the bus is the fresher voice for the same moment. */
export function mergePoints(
  a: WeatherPoint[],
  b: WeatherPoint[],
): WeatherPoint[] {
  const byTs = new Map<number, WeatherPoint>();
  for (const p of b) byTs.set(p.ts, p);
  for (const p of a) byTs.set(p.ts, p);
  return [...byTs.values()].sort((x, y) => x.ts - y.ts);
}

/** The station axis's corner labels as an offset from now (issue #106):
 * -86400 -> "−24h", +432000 -> "+5d". Hours read naturally for a day or two
 * and stop meaning anything past that, which is dayTicks' reasoning applied
 * to the corners. A live window prints exactly the "−24h" / "+5d" the view
 * has always shown; a panned one tells the truth instead. U+2212 minus, not
 * a hyphen -- it lines up in the telemetry face. */
export function windowEdgeLabel(offsetS: number): string {
  const sign = offsetS < 0 ? "−" : "+";
  const abs = Math.abs(offsetS);
  if (abs < 48 * 3600) return `${sign}${Math.round(abs / 3600)}h`;
  return `${sign}${Math.round(abs / 86400)}d`;
}

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
      dew_point_f: num(o.dew_point_f),
      vpd_inhg: num(o.vpd_inhg),
      wind_max_daily_gust_mph: num(o.wind_max_daily_gust_mph),
      solar_wm2: num(o.solar_wm2),
      uv_index: num(o.uv_index),
      raining: num(o.raining),
      rain_rate_inhr: num(o.rain_rate_inhr),
      rain_event_in: num(o.rain_event_in),
      rain_day_in: num(o.rain_day_in),
      rain_week_in: num(o.rain_week_in),
      rain_month_in: num(o.rain_month_in),
      rain_year_in: num(o.rain_year_in),
      pressure_abs_inhg: num(o.pressure_abs_inhg),
      pressure_rel_inhg: num(o.pressure_rel_inhg),
      indoor_temp_f: num(o.indoor_temp_f),
      indoor_humidity_pct: num(o.indoor_humidity_pct),
      station_battery: num(o.station_battery),
      station_voltage: num(o.station_voltage),
      station_signal: num(o.station_signal),
    };
  } catch {
    return null;
  }
}

/** Parse a weather/report payload; null for anything malformed or empty --
 * a segment with no words is no segment. */
export function parseReport(payload: string): WeatherReport | null {
  try {
    const o = JSON.parse(payload);
    if (typeof o?.ts !== "number") return null;
    if (typeof o?.text !== "string" || o.text.trim() === "") return null;
    return { ts: o.ts, text: o.text, model: str(o.model) };
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
        humidity_pct: num(p.humidity_pct),
        dew_point_f: num(p.dew_point_f),
        pressure_rel_inhg: num(p.pressure_rel_inhg),
        rain_rate_inhr: num(p.rain_rate_inhr),
        rain_day_in: num(p.rain_day_in),
        solar_wm2: num(p.solar_wm2),
        uv_index: num(p.uv_index),
        pop: num(p.pop),
        snow_3h_in: num(p.snow_3h_in),
      }));
  } catch {
    return null;
  }
}

// --- The seasonal archive (issue #105) ----------------------------------------
// The read half of weather_archive.py. Hand-mirrored types and a thin fetch,
// the lib/history.ts convention: no client, no retry, throw on non-OK.

/** The widest range the archive answers, mirroring weather_archive.MAX_SPAN_S
 * -- the /history clamp precedent ("a typo can't bucket ten years"). Both
 * ends clamp it: the route so a bad `from` can't scan the table, and here so
 * an absurd ask never leaves the browser. */
export const ARCHIVE_MAX_SPAN_S = 90 * 86400;

/** Validate + clamp a requested archive range, mirroring
 * weather_archive.clamp_range(). null when either end is absent, blank, or
 * not a number -- the route answers that with an empty series, never an
 * error. The clamp anchors at `to`: ask for ten years and you get the newest
 * 90 days of it. An inverted range is left inverted on purpose -- it selects
 * nothing, which is the honest answer to a range containing no time. */
export function parseRange(
  rawFrom: string | null,
  rawTo: string | null,
  maxSpanS = ARCHIVE_MAX_SPAN_S,
): { from: number; to: number } | null {
  const epoch = (raw: string | null): number | null => {
    if (raw === null || raw.trim() === "") return null;
    const n = Number(raw);
    return Number.isFinite(n) ? Math.trunc(n) : null;
  };
  const to = epoch(rawTo);
  let from = epoch(rawFrom);
  if (from === null || to === null) return null;
  if (to - from > maxSpanS) from = to - maxSpanS;
  return { from, to };
}

/** The archived observations in [from, to], both ends inclusive, oldest
 * first. `cache: "no-store"` -- unlike a frame's bytes, a time range's
 * contents grow, so nothing here is immutable. An empty archive, an unset
 * MERLE_WEATHER_DB, and a range with no data all answer `{points: []}`
 * quietly: on day one an empty archive is the normal state, not an error.
 * A malformed body yields [] for the same reason (parsePoints returns null);
 * only a non-OK status is worth throwing over. */
export async function fetchArchive(
  from: number,
  to: number,
): Promise<WeatherPoint[]> {
  const res = await fetch(`/weather/history?from=${from}&to=${to}`,
    { cache: "no-store" });
  if (!res.ok) throw new Error(`/weather/history -> ${res.status}`);
  return parsePoints(await res.text()) ?? [];
}

// --- Pure chart shaping -------------------------------------------------------

export type Trend = { observed: WeatherPoint[]; coming: WeatherPoint[] };

// How long the station's calibration outranks the model (issue #71): the
// forecast is fully pulled to the station at the seam and is its raw self
// again half a day out, where the model genuinely knows better than a
// constant offset.
export const BLEND_HORIZON_S = 12 * 3600;

/** Station-anchored bias correction for the forecast's temperature (issue
 * #71) -- MOS-style calibration, not cosmetic smoothing. The offset between
 * the last observed temperature and the forecast's opinion of that same
 * moment (linearly interpolated; clamped to the first point when the anchor
 * precedes the whole series) is added to each forecast temp, decaying
 * linearly to zero across the horizon. TEMPERATURE ONLY: an additive offset
 * on the station's sheltered wind would go negative. Returns fresh points,
 * never mutates; unchanged input when either side lacks a temperature. */
export function blendForecast(
  observed: WeatherPoint[],
  coming: WeatherPoint[],
  horizonS = BLEND_HORIZON_S,
): WeatherPoint[] {
  const anchor = [...observed]
    .reverse()
    .find((p) => p.temp_f !== null);
  if (!anchor || horizonS <= 0) return coming;
  const temps = coming
    .filter((p) => p.temp_f !== null)
    .sort((a, b) => a.ts - b.ts);
  const first = temps[0];
  if (!first) return coming;
  // The forecast's temperature at the anchor's moment: the first point's
  // when the anchor precedes the series (the usual case -- forecast points
  // live in the future), otherwise interpolated between its neighbors.
  let at = first.temp_f!;
  for (let i = 0; i + 1 < temps.length; i++) {
    const a = temps[i];
    const b = temps[i + 1];
    if (anchor.ts >= a.ts && anchor.ts <= b.ts) {
      at = a.temp_f! +
        ((anchor.ts - a.ts) / (b.ts - a.ts)) * (b.temp_f! - a.temp_f!);
      break;
    }
    if (anchor.ts > b.ts) at = b.temp_f!;
  }
  const offset = anchor.temp_f! - at;
  return coming.map((p) => {
    if (p.temp_f === null) return { ...p };
    // clamped both ways: a point somehow behind the anchor gets the full
    // offset, never an amplified one
    const decay = Math.min(1, Math.max(0, 1 - (p.ts - anchor.ts) / horizonS));
    return { ...p, temp_f: p.temp_f + offset * decay };
  });
}

/** Clip history and forecast to an EXPLICIT window [ts0, ts1] (issue #106),
 * both sorted by ts. `now` is still a parameter, but only as the seam that
 * splits observed from forecast -- it is no longer the window's anchor, which
 * is the whole point: a panned window sits wherever the viewer dragged it.
 *
 * The forecast temps are blendForecast-calibrated against the trail (issue
 * #71), and the last observed point is PREPENDED to `coming` so the two
 * polylines meet at the seam instead of leaving a gap. The stitch only
 * happens when there is actually a forecast in view: a window panned entirely
 * into the past has no seam to close, and stitching there would emit a
 * one-point series that draws nothing and only muddies the contract. */
export function trendSeries(
  history: WeatherPoint[],
  forecast: WeatherPoint[],
  now: number,
  ts0: number,
  ts1: number,
): Trend {
  const byTs = (a: WeatherPoint, b: WeatherPoint) => a.ts - b.ts;
  const observed = history
    .filter((p) => p.ts >= ts0 && p.ts <= Math.min(now, ts1))
    .sort(byTs);
  const coming = blendForecast(
    observed,
    forecast
      .filter((p) => p.ts > Math.max(now, ts0) && p.ts <= ts1)
      .sort(byTs),
  );
  const last = observed[observed.length - 1];
  return {
    observed,
    coming: last && coming.length ? [last, ...coming] : coming,
  };
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

/** Axis ceiling for one station series (issue #51): the windCeil recipe --
 * floor so a quiet day hugs the baseline, round up to a clean step for the
 * label. Three callers (rain, solar, UV in the large view) earn the
 * extraction. */
export function seriesCeil(
  pts: WeatherPoint[],
  value: (p: WeatherPoint) => number | null,
  floor: number,
  step: number,
): number {
  const vals = pts.map(value).filter((v): v is number => v !== null);
  const ceil = Math.ceil(Math.max(0, ...vals) / step) * step;
  // toFixed dodges float dust (0.30000000000000004) in axis labels
  return Number(Math.max(floor, ceil).toFixed(4));
}

/** Pressure axis for the large view: barometric swings are small numbers on
 * a big scale, so pad lightly and hold a minimum span of 0.3 inHg (a steady
 * day reads as steady, the tempRange reasoning). Hundredths, not integers --
 * that IS the unit's resolution. Null when nothing has a pressure. */
export function pressureRange(
  pts: WeatherPoint[],
): { min: number; max: number } | null {
  const vals = pts
    .map((p) => p.pressure_rel_inhg)
    .filter((v): v is number => v !== null);
  if (vals.length === 0) return null;
  let min = Math.min(...vals) - 0.05;
  let max = Math.max(...vals) + 0.05;
  const shortfall = 0.3 - (max - min);
  if (shortfall > 0) {
    min -= shortfall / 2;
    max += shortfall / 2;
  }
  return { min: Number(min.toFixed(2)), max: Number(max.toFixed(2)) };
}

// Tendency, the weather desk's oldest instrument: a series' move over the
// last ~3h of observed trail. The span was born with the barometer (issue
// #51) and generalized in #67 -- temperature, dew point, and humidity read
// the same way, each with its own idea of "steady": ±0.02 inHg is the
// meteorological convention for pressure; the others are set just above
// sensor jitter so the arrow doesn't twitch on noise. The trail must reach
// back at least half the span before we call a trend at all (a fresh
// service has no opinion).
export const TREND_SPAN_S = 3 * 3600;
export const PRESSURE_TREND_EPS_INHG = 0.02;
export const TEMP_TREND_EPS_F = 1.5;
export const DEW_TREND_EPS_F = 1;
export const HUMIDITY_TREND_EPS_PCT = 3;

export type SeriesTrend = "rising" | "falling" | "steady";

/** One series' tendency from the observed window, or null when the trail is
 * too short (or empty of the series) to judge. Compares the newest reading
 * to the one nearest 3h before it -- real reports, never interpolation. */
export function seriesTrend(
  history: WeatherPoint[],
  now: number,
  value: (p: WeatherPoint) => number | null,
  eps: number,
  spanS = TREND_SPAN_S,
): SeriesTrend | null {
  const pts = history
    .filter((p) => value(p) !== null && p.ts <= now)
    .sort((a, b) => a.ts - b.ts);
  const latest = pts[pts.length - 1];
  if (!latest) return null;
  const target = latest.ts - spanS;
  let anchor: WeatherPoint | null = null;
  let anchorD = Infinity;
  for (const p of pts) {
    const d = Math.abs(p.ts - target);
    if (d < anchorD) {
      anchor = p;
      anchorD = d;
    }
  }
  if (!anchor || anchorD > spanS / 2) return null;
  const delta = value(latest)! - value(anchor)!;
  if (delta > eps) return "rising";
  if (delta < -eps) return "falling";
  return "steady";
}

/** The barometer's tendency -- seriesTrend's original caller. */
export function pressureTrend(
  history: WeatherPoint[],
  now: number,
): SeriesTrend | null {
  return seriesTrend(
    history,
    now,
    (p) => p.pressure_rel_inhg,
    PRESSURE_TREND_EPS_INHG,
  );
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

/** The chart point nearest a hovered timestamp -- the tooltip reads real
 * reports, never values interpolated between them. Ties go to the earlier
 * point; null when there is nothing to snap to. */
export function nearestPoint(
  pts: WeatherPoint[],
  ts: number,
): WeatherPoint | null {
  let best: WeatherPoint | null = null;
  let bestD = Infinity;
  for (const p of pts) {
    const d = Math.abs(p.ts - ts);
    if (d < bestD) {
      best = p;
      bestD = d;
    }
  }
  return best;
}

export type TimeTick = { offsetS: number; frac: number };

/** Interior time-axis ticks across [-pastS, +futureS] at stepS spacing, as
 * fractions of the window width. The endpoints and "now" (offset 0) are
 * excluded -- they already carry their own labels and divider. */
export function timeTicks(
  pastS = PAST_S,
  futureS = FUTURE_S,
  stepS = 12 * 3600,
): TimeTick[] {
  const span = pastS + futureS;
  if (span <= 0 || stepS <= 0) return [];
  const ticks: TimeTick[] = [];
  const first = -Math.floor(pastS / stepS) * stepS;
  for (let off = first; off <= futureS; off += stepS) {
    if (off === 0 || off === -pastS || off === futureS) continue;
    ticks.push({ offsetS: off, frac: (off + pastS) / span });
  }
  return ticks;
}

export type DayTick = { ts: number; frac: number; label: string };

/** Local-midnight gridlines for the station view's 6-day window (issue #60):
 * every midnight strictly inside (ts0, ts1), each labeled with the short
 * weekday of the day it begins, lowercased into the telemetry voice. At 144h
 * the offset arithmetic of timeTicks ("+108h") stops meaning anything -- days
 * are the honest unit, and they're the viewer's local days (same reasoning as
 * the epoch-seconds convention: the dashboard formats, nobody parses).
 * Stepping is +36h then re-floor to midnight, so DST's 23/25h days can't
 * skip or double a tick. Empty for a degenerate window.
 *
 * Takes the window explicitly since issue #106 -- it used to derive one from
 * `now`, which only worked while the window was anchored there. A panned
 * window isn't. */
export function dayTicks(ts0: number, ts1: number): DayTick[] {
  if (ts1 <= ts0) return [];
  const ticks: DayTick[] = [];
  const d = new Date(ts0 * 1000);
  d.setHours(0, 0, 0, 0);
  while (d.getTime() / 1000 < ts1) {
    const ts = d.getTime() / 1000;
    if (ts > ts0) {
      ticks.push({
        ts,
        frac: (ts - ts0) / (ts1 - ts0),
        label: d
          .toLocaleDateString(undefined, { weekday: "short" })
          .toLowerCase(),
      });
    }
    d.setTime(d.getTime() + 36 * 3600 * 1000);
    d.setHours(0, 0, 0, 0);
  }
  return ticks;
}

/** Whether the station view's snow strip earns its row (issue #69): local
 * months November through March. Outside the season the strip hides rather
 * than sitting dead for seven months -- the owner-sanctioned exception to
 * the reserve-the-space rule, with a safety valve at the call site: a
 * forecast actually carrying snow shows the strip in any month. */
export function snowSeason(ts: number): boolean {
  const m = new Date(ts * 1000).getMonth(); // 0 = january, local time
  return m >= 10 || m <= 2;
}

export type NightBand = { start: number; end: number };

const DAY_S = 86_400;

/** How far from today the repeated sun times stay honest (issue #106). The
 * bands are built by repeating TODAY's sunrise/sunset at 24h offsets, and day
 * length drifts ~2 minutes per day: a week out that's ~15 minutes, which at
 * the station window's scale (144h across ~1400px) is about 2px -- genuinely
 * invisible. A month out it's an hour, which is visibly wrong, and
 * wrong-but-confident is the one thing this chart doesn't do.
 *
 * So the bands simply STOP at the horizon rather than drifting into fiction.
 * Panned deep into the archive the chart has no night shading, which is an
 * honest "we don't know the sun times back there" -- the browser is only ever
 * told today's, never the station's lat/lon. Computing them per-day for real
 * needs that location on the bus, which is a backend change and its own
 * issue; this is the honest answer until then. */
export const NIGHT_BAND_HORIZON_DAYS = 7;

/** Night intervals (sunset -> next sunrise) intersecting [ts0, ts1], built by
 * repeating today's sun times at 24h offsets, out to the horizon above. Empty
 * when the report has no sun times or they are out of order (garbage in, no
 * bands out).
 *
 * `k` is the repetition index, which IS the band's distance from today in
 * days -- so the horizon needs no clock of its own, and the panel's live
 * window (never more than 2 days from now) can't reach it. */
export function nightBands(
  sunrise: number | null,
  sunset: number | null,
  ts0: number,
  ts1: number,
  horizonDays = NIGHT_BAND_HORIZON_DAYS,
): NightBand[] {
  if (sunrise === null || sunset === null) return [];
  if (sunset <= sunrise || sunset - sunrise >= DAY_S) return [];
  if (ts1 <= ts0) return [];
  const bands: NightBand[] = [];
  const k0 = Math.floor((ts0 - sunset) / DAY_S) - 1;
  const k1 = Math.ceil((ts1 - sunset) / DAY_S) + 1;
  for (let k = k0; k <= k1; k++) {
    if (Math.abs(k) > horizonDays) continue;
    const start = Math.max(sunset + k * DAY_S, ts0);
    const end = Math.min(sunrise + (k + 1) * DAY_S, ts1);
    if (end > start) bands.push({ start, end });
  }
  return bands;
}

/** The eight skies the station can draw (issue #78). Hyphenated keys double
 * as stable identifiers for the SVG components in Dashboard.tsx. */
export type ConditionIconKey =
  | "sunny"
  | "mostly-sunny"
  | "partly-cloudy"
  | "cloudy"
  | "stormy"
  | "raining"
  | "snowing"
  | "windy";

// "Super windy" thresholds: sustained wind or gusts strong enough that wind
// IS the story, whatever the sky is doing behind it.
export const WINDY_SUSTAINED_MPH = 20;
export const WINDY_GUST_MPH = 30;

/** Which icon tells the sky's story right now (issue #78). Precedence runs
 * drama-first: storm > snow > rain > wind > the four cloud-cover states.
 * OWM's `condition` is weather.main ("Clear", "Clouds", "Rain", ...);
 * `description` refines the Clouds group ("few clouds", "overcast clouds").
 * The station's piezo (`raining === 1`) outranks OWM's opinion -- the
 * instrument in the driveway beats the grid cell's word. Null when there is
 * nothing to say (no report, or a report with no sky fields) -- the caller
 * keeps the slot reserved either way (house rule #1). */
export function conditionIcon(
  current: CurrentWeather | null,
): ConditionIconKey | null {
  if (current === null) return null;
  const cond = current.condition;
  if (cond === "Thunderstorm") return "stormy";
  if (cond === "Snow") return "snowing";
  if (cond === "Rain" || cond === "Drizzle" || current.raining === 1)
    return "raining";
  if (
    (current.wind_mph ?? 0) >= WINDY_SUSTAINED_MPH ||
    (current.wind_gust_mph ?? 0) >= WINDY_GUST_MPH ||
    cond === "Squall" ||
    cond === "Tornado"
  )
    return "windy";
  if (cond === "Clear") return "sunny";
  if (cond === "Clouds") {
    const desc = current.description ?? "";
    if (desc === "few clouds") return "mostly-sunny";
    if (desc === "scattered clouds") return "partly-cloudy";
    return "cloudy"; // broken clouds, overcast clouds
  }
  // The rest of OWM's atmosphere group (Mist, Fog, Haze, Smoke, Dust, ...)
  // reads as a grey sky for now -- dedicated icons are a follow-up.
  if (cond !== null) return "cloudy";
  return null;
}

/** Wind bearing -> the 8-point compass a field notebook would use.
 * OpenWeather reports the direction the wind comes FROM. */
export function compass(deg: number | null): string {
  if (deg === null) return "";
  const points = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  return points[Math.round(((deg % 360) + 360) % 360 / 45) % 8];
}
