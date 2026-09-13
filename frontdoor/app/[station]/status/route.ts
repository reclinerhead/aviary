// GET /<station>/status -- last check-in time and current address. For Todd,
// and for the text message that replaces a lost bookmark. Unauthenticated on
// purpose: a LAN address is not a secret, and the SSID is never stored.

import { isValidSlug } from "@/lib/frontdoor";
import { notReportedPage, statusPage, unknownStationPage } from "@/lib/pages";
import { readStation } from "@/lib/store";

export const dynamic = "force-dynamic";

const HTML = { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" };

export async function GET(_request: Request, { params }: { params: Promise<{ station: string }> }) {
  const { station } = await params;
  if (!isValidSlug(station)) {
    return new Response(unknownStationPage(), { status: 404, headers: HTML });
  }
  let rec;
  try {
    rec = await readStation(station);
  } catch (err) {
    console.error(`status: store read failed for ${station}: ${(err as Error).message}`);
    return new Response("store unavailable", { status: 503, headers: { "Cache-Control": "no-store" } });
  }
  if (!rec) {
    return new Response(notReportedPage(station), { status: 404, headers: HTML });
  }
  return new Response(statusPage(station, rec), { status: 200, headers: HTML });
}
