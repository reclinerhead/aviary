# The Flyway

> Spoke of the [Merle Technical Guide](../../TechnicalGuide.md) — read the hub first for the machine roster, quick start, and cross-cutting conventions.
>
> **Covers:** the station network layer — `flyway/` (the liveness watcher; the front-door check-in, #545) and `frontdoor/` (the public redirect site, the repo's one Vercel surface); epic #212 Phase 3's hub store + sync and Phase 4's bus bridge land here later
> **Runs on:** pearl
> **Related:** epic #212 (remote stations + The Flyway); issue #342

## What the Flyway is

The network layer of epic #212: everything on the house orchestrator (orchid after #371 Phase 3; was pearl) that concerns the
*remote* stations (birdhouse live at Mom's; kestrel live at Kathy's
since 2026-08-29, station 2). A station is
fully self-contained by principle — own broker, own `earl.db`, own dashboard
— so nothing here is load-bearing for a station. The Flyway is where the
family's boxes become visible to each other: liveness today, the aggregated
detections (`flyway.db`) and the Aviary's Flyway tab post-birthday.

`flyway/` is a role package like the rest (`listener/`, `weatherpost/`):
named for what it does, not what's in it, and deliberately the future home
of the Phase 3 courier/hub code so the epic's later phases are additions to
a package, not new root modules.

## The liveness watcher (`flyway/liveness.py`, issue #342)

Pearl's word on whether each station is alive. The house's Last-Will idiom
can't answer this — a station's will fires on its *own* loopback broker
(principle 1), and the tailnet ACL forbids the station pushing anything to
pearl (principle 7) — so pearl polls, in the one direction the ACL allows,
and speaks for the station on its own bus:

- **Probes the station's dashboard, not its IP** — an HTTP 2xx/3xx from the
  Aviary through the station's Caddy proves box powered + network up +
  service serving; a ping only proves a kernel.
- **Publishes retained `online`/`offline` to `network/<station>/status`**
  (`bus.station_status_topic()`) — the `weather/status` payload contract
  verbatim, in the namespace Phase 4 already reserves for
  `network/<station>/audio/events`. The watcher is that bridge's first
  tenant, not a one-off.
- **Debounced in code, not config**: offline after `OFFLINE_AFTER` (5)
  consecutive missed probes — about 5 minutes at the default cadence —
  and back online after `ONLINE_AFTER` (2) consecutive answers. Her power,
  her internet, and our internet are three different outages that look
  identical from pearl, and a flapping lamp is noise (the #175
  visit-debounce lesson, one domain over). The state machine (`step()`) is
  pure and unit-tested (`test_flyway_liveness.py`); "unknown" flips online
  on the *first* good probe — startup generosity carries no flap risk —
  but going dark always costs the full threshold.
- **Verdicts republish only on change.** A watcher restart begins at
  "unknown" and stays quiet until the debounce earns a verdict; the
  broker's previously-retained value stands in the meantime, which is the
  honest best answer available.
- **The watcher's own presence** rides `services/station-liveness/status`
  (the standard Last-Will contract), so a dead watcher is visible as
  itself — never mistaken for a dead station, whose retained verdict would
  merely go stale.
- **The audience is us, not her.** The lamp lives on *our* Homestead — the
  Birdhouse tile in `launchpad/tiles.json` carries the topic in its
  `presence` key (config-only, the #147 mechanism). She won't notice a
  dark dashboard; we need to, before it becomes a birthday-week house call.

Config is the `bus.py` ethos — `MERLE_STATIONS` (required, no default;
comma-separated `name=url` pairs, names topic-safe `[a-z0-9_-]` because
they become topic segments) and `MERLE_LIVENESS_INTERVAL_S` (default 60).
Ops detail, unit file, and desk tests: `Servers/Pearl.md` § The station
liveness watcher.

## The front door (`flyway/checkin.py` + `frontdoor/`, issue #545)

A station's address on a LAN we don't administer is whatever the router
leased the Wi-Fi stick, and `<station>.local` never resolved at either
house. So the bookmark is a **stable public URL that redirects to the
station's current LAN address**:

```
https://aviary.toddtech.llc/<MERLE_STATION_ID>[/path]   -> 302 http://<lan ip>/[path]
https://aviary.toddtech.llc/<id>/status                  -> last check-in + address (for us)
POST https://aviary.toddtech.llc/api/checkin             -> the station reporting itself
```

Two halves, one contract, one repo:

- **The station reports itself.** `flyway/checkin.py` (stdlib only) reads
  `ip -j -4 addr show`, picks the RFC 1918 address on the Wi-Fi interface
  (`wl*` first, any private address as fallback — **never** `169.254/16`
  or the tailnet's `100.64/10`, neither reachable from a relative's
  browser), and POSTs `{station, ip, iface, sha, ts}` with a per-station
  bearer token. It runs as the `station-checkin` oneshot: on boot, every
  15 minutes from a timer, and the moment NetworkManager fires
  `up`/`dhcp4-change` (a dispatcher hook that carries no secrets — the
  unit's `EnvironmentFile` does). Env: `MERLE_STATION_ID`,
  `MERLE_FRONTDOOR_URL`, `MERLE_FRONTDOOR_TOKEN`, all required-no-default.
  Pure logic (`pick_lan_addr`, `build_payload`) in `test_flyway_checkin.py`.
- **The site remembers and redirects.** `frontdoor/` is a Next.js app of
  route handlers only (no React tree), deployed as its own Vercel project
  with Root Directory `frontdoor` — the repo's **one** Vercel surface. The
  check-in resolves the bearer token to a station in constant time against
  the `FRONTDOOR_STATIONS` roster (`id:token,…`), requires the body's
  station to be that one, rejects anything but a private IPv4, and stores
  `{ip, iface, sha, seen_at}` in Upstash Redis (Vercel Marketplace; the
  SSID is never sent or stored). The redirect is **302, never 301, with
  `no-store`** — a cached permanent redirect would bake the IP into the
  browser, the exact failure this replaces — and goes to the last-known
  address regardless of age (the address stays right until the router
  changes it; a station whose internet is down is in a house whose
  internet is down). Pure logic (slug/IP validation, roster parsing,
  redirect-target building that cannot leave the station origin) in
  `frontdoor/lib/frontdoor.test.ts`.

Why this is principle-7-clean: the station's only new traffic is HTTPS
egress to one public endpoint — the "ordinary internet egress" class the
epic already uses for autodeploy and the courier. No ACL rule, nothing
inbound. The station page itself stays plain HTTP on the LAN
(`Caddyfile.station`); the HTTPS→HTTP redirect is followed by every
browser, and the "Not Secure" label on the Aviary is the same one the IP
bookmarks showed. A fully-HTTPS station (public DNS name carrying the
private address + a DNS-01 certificate) is a possible follow-up, not this.

Deployment-day rule: **Todd sets the bookmarks**, home-screen icons, and
the QR sticker to the front-door URL — a person who bookmarks after
landing bookmarks the IP again. The DHCP reservation becomes optional.
Ops (units, hook, rollout, failure modes): `Servers/Birdhouse.md` § The
units and step 13; Vercel project setup: `frontdoor/README.md`.

## What arrives here later (decided in epic #212, not yet built)

- **Phase 3 — the hub**: `flyway.db` beside pearl's `earl.db`; stations
  push nightly digests and pull the family rollup over one tailnet port
  (the single ACL rule `tag:station → pearl:<port>`). Store-and-forward,
  station-local `earl.db` always the source of truth, every field from a
  station validated as untrusted input.
- **Phase 4 — the fun layer**: the Flyway tab in the Aviary, the bridge
  republishing remote events as `network/<station>/audio/events` (lifers
  near-real-time, the rest nightly), the weekly template-generated Flyway
  Report. No LLM in the delivery path.
