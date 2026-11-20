# CLAUDE.md

Notes for coding agents working in this repo. Todd's global instructions (issues as the contract, feature branches, PRs that close an issue) apply; this file adds only what is specific to the Aviary.

## Read first

- [`TechnicalGuide.md`](TechnicalGuide.md) is the hub of the technical guide. Read it, then the spoke under `docs/guide/` for the component you are touching. Update the spoke in the same PR as the code.
- Box runbooks (unit files, installs, traps) live in `reclinerhead/toddtech-infrastructure`, not here.

## Tests

- **Python:** tests are root-level `test_*.py`. Run them with `.venv/bin/python -m pytest <files>`. There is no `pytest.ini`: **a new test file must be added by hand to the `pytest` line in `.github/workflows/tests.yml`, or CI never runs it.**
- **`web/` and `frontdoor/`** are separate pnpm apps with their own lockfiles. In each: `pnpm test`, `pnpm exec tsc --noEmit`, `pnpm lint`. CI does not run `next build`, so run `pnpm build` in `web/` before any PR that touches it.
- Model-, microphone- and broker-dependent code is not in CI. It is desk-checked on orchid after merge, and the PR says so.

## A merge is a deploy

- Merging to `main` deploys to **orchid within a minute** and to the **stations (birdhouse, kestrel) that night**, with nobody at a keyboard there.
- Any merge restarts the Earl units, and a restarted listener is deaf for about 3.5 minutes. Land related changes as one PR.
- A change must keep working beside the previous version of its neighbors: the stations run yesterday's code against today's for up to a day.
- Never rename a `MERLE_*` env var, a bus topic, or a feed name as a side effect. Each is a contract on three boxes or a label in the bird record.

## Never

- **Never put coordinates finer than city level, a LAN address, or a credential in the repo, a commit message, an issue or a PR.** The repo is public.
- **Never run `git clean -x` in a box's `~/aviary`.** `earl.db` and `weather.db` live there, ignored but not protected.
- Never `cp` a live SQLite store; use the backup API (see the runbooks).
- Nothing runs with `sudo` over an agent's SSH session. A step that needs it is a paste block for Todd.

## Station etiquette

Not every station belongs to family. Name stations by their names, and describe cross-station facts as "flyway firsts" or "the flyway life list", never in family terms.
