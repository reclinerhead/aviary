# =============================================================================
# project-squirrel -- listener/enrichment_loop.py
#
# The Aviary tends itself (issue #217): one loop service on pearl that runs
# both enrichment worklists, each whenever its dependency is available --
# the network for Wikipedia profiles, some reachable Ollama host for the
# field notes. A lifer lands on the life list and, within one tick, has its
# portrait, description, and (with a model up) its field notes, with no
# human anywhere in the loop.
#
#   python -m listener.enrichment_loop          # the earl-enrichment unit
#
# ONE TICK, TWO PASSES, IN THIS ORDER:
#   1. Profile pass -- drain species_profile.worklist() through the ~daily
#      retry gate. Depends only on the network, so it runs even when no
#      model is up anywhere.
#   2. Field-notes pass -- probe the MERLE_OLLAMA candidates in preference
#      order via /api/tags (liveness + which models the host holds, one
#      cheap call); the first that answers wins; drain a bounded slice of
#      species_analysis.worklist() through its fingerprint gate.
# The order is load-bearing, not cosmetic: rhythm_prompt() feeds the model
# the profile's description, so profile-before-notes means a brand-new bird
# has its background before its first note is written -- same tick.
#
# SCHEDULED BY OPPORTUNITY, NOT BY CLOCK (#206's design): Ollama lives on
# bluejay, a workstation that is off overnight and busy some afternoons. A
# clock-driven job would fire into a window where the model is often absent;
# this loop just notices when it comes back. Bluejay off overnight and
# bluejay back at breakfast are the same code path.
#
# QUIET WHEN IDLE: a tick with nothing to do -- empty worklists, no host
# answering -- logs NOTHING. A loop service rather than a systemd timer for
# exactly this reason (Servers/Pearl.md's #35 journal-spam disease); the
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
#                           workstation someone is sitting at.
# =============================================================================

import json
import os
import time
import urllib.request

from listener import species_analysis, species_profile
from narration.narrator import Ollama, ollama_candidates

TICK_S = 900          # a no-op tick costs one SQL query, so this is plenty
PROBE_TIMEOUT_S = 5   # a host that can't list its models in 5s isn't "up"
DEFAULT_TICK_CAP = 3


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
    usable, and outranks() guarantees an unknown model can never claw back
    a ranked row. An empty host offers nothing."""
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


def drain_analysis(host, port, model, cap, now=None):
    """A bounded slice of the field-notes worklist against one host. The cap
    counts GENERATIONS, not gate-skips -- a tick that finds forty settled
    species and writes nothing spent nothing. 'llm-down' mid-drain stops the
    slice: the host died under us, and hammering a corpse helps nobody."""
    counts = {}
    conn = species_analysis.connect(species_analysis.db_path())
    try:
        if not has_table(conn, "life_list"):
            return counts
        ollama = Ollama(host, port, model)
        todo = species_analysis.worklist(conn, model=model)
        written = 0
        for i, (sci, common, _visits) in enumerate(todo):
            if written >= cap:
                counts["deferred"] = len(todo) - i
                print(f"[enrich] notes: cap ({cap}) reached, "
                      f"{len(todo) - i} deferred to a later tick", flush=True)
                break
            try:
                status, _ = species_analysis.analyze_species(
                    conn, sci, common, ollama=ollama,
                    weather_path=species_analysis.weather_db_path(),
                    now=now, host=f"{host}:{port}")
            except Exception as e:
                print(f"[enrich] notes {common}: FAILED ({e})", flush=True)
                counts["failed"] = counts.get("failed", 0) + 1
                continue
            counts[status] = counts.get(status, 0) + 1
            if status == "written":
                written += 1
                print(f"[enrich] notes {common}: written "
                      f"({model} via {host}:{port})", flush=True)
            elif status == "llm-down":
                print(f"[enrich] notes: {host}:{port} stopped answering "
                      "mid-drain -- deferring the rest", flush=True)
                break
    finally:
        conn.close()
    return counts


def run_analysis_pass(now=None):
    """Probe the candidates in preference order; the first that answers gets
    this tick's slice. No host answering is a no-op that logs nothing --
    bluejay being off IS the designed steady state, not an incident."""
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
        return drain_analysis(host, port, model, cap, now=now)
    return {}


def tick(now=None):
    """One turn of the crank: profiles first (the description a note wants
    background from), then notes. A failed pass is one loud line, never a
    dead service -- the other pass still runs."""
    results = {}
    try:
        results["profile"] = run_profile_pass(now=now)
    except Exception as e:
        print(f"[enrich] profile pass FAILED ({e})", flush=True)
    try:
        results["analysis"] = run_analysis_pass(now=now)
    except Exception as e:
        print(f"[enrich] analysis pass FAILED ({e})", flush=True)
    return results


def main():
    interval = int(
        os.environ.get("MERLE_ENRICH_INTERVAL_S", "").strip() or TICK_S)
    hosts = ", ".join(f"{h}:{p}" for h, p in ollama_candidates())
    print(f"[enrich] loop up -- tick every {interval}s; ollama candidates: "
          f"{hosts or 'none (profile pass only)'}", flush=True)
    while True:
        tick()
        time.sleep(interval)


if __name__ == "__main__":
    main()
