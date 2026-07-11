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
export const NARRATOR_STATUS_WILDCARD = "narrators/+/status";

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
