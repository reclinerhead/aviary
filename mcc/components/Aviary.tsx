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
import { useRouter } from "next/navigation";
import {
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
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
  ARRIVALS_24H_S,
  ARRIVALS_WEEK_S,
  AnalysisStats,
  RosterEntry,
  SortDir,
  SortKey,
  Visit,
  archiveStats,
  clipUrl,
  enhancedClipUrl,
  collapseVisits,
  cropPosition,
  dayAnchor,
  dayGroups,
  dayStart,
  liferNumber,
  newArrivals,
  nextBefore,
  parseSpeciesFilter,
  portraitAspect,
  portraitUrl,
  rhythmStrip,
  rivalLine,
  rosterOrder,
  shareOfYard,
  standingFor,
  todayVisitors,
  weatherChips,
  yardRecords,
} from "@/lib/aviary";
import { VisitsChart } from "@/components/VisitsChart";

// Ticker sizing: hydrate the newest 50, let live arrivals grow it to 80
// before the oldest fall off -- the JOURNAL_LIMIT idea, one namespace over.
const HYDRATE_LIMIT = 50;
const TICKER_CAP = 80;
// The profile reads enough raw rows to survive a pre-#175 morning (25
// windows per visit) and still show a real page of visits.
const PROFILE_ROWS = 200;
// The archive's page size (#211): one fetch per scroll-reach, deduped by
// key against the cursor's deliberate one-row overlap.
const ARCHIVE_PAGE = 100;

type TickerEvent = AudioEvent & { key: string };
type ArchiveRow = BirdEvent & { key: string };
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
/** The hero's "listening since" value (#260): dayOf plus the year, because
 * this date is the one place the band speaks about years, not days. */
const sinceOf = (ts: number) =>
  new Date(ts * 1000).toLocaleDateString([], {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
/** Time alone for today's moments, day + time for older ones. `midnight`
 * comes from state (computed once on mount), never Date.now() in render --
 * the SSR/hydration rule; rows only exist client-side anyway. */
const stampOf = (ts: number, midnight: number | null) =>
  midnight !== null && ts >= midnight
    ? timeOf(ts)
    : `${dayOf(ts)} · ${timeOf(ts)}`;
/** The archive's day headers (#211): weekday + date, the year only when it
 * isn't this one. Render-time locale calls are fine here for the same reason
 * stampOf's are -- archive rows only exist client-side. */
const dayLabelOf = (day: number) => {
  const d = new Date(day * 1000);
  return d.toLocaleDateString([], {
    weekday: "long",
    month: "long",
    day: "numeric",
    ...(d.getFullYear() === new Date().getFullYear()
      ? {}
      : { year: "numeric" }),
  });
};
/** Seconds-into-day -> "5:41 am", the records panel's clock voice (#220). */
const clockOf = (secs: number) => {
  const h24 = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return `${h24 % 12 || 12}:${String(m).padStart(2, "0")} ${h24 < 12 ? "am" : "pm"}`;
};
/** A local epoch -> the date input's "yyyy-mm-dd" (dayAnchor's inverse leg:
 * both sides speak the VIEWER's calendar, never UTC's). */
const dateInputOf = (ts: number) => {
  const d = new Date(ts * 1000);
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${mm}-${dd}`;
};

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
  // The archive (#211) wears the same thumb at h-16; the ticker keeps its
  // h-9. Two real callers, one geometry rule -- the size is the only knob.
  box = "h-9 w-9",
  glyph = "h-5 w-5",
}: {
  sci: string;
  has: boolean;
  w?: number | null;
  h?: number | null;
  box?: string;
  glyph?: string;
}) {
  const [lost, setLost] = useState(false);
  return (
    <span
      className={`flex ${box} shrink-0 items-center justify-center overflow-hidden rounded-sm border border-line bg-panel text-inkfaint`}
    >
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
        <BirdGlyph className={glyph} />
      )}
    </span>
  );
}

// --- Clip playback -----------------------------------------------------------

type ClipPlayer = {
  playing: string | null;
  faded: ReadonlySet<string>;
  enhanced: boolean;
  setEnhanced: (on: boolean) => void;
  toggle: (clip: string) => void;
};

/** One shared <audio> per view: clicking a second clip stops the first
 * (two yards' worth of birdsong at once is noise, not data). A clip that
 * fails to load is marked faded -- pruned past the retention window is the
 * normal end of a clip's life, the Field Journal's pruned-thumbnail rule.
 *
 * ENHANCED PLAYBACK (issue #190). In enhanced mode the player asks for the
 * pass's `-enh` sibling first and, if the route 404s, retries the raw clip
 * and says nothing. That silent retry IS the existence check: the pass keeps
 * no state about which clips it has processed -- file existence is the
 * source of truth, deliberately -- so the only honest way to ask "is there
 * an enhanced version?" is to ask for it. A clip only earns the faded mark
 * once the RAW file is gone too, which is the one thing that actually means
 * the recording is pruned.
 *
 * Every key here -- `playing`, `faded`, `pending` -- is the RAW clip path,
 * never the sibling. Rows are identified by the recording, not by which
 * rendering of it happened to play, so flipping the mode mid-session can't
 * strand a lit button or double-mark a faded one. */
function useClipPlayer(): ClipPlayer {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const playingRef = useRef<string | null>(null);
  // Aborted when an attempt ends, which is what makes each attempt's failure
  // signals identifiable -- see the note in `start`.
  const attemptRef = useRef<AbortController | null>(null);
  const enhancedRef = useRef(true);
  const [playing, setPlaying] = useState<string | null>(null);
  const [faded, setFaded] = useState<ReadonlySet<string>>(new Set());
  // Default ON: a faint bird under a plane is the case that motivated the
  // pass, and the raw clip is one click away for the A/B.
  const [enhanced, setEnhancedState] = useState(true);

  const stop = useCallback((clip: string | null) => {
    playingRef.current = clip;
    setPlaying(clip);
  }, []);

  /** Point the element at one rendering of `clip` and play it.
   *
   * A missing file reports itself TWICE -- the element's `error` event and
   * the `play()` promise's rejection, in an order that varies by engine --
   * and the fallback made that a real bug: the error event would kick off
   * the raw retry, and then the stale rejection from the *enhanced* attempt
   * would land, read the now-current state, and mark a clip faded that was
   * at that moment playing perfectly. (Measured: a clip with no sibling fell
   * back to raw, fetched 200, and still wore the faded stamp.)
   *
   * So each attempt owns an AbortController. Whichever signal arrives first
   * aborts it and acts; the second sees an aborted controller and returns.
   * Listeners are registered against the signal, so a superseded attempt
   * detaches itself rather than lingering to misjudge its successor. */
  const start = useCallback(
    (a: HTMLAudioElement, clip: string, useEnhanced: boolean) => {
      attemptRef.current?.abort();
      const ctl = new AbortController();
      attemptRef.current = ctl;
      const enh = useEnhanced ? enhancedClipUrl(clip) : null;

      const failed = () => {
        if (ctl.signal.aborted) return; // this attempt was already resolved
        ctl.abort();
        if (enh !== null) {
          // No sibling for this one. Fall back quietly -- an un-enhanced clip
          // is not an error, it's a clip the pass hasn't reached yet. This
          // silent retry IS the existence check (see the hook's note).
          start(a, clip, false);
          return;
        }
        setFaded((prev) => new Set(prev).add(clip));
        stop(null);
      };

      a.addEventListener("error", failed, { signal: ctl.signal });
      a.src = enh ?? clipUrl(clip);
      a.play().catch(failed);
      stop(clip);
    },
    [stop],
  );

  const toggle = useCallback(
    (clip: string) => {
      let a = audioRef.current;
      if (!a) {
        a = new Audio();
        a.addEventListener("ended", () => stop(null));
        audioRef.current = a;
      }
      if (playingRef.current === clip) {
        a.pause();
        stop(null);
        return;
      }
      start(a, clip, enhancedRef.current);
    },
    [start, stop],
  );

  const setEnhanced = useCallback((on: boolean) => {
    enhancedRef.current = on;
    setEnhancedState(on);
    // Whatever is playing was rendered the other way; let it finish rather
    // than restarting it under the listener (house rule #1's spirit -- the
    // control changes what plays NEXT, it doesn't yank the current clip).
  }, []);

  useEffect(
    () => () => {
      audioRef.current?.pause();
      audioRef.current = null;
    },
    [],
  );

  return { playing, faded, enhanced, setEnhanced, toggle };
}

/** The raw/enhanced switch, in a panel's label row. Two states, both always
 * rendered at a fixed width -- it reads as one instrument nameplate rather
 * than a control that resizes as you use it (house rule #1). */
function EnhanceToggle({ player }: { player: ClipPlayer }) {
  return (
    <span className="flex items-center gap-1">
      <span className="stamp text-[9px] text-inkfaint">audio</span>
      <span className="flex overflow-hidden rounded-sm border border-line">
        {([
          ["raw", false],
          ["enh", true],
        ] as const).map(([label, on]) => (
          <button
            key={label}
            type="button"
            onClick={() => player.setEnhanced(on)}
            aria-pressed={player.enhanced === on}
            title={
              on
                ? "play the enhanced clip — band-limited, denoised, normalized"
                : "play the clip exactly as Earl recorded it"
            }
            className={`stamp w-8 py-0.5 text-[9px] transition-colors ${
              player.enhanced === on
                ? "bg-panel2 text-squirrel"
                : "text-inkfaint hover:text-inkdim"
            }`}
          >
            {label}
          </button>
        ))}
      </span>
    </span>
  );
}

/** New Arrivals' window switch (#224): the EnhanceToggle nameplate, one
 * knob over -- two states, both always rendered at a fixed width. */
function ArrivalsToggle({
  windowS,
  onChange,
}: {
  windowS: number;
  onChange: (s: number) => void;
}) {
  return (
    <span className="flex items-center gap-1">
      <span className="stamp text-[9px] text-inkfaint">window</span>
      <span className="flex overflow-hidden rounded-sm border border-line">
        {(
          [
            ["24h", ARRIVALS_24H_S],
            ["7d", ARRIVALS_WEEK_S],
          ] as const
        ).map(([label, s]) => (
          <button
            key={label}
            type="button"
            onClick={() => onChange(s)}
            aria-pressed={windowS === s}
            title={
              s === ARRIVALS_24H_S
                ? "species first heard in the last 24 hours"
                : "species first heard in the last week"
            }
            className={`stamp w-8 py-0.5 text-[9px] transition-colors ${
              windowS === s
                ? "bg-panel2 text-squirrel"
                : "text-inkfaint hover:text-inkdim"
            }`}
          >
            {label}
          </button>
        ))}
      </span>
    </span>
  );
}

/** New Arrivals (#224): the yard's newest species, featured -- a lifer is
 * the most exciting thing Earl ever reports, and it deserves more than a
 * ticker row. Each arrival is a card: the portrait at full rail width (the
 * 4:3 top-crop tile idiom -- the head stays in frame; a lifer the
 * enrichment loop hasn't dressed yet wears the pending glyph in the same
 * reserved frame), names, the first-heard moment, its lifer number, and a
 * play slot for the first-contact recording. Derives from the roster the
 * grid already maintains -- zero new fetches -- so a live lifer surfaces
 * here the moment the bus announces it (the ticker-prepend precedent:
 * appearing IS this panel's purpose; existing cards never reorder). The
 * empty state holds the panel's footprint, which is most days -- the quiet
 * frame is what makes a card landing feel like an event. */
function NewArrivals({
  roster,
  now,
  midnight,
  player,
}: {
  roster: Record<string, RosterEntry>;
  now: number | null;
  midnight: number | null;
  player: ClipPlayer;
}) {
  const [windowS, setWindowS] = useState<number>(ARRIVALS_24H_S);
  const entries = Object.values(roster);
  const arrivals = now === null ? [] : newArrivals(entries, now - windowS);
  return (
    <section
      id="new-arrivals"
      className="panel scroll-mt-4 rounded-sm border border-line bg-panel"
    >
      <PanelLabel
        title="New Arrivals"
        right={<ArrivalsToggle windowS={windowS} onChange={setWindowS} />}
      />
      <div className="px-4 pb-4">
        {arrivals.length === 0 ? (
          <div className="flex min-h-[88px] items-center justify-center rounded-sm border border-line bg-panel2">
            <span className="stamp px-4 text-center text-xs text-inkfaint">
              no new species in the last{" "}
              {windowS === ARRIVALS_24H_S ? "24 hours" : "week"}
            </span>
          </div>
        ) : (
          <ul className="flex flex-col gap-3">
            {arrivals.map((e) => {
              const lifer = liferNumber(entries, e.species_sci);
              return (
                <li
                  key={e.species_sci}
                  className="overflow-hidden rounded-sm border border-line bg-panel2"
                >
                  <Link
                    href={`/aviary/${encodeURIComponent(e.species_sci)}`}
                    className="block"
                  >
                    <Portrait
                      sci={e.species_sci}
                      has={Boolean(e.image_file)}
                      alt={e.species_common}
                      glyphClass="h-12 w-12"
                      w={e.image_w}
                      h={e.image_h}
                      boxAspect={4 / 3}
                      style={{ aspectRatio: "4 / 3" }}
                      className="w-full border-b border-line bg-panel"
                    />
                  </Link>
                  <div className="flex items-center gap-2.5 px-3 py-2.5">
                    <div className="min-w-0 flex-1">
                      <Link
                        href={`/aviary/${encodeURIComponent(e.species_sci)}`}
                        className="block truncate text-base text-ink transition-colors hover:text-squirrel"
                        style={{ fontFamily: "var(--font-display)" }}
                      >
                        {e.species_common}
                      </Link>
                      <p className="truncate text-[11px] italic text-inkdim">
                        {e.species_sci}
                      </p>
                      <div className="stamp mt-0.5 flex flex-wrap gap-x-2 text-[9px] text-inkfaint">
                        <span>first heard {stampOf(e.first_ts, midnight)}</span>
                        <span>via {e.first_source}</span>
                        {lifer && <span>lifer No. {lifer.n}</span>}
                      </div>
                    </div>
                    {/* The first-contact recording, right where the
                        excitement is (#220's affordance, promoted). */}
                    <PlaySlot clip={e.first_clip} player={player} />
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}

/** The reserved player slot every event row carries (fixed w-12 x h-8, the
 * no-layout-shift rule): a play/stop control when the event has a clip, the
 * quiet "faded" stamp once its file is known pruned, and a dim placeholder
 * dot when the clip write failed -- never a missing element, never a broken
 * player. Playing wears --led: green means live. */
function PlaySlot({ clip, player }: { clip: string | null; player: ClipPlayer }) {
  const box =
    "flex h-8 w-12 shrink-0 items-center justify-center rounded-sm border";
  if (!clip)
    return (
      <span className={`${box} border-line text-inkfaint`} title="no clip">
        <span className="text-[9px]">·</span>
      </span>
    );
  if (player.faded.has(clip))
    return (
      <span
        className={`${box} border-line`}
        title="clip faded — pruned past the retention window"
      >
        <span className="stamp text-[9px] text-inkfaint">faded</span>
      </span>
    );
  const on = player.playing === clip;
  return (
    <button
      type="button"
      onClick={() => player.toggle(clip)}
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
  jumps,
}: {
  lamp?: { busUp: boolean; status: string | null };
  back?: { href: string; label: string };
  /** Small-viewport anchor chips in the tab bar (#265) -- only the main
   * page has the stacked-columns problem (and the sections) they jump to,
   * so only it passes them; the lamp precedent. Plain hash anchors on
   * purpose: instant, pre-hydration, shareable. */
  jumps?: { href: string; label: string }[];
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
      <nav className="mt-3 flex items-baseline gap-4 border-b border-line">
        <span className="stamp -mb-px border-b-2 border-ink pb-1.5 text-xs text-ink">
          aviary
        </span>
        {jumps && (
          <span className="ml-auto flex gap-4 lg:hidden">
            {jumps.map((j) => (
              <a
                key={j.href}
                href={j.href}
                className="stamp pb-1.5 text-xs text-inkdim transition-colors hover:text-squirrel"
              >
                {j.label}
              </a>
            ))}
          </span>
        )}
      </nav>
    </header>
  );
}

// --- The /aviary page --------------------------------------------------------

/** One hero tile (#260): the StandingTile idiom scaled up for the page hero
 * -- stamp label, display-font value, sub line. Deliberately its own shape
 * rather than a StandingTile prop: the standings band (#220) keeps its look
 * untouched, and the hero's values earn a responsive step up. */
function HeroTile({
  label,
  value,
  sub,
  // The date tile's escape hatch: "Jun 20, 2026" is wider than any count
  // and clips a phone-width column at the default scale, so it alone steps
  // down one size below sm. Every row keeps a default-scale count tile
  // beside it, and the grid row is sized by the tallest cell -- so the
  // band's height (the fresh overlay's footprint) never moves.
  valueClass = "text-2xl sm:text-3xl xl:text-4xl",
}: {
  label: string;
  value: string;
  sub: string;
  valueClass?: string;
}) {
  return (
    <div className="panel rounded-sm border border-line bg-panel p-4">
      <div className="stamp text-[10px] text-inkfaint">{label}</div>
      <div
        className={`mt-1 whitespace-nowrap text-ink ${valueClass}`}
        style={{ fontFamily: "var(--font-display)" }}
      >
        {value}
      </div>
      <div className="mt-0.5 min-h-[1rem] text-xs text-inkdim">{sub}</div>
    </div>
  );
}

/** The archive hero (#260): the whole record's numbers above both columns.
 * Everything derives from the roster state the page already maintains --
 * zero fetches of its own, so live detections and lifers tick the numbers
 * in place for free. Three states: dashes while the roster is in flight (a
 * populated archive must never flash the day-one message during hydration),
 * the FRESH-AVIARY hero once loaded-and-empty, the four tiles otherwise.
 * The fresh state is an overlay on the tile grid rendered invisible: the
 * tiles keep defining the band's height at every breakpoint, so the flip to
 * a live scoreboard -- this band's whole payoff on day one -- changes
 * content only and can never shift the page below (house rule #1; the New
 * Arrivals reserved-footprint move at page scale). */
function ArchiveHero({
  roster,
  loaded,
  now,
}: {
  roster: Record<string, RosterEntry>;
  loaded: boolean;
  now: number | null;
}) {
  const stats = archiveStats(Object.values(roster), now);
  const fresh = loaded && stats.species === 0;
  const dash = "—";
  return (
    <section className="relative mb-4">
      <div
        aria-hidden={fresh}
        className={`grid grid-cols-2 gap-4 lg:grid-cols-4 ${fresh ? "invisible" : ""}`}
      >
        <HeroTile
          label="species on record"
          value={loaded ? String(stats.species) : dash}
          sub={
            loaded
              ? `across ${stats.visits === 1 ? "1 visit" : `${stats.visits} visits`}`
              : dash
          }
        />
        <HeroTile
          label="species this week"
          value={loaded ? String(stats.week) : dash}
          sub={loaded ? `of ${stats.species} on record` : dash}
        />
        <HeroTile
          label="species today"
          value={loaded ? String(stats.today) : dash}
          sub={loaded ? `of ${stats.week} this week` : dash}
        />
        <HeroTile
          label="listening since"
          valueClass="text-xl sm:text-3xl xl:text-4xl"
          value={loaded && stats.since !== null ? sinceOf(stats.since) : dash}
          sub={
            loaded && stats.days !== null
              ? stats.days === 1
                ? "1 day on the air"
                : `${stats.days} days on the air`
              : dash
          }
        />
      </div>
      {fresh && (
        <div className="panel absolute inset-0 flex flex-col items-center justify-center gap-1 rounded-sm border border-line bg-panel px-4 text-center">
          <span className="stamp text-[10px] text-inkfaint">
            a brand new aviary
          </span>
          <span
            className="text-2xl text-ink sm:text-3xl"
            style={{ fontFamily: "var(--font-display)" }}
          >
            waiting for the first visitor
          </span>
          <span className="stamp text-[10px] text-inkdim">
            the record starts with the next bird
          </span>
        </div>
      )}
    </section>
  );
}

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
  // New Arrivals' clock (#224): fixed at mount like the midnight boundary,
  // so a card never silently ages out of the window mid-session.
  const [nowTs, setNowTs] = useState<number | null>(null);
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
    setNowTs(Math.floor(Date.now() / 1000));

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
              // A live arrival is inside the trailing week by definition.
              week: cur.week + 1,
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
            week: 1,
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
      <AviaryMasthead
        lamp={{ busUp, status: earlStatus }}
        jumps={[
          { href: "#latest-events", label: "events ↓" },
          { href: "#new-arrivals", label: "arrivals ↓" },
        ]}
      />
      {/* The whole record's numbers, above both columns (#260). */}
      <ArchiveHero roster={roster} loaded={loaded} now={nowTs} />
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
                          {/* The scientific name, the New Arrivals card's
                              vocabulary (#226) -- the tile-sized truncated
                              lead earned nothing. Always present, so the
                              slot is uniformly one line and enrichment
                              landing changes nothing in the grid at all
                              (it only dresses the portrait). The lead still
                              rides the roster for the profile and the
                              archive's solo band. */}
                          <span className="block truncate text-[11px] italic text-inkdim">
                            {sci}
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
          <section
            id="latest-events"
            className="panel scroll-mt-4 rounded-sm border border-line bg-panel"
          >
            <PanelLabel
              title="Latest Events"
              right={<EnhanceToggle player={player} />}
            />
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
                // -m-3/p-3: paint headroom for the #259 glow (#263). The
                // overflow-y clip cuts everything outside the padding box,
                // and box-shadow paints outside the row -- so the box grows
                // 12px past the rows on every side while the rows keep
                // their exact position and width. max-h rises by the same
                // 24px (border-box), so the populated panel's height is
                // unchanged.
                <ul className="scrollpane -m-3 flex max-h-[564px] flex-col gap-1.5 overflow-y-auto p-3">
                  {events.map((e) => (
                    <li
                      key={e.key}
                      className={
                        hydratedKeys.current.has(e.key) ? "" : "event-new"
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
                          <div className="min-w-0 flex-1">
                            {/* The name owns its whole line now (#207): the
                                stamp used to share it and squeezed half the
                                common names into an ellipsis. The time drops
                                into the meta row's front instead — where it
                                reads next to the source it belongs with. */}
                            <Link
                              href={`/aviary/${encodeURIComponent(e.species_sci)}`}
                              className="block truncate text-sm text-ink transition-colors hover:text-squirrel"
                              style={{ fontFamily: "var(--font-display)" }}
                            >
                              {e.species_common}
                            </Link>
                            <div className="stamp flex gap-2 text-[9px] text-inkfaint">
                              <span>{stampOf(e.ts, midnight)}</span>
                              <span>{e.source}</span>
                              <span>{e.confidence.toFixed(2)}</span>
                              {e.wind_suspect && <span>wind?</span>}
                            </div>
                          </div>
                          {/* Play control trails the row (#209): the flex-1
                              text block above pushes it to the right edge, so
                              the eye reads the bird first and the affordance
                              sits in one predictable spot. */}
                          <PlaySlot clip={e.clip} player={player} />
                        </div>
                      ) : (
                        // A notable sound (#174): quieter, species-less, and
                        // bus-only -- it vanishes on reload by design (#182).
                        <div className="flex items-center gap-2.5 rounded-sm border border-line/60 px-2.5 py-1.5 opacity-70">
                          <div className="flex min-w-0 flex-1 items-baseline justify-between gap-2">
                            <span className="stamp truncate text-[10px] lowercase text-inkfaint">
                              {e.class}
                            </span>
                            <span className="shrink-0 text-[10px] text-inkfaint">
                              {stampOf(e.ts, midnight)}
                            </span>
                          </div>
                          {/* Play control trails the row too (#209), so the
                              panel reads consistently across both row types. */}
                          <PlaySlot clip={e.clip} player={player} />
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
              {/* The archive's front door (#211): always rendered -- present
                  in the empty state too, so it can never pop in and shift
                  the panel below. The ticker shows the last 80; the record
                  goes back to the first bird. */}
              <div className="pt-2.5 text-right">
                <Link
                  href="/aviary/events"
                  className="stamp text-[10px] text-inkdim transition-colors hover:text-squirrel"
                >
                  browse the full record →
                </Link>
              </div>
            </div>
          </section>

          {/* The newest species, featured (#224) -- between the ticker that
              mentions them once and the visitors rail that treats them like
              regulars. */}
          <NewArrivals
            roster={roster}
            now={nowTs}
            midnight={midnight}
            player={player}
          />

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
                        className="flex items-baseline gap-2 rounded-sm border border-line bg-panel2 px-2 py-1.5 transition-colors hover:border-linebright"
                      >
                        {/* text-[11px] (down from text-xs) + tighter px so the
                            longer names stop forcing a one-tile row (#207) —
                            most rows now hold two across the 340px rail. */}
                        <span className="text-[11px] text-ink">
                          {t.species_common}
                        </span>
                        <span className="text-[11px] text-inkdim">{t.count}</span>
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

/** The field-naturalist blocks (#186), set as a journal spread (#220): two
 * facing pages, each an entry with its own margin figure, dateline, and
 * signature -- notes in a book, not columns in a panel. Prose written by the
 * analysis pass on pearl and merely read here -- nothing generates at render
 * time, ever. The no-notes empty state keeps the pre-#220 panel footprint
 * exactly, so day one still can't shift the page. */
function FieldNotes({ analysis }: { analysis: Analysis | null }) {
  const has = analysis?.rhythm || analysis?.weather;
  if (!has)
    return (
      <section className="panel mt-4 rounded-sm border border-line bg-panel">
        <div className="flex items-baseline justify-between gap-3 px-4 pb-2 pt-3">
          <h2
            className="text-lg text-ink"
            style={{ fontFamily: "var(--font-display)" }}
          >
            Field Notes
          </h2>
        </div>
        <div className="grid min-h-[120px] gap-5 px-4 pb-4">
          <div className="flex min-h-[120px] items-center justify-center rounded-sm border border-line bg-panel2">
            <span className="stamp px-6 text-center text-xs text-inkfaint">
              no field notes yet — they arrive with the analysis pass
            </span>
          </div>
        </div>
      </section>
    );
  const stats = analysis?.stats ?? null;
  const dateline = analysis?.generated_ts
    ? `${dayOf(analysis.generated_ts)}${analysis.model ? ` · ${analysis.model}` : ""}`
    : null;
  return (
    <section className="mt-4">
      <div className="flex items-baseline justify-between gap-3 pb-2">
        <h2
          className="text-lg text-ink"
          style={{ fontFamily: "var(--font-display)" }}
        >
          Field Notes
        </h2>
      </div>
      <div className="grid items-stretch gap-5 md:grid-cols-2">
        <JournalPage
          numeral="I"
          title="the rhythm"
          text={analysis?.rhythm ?? null}
          stamp={
            stats?.total_visits != null
              ? `${stats.total_visits} visits`
              : null
          }
          figure={<RhythmStrip stats={stats} />}
          dateline={dateline}
        />
        <JournalPage
          numeral="II"
          title="weather & timing"
          text={analysis?.weather ?? null}
          stamp={
            stats?.weather?.visits_matched != null
              ? `${stats.weather.visits_matched} matched`
              : null
          }
          figure={<WeatherChipsRow stats={stats} />}
          dateline={dateline}
        />
      </div>
    </section>
  );
}

/** One page of the journal: entry heading over a ruled line, the margin
 * figure, the prose (drop cap on the opening letter -- the naturalist's
 * flourish), and a dateline footer signed by the house voice. Fixed
 * skeleton in every state; only the words change. */
function JournalPage({
  numeral,
  title,
  text,
  stamp,
  figure,
  dateline,
}: {
  numeral: string;
  title: string;
  text: string | null;
  stamp: string | null;
  figure: ReactNode;
  dateline: string | null;
}) {
  return (
    <article className="panel flex min-w-0 flex-col rounded-sm border border-line bg-panel px-5 pb-3 pt-4">
      <header className="flex items-baseline justify-between gap-3 border-b border-line pb-2">
        <h3
          className="text-base text-ink"
          style={{ fontFamily: "var(--font-display)" }}
        >
          Entry {numeral} — {title}
        </h3>
        {stamp && (
          <span className="stamp shrink-0 text-[9px] text-inkfaint">
            {stamp}
          </span>
        )}
      </header>
      <div className="mt-3">{figure}</div>
      {text ? (
        <div className="mt-3 flex-1 space-y-2.5 text-sm leading-relaxed text-inkdim">
          {text.split(/\n+/).map((para, i) => (
            <p
              key={i}
              className={
                i === 0
                  ? "first-letter:float-left first-letter:mr-1.5 first-letter:text-[34px] first-letter:leading-[0.8] first-letter:text-ink first-letter:[font-family:var(--font-display)]"
                  : undefined
              }
            >
              {para}
            </p>
          ))}
        </div>
      ) : (
        <p className="stamp mt-3 flex-1 text-[9px] text-inkfaint">
          not written yet
        </p>
      )}
      <footer className="mt-4 flex items-baseline justify-between gap-3 border-t border-line pt-2">
        <span className="stamp text-[9px] text-inkfaint">
          {dateline ?? "—"}
        </span>
        <span
          className="text-xs italic text-inkfaint"
          style={{ fontFamily: "var(--font-display)" }}
        >
          — Earl
        </span>
      </footer>
    </article>
  );
}

/** The rhythm page's margin figure (#220): the stored 24-hour histogram as
 * a strip, peak-window hours lit in --wing. STATS_JSON VERBATIM -- the same
 * numbers the prose beside it was written from, server-local hours and all
 * (the deliberate split: the chart above speaks viewer-local, the figures
 * speak the prose's clock). Reserved height in every state. */
function RhythmStrip({ stats }: { stats: AnalysisStats | null }) {
  const cells = rhythmStrip(stats);
  return (
    <div className="h-[52px]">
      {cells ? (
        <>
          <div className="flex h-8 items-end gap-px">
            {cells.map((c, h) => (
              <div
                key={h}
                className="min-w-0 flex-1 rounded-[1px]"
                style={{
                  height: `${Math.max(6, Math.round(c.frac * 100))}%`,
                  background: "var(--wing)",
                  opacity: c.peak ? 1 : 0.3,
                }}
              />
            ))}
          </div>
          <div className="stamp mt-1 flex justify-between text-[8px] text-inkfaint">
            <span>12a</span>
            <span>6a</span>
            <span>noon</span>
            <span>6p</span>
            <span>12a</span>
          </div>
        </>
      ) : (
        <div className="flex h-full items-center justify-center rounded-sm border border-line bg-panel2">
          <span className="stamp text-[9px] text-inkfaint">
            no figures stored yet
          </span>
        </div>
      )}
    </div>
  );
}

/** The weather page's margin figure: the pass's exposure-normalised effects
 * as full-width rows, strongest first (#257 -- was a cluster of 9px chips,
 * too small to read; the findings are the figure, so they earn real size).
 * A thin finding keeps its hedge in pixels -- dashed, dimmed, tagged -- the
 * show-with-hedging rule made visible. Each row spans the column so the label
 * can never wrap (nowrap + truncate guard); the percentage holds a fixed
 * gutter so the labels align down the left like a ledger. Only rendered at
 * all when the pass judged the sample sufficient; a confident row over hedged
 * prose would be the figure contradicting the writing. */
function WeatherChipsRow({ stats }: { stats: AnalysisStats | null }) {
  const chips = weatherChips(stats);
  return (
    <div className="flex min-h-[52px] flex-col gap-1.5">
      {chips.length > 0 ? (
        chips.map((c) => (
          <span
            key={c.label}
            className={`stamp flex w-full items-baseline gap-3 whitespace-nowrap rounded-sm border px-3 py-2 text-base ${
              c.thin
                ? "border-dashed border-line text-inkdim"
                : "border-line text-ink"
            }`}
          >
            <span
              className={`w-14 shrink-0 tabular-nums ${
                c.thin ? undefined : "text-wing"
              }`}
            >
              {c.pct > 0 ? `+${c.pct}%` : `−${Math.abs(c.pct)}%`}
            </span>
            <span className="truncate">
              {c.label}
              {c.thin ? " · thin" : ""}
            </span>
          </span>
        ))
      ) : (
        <div className="flex min-h-[52px] w-full items-center justify-center rounded-sm border border-line bg-panel2">
          <span className="stamp px-4 text-center text-xs text-inkfaint">
            no confident weather figures yet
          </span>
        </div>
      )}
    </div>
  );
}

/** The standings band (#220): where this bird sits in the yard, computed at
 * load from payloads the page already holds and NEVER reshuffled by live
 * events (house rule #1 -- a scoreboard jumping under the reader is the
 * worst version of a reshuffle). Week and all-time ranks from the roster;
 * records from the full visit record; every tile reserves its geometry
 * while data is in flight. */
function StandingsBand({
  roster,
  sci,
  openings,
  now,
}: {
  roster: RosterEntry[] | null;
  sci: string;
  openings: number[] | null;
  now: number | null;
}) {
  const week = roster ? standingFor(roster, sci, (e) => e.week) : null;
  const allTime = roster ? standingFor(roster, sci, (e) => e.visits) : null;
  const yardTotal = roster?.reduce((sum, e) => sum + e.visits, 0) ?? 0;
  const share = allTime ? shareOfYard(yardTotal, allTime.count) : null;
  const lifer = roster ? liferNumber(roster, sci) : null;
  const records =
    openings !== null && now !== null ? yardRecords(openings, now) : null;
  const dash = "—";
  return (
    <section className="mt-4 grid gap-4 md:grid-cols-3">
      <StandingTile
        label="standings · this week"
        value={week && week.count > 0 ? `No. ${week.rank}` : dash}
        sub={week ? rivalLine(week, "no visits this week") : dash}
      />
      <StandingTile
        label="all time"
        value={allTime && allTime.count > 0 ? `No. ${allTime.rank}` : dash}
        sub={allTime ? (share ?? rivalLine(allTime, "no visits yet")) : dash}
      />
      <div className="panel rounded-sm border border-line bg-panel p-4">
        <div className="stamp text-[10px] text-inkfaint">yard records</div>
        <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
          <RecordRow
            label="busiest day"
            value={
              records?.busiestDay
                ? `${records.busiestDay.count} ${records.busiestDay.count === 1 ? "visit" : "visits"} · ${dayOf(records.busiestDay.day)}`
                : dash
            }
          />
          <RecordRow
            label="streak"
            value={
              records
                ? records.streak > 0
                  ? `${records.streak} ${records.streak === 1 ? "day" : "days"}`
                  : "over"
                : dash
            }
          />
          <RecordRow
            label="longest silence"
            value={
              records
                ? records.longestSilenceDays > 0
                  ? `${records.longestSilenceDays} ${records.longestSilenceDays === 1 ? "day" : "days"}`
                  : "under a day"
                : dash
            }
          />
          <RecordRow
            label="earliest heard"
            value={records?.earliest != null ? clockOf(records.earliest) : dash}
          />
          <RecordRow
            label="latest heard"
            value={records?.latest != null ? clockOf(records.latest) : dash}
          />
          <RecordRow
            label="lifer"
            value={lifer ? `No. ${lifer.n} of ${lifer.of}` : dash}
          />
        </dl>
      </div>
    </section>
  );
}

function StandingTile({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub: string;
}) {
  return (
    <div className="panel rounded-sm border border-line bg-panel p-4">
      <div className="stamp text-[10px] text-inkfaint">{label}</div>
      <div
        className="mt-1 text-3xl text-ink"
        style={{ fontFamily: "var(--font-display)" }}
      >
        {value}
      </div>
      <div className="mt-0.5 min-h-[1rem] text-xs text-inkdim">{sub}</div>
    </div>
  );
}

function RecordRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="stamp text-[9px] text-inkfaint">{label}</dt>
      <dd className="truncate text-inkdim">{value}</dd>
    </div>
  );
}

type Analysis = {
  rhythm: string | null;
  weather: string | null;
  stats: AnalysisStats | null;
  model: string | null;
  generated_ts: number | null;
};

export function SpeciesProfile({ sci }: { sci: string }) {
  const [entry, setEntry] = useState<RosterEntry | null>(null);
  // The whole roster rides along (#220): the standings band ranks this bird
  // against every other, and the payload is already on the wire for `entry`.
  const [roster, setRoster] = useState<RosterEntry[] | null>(null);
  // Every visit opening on record, for the yard-records panel -- fetched
  // once entry lands (the range needs first_ts), computed client-local.
  const [openings, setOpenings] = useState<number[] | null>(null);
  const [visits, setVisits] = useState<Visit[] | null>(null);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [midnight, setMidnight] = useState<number | null>(null);
  const [now, setNow] = useState<number | null>(null);
  // The description is clamped at rest (#196). Only long leads earn the
  // toggle -- a two-sentence stub with a "read more" under it would be a
  // control that does nothing visible.
  const [bioOpen, setBioOpen] = useState(false);
  // Species removal (#216): the confirm overlay's lifecycle. `removing`
  // freezes the dialog while the DELETE is in flight (no second click, no
  // Escape-mid-delete); a failure reports in the dialog's reserved line and
  // leaves everything standing.
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [removeFailed, setRemoveFailed] = useState(false);
  const router = useRouter();
  const player = useClipPlayer();

  // Escape cancels the confirm -- the overlays' idiom -- but never an
  // in-flight delete, which has no honest way to be called back.
  useEffect(() => {
    if (!confirmOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !removing) setConfirmOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirmOpen, removing]);

  const removeSpecies = () => {
    setRemoving(true);
    setRemoveFailed(false);
    fetch(`/aviary/species/${encodeURIComponent(sci)}`, { method: "DELETE" })
      .then((r) => {
        if (!r.ok) throw new Error(String(r.status));
        // The roster, archive, and ticker hydration all read the tables the
        // DELETE just emptied -- landing on /aviary refetches them and the
        // species is simply gone. Ticker rows already in other tabs' memory
        // linger until their reload (accepted, #216).
        router.push("/aviary");
      })
      .catch(() => {
        setRemoving(false);
        setRemoveFailed(true);
      });
  };

  useEffect(() => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    const mid = Math.floor(d.getTime() / 1000);
    setMidnight(mid);
    const nowTs = Math.floor(Date.now() / 1000);
    setNow(nowTs);
    // The roster carries this species' totals, first-heard, and today count
    // (the same grouped counts the grid shows -- one counting rule
    // everywhere); the per-species recent cut becomes the visits list.
    fetch(`/aviary/roster?today=${mid}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : { species: [] }))
      .then((body: { species?: RosterEntry[] }) => {
        const list = Array.isArray(body.species) ? body.species : [];
        setRoster(list);
        const found = list.find((e) => e.species_sci === sci);
        setEntry(found ?? null);
        // The full visit record for the yard-records panel (#220): the
        // chart's own route, asked once from first-heard to now. The route
        // clamps spans past 400 days at the `to` end -- when the record
        // outgrows that, these become records of the recent era and the
        // clamp constant is the thing to revisit, not this call.
        if (found) {
          fetch(
            `/aviary/visits/${encodeURIComponent(sci)}?from=${found.first_ts - 1}&to=${nowTs + 3600}`,
            { cache: "no-store" },
          )
            .then((r) => (r.ok ? r.json() : { visits: [] }))
            .then((b: { visits?: number[] }) =>
              setOpenings(Array.isArray(b.visits) ? b.visits : []),
            )
            .catch(() => setOpenings([]));
        }
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
    setOpenings(null); // a different bird's records are not this bird's
  }, [sci]);

  // How much prose to show at rest is set by the PHOTO, not by a constant
  // (#196 follow-up): the clamp fills ~75% of the portrait's height, so a
  // tall bird earns more text and a wide one less, and neither leaves the
  // gutter beside the photo conspicuously empty. Computed from the stored
  // dimensions rather than measured -- the figure is `md:w-[300px]`, and
  // text-sm/leading-relaxed is 22.75px a line -- so it is known before
  // first paint and nothing reflows. Bounded at both ends: a panoramic
  // photo still shows a readable few lines, and a very tall one does not
  // dump the whole encyclopedia. Unknown dimensions keep the original six.
  const clampLines = (() => {
    const w = entry?.image_w, h = entry?.image_h;
    if (!w || !h) return 6;
    return Math.max(5, Math.min(18, Math.round((300 * (h / w) * 0.75) / 22.75)));
  })();
  // Whether the toggle is needed at all, measured before paint (a control
  // appearing a frame late is the layout-shift rule broken in miniature).
  // Only measured while CLOSED: open, scrollHeight equals clientHeight, and
  // re-measuring there would delete the "read less" control mid-read.
  const bodyRef = useRef<HTMLDivElement>(null);
  const [longBio, setLongBio] = useState(false);
  useLayoutEffect(() => {
    if (bioOpen) return;
    const el = bodyRef.current;
    if (el) setLongBio(el.scrollHeight > el.clientHeight + 1);
  }, [entry?.description, clampLines, bioOpen]);

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
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <h2
                  className="text-2xl text-ink"
                  style={{ fontFamily: "var(--font-display)" }}
                >
                  {entry?.species_common ?? "…"}
                </h2>
                <p className="text-sm italic text-inkdim">{sci}</p>
              </div>
              {/* Species removal (#216): the misidentification eraser, worn
                  quietly -- the sort control's chrome in the faint ink, and
                  the destructive weight carried by the confirm step rather
                  than by color (the palette's only red means "chipmunk",
                  never "danger"). Always rendered, merely disabled until
                  the entry lands (the now-button idiom): a control that
                  appeared with the fetch would nudge the header. */}
              <button
                type="button"
                disabled={!entry}
                onClick={() => {
                  setRemoveFailed(false);
                  setConfirmOpen(true);
                }}
                className="stamp mt-1 shrink-0 rounded-sm border border-line px-2 py-1 text-[10px] text-inkfaint transition-colors hover:border-linebright hover:text-ink disabled:cursor-default disabled:opacity-40 disabled:hover:border-line disabled:hover:text-inkfaint"
              >
                remove species
              </button>
            </div>
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
              {/* First contact (#220): the very first recording Earl ever
                  made of this bird -- the one clip retention exempts forever
                  (#175), which until now nothing played. PlaySlot's own
                  geometry covers every state: a pre-clip-era lifer shows the
                  reserved no-clip dot, a hand-pruned file the faded stamp. */}
              <div>
                <dt className="stamp text-[9px] text-inkfaint">
                  first contact
                </dt>
                <dd className="mt-0.5">
                  {entry ? (
                    <PlaySlot clip={entry.first_clip} player={player} />
                  ) : (
                    <span className="text-inkdim">—</span>
                  )}
                </dd>
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
                    ref={bodyRef}
                    className="space-y-2.5 text-sm leading-relaxed text-inkdim"
                    // Inline rather than a line-clamp-N class: the count is
                    // per-species, and Tailwind can only emit classes it can
                    // see in the source.
                    style={
                      bioOpen
                        ? undefined
                        : {
                            display: "-webkit-box",
                            WebkitBoxOrient: "vertical",
                            WebkitLineClamp: clampLines,
                            overflow: "hidden",
                          }
                    }
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
            <PanelLabel
              title="Recent Visits"
              right={<EnhanceToggle player={player} />}
            />
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
      {/* Full-width under both columns, the floor #192's layout cleared.
          Order per #222: the standings band leads (the at-a-glance card),
          then the chart it summarizes, then the journal spread. Only for a
          bird actually in the record -- an unknown species has no rhythm to
          draw and nothing to say. */}
      {entry && (
        <>
          <StandingsBand roster={roster} sci={sci} openings={openings} now={now} />
          <VisitsChart sci={sci} />
          <FieldNotes analysis={analysis} />
        </>
      )}
      {/* The confirm (#216): an overlay, never in-place expansion (house
          rule #1 -- nothing on the page may move to make room for a
          question). Mounted OUTSIDE every `.panel` deliberately: the panel
          reveal animates a transform, and a transformed ancestor becomes a
          fixed element's containing block -- inside the hero this dialog
          would center on the panel, not the viewport. Backdrop click and
          Escape cancel; nothing is deleted until the explicit confirm. */}
      {confirmOpen && entry && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={`Remove the ${entry.species_common} from the record`}
          className="fixed inset-0 z-50 flex items-center justify-center bg-bg/70 p-4"
          onClick={() => !removing && setConfirmOpen(false)}
        >
          <div
            className="w-full max-w-md rounded-sm border border-linebright bg-panel p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <p className="stamp text-[9px] text-inkfaint">
              striking from the record
            </p>
            <h3
              className="mt-1 text-xl text-ink"
              style={{ fontFamily: "var(--font-display)" }}
            >
              Remove the {entry.species_common}?
            </h3>
            <p className="mt-3 text-sm leading-relaxed text-inkdim">
              This deletes the whole record for{" "}
              <span className="italic">{sci}</span> — all{" "}
              {entry.visits === 1 ? "1 recorded visit" : `${entry.visits} recorded visits`},
              every clip including first contact, and the portrait. On the
              books since {dateOf(entry.first_ts)}. If Earl ever hears it
              again, it starts over as a brand-new lifer.
            </p>
            {/* The failure line reserves its height empty -- an error
                appearing must not bump the buttons (house rule #1). */}
            <p className="stamp mt-3 min-h-[14px] text-[10px] text-inkdim">
              {removeFailed
                ? "the record refused the delete — nothing was removed"
                : ""}
            </p>
            <div className="mt-3 flex justify-end gap-2">
              <button
                type="button"
                autoFocus
                disabled={removing}
                onClick={() => setConfirmOpen(false)}
                className="stamp rounded-sm border border-line px-3 py-1.5 text-[10px] text-inkdim transition-colors hover:border-linebright hover:text-ink disabled:opacity-40"
              >
                keep it
              </button>
              {/* min-w so "removing …" wears the same footprint as
                  "remove it" -- the label change resizes nothing. */}
              <button
                type="button"
                disabled={removing}
                onClick={removeSpecies}
                className="stamp min-w-[96px] rounded-sm border border-linebright bg-panel2 px-3 py-1.5 text-center text-[10px] text-ink transition-colors hover:bg-panel disabled:opacity-40"
              >
                {removing ? "removing …" : "remove it"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// --- The /aviary/events page (issue #211) ------------------------------------

/** The Full Record: the archive the ticker isn't. The ticker shows the last
 * 80 moments and forgets; this page walks the whole store -- newest first
 * under sticky local-day headers, filtered by species pills (combinable),
 * repositioned by a jump-to-date, paged backward by an IntersectionObserver
 * as the viewer scrolls. Filters live in the query string (replaceState, read
 * once on mount), so a filtered view is a shareable URL -- the /weather page
 * reasoning for being a page at all.
 *
 * Deliberately NO bus client here (v1): this is an archive browser, not a
 * second ticker; new arrivals appear on reload. And only detections ever
 * appear -- sound events are bus-only by design (#182), so the archive can't
 * hold what the store never kept.
 *
 * Pagination is the hydration/live merge trick turned sideways: the cursor
 * is INCLUSIVE (`ts <= before`, the client's own oldest row re-requested)
 * and audioEventKey dedupes the overlap, so a same-second sighting straddling
 * a page boundary is a no-op instead of a dropped bird. A short page is the
 * record's true end for the active filter -- the WHERE runs before the
 * LIMIT -- so `exhausted` needs no second opinion. */
export function AviaryEvents() {
  const [roster, setRoster] = useState<Record<string, RosterEntry>>({});
  // Pill order: most-visited first, computed once at load (rosterOrder --
  // the grid's sort logic, reused). Static thereafter: a filter bar that
  // reshuffles as you use it is house rule #1 broken in a control.
  const [pillOrder, setPillOrder] = useState<string[]>([]);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  // The date input's own value ("" = live); dayAnchor turns it into a cursor.
  const [anchorDay, setAnchorDay] = useState("");
  const [rows, setRows] = useState<ArchiveRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [exhausted, setExhausted] = useState(false);
  const [midnight, setMidnight] = useState<number | null>(null);
  // Gate: the first page waits for the URL parse, so a shared link fetches
  // its filtered view once rather than the unfiltered view and then again.
  const [ready, setReady] = useState(false);
  const player = useClipPlayer();
  // Generation guards every async landing: a filter click mid-fetch makes
  // the in-flight page stale, and stale pages must not splice into the new
  // list. Refs beside it mirror state the observer callback needs without
  // re-subscribing per render.
  const genRef = useRef(0);
  const beforeRef = useRef<number | null>(null);
  const queryRef = useRef("");
  const busyRef = useRef(false);
  const exhaustedRef = useRef(false);
  const keysRef = useRef<Set<string>>(new Set());
  // Keys that arrived LIVE over the bus (not loaded from an archive page):
  // only these glow. The inverse of the ticker's hydratedKeys -- here the
  // default is "from the store, no glow", so we mark the new ones instead.
  const liveKeysRef = useRef<Set<string>>(new Set());
  // The live bus handler is subscribed once, so it reads the current filter
  // through refs rather than closing over stale state (the loadPage idiom).
  const selectedRef = useRef<ReadonlySet<string>>(selected);
  const anchorDayRef = useRef(anchorDay);
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);
  useEffect(() => {
    anchorDayRef.current = anchorDay;
  }, [anchorDay]);

  useEffect(() => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    const mid = Math.floor(d.getTime() / 1000);
    setMidnight(mid);

    fetch(`/aviary/roster?today=${mid}`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : { species: [] }))
      .then((body: { species?: RosterEntry[] }) => {
        const entries = Array.isArray(body.species) ? body.species : [];
        setRoster(Object.fromEntries(entries.map((e) => [e.species_sci, e])));
        setPillOrder(rosterOrder(entries, "visits", "desc"));
      })
      .catch(() => {});

    // The shared-link leg: filters arrive in the query string exactly as
    // the writeback below leaves them.
    const qs = new URLSearchParams(window.location.search);
    const sel = parseSpeciesFilter(qs.get("species"));
    if (sel.length > 0) setSelected(new Set(sel));
    const day = qs.get("day") ?? "";
    if (day !== "" && dayAnchor(day) !== null) setAnchorDay(day);
    setReady(true);
  }, []);

  /** One page from the archive. Stable on purpose (everything varying rides
   * refs), so the observer effect subscribes once. */
  const loadPage = useCallback(async (gen: number, append: boolean) => {
    busyRef.current = true;
    setLoading(true);
    const before = beforeRef.current;
    const url =
      `/aviary/recent?${queryRef.current}` +
      (before !== null ? `&before=${before}` : "");
    try {
      const r = await fetch(url, { cache: "no-store" });
      const body: { events?: unknown[] } = r.ok
        ? await r.json()
        : { events: [] };
      if (gen !== genRef.current) return; // superseded by a filter change
      const raw = (Array.isArray(body.events) ? body.events : [])
        .map(audioEventFrom)
        .filter((e): e is BirdEvent => e?.kind === "detection");
      const fresh: ArchiveRow[] = [];
      for (const e of raw) {
        const key = audioEventKey(e);
        if (keysRef.current.has(key)) continue; // the cursor's overlap row
        keysRef.current.add(key);
        fresh.push({ ...e, key });
      }
      if (raw.length < ARCHIVE_PAGE) {
        exhaustedRef.current = true;
        setExhausted(true);
      } else {
        beforeRef.current = nextBefore(raw[raw.length - 1].ts, before);
      }
      setRows((prev) => (append ? [...prev, ...fresh] : fresh));
    } catch {
      // A failed page ends the scroll politely rather than hammering a dead
      // route from the observer; a reload starts fresh.
      if (gen === genRef.current) {
        exhaustedRef.current = true;
        setExhausted(true);
      }
    } finally {
      if (gen === genRef.current) {
        busyRef.current = false;
        setLoading(false);
      }
    }
  }, []);

  // Filter changes (and the gated first load): reset and fetch page one.
  useEffect(() => {
    if (!ready) return;
    const gen = ++genRef.current;
    keysRef.current = new Set();
    // A refetch re-reads live rows from the store as ordinary history; they
    // must not glow a second time, so the live set resets with the page.
    liveKeysRef.current = new Set();
    exhaustedRef.current = false;
    beforeRef.current = anchorDay === "" ? null : dayAnchor(anchorDay);
    const qp = new URLSearchParams();
    qp.set("limit", String(ARCHIVE_PAGE));
    if (selected.size > 0) qp.set("species", [...selected].join(","));
    queryRef.current = qp.toString();
    setExhausted(false);
    setRows([]);
    void loadPage(gen, false);

    // Writeback: the URL always says what the view shows.
    const share = new URLSearchParams();
    if (selected.size > 0) share.set("species", [...selected].join(","));
    if (anchorDay !== "") share.set("day", anchorDay);
    const search = share.toString();
    window.history.replaceState(
      null,
      "",
      search ? `?${search}` : window.location.pathname,
    );
  }, [ready, selected, anchorDay, loadPage]);

  // Live arrivals (issue #259): the archive was fetch-only, so a new event
  // never showed until reload. Subscribe like the ticker and prepend -- but
  // ONLY on the live view (no pinned day) and only if the event passes the
  // active species filter, both read through refs so this subscribes once. A
  // past-day or filtered-out event must never splice in. The prepended row is
  // marked live so it (and only it) glows; dayGroups re-buckets it to the top
  // of today. The same ±1 race the ticker accepts applies here (a bus event
  // landing during a filter reset), corrected by a reload.
  useEffect(() => {
    const url = busUrl(
      window.location.hostname,
      process.env.NEXT_PUBLIC_MERLE_MQTT_WS,
    );
    const client = mqtt.connect(url, { reconnectPeriod: 3000 });
    client.on("connect", () => client.subscribe(AUDIO_EVENTS_TOPIC));
    client.on("error", (err) =>
      console.debug("[bus] error", err?.message ?? err),
    );
    client.on("message", (_topic, payload) => {
      const event = parseAudioEvent(payload.toString());
      if (!event || event.kind !== "detection") return;
      if (anchorDayRef.current !== "") return; // a pinned past day stays put
      const sel = selectedRef.current;
      if (sel.size > 0 && !sel.has(event.species_sci)) return;
      const key = audioEventKey(event);
      if (keysRef.current.has(key)) return;
      keysRef.current.add(key);
      liveKeysRef.current.add(key);
      setRows((prev) => [{ ...event, key }, ...prev]);
    });
    return () => {
      client.end(true);
    };
  }, []);

  // The scroll's engine: reaching within 600px of the sentinel asks for the
  // next page. Refs carry the guards; the boolean dep re-subscribes when the
  // sentinel itself mounts or unmounts (it only exists alongside rows).
  const hasRows = rows.length > 0;
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      (entries) => {
        if (!entries.some((en) => en.isIntersecting)) return;
        if (busyRef.current || exhaustedRef.current) return;
        if (genRef.current === 0) return; // first page not kicked off yet
        void loadPage(genRef.current, true);
      },
      { rootMargin: "600px 0px" },
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [loadPage, hasRows]);

  const togglePill = (sci: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(sci)) next.delete(sci);
      else next.add(sci);
      return next;
    });

  const pillClass = (on: boolean) =>
    `stamp flex items-baseline gap-1.5 rounded-sm border px-2 py-1 text-[10px] transition-colors ${
      on
        ? "border-linebright bg-panel2 text-squirrel"
        : "border-line text-inkdim hover:border-linebright hover:text-ink"
    }`;

  const soloSci = selected.size === 1 ? [...selected][0] : null;
  const solo = soloSci !== null ? roster[soloSci] : undefined;
  const groups = dayGroups(rows);

  return (
    <div className="mx-auto w-full max-w-[1500px] px-4 py-6">
      <AviaryMasthead back={{ href: "/aviary", label: "← the aviary" }} />
      <section className="panel rounded-sm border border-line bg-panel">
        <PanelLabel
          title="The Full Record"
          right={<EnhanceToggle player={player} />}
        />

        {/* The controls: species pills (combinable), then the date leg.
            Wrapping flex -- on a phone the date group drops to its own
            line rather than crushing the pills. */}
        <div className="flex flex-wrap items-start gap-x-6 gap-y-2 px-4 pb-3">
          <div className="flex min-w-0 flex-1 flex-wrap gap-1.5">
            <button
              type="button"
              onClick={() => setSelected(new Set())}
              aria-pressed={selected.size === 0}
              className={pillClass(selected.size === 0)}
            >
              all birds
            </button>
            {pillOrder.map((sci) => {
              const e = roster[sci];
              if (!e) return null;
              return (
                <button
                  key={sci}
                  type="button"
                  onClick={() => togglePill(sci)}
                  aria-pressed={selected.has(sci)}
                  className={pillClass(selected.has(sci))}
                >
                  <span>{e.species_common}</span>
                  <span className="text-inkfaint">{e.visits}</span>
                </button>
              );
            })}
          </div>
          <div className="flex shrink-0 items-center gap-1.5">
            <label className="flex items-center gap-1.5">
              <span className="stamp text-[9px] text-inkfaint">jump to</span>
              <input
                type="date"
                value={anchorDay}
                max={midnight === null ? undefined : dateInputOf(midnight)}
                onChange={(ev) => setAnchorDay(ev.target.value)}
                className="stamp rounded-sm border border-line bg-panel2 px-2 py-1 text-[10px] text-inkdim [color-scheme:dark] focus:border-linebright focus:outline-none"
              />
            </label>
            {/* Always rendered, merely disabled while live -- the chart's
                now-control idiom; a button popping in on jump would shove
                the date input sideways (house rule #1). */}
            <button
              type="button"
              onClick={() => setAnchorDay("")}
              disabled={anchorDay === ""}
              title={
                anchorDay === ""
                  ? "already showing the latest"
                  : "back to the latest"
              }
              className="stamp rounded-sm border border-line px-2 py-1 text-[10px] text-inkdim transition-colors enabled:hover:border-squirrel enabled:hover:text-squirrel disabled:opacity-40"
            >
              now
            </button>
          </div>
        </div>

        {/* The solo band (#211): one selected bird earns its context --
            totals, first-heard, and the enrichment lead -- because the
            filtered page IS that bird's story and there is room to say so.
            Reserved while the roster is still landing, so a shared
            single-species link can't pop the band in under the reader. */}
        {soloSci !== null && (
          <div className="mx-4 mb-3 rounded-sm border border-line bg-panel2 p-3">
            {solo ? (
              <div className="flex gap-3">
                <Portrait
                  sci={soloSci}
                  has={Boolean(solo.image_file)}
                  alt={solo.species_common}
                  glyphClass="h-8 w-8"
                  w={solo.image_w}
                  h={solo.image_h}
                  boxAspect={4 / 3}
                  style={{
                    aspectRatio: portraitAspect(solo.image_w, solo.image_h),
                  }}
                  className="w-24 shrink-0 self-start rounded-sm border border-line bg-panel"
                />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
                    <span
                      className="text-lg text-ink"
                      style={{ fontFamily: "var(--font-display)" }}
                    >
                      {solo.species_common}
                    </span>
                    <span className="text-xs italic text-inkdim">
                      {soloSci}
                    </span>
                    <Link
                      href={`/aviary/${encodeURIComponent(soloSci)}`}
                      className="stamp text-[10px] text-inkdim transition-colors hover:text-squirrel"
                    >
                      full profile →
                    </Link>
                  </div>
                  <dl className="mt-1 flex flex-wrap gap-x-5 gap-y-1 text-xs">
                    <div>
                      <dt className="stamp text-[9px] text-inkfaint">visits</dt>
                      <dd className="text-inkdim">{solo.visits}</dd>
                    </div>
                    <div>
                      <dt className="stamp text-[9px] text-inkfaint">today</dt>
                      <dd className="text-inkdim">{solo.today}</dd>
                    </div>
                    <div>
                      <dt className="stamp text-[9px] text-inkfaint">
                        first heard
                      </dt>
                      <dd className="text-inkdim">{dayOf(solo.first_ts)}</dd>
                    </div>
                  </dl>
                  {solo.description && (
                    <p className="mt-1.5 line-clamp-2 text-[11px] leading-[1.4] text-inkdim">
                      {solo.description}
                    </p>
                  )}
                </div>
              </div>
            ) : (
              <div className="flex min-h-[96px] items-center justify-center">
                <span className="stamp text-xs text-inkfaint">
                  opening the record …
                </span>
              </div>
            )}
          </div>
        )}

        <div className="px-4 pb-4">
          {rows.length === 0 ? (
            <div className="flex min-h-[200px] items-center justify-center rounded-sm border border-line bg-panel2">
              <span className="stamp px-4 text-center text-xs text-inkfaint">
                {loading
                  ? "opening the record …"
                  : selected.size > 0
                    ? "no events on record for this selection"
                    : "no events on record yet — earl is listening"}
              </span>
            </div>
          ) : (
            <>
              {groups.map((g) => (
                <section key={g.day}>
                  {/* Sticky day headers are what fix "which day am I in":
                      the date is structural, not a per-row stamp. Opaque
                      panel ground so rows slide under, never through. */}
                  <div className="sticky top-0 z-10 flex items-baseline gap-2 bg-panel py-1.5">
                    <span className="stamp text-[10px] text-inkdim">
                      {dayLabelOf(g.day)}
                    </span>
                    {midnight !== null && g.day === midnight && (
                      <span className="stamp text-[9px] text-led">today</span>
                    )}
                    {midnight !== null &&
                      g.day === dayStart(midnight - 3600) && (
                        <span className="stamp text-[9px] text-inkfaint">
                          yesterday
                        </span>
                      )}
                    <span className="stamp ml-auto text-[9px] text-inkfaint">
                      {g.rows.length === 1
                        ? "1 event"
                        : `${g.rows.length} events`}
                    </span>
                  </div>
                  <ul className="grid gap-2 pb-3 lg:grid-cols-2 2xl:grid-cols-3">
                    {g.rows.map((e) => (
                      <li
                        key={e.key}
                        className={`flex items-center gap-3 rounded-sm border border-line bg-panel2 px-3 py-2.5${
                          liveKeysRef.current.has(e.key) ? " event-new" : ""
                        }`}
                      >
                        <TickerThumb
                          sci={e.species_sci}
                          has={Boolean(roster[e.species_sci]?.image_file)}
                          w={roster[e.species_sci]?.image_w}
                          h={roster[e.species_sci]?.image_h}
                          box="h-16 w-16"
                          glyph="h-7 w-7"
                        />
                        <div className="min-w-0 flex-1">
                          <Link
                            href={`/aviary/${encodeURIComponent(e.species_sci)}`}
                            className="block truncate text-base text-ink transition-colors hover:text-squirrel"
                            style={{ fontFamily: "var(--font-display)" }}
                          >
                            {e.species_common}
                          </Link>
                          <div className="text-xs text-inkdim">
                            {timeOf(e.ts)}
                          </div>
                          <div className="stamp flex gap-2 text-[9px] text-inkfaint">
                            <span>{e.source}</span>
                            <span>{e.confidence.toFixed(2)}</span>
                            {e.wind_suspect && <span>wind?</span>}
                          </div>
                        </div>
                        <PlaySlot clip={e.clip} player={player} />
                      </li>
                    ))}
                  </ul>
                </section>
              ))}
              <div ref={sentinelRef} className="h-px" />
              {/* Fixed-height tail: the endcap and the fetching stamp share
                  one reserved row, so neither arriving can shift the list. */}
              <div className="flex h-9 items-center justify-center">
                <span className="stamp text-[10px] text-inkfaint">
                  {exhausted
                    ? "the record begins here"
                    : loading
                      ? "opening older days …"
                      : ""}
                </span>
              </div>
            </>
          )}
        </div>
      </section>
    </div>
  );
}
