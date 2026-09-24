# frontdoor — the station front door (issue #545)

`https://aviary.toddtech.llc/<station>` redirects to wherever that Aviary
station lives on its home LAN right now. The station keeps the record
current itself (`flyway/checkin.py`, the `station-checkin` timer). Design
and contract: `docs/guide/flyway.md` § The front door. This README is the
ops side: how the Vercel project is set up and how a station is enrolled.

## What it serves

| Route | Answer |
| --- | --- |
| `GET /<station>[/path]` | `302` → `http://<last-known LAN ip>/path`, `Cache-Control: no-store`. Unknown slug or never-reported station → a plain 404 page, no redirect |
| `GET /<station>/status` | last check-in time, address, interface, build sha |
| `POST /api/checkin` | the station reporting itself — `Authorization: Bearer <token>`, JSON `{station, ip, iface?, sha?, ts?}` → `401` unknown token · `403` token/station mismatch · `400` bad body or non-private IPv4 · `200` stored |
| `GET /` | a static landing page; lists nothing |

Route handlers only — no pages, no React tree. Pure logic in
`lib/frontdoor.ts`, covered by `lib/frontdoor.test.ts` (vitest; the CI
`frontdoor` job runs it).

## Vercel project (one-time)

This folder is its own Vercel project; the rest of project-squirrel never
deploys to Vercel.

1. **Vercel → Add New → Project → Import `reclinerhead/project-squirrel`.**
   The Vercel GitHub app must have access to the repo (it is private).
2. **Root Directory: `frontdoor`.** Framework preset Next.js is detected.
   Leave "Include source files outside the Root Directory" off.
3. **Skip builds for unrelated commits.** Project → Settings → Git →
   enable the option that skips deployments when the Root Directory has no
   changes. If that toggle is not offered, set the Ignored Build Step
   command to `git diff --quiet HEAD^ HEAD -- .` (exit 0 = skip).
4. **Storage → Upstash for Redis → create + connect** to this project. The
   Marketplace injects `KV_REST_API_URL` / `KV_REST_API_TOKEN`; the code
   reads them via `Redis.fromEnv()` (the `UPSTASH_REDIS_REST_*` pair works
   too). Free tier is plenty: one small JSON value per station.
5. **Env `FRONTDOOR_STATIONS`** (Production + Preview): comma-separated
   `id:token` pairs, one per station, e.g.

   ```
   birdhouse:<48 hex>,kestrel:<48 hex>,lakehouse:<48 hex>
   ```

   Ids are the stations' `MERLE_STATION_ID`s. Tokens: `openssl rand -hex 24`
   on any box (PowerShell: `-join ((1..24) | % { '{0:x2}' -f (Get-Random -Max 256) })`).
   Never reuse a token across stations; rotating one is editing this var
   and the station's `station.env`.
6. **Domain `aviary.toddtech.llc`** → Project → Settings → Domains → Add.
   Vercel prints the CNAME to create at the DNS host for `toddtech.llc`.
7. Deploy (the first import does). Then `https://aviary.toddtech.llc/` shows
   the landing page and `/nothing` the "not a station" page.

## Enrolling a station

On the Vercel side: add `id:token` to `FRONTDOOR_STATIONS`, redeploy (env
changes need one). On the station: the two lines in `station.env`
(`MERLE_FRONTDOOR_URL`, `MERLE_FRONTDOOR_TOKEN` — `station.env.example` §
The front door), the `station-checkin` service + timer and the
NetworkManager dispatcher hook (`toddtech-infrastructure/servers/Birdhouse.md` § The units), then
`systemctl start station-checkin.service` and watch `/<id>/status` fill
in. Autodeploy ships the code; it never ships env or units.

Proving the API from a desk, with a station's token:

```
curl -s -X POST https://aviary.toddtech.llc/api/checkin \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"station":"birdhouse","ip":"192.168.1.47","iface":"wlx00c0cabcf4e4"}'
curl -si https://aviary.toddtech.llc/birdhouse | head -5     # 302 + Location + no-store
```

## Local

```
pnpm install
pnpm test
pnpm dev          # FRONTDOOR_STATIONS + KV_REST_API_* in .env.local to exercise the KV
```

Without the KV vars every station route answers 503/"store unavailable";
the landing page and the validators need nothing.
