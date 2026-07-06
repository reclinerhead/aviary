// Client for the Merle event bus (Mosquitto over WebSockets). Unlike daemon
// HTTP traffic -- which rides the /daemon/* rewrite -- the browser connects to
// the broker DIRECTLY: Next.js rewrites can't proxy WebSockets. The broker
// listens on ws://<this host>:9001 (mosquitto.conf at the repo root), so a
// phone on the LAN reaches it the same way it reaches the dev server.

export type NarrationLine = {
  ts: string;
  narrator: string;
  voice: string;
  text: string;
  event_kind: string;
};

export const NARRATION_TOPIC = "narration/lines";
export const NARRATOR_STATUS_WILDCARD = "narrators/+/status";

/** The broker's WebSocket URL: same host the dashboard was loaded from (works
 * on localhost and from a phone on the LAN alike), port 9001, unless
 * NEXT_PUBLIC_MERLE_MQTT_WS overrides the whole thing. */
export function busUrl(hostname: string, override?: string): string {
  if (override) return override;
  return `ws://${hostname || "localhost"}:9001`;
}

// --- Pure parsing helpers (unit-tested in bus.test.ts) -----------------------

/** Parse a narration/lines payload; null for anything malformed (the bus is a
 * shared room -- never let a stray message crash the journal). */
export function parseLine(payload: string): NarrationLine | null {
  try {
    const o = JSON.parse(payload);
    if (typeof o?.text !== "string" || o.text === "") return null;
    return {
      ts: typeof o.ts === "string" ? o.ts : "",
      narrator: typeof o.narrator === "string" ? o.narrator : "unknown",
      voice: typeof o.voice === "string" ? o.voice : "",
      text: o.text,
      event_kind: typeof o.event_kind === "string" ? o.event_kind : "",
    };
  } catch {
    return null;
  }
}

/** "narrators/marlin/status" -> "marlin"; null for any other topic. */
export function statusTopicId(topic: string): string | null {
  const m = /^narrators\/([^/]+)\/status$/.exec(topic);
  return m ? m[1] : null;
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
