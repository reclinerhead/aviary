"use client";

// The Aviary (epic #182 Phase 1, issue #183): the Earl Birdwatch GUI.
// Everything here READS -- the page renders earl.db (via /aviary/* routes)
// and the live bus; the listener is never touched. Two views share this
// file the way Dashboard.tsx holds the dashboard's: `Aviary` is the /aviary
// page (species grid + live ticker + today's visitors), `SpeciesProfile`
// the /aviary/[species] page -- the frame Phases 2-4 fill in with real
// portraits, prose, and the visits chart.
//
// Live mechanics are the Field Journal's: hydrate over HTTP, then merge
// live audio/events on top over mqtt.js (the BROWSER build -- the hard-won
// default-entry gotcha), prepend with stable content-derived keys so the
// hydration/live overlap is a no-op, never a duplicate row. The no-layout-
// shift rule runs throughout: every event row reserves its player slot,
// empty states reserve their panels' footprint, and the grid re-sorts ONLY
// on a sort click -- live counts land in place, lifers append at the end.

import mqtt from "mqtt/dist/mqtt.esm";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  AUDIO_EVENTS_TOPIC,
  AUDIO_STATUS_TOPIC,
  AudioEvent,
  BirdEvent,
  audioEventFrom,
  audioEventKey,
  busUrl,
  parseAudioEvent,
} from "@/lib/bus";
import {
  RosterEntry,
  SortDir,
  SortKey,
  Visit,
  clipUrl,
  collapseVisits,
  cropPosition,
  portraitAspect,
  portraitUrl,
  rosterOrder,
  todayVisitors,
} from "@/lib/aviary";
import { VisitsChart } from "@/components/VisitsChart";

// Ticker sizing: hydrate the newest 50, let live arrivals grow it to 80
// before the oldest fall off -- the JOURNAL_LIMIT idea, one namespace over.
const HYDRATE_LIMIT = 50;
const TICKER_CAP = 80;
// The profile reads enough raw rows to survive a pre-#175 morning (25
// windows per visit) and still show a real page of visits.
const PROFILE_ROWS = 200;

type TickerEvent = AudioEvent & { key: string };
type TodayTile = { species_sci: string; species_common: string; count: number };

// --- Formatting (locale-side -- the audio namespace is epoch end to end) ----

const timeOf = (ts: number) =>
  new Date(ts * 1000).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
const dayOf = (ts: number) =>
  new Date(ts * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
const dateOf = (ts: number) =>
  new Date(ts * 1000).toLocaleDateString([], {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
/** Time alone for today's moments, day + time for older ones. `midnight`
 * comes from state (computed once on mount), never Date.now() in render --
 * the SSR/hydration rule; rows only exist client-side anyway. */
const stampOf = (ts: number, midnight: number | null) =>
  midnight !== null && ts >= midnight
    ? timeOf(ts)
    : `${dayOf(ts)} · ${timeOf(ts)}`;

// --- The placeholder portrait (Phase 2 replaces it with Wikipedia's) --------

/** The launchpad's Earl bird (launchpad/index.html icon-bird), copied not
 * abstracted -- the Homestead precedent -- so the module wears one glyph on
 * every surface. Structure strokes in currentColor; the tile's ink scale
 * carries it. */
function BirdGlyph({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M4 20h16" />
      <path d="M11.5 20v-2.2" />
      <path d="M11.5 17.8c-2.8-.4-4.4-2.5-4.4-5.2 0-3 2.2-5.1 4.9-5.1 1.9 0 3.3.9 4.1 2.3l2.9 1-2.4 1.5c-.2 3.3-2 5.2-5.1 5.5Z" />
      <circle cx="13.4" cy="9.9" r="0.5" />
    </svg>
  );
}

/** The portrait slot (#184): the enrichment pass's Wikipedia photo when the
 * roster says one exists, the reserved placeholder block otherwise -- one
 * geometry, so enrichment landing can never shift a tile. A photo that
 * 404s (shelf pruned by hand, pass half-run) falls back to the placeholder,
 * never a broken image. Lazy: only portraits in view fetch. */
function Portrait({
  sci,
  has,
  alt,
  glyphClass,
  className,
  w,
  h,
  boxAspect,
  style,
}: {
  sci: string;
  has: boolean;
  alt: string;
  glyphClass: string;
  className: string;
  // The portrait's real shape (#185), NULL on rows awaiting the pass's
  // backfill -- unknown means "keep the old centered crop", never a guess.
  w?: number | null;
  h?: number | null;
  /** The fixed box's own ratio (4/3 for tiles, 1 for thumbs). */
  boxAspect: number;
  /** Geometry the caller reserves (the profile's true-aspect figure). It
   * rides BOTH branches, so the placeholder holds exactly the shape the
   * photo will take -- enrichment landing can't shift the page. */
  style?: React.CSSProperties;
}) {
  const [lost, setLost] = useState(false);
  if (has && !lost)
    return (
      <img
        src={portraitUrl(sci)}
        alt={alt}
        loading="lazy"
        onError={() => setLost(true)}
        // Crop from the TOP for portrait-orientation sources: a bird's head
        // sits high in the frame, and a centered crop of a tall photo cuts
        // exactly the part that identifies it (#185).
        style={{ ...style, objectPosition: cropPosition(w, h, boxAspect) }}
        className={`${className} object-cover`}
      />
    );
  return (
    <span
      style={style}
      className={`${className} flex flex-col items-center justify-center gap-1 text-inkfaint`}
    >
      <BirdGlyph className={glyphClass} />
      <span className="stamp text-[9px]">portrait pending</span>
    </span>
  );
}

/** The ticker's identity thumb (#192): the music player's search-result
 * idiom, one stack over -- a small square portrait leading the row so the
 * eye finds the bird before the words. Species without a portrait yet
 * (un-enriched, a fresh lifer, a failed load) wear the glyph in the same
 * reserved square -- one geometry, never a broken image. Decorative
 * alt="": the species name sits right beside it. */
function TickerThumb({
  sci,
  has,
  w,
  h,
}: {
  sci: string;
  has: boolean;
  w?: number | null;
  h?: number | null;
}) {
  const [lost, setLost] = useState(false);
  return (
    <span className="flex h-9 w-9 shrink-0 items-center justify-center overflow-hidden rounded-sm border border-line bg-panel text-inkfaint">
      {has && !lost ? (
        <img
          src={portraitUrl(sci)}
          alt=""
          loading="lazy"
          onError={() => setLost(true)}
          // Square box: same top-crop rule, so a tall photo keeps its head
          // even at 36px, where a decapitated bird is unidentifiable (#185).
          style={{ objectPosition: cropPosition(w, h, 1) }}
          className="h-full w-full object-cover"
        />
      ) : (
        <BirdGlyph className="h-5 w-5" />
      )}
    </span>
  );
}

// --- Clip playback -----------------------------------------------------------

type ClipPlayer = {
  playing: string | null;
  faded: ReadonlySet<string>;
  toggle: (url: string) => void;
};

/** One shared <audio> per view: clicking a second clip stops the first
 * (two yards' worth of birdsong at once is noise, not data). A clip that
 * fails to load is marked faded -- pruned past the retention window is the
 * normal end of a clip's life, the Field Journal's pruned-thumbnail rule. */
function useClipPlayer(): ClipPlayer {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const pendingRef = useRef<string | null>(null);
  const playingRef = useRef<string | null>(null);
  const [playing, setPlaying] = useState<string | null>(null);
  const [faded, setFaded] = useState<ReadonlySet<string>>(new Set());

  const stop = useCallback((url: string | null) => {
    playingRef.current = url;
    setPlaying(url);
  }, []);

  const toggle = useCallback(
    (url: string) => {
      let a = audioRef.current;
      if (!a) {
        a = new Audio();
        a.addEventListener("ended", () => stop(null));
        // The error's target src is absolute; the ref remembers which of OUR
        // urls was asked for, so the faded mark lands on the right row.
        a.addEventListener("error", () => {
          const dead = pendingRef.current;
          if (dead) setFaded((prev) => new Set(prev).add(dead));
          stop(null);
        });
        audioRef.current = a;
      }
      if (playingRef.current === url) {
        a.pause();
        stop(null);
        return;
      }
      pendingRef.current = url;
      a.src = url;
      a.play().catch(() => {
        // NotSupportedError lands here on a 404 before the error event does
        // on some engines; same verdict either way.
        setFaded((prev) => new Set(prev).add(url));
        stop(null);
      });
      stop(url);
    },
    [stop],
  );

  useEffect(
    () => () => {
      audioRef.current?.pause();
      audioRef.current = null;
    },
    [],
  );

  return { playing, faded, toggle };
}

/** The reserved player slot every event row carries (fixed w-12 x h-8, the
 * no-layout-shift rule): a play/stop control when the event has a clip, the
 * quiet "faded" stamp once its file is known pruned, and a dim placeholder
 * dot when the clip write failed -- never a missing element, never a broken
 * player. Playing wears --led: green means live. */
function PlaySlot({ clip, player }: { clip: string | null; player: ClipPlayer }) {
  const url = clip ? clipUrl(clip) : null;
  const box =
    "flex h-8 w-12 shrink-0 items-center justify-center rounded-sm border";
  if (!url)
    return (
      <span className={`${box} border-line text-inkfaint`} title="no clip">
        <span className="text-[9px]">·</span>
      </span>
    );
  if (player.faded.has(url))
    return (
      <span
        className={`${box} border-line`}
        title="clip faded — pruned past the retention window"
      >
        <span className="stamp text-[9px] text-inkfaint">faded</span>
      </span>
    );
  const on = player.playing === url;
  return (
    <button
      type="button"
      onClick={() => player.toggle(url)}
      aria-label={on ? "Stop the clip" : "Play the clip"}
      title={on ? "stop" : "play the recording"}
      className={`${box} transition-colors ${
        on
          ? "border-led text-led"
          : "border-linebright text-inkdim hover:border-squirrel hover:text-squirrel"
      }`}
    >
      {on ? (
        <svg viewBox="0 0 16 16" className="lamp h-3 w-3" aria-hidden="true">
          <rect x="3.5" y="3.5" width="9" height="9" fill="currentColor" />
        </svg>
      ) : (
        <svg viewBox="0 0 16 16" className="h-3 w-3" aria-hidden="true">
          <path d="M4.5 2.8v10.4L13 8Z" fill="currentColor" />
        </svg>
      )}
    </button>
  );
}

// --- Shared chrome -----------------------------------------------------------

function PanelLabel({
  title,
  right,
}: {
  title: string;
  right?: React.ReactNode;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 px-4 pb-2 pt-3">
      <h2
        className="text-lg text-ink"
        style={{ fontFamily: "var(--font-display)" }}
      >
        {title}
      </h2>
      {right}
    </div>
  );
}

/** Earl's presence, off the retained audio/status ("online"/"offline" with
 * Last Will -- the weather/status contract): the masthead lamp that lets the
 * ticker's empty state mean what it says. Any other retained string renders
 * verbatim -- a future Earl can be "on coffee break" too. */
function EarlLamp({ busUp, status }: { busUp: boolean; status: string | null }) {
  if (!busUp)
    return <span className="stamp text-xs text-inkfaint">bus quiet</span>;
  if (status === "online")
    return (
      <span className="flex items-center gap-1.5">
        <span className="lamp h-2 w-2 rounded-full bg-led" />
        <span className="stamp text-xs text-led">earl, on the air</span>
      </span>
    );
  if (status === "offline")
    return (
      <span className="flex items-center gap-1.5">
        <span className="h-2 w-2 rounded-full bg-inkfaint opacity-60" />
        <span className="stamp text-xs text-inkfaint">earl, off the air</span>
      </span>
    );
  if (status)
    return (
      <span className="flex items-center gap-1.5">
        <span className="breathe h-2 w-2 rounded-full bg-turkey" />
        <span className="stamp text-xs text-inkdim">earl · {status}</span>
      </span>
    );
  return <span className="stamp text-xs text-inkfaint">no word from earl</span>;
}

/** Page masthead + the tab bar. Aviary is the only tab there is -- future
 * listening modules earn tabs when they exist, not placeholder chrome.
 * The lamp is optional because only the dashboard runs a bus client; the
 * profile page showing "bus quiet" would be a lie about a bus it never
 * asked. */
function AviaryMasthead({
  lamp,
  back,
}: {
  lamp?: { busUp: boolean; status: string | null };
  back?: { href: string; label: string };
}) {
  return (
    <header className="mb-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h1
          className="text-3xl text-ink"
          style={{ fontFamily: "var(--font-display)" }}
        >
          The Aviary
        </h1>
        <div className="flex items-center gap-4">
          {lamp && <EarlLamp busUp={lamp.busUp} status={lamp.status} />}
          <Link
            href={back?.href ?? "/"}
            className="stamp text-xs text-inkdim transition-colors hover:text-squirrel"
          >
            {back?.label ?? "← control center"}
          </Link>
        </div>
      </div>
      <p className="stamp mt-1 text-xs text-inkfaint">
        earl with the ears · every bird the yard has announced
      </p>
      <nav className="mt-3 flex gap-4 border-b border-line">
        <span className="stamp -mb-px border-b-2 border-ink pb-1.5 text-xs text-ink">
          aviary
        </span>
      </nav>
    </header>
  );
}

// --- The /aviary page --------------------------------------------------------

export function Aviary() {
  // The roster by species key; the grid's ORDER is separate state on
  // purpose -- it changes only on a sort click (and appends lifers), so
  // live count updates land in place and nothing reshuffles on its own.
  const [roster, setRoster] = useState<Record<string, RosterEntry>>({});
  const [order, setOrder] = useState<string[]>([]);
  const [sortKey, setSortKey] = useState<SortKey>("visits");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [rail, setRail] = useState<TodayTile[]>([]);
  const [events, setEvents] = useState<TickerEvent[]>([]);
  const [busUp, setBusUp] = useState(false);
  const [earlStatus, setEarlStatus] = useState<string | null>(null);
  const [midnight, setMidnight] = useState<number | null>(null);
  const [loaded, setLoaded] = useState(false);
  // Hydrated rows arrive already-read: they get no filed-flash on mount
  // (the broadcast view's openedWith lesson); only live arrivals flare.
  const hydratedKeys = useRef<Set<string>>(new Set());
  const midnightRef = useRef<number | null>(null);
  const player = useClipPlayer();

  useEffect(() => {
    // Local midnight, computed once client-side: the server can't know the
    // viewer's timezone, so the client says where its day began. A tab left
    // open past midnight keeps yesterday's boundary until reload -- accepted.
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    const mid = Math.floor(d.getTime() / 1000);
    midnightRef.current = mid;
    setMidnight(mid);

    fetch(`/aviary/roster?today=${mid}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : { species: [] }))
      .then((body: { species?: RosterEntry[] }) => {
        const entries = Array.isArray(body.species) ? body.species : [];
        setRoster(Object.fromEntries(entries.map((e) => [e.species_sci, e])));
        // Initial order matches the sort control's initial state (most
        // visits first) -- the literals here and in useState must agree.
        setOrder(rosterOrder(entries, "visits", "desc"));
        setRail(todayVisitors(entries));
      })
      .catch(() => {})
      .finally(() => setLoaded(true));

    fetch(`/aviary/recent?limit=${HYDRATE_LIMIT}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : { events: [] }))
      .then((body: { events?: unknown[] }) => {
        const rows = Array.isArray(body.events) ? body.events : [];
        const hydrated = rows
          .map(audioEventFrom)
          .filter((e): e is AudioEvent => e !== null)
          .map((e) => ({ ...e, key: audioEventKey(e) }));
        hydrated.forEach((e) => hydratedKeys.current.add(e.key));
        // Live arrivals may already be in state (the bus connects fast);
        // keys make the merge a dedupe, newest live rows staying on top.
        setEvents((prev) => {
          const seen = new Set(prev.map((e) => e.key));
          return [
            ...prev,
            ...hydrated.filter((e) => !seen.has(e.key)),
          ].slice(0, TICKER_CAP);
        });
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    // Straight to the broker over WebSockets -- the /daemon proxy can't
    // carry them (the Field Journal's client, one namespace over).
    const url = busUrl(
      window.location.hostname,
      process.env.NEXT_PUBLIC_MERLE_MQTT_WS,
    );
    const client = mqtt.connect(url, { reconnectPeriod: 3000 });
    client.on("connect", () => {
      setBusUp(true);
      client.subscribe([AUDIO_EVENTS_TOPIC, AUDIO_STATUS_TOPIC]);
    });
    client.on("close", () => setBusUp(false));
    // Mandatory: an unhandled mqtt.js "error" throws and wedges reconnect.
    client.on("error", (err) =>
      console.debug("[bus] error", err?.message ?? err),
    );
    client.on("message", (topic, payload) => {
      if (topic === AUDIO_STATUS_TOPIC) {
        setEarlStatus(payload.toString());
        return;
      }
      const event = parseAudioEvent(payload.toString());
      if (!event) return;
      const key = audioEventKey(event);
      setEvents((prev) =>
        prev.some((e) => e.key === key)
          ? prev
          : [{ ...event, key }, ...prev].slice(0, TICKER_CAP),
      );
      if (event.kind !== "detection") return;
      // A live detection IS a visit opening (#175: the listener publishes
      // only the opening window), so counting per event is the same rule
      // the roster's query-time grouping applies. The razor-thin race --
      // hydration tallying a row whose bus event then also arrives -- is a
      // ±1 a reload corrects; not worth a dedupe against the store.
      const sci = event.species_sci;
      const isToday =
        midnightRef.current !== null && event.ts >= midnightRef.current;
      setRoster((prev) => {
        const cur = prev[sci];
        if (cur)
          return {
            ...prev,
            [sci]: {
              ...cur,
              visits: cur.visits + 1,
              today: cur.today + (isToday ? 1 : 0),
            },
          };
        // A lifer with the page open: the store's INSERT OR IGNORE is
        // minting the same first-heard moment right now.
        return {
          ...prev,
          [sci]: {
            species_sci: sci,
            species_common: event.species_common,
            first_ts: event.ts,
            first_source: event.source,
            first_clip: event.clip,
            visits: 1,
            today: isToday ? 1 : 0,
          },
        };
      });
      // Lifers append at the END whatever the sort says -- re-sorting on
      // arrival is exactly the self-reshuffle house rule #1 bans.
      setOrder((prev) => (prev.includes(sci) ? prev : [...prev, sci]));
      if (isToday)
        setRail((prev) => {
          const i = prev.findIndex((t) => t.species_sci === sci);
          if (i < 0)
            return [
              ...prev,
              {
                species_sci: sci,
                species_common: event.species_common,
                count: 1,
              },
            ];
          const next = [...prev];
          next[i] = { ...next[i], count: next[i].count + 1 };
          return next;
        });
    });
    return () => {
      client.end(true);
    };
  }, []);

  const resort = (key: SortKey, dir: SortDir) => {
    setSortKey(key);
    setSortDir(dir);
    setOrder(rosterOrder(Object.values(roster), key, dir));
  };

  const sortButton = (key: SortKey, label: string) => (
    <button
      type="button"
      // Switching keys lands on that key's natural direction (names read
      // A-first, visit counts read busiest-first); the arrow flips it.
      onClick={() =>
        resort(key, key === sortKey ? sortDir : key === "name" ? "asc" : "desc")
      }
      aria-pressed={sortKey === key}
      className={`stamp rounded-sm border px-2 py-1 text-[10px] transition-colors ${
        sortKey === key
          ? "border-linebright text-ink"
          : "border-line text-inkfaint hover:border-linebright hover:text-inkdim"
      }`}
    >
      {label}
    </button>
  );

  return (
    <div className="mx-auto w-full max-w-[1500px] px-4 py-6">
      <AviaryMasthead lamp={{ busUp, status: earlStatus }} />
      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
        {/* Hero: the aviary grid */}
        <section className="panel self-start rounded-sm border border-line bg-panel">
          <PanelLabel
            title="The Life List"
            right={
              <span className="flex items-center gap-1.5">
                {sortButton("name", "name")}
                {sortButton("visits", "visits")}
                <button
                  type="button"
                  onClick={() =>
                    resort(sortKey, sortDir === "asc" ? "desc" : "asc")
                  }
                  aria-label={`Sort ${sortDir === "asc" ? "descending" : "ascending"}`}
                  title={sortDir === "asc" ? "ascending" : "descending"}
                  className="stamp rounded-sm border border-line px-2 py-1 text-[10px] text-inkdim transition-colors hover:border-linebright hover:text-ink"
                >
                  {sortDir === "asc" ? "↑" : "↓"}
                </button>
              </span>
            }
          />
          <div className="px-4 pb-4">
            {order.length === 0 ? (
              <div className="flex min-h-[280px] items-center justify-center rounded-sm border border-line bg-panel2">
                <span className="stamp text-xs text-inkfaint">
                  {loaded
                    ? "no birds on record yet — earl is listening"
                    : "opening the record …"}
                </span>
              </div>
            ) : (
              <ul className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-4">
                {order.map((sci) => {
                  const e = roster[sci];
                  if (!e) return null;
                  return (
                    <li key={sci}>
                      <Link
                        href={`/aviary/${encodeURIComponent(sci)}`}
                        className="group flex h-full flex-col gap-2 rounded-sm border border-line bg-panel2 p-3 transition-colors hover:border-linebright"
                      >
                        <Portrait
                          sci={sci}
                          has={Boolean(e.image_file)}
                          alt={e.species_common}
                          glyphClass="h-12 w-12"
                          w={e.image_w}
                          h={e.image_h}
                          boxAspect={4 / 3}
                          className="aspect-[4/3] w-full rounded-sm border border-line bg-panel transition-colors group-hover:text-inkdim"
                        />
                        <span className="min-w-0">
                          <span
                            className="block truncate text-ink"
                            style={{ fontFamily: "var(--font-display)" }}
                          >
                            {e.species_common}
                          </span>
                          {/* Two clamped lines of the lead, reserved whether
                              or not prose has arrived -- enrichment landing
                              never shifts the grid (#184). */}
                          <span className="line-clamp-2 min-h-[2.6em] text-[11px] leading-[1.3] text-inkdim">
                            {e.description ?? ""}
                          </span>
                          <span className="block text-xs text-inkdim">
                            {e.visits === 1 ? "1 visit" : `${e.visits} visits`}
                            {e.today > 0 && (
                              <span className="text-led"> · {e.today} today</span>
                            )}
                          </span>
                          <span className="stamp block text-[9px] text-inkfaint">
                            first heard {dayOf(e.first_ts)}
                          </span>
                        </span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </section>

        {/* Right rail */}
        <div className="flex flex-col gap-4">
          <section className="panel rounded-sm border border-line bg-panel">
            <PanelLabel title="Latest Events" />
            <div className="px-4 pb-4">
              {events.length === 0 ? (
                <div className="flex min-h-[160px] items-center justify-center rounded-sm border border-line bg-panel2">
                  <span className="stamp px-4 text-center text-xs text-inkfaint">
                    {busUp
                      ? "listening — no arrivals yet"
                      : "bus quiet — live arrivals paused"}
                  </span>
                </div>
              ) : (
                <ul className="scrollpane flex max-h-[540px] flex-col gap-1.5 overflow-y-auto pr-1">
                  {events.map((e) => (
                    <li
                      key={e.key}
                      className={
                        hydratedKeys.current.has(e.key) ? "" : "journal-filed"
                      }
                    >
                      {e.kind === "detection" ? (
                        <div className="flex items-center gap-2.5 rounded-sm border border-line bg-panel2 px-2.5 py-2">
                          <TickerThumb
                            sci={e.species_sci}
                            has={Boolean(roster[e.species_sci]?.image_file)}
                            w={roster[e.species_sci]?.image_w}
                            h={roster[e.species_sci]?.image_h}
                          />
                          <PlaySlot clip={e.clip} player={player} />
                          <div className="min-w-0 flex-1">
                            <div className="flex items-baseline justify-between gap-2">
                              <Link
                                href={`/aviary/${encodeURIComponent(e.species_sci)}`}
                                className="truncate text-sm text-ink transition-colors hover:text-squirrel"
                                style={{ fontFamily: "var(--font-display)" }}
                              >
                                {e.species_common}
                              </Link>
                              <span className="shrink-0 text-[10px] text-inkdim">
                                {stampOf(e.ts, midnight)}
                              </span>
                            </div>
                            <div className="stamp flex gap-2 text-[9px] text-inkfaint">
                              <span>{e.source}</span>
                              <span>{e.confidence.toFixed(2)}</span>
                              {e.wind_suspect && <span>wind?</span>}
                            </div>
                          </div>
                        </div>
                      ) : (
                        // A notable sound (#174): quieter, species-less, and
                        // bus-only -- it vanishes on reload by design (#182).
                        <div className="flex items-center gap-2.5 rounded-sm border border-line/60 px-2.5 py-1.5 opacity-70">
                          <PlaySlot clip={e.clip} player={player} />
                          <div className="flex min-w-0 flex-1 items-baseline justify-between gap-2">
                            <span className="stamp truncate text-[10px] lowercase text-inkfaint">
                              {e.class}
                            </span>
                            <span className="shrink-0 text-[10px] text-inkfaint">
                              {stampOf(e.ts, midnight)}
                            </span>
                          </div>
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </section>

          <section className="panel rounded-sm border border-line bg-panel">
            <PanelLabel title="Today's Visitors" />
            <div className="px-4 pb-4">
              {rail.length === 0 ? (
                <div className="flex min-h-[56px] items-center justify-center rounded-sm border border-line bg-panel2">
                  <span className="stamp text-xs text-inkfaint">
                    no visitors since midnight
                  </span>
                </div>
              ) : (
                <ul className="flex min-h-[56px] flex-wrap content-start gap-2">
                  {rail.map((t) => (
                    <li key={t.species_sci}>
                      <Link
                        href={`/aviary/${encodeURIComponent(t.species_sci)}`}
                        className="flex items-baseline gap-2 rounded-sm border border-line bg-panel2 px-2.5 py-1.5 transition-colors hover:border-linebright"
                      >
                        <span className="text-xs text-ink">
                          {t.species_common}
                        </span>
                        <span className="text-xs text-inkdim">{t.count}</span>
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

// --- The /aviary/[species] page ---------------------------------------------

/** The field-naturalist blocks (#186). Prose written by the analysis pass on
 * pearl and merely read here -- nothing generates at render time, ever. The
 * footprint is reserved in every state, so the blocks landing (or being
 * absent on day one) can't shift the page. */
function FieldNotes({ analysis }: { analysis: Analysis | null }) {
  const has = analysis?.rhythm || analysis?.weather;
  return (
    <section className="panel mt-4 rounded-sm border border-line bg-panel">
      <div className="flex items-baseline justify-between gap-3 px-4 pb-2 pt-3">
        <h2
          className="text-lg text-ink"
          style={{ fontFamily: "var(--font-display)" }}
        >
          Field Notes
        </h2>
        {analysis?.generated_ts && (
          <span className="stamp text-[10px] text-inkfaint">
            as of{" "}
            {new Date(analysis.generated_ts * 1000).toLocaleDateString([], {
              month: "short",
              day: "numeric",
            })}
            {analysis.model ? ` · ${analysis.model}` : ""}
          </span>
        )}
      </div>
      <div className="grid min-h-[120px] gap-5 px-4 pb-4 md:grid-cols-2">
        {has ? (
          <>
            <Note title="the rhythm" text={analysis?.rhythm ?? null} />
            <Note title="weather & timing" text={analysis?.weather ?? null} />
          </>
        ) : (
          <div className="flex min-h-[120px] items-center justify-center rounded-sm border border-line bg-panel2 md:col-span-2">
            <span className="stamp px-6 text-center text-xs text-inkfaint">
              no field notes yet — they arrive with the analysis pass
            </span>
          </div>
        )}
      </div>
    </section>
  );
}

function Note({ title, text }: { title: string; text: string | null }) {
  return (
    <div className="min-w-0">
      <h3 className="stamp mb-1.5 text-[10px] text-inkfaint">{title}</h3>
      {text ? (
        <div className="space-y-2.5 text-sm leading-relaxed text-inkdim">
          {text.split(/\n+/).map((para, i) => (
            <p key={i}>{para}</p>
          ))}
        </div>
      ) : (
        <p className="stamp text-[9px] text-inkfaint">not written yet</p>
      )}
    </div>
  );
}

type Analysis = {
  rhythm: string | null;
  weather: string | null;
  model: string | null;
  generated_ts: number | null;
};

export function SpeciesProfile({ sci }: { sci: string }) {
  const [entry, setEntry] = useState<RosterEntry | null>(null);
  const [visits, setVisits] = useState<Visit[] | null>(null);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [midnight, setMidnight] = useState<number | null>(null);
  // The description is clamped at rest (#196). Only long leads earn the
  // toggle -- a two-sentence stub with a "read more" under it would be a
  // control that does nothing visible.
  const [bioOpen, setBioOpen] = useState(false);
  const player = useClipPlayer();

  useEffect(() => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    const mid = Math.floor(d.getTime() / 1000);
    setMidnight(mid);
    // The roster carries this species' totals, first-heard, and today count
    // (the same grouped counts the grid shows -- one counting rule
    // everywhere); the per-species recent cut becomes the visits list.
    fetch(`/aviary/roster?today=${mid}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : { species: [] }))
      .then((body: { species?: RosterEntry[] }) => {
        const found = (Array.isArray(body.species) ? body.species : []).find(
          (e) => e.species_sci === sci,
        );
        setEntry(found ?? null);
      })
      .catch(() => {})
      .finally(() => setLoaded(true));
    fetch(
      `/aviary/recent?species=${encodeURIComponent(sci)}&limit=${PROFILE_ROWS}`,
      { cache: "no-store" },
    )
      .then((r) => (r.ok ? r.json() : { events: [] }))
      .then((body: { events?: unknown[] }) => {
        const rows = (Array.isArray(body.events) ? body.events : [])
          .map(audioEventFrom)
          .filter((e): e is BirdEvent => e?.kind === "detection");
        setVisits(collapseVisits(rows));
      })
      .catch(() => setVisits([]));
    // The field notes (#186): read-only, already written by the pass.
    fetch(`/aviary/analysis/${encodeURIComponent(sci)}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((a: Analysis | null) => setAnalysis(a))
      .catch(() => {});
    setBioOpen(false); // a different bird opens closed
  }, [sci]);

  // The clamp shows six lines; anything near that is worth a toggle. Measured
  // in characters rather than by probing the DOM for overflow -- the reflow
  // that would need runs after paint, and a control appearing a frame late is
  // the layout-shift rule broken in miniature.
  const longBio = (entry?.description?.length ?? 0) > 420;

  return (
    <div className="mx-auto w-full max-w-[1500px] px-4 py-6">
      <AviaryMasthead back={{ href: "/aviary", label: "← the aviary" }} />
      {loaded && !entry ? (
        <section className="panel flex min-h-[200px] items-center justify-center rounded-sm border border-line bg-panel">
          <span className="stamp text-xs text-inkfaint">
            no bird by that name in the record
          </span>
        </section>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_340px]">
          {/* Stretch, not items-start: the hero and the rail must share a
              bottom edge. The hero sets the row's height (see the rail's
              wrapper below) and the rail then matches it exactly. */}
          <section className="panel relative overflow-hidden rounded-sm border border-line bg-panel p-4">
            {/* The bird wears its own portrait (#196), the music player's
                album-hero idiom on this palette. #157's finding carries
                over and is not re-litigated here: a heavy blur read as a
                colour smear, so this is a lightly softened band of the
                actual photo -- the bird stays recognizable. scale-105 keeps
                blur-softened edges off-frame. Faded harder than the album
                hero's 75%: bird photos are high-frequency (feathers, twigs,
                foliage) where cover art is flat, and this hero carries real
                body prose rather than a title and a stamp row.
                Un-enriched species get no backdrop at all -- exactly the
                flat panel they render today, and no layout difference. */}
            {entry?.image_file && (
              <div className="pointer-events-none absolute inset-0" aria-hidden>
                <img
                  src={portraitUrl(sci)}
                  alt=""
                  className="h-full w-full scale-105 object-cover object-top opacity-[0.78] blur-md saturate-[1.2]"
                />
                {/* Two scrims share the legibility job (the AlbumView
                    trick): the house bottom-up fade, plus a right-anchored
                    one under the text column specifically -- darkest
                    exactly where the prose runs, lightest where the art is
                    the point.
                    **Crank the art, then scrim the text** -- the first pass
                    got this backwards, dimming the photo to 40% AND using a
                    light scrim, which spent the contrast budget everywhere
                    and showed the bird nowhere. These numbers come from a
                    parameter sweep over every portrait actually on the life
                    list, compositing this exact layer stack in a canvas and
                    keeping the most visible setting whose WORST body-text
                    contrast still clears AA: 2.7x the backdrop luminance of
                    the first pass at 4.64:1 worst case. Re-tune by sweeping,
                    not by eye -- a value that looks fine on the jay fails on
                    the robin, whose photo is much brighter.
                    Layout drives the asymmetry: the floated portrait covers
                    the LEFT, so a left-open scrim shows image where nothing
                    can be seen anyway, while the top band is where the
                    header sits (2xl display type, which tolerates a busier
                    ground than body prose does). */}
                <div className="absolute inset-0 bg-gradient-to-t from-panel via-panel/75 to-panel/5" />
                <div className="absolute inset-0 bg-gradient-to-l from-panel/95 via-panel/[0.78] to-transparent" />
              </div>
            )}
            <div className="relative">
            <h2
              className="text-2xl text-ink"
              style={{ fontFamily: "var(--font-display)" }}
            >
              {entry?.species_common ?? "…"}
            </h2>
            <p className="text-sm italic text-inkdim">{sci}</p>
            <dl className="mt-3 flex flex-wrap gap-x-6 gap-y-1.5 text-xs">
              <div>
                <dt className="stamp text-[9px] text-inkfaint">first heard</dt>
                <dd className="text-inkdim">
                  {entry ? (
                    <>
                      {dateOf(entry.first_ts)}
                      <span className="text-inkfaint">
                        {" "}
                        · via {entry.first_source}
                      </span>
                    </>
                  ) : (
                    "—"
                  )}
                </dd>
              </div>
              <div>
                <dt className="stamp text-[9px] text-inkfaint">visits</dt>
                <dd className="text-inkdim">{entry ? entry.visits : "—"}</dd>
              </div>
              <div>
                <dt className="stamp text-[9px] text-inkfaint">today</dt>
                <dd className="text-inkdim">{entry ? entry.today : "—"}</dd>
              </div>
            </dl>
            {/* The magazine body (#192): the portrait + its CC-BY credit
                float left in the description's text flow -- the Field
                Journal's magazine-wrap precedent, and flow-root so a short
                description still holds the photo's full height. The float
                drops on small screens, where wrapped text would be a
                two-words-per-line ribbon. Phase 4's visit analysis lands
                under this. */}
            <div className="mt-4 flow-root">
              <div className="mb-3 w-full md:float-left md:mb-2 md:mr-5 md:w-[300px]">
                {/* The profile crops NOTHING (#185): with real dimensions
                    the figure takes the photo's own shape, so the whole
                    bird is always in frame. The ratio is reserved before
                    the bytes land, so the photo arriving shifts nothing;
                    an un-backfilled row falls back to the old 4:3. */}
                <Portrait
                  sci={sci}
                  has={Boolean(entry?.image_file)}
                  alt={entry?.species_common ?? sci}
                  glyphClass="h-16 w-16"
                  w={entry?.image_w}
                  h={entry?.image_h}
                  boxAspect={4 / 3}
                  style={{
                    aspectRatio: portraitAspect(entry?.image_w, entry?.image_h),
                  }}
                  className="w-full rounded-sm border border-line bg-panel2"
                />
                {entry?.image_attribution && (
                  <p className="stamp mt-1.5 text-[9px] leading-relaxed text-inkfaint">
                    {entry.image_attribution}
                  </p>
                )}
              </div>
              {entry?.description ? (
                <>
                  {/* Clamped by default (#196, the ArtistView bio pattern):
                      Wikipedia's lead runs four or five paragraphs, which
                      buried the chart and the field notes below the fold and
                      is more encyclopedia than anyone asked for. It is also
                      what makes the backdrop legible -- a short block of
                      prose over softened art reads where five paragraphs
                      would not. The float keeps the photo beside it either
                      way, so expanding grows the hero and shifts nothing
                      above it. */}
                  <div
                    className={`space-y-2.5 text-sm leading-relaxed text-inkdim ${
                      bioOpen ? "" : "line-clamp-6"
                    }`}
                  >
                    {entry.description.split(/\n+/).map((para, i) => (
                      <p key={i}>{para}</p>
                    ))}
                  </div>
                  {longBio && (
                    <button
                      type="button"
                      onClick={() => setBioOpen((o) => !o)}
                      className="mt-1.5 text-sm text-ink underline decoration-line underline-offset-4 transition-colors hover:decoration-linebright"
                    >
                      {bioOpen ? "read less" : "read more"}
                    </button>
                  )}
                </>
              ) : (
                <p className="stamp text-[9px] text-inkfaint">
                  field notes arrive with the enrichment pass
                </p>
              )}
            </div>
            </div>
          </section>

          {/* The right rail (#192): visits don't need the page's width, and
              moving them clears the full-width floor below this grid for
              Phase 3's visits-over-time chart.
              The wrapper contributes NO height of its own on lg -- its only
              child is absolutely positioned -- so the grid row is sized by
              the hero alone and the rail then fills it exactly, bottom edges
              flush. Letting the rail size itself instead would misalign it
              in both directions: a bird with two visits ends short, and one
              with forty runs past. Below lg the panels stack, so the
              absolute positioning drops away and the rail flows normally. */}
          <div className="relative">
          <section className="panel flex flex-col rounded-sm border border-line bg-panel lg:absolute lg:inset-0">
            <PanelLabel title="Recent Visits" />
            <div className="flex min-h-0 flex-1 flex-col px-4 pb-4">
              {visits === null ? (
                <div className="flex min-h-[120px] items-center justify-center rounded-sm border border-line bg-panel2">
                  <span className="stamp text-xs text-inkfaint">
                    opening the record …
                  </span>
                </div>
              ) : visits.length === 0 ? (
                <div className="flex min-h-[120px] items-center justify-center rounded-sm border border-line bg-panel2">
                  <span className="stamp text-xs text-inkfaint">
                    no visits on record
                  </span>
                </div>
              ) : (
                // On lg this fills whatever height the hero set, scrolling
                // inside it -- a max-height there would fight the stretch
                // and reopen the very gap this closes. Below lg the panels
                // stack and there is no hero to match, so the cap stays:
                // without it the list renders all 200 rows at ~11,000px and
                // buries the chart under a mile of scrolling.
                <ul className="scrollpane flex max-h-[560px] min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto pr-1 lg:max-h-none">
                  {visits.map((v) => (
                    <li
                      key={v.ts}
                      className="flex items-center gap-2.5 rounded-sm border border-line bg-panel2 px-2.5 py-2"
                    >
                      <PlaySlot clip={v.clip} player={player} />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-baseline justify-between gap-2">
                          <span className="text-xs text-ink">
                            {stampOf(v.ts, midnight)}
                          </span>
                          <span className="stamp shrink-0 text-[9px] text-inkfaint">
                            {v.source}
                          </span>
                        </div>
                        <div className="stamp flex gap-2 text-[9px] text-inkfaint">
                          <span>best {v.best.toFixed(2)}</span>
                          {v.windows > 1 && <span>{v.windows} windows</span>}
                          {v.wind_suspect && <span>wind?</span>}
                        </div>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </section>
          </div>
        </div>
      )}
      {/* Full-width under both columns, the floor #192's layout cleared:
          the chart (#185), then the prose written about it (#186). Only for
          a bird actually in the record -- an unknown species has no rhythm
          to draw and nothing to say. */}
      {entry && (
        <>
          <VisitsChart sci={sci} />
          <FieldNotes analysis={analysis} />
        </>
      )}
    </div>
  );
}
