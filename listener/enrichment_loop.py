# =============================================================================
# project-squirrel -- listener/enrichment_loop.py
#
# The Aviary tends itself (issue #217): one loop service -- pearl and every
# station (#312) -- that runs the enrichment worklists, each whenever its
# dependency is available: the network for Wikipedia profiles, numpy-only
# local CPU for clip enhancement, some reachable Ollama host for the field
# notes. A lifer lands on the life list and, within one tick, has its
# portrait, description, an audible recording, and (with a model up) its
# field notes, with no human anywhere in the loop.
#
#   python -m listener.enrichment_loop          # the earl-enrichment unit
#
# ONE TICK, FIVE PASSES, IN THIS ORDER:
#   1. Profile pass -- drain species_profile.worklist() through the
#      30-minute retry gate (#501). Depends only on the network, so it
#      runs even when no model is up anywhere.
#   1b. Thumb pass (#495) -- drain species_profile.thumb_worklist(), bounded
#      per tick: every shelf original without a current -thumb sibling gets
#      the tile-sized one. Purely local, fs-mtime-driven (owner drops
#      included), directly after profiles so a brand-new lifer's portrait
#      gets its tile thumb in the same tick. Lazy Pillow, the clip pass's
#      numpy shape: a box without it logs one line and the rest still runs.
#   2. Clip pass (#325) -- drain clip_enhance.worklist(), bounded per tick:
#      every original without a current -enh sibling gets one. Purely
#      local, so it always runs; automatic on both roles because pearl's
#      ENH toggle spent weeks silently falling back to raw, and a station's
#      passive-mic clips are born ~20 dB quieter than camera-AGC audio.
#   3. Vocal-fill pass (#447 Phase 0 / #442) -- score sightings.vocal_fill
#      for rows still NULL, bounded per tick. Same DSP as the hand
#      clip_vocal_fill CLI. New live visits get a fill without a full-store
#      backfill so composite bestClips (and the new-best announcement) can
#      crown a full song, not only a pre-scored archive. Lazy numpy import
#      like the clip pass -- lean boxes without numpy log once and continue.
#   3b. Showcase pass (#551) -- the three best recordings per species, each
#      a different call. Plans in-process (SQL + stat, no TensorFlow): if
#      pool clips await a signature, spawns `clip_showcase --embed-only`
#      under MERLE_SHOWCASE_PYTHON (the BirdNET venv -- this loop's repo
#      venv has numpy but no encoder), bounded per tick; then re-picks the
#      species whose embedded pool changed, in-process (numpy only). No
#      MERLE_SHOWCASE_PYTHON on the unit: said once per loop life, slot 1
#      is still written (bestClips needs no vector) so the champion is
#      protected from retention, slots 2/3 wait for an encoder.
#   4. Field-journal pass (#614) -- first, with or without a host, purge the
#      notes no gate can reach (#612) and, when the BirdNET venv is set,
#      refresh the regional prior if a lifer or a move made it stale
#      (region_prior.py, a subprocess like the showcase encoder). Then
#      probe the MERLE_OLLAMA candidates in preference order via /api/tags
#      (liveness + which models the host holds, one cheap call); the first
#      that answers gets this tick's slice: the stats rows the #217
#      worklist + fingerprint gate let through (free -- no model, so they
#      never count against the cap, and a page's figures are there when
#      its first entry lands, #626), then news entries (anomaly, arrival,
#      departure, record), then each bird's first entry, then routine
#      entries (every 28 days, #638) -- one MERLE_ENRICH_CAP across the
#      entries.
# The order is load-bearing, not cosmetic: the entry prompt feeds the
# model the profile's description, so profile-before-notes means
# a brand-new bird has its background before its first note is written --
# same tick; and
# clips-before-notes keeps audible evidence from waiting behind an LLM that
# can take minutes per species. Fill rides after enhance (both need numpy)
# and before notes so the metric is local-only and never blocked on Ollama.
#
# SCHEDULED BY OPPORTUNITY, NOT BY CLOCK (#206's design): Ollama lives on
# bluejay, a workstation that is off overnight and busy some afternoons. A
# clock-driven job would fire into a window where the model is often absent;
# this loop just notices when it comes back. Bluejay off overnight and
# bluejay back at breakfast are the same code path.
#
# QUIET WHEN IDLE: a tick with nothing to do -- empty worklists, no host
# answering -- logs NOTHING. A loop service rather than a systemd timer for
# exactly this reason (toddtech-infrastructure/servers/Pearl.md's #35 journal-spam disease); the
# journal reads as a history of actual work.
#
# NOTHING HERE IS PASS LOGIC. The per-species functions belong to the
# passes (the reusable-pass rule -- both CLIs and their --refresh recipes
# keep working unchanged); this module only turns the crank: gate, probe,
# bound, log.
#
# Config (env), on top of what the passes themselves read:
#   MERLE_OLLAMA            comma-separated "host[:port]" candidates, best
#                           first (narrator.ollama_candidates)
#   MERLE_ENRICH_INTERVAL_S seconds between ticks (default 900)
#   MERLE_ENRICH_CAP        field-note generations per tick, per host: one
#                           number for all hosts, or a comma list matching
#                           MERLE_OLLAMA's order (last entry extends); 0
#                           means "never generate on this host". Default 3
#                           -- conservative, because today's only host is a
#                           workstation someone is sitting at. The gateway
#                           writer reads position 0.
#   MERLE_LLM_CHAIN         who writes the notes, in preference order
#                           (#617): "ollama" (default), "gateway", or
#                           "gateway,ollama" -- the house since 10-25
#   AI_GATEWAY_API_KEY      the gateway writer (gateway_client.py) and
#   MERLE_GATEWAY_MODEL     its daily cap, MERLE_GATEWAY_DAILY_CAP (40)
# =============================================================================

import json
import os
import subprocess
import sys
import time
import urllib.request

from listener import species_analysis, species_profile
import gateway_client as gateway
from ollama_client import Ollama, ollama_candidates

TICK_S = 900          # a no-op tick costs one SQL query, so this is plenty
PROBE_TIMEOUT_S = 5   # a host that can't list its models in 5s isn't "up"
DEFAULT_TICK_CAP = 3
CLIP_TICK_CAP = 200   # clips enhanced per tick (#325): bounds the first
                      # drain of a weeks-deep backlog; the worklist is
                      # idempotent, so what defers this tick lands next
FILL_TICK_CAP = 200   # vocal_fill scores per tick (#447 Phase 0): same
                      # bound shape as the enhance pass
SHOWCASE_TICK_CAP = 200   # clips embedded per tick (#551): ~10 s on orchid;
                          # a whole-store backfill is the hand CLI's job
SHOWCASE_TIMEOUT_S = 900  # the encode subprocess: model load + 200 clips
                          # is well under a minute; a hang must not eat
                          # the loop
_showcase_warned = False  # "no encoder on this unit", once per loop life
THUMB_TICK_CAP = 200  # thumbs per tick (#495): a whole-shelf backfill fits
                      # one tick at today's life-list size; the bound is for
                      # a future --refresh-all landing the lot at once

# --- Pure shaping (test_listener_enrichment_loop.py) --------------------------

def parse_tags(d):
    """An /api/tags response -> the model names the host holds, [] for any
    shape that isn't one."""
    models = (d or {}).get("models") or []
    return [m.get("name") for m in models
            if isinstance(m, dict) and m.get("name")]


def pick_model(available, rank=None):
    """The best-ranked model this host holds (species_analysis.MODEL_RANK
    order). A host holding only unranked models offers its first tag --
    usable; since #626 nothing is ever re-narrated, so the rank is a
    preference, not a guard. An empty host offers nothing."""
    if not available:
        return None
    order = species_analysis.model_rank_list() if rank is None else list(rank)
    for name in order:
        if name in available:
            return name
    return available[0]


def tick_cap(raw, index, default=DEFAULT_TICK_CAP):
    """Per-host generation cap from MERLE_ENRICH_CAP: "20,2" by position
    (the last entry extends to hosts past the list's end), a single number
    for every host, 0 = never generate on this host. Empty or garbage falls
    back to the conservative default rather than an accidental free-for-all.
    """
    entries = [e.strip() for e in (raw or "").split(",") if e.strip()]
    if not entries:
        return default
    pick = entries[index] if index < len(entries) else entries[-1]
    try:
        n = int(pick)
    except ValueError:
        return default
    return n if n >= 0 else default


def has_table(conn, name):
    """A bare dev checkout's earl.db has no life_list; that's a quiet empty
    state (the read routes' posture), never a crash-loop in the journal."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (name,)).fetchone() is not None


# --- The wire -----------------------------------------------------------------

def probe(host, port, timeout=PROBE_TIMEOUT_S):
    """One cheap GET answering "alive, holding which models". None means
    down -- which for bluejay is a NORMAL state, not an error, so callers
    log nothing about it."""
    url = f"http://{host}:{port}/api/tags"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return parse_tags(json.loads(r.read().decode("utf-8")))
    except Exception:
        return None


# --- The two passes, cranked --------------------------------------------------

def run_profile_pass(now=None):
    """Drain the profile worklist through the retry gate. Every attempt is
    stamped, success and failure alike -- a species that gained a row leaves
    the worklist anyway, so the stamp only ever spaces out retries of
    species Wikipedia had nothing (or an error) for."""
    now = now if now is not None else time.time()
    counts = {}
    conn = species_profile.connect(species_profile.db_path())
    try:
        if not has_table(conn, "life_list"):
            return counts
        attempts = species_profile.attempts_map(conn)
        todo = [(sci, common)
                for sci, common in species_profile.worklist(conn)
                if species_profile.attempt_due(attempts.get(sci), now)]
        media = species_profile.clips_dir()
        for i, (sci, common) in enumerate(todo):
            if i:
                time.sleep(species_profile.THROTTLE_S)
            try:
                status = species_profile.enrich_species(conn, media, sci)
            except Exception as e:
                print(f"[enrich] profile {common}: FAILED ({e})", flush=True)
                counts["failed"] = counts.get("failed", 0) + 1
                species_profile.record_attempt(conn, sci, now)
                continue
            counts[status] = counts.get(status, 0) + 1
            species_profile.record_attempt(conn, sci, now)
            print(f"[enrich] profile {common}: {status}", flush=True)
    finally:
        conn.close()
    return counts


def run_thumb_pass(cap=None):
    """Drain the thumbnail worklist (#495): every shelf original without a
    current -thumb sibling gets the tile-sized one, via the same per-file
    function the --thumbs CLI calls (the reusable-pass rule). Purely local
    and fs-mtime-driven, so owner photos dropped onto the shelf by hand are
    covered too -- the original is read-only input, never touched.

    The import probe below is the lazy-dependency guard, one level up from
    write_thumb's own lazy import: without it a Pillow-less box would log
    one ImportError PER FILE per tick instead of one loud pass-level line
    through tick()'s catch."""
    from PIL import Image  # noqa: F401 -- probe: fail the PASS once, not per file

    cap = THUMB_TICK_CAP if cap is None else cap
    media = species_profile.clips_dir()
    todo = species_profile.thumb_worklist(media)
    counts = {}
    if len(todo) > cap:
        counts["deferred"] = len(todo) - cap   # no silent caps; next tick
        todo = todo[:cap]
    for name in todo:
        try:
            status = species_profile.write_thumb(media, name)
        except Exception as e:
            print(f"[enrich] thumb {name}: FAILED ({e}) -- staying on the "
                  "worklist", flush=True)
            counts["failed"] = counts.get("failed", 0) + 1
            continue
        counts[status] = counts.get(status, 0) + 1
    if counts:
        print("[enrich] thumbs: " + ", ".join(
            f"{v} {k}" for k, v in sorted(counts.items())), flush=True)
    return counts


def run_clip_pass(cap=None):
    """Drain the clip-enhancement worklist (#325): every original without a
    current -enh sibling gets one, via the same per-clip function the CLI
    calls (the reusable-pass rule -- --refresh recipes keep working, nothing
    drifts). Automatic on BOTH roles since #325: on pearl the ENH toggle had
    quietly fallen back to raw for weeks of un-passed clips, and a station's
    clips are born ~20 dB quieter than camera-AGC audio -- the normalize step
    is what makes them audible at all.

    The import is lazy on purpose (the earl.py heavy-imports precedent):
    clip_enhance needs numpy at module top, and this loop must stay
    importable -- and the other two passes runnable -- on a box without it.
    A numpy-less box logs the failure once per tick via tick()'s guard."""
    from listener import clip_enhance
    cap = CLIP_TICK_CAP if cap is None else cap   # read at call time, not def
    media = clip_enhance.clips_dir()
    todo = clip_enhance.worklist(media)
    counts = {}
    if len(todo) > cap:
        counts["deferred"] = len(todo) - cap   # no silent caps; next tick
        todo = todo[:cap]
    for relpath in todo:
        try:
            status = clip_enhance.enhance_clip(media, relpath)
        except Exception as e:
            print(f"[enrich] clip {relpath}: FAILED ({e}) -- staying on the "
                  "worklist", flush=True)
            counts["failed"] = counts.get("failed", 0) + 1
            continue
        counts[status] = counts.get(status, 0) + 1
    if counts:
        print("[enrich] clips: " + ", ".join(
            f"{v} {k}" for k, v in sorted(counts.items())), flush=True)
    return counts


def run_vocal_fill_pass(cap=None):
    """Drain unscored visit clips for vocal_fill (#447 Phase 0 / #442).

    New detections land with vocal_fill NULL; without this pass a full
    backfill would leave every *future* visit unscored, and composite
    bestClips would rank them as fill -1 so they rarely crown. Same
    per-row function as `python -m listener.clip_vocal_fill`. Lazy import
    of clip_vocal_fill (numpy) so a lean box without it still runs the
    other passes -- one loud line, not a dead loop."""
    from listener import clip_vocal_fill as vf
    from listener import sightings

    cap = FILL_TICK_CAP if cap is None else cap
    media = vf.clips_dir()
    counts = {}
    conn = sightings.connect(sightings.db_path())
    try:
        if not has_table(conn, "sightings"):
            return counts
        todo = vf.worklist(conn)
        if len(todo) > cap:
            counts["deferred"] = len(todo) - cap
            todo = todo[:cap]
        for sid, relpath in todo:
            try:
                status = vf.score_row(conn, media, sid, relpath)
            except Exception as e:
                print(f"[enrich] vocal-fill id={sid} {relpath}: FAILED ({e}) "
                      "-- staying on the worklist", flush=True)
                counts["failed"] = counts.get("failed", 0) + 1
                continue
            counts[status] = counts.get(status, 0) + 1
        if counts:
            conn.commit()
            print("[enrich] vocal-fill: " + ", ".join(
                f"{v} {k}" for k, v in sorted(counts.items())), flush=True)
    finally:
        conn.close()
    return counts


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_showcase_pass(cap=None, now=None):
    """The species showcase (#551): plan in-process, encode in the BirdNET
    venv, pick in-process. Same per-species functions as the hand CLI (the
    reusable-pass rule). Lazy import of clip_showcase (numpy) so a lean box
    still runs the other passes -- one loud line, not a dead loop.

    Quiet when idle: a tick with nothing awaiting an encoder and no species
    whose pool changed does no work and logs nothing. The encode step is a
    SUBPROCESS because TensorFlow lives in ~/earl-venv, not here, and
    because a model that loads per call must not become a permanent
    gigabyte in a loop that mostly sleeps."""
    global _showcase_warned
    from listener import clip_showcase as sc

    cap = SHOWCASE_TICK_CAP if cap is None else cap
    media = sc.clips_dir()
    counts = {}
    conn = sc.connect(sc.db_path())
    try:
        if not has_table(conn, "sightings"):
            return counts
        n_todo, stale = sc.plan(conn, media)
    finally:
        conn.close()
    if not n_todo and not stale:
        return counts

    python = os.environ.get("MERLE_SHOWCASE_PYTHON", "").strip()
    if n_todo and python:
        cmd = [python, "-m", "listener.clip_showcase", "--embed-only",
               "--limit", str(cap)]
        try:
            proc = subprocess.run(cmd, cwd=repo_root(), capture_output=True,
                                  text=True, timeout=SHOWCASE_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"[enrich] showcase: encoder FAILED to run ({e}) -- "
                  f"{n_todo} clips stay on the worklist", flush=True)
            counts["encoder-failed"] = 1
            proc = None
        if proc is not None:
            for line in proc.stdout.splitlines():
                if line.startswith("[showcase]"):
                    print(f"[enrich] {line[len('[showcase] '):]}", flush=True)
            if proc.returncode != 0:
                tail = proc.stderr.strip().splitlines()[-1:] or ["no stderr"]
                print(f"[enrich] showcase: encoder exited {proc.returncode} "
                      f"({tail[0]}) -- clips stay on the worklist", flush=True)
                counts["encoder-failed"] = 1
            else:
                counts["embedded"] = min(n_todo, cap)
                if n_todo > cap:
                    counts["deferred"] = n_todo - cap   # no silent caps
    elif n_todo and not _showcase_warned:
        print(f"[enrich] showcase: MERLE_SHOWCASE_PYTHON not set on this "
              f"unit -- {n_todo} clips await an encoder; slot 1 only",
              flush=True)
        _showcase_warned = True

    conn = sc.connect(sc.db_path())
    try:
        picks = sc.pick_pass(conn, media, now=now)
    finally:
        conn.close()
    for k, v in picks.items():
        if k != "unchanged":
            counts[k] = v
    return counts


def drain_analysis(ollama, where, model, cap, now=None):
    """A bounded slice of the field-notes worklist against one writer
    (`ollama` is any client with the narrator's complete(); `where` its
    provenance host). The cap counts GENERATIONS, not gate-skips -- a tick
    that finds forty settled species and writes nothing spent nothing.
    'llm-down' mid-drain stops the slice: the writer died under us, and
    hammering a corpse helps nobody."""
    counts = {}
    conn = species_analysis.connect(species_analysis.db_path())
    try:
        if not has_table(conn, "life_list"):
            return counts
        now = now if now is not None else time.time()
        # One context for the whole slice: the weather archive, places,
        # Home and the fair microphones are read once, not per bird.
        ctx = species_analysis.journal_context(
            conn, species_analysis.weather_db_path(), now)
        entries = species_analysis.journal_worklist(conn, ctx)
        ahead = species_analysis.AHEAD_OF_ROUTINE
        # The stats rows go first and cost nothing (#626: no model), so the
        # cap counts entries alone -- a tick that re-stores a hundred rows
        # on a version bump still writes its full slice of entries.
        queue = ([("stats", s) for s in species_analysis.worklist(conn)]
                 + [("entry", e) for e in entries if e[2][0] in ahead]
                 + [("entry", e) for e in entries if e[2][0] not in ahead])
        written = 0
        for i, (what, item) in enumerate(queue):
            if what == "entry" and written >= cap:
                counts["deferred"] = len(queue) - i
                print(f"[enrich] notes: cap ({cap}) reached, "
                      f"{len(queue) - i} deferred to a later tick", flush=True)
                break
            sci, common = item[0], item[1]
            try:
                if what == "stats":
                    status, _ = species_analysis.analyze_species(
                        conn, sci, common, ollama=ollama, weather_path=None,
                        now=now, host=where, ctx=ctx)
                    counts[f"stats {status}"] = \
                        counts.get(f"stats {status}", 0) + 1
                    continue
                else:
                    status, trigger = species_analysis.write_entry(
                        conn, sci, common, ollama=ollama, ctx=ctx,
                        host=where, due=item[2])
                    label = f"entry ({trigger})"
            except Exception as e:
                print(f"[enrich] notes {common} {what}: FAILED ({e})",
                      flush=True)
                counts["failed"] = counts.get("failed", 0) + 1
                continue
            counts[status] = counts.get(status, 0) + 1
            if status == "written":
                written += 1
                print(f"[enrich] notes {common} {label}: written "
                      f"({model} via {where})", flush=True)
            elif status == "llm-down":
                print(f"[enrich] notes: {where} stopped answering "
                      "mid-drain -- deferring the rest", flush=True)
                break
    finally:
        conn.close()
    return counts


PRIOR_TIMEOUT_S = 300     # model load + 48 predictions: ~3 s on orchid
_prior_warned = False     # "no BirdNET venv for the prior", once per life


def run_prior():
    """Refresh the regional prior (#614, listener/region_prior.py) when a
    lifer or a move made it stale -- before the notes, so a departure
    written this tick can cite it. The geo model lives in the BirdNET venv
    (MERLE_SHOWCASE_PYTHON, the showcase encoder's), so this spawns the
    module there; without that venv the journal simply makes own-record
    claims, said once per loop life. Quiet when nothing is due."""
    global _prior_warned
    from listener import region_prior
    path = species_analysis.db_path()
    if not os.path.exists(path):
        return {}
    conn = species_analysis.connect(path)
    try:
        if not has_table(conn, "life_list"):
            return {}
        conn.executescript(region_prior.SCHEMA)
        due = region_prior.prior_due(conn)
    finally:
        conn.close()
    if not due:
        return {}
    python = os.environ.get("MERLE_SHOWCASE_PYTHON", "").strip()
    if not python:
        if not _prior_warned:
            print("[enrich] prior: MERLE_SHOWCASE_PYTHON not set on this unit "
                  "-- the journal makes own-record claims only", flush=True)
            _prior_warned = True
        return {}
    try:
        proc = subprocess.run([python, "-m", "listener.region_prior"],
                              cwd=repo_root(), capture_output=True, text=True,
                              timeout=PRIOR_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[enrich] prior: FAILED to run ({e})", flush=True)
        return {"failed": 1}
    for line in proc.stdout.splitlines():
        if line.startswith("[prior]"):
            # Keep the word: "[enrich] prior: 118 species x 48 weeks ..."
            # is what a journal grep for the prior finds.
            print(f"[enrich] prior: {line[len('[prior] '):]}", flush=True)
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-1:] or ["no stderr"]
        print(f"[enrich] prior: exited {proc.returncode} ({tail[0]})",
              flush=True)
        return {"failed": 1}
    return {"computed": 1}


def run_purge():
    """Delete the notes no gate can reach (#612, species_analysis.
    purge_stale) -- before any host probe, because it needs no model. Quiet
    when there is nothing to purge, and a store that doesn't exist yet is
    never created here (a bare checkout stays bare)."""
    path = species_analysis.db_path()
    if not os.path.exists(path):
        return []
    conn = species_analysis.connect(path)
    try:
        if not has_table(conn, "sightings"):
            return []
        purged = species_analysis.purge_stale(conn)
    finally:
        conn.close()
    for sci in purged:
        print(f"[enrich] notes {sci}: purged (old prompt version, under the "
              "visit floor -- the page shows no notes until it earns them)",
              flush=True)
    return purged


def run_analysis_pass(now=None):
    """Probe the candidates in preference order; the first that answers gets
    this tick's slice. No host answering is a no-op that logs nothing --
    bluejay being off IS the designed steady state, not an incident."""
    purged = run_purge()
    prior = run_prior()
    counts = drain_first_writer(now)
    if purged:
        counts["purged"] = len(purged)
    if prior:
        counts["prior"] = next(iter(prior))
    return counts


_chain_warned = None      # the MERLE_LLM_CHAIN complaint said, once per value
_gateway_warned = False   # "gateway named, no key", once per loop life


def drain_first_writer(now=None):
    """The tick's slice goes to the first writer in MERLE_LLM_CHAIN that is
    available (#617): the gateway when its key is set and the box is under
    its daily cap, Ollama when a candidate answers its probe. A box on the
    default chain ("ollama") never builds the gateway writer, whatever its
    environment holds -- the no-surprise-bill guarantee of #632, now pinned
    to the chain rather than to the loop."""
    global _chain_warned
    now = now if now is not None else time.time()
    try:
        chain = species_analysis.llm_chain()
    except ValueError as e:
        if _chain_warned != str(e):
            _chain_warned = str(e)
            print(f"[enrich] notes: {e} -- no writer this tick", flush=True)
        return {}
    for name in chain:
        if name == "gateway":
            counts = drain_gateway(now)
            if counts is not None:
                return counts
            continue
        for i, (host, port) in enumerate(ollama_candidates()):
            cap = tick_cap(os.environ.get("MERLE_ENRICH_CAP", ""), i)
            if cap == 0:
                continue
            models = probe(host, port)
            if models is None:
                continue
            model = pick_model(models)
            if not model:
                continue
            return drain_analysis(Ollama(host, port, model),
                                  f"{host}:{port}", model, cap, now=now)
    return {}


def drain_gateway(now):
    """The gateway writer's slice, or None when it cannot write this tick:
    no key (said once per loop life, like the missing encoder), a tick cap
    of 0, or the daily cap reached (one line per tick -- the day's budget
    is news, a missing key is a standing state). The slice is bounded by
    the tick cap AND what is left of the day."""
    global _gateway_warned
    cfg = gateway.gateway_config()
    if cfg is None:
        if not _gateway_warned:
            _gateway_warned = True
            print("[enrich] notes: MERLE_LLM_CHAIN names gateway but "
                  "AI_GATEWAY_API_KEY is not set on this unit -- skipping it",
                  flush=True)
        return None
    cap = tick_cap(os.environ.get("MERLE_ENRICH_CAP", ""), 0)
    if cap == 0:
        return None
    conn = species_analysis.connect(species_analysis.db_path())
    try:
        if not has_table(conn, "life_list"):
            return {}
        today = species_analysis.gateway_entries_today(conn, now)
    finally:
        conn.close()
    daily = species_analysis.gateway_daily_cap()
    if today >= daily:
        print(f"[enrich] notes: the gateway wrote {today} entries today, cap "
              f"{daily} -- not asked again until tomorrow", flush=True)
        return None
    key, model = cfg
    return drain_analysis(gateway.Gateway(key, model), gateway.GATEWAY_HOST,
                          model, min(cap, daily - today), now=now)


def tick(now=None):
    """One turn of the crank: profiles first (the description a note wants
    background from), then thumbs (#495 -- a fresh portrait's tile sibling,
    same tick), then clips (#325), then vocal_fill (#447 Phase 0), then the
    showcase (#551 -- it ranks by the fill the previous pass just wrote and
    encodes the sibling the clip pass just made), then notes. A failed pass is one loud line, never a dead service -- the
    other passes still run."""
    results = {}
    try:
        results["profile"] = run_profile_pass(now=now)
    except Exception as e:
        print(f"[enrich] profile pass FAILED ({e})", flush=True)
    try:
        results["thumbs"] = run_thumb_pass()
    except Exception as e:
        print(f"[enrich] thumb pass FAILED ({e})", flush=True)
    try:
        results["clips"] = run_clip_pass()
    except Exception as e:
        print(f"[enrich] clip pass FAILED ({e})", flush=True)
    try:
        results["vocal_fill"] = run_vocal_fill_pass()
    except Exception as e:
        print(f"[enrich] vocal-fill pass FAILED ({e})", flush=True)
    try:
        results["showcase"] = run_showcase_pass(now=now)
    except Exception as e:
        print(f"[enrich] showcase pass FAILED ({e})", flush=True)
    try:
        results["analysis"] = run_analysis_pass(now=now)
    except Exception as e:
        print(f"[enrich] analysis pass FAILED ({e})", flush=True)
    return results


def main():
    interval = int(
        os.environ.get("MERLE_ENRICH_INTERVAL_S", "").strip() or TICK_S)
    hosts = ", ".join(f"{h}:{p}" for h, p in ollama_candidates())
    try:
        chain = ",".join(species_analysis.llm_chain())
    except ValueError as e:
        chain = f"INVALID ({e})"
    print(f"[enrich] loop up -- tick every {interval}s; writers: {chain}; "
          f"ollama candidates: {hosts or 'none'}", flush=True)
    while True:
        tick()
        time.sleep(interval)


if __name__ == "__main__":
    main()
