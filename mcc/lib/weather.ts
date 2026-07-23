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
  // The station's location (issue #111), echoed from OWM's `coord`. Null on a
  // pre-#111 payload (an older weather.py through a deploy) -- the chart then
  // draws no night bands rather than guessing them at depth.
  lat: number | null;
  lon: number | null;
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
      lat: num(o.lat),
      lon: num(o.lon),
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

// --- Precipitation shade (issue #113) -----------------------------------------
// Each precip bar's shade tracks ITS OWN value -- vivid for a sure thing, faint
// for a maybe -- so a 90% Friday reads as a tall vivid slab and a 20% Tuesday as
// a short ghost without hovering. Redundant with the bar's height on purpose:
// the shade makes no claim the height doesn't already make, so it cannot be
// misread and it needs no legend of its own.
//
// Mixed toward --panel rather than by opacity, and that is not a style choice:
// the observed bars are 0.7 viewBox units wide but 5-minute points sit ~0.56
// apart across 144h, so they ALREADY overlap. Stacked translucent bars compound
// into intensities the station never measured (the warning that has been sitting
// in Dashboard.tsx since #56). Solid fills overlap by overwriting.
//
// Direction: vivid = more. On pine-black, fading a colour blends it toward the
// near-black panel, so faint reads DIMMER AND GREYER, not lighter -- which is
// why "solid white for heavy snow, grey for a dusting" and "stronger blue for a
// sure thing" are the same ramp described from opposite ends.

/** Floors for the faint end, in mix-toward-token percent. Not one shared
 * number, because white and blue don't disappear at the same rate: at these
 * floors BOTH land at ~2.1:1 contrast against --panel (measured, not guessed --
 * rain 40% -> 2.14:1, snow 30% -> 2.13:1). The floor is what keeps "unlikely"
 * distinguishable from "no data", which is a distinction this chart never
 * blurs. */
export const RAIN_SHADE_FLOOR = 0.4;
export const SNOW_SHADE_FLOOR = 0.3;
/** The forecast's ceiling stays under the observed trail's full voice: a
 * prediction, however confident, must never shout as loud as a measurement.
 * (Snow has no observed sibling on its strip -- the piezo is snow-blind -- so
 * nothing there is being out-shouted, and heavy snow gets the full white.) */
export const FORECAST_SHADE_CEIL = 0.85;

/** How strongly a precip bar wears its ink: `value` against `max`, floored so
 * the faintest bar still reads, ceilinged by `ceil`. Returns a 0..1 mix weight
 * for the token (1 = the token at full strength, 0 = the bare panel).
 *
 * A non-finite or non-positive `max` yields the floor rather than NaN or a
 * divide-by-zero: an empty strip draws its bars faint, never invisible and
 * never black. Values past `max` clamp -- the ceilings are `seriesCeil`'s job
 * and a downpour past the scale is still just "the most". */
export function precipShade(
  value: number,
  max: number,
  floor: number,
  ceil = 1,
): number {
  if (!Number.isFinite(max) || max <= 0 || !Number.isFinite(value)) return floor;
  const t = Math.min(1, Math.max(0, value / max));
  return floor + (ceil - floor) * t;
}

/** The CSS colour for a precip bar at that weight. oklab so the ramp steps
 * perceptually rather than lurching through sRGB's middle; the browser does the
 * mixing, so --rain and --panel stay the single source of both values. */
export function precipFill(token: string, weight: number): string {
  const pct = Math.round(Math.min(1, Math.max(0, weight)) * 100);
  return `color-mix(in oklab, ${token} ${pct}%, var(--panel))`;
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

/** `bridged` says whether `coming[0]` is the observed-side stitch rather than a
 * forecast step. Only the drawn line wants that point; anything READING the
 * forecast has to skip it, and it can't tell by looking (see tempMarks). */
export type Trend = {
  observed: WeatherPoint[];
  coming: WeatherPoint[];
  bridged: boolean;
};

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
  const bridged = Boolean(last) && coming.length > 0;
  return {
    observed,
    coming: bridged ? [last, ...coming] : coming,
    bridged,
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

// --- Daily highs and lows (issue #113) ----------------------------------------

export type TempMark = { ts: number; temp_f: number; kind: "high" | "low" };

/** Two labelled turning points can't sit closer than this. A diurnal cycle is
 * ~24h, so two "highs" six hours apart means one of them is a shower's wiggle,
 * not an afternoon. Generous enough to survive a flat, weird day. */
export const TEMP_MARK_MIN_GAP_S = 10 * 3600;

/** The peaks and valleys of a forecast series -- the day's high and low, found
 * as TURNING POINTS rather than per-calendar-day min/max.
 *
 * That distinction is the whole design. Bucketing by calendar day is right for
 * SPEECH -- "how cold does Friday get" is a real question with a calendar answer,
 * which is why weather.py's extended_digest does exactly that for Willard. It is
 * wrong for labelling a CURVE, and it fails precisely when the chart is most
 * worth reading: on an evening cold front the temperature falls all night, so
 * Friday's coldest sample is 23:00 (still dropping -- a bucket edge, not a
 * bottom) and Saturday's is 06:00. Two labels seven hours apart on one
 * continuous slide into one valley. Turning points give that valley the single
 * label it deserves, with no special-casing of midnight.
 *
 * Willard and this chart therefore answer different questions and will visibly
 * disagree on exactly that night -- and both are right. That's deliberate.
 *
 * The labelled point is always a REAL forecast point, never an interpolation or
 * an average of two: averaging the two lows would invent a temperature nobody
 * forecast, which is the one thing this chart doesn't do (nearestPoint snaps,
 * linePath splits on null rather than bridging).
 *
 * Endpoints are never marked. The first and last points of a series aren't
 * turning points, they're where the data ran out.
 *
 * `bridged` drops trendSeries' observed-side stitch before any of that, because
 * it is a 5-minute trail sample wearing a forecast series' clothes -- and it is
 * not enough to merely leave it unlabelled. As a NEIGHBOUR it still decides
 * whether the first forecast step is a turning point, and it is the one sample
 * in the trail guaranteed to sit next to that step: on any morning climbing
 * toward its high, the last reading before the seam lands above the first
 * 3-hour step, which reads as a valley the weather never had (issue #103). It
 * gets labelled at the now line, where the trail is steepest and cuts straight
 * through the label. That is exactly the 5-minute noise this function is
 * forecast-only to avoid, smuggled back in one point at a time.
 *
 * Dropping it costs a real dawn valley its label -- the first forecast step has
 * no forecast predecessor to turn against, so it is an endpoint like any other,
 * and the honest answer is silence rather than a guess against a sample from a
 * different series.
 *
 * A run of equal temperatures marks its first sample, so a flat top labels once
 * rather than at every sample across it. */
export function tempMarks(
  pts: WeatherPoint[],
  bridged = false,
  minGapS = TEMP_MARK_MIN_GAP_S,
): TempMark[] {
  // Slice before the null filter: the bridge is coming[0] whatever it measured,
  // so filtering first could drop it and slice a real forecast step instead.
  const s = (bridged ? pts.slice(1) : pts)
    .filter((p) => p.temp_f !== null)
    .sort((a, b) => a.ts - b.ts);
  const marks: TempMark[] = [];
  for (let i = 1; i < s.length - 1; i++) {
    const t = s[i].temp_f!;
    if (s[i - 1].temp_f === t) continue; // mid-plateau: its run already marked
    // Walk past equal neighbours so a plateau is judged by the real slope on
    // each side rather than by its own flat top.
    let b = i + 1;
    while (b < s.length && s[b].temp_f === t) b++;
    if (b >= s.length) continue; // a plateau running off the end isn't a turn
    const prev = s[i - 1].temp_f!;
    const next = s[b].temp_f!;
    if (t > prev && t > next) marks.push({ ts: s[i].ts, temp_f: t, kind: "high" });
    else if (t < prev && t < next) marks.push({ ts: s[i].ts, temp_f: t, kind: "low" });
  }
  // Thin the wiggles. Extrema always ALTERNATE high/low/high, so two highs are
  // never neighbours in this list -- a shallow low sits between them. Comparing
  // only against the previous mark would therefore never catch the pair that
  // matters; the same-kind mark is two back. When two same-kind marks turn out
  // to be one feature, the wiggle between them goes with the loser.
  const kept: TempMark[] = [];
  for (const m of marks) {
    const last = kept[kept.length - 1];
    if (!last) {
      kept.push(m);
      continue;
    }
    const rival = last.kind === m.kind ? last : kept[kept.length - 2];
    if (rival && rival.kind === m.kind && m.ts - rival.ts < minGapS) {
      const better =
        m.kind === "high" ? m.temp_f > rival.temp_f : m.temp_f < rival.temp_f;
      if (better) kept[kept.indexOf(rival)] = m;
      if (rival !== last) kept.splice(kept.indexOf(last), 1); // the wiggle
      continue;
    }
    kept.push(m);
  }
  return kept;
}

export type NightBand = { start: number; end: number };

const DAY_S = 86_400;

const DEG = Math.PI / 180;
const mod360 = (x: number): number => ((x % 360) + 360) % 360;

/** Sunrise and sunset for one calendar day at a location, as epoch seconds --
 * the NOAA solar-position algorithm, pure and deterministic (issue #111). This
 * is what lets the chart shade night at ANY pan depth: the browser used to
 * know only today's sun times (repeated at 24h offsets out to a horizon,
 * issue #106), because `weather/current` carried the times but never the
 * lat/lon needed to recompute them. Now it does.
 *
 * `dayStart` is UTC midnight of the target day; the returned epochs are the
 * true instants, so a sunset past midnight UTC (the normal case in the western
 * hemisphere) correctly lands in the next UTC day. The solar quantities are
 * evaluated once at the day's noon rather than iterated to each event -- the
 * declination barely moves across a day, and the result holds well inside the
 * couple-of-minutes tolerance the night bands care about (verified against
 * published almanac times in the tests). 90.833 deg is the standard
 * sunrise/sunset zenith: the sun's disc radius plus atmospheric refraction.
 *
 * Null when the sun neither rises nor sets that day (polar day/night, |cosH|
 * > 1) -- honestly no ordinary sunrise/sunset to report. The driveway never
 * sees it; nightBands treats such a day as unshaded. */
export function sunTimes(
  lat: number,
  lon: number,
  dayStart: number,
): { sunrise: number; sunset: number } | null {
  const jc = (dayStart + 43_200) / 86_400 + 2440587.5; // Julian day, day-noon
  const t = (jc - 2451545) / 36525; // Julian century
  const l0 = mod360(280.46646 + t * (36000.76983 + t * 0.0003032));
  const m = 357.52911 + t * (35999.05029 - 0.0001537 * t);
  const e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t);
  const c =
    Math.sin(DEG * m) * (1.914602 - t * (0.004817 + 0.000014 * t)) +
    Math.sin(DEG * 2 * m) * (0.019993 - 0.000101 * t) +
    Math.sin(DEG * 3 * m) * 0.000289;
  const omega = 125.04 - 1934.136 * t;
  const appLong = l0 + c - 0.00569 - 0.00478 * Math.sin(DEG * omega);
  const obliq =
    23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60 +
    0.00256 * Math.cos(DEG * omega);
  const decl = Math.asin(Math.sin(DEG * obliq) * Math.sin(DEG * appLong)) / DEG;
  const y = Math.tan((DEG * obliq) / 2) ** 2;
  const eqTime =
    (4 / DEG) *
    (y * Math.sin(2 * DEG * l0) -
      2 * e * Math.sin(DEG * m) +
      4 * e * y * Math.sin(DEG * m) * Math.cos(2 * DEG * l0) -
      0.5 * y * y * Math.sin(4 * DEG * l0) -
      1.25 * e * e * Math.sin(2 * DEG * m)); // minutes
  const cosH =
    Math.cos(DEG * 90.833) / (Math.cos(DEG * lat) * Math.cos(DEG * decl)) -
    Math.tan(DEG * lat) * Math.tan(DEG * decl);
  if (cosH > 1 || cosH < -1) return null; // sun never rises or never sets
  const ha = Math.acos(cosH) / DEG; // half-day arc, degrees
  const noonMin = 720 - 4 * lon - eqTime; // solar noon, UTC minutes past dayStart
  return {
    sunrise: dayStart + (noonMin - 4 * ha) * 60,
    sunset: dayStart + (noonMin + 4 * ha) * 60,
  };
}

/** Night intervals (sunset -> next sunrise) intersecting [ts0, ts1], with each
 * day's sun times COMPUTED from the station's lat/lon (issue #111) rather than
 * repeated from today's -- so the shading is correct at any pan depth, and the
 * 7-day honesty horizon #106 needed is gone with the guesswork that forced it.
 *
 * Empty when lat/lon is unknown (a pre-#111 payload): honest absence stays the
 * fallback, exactly as the horizon's ethos demanded -- no bands beats drifting
 * ones. A day the sun never rises or sets is left unshaded (see sunTimes). */
export function nightBands(
  lat: number | null,
  lon: number | null,
  ts0: number,
  ts1: number,
): NightBand[] {
  if (lat === null || lon === null) return [];
  if (ts1 <= ts0) return [];
  const bands: NightBand[] = [];
  // Pair each day's sunset with the NEXT day's sunrise; pad a day each side so
  // a band straddling either window edge is still generated before clamping.
  const first = Math.floor(ts0 / DAY_S) * DAY_S - DAY_S;
  for (let day = first; day <= ts1 + DAY_S; day += DAY_S) {
    const tonight = sunTimes(lat, lon, day);
    const tomorrow = sunTimes(lat, lon, day + DAY_S);
    if (tonight === null || tomorrow === null) continue;
    const start = Math.max(tonight.sunset, ts0);
    const end = Math.min(tomorrow.sunrise, ts1);
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
