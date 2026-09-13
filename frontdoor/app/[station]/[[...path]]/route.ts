// GET /<station>[/<path...>] -- the redirect (issue #545).
//
// 302, never 301, and no-store: a cached permanent redirect would bake the
// LAN address into the browser, which is the exact failure the front door
// exists to end. Redirect to the last-known address regardless of how old it
// is -- the address stays right until the router changes it, and a station
// whose internet is down is in a house whose internet is down.

import type { NextRequest } from "next/server";

import { buildTarget, isValidSlug } from "@/lib/frontdoor";
import { notReportedPage, unknownStationPage } from "@/lib/pages";
import { readStation } from "@/lib/store";

export const dynamic = "force-dynamic";

const HTML = { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" };

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ station: string; path?: string[] }> },
) {
  const { station, path } = await params;
  if (!isValidSlug(station)) {
    return new Response(unknownStationPage(), { status: 404, headers: HTML });
  }
  let rec;
  try {
    rec = await readStation(station);
  } catch (err) {
    console.error(`redirect: store read failed for ${station}: ${(err as Error).message}`);
    return new Response("store unavailable", { status: 503, headers: { "Cache-Control": "no-store" } });
  }
  if (!rec) {
    return new Response(notReportedPage(station), { status: 404, headers: HTML });
  }
  const target = buildTarget(rec.ip, path, request.nextUrl.search);
  if (!target) {
    return new Response(unknownStationPage(), { status: 404, headers: HTML });
  }
  return new Response(null, {
    status: 302,
    headers: { Location: target, "Cache-Control": "no-store" },
  });
}
