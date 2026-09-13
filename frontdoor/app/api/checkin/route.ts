// POST /api/checkin -- a station reporting where it is (issue #545).
//
//   Authorization: Bearer <that station's token>
//   { "station": "kestrel", "ip": "192.168.4.37", "iface": "wlx...", "sha": "abc1234", "ts": "..." }
//
// 401 for a token nobody owns, 403 when the token's station is not the one
// in the body, 400 for a body that fails validation, 200 with the stored
// record otherwise. The token roster is FRONTDOOR_STATIONS on the server;
// nothing about it is ever echoed back.

import { isPrivateIPv4, isValidIface, isValidSha, isValidSlug, parseRoster, stationForToken, type StationRecord } from "@/lib/frontdoor";
import { writeStation } from "@/lib/store";

export const dynamic = "force-dynamic";

const JSON_HEADERS = { "Content-Type": "application/json", "Cache-Control": "no-store" };

function reply(status: number, body: Record<string, unknown>): Response {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

export async function POST(request: Request) {
  let roster: Map<string, string>;
  try {
    roster = parseRoster(process.env.FRONTDOOR_STATIONS);
  } catch (err) {
    console.error(`checkin: ${(err as Error).message}`);
    return reply(500, { error: "front door misconfigured" });
  }

  const auth = request.headers.get("authorization") ?? "";
  const presented = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
  const owner = presented ? stationForToken(roster, presented) : null;
  if (!owner) return reply(401, { error: "unauthorized" });

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return reply(400, { error: "body must be JSON" });
  }
  if (typeof body !== "object" || body === null) return reply(400, { error: "body must be an object" });
  const { station, ip, iface, sha } = body as Record<string, unknown>;

  if (!isValidSlug(station)) return reply(400, { error: "station must be a lowercase hostname-safe id" });
  if (station !== owner) return reply(403, { error: "token does not belong to that station" });
  if (!isPrivateIPv4(ip)) return reply(400, { error: "ip must be a private IPv4 address (RFC 1918)" });
  if (iface !== undefined && iface !== null && !isValidIface(iface)) return reply(400, { error: "iface is malformed" });
  if (sha !== undefined && sha !== null && !isValidSha(sha)) return reply(400, { error: "sha is malformed" });

  const record: StationRecord = {
    ip,
    iface: isValidIface(iface) ? iface : null,
    sha: isValidSha(sha) ? sha : null,
    seen_at: new Date().toISOString(),
  };
  try {
    await writeStation(station, record);
  } catch (err) {
    console.error(`checkin: store write failed for ${station}: ${(err as Error).message}`);
    return reply(503, { error: "store unavailable" });
  }
  console.log(`checkin: ${station} at ${record.ip} (${record.iface ?? "iface?"}, ${record.sha ?? "sha?"})`);
  return reply(200, { ok: true, station, ...record });
}
