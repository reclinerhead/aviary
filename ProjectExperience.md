# Project Experience — The Aviary

**What:** an always-on acoustic bird monitor and the field guide it writes for itself, running at three houses — designed, built, deployed and operated solo
**Active:** July 2026 – present · in production 24/7
**Repository:** https://github.com/reclinerhead/aviary (public, MIT)
**Last updated:** 2026-11-20 · updated at milestones, not on every change (see TechnicalGuide.md)

> **Purpose of this file.** A self-contained, factual summary of this project and the skills it demonstrates, written to be pasted into an AI session for resume building, cover letters or interview prep. Every claim is meant to be interview-defensible. It covers the Aviary only. The Aviary grew up inside a larger private household platform (a driveway vision model, a rover, home automation); that wider work is not described here.

## The project in three paragraphs

The Aviary listens to a yard around the clock. Microphones feed a daemon that runs Cornell's BirdNET classifier on every three-second window, filters the result through a speech gate, a regional plausibility mask, a confidence threshold that rises in wind, and a two-window confirmation hold, and records what survives. From that record it keeps a life list with first-heard dates, picks the three best recordings of each species (each a different call), fetches a portrait and profile for every new bird, and writes dated field notes in which every figure is computed in Python before a language model is allowed to narrate it. A weather post runs beside it so the birds can be read against the weather.

It runs on three boxes at three houses: the author's own, and two "field stations" on small fanless PCs at other households, hours from a keyboard, on Wi-Fi the author does not control. Each box is fully self-contained (its own MQTT broker, SQLite stores and web app). A merge to `main` deploys itself: to the house within a minute, and to the stations overnight through a fast-forward-only release branch promoted by a GitHub Action. As of 2026-11-20 the house record holds 29,403 sightings of 121 species since 2026-07-18.

Development is issue-driven and AI-assisted: every non-trivial change starts as a GitHub issue with an explicit contract (design, do-not-change list, test contract, acceptance criteria) and ends as a reviewed pull request that closes it, with the living technical guide updated in the same commit set. In October 2026 the project was split out of a private monorepo into this public repository with its commit history and issue numbers intact.

## Skills demonstrated

### Acoustic ML in production
- Operates BirdNET (a CNN acoustic bird classifier) 24/7 over multiple microphones per site, with a warm prediction session per source in its own spawned process.
- Designed the accept/reject gate as a pure, fully unit-tested core: a hard speech invariant (a window classified as human speech produces no event and no recording), a weekly regional species mask from BirdNET's geo model, a wind-raised confidence threshold, per-species operator rules, and a confirmation hold that suppresses one-off mid-confidence false positives.
- Added a YAMNet front classifier for speech and notable sounds, and documented the failure that shaped it: shipped first as a veto on BirdNET, it discarded 17 of 25 confirmed detections in an afternoon, and was rebuilt so that only speech withholds a window.
- Ported the pipeline to a no-AVX CPU class where stock TensorFlow wheels crash on import, by running both models on LiteRT.
- Built measurement into the system: what the regional mask discards is counted and logged per species, with a report CLI, so mask exceptions are decided from data.

### Distributed systems on unreliable ground
- Designed an MQTT topic contract with documented semantics (retained topics carry state, event topics carry moments), with presence via Last Will and a single module every process imports its topic names from.
- Established fail-loud configuration: required environment variables raise at startup rather than defaulting, so a misconfigured service cannot look healthy while publishing into the void.
- Built "merge is the deploy": a systemd watcher on each box polls its branch, fast-forward pulls, restarts that box's services, and rebuilds the web app only when its code changed; a failed build never restarts the app and a dirty checkout is skipped loudly.
- Runs remote stations on a nightly release channel, promoted by a GitHub Action so no box ever holds a write credential.
- Engineered for boxes nobody can walk over to: a liveness watcher with debounce, a public "front door" redirect so a relative's bookmark survives a DHCP change, capture-stall detection without watchdog threads, and a Wi-Fi watchdog that recovers a half-dead USB adapter without a power cycle.
- Treats two SQLite files as irreplaceable and designs around that: synchronous writes for the bird record, backup-API copies only, and a store move during migration verified by integrity check and row counts on both sides.

### LLM engineering
- Built field notes on the rule "Python computes, the model narrates": a finding engine computes typed findings with effect size and support, an editor ranks them by interest and novelty and decides whether an entry is due, and a validator rejects prose containing any figure it was not given, with retry and a template fallback.
- Runs a provider-agnostic writer chain (a hosted Claude model through an AI gateway, falling back to a local Ollama model) with per-box spend caps, and keeps every figure behind each note stored for audit.
- Wrote an LLM-narrated weather segment with sentence-complete trimming and transition-only error logging for a model host that is off most of the time.

### Full-stack web
- Built the web app in Next.js (App Router), TypeScript and Tailwind: a live detection view over WebSocket MQTT, a life list, per-species pages with recordings and charts, an event archive, a region-mask view, and a weather tab, reading SQLite directly through route handlers.
- Designed for non-technical family users on tablets and touchscreen laptops, with one reusable announcement strip and a registry of everything allowed to speak through it.
- Python service layer with SQLite stores, idempotent schema upgrades at connect, and an enrichment framework where every batch transformation is a worklist-driven, re-runnable, per-entity pass.

### Linux operations
- Operates Ubuntu boxes at three sites over a Tailscale mesh in which remote stations are tagged and can initiate nothing toward the house.
- systemd units per service, journald conventions, Caddy reverse proxies, per-box Mosquitto brokers with a read-only anonymous ACL on the LAN-facing listener.
- Maintains per-machine runbooks in a separate infrastructure repository, updated when the box matches them.

### Migration and repository engineering
- Split the project out of a private monorepo into a public repository with `git filter-repo`, preserving authors, dates and messages, and excluding out-of-scope code from history entirely.
- Kept issue and PR numbers faithful across the move (issues transferred at their original numbers, placeholders consumed for the rest) so that roughly 900 in-code references and 570 commit subjects still resolve.
- Ran a privacy pass over the full history and every issue before the repository went public (no coordinates finer than city level, no credentials).
- Cut three production boxes over to the new repository one at a time, each with a written rollback, moving the stores by same-filesystem rename and checking their integrity before anything restarted.

### Engineering process
- Issue-driven development end to end: every change is traceable from its issue through its contract to a merged PR.
- Living documentation as part of every PR: a hub-and-spoke technical guide in the repo and runbooks beside it, which coding agents read before touching code and update in the same commit set.
- Selective, high-value testing: pure logic (gating, ranking, parsing, shaping) has pytest and Vitest coverage, about 800 Python tests in CI; I/O seams are desk-checked on a box and the PR says so.

## Skills in development

- **The community layer ("the Flyway"):** a derived hub store and per-station digests so the yards can be compared, designed so every connection is opened by the house and stations still initiate nothing.
- **Reading birds against climate:** a reanalysis baseline and season indices for the field notes.
- **Applied embeddings:** audio-embedding distance as a false-positive filter, gated on measurement.

## Technology inventory

Python (paho-mqtt, NumPy, Pillow, pytest) · BirdNET · YAMNet · TensorFlow / LiteRT · TypeScript / Next.js / React / Tailwind / Vitest · SQLite · MQTT (Mosquitto) · systemd / Ubuntu · Caddy · Tailscale · Ollama · Claude via Vercel AI Gateway · Vercel (the front door) · GitHub Actions · git filter-repo
