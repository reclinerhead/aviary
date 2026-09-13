// The one KV: a JSON record per station, keyed `station:<id>`. Upstash Redis
// via the Vercel Marketplace -- `Redis.fromEnv()` reads KV_REST_API_URL /
// KV_REST_API_TOKEN (what the Marketplace injects) or the UPSTASH_REDIS_REST_*
// pair, whichever is set. No TTL: the last-known address is the best answer
// even when it is stale (issue #545's redirect-regardless-of-staleness rule).

import { Redis } from "@upstash/redis";

import type { StationRecord } from "./frontdoor";

let client: Redis | null = null;

function redis(): Redis {
  if (!client) client = Redis.fromEnv();
  return client;
}

export async function readStation(id: string): Promise<StationRecord | null> {
  return (await redis().get<StationRecord>(`station:${id}`)) ?? null;
}

export async function writeStation(id: string, record: StationRecord): Promise<void> {
  await redis().set(`station:${id}`, record);
}
