"use client";

// The weather station view and its feed hook, lifted out of the MCC's
// Dashboard.tsx by #628 -- verbatim: nothing here changed but the file.
// It draws nothing from the daemon, frames or history libraries; the
// weather app has only ever needed the weather topics and the bus URL.
// The browser build, NOT the default "mqtt" (Node) build: the Node build can't
// serialize packets in the browser under Turbopack, so its CONNECT never sends
// and the client dies with "connack timeout". See lib/mqtt.browser.d.ts.
import mqtt from "mqtt/dist/mqtt.esm";
import { useEffect, useRef, useState } from "react";
import { busUrl, voiceColor } from "@/lib/bus";
import {
  ConditionIconKey,
  CurrentWeather,
  DEW_TREND_EPS_F,
  DayTick,
  HUMIDITY_TREND_EPS_PCT,
  REPORT_STALE_S,
  STALE_AFTER_S,
  STATION_FUTURE_S,
  STATION_SPAN_S,
  TEMP_TREND_EPS_F,
  WEATHER_CURRENT_TOPIC,
  WEATHER_FORECAST_TOPIC,
  WEATHER_HISTORY_TOPIC,
  WEATHER_REPORT_TOPIC,
  WEATHER_STATUS_TOPIC,
  Trend,
  WeatherPoint,
  WeatherReport,
  WeatherStatus,
  FORECAST_SHADE_CEIL,
  RAIN_SHADE_FLOOR,
  SNOW_SHADE_FLOOR,
  ageText,
  clampWindow,
  compass,
  conditionIcon,
  dayTicks,
  fetchArchive,
  linePath,
  mergePoints,
  precipFill,
  precipShade,
  tempMarks,
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
  trendSeries,
  weatherInstrumentsAvailable,
  windCeil,
  windowEdgeLabel,
} from "@/lib/weather";


/* --- Condition icons (issue #78): the sky at a glance -----------------------
   Eight hand-drawn glyphs in the house line-work style, sized to ride beside
   the temperature readout. Structure strokes inherit currentColor so the
   stale and off-air states dim them along with the text; the subject of each
   sky (sun disc, rain, lightning) wears its accent only while the report is
   fresh. Clouds fill with the panel color so an overlap occludes what sits
   behind it instead of tangling line-work. */

const CONDITION_LABEL: Record<ConditionIconKey, string> = {
  sunny: "sunny",
  "mostly-sunny": "mostly sunny",
  "partly-cloudy": "partly cloudy",
  cloudy: "cloudy",
  stormy: "stormy",
  raining: "raining",
  snowing: "snowing",
  windy: "super windy",
};

// One cumulus, drawn once; every cloudy sky places it by transform.
const CLOUD_PATH = "M35 38H18a14 14 0 1 1 13.42-18h3.58a9 9 0 1 1 0 18Z";

/** Eight rays around a sun disc -- shared by the three sunny-ish skies. */
function SunRays({
  cx,
  cy,
  from,
  to,
}: {
  cx: number;
  cy: number;
  from: number;
  to: number;
}) {
  return (
    <g>
      {[0, 45, 90, 135, 180, 225, 270, 315].map((a) => (
        <line
          key={a}
          x1={cx}
          y1={cy - to}
          x2={cx}
          y2={cy - from}
          transform={`rotate(${a} ${cx} ${cy})`}
        />
      ))}
    </g>
  );
}

function ConditionGlyph({
  icon,
  size,
  live,
}: {
  icon: ConditionIconKey;
  size: number;
  live: boolean;
}) {
  const sun = live ? "var(--turkey)" : "currentColor";
  // Rain draws in the weather ink (issue #113), not the live-lamp green it
  // used to borrow. Snow keeps currentColor -- it is already the ink the
  // glyph's structure wears, and white IS snow's colour.
  const rain = live ? "var(--rain)" : "currentColor";
  const common = {
    viewBox: "0 0 48 48",
    width: size,
    height: size,
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 2.4,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
  switch (icon) {
    case "sunny":
      return (
        <svg {...common} stroke={sun}>
          <circle cx="24" cy="24" r="9.5" />
          <SunRays cx={24} cy={24} from={14.5} to={19} />
        </svg>
      );
    case "mostly-sunny":
      return (
        <svg {...common}>
          <g stroke={sun}>
            <circle cx="19" cy="19" r="8" />
            <SunRays cx={19} cy={19} from={11.5} to={15.5} />
          </g>
          <g transform="translate(16 20) scale(0.58)">
            <path d={CLOUD_PATH} fill="var(--panel)" strokeWidth={2.4 / 0.58} />
          </g>
        </svg>
      );
    case "partly-cloudy":
      return (
        <svg {...common}>
          <g stroke={sun}>
            <circle cx="17" cy="15" r="7" />
            <SunRays cx={17} cy={15} from={10} to={13.5} />
          </g>
          <g transform="translate(4.5 12) scale(0.76)">
            <path d={CLOUD_PATH} fill="var(--panel)" strokeWidth={2.4 / 0.76} />
          </g>
        </svg>
      );
    case "cloudy":
      return (
        <svg {...common}>
          <g transform="translate(17.5 3) scale(0.5)" strokeOpacity={0.55}>
            <path d={CLOUD_PATH} strokeWidth={2.4 / 0.5} />
          </g>
          <g transform="translate(0 5) scale(0.92)">
            <path d={CLOUD_PATH} fill="var(--panel)" strokeWidth={2.4 / 0.92} />
          </g>
        </svg>
      );
    case "stormy":
      return (
        <svg {...common}>
          <g transform="translate(3.5 -5) scale(0.85)">
            <path d={CLOUD_PATH} fill="var(--panel)" strokeWidth={2.4 / 0.85} />
          </g>
          <path
            d="M26 25 L18.5 37 H23.5 L21.5 46 L31.5 33.5 H26 L29 25 Z"
            fill={sun}
            stroke="none"
          />
        </svg>
      );
    case "raining":
      return (
        <svg {...common}>
          <g transform="translate(3.5 -3.5) scale(0.85)">
            <path d={CLOUD_PATH} fill="var(--panel)" strokeWidth={2.4 / 0.85} />
          </g>
          <g stroke={rain}>
            <line x1="15.5" y1="33" x2="13" y2="41" />
            <line x1="23.5" y1="35" x2="21" y2="43" />
            <line x1="31.5" y1="33" x2="29" y2="41" />
          </g>
        </svg>
      );
    case "snowing":
      return (
        <svg {...common}>
          <g transform="translate(3.5 -3.5) scale(0.85)">
            <path d={CLOUD_PATH} fill="var(--panel)" strokeWidth={2.4 / 0.85} />
          </g>
          {/* three six-armed flakes: three crossing lines each */}
          <g strokeWidth={1.7}>
            {(
              [
                [15, 37],
                [24, 41],
                [33, 37],
              ] as const
            ).map(([x, y]) =>
              [0, 60, 120].map((a) => (
                <line
                  key={`${x}-${a}`}
                  x1={x}
                  y1={y - 3.4}
                  x2={x}
                  y2={y + 3.4}
                  transform={`rotate(${a} ${x} ${y})`}
                />
              )),
            )}
          </g>
        </svg>
      );
    case "windy":
      return (
        <svg {...common}>
          <path d="M19.6 8.8A4 4 0 1 1 22 16H4" />
          <path d="M35 16a5 5 0 1 1 4 8H4" />
          <path d="M25.6 39.2A4 4 0 1 0 28 32H4" />
        </svg>
      );
  }
}

/** The icon's slot beside a temperature readout. Fixed-size whether or not a
 * sky is known (house rule #1): no report means an empty reservation, never
 * a collapsed one. */
function ConditionBadge({
  icon,
  size,
  live,
}: {
  icon: ConditionIconKey | null;
  size: number;
  live: boolean;
}) {
  return (
    <span
      className={`flex shrink-0 items-center justify-center ${live ? "text-ink" : "text-inkfaint"}`}
      style={{ width: size, height: size }}
      role={icon ? "img" : undefined}
      aria-label={icon ? CONDITION_LABEL[icon] : undefined}
    >
      {icon && <ConditionGlyph icon={icon} size={size} live={live} />}
    </span>
  );
}

const WEATHER_TICK_MS = 60_000;

/** hh:mm from epoch seconds, viewer-local. Only ever rendered after bus data
 * arrives (client-side), so it can't cause a hydration mismatch. */
function clock(ts: number | null): string {
  if (ts === null) return "—";
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "fri 14:05" -- weekday + clock, viewer-local. The chart spans three days,
 * so the hover readout names the day, not just the hour. */
function dayClock(ts: number): string {
  const day = new Date(ts * 1000)
    .toLocaleDateString(undefined, { weekday: "short" })
    .toLowerCase();
  return `${day} ${clock(ts)}`;
}

/** "—" for a missing reading, the integer otherwise. Shared by the panel and
 * the station view (issue #51) -- a hole in the data is an em-dash, never
 * NaN°. */
const wxRound = (v: number | null) => (v === null ? "—" : Math.round(v));

/** Fixed decimals or the em-dash -- rain and pressure read in hundredths. */
const wxFixed = (v: number | null, digits: number) =>
  v === null ? "—" : v.toFixed(digits);

/** The barometer's tendency as a compass-adjacent glyph. Null trend (trail
 * too short) renders as empty -- the slot is reserved by the row itself. */
const TREND_GLYPH = { rising: "↗", falling: "↘", steady: "→" } as const;

/** The weather feed (issue #142): one bus client's worth of retained
 * weather topics plus the staleness/presence judgements, shared by the
 * Weather Post panel and the standalone /weather page. Each caller runs its
 * own client — the panels' self-contained idiom; Mosquitto shrugs at a
 * second WebSocket — so mounting the page never entangles the dashboard. */
export function useWeatherFeed() {
  const [current, setCurrent] = useState<CurrentWeather | null>(null);
  const [history, setHistory] = useState<WeatherPoint[]>([]);
  const [forecast, setForecast] = useState<WeatherPoint[]>([]);
  // Willard's on-air segment (issue #45): retained LLM narration, absent
  // entirely when the weather service runs without an Ollama (the topic is
  // simply never published).
  const [report, setReport] = useState<WeatherReport | null>(null);
  // Presence (issue #31): retained "online"/"offline" from weather/status.
  // null = no retained status on the broker (pre-#31 service), which falls
  // back to judging Willard purely by report freshness.
  const [status, setStatus] = useState<WeatherStatus | null>(null);
  const [busUp, setBusUp] = useState(false);
  // "now" lives in state, never Date.now() in render (the house hydration
  // rule) -- a slow tick marches the chart window and staleness check forward
  // between reports.
  const [now, setNow] = useState<number | null>(null);

  useEffect(() => {
    const tick = () => setNow(Math.floor(Date.now() / 1000));
    // Deferred a microtask, the StationRecords trick: no synchronous setState
    // path from the effect body, zero visible cost.
    queueMicrotask(tick);
    const id = setInterval(tick, WEATHER_TICK_MS);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    // Its own bus client, same shape as FieldJournal's: each panel stays
    // self-contained (Mosquitto shrugs at a second WebSocket) rather than
    // threading a shared client through two unrelated components. All four
    // weather topics are RETAINED, so the broker replays the latest report,
    // forecast, 48h window, and presence the moment we subscribe -- weather
    // has no HTTP.
    const url = busUrl(
      window.location.hostname,
      process.env.NEXT_PUBLIC_MERLE_MQTT_WS,
    );
    const client = mqtt.connect(url, { reconnectPeriod: 3000 });
    client.on("connect", () => {
      setBusUp(true);
      client.subscribe([
        WEATHER_CURRENT_TOPIC,
        WEATHER_FORECAST_TOPIC,
        WEATHER_HISTORY_TOPIC,
        WEATHER_REPORT_TOPIC,
        WEATHER_STATUS_TOPIC,
      ]);
    });
    client.on("close", () => setBusUp(false));
    // Mandatory: an unhandled mqtt.js "error" throws and wedges its reconnect
    // loop (see FieldJournal, which also surfaces the reason for both panels).
    client.on("error", () => {});
    client.on("message", (topic, payload) => {
      const text = payload.toString();
      if (topic === WEATHER_CURRENT_TOPIC) {
        const report = parseCurrent(text);
        if (report) {
          setCurrent(report);
          setNow(Math.floor(Date.now() / 1000));
        }
      } else if (topic === WEATHER_FORECAST_TOPIC) {
        setForecast(parsePoints(text) ?? []);
      } else if (topic === WEATHER_HISTORY_TOPIC) {
        setHistory(parsePoints(text) ?? []);
      } else if (topic === WEATHER_REPORT_TOPIC) {
        setReport(parseReport(text));
      } else if (topic === WEATHER_STATUS_TOPIC) {
        setStatus(parseStatus(text));
      }
    });
    return () => {
      client.end(true);
    };
  }, []);

  // Presence beats freshness: a retained "offline" (graceful stop or the
  // Last Will after a crash) is announced the moment it happens, while
  // staleness needs 3 missed polls to accrue. No retained status at all
  // (pre-#31 service) leaves the freshness judgement in charge.
  const offline = status === "offline";
  // A report older than 3 missed polls is presented as stale, never as now.
  const stale =
    current !== null && now !== null && current.ts < now - STALE_AFTER_S;
  const reporting = current !== null && !stale && !offline;
  // The segment ages on its own clock (broadcasts are ~30 min apart, polls
  // 10): past REPORT_STALE_S it's history, not news, and the between-
  // broadcasts state takes over.
  const onAir =
    report !== null && now !== null && now - report.ts <= REPORT_STALE_S;

  // The barometer's tendency, judged from the observed trail (issue #51).
  const baroTrend = now !== null ? pressureTrend(history, now) : null;

  return {
    current,
    history,
    forecast,
    report,
    now,
    busUp,
    offline,
    stale,
    reporting,
    onAir,
    baroTrend,
  };
}


/* --- The Station View (issue #51): the Weather Post, writ large ------------- */

// Large-chart geometry. Same stretched-viewBox rules as the panel chart
// (strokes carry vector-effect), but its own window (issue #60): 24h back +
// the full 5-day forecast forward, "now" at the 1/6 mark. Gridlines are the
// viewer's local midnights (dayTicks), not 12h offsets -- at 144h, days are
// the honest unit. The strips underneath share ts0/ts1 with the main chart,
// so every timestamp lines up vertically across all four.
const WXL_W = 960;
const WXL_H = 240;
const WXL_STRIP_H = 72;
// How much archive to ask for at a time when panning past what we hold
// (issue #106). A week of 5-minute rows is ~2000 points -- a chunk big enough
// that a station outage doesn't read as the end of the record, and small
// enough to land before the viewer notices. History is immutable, so each
// chunk is fetched exactly once.
const ARCHIVE_CHUNK_S = 7 * 86400;
// A press that travels less than this is a tap, not a drag: the crosshair
// placement a touchscreen can't express as hover.
const TAP_SLOP_PX = 4;
// OpenWeather's forecast step: each point's rain_rate_inhr (issue #56) is the
// average over the 3 hours ENDING at its ts ("volume for last 3 hours"), so
// its ghost bar spans that window on the strip.
const WX_FORECAST_STEP_S = 3 * 3600;

/** The circled i riding after a WxStat label (issue #63) -- the house
 * line-work, sized to sit inside the stamp line. */
function InfoIcon() {
  return (
    <svg
      viewBox="0 0 12 12"
      width="11"
      height="11"
      aria-hidden="true"
      className="shrink-0"
    >
      <circle
        cx="6"
        cy="6"
        r="5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1"
      />
      <line
        x1="6"
        y1="5.3"
        x2="6"
        y2="8.6"
        stroke="currentColor"
        strokeWidth="1.3"
      />
      <circle cx="6" cy="3.3" r="0.8" fill="currentColor" />
    </svg>
  );
}

/** The wind cell's compass rose (issue #63): a ring with cardinal ticks and
 * a needle rotated to the bearing the wind comes FROM (matching the letter
 * beside it -- a "W" wind points the needle west). No bearing draws the
 * ring and ticks alone, reserving the same footprint (house rule #1). */
function CompassGlyph({ deg }: { deg: number | null }) {
  return (
    <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true">
      <circle
        cx="8"
        cy="8"
        r="7"
        fill="none"
        stroke="var(--line-bright)"
        strokeWidth="1"
      />
      {[0, 90, 180, 270].map((t) => (
        <line
          key={t}
          x1="8"
          y1="1"
          x2="8"
          y2="2.6"
          stroke="var(--ink-faint)"
          strokeWidth="1"
          transform={`rotate(${t} 8 8)`}
        />
      ))}
      {deg !== null && (
        <path
          d="M8 2.8 L10.1 10.2 L8 8.7 L5.9 10.2 Z"
          fill="var(--squirrel)"
          transform={`rotate(${deg} 8 8)`}
        />
      )}
    </svg>
  );
}

/** One labeled reading in the hero grid: stamped label, instrument-sized
 * value, the unit riding quietly after it, `aside` riding to their right
 * (the wind cell's compass). `info` (issue #63) appends a quiet circled-i
 * whose hover/focus reveals a field-manual note in the readout chip's
 * dress -- pure CSS, an absolute overlay (house rule #1: revealing it can
 * never shift the grid), focusable so keyboards and touch taps get it
 * too. */
function WxStat({
  label,
  value,
  unit,
  sub,
  info,
  aside,
}: {
  label: string;
  value: string | number;
  unit?: string;
  sub?: React.ReactNode;
  info?: string;
  aside?: React.ReactNode;
}) {
  return (
    <div className="border-l border-line pl-3">
      <div className="stamp flex items-center gap-1.5 text-[10px] text-inkfaint">
        {label}
        {info && (
          <span
            tabIndex={0}
            aria-label={`about ${label}`}
            className="group relative -my-1 inline-flex cursor-help rounded-sm py-1 text-inkfaint/70 outline-none transition-colors hover:text-inkdim focus-visible:text-inkdim"
          >
            <InfoIcon />
            <span
              role="tooltip"
              className="pointer-events-none invisible absolute left-1/2 top-full z-20 mt-1.5 w-60 -translate-x-1/2 rounded-sm border border-linebright bg-panel2 px-2.5 py-2 text-[11px] font-normal normal-case leading-relaxed tracking-normal text-inkdim opacity-0 transition-opacity duration-150 group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100"
            >
              {info}
            </span>
          </span>
        )}
      </div>
      <div className="flex items-center gap-2 text-xl tabular-nums text-ink">
        <span>
          {value}
          {unit && <span className="ml-1 text-xs text-inkdim">{unit}</span>}
        </span>
        {aside}
      </div>
      {/* min-h reserves the sub-line so the grid rows stay ranked even when
          a reading has nothing extra to say (house rule #1). */}
      <div className="min-h-[16px] text-[11px] text-inkdim">{sub}</div>
    </div>
  );
}

/** Segment meter for the WH90's battery (0-5) and radio signal (0-4) -- gear
 * gauge, not percentage bar. Unknown level draws all sockets empty. */
function SegMeter({
  n,
  of,
  tone = "var(--led)",
}: {
  n: number | null;
  of: number;
  tone?: string;
}) {
  return (
    <span className="inline-flex items-center gap-0.5">
      {Array.from({ length: of }, (_, i) => (
        <span
          key={i}
          className="h-1.5 w-2.5 rounded-[1px]"
          style={
            n !== null && i < n
              ? { backgroundColor: tone }
              : { backgroundColor: "var(--panel-2)", border: "1px solid var(--line)" }
          }
        />
      ))}
    </span>
  );
}

/** One instrument strip under the main chart: its own svg sharing the time
 * axis, the now-divider, and faint tick rules for alignment. The series itself
 * comes in as svg children.
 *
 * The label sits in the forecast half because the station's instruments mostly
 * leave the future blank -- but that is no longer a reason to expect an EMPTY
 * corner, and it was the reason for a long time. Issue #56 noted the rain strip
 * as the one exception whose ghosts "stay well below the label's corner"; #65
 * then rescaled those ghosts to CHANCE on a fixed 0-100% strip, where a
 * confident Friday is a full-height bar growing straight into it. The label
 * stops relying on an empty corner and brings its own scrim instead (#113). */
function WxStrip({
  label,
  scale,
  ticks,
  nowFrac,
  children,
}: {
  label: string;
  scale: string;
  /** gridline positions as window fractions -- the midnights of dayTicks,
   *  computed once by the station view so all four charts agree */
  ticks: number[];
  /** the seam's position in the window, or null once the viewer has panned
   *  far enough back that "now" isn't on the chart at all (issue #106) */
  nowFrac: number | null;
  children: React.ReactNode;
}) {
  return (
    <div className="relative mt-1 h-[72px] w-full border-t border-line/60">
      <svg
        viewBox={`0 0 ${WXL_W} ${WXL_STRIP_H}`}
        preserveAspectRatio="none"
        className="h-full w-full"
        aria-hidden="true"
      >
        {ticks.map((frac) => (
          <line
            key={frac}
            x1={frac * WXL_W}
            y1={0}
            x2={frac * WXL_W}
            y2={WXL_STRIP_H}
            stroke="var(--line)"
            opacity="0.5"
            vectorEffect="non-scaling-stroke"
          />
        ))}
        {nowFrac !== null && (
          <line
            x1={nowFrac * WXL_W}
            y1={0}
            x2={nowFrac * WXL_W}
            y2={WXL_STRIP_H}
            stroke="var(--line-bright)"
            strokeDasharray="2 4"
            vectorEffect="non-scaling-stroke"
          />
        )}
        {children}
      </svg>
      {/* The heading and the scale whisper both sit OVER the data now, not
          beside it (issue #113). The corner used to be reliably empty -- #56
          put the label there precisely because the station leaves the future
          blank -- but #65 rescaled the forecast ghosts to chance on a fixed
          0-100% strip, so a confident Friday is a full-height bar growing
          straight into this corner. The premise is gone, so the label stops
          relying on it: it wears the panel as a scrim and reads at station-view
          size instead of whispering at 10px in the palest ink on the board.
          Both stay absolute overlays, so none of this can shift the chart. */}
      <span className="stamp pointer-events-none absolute right-1 top-1 rounded-sm bg-panel/80 px-1 py-px text-[11px] text-inkdim">
        {label}
      </span>
      <span className="pointer-events-none absolute left-1 top-0.5 rounded-sm bg-panel/80 px-1 py-px text-[11px] tabular-nums text-inkdim">
        {scale}
      </span>
    </div>
  );
}

/** The station chart's calendar axis (#60): weekday labels centered over
 * their day's slice between midnight gridlines, endpoints and the now stamp
 * anchoring the window. Narrow partial days at the window's edges and
 * anything shadowing the now stamp stay quiet -- the hover chip names every
 * point anyway. Rendered twice since issue #65's follow-up: once above the
 * chart stack and once below, so neither end of a tall chart leaves the
 * reader guessing which day they're over. */
function WxTimeAxis({
  days,
  nowFrac,
  leftLabel,
  rightLabel,
  className,
}: {
  days: DayTick[];
  /** null once "now" is off the chart -- a window panned into the past has
   *  no seam to stamp, and the corners say how far back it sits (#106) */
  nowFrac: number | null;
  leftLabel: string;
  rightLabel: string;
  className: string;
}) {
  return (
    <div className={`relative h-4 text-[10px] text-inkfaint ${className}`}>
      <span className="absolute left-0">{leftLabel}</span>
      {days.map((t, i) => {
        const end = days[i + 1]?.frac ?? 1;
        const center = (t.frac + end) / 2;
        if (end - t.frac < 0.05) return null;
        if (nowFrac !== null && Math.abs(center - nowFrac) < 0.04) return null;
        if (center > 0.96) return null;
        return (
          <span
            key={t.ts}
            className="stamp absolute -translate-x-1/2"
            style={{ left: `${center * 100}%` }}
          >
            {t.label}
          </span>
        );
      })}
      {nowFrac !== null && (
        <span
          className="stamp absolute -translate-x-1/2 text-inkdim"
          style={{ left: `${nowFrac * 100}%` }}
        >
          now
        </span>
      )}
      <span className="absolute right-0">{rightLabel}</span>
    </div>
  );
}

export function WeatherStationView({
  current,
  history,
  forecast,
  report,
  now,
  reporting,
  offline,
  onAir,
  baroTrend,
  weatherJournal = false,
}: {
  current: CurrentWeather | null;
  history: WeatherPoint[];
  forecast: WeatherPoint[];
  report: WeatherReport | null;
  now: number | null;
  reporting: boolean;
  offline: boolean;
  onAir: boolean;
  baroTrend: "rising" | "falling" | "steady" | null;
  /** This box's field journal carries weather (#655): decided on the
   *  server per box, so the "why weather?" note is fixed from first paint. */
  weatherJournal?: boolean;
}) {
  // A page section under the Aviary masthead since #652. Until then this was
  // the MCC dashboard's full-screen overlay, with the Live Watch contract
  // (Escape closes, body scroll locks); the page scrolls now, the masthead
  // navigates, and nothing here closes. Still no requestFullscreen -- a page
  // of instruments, not a video, and it should scroll on small screens.

  // Hover scrub, the panel chart's contract: pointer x as a fraction of the
  // chart stack's width; the crosshair spans main chart and strips alike.
  const [hoverFrac, setHoverFrac] = useState<number | null>(null);

  // --- The pannable window (issue #106) -----------------------------------
  // null means LIVE: the window tracks `now` and sits exactly where it always
  // has (24h back, 120h ahead, "now" at the 1/6 mark). Once panned it holds
  // an ABSOLUTE right edge instead -- a window the viewer dragged to last
  // Tuesday must stay on last Tuesday, not creep rightward as `now` ticks
  // forward underneath it. It doubles as the snap-back control's state: home
  // is null, not a timestamp to recompute.
  const [windowEnd, setWindowEnd] = useState<number | null>(null);
  // Whatever the archive has handed over so far, additive to the retained bus
  // window and never a replacement for it (#105 is a second source, not the
  // trail's source). History is immutable once past, so a range fetched once
  // is never refetched -- this only ever grows.
  const [archived, setArchived] = useState<WeatherPoint[]>([]);
  // The wall: set when the archive says there is nothing older, or when it
  // can't be reached at all. Both mean the same thing to a viewer -- the
  // record stops here -- and both must stop us asking, or a drag pinned at
  // the wall would refetch forever.
  const [exhausted, setExhausted] = useState(false);
  const drag = useRef<{ x: number; ts1: number; moved: boolean } | null>(null);
  const dragging = useRef(false);
  const inFlight = useRef(0);
  // The floor we last asked below. A ref, not state, and this is the whole
  // reason it exists: a chunk resolving does not re-render synchronously, so
  // a pointermove landing between the fetch settling and React catching up
  // still sees the previous render's `oldestKnown` and would ask for the same
  // week a second time. inFlight can't catch that -- it's already back to 0.
  const askedBelow = useRef<number | null>(null);

  const live = windowEnd === null;
  const newest = (now ?? 0) + STATION_FUTURE_S;
  const ts1 = windowEnd ?? newest;
  const ts0 = ts1 - STATION_SPAN_S;

  // The trail is the bus window plus the archive, deduped: the two overlap by
  // design (the archive holds the same rows the window does).
  const trail = mergePoints(history, archived);
  const oldestKnown = trail.length ? trail[0].ts : ts0;

  const trend: Trend =
    now !== null
      ? trendSeries(trail, forecast, now, ts0, ts1)
      : { observed: [], coming: [], bridged: false };
  const allPts = [...trend.observed, ...trend.coming];
  const nights = nightBands(
    current?.lat ?? null,
    current?.lon ?? null,
    ts0,
    ts1,
  );
  // Local midnights, the window's gridlines and axis labels alike (#60),
  // now over whatever window the viewer dragged to rather than `now`'s.
  const days = now !== null ? dayTicks(ts0, ts1) : [];
  const dayFracs = days.map((d) => d.frac);
  // Where "now" falls in the window -- the 1/6 mark while live, off the left
  // edge entirely once panned back a few days. Null means the seam isn't in
  // view and nothing should draw it.
  const nowFrac =
    now !== null && now >= ts0 && now <= ts1 ? (now - ts0) / (ts1 - ts0) : null;

  // --- The axes settle when the gesture does (issue #106) -----------------
  // Every scale here is computed from the points in view, so a drag would
  // rescale them on every frame and the chart would breathe -- the
  // no-layout-shift rule violated inside the chart's own frame. They freeze
  // at whatever is on screen when a drag starts and settle once when the
  // gesture is over AND any fetch it kicked off has landed: one controlled
  // rescale when the viewer is done moving, never a jitter under the finger
  // and never a jump the moment a fetch returns.
  const liveAxes = {
    range: tempRange(allPts),
    windMax: windCeil(allPts),
    rainMax: seriesCeil(trend.observed, (p) => p.rain_rate_inhr, 0.25, 0.25),
    snowMax: seriesCeil(trend.coming, (p) => p.snow_3h_in, 1, 0.5),
    solarMax: seriesCeil(trend.observed, (p) => p.solar_wm2, 200, 100),
    presRange: pressureRange(trend.observed),
  };
  const [frozenAxes, setFrozenAxes] = useState<typeof liveAxes | null>(null);
  const { range, windMax, rainMax, snowMax, solarMax, presRange } =
    frozenAxes ?? liveAxes;
  const hasChart = now !== null && range !== null && allPts.length > 1;

  // The forecast's peaks and valleys (issue #113): what a viewer actually wants
  // to know -- how hot Friday gets, how cold tonight -- without hovering point
  // to point. Turning points, not per-calendar-day min/max, so a valley that
  // spans midnight gets ONE label; see tempMarks. Forecast only: the observed
  // trail is 5-minute data where every passing cloud is a turning point -- and
  // `bridged` keeps the seam stitch from being the one cloud that gets through.
  const marks = hasChart ? tempMarks(trend.coming, trend.bridged) : [];

  // --- Panning ------------------------------------------------------------
  // The first drag interaction in the codebase, so it sets the pattern:
  // pointer events throughout (one path for mouse and touch, as the hover
  // scrub already does), setPointerCapture so a drag that leaves the chart
  // doesn't strand mid-gesture, and touch-action: pan-y on the surface so a
  // horizontal drag pans while a vertical one still scrolls the overlay.
  const settleAxes = () => {
    if (inFlight.current === 0 && !dragging.current) setFrozenAxes(null);
  };

  /** Ask the archive for the chunk older than everything we hold. Keyed off
   * what the viewer ASKED for, never the clamped result: the clamp pins ts0
   * at the wall, so waiting for the clamped window to cross it would mean the
   * request never fires at all. Nothing here fetches while the window sits
   * inside the retained trail -- the first day of panning is already on the
   * wire and costs no network. */
  const askArchive = (wantTs0: number) => {
    if (exhausted || inFlight.current > 0) return;
    if (!trail.length || wantTs0 >= oldestKnown) return;
    // Already asked below this floor: the answer is in flight or already
    // merged, and history doesn't change once past. A range is fetched once.
    if (askedBelow.current !== null && oldestKnown >= askedBelow.current) return;
    askedBelow.current = oldestKnown;
    inFlight.current += 1;
    fetchArchive(oldestKnown - ARCHIVE_CHUNK_S, oldestKnown - 1)
      .then((pts) => {
        // Nothing older exists: this is the archive's first reading, the end
        // of the record rather than a gap in it. Stop here and stop asking.
        if (pts.length === 0) setExhausted(true);
        else setArchived((a) => mergePoints(a, pts));
      })
      // Route unreachable or unset MERLE_WEATHER_DB: the chart is exactly the
      // chart it was before the archive existed, and panning stops at the
      // retained window's edge. Never a thrown error at a viewer.
      .catch(() => setExhausted(true))
      .finally(() => {
        inFlight.current -= 1;
        settleAxes();
      });
  };

  const panTo = (wantTs1: number) => {
    const c = clampWindow(
      wantTs1 - STATION_SPAN_S,
      wantTs1,
      oldestKnown,
      newest,
    );
    // Dragged back to the right edge: return to LIVE rather than freezing an
    // absolute end that `now` would immediately outrun.
    setWindowEnd(c.ts1 >= newest ? null : c.ts1);
    askArchive(wantTs1 - STATION_SPAN_S);
  };

  const fracAt = (e: React.PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return r.width > 0
      ? Math.min(1, Math.max(0, (e.clientX - r.left) / r.width))
      : null;
  };

  // The barometer's tendency treatment, extended to the rest of the desk
  // (issue #67). Temperature and humidity mostly ride the sun -- honest,
  // just diurnal; a moving dew point is the interesting one, it means the
  // air itself is changing. Null (short trail) renders as reserved blank.
  const tempTrend =
    now !== null
      ? seriesTrend(history, now, (p) => p.temp_f, TEMP_TREND_EPS_F)
      : null;
  const dewTrend =
    now !== null
      ? seriesTrend(history, now, (p) => p.dew_point_f, DEW_TREND_EPS_F)
      : null;
  const humidityTrend =
    now !== null
      ? seriesTrend(history, now, (p) => p.humidity_pct, HUMIDITY_TREND_EPS_PCT)
      : null;

  // The strips chart the observed trail -- the forecast carries none of
  // these series, and an honest chart leaves the future blank. Two
  // exceptions: the rain strip's future half charts the CHANCE of
  // precipitation (issue #65 -- pop is what a viewer plans around; the
  // forecast's volumes read as slivers on the piezo's scale), and the snow
  // strip is forecast-only (the piezo is snow-blind, so its observed half
  // is honestly blank forever). The rain ceiling is observed-only again --
  // the future half rides its own fixed 0-100% scale. Their ceilings ride
  // the settling rule with every other axis (issue #106).
  const observed = trend.observed;
  // The snow strip goes seasonal (issue #69, owner's call): hidden April
  // through October rather than sitting dead for seven months -- but a
  // forecast actually carrying snow shows it in any month. The valve only
  // ever ADDS the row; the gate never hides real data.
  const showSnow =
    now !== null &&
    (snowSeason(now) ||
      trend.coming.some((p) => (p.snow_3h_in ?? 0) > 0));

  const hovered =
    hasChart && hoverFrac !== null
      ? nearestPoint(allPts, ts0 + hoverFrac * (ts1 - ts0))
      : null;
  const hoveredFrac = hovered ? (hovered.ts - ts0) / (ts1 - ts0) : 0;
  // The station-series line of the readout: what the hovered point actually
  // measured (observed side), or -- the issue #56 exception -- what the
  // forecast expects to fall, plus its chance (and snow, #65).
  const hoveredExtras = hovered
    ? [
        // >= half a display digit, so a drizzle that rounds to "0.00/hr"
        // (or "0.0 in") stays quiet instead of printing a zero
        hovered.rain_rate_inhr !== null && hovered.rain_rate_inhr >= 0.005
          ? `rain ${hovered.rain_rate_inhr.toFixed(2)}/hr`
          : null,
        hovered.snow_3h_in !== null && hovered.snow_3h_in >= 0.05
          ? `snow ${hovered.snow_3h_in.toFixed(1)} in`
          : null,
        now !== null &&
        hovered.ts > now &&
        hovered.pop !== null &&
        hovered.pop >= 0.05
          ? `chance ${Math.round(hovered.pop * 100)}%`
          : null,
        hovered.pressure_rel_inhg !== null
          ? `baro ${hovered.pressure_rel_inhg.toFixed(2)}`
          : null,
        hovered.solar_wm2 !== null
          ? `solar ${Math.round(hovered.solar_wm2).toLocaleString("en-US")}`
          : null,
        hovered.uv_index !== null && hovered.uv_index >= 1
          ? `uv ${Math.round(hovered.uv_index)}`
          : null,
      ].filter(Boolean)
    : [];

  const battery = current?.station_battery ?? null;
  const batteryTone =
    battery !== null && battery <= 1
      ? "var(--chipmunk)"
      : battery === 2
        ? "var(--turkey)"
        : "var(--led)";
  // Same cast accent as the panel segment (issue #92).
  const willardVoice = voiceColor("Willard");
  // OWM-only station mode (#428): no gateway/WH90 — dim hardware chrome.
  const instruments = weatherInstrumentsAvailable(current);

  return (
    <section aria-label="Weather station view">
      {/* The frame (max-w, padding) is the Aviary page's since #652, so every
          tab shares it; the right rail is fixed 320px, so the extra room all
          goes to the chart (issue #60). The bare column below is the old
          overlay's content wrapper, kept so the instruments under it keep
          their indentation; it carries no layout of its own. */}
      <div>
        {/* The page heading: Willard's billing, with the lamp semantics the
            retired panel's masthead set. The close control that rode beside
            the lamp left with the overlay contract (#652) -- the Aviary
            masthead above is the way anywhere -- and so did the rule under
            this row, which would have sat one line below the tab bar's. */}
        <div className="flex items-center justify-between gap-3">
          <h2
            className="text-2xl text-ink"
            style={{ fontFamily: "var(--font-display)" }}
          >
            Weather Post{" "}
            <span className="stamp ml-2 align-middle text-[10px] text-inkfaint">
              station view
            </span>
          </h2>
          {offline ? (
            <span className="flex items-center gap-1.5 text-xs">
              <span className="inline-block h-2 w-2 rounded-full bg-inkfaint" />
              <span className="stamp text-inkfaint">on coffee break</span>
            </span>
          ) : reporting ? (
            <span className="flex items-center gap-1.5 text-xs">
              <span className="lamp inline-block h-2 w-2 rounded-full bg-led text-led" />
              <span className="stamp text-led">on duty</span>
            </span>
          ) : (
            <span className="flex items-center gap-1.5 text-xs">
              <span className="breathe inline-block h-2 w-2 rounded-full bg-turkey" />
              <span className="stamp text-turkey">no fresh report</span>
            </span>
          )}
        </div>

        {/* Why weather? (#655): a birdwatching app's answer to its own weather
            tab, in the mask page's explainer idiom (#596). Static per box --
            no state reaches it, so it never moves (house rule #1). Every
            sentence is true on every box except the journal's, which the
            server includes only where the field journal carries weather
            (the house; the stations write no notes). */}
        <p className="mt-2 max-w-3xl text-sm leading-relaxed text-inkdim">
          <span className="stamp mr-2 text-xs text-inkfaint">why weather?</span>
          Birds keep their own schedule, and weather is one of the few things
          shaping it that we can measure.
          {weatherJournal &&
            " The field journal checks how often each bird turns up under clear skies, cloud, rain and snow, and in spells warmer or colder than usual for the season, against its own average, and a bird’s notes say so when the weather makes a difference."}{" "}
          When the wind blows past 15 miles an hour, Earl needs more
          confidence before a detection counts, and the ones that still count
          are marked <span className="stamp text-inkdim">wind?</span>. The
          readings come from the yard’s own weather station where there is
          one, and from OpenWeather’s report for this location where there
          isn’t.
        </p>

        <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
          {/* --- The sky: hero conditions + the big chart ------------------ */}
          <main className="min-w-0">
            <section className="panel rounded-sm border border-line bg-panel px-4 pb-4 pt-3">
              {/* Top-aligned like the panel (issue #54): items-end left a
                  dead void above the temperature once the WxStat grid grew
                  taller than the headline. */}
              <div className="flex flex-wrap items-start gap-x-8 gap-y-4">
                {/* The sky's icon (issue #78) takes the left column; feels-like
                    and the tendency stack beside the number to make the room. */}
                <div className="flex items-center gap-4">
                  <ConditionBadge
                    icon={conditionIcon(current)}
                    size={58}
                    live={reporting}
                  />
                  <div>
                    <div className="flex items-center gap-3">
                      <span
                        className={`text-6xl font-bold tabular-nums ${reporting ? "text-ink" : "text-inkfaint"}`}
                      >
                        {wxRound(current?.temp_f ?? null)}°
                      </span>
                      <span className="flex flex-col text-base leading-snug text-inkdim">
                        <span>feels {wxRound(current?.feels_like_f ?? null)}°</span>
                        {/* the tendency (issue #67) gets its own line under the
                            feels-like; min-h reserves it before the trail is
                            long enough to have an opinion */}
                        <span className="min-h-[24px]">
                          {tempTrend &&
                            `${TREND_GLYPH[tempTrend]} ${tempTrend}`}
                        </span>
                      </span>
                    </div>
                    {/* The sky gets headline billing next to the number, the
                        panel's #54 treatment at station-view scale. */}
                    <div
                      className={`mt-0.5 min-h-[28px] text-lg ${reporting ? "text-ink" : "text-inkfaint"}`}
                    >
                      {current?.description ?? ""}
                    </div>
                    <div className="stamp mt-1 text-[10px] text-inkfaint">
                      sun {clock(current?.sunrise ?? null)} –{" "}
                      {clock(current?.sunset ?? null)}
                      {current !== null && now !== null && (
                        <>
                          {" "}
                          · read{" "}
                          {/* min-w reserves "just now" (8ch at this size and
                              tracking) -- this stamp is the left column's
                              widest line, so the age flipping to "1m ago" and
                              back would walk the whole stat grid sideways
                              (house rule #1) */}
                          <span className="inline-block min-w-[6.3em]">
                            {ageText(current.ts, now)}
                          </span>
                        </>
                      )}
                    </div>
                  </div>
                </div>
                <div className="grid flex-1 grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-3">
                  <WxStat
                    label="dew point"
                    value={wxRound(current?.dew_point_f ?? null)}
                    unit="°F"
                    sub={dewTrend && `${TREND_GLYPH[dewTrend]} ${dewTrend}`}
                    info={
                      instruments
                        ? "The temperature the air would have to cool to for its moisture to bead out as dew, figured from the station's temperature and humidity. The closer it runs to the actual temperature, the heavier the air feels — 50s is comfortable, 60s is sticky, 70s is a swamp."
                        : "The temperature the air would have to cool to for its moisture to bead out as dew, figured from the area temperature and humidity (no local sensor on this station). The closer it runs to the actual temperature, the heavier the air feels."
                    }
                  />
                  <WxStat
                    label="humidity"
                    value={wxRound(current?.humidity_pct ?? null)}
                    unit="%"
                    sub={
                      humidityTrend &&
                      `${TREND_GLYPH[humidityTrend]} ${humidityTrend}`
                    }
                    info={
                      instruments
                        ? "How much water vapor the air is carrying, as a percentage of all it could hold at this temperature — read by the WH90's hygrometer over the driveway. Warm air holds more, so the same percentage sits heavier on a hot afternoon than a cool morning."
                        : "How much water vapor the air is carrying, as a percentage of all it could hold at this temperature — from the area weather feed (no local hygrometer on this station)."
                    }
                  />
                  <WxStat
                    label="wind"
                    value={wxRound(current?.wind_mph ?? null)}
                    unit="mph"
                    aside={
                      <span className="flex items-center gap-1.5 text-xs text-inkdim">
                        <CompassGlyph deg={current?.wind_deg ?? null} />
                        {/* min-w reserves the widest label ("NW") so a
                            bearing arriving can't nudge the row */}
                        <span className="min-w-[2ch]">
                          {compass(current?.wind_deg ?? null) || "—"}
                        </span>
                      </span>
                    }
                    sub={[
                      current?.wind_gust_mph != null
                        ? `gusts ${Math.round(current.wind_gust_mph)}`
                        : null,
                      current?.wind_max_daily_gust_mph != null
                        ? `max ${Math.round(current.wind_max_daily_gust_mph)}`
                        : null,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  />
                  <WxStat
                    label="barometer"
                    value={wxFixed(current?.pressure_rel_inhg ?? null, 2)}
                    unit="in"
                    sub={baroTrend && `${TREND_GLYPH[baroTrend]} ${baroTrend}`}
                    info={
                      instruments
                        ? "The weight of the air, in inches of mercury — measured at the gateway and corrected to sea level so it reads like the forecasts do. The arrow matters more than the number: steadily falling usually means weather moving in, rising means it's clearing out."
                        : "The weight of the air, in inches of mercury at sea level — from the area weather feed (no barometer at this station). The arrow matters more than the number: steadily falling usually means weather moving in, rising means it's clearing out."
                    }
                  />
                  <WxStat
                    label="uv index"
                    value={wxRound(current?.uv_index ?? null)}
                    sub={
                      current?.solar_wm2 != null &&
                      `solar ${Math.round(current.solar_wm2).toLocaleString("en-US")} W/m²`
                    }
                    // One line, like its siblings (#657): a multi-line
                    // attribute string compiles differently for the server
                    // and the browser, and the mismatch cost every load a
                    // full client re-render.
                    info="The strength of the sun's ultraviolet at the station, on the standard 0–11 scale from the WH90's light sensor. 0–2 is low, 3–5 moderate, 6–7 high, 8 and up means even the turkeys should find shade. The solar wattage below is the same sunshine as raw power."
                  />
                  <WxStat
                    label="rain today"
                    value={wxFixed(current?.rain_day_in ?? null, 2)}
                    unit="in"
                    sub={
                      current?.raining === 1 ? (
                        <span className="flex items-center gap-1.5 text-led">
                          <span className="lamp inline-block h-1.5 w-1.5 rounded-full bg-led text-led" />
                          falling ·{" "}
                          {wxFixed(current?.rain_rate_inhr ?? null, 2)}/hr
                        </span>
                      ) : (
                        "dry"
                      )
                    }
                  />
                </div>
              </div>
              {/* The seldom-read dials, one quiet line: the piezo's ledgers
                  and the agronomist's vapor number. */}
              <div className="mt-3 border-t border-line pt-2 text-[11px] text-inkfaint">
                rain event {wxFixed(current?.rain_event_in ?? null, 2)} in ·
                week {wxFixed(current?.rain_week_in ?? null, 2)} · month{" "}
                {wxFixed(current?.rain_month_in ?? null, 2)} · year{" "}
                {wxFixed(current?.rain_year_in ?? null, 2)} in · vpd{" "}
                {wxFixed(current?.vpd_inhg ?? null, 3)} in
              </div>
            </section>

            {/* --- The trend, four instruments tall ------------------------ */}
            <section className="panel mt-4 rounded-sm border border-line bg-panel px-4 pb-4 pt-3">
              <div className="flex items-center justify-between gap-3 text-[10px] text-inkfaint">
                <span>
                  <span className="text-squirrel">—</span> temp °F ·{" "}
                  <span className="text-inkdim">—</span> wind mph · observed
                  solid, forecast dashed
                </span>
                {/* Home. Always rendered and merely disabled while the window
                    is live, never appearing on pan -- a control that pops into
                    existence would shove this line's legend sideways (house
                    rule #1). The masthead's chrome, the stepper's disabled
                    idiom: no new vocabulary for an old job. */}
                <button
                  type="button"
                  onClick={() => setWindowEnd(null)}
                  disabled={live}
                  aria-label="Return the chart to now"
                  className="stamp shrink-0 rounded-sm border border-line px-2 py-0.5 text-inkdim transition-colors hover:border-linebright hover:text-squirrel disabled:pointer-events-none disabled:opacity-40"
                >
                  now
                </button>
              </div>
              <WxTimeAxis
                days={days}
                nowFrac={nowFrac}
                leftLabel={windowEdgeLabel(ts0 - (now ?? 0))}
                rightLabel={windowEdgeLabel(ts1 - (now ?? 0))}
                className="mt-1.5"
              />
              <div
                className={`relative mt-1 ${hasChart ? "cursor-grab active:cursor-grabbing" : ""}`}
                // pan-y: a horizontal drag is ours, a vertical one still
                // scrolls the overlay underneath (the browser fires
                // pointercancel when it claims the gesture, which ends the
                // drag cleanly).
                style={{ touchAction: "pan-y" }}
                onPointerDown={(e) => {
                  if (!hasChart || now === null) return;
                  e.currentTarget.setPointerCapture(e.pointerId);
                  drag.current = { x: e.clientX, ts1, moved: false };
                  dragging.current = true;
                  setFrozenAxes(frozenAxes ?? liveAxes);
                  // A finger on the glass isn't hovering; the crosshair waits
                  // to see whether this becomes a tap or a drag.
                  if (e.pointerType !== "mouse") setHoverFrac(null);
                }}
                onPointerMove={(e) => {
                  const d = drag.current;
                  if (d) {
                    const r = e.currentTarget.getBoundingClientRect();
                    if (r.width <= 0) return;
                    const dx = e.clientX - d.x;
                    if (!d.moved && Math.abs(dx) > TAP_SLOP_PX) d.moved = true;
                    if (d.moved) {
                      setHoverFrac(null);
                      // Drag right and the chart follows your hand, which
                      // means walking backwards in time.
                      panTo(d.ts1 - (dx / r.width) * STATION_SPAN_S);
                    }
                    return;
                  }
                  // Only a mouse can hover. Touch scrubbing is the tap below.
                  if (e.pointerType === "mouse") setHoverFrac(fracAt(e));
                }}
                onPointerUp={(e) => {
                  const d = drag.current;
                  drag.current = null;
                  dragging.current = false;
                  settleAxes();
                  if (!d) return;
                  // A press that never travelled is a tap: place the
                  // crosshair. A mouse release just restores the hover it had.
                  if (!d.moved || e.pointerType === "mouse")
                    setHoverFrac(fracAt(e));
                }}
                onPointerLeave={() => {
                  if (!drag.current) setHoverFrac(null);
                }}
                onPointerCancel={() => {
                  drag.current = null;
                  dragging.current = false;
                  settleAxes();
                  setHoverFrac(null);
                }}
              >
                {/* main chart: temperature + wind, the panel chart writ tall */}
                <div className="relative h-72 w-full sm:h-96">
                  {hasChart && (
                    <svg
                      viewBox={`0 0 ${WXL_W} ${WXL_H}`}
                      preserveAspectRatio="none"
                      className="h-full w-full"
                      role="img"
                      aria-label="Observed and forecast temperature and wind"
                    >
                      {nights.map((b) => (
                        <rect
                          key={b.start}
                          x={((b.start - ts0) / (ts1 - ts0)) * WXL_W}
                          y={0}
                          width={((b.end - b.start) / (ts1 - ts0)) * WXL_W}
                          height={WXL_H}
                          fill="black"
                          opacity="0.25"
                        />
                      ))}
                      {days.map((t) => (
                        <line
                          key={t.ts}
                          x1={t.frac * WXL_W}
                          y1={0}
                          x2={t.frac * WXL_W}
                          y2={WXL_H}
                          stroke="var(--line)"
                          vectorEffect="non-scaling-stroke"
                        />
                      ))}
                      {nowFrac !== null && (
                        <line
                          x1={nowFrac * WXL_W}
                          y1={0}
                          x2={nowFrac * WXL_W}
                          y2={WXL_H}
                          stroke="var(--line-bright)"
                          strokeDasharray="2 4"
                          vectorEffect="non-scaling-stroke"
                        />
                      )}
                      <path
                        d={linePath(trend.observed, (p) => p.wind_mph, ts0, ts1, 0, windMax, WXL_W, WXL_H)}
                        fill="none"
                        stroke="var(--ink-dim)"
                        strokeWidth="1"
                        opacity="0.8"
                        vectorEffect="non-scaling-stroke"
                      />
                      <path
                        d={linePath(trend.coming, (p) => p.wind_mph, ts0, ts1, 0, windMax, WXL_W, WXL_H)}
                        fill="none"
                        stroke="var(--ink-dim)"
                        strokeWidth="1"
                        strokeDasharray="3 3"
                        opacity="0.5"
                        vectorEffect="non-scaling-stroke"
                      />
                      <path
                        d={linePath(trend.observed, (p) => p.temp_f, ts0, ts1, range.min, range.max, WXL_W, WXL_H)}
                        fill="none"
                        stroke="var(--squirrel)"
                        strokeWidth="1.8"
                        vectorEffect="non-scaling-stroke"
                      />
                      <path
                        d={linePath(trend.coming, (p) => p.temp_f, ts0, ts1, range.min, range.max, WXL_W, WXL_H)}
                        fill="none"
                        stroke="var(--squirrel)"
                        strokeWidth="1.4"
                        strokeDasharray="4 3"
                        opacity="0.65"
                        vectorEffect="non-scaling-stroke"
                      />
                    </svg>
                  )}
                  {hasChart && range !== null && (
                    <>
                      <span className="pointer-events-none absolute left-1 top-0.5 text-[10px] tabular-nums text-squirrel opacity-60">
                        {range.max}°
                      </span>
                      <span className="pointer-events-none absolute bottom-0.5 left-1 text-[10px] tabular-nums text-squirrel opacity-60">
                        {range.min}°
                      </span>
                      <span className="pointer-events-none absolute right-1 top-0.5 text-[10px] tabular-nums text-inkfaint">
                        {windMax} mph
                      </span>
                    </>
                  )}
                  {/* Each forecast peak and valley wears its temperature
                      (issue #113), positioned where it actually happens. HTML
                      overlays for the same reason as the snapped dots below:
                      the viewBox is stretched, so svg text would squash. They
                      read the BLENDED temps (#71 -- trend.coming is already
                      calibrated), or a label would disagree with the line it
                      sits on. Ambient, not the readout: the hover chip stays
                      the detailed answer, so these dim out of its way. */}
                  {hasChart && range !== null &&
                    marks.map((m) => {
                      const frac = (m.ts - ts0) / (ts1 - ts0);
                      if (frac < 0.02 || frac > 0.98) return null;
                      const y =
                        (1 - (m.temp_f - range.min) / (range.max - range.min)) *
                        100;
                      return (
                        <span
                          key={`${m.kind}-${m.ts}`}
                          className={`stamp pointer-events-none absolute -translate-x-1/2 text-[10px] tabular-nums text-squirrel transition-opacity ${
                            hovered ? "opacity-40" : "opacity-80"
                          }`}
                          style={{
                            left: `${frac * 100}%`,
                            top: `${y}%`,
                            // above a peak, below a valley -- the label sits
                            // off the line, never on it
                            marginTop: m.kind === "high" ? "-1.15rem" : "0.3rem",
                          }}
                        >
                          {wxRound(m.temp_f)}°
                        </span>
                      );
                    })}
                  {/* snapped dots, the panel chart's HTML-overlay trick (the
                      viewBox is stretched; svg circles would squash). */}
                  {hovered && range !== null && (
                    <>
                      {hovered.temp_f !== null && (
                        <span
                          className="pointer-events-none absolute h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full border border-squirrel bg-panel"
                          style={{
                            left: `${hoveredFrac * 100}%`,
                            top: `${(1 - (hovered.temp_f - range.min) / (range.max - range.min)) * 100}%`,
                          }}
                        />
                      )}
                      {hovered.wind_mph !== null && (
                        <span
                          className="pointer-events-none absolute h-1.5 w-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full border border-inkdim bg-panel"
                          style={{
                            left: `${hoveredFrac * 100}%`,
                            top: `${(1 - hovered.wind_mph / windMax) * 100}%`,
                          }}
                        />
                      )}
                    </>
                  )}
                </div>

                {/* the station's own instruments, one strip each, sharing
                    the time axis -- rain falls, snow waits its season, the
                    barometer wanders, the sun arcs, and they all line up
                    under the temperature */}
                {hasChart && (
                  <>
                    <WxStrip
                      label="rain"
                      scale={`${rainMax}`}
                      ticks={dayFracs}
                      nowFrac={nowFrac}
                    >
                      {/* bar width follows the window: 5-min points sit 0.56
                          viewBox units apart across 144h, so wider bars would
                          stack opacity into false intensity */}
                      {observed
                        .filter(
                          (p) =>
                            p.rain_rate_inhr !== null && p.rain_rate_inhr > 0,
                        )
                        .map((p) => (
                          <rect
                            key={p.ts}
                            x={((p.ts - ts0) / (ts1 - ts0)) * WXL_W - 0.35}
                            y={
                              WXL_STRIP_H -
                              (Math.min(p.rain_rate_inhr!, rainMax) / rainMax) *
                                WXL_STRIP_H
                            }
                            width={0.7}
                            height={
                              (Math.min(p.rain_rate_inhr!, rainMax) / rainMax) *
                              WXL_STRIP_H
                            }
                            style={{
                              fill: precipFill(
                                "var(--rain)",
                                precipShade(
                                  p.rain_rate_inhr!,
                                  rainMax,
                                  RAIN_SHADE_FLOOR,
                                ),
                              ),
                            }}
                          />
                        ))}
                      {/* the forecast half changes units at the now line
                          (issue #65): ghost bars are the CHANCE of
                          precipitation, full strip = 100% -- the forecast's
                          volumes read as slivers on the piezo's in/hr scale,
                          but a 20% Thursday deserves a 20%-tall bar. Volumes
                          stay one hover away in the chip. Each bar spans the
                          3h window ending at its point, clipped at the now
                          line (the observed trail owns the past). */}
                      {now !== null &&
                        trend.coming
                          .filter(
                            (p) => p.ts > now && p.pop !== null && p.pop > 0,
                          )
                          .map((p) => {
                            const t0 = Math.max(
                              p.ts - WX_FORECAST_STEP_S,
                              now,
                            );
                            const w =
                              ((p.ts - t0) / (ts1 - ts0)) * WXL_W - 1.4;
                            if (w <= 0) return null;
                            const h = Math.min(p.pop!, 1) * WXL_STRIP_H;
                            return (
                              <rect
                                key={p.ts}
                                x={((t0 - ts0) / (ts1 - ts0)) * WXL_W + 0.7}
                                y={WXL_STRIP_H - h}
                                width={w}
                                height={h}
                                // Shade = the chance, same as the height (#113):
                                // a 90% Friday is a tall VIVID slab, a 20%
                                // Tuesday a short ghost. Ceilinged below the
                                // observed trail's full voice -- a forecast,
                                // however certain, never shouts as loud as a
                                // measurement.
                                style={{
                                  fill: precipFill(
                                    "var(--rain)",
                                    precipShade(
                                      p.pop!,
                                      1,
                                      RAIN_SHADE_FLOOR,
                                      FORECAST_SHADE_CEIL,
                                    ),
                                  ),
                                }}
                              />
                            );
                          })}
                    </WxStrip>
                    {/* snow, forecast-only (issue #65) and seasonal (issue
                        #69): the piezo cannot see snow, so the observed half
                        is honestly blank forever, and April through October
                        the whole row stands down -- unless the forecast
                        actually carries snow, which overrides the calendar
                        in any month. Snow-white ink, never the rain's blue.
                        Its ramp reaches FULL white (issue #113) where rain's
                        forecast is ceilinged: rain's ghosts share a strip with
                        a measured trail they mustn't out-shout, and snow's
                        observed half is blank forever, so there is nothing
                        here to shout over -- heavy snow gets the whole voice. */}
                    {showSnow && (
                    <WxStrip
                      label="snow · forecast in per 3h"
                      scale={`${snowMax}`}
                      ticks={dayFracs}
                      nowFrac={nowFrac}
                    >
                      {now !== null &&
                        trend.coming
                          .filter(
                            (p) =>
                              p.ts > now &&
                              p.snow_3h_in !== null &&
                              p.snow_3h_in > 0,
                          )
                          .map((p) => {
                            const t0 = Math.max(
                              p.ts - WX_FORECAST_STEP_S,
                              now,
                            );
                            const w =
                              ((p.ts - t0) / (ts1 - ts0)) * WXL_W - 1.4;
                            if (w <= 0) return null;
                            const h =
                              (Math.min(p.snow_3h_in!, snowMax) / snowMax) *
                              WXL_STRIP_H;
                            return (
                              <rect
                                key={p.ts}
                                x={((t0 - ts0) / (ts1 - ts0)) * WXL_W + 0.7}
                                y={WXL_STRIP_H - h}
                                width={w}
                                height={h}
                                style={{
                                  fill: precipFill(
                                    "var(--ink)",
                                    precipShade(
                                      p.snow_3h_in!,
                                      snowMax,
                                      SNOW_SHADE_FLOOR,
                                    ),
                                  ),
                                }}
                              />
                            );
                          })}
                    </WxStrip>
                    )}
                    <WxStrip
                      label="barometer · in"
                      scale={
                        presRange ? `${presRange.max}–${presRange.min}` : ""
                      }
                      ticks={dayFracs}
                      nowFrac={nowFrac}
                    >
                      {presRange && (
                        <path
                          d={linePath(observed, (p) => p.pressure_rel_inhg, ts0, ts1, presRange.min, presRange.max, WXL_W, WXL_STRIP_H)}
                          fill="none"
                          stroke="var(--ink)"
                          strokeWidth="1.2"
                          opacity="0.55"
                          vectorEffect="non-scaling-stroke"
                        />
                      )}
                    </WxStrip>
                    {/* Solar is gateway/WH90-only (#428): OWM station mode has
                        no irradiance series, so the row stands down entirely
                        rather than paint an empty strip. UV dashed trail
                        retired — hover still carries uv when present at home. */}
                    {instruments && (
                    <WxStrip
                      label="solar · W/m²"
                      scale={`${solarMax}`}
                      ticks={dayFracs}
                      nowFrac={nowFrac}
                    >
                      <path
                        d={linePath(observed, (p) => p.solar_wm2, ts0, ts1, 0, solarMax, WXL_W, WXL_STRIP_H)}
                        fill="none"
                        stroke="var(--turkey)"
                        strokeWidth="1.2"
                        opacity="0.8"
                        vectorEffect="non-scaling-stroke"
                      />
                    </WxStrip>
                    )}
                  </>
                )}

                {/* one crosshair through the whole instrument stack */}
                {hovered && (
                  <span
                    className="pointer-events-none absolute inset-y-0 w-px bg-inkdim/40"
                    style={{ left: `${hoveredFrac * 100}%` }}
                  />
                )}
                {hovered && now !== null && (
                  <div
                    className="pointer-events-none absolute top-1 z-10 whitespace-nowrap rounded-sm border border-linebright bg-panel2 px-2 py-1"
                    style={
                      hoveredFrac < 0.5
                        ? { left: `calc(${hoveredFrac * 100}% + 10px)` }
                        : { right: `calc(${(1 - hoveredFrac) * 100}% + 10px)` }
                    }
                  >
                    <div className="stamp text-[10px] text-inkfaint">
                      {dayClock(hovered.ts)} ·{" "}
                      {hovered.ts <= now ? "observed" : "forecast"}
                    </div>
                    <div className="text-[11px] tabular-nums text-ink">
                      {wxRound(hovered.temp_f)}°
                      {hovered.condition && (
                        <span className="text-inkdim">
                          {" "}
                          · {hovered.condition.toLowerCase()}
                        </span>
                      )}
                    </div>
                    <div className="text-[10px] tabular-nums text-inkdim">
                      wind {wxRound(hovered.wind_mph)} mph
                      {hovered.wind_gust_mph !== null &&
                        ` · gusts ${Math.round(hovered.wind_gust_mph)}`}
                    </div>
                    {hoveredExtras.length > 0 && (
                      <div className="text-[10px] tabular-nums text-inkdim">
                        {hoveredExtras.join(" · ")}
                      </div>
                    )}
                  </div>
                )}
              </div>
              <WxTimeAxis
                days={days}
                nowFrac={nowFrac}
                leftLabel={windowEdgeLabel(ts0 - (now ?? 0))}
                rightLabel={windowEdgeLabel(ts1 - (now ?? 0))}
                className="mt-0.5"
              />
            </section>
          </main>

          {/* --- The desk and the hardware: Willard + the station itself --- */}
          <aside className="flex min-w-0 flex-col gap-4">
            {/* Same #92 voice treatment as the panel segment: rail + stamp
                in willardVoice, body ink; rail reserved empty too. */}
            <section className="panel rounded-sm border border-line bg-panel">
              <div
                className="mx-4 mb-4 mt-3 border-l-2 pl-3"
                style={{ borderLeftColor: willardVoice }}
              >
                <div className="flex items-baseline gap-2 text-[11px]">
                  <span className="stamp" style={{ color: willardVoice }}>
                    willard, on the air
                  </span>
                  {onAir && report !== null && (
                    <span className="text-inkfaint">{clock(report.ts)}</span>
                  )}
                </div>
                {onAir && report !== null ? (
                  <p
                    className="pt-2 text-[17px] leading-snug text-ink"
                    style={{
                      fontFamily: "var(--font-display)",
                      whiteSpace: "pre-line",
                    }}
                  >
                    {report.text}
                  </p>
                ) : (
                  <p className="pt-2 text-sm leading-relaxed text-inkfaint">
                    willard is between broadcasts — the forecast desk is quiet
                  </p>
                )}
              </div>
            </section>

            <section className="panel rounded-sm border border-line bg-panel">
              <div className="flex items-baseline justify-between gap-3 px-4 pt-3">
                <span
                  className={`stamp text-[11px] ${instruments ? "text-inkdim" : "text-inkfaint"}`}
                >
                  the station itself
                </span>
                {!instruments && (
                  <span className="stamp text-xs text-inkfaint">
                    Station not available
                  </span>
                )}
              </div>
              {instruments ? (
                <dl className="flex flex-col gap-2.5 px-4 pb-4 pt-3 text-[12px]">
                  <div className="flex items-baseline justify-between gap-3">
                    <dt className="stamp text-[10px] text-inkfaint">
                      indoors · at the gateway
                    </dt>
                    <dd className="tabular-nums text-ink">
                      {wxRound(current?.indoor_temp_f ?? null)}° ·{" "}
                      {wxRound(current?.indoor_humidity_pct ?? null)}% rh
                    </dd>
                  </div>
                  <div className="flex items-baseline justify-between gap-3">
                    <dt className="stamp text-[10px] text-inkfaint">
                      barometer · absolute
                    </dt>
                    <dd className="tabular-nums text-inkdim">
                      {wxFixed(current?.pressure_abs_inhg ?? null, 2)} in
                    </dd>
                  </div>
                  <div className="flex items-center justify-between gap-3 border-t border-line pt-2.5">
                    <dt className="stamp text-[10px] text-inkfaint">
                      wh90 battery
                    </dt>
                    {/* meter last so its right edge lands flush with the radio
                        signal's below it -- the two green meters read as one
                        column (issue #103) */}
                    <dd className="flex items-center gap-2 tabular-nums text-inkdim">
                      {current?.station_voltage != null &&
                        `${current.station_voltage.toFixed(2)}v`}
                      <SegMeter n={battery} of={5} tone={batteryTone} />
                    </dd>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <dt className="stamp text-[10px] text-inkfaint">
                      radio signal
                    </dt>
                    <dd className="flex items-center gap-2 text-inkdim">
                      <SegMeter n={current?.station_signal ?? null} of={4} />
                    </dd>
                  </div>
                  <div className="flex items-baseline justify-between gap-3 border-t border-line pt-2.5">
                    <dt className="stamp text-[10px] text-inkfaint">
                      last report
                    </dt>
                    <dd className="tabular-nums text-inkdim">
                      {current !== null && now !== null
                        ? `${clock(current.ts)} · ${ageText(current.ts, now)}`
                        : "—"}
                    </dd>
                  </div>
                </dl>
              ) : (
                /* Footprint held, chrome dimmed (#428): remote stations have
                   no gateway/WH90. Last-report stays full strength so
                   "is the feed alive?" is still answerable. */
                <div className="px-4 pb-4 pt-3 text-[12px]">
                  <dl className="flex flex-col gap-2.5 opacity-40">
                    <div className="flex items-baseline justify-between gap-3">
                      <dt className="stamp text-[10px] text-inkfaint">
                        indoors · at the gateway
                      </dt>
                      <dd className="tabular-nums text-inkfaint">—</dd>
                    </div>
                    <div className="flex items-baseline justify-between gap-3">
                      <dt className="stamp text-[10px] text-inkfaint">
                        barometer · absolute
                      </dt>
                      <dd className="tabular-nums text-inkfaint">—</dd>
                    </div>
                    <div className="flex items-center justify-between gap-3 border-t border-line pt-2.5">
                      <dt className="stamp text-[10px] text-inkfaint">
                        wh90 battery
                      </dt>
                      <dd className="flex items-center gap-2 text-inkfaint">
                        <SegMeter n={null} of={5} />
                      </dd>
                    </div>
                    <div className="flex items-center justify-between gap-3">
                      <dt className="stamp text-[10px] text-inkfaint">
                        radio signal
                      </dt>
                      <dd className="flex items-center gap-2 text-inkfaint">
                        <SegMeter n={null} of={4} />
                      </dd>
                    </div>
                  </dl>
                  <div className="mt-2.5 flex items-baseline justify-between gap-3 border-t border-line pt-2.5">
                    <dt className="stamp text-[10px] text-inkfaint">
                      last report
                    </dt>
                    <dd className="tabular-nums text-inkdim">
                      {current !== null && now !== null
                        ? `${clock(current.ts)} · ${ageText(current.ts, now)}`
                        : "—"}
                    </dd>
                  </div>
                </div>
              )}
            </section>
          </aside>
        </div>
      </div>
    </section>
  );
}
