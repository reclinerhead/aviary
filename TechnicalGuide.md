# The Aviary: technical guide

The living record of how the Aviary is built and why. It describes the current state; chronology lives in `git log`, the pull requests and the issues. This file is the hub: what holds across the whole project. Each component has a spoke under [`docs/guide/`](docs/guide/).

**How to read it (coding agents included):** read this hub, then open only the spokes for the components you are touching. A PR that changes a component updates that component's spoke in the same commit set; a PR that changes a project-wide convention updates this hub.

## What the Aviary is

An always-on bird listener and the field guide it keeps for itself. Microphones in a yard feed **Earl**, who runs BirdNET on every three-second window, decides what is a real local bird, and records it. From that record the Aviary keeps a life list, the best recordings of each species, a portrait and profile for every bird, and dated field notes. **Willard** runs a weather post beside it, so the record can be read against the weather. A web app shows all of it.

It began inside `project-squirrel`, a private everything-repo for one household (a driveway vision model, a rover, home automation). The Aviary moved here with its history in October and November 2026 (epic #627). Issue and PR numbers mean the same thing in both repos, so a `#N` in code, a commit or this guide resolves here. The guide sometimes names things that stayed behind (the vision daemon, the narrators, the MCC dashboard, the boxes `pearl`, `bluejay` and `merle`). They appear only where they explain why this repo's code is the way it is.

## Where it runs

Three boxes run the same code from this repo, each for a different yard. Nothing is shared between them at runtime: each has its own broker, its own stores and its own web app.

| Box | Where | Role | Deploys from |
| --- | --- | --- | --- |
| **orchid** | the house (Kalamazoo, Michigan) | The house Aviary: two camera microphones and a garage microphone, the field notes' writer chain, and the liveness watcher for the stations | `main`, within a minute of a merge |
| **birdhouse** | another household | A field station: one USB microphone | `aviary-station-stable`, nightly |
| **kestrel** | another household | A field station: a USB microphone and a Wi-Fi microphone | `aviary-station-stable`, nightly |

The stations are small fanless PCs on someone else's Wi-Fi, hours from a keyboard. Two rules follow from that and shape much of the code:

- **A station is self-contained.** No station depends on anything at the house, and every external fact it needs arrives in one file, `~/station.env` (`station.env.example` is the committed contract).
- **A station initiates nothing toward the house.** The tailnet policy names no station as a source. The house dials in (the liveness probe); a station's only outbound call is the HTTPS check-in to the public front door.

How each box is installed, its unit files, and its traps are not in this repo. They are the runbooks in `reclinerhead/toddtech-infrastructure`: `servers/Orchid.md`, `servers/Birdhouse.md`, `servers/Kestrel.md`. Read the runbook before touching a box.

## The services

Every box runs the first five. Each is a systemd unit running a module from the checkout (`python -m package.module`, never a file path, so the repo root is on `sys.path`).

| Unit | Code | Owns |
| --- | --- | --- |
| `earl-listener` | `listener/earl.py`, `listener/gate.py` | Capture, the speech gate, BirdNET, the region mask, the confirm hold, visits, clips. Publishes `audio/*`. Runs from its own venv (`~/earl-venv`), because BirdNET's runtime depends on the CPU |
| `earl-sightings` | `listener/sightings.py` | The bird record, `earl.db`: sightings, the life list, the region mask in use, clip retention |
| `earl-enrichment` | `listener/enrichment_loop.py` and the passes it runs | Everything that dresses the record: Wikipedia profiles and portraits, clip enhancement, the three-recording showcase, fun facts, and (at the house) field notes |
| `willard-weather` | `weatherpost/` | The weather post and `weather.db`, the seasonal archive. Publishes `weather/*` |
| `aviary-web` | `web/` | The Next.js app: the Aviary and its weather tab, on port 3000 behind Caddy |
| `station-liveness` (orchid) | `flyway/liveness.py` | The house's word on whether each station is alive. Publishes `network/<station>/status` |
| `station-checkin` (stations) | `flyway/checkin.py` | Tells the public front door the station's current LAN address |
| `station-netwatch` (stations) | `flyway/netwatch.py` | Recovers a half-dead Wi-Fi stick without a power cycle |
| `aviary-autodeploy` (orchid), `station-autodeploy` (stations) | `Servers/autodeploy.sh` | The deploy watcher |

`frontdoor/` is the one piece that runs somewhere else: a tiny Next.js app on Vercel that answers `https://aviary.toddtech.llc/<station>` with a redirect to the address the station last reported.

## The bus

Each box has its own Mosquitto broker. `bus.py` holds every topic name, and every process imports its topics from there. The bus is live transport only; SQLite is the archive.

| Topic | Publisher | Retained | Carries |
| --- | --- | --- | --- |
| `audio/events` | Earl | no | One JSON object per accepted detection or notable sound |
| `audio/status` | Earl | yes | `online` / `offline` (the Last Will) |
| `audio/sources` | Earl | yes | Per-microphone health |
| `audio/mask` | Earl | yes | The region mask in use: week, status, every allowed species with its geo score |
| `weather/current`, `weather/forecast`, `weather/history` | Willard | yes | Conditions, the forecast series, the rolling 48-hour window |
| `weather/report` | Willard | yes | Willard's on-air segment, when a model is configured |
| `weather/status` | Willard | yes | `online` / `offline` |
| `services/<name>/status` | any other service | yes | Presence, same contract. Today: `services/station-liveness/status` |
| `network/<station>/status` | `station-liveness` on orchid | yes | The house's debounced verdict on a station |

Two rules: **retained topics carry state, event topics carry moments** (never retain an event), and **timestamps on the bus and in the stores are unix epoch seconds** (the browser formats them).

## How code reaches a box

**Merging a pull request is the deploy.** Nobody copies files to a box.

- `Servers/autodeploy.sh` runs on every box as a root loop service. Every 60 seconds it fetches its branch; on a change it pulls `--ff-only`, restarts the units in `MERLE_DEPLOY_UNITS`, and rebuilds and restarts `aviary-web` only if the merge touched `web/` (`MERLE_DEPLOY_MCC=1`; the name predates the rename and is kept).
- **orchid follows `main`.** A merge is live at the house within a minute.
- **The stations follow `aviary-station-stable`**, a fast-forward-only branch that nobody commits to. The `promote-stations` GitHub Action moves it to `main`'s tip once a night (07:00 UTC), so a station absorbs at most one day of changes, while its house sleeps. An urgent fix is the same Action run by hand.
- A failed web build never restarts the app (the old build keeps serving). A dirty checkout is skipped loudly, never clobbered.
- The repo is public and every box fetches over anonymous https. **If the repo is ever made private, every box's fetch fails quietly**: a visibility change is a fleet event.

Because any merge restarts the Earl units, and a restarted listener is deaf for about three and a half minutes while its models load, related changes land as one PR where they can.

## Repo layout

| Path | Holds |
| --- | --- |
| `listener/` | Earl and everything built on the bird record: `earl.py` (the daemon), `gate.py` (the pure accept/reject core), `sightings.py` (the record), `mic_by_mac.py`, `masked_report.py`, the enrichment loop and its passes (`species_profile.py`, `clip_enhance.py`, `clip_vocal_fill.py`, `clip_showcase.py`, `fun_facts.py`), and the field journal (`findings.py`, `editor.py`, `species_analysis.py`, `notes_validate.py`, `region_prior.py`, `solar.py`) |
| `weatherpost/` | `weather.py` (Willard) and `weather_archive.py` |
| `flyway/` | `liveness.py`, `checkin.py`, `netwatch.py` |
| `web/` | The Next.js app (App Router, TypeScript, Tailwind 4, pnpm). Its own lockfile and Vitest suite |
| `frontdoor/` | The station front door (Next.js on Vercel). Its own lockfile and Vitest suite |
| `Servers/` | What a box runs or copies from the checkout: `autodeploy.sh`, `deploy-web.sh`, `Caddyfile.station`, the station Mosquitto config, the netwatch units |
| `tools/`, `fixtures/` | A one-off probe and a test fixture |
| repo root | `bus.py`, `feeds.py` and the registries (`feeds.yml` for the house, `feeds.station.yml`, `feeds.kestrel.yml`), the model clients (`ollama_client.py`, `gateway_client.py`), `station.env.example`, `requirements.txt`, and every `test_*.py` |

**Packages are named for their role, not their contents** (`weatherpost/`, not `weather/`): a package `X/` containing `X.py` makes `import X` resolve to the package. Every `__init__.py` is empty on purpose.

## Cross-cutting conventions

- **`MERLE_*` env vars, required with no default.** Anything a service cannot run correctly without (`MERLE_MQTT`, `MERLE_LATLON`) raises at startup. A misconfigured service must never look healthy while publishing into the void. The prefix is the old project's and is kept: renaming an env var is a change on three boxes.
- **Every path in a unit is absolute.** The web app's working directory is `web/` and every other unit's is the repo root, so the same relative `weather.db` would name two files.
- **The feed registry is the only place a microphone is declared.** Code dispatches on a feed's `kind`, never its name. A feed's name is also its `source` label in the record and its clip directory, so renaming one is a data migration.
- **The two irreplaceable files are `earl.db` and `weather.db`.** First-heard dates and past weather cannot be bought back. On every box they sit in the checkout root, gitignored. Ignored is not protected: **never run `git clean -x` in `~/aviary`.** Copy them with SQLite's backup API, never `cp` (the WAL holds the data).
- **Speech is never an event.** A window whose top label is human produces no event and no clip, by construction. Earl classifies; he does not record conversations.
- **Enrichment is a pass, never a migration.** Every batch transformation is a worklist-driven, idempotent, per-entity function with a thin CLI over it, re-runnable when new birds arrive.
- **Python computes, the model narrates.** Every figure in a field note is computed and stored before a model sees it, and a validator rejects prose that carries a number it was not given.
- **Pure logic is unit-tested; I/O seams are desk-checked.** Gating, ranking, parsing and shaping live in pure functions with tests. Capture loops, the broker and the model calls are checked on a box.
- **CI's pytest list is kept by hand.** `.github/workflows/tests.yml` names every Python test file. A new `test_*.py` that is not added to that line never runs.
- **Nothing that locates a house.** No coordinates finer than city level, and no network credentials, anywhere in the repo, its history or its issues.
- **A notice is a row in the registry.** Anything the Aviary wants to tell its owner goes through the one announcement strip (`NOTICE_KIND_RANK` in `web/lib/aviary.ts`), never a second piece of chrome.

## The spokes

| Spoke | Covers |
| --- | --- |
| [earl.md](docs/guide/earl.md) | The listener: capture, the gates, the region mask, the confirm hold, visits, clips, the bird record, the feed registry |
| [aviary.md](docs/guide/aviary.md) | The Aviary: the pages, the enrichment passes, the showcase, the field journal, the notice strip |
| [weather.md](docs/guide/weather.md) | Willard, the seasonal archive, and the weather tab |
| [web.md](docs/guide/web.md) | The Next.js app: the unit, the deploy order, runtime gotchas, the design language |
| [flyway.md](docs/guide/flyway.md) | The station network: liveness, the front door, the Wi-Fi watchdog |

## Keeping ProjectExperience.md current

[`ProjectExperience.md`](ProjectExperience.md) is a self-contained, factual summary of this project and the skills it demonstrates, written to be pasted into an AI session for resume or interview preparation. Update it at milestones (an epic ships, a new skill area enters the project, a cited number changes materially), not on routine changes, and refresh its "Last updated" date when you do.
