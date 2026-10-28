# The Aviary

**An always-on bird listener for the backyard, and the field guide it keeps for itself.**

The Aviary listens to the yard around the clock, names every bird it hears, keeps the recordings worth keeping, and turns months of detections into something a person wants to read. It runs in three yards today: the house, plus two self-contained field stations at family members' homes that have been listening since August 2026. The house alone has logged more than a hundred species since July.

Three characters do the work:

- **Earl** (`listener/`) is the ears. He pulls raw audio from every microphone in the yard, screens out human speech, runs Cornell's BirdNET on every three-second window, and keeps the bird record.
- **The Aviary** (`web/`) is the face: a web app that shows the life list, plays the recordings, and charts when each species comes and goes.
- **Willard** (`weatherpost/`) is the weatherman: a backyard weather station plus a forecast, so the birds have context.

## What it does

- **Listens day and night.** Every three-second window from every mic goes through BirdNET 2.4. A detection has to clear a confidence bar (raised on windy days), fit BirdNET's range model for this place and week, and, if it lands in the uncertain middle band, be heard twice before it counts. One singing cardinal is one *visit*, not twenty-five events.
- **Never records a conversation.** A YAMNet classifier hears every window first, and a window with human speech in it is dropped before BirdNET sees it. No event, no clip. That rule lives in code, not in a setting.
- **Keeps the recordings that matter.** Each visit saves a nine-second clip, and an enhancement pass strips power-line hum and low-frequency noise (traffic, wind, the furnace) so a faint bird is actually audible. The first recording of every species is kept forever. Each species also gets a three-clip repertoire: its best recording plus two more that are each a *different* call, picked by comparing BirdNET's own audio embeddings.
- **Builds a life list.** First-heard is a fact the database enforces: replays and restarts can never move it.
- **Shows it all in the Aviary.** Every species is a tile with its visit count, and a live ticker announces arrivals with inline playback. Each species has its own page with a Wikipedia portrait and summary, activity charts, its recordings, and field notes. Tabs cover the full detection record, the species mask (what the range model expects here this week), and the weather.
- **Writes field notes.** Python computes the facts and a language model narrates them: a local model through Ollama, or Claude on demand. Every figure a note uses is stored so it can be checked.
- **Watches the weather.** At home, an Ecowitt gateway and sensor array is the source of truth for everything it can measure, and OpenWeather supplies the forecast. Field stations without instruments run on OpenWeather alone.
- **Runs at family members' homes.** A field station is a small, low-power box with its own message broker, database and dashboard, so it never depends on the house. Each station gets a stable bookmark through a public front door that redirects to wherever the local router put it, and the house checks every minute that each station is still up.

## Tech stack

| Layer | Built with |
| --- | --- |
| Listening | Python 3.11, BirdNET 2.4 (TensorFlow, or LiteRT on CPUs without AVX), YAMNet, ffmpeg and ALSA capture, numpy for the clip DSP |
| Microphones | IP camera audio over RTSP, USB microphones, an ESP32-S3 network mic |
| Messaging and storage | MQTT (Mosquitto, paho-mqtt, and mqtt.js over WebSockets for the live page), SQLite |
| Web app | Next.js 16 (App Router), React 19, TypeScript, Tailwind CSS 4, `node:sqlite`, pnpm |
| Field notes | Ollama with a local model; Claude through the Vercel AI Gateway, on demand |
| Weather | Ecowitt GW2000B gateway and WH90 sensor array (local API), OpenWeather |
| Stations and ops | systemd, Caddy, Tailscale, git-driven autodeploy with a promotion channel for the field stations, and a front door on Vercel backed by Upstash Redis |
| Tests | pytest and Vitest, run by GitHub Actions on every pull request |

## Under the hood

https://github.com/user-attachments/assets/5a01a753-f71f-43d1-9641-45e526c57d93

## What's next

**The community layer** ([#556](https://github.com/reclinerhead/aviary/issues/556)) is the big one. Three yards run the same listener in the same region, and none of them knows what the others heard: nobody can see that one yard logged a Pileated Woodpecker the same week another got its first. The plan:

- Each station shares a nightly digest of its day: which species, how many visits, its best recordings.
- A hub at the house gathers the digests into a family life list, with station firsts, family firsts, and per-species races (who heard it first, who hears it most). Every yard runs the same thresholds, and the rollup favors firsts and records over raw counts, which a rural yard would always win.
- A new **Flyway** tab in every station's Aviary shows the family picture, with an honest "as of" stamp when the hub is out of reach.
- A weekly Flyway Report, written from templates. No language model in the delivery path.

Also on the way:

- **The field journal** ([#611](https://github.com/reclinerhead/aviary/issues/611)): a finding engine that looks for what a naturalist would lead with (first song against sunrise, the species that arrived and left with the season, the "swallow" that only sings at 2 a.m.) and writes dated entries worth reading.
- **Dawn Chorus** ([#507](https://github.com/reclinerhead/aviary/issues/507)): a short spoken summary of yesterday in each yard, waiting in the morning.
- **Announcements** ([#447](https://github.com/reclinerhead/aviary/issues/447)): one quiet strip in the Aviary for the moments worth a mention, like a new best recording or a rare visitor.
- **Your own photos** ([#213](https://github.com/reclinerhead/aviary/issues/213)): replace the Wikipedia portrait with a photo of the actual bird in your yard.

## A guided tour

https://github.com/user-attachments/assets/6105475a-8c8b-46a3-a488-5347b5bb7b35

## Repository layout

| Path | What lives there |
| --- | --- |
| `listener/` | Earl: capture, the speech and bird gates, visits, clips, the bird record, and the enrichment passes (species profiles, clip enhancement, the repertoire, field notes) |
| `weatherpost/` | Willard: the weather post and its seasonal archive |
| `flyway/` | The station network: the liveness watcher and the front-door check-in |
| `web/` | The Next.js web app: the Aviary and its weather tab |
| `frontdoor/` | The stations' stable public address (Next.js route handlers on Vercel) |
| `Servers/` | Caddy and Mosquitto config, and the deploy scripts |
| `docs/guide/` | The technical guide, one page per area |
| repo root | `bus.py` (the MQTT topic contract), `feeds.py` and `feeds*.yml` (the microphone registry), the model clients, `station.env.example`, and the pytest suites |

## Documentation

The technical guide in [`docs/guide/`](docs/guide/) is the living record of how everything is built and why: [Earl](docs/guide/earl.md), [the Aviary](docs/guide/aviary.md), [the web app](docs/guide/web.md), [the weather post](docs/guide/weather.md) and [the Flyway](docs/guide/flyway.md). Non-trivial changes start as an issue and land as a pull request that closes it, so the issue history is the design history, back to the first commit in July 2026.

## License

[MIT](LICENSE)
