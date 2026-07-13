// Client for the Merle event bus (Mosquitto over WebSockets). Unlike daemon
// HTTP traffic -- which rides the /daemon/* proxy route -- the browser connects
// to the broker DIRECTLY: the HTTP proxy can't carry WebSockets. The broker
// lives on pearl (192.168.1.64, config at /etc/mosquitto/conf.d/squirrel.conf)
// and listens on ws://192.168.1.64:9001 -- NEXT_PUBLIC_MERLE_MQTT_WS in
// mcc/.env.local points there, and a phone on the LAN reaches it directly.

export type NarrationLine = {
  ts: string;
  narrator: string;
  voice: string;
  text: string;
  event_kind: string;
};

export const NARRATION_TOPIC = "narration/lines";
// Per-narrator retained journal windows (issue #80): each narrator republishes
// only its own window to narration/journal/<mqtt_id>, and the dashboard merges
// them -- a single shared retained topic meant each narrator's republish
// clobbered the other's window.
export const NARRATION_JOURNAL_WILDCARD = "narration/journal/+";
export const NARRATOR_STATUS_WILDCARD = "narrators/+/status";

// A journal entry with a stable client-side key. Keys are derived from the
// line's CONTENT (not a mint-on-arrival counter): the retained journal window
// is republished whole on every new line, and content-derived keys let React
// keep the existing <li> DOM nodes, so the filed-entry animation plays only
// for genuinely new lines -- never for the 49 the window re-delivered.
export type JournalEntry = NarrationLine & { key: string };

/** The broker's WebSocket URL. In the pearl topology NEXT_PUBLIC_MERLE_MQTT_WS
 * (mcc/.env.local) supplies the whole URL; the fallback -- same host the page
 * was loaded from, port 9001 -- only ever worked when the broker shared a
 * machine with the dev server, and survives as a guard for any future
 * same-host broker.
 *
 * "localhost" is pinned to IPv4 127.0.0.1: Windows browsers resolve localhost
 * to IPv6 ::1 first, and the WebSocket to Mosquitto over ::1 fails to complete
 * the MQTT handshake even though the TCP connects (the daemon proxy dodges the
 * identical trap -- see next.config.ts). A real LAN hostname/IP is left as-is. */
export function busUrl(hostname: string, override?: string): string {
  if (override) return override;
  const host = !hostname || hostname === "localhost" ? "127.0.0.1" : hostname;
  return `ws://${host}:9001`;
}

// --- Pure parsing helpers (unit-tested in bus.test.ts) -----------------------

/** One narration line from an already-parsed JSON value; null when unusable.
 * Shared by the live topic and the journal window so the two can't drift. */
function lineFrom(o: unknown): NarrationLine | null {
  const l = o as Record<string, unknown> | null;
  if (typeof l?.text !== "string" || l.text === "") return null;
  return {
    ts: typeof l.ts === "string" ? l.ts : "",
    narrator: typeof l.narrator === "string" ? l.narrator : "unknown",
    voice: typeof l.voice === "string" ? l.voice : "",
    text: l.text,
    event_kind: typeof l.event_kind === "string" ? l.event_kind : "",
  };
}

/** Parse a narration/lines payload; null for anything malformed (the bus is a
 * shared room -- never let a stray message crash the journal). */
export function parseLine(payload: string): NarrationLine | null {
  try {
    return lineFrom(JSON.parse(payload));
  } catch {
    return null;
  }
}

/** Parse a narration/journal window payload ({lines: [...]}, oldest first):
 * the retained field journal (issue #58). Null when the payload isn't a
 * window at all; individual bad lines are dropped without discarding the
 * rest -- one stray line must not blank the whole journal. */
export function parseJournal(payload: string): NarrationLine[] | null {
  try {
    const o = JSON.parse(payload) as { lines?: unknown } | null;
    if (!Array.isArray(o?.lines)) return null;
    return o.lines
      .map(lineFrom)
      .filter((l): l is NarrationLine => l !== null);
  } catch {
    return null;
  }
}

/** Journal window (oldest first, as published) -> display entries (newest
 * first) with stable, unique keys. A same-second duplicate line gets an
 * occurrence suffix so keys stay unique without giving up stability. */
export function toJournalEntries(lines: NarrationLine[]): JournalEntry[] {
  const seen = new Map<string, number>();
  const entries = lines.map((line) => {
    const base = `${line.ts}|${line.narrator}|${line.text}`;
    const n = seen.get(base) ?? 0;
    seen.set(base, n + 1);
    return { ...line, key: n ? `${base}|${n}` : base };
  });
  return entries.reverse();
}

/** "narrators/marlin/status" -> "marlin"; null for any other topic. */
export function statusTopicId(topic: string): string | null {
  const m = /^narrators\/([^/]+)\/status$/.exec(topic);
  return m ? m[1] : null;
}

/** "narration/journal/marlin" -> "marlin"; null for any other topic --
 * including the retired bare "narration/journal" (a stale retained blob from
 * before issue #80 must not be mistaken for a narrator's window). */
export function journalTopicId(topic: string): string | null {
  const m = /^narration\/journal\/([^/]+)$/.exec(topic);
  return m ? m[1] : null;
}

/** Merge per-narrator journal windows (issue #80) into one show-wide window:
 * oldest first (the wire order toJournalEntries expects), interleaved by ts,
 * capped at `limit` keeping the newest. Line ts values are ISO strings, so
 * string comparison is chronological; the sort is stable, so same-second
 * lines keep their within-window order. */
export function mergeJournals(
  windows: Record<string, NarrationLine[]>,
  limit: number,
): NarrationLine[] {
  return Object.values(windows)
    .flat()
    .sort((a, b) => (a.ts < b.ts ? -1 : a.ts > b.ts ? 1 : 0))
    .slice(-limit);
}

/** Voice colors (issue #89 follow-up): a stable accent per narrator so the
 * Field Journal's back-and-forth reads at a glance -- worn by the name stamp
 * and the entry rail, never the body text (the voice stays ink; hue carries
 * identity, intensity carries recency). The named cast is art-directed --
 * the host warm squirrel-orange, the field man turkey-khaki -- and any
 * future guest voice gets a deterministic pick from the same palette (led
 * last: it moonlights as the live/newest signal elsewhere in the panel). */
const CAST_COLORS: Record<string, string> = {
  Marlin: "var(--squirrel)",
  Jim: "var(--turkey)",
};
const VOICE_PALETTE = [
  "var(--squirrel)",
  "var(--turkey)",
  "var(--chipmunk)",
  "var(--led)",
];

export function voiceColor(narrator: string): string {
  const cast = CAST_COLORS[narrator];
  if (cast) return cast;
  let h = 0;
  for (let i = 0; i < narrator.length; i++)
    h = (h * 31 + narrator.charCodeAt(i)) >>> 0;
  return VOICE_PALETTE[h % VOICE_PALETTE.length];
}

/** Match a persona's tts_voice hint ("David") against the browser's installed
 * voices by substring, case-insensitive. Null means "use the default voice". */
export function pickVoice<V extends { name: string }>(
  voices: V[],
  hint: string,
): V | null {
  if (!hint) return null;
  const needle = hint.toLowerCase();
  return voices.find((v) => v.name.toLowerCase().includes(needle)) ?? null;
}
