// =============================================================================
// project-squirrel -- frontdoor/lib/frontdoor.ts
//
// The pure logic of the station front door (issue #545): slug and address
// validation, the roster parser, constant-time token matching, and the
// redirect target builder. Nothing in here touches the network or the KV;
// that lives in store.ts and the route handlers. Covered by frontdoor.test.ts.
// =============================================================================

import { createHash, timingSafeEqual } from "node:crypto";

/** A station id: lowercase, hostname-safe, the MERLE_STATION_ID contract. */
const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;

/** What a station may claim its interface is called (wlx<mac>, enp1s0...). */
const IFACE_RE = /^[A-Za-z0-9_.-]{1,32}$/;

/** A git short (or full) sha. */
const SHA_RE = /^[0-9a-f]{4,40}$/;

export function isValidSlug(slug: unknown): slug is string {
  return typeof slug === "string" && SLUG_RE.test(slug);
}

export function isValidIface(iface: unknown): iface is string {
  return typeof iface === "string" && IFACE_RE.test(iface);
}

export function isValidSha(sha: unknown): sha is string {
  return typeof sha === "string" && SHA_RE.test(sha);
}

/**
 * RFC 1918 only: 10/8, 172.16/12, 192.168/16. Deliberately NOT link-local
 * (169.254/16) and NOT the tailnet's 100.64/10 -- neither is an address a
 * relative's browser can reach, so accepting one would redirect them into
 * a black hole while the status page looked fine.
 */
export function isPrivateIPv4(ip: unknown): ip is string {
  if (typeof ip !== "string") return false;
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(ip);
  if (!m) return false;
  const [a, b, c, d] = m.slice(1).map(Number);
  if ([a, b, c, d].some((n) => n > 255)) return false;
  // Reject leading zeros ("192.168.01.5") so the stored form is canonical.
  if (m.slice(1).some((s) => s.length > 1 && s.startsWith("0"))) return false;
  if (a === 10) return true;
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  return false;
}

/**
 * FRONTDOOR_STATIONS: comma-separated `id:token` pairs. Fails loud on any
 * malformed entry (the bus.py ethos): a roster that half-parses would let a
 * station silently fall off the front door.
 */
export function parseRoster(raw: string | undefined): Map<string, string> {
  const roster = new Map<string, string>();
  if (!raw || !raw.trim()) {
    throw new Error("FRONTDOOR_STATIONS is required (id:token,id:token)");
  }
  for (const entry of raw.split(",")) {
    const item = entry.trim();
    if (!item) continue; // tolerate a trailing comma
    const colon = item.indexOf(":");
    if (colon < 1) throw new Error(`FRONTDOOR_STATIONS entry has no token: ${item}`);
    const id = item.slice(0, colon).trim();
    const token = item.slice(colon + 1).trim();
    if (!isValidSlug(id)) throw new Error(`FRONTDOOR_STATIONS bad station id: ${id}`);
    if (token.length < 16) throw new Error(`FRONTDOOR_STATIONS token too short for ${id}`);
    if (roster.has(id)) throw new Error(`FRONTDOOR_STATIONS duplicate station: ${id}`);
    roster.set(id, token);
  }
  if (roster.size === 0) throw new Error("FRONTDOOR_STATIONS has no stations");
  return roster;
}

/** Constant-time equality over sha256 digests, so length never leaks. */
export function tokensMatch(presented: string, expected: string): boolean {
  const a = createHash("sha256").update(presented).digest();
  const b = createHash("sha256").update(expected).digest();
  return timingSafeEqual(a, b);
}

/**
 * Which station does this bearer token belong to? Every roster entry is
 * compared (no early exit on a hit) so timing is the same for a miss and a
 * hit. Returns null for a token nobody owns.
 */
export function stationForToken(roster: Map<string, string>, presented: string): string | null {
  let found: string | null = null;
  for (const [id, token] of roster) {
    if (tokensMatch(presented, token)) found = id;
  }
  return found;
}

/**
 * The redirect target. The origin is built from a validated private IPv4,
 * so the result can never leave the station: a path segment cannot smuggle
 * a host in because each segment is percent-encoded and joined onto a fixed
 * `http://<ip>/`. Empty segments and dot segments are refused outright.
 */
export function buildTarget(ip: string, segments: readonly string[] | undefined, search = ""): string | null {
  if (!isPrivateIPv4(ip)) return null;
  const parts = segments ?? [];
  if (parts.length > 16) return null;
  for (const s of parts) {
    if (!s || s === "." || s === ".." || s.includes("/") || s.includes("\\")) return null;
    if (s.length > 200) return null;
  }
  const path = parts.map(encodeURIComponent).join("/");
  const query = search && search.startsWith("?") && search.length > 1 ? search : "";
  return `http://${ip}/${path}${query}`;
}

/** "3 minutes ago" for the status page; the input is an ISO timestamp. */
export function describeAge(seenAtIso: string, now: Date = new Date()): string {
  const seen = Date.parse(seenAtIso);
  if (Number.isNaN(seen)) return "unknown";
  const s = Math.max(0, Math.round((now.getTime() - seen) / 1000));
  if (s < 60) return s === 1 ? "1 second ago" : `${s} seconds ago`;
  const m = Math.round(s / 60);
  if (m < 60) return m === 1 ? "1 minute ago" : `${m} minutes ago`;
  const h = Math.round(m / 60);
  if (h < 48) return h === 1 ? "1 hour ago" : `${h} hours ago`;
  const d = Math.round(h / 24);
  return `${d} days ago`;
}

export type StationRecord = {
  ip: string;
  iface: string | null;
  sha: string | null;
  seen_at: string; // ISO, the server's clock
};
