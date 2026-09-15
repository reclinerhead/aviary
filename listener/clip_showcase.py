# =============================================================================
# project-squirrel -- listener/clip_showcase.py
#
# The species showcase (issue #551): the THREE best recordings of each
# species, each a different call. Slot 1 is the Aviary's best recording
# exactly (the bestClips rule, ported below). Slots 2 and 3 are the fullest,
# most confident clips that sound DIFFERENT from every slot already filled,
# so a cardinal's page can hold its whistled song, its metallic chip and
# whatever else the yard produces -- filled in over time, honestly empty
# until a distinct, good-enough recording exists.
#
#   python -m listener.clip_showcase                 # embed what's missing, re-pick what changed
#   python -m listener.clip_showcase --limit 200     # bounded (the enrichment loop's call)
#   python -m listener.clip_showcase --embed-only --limit 200
#   python -m listener.clip_showcase --refresh "Cardinalis cardinalis"
#   python -m listener.clip_showcase --report        # the listening report, from stored vectors
#   python -m listener.clip_showcase --dry-run
#
# THE SIGNATURE is BirdNET 2.4's own embedding: the 1024-dim penultimate
# layer the classifier already computes, via the `birdnet` package's
# `encode_arrays` (same weights Earl runs, no second model). Encoded input
# is the CENTER 3 s of the clip (prev+event+next -> the detected window),
# from the -enh sibling when it exists (band-limited, denoised: the
# microphone's room mostly washes out -- Phase 0 measured same-mic vs
# cross-mic distance ratios of 1.00-1.08 on four of five species). Distance
# is cosine on L2-normalized vectors; "different" is >= TAU.
#
# THE RULE (select_showcase, pure, pytest-covered):
#   slot 1  = bestClips (mcc/lib/aviary.ts #333/#442), ported verbatim and
#             pinned to fixtures/best_clips.json which both test suites read
#   pool    = the species' top POOL_N clips by fill among conf >= floor
#             (plus the bestClips window set, so slot 1 is always present),
#             embedded, file on disk
#   radius  = the 25th percentile of the pool's pairwise distances --
#             SPECIES-RELATIVE, never a fixed number (a fixed 0.25 flagged
#             36 of 100 woodpecker clips as loners; that species' calls are
#             simply spread wider)
#   clean   = at least NEIGHBOUR_FRACTION of the pool within radius (3 of
#             100 as measured; at least 1 in any pool)
#   slot 2  = first in pool order with dist(slot1) >= TAU and clean
#   slot 3  = first with dist to BOTH >= TAU and clean
#
# WHY THE NEIGHBOUR RULE IS THE CLEANLINESS TEST (Phase 0's finding): a
# chipmunk or an engine under the bird makes a one-off mixture whose vector
# sits alone in the pool, while a genuine second call type recurs and has
# neighbours. A species-label test does NOT work here -- BirdNET has no
# class for a chipmunk, so off-class noise is invisible to labels (verified
# on the chickadee case: the contaminated clip's center segment read as a
# clean 0.84 chickadee). Accepted trade-off: a rare call heard once waits
# until it has been heard a few more times.
#
# SLOTS 2/3 USE AN ABSOLUTE FLOOR, NOT THE cMax WINDOW, on purpose: BirdNET
# is most confident on song and less on chips and alarm calls, so "within
# 0.05 of the species max" would exclude exactly the different calls this
# exists to catch.
#
# TWO TABLES, BOTH OWNED HERE (the species_analysis "this pass owns only its
# own table" rule; sightings stays append-only, the MCC only ever reads):
#   clip_signature    one vector per clip path, keyed on the FILE (a `best`
#                     rewrite bumps mtime -> re-embedded, the clip_enhance
#                     idempotence rule)
#   species_showcase  (species_sci, slot) -> clip + the distances that
#                     justified it; sightings.exempt_clips() reads it so
#                     retention never removes a showcase clip
#
# RUNS UNDER THE BIRDNET VENV. The enrichment loop lives in the repo venv,
# which has numpy but no TensorFlow, so the loop plans in-process (SQL +
# stat, this module's pure half) and spawns `--embed-only` under
# MERLE_SHOWCASE_PYTHON for the encode step; picking is numpy-only and runs
# in-process. `birdnet` is imported lazily, inside the embed function, so
# this module imports clean everywhere numpy does.
#
# Config (env):
#   MERLE_EARL_DB     the bird record (default "earl.db")
#   MERLE_EARL_CLIPS  clip dir (default "clips")
# =============================================================================

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import wave

import numpy as np

from listener import clip_enhance as ce
from listener import gate

DEFAULT_DB_PATH = "earl.db"

# --- The measured constants (Phase 0, 2026-09-15, orchid's live store) ------
TAU = 0.30                  # cosine distance that counts as "a different call"
SHOWCASE_CONF_FLOOR = 0.80  # slots 2/3: absolute, not the cMax window
POOL_N = 100                # top clips by fill per species that compete
WINDOW_EXTRA = 30           # plus the bestClips window set, so slot 1 is present
RADIUS_PERCENTILE = 25      # neighbour radius = this percentile of pairwise dist
NEIGHBOUR_FRACTION = 0.03   # the loner rule: a slot-2/3 pick needs at least
                            # this fraction of the pool within radius (3 at
                            # the measured pool of 100; never below 1). A
                            # fixed count would starve small pools: at p25 a
                            # pool of 12 averages ~2.75 neighbours per clip.
MIN_POOL = 10               # fewer embedded candidates: slot 1 only
SLOTS = 3
MODEL_ID = "birdnet-2.4"
DIM = 1024
SR = gate.SAMPLE_RATE
SEG = gate.WINDOW_S * SR

# The bestClips rule from mcc/lib/aviary.ts (#333, #442), ported verbatim.
BEST_CLIP_CONF_WINDOW = 0.05
BEST_CLIP_CONF_FLOOR = 0.90

SCHEMA = """
CREATE TABLE IF NOT EXISTS clip_signature (
    clip        TEXT PRIMARY KEY,
    sighting_id INTEGER,
    species_sci TEXT NOT NULL,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vec         BLOB NOT NULL,
    src         TEXT NOT NULL,
    src_mtime   REAL NOT NULL,
    computed_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS clip_signature_species
    ON clip_signature(species_sci);
CREATE TABLE IF NOT EXISTS species_showcase (
    species_sci TEXT NOT NULL,
    slot        INTEGER NOT NULL,
    clip        TEXT NOT NULL,
    sighting_id INTEGER,
    confidence  REAL,
    vocal_fill  REAL,
    dist_json   TEXT,
    radius      REAL,
    pool_n      INTEGER,
    pool_fp     TEXT,
    chosen_ts   INTEGER NOT NULL,
    PRIMARY KEY (species_sci, slot)
);
"""


def db_path():
    return os.environ.get("MERLE_EARL_DB", "").strip() or DEFAULT_DB_PATH


def clips_dir():
    return os.environ.get("MERLE_EARL_CLIPS", "").strip() or ce.DEFAULT_CLIPS_DIR


def connect(path):
    """The species_profile.connect() shape: WAL, idempotent schema, this
    pass owning only its own tables."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# --- Pure: distance and the rule ---------------------------------------------

def l2norm(vecs):
    """Rows normalized to unit length; a zero row stays zero (and is then
    honestly unusable -- a distance of exactly 1.0 from everything)."""
    v = np.asarray(vecs, dtype=np.float32)
    if v.ndim == 1:
        v = v[None, :]
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return v / norms


def cosine_dist(a, b):
    a = l2norm(a)[0]
    b = l2norm(b)[0]
    return float(1.0 - np.dot(a, b))


def pairwise(vecs):
    """Full cosine-distance matrix over unit rows; diagonal is 0."""
    v = l2norm(vecs)
    d = 1.0 - v @ v.T
    np.fill_diagonal(d, 0.0)
    return np.clip(d, 0.0, 2.0)


def neighbour_radius(D, percentile=RADIUS_PERCENTILE):
    """The species-relative radius: a percentile of the pool's own pairwise
    distances. Scale-invariant by construction -- the same layout at twice
    the spread yields twice the radius and the same neighbour counts."""
    n = D.shape[0]
    if n < 2:
        return 0.0
    iu = np.triu_indices(n, 1)
    return float(np.percentile(D[iu], percentile))


def _fill(c):
    f = c.get("vocal_fill")
    return -1.0 if f is None else float(f)


def _ident(c):
    return c.get("id", c.get("clip"))


def best_clip(cands, cmax=None):
    """bestClips (mcc/lib/aviary.ts), ported: pure confidence when no
    candidate has a fill (strictly greater wins, equal keeps the earlier
    take); otherwise the max fill among rows inside the confidence window
    (>= cMax - 0.05 or >= 0.90), ties by higher confidence then earlier ts.
    `cmax` may be supplied by the caller when `cands` is a slice of the
    species' rows (the pool), so the window is the species' real one."""
    if not cands:
        return None
    if not any(c.get("vocal_fill") is not None for c in cands):
        best = cands[0]
        for c in cands:
            if c["confidence"] > best["confidence"] or (
                    c["confidence"] == best["confidence"]
                    and c["ts"] < best["ts"]):
                best = c
        return best
    cmax = max(c["confidence"] for c in cands) if cmax is None else cmax
    elig = [c for c in cands
            if c["confidence"] >= cmax - BEST_CLIP_CONF_WINDOW
            or c["confidence"] >= BEST_CLIP_CONF_FLOOR]
    if not elig:
        # Only when a supplied cmax exceeds every candidate (the champion's
        # file is gone): the pool's own window, honestly.
        return best_clip(cands)
    best = elig[0]
    for c in elig:
        f, bf = _fill(c), _fill(best)
        if f > bf:
            best = c
        elif f == bf and (c["confidence"] > best["confidence"] or (
                c["confidence"] == best["confidence"]
                and c["ts"] < best["ts"])):
            best = c
    return best


def pool_order(cands):
    """Slot 2/3 competition order: fullest first, then most confident, then
    the earlier take (the older file has already survived retention)."""
    return sorted(cands, key=lambda c: (-_fill(c), -c["confidence"], c["ts"]))


def min_neighbours(n, fraction=NEIGHBOUR_FRACTION):
    """How many neighbours a slot-2/3 pick needs in a pool of n: the
    measured 3-of-100, scaled, floored at one (heard at least twice)."""
    return max(1, int(np.ceil(fraction * n)))


def select_showcase(cands, vecs, *, cmax=None, tau=TAU, floor=SHOWCASE_CONF_FLOOR,
                    neighbour_fraction=NEIGHBOUR_FRACTION, min_pool=MIN_POOL,
                    radius_percentile=RADIUS_PERCENTILE, slots=SLOTS):
    """The rule. `cands` are dicts (id, clip, confidence, vocal_fill, ts);
    `vecs` is the matching (n, dim) array. Returns
        {"slots": [{"cand", "dists"}...], "radius", "pool_n", "loners"}
    where dists are to the earlier slots and loners lists candidates that
    were far enough but had no neighbours (the listening report's "why not
    this one")."""
    out = {"slots": [], "radius": 0.0, "pool_n": len(cands), "loners": []}
    if not cands:
        return out
    s1 = best_clip(cands, cmax=cmax)
    out["slots"].append({"cand": s1, "dists": []})
    if len(cands) < min_pool:
        return out
    idx = {_ident(c): i for i, c in enumerate(cands)}
    D = pairwise(vecs)
    radius = neighbour_radius(D, radius_percentile)
    out["radius"] = radius
    need = min_neighbours(len(cands), neighbour_fraction)
    nb = (D < radius).sum(axis=1) - 1   # minus self
    chosen = [s1]
    noted = set()   # a loner is reported once, not once per slot attempt
    for _ in range(slots - 1):
        pick = None
        for c in pool_order(cands):
            if c["confidence"] < floor:
                continue
            if any(_ident(c) == _ident(s) for s in chosen):
                continue
            i = idx[_ident(c)]
            dists = [float(D[i, idx[_ident(s)]]) for s in chosen]
            if min(dists) < tau:
                continue
            if nb[i] < need:
                if _ident(c) not in noted:
                    noted.add(_ident(c))
                    out["loners"].append({"cand": c, "dists": dists,
                                          "neighbours": int(nb[i])})
                continue
            pick = (c, dists)
            break
        if pick is None:
            break
        chosen.append(pick[0])
        out["slots"].append({"cand": pick[0], "dists": pick[1]})
    return out


# --- The store side: pools, signatures, fingerprints -------------------------

def pool_species(conn):
    """Every species with at least one clip row: [(sci, common)]."""
    return [(r["species_sci"], r["species_common"]) for r in conn.execute(
        "SELECT species_sci, MAX(species_common) AS species_common"
        " FROM sightings WHERE clip IS NOT NULL AND clip != ''"
        " GROUP BY species_sci ORDER BY species_sci")]


def species_cmax(conn, sci):
    r = conn.execute(
        "SELECT MAX(confidence) FROM sightings WHERE species_sci = ?"
        " AND clip IS NOT NULL AND clip != ''", (sci,)).fetchone()
    return None if r is None else r[0]


def species_pool(conn, sci, *, floor=SHOWCASE_CONF_FLOOR, pool_n=POOL_N,
                 window_extra=WINDOW_EXTRA):
    """The species' competing rows, as dicts: the top `pool_n` by fill among
    conf >= floor, UNION the top `window_extra` by fill inside the bestClips
    confidence window (so the champion is always in the pool -- it is the
    max-fill row of that set by definition). Returns (rows, cmax)."""
    cmax = species_cmax(conn, sci)
    if cmax is None:
        return [], None
    base = ("SELECT id, ts, source, species_sci, confidence, clip, vocal_fill"
            " FROM sightings WHERE species_sci = ? AND clip IS NOT NULL"
            " AND clip != '' AND confidence >= ?"
            " ORDER BY vocal_fill DESC, confidence DESC, ts ASC LIMIT ?")
    rows = {}
    for r in conn.execute(base, (sci, floor, pool_n)):
        rows[r["id"]] = dict(r)
    lo = min(cmax - BEST_CLIP_CONF_WINDOW, BEST_CLIP_CONF_FLOOR)
    for r in conn.execute(base, (sci, lo, window_extra)):
        rows.setdefault(r["id"], dict(r))
    return list(rows.values()), cmax


def source_for(media_dir, relpath):
    """(full path, 'enh'|'raw', mtime) for the file to encode: the enhanced
    sibling when it exists, else the original; None when neither is on disk
    (pruned, or a failed write -- an honest gap, never a candidate)."""
    if not relpath or not relpath.endswith(".wav") or ce.is_enhanced(relpath):
        return None
    for kind, rel in (("enh", ce.enhanced_relpath(relpath)), ("raw", relpath)):
        full = os.path.join(media_dir, rel)
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        return full, kind, mtime
    return None


def load_signatures(conn, sci):
    """{clip: (src, src_mtime, vec)} for one species."""
    out = {}
    for r in conn.execute(
            "SELECT clip, src, src_mtime, vec, dim FROM clip_signature"
            " WHERE species_sci = ?", (sci,)):
        vec = np.frombuffer(r["vec"], dtype=np.float32)
        if vec.size != r["dim"]:
            continue   # a truncated blob is not a signature
        out[r["clip"]] = (r["src"], float(r["src_mtime"]), vec)
    return out


def store_signature(conn, *, clip, sighting_id, sci, vec, src, src_mtime, now):
    v = np.asarray(vec, dtype=np.float32)
    conn.execute(
        "INSERT OR REPLACE INTO clip_signature"
        " (clip, sighting_id, species_sci, model, dim, vec, src, src_mtime,"
        " computed_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (clip, sighting_id, sci, MODEL_ID, int(v.size), v.tobytes(), src,
         float(src_mtime), int(now)))


def plan_species(conn, media_dir, sci):
    """Everything the two arms need for one species, computed once:
        rows      the pool rows whose file is on disk, each with 'src'
        todo      rows still needing a (current) signature
        vecs      {clip: vec} for rows that have one
        fp        fingerprint of the embedded pool (what a pick depends on)
        cmax      the species' real max confidence
    A row is re-embedded when its source file is newer than the stored
    signature or is a different file (raw -> enh sibling landed)."""
    rows, cmax = species_pool(conn, sci)
    sigs = load_signatures(conn, sci)
    present, todo, vecs = [], [], {}
    for r in rows:
        src = source_for(media_dir, r["clip"])
        if src is None:
            continue
        full, kind, mtime = src
        r = dict(r, src_full=full, src_kind=kind, src_mtime=mtime)
        present.append(r)
        sig = sigs.get(r["clip"])
        if sig is None or sig[0] != kind or sig[1] < mtime:
            todo.append(r)
        elif sig is not None:
            vecs[r["clip"]] = sig[2]
    parts = sorted(f"{r['clip']}|{r['confidence']}|{r['vocal_fill']}|"
                   f"{sigs[r['clip']][1]}" for r in present
                   if r["clip"] in vecs)
    fp = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    return {"rows": present, "todo": todo, "vecs": vecs, "fp": fp,
            "cmax": cmax}


def stored_fingerprint(conn, sci):
    r = conn.execute(
        "SELECT pool_fp FROM species_showcase WHERE species_sci = ?"
        " AND slot = 1", (sci,)).fetchone()
    return None if r is None else r["pool_fp"]


def stored_slots(conn, sci):
    return {r["slot"]: r["clip"] for r in conn.execute(
        "SELECT slot, clip FROM species_showcase WHERE species_sci = ?",
        (sci,))}


def plan(conn, media_dir):
    """The loop's pre-check, in-process and TensorFlow-free: how many clips
    wait for an encoder, and which species need a re-pick. Returns
    (n_todo, stale_species)."""
    n_todo, stale = 0, []
    for sci, common in pool_species(conn):
        p = plan_species(conn, media_dir, sci)
        n_todo += len(p["todo"])
        if p["rows"] and p["fp"] != stored_fingerprint(conn, sci):
            stale.append((sci, common))
    return n_todo, stale


# --- The encoder arm (BirdNET venv) ------------------------------------------

def read_center(full):
    """The clip's center 3 s as float32 -- prev+event+next makes that the
    detected window; a short clip is padded, a longer one takes the middle
    whole segment. Raises on a file we did not write (not mono s16le)."""
    with wave.open(full, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != gate.BYTES_PER_SAMPLE:
            raise ValueError(f"{full}: not mono s16le")
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    if sr != SR:
        raise ValueError(f"{full}: sample rate {sr}")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if x.size < SEG:
        return np.pad(x, (0, SEG - x.size))
    n = x.size // SEG
    i = n // 2
    return x[i * SEG:(i + 1) * SEG]


def load_model():
    """Lazy: TensorFlow only enters the process that encodes. Honors the
    same MERLE_BIRDNET_LIBRARY knob as earl.py (LiteRT on no-AVX stations,
    verified to expose the encoder in Phase 0)."""
    import birdnet
    from listener.earl import birdnet_load_kwargs
    return birdnet.load("acoustic", "2.4", "tf", **birdnet_load_kwargs())


def embed_segments(model, segments, batch_size=16):
    """(n, 3 s float32) -> (n, DIM) unit vectors. ONE encode_arrays call:
    each call spins up a session (and forks workers), so per-clip calls
    would pay seconds each."""
    inp = [(np.asarray(s, dtype=np.float32), SR) for s in segments]
    result = model.encode_arrays(inp, batch_size=batch_size)
    return l2norm(np.asarray(result.embeddings[:, 0, :], dtype=np.float32))


def embed_rows(conn, rows, model, now=None, log=print):
    """Encode the given planned rows and store their signatures. Returns
    the count written; unreadable files are logged and skipped (they stay
    on the worklist -- a loud line per tick is the right amount of noise
    for a file we could not read)."""
    now = time.time() if now is None else now
    segs, keep = [], []
    for r in rows:
        try:
            segs.append(read_center(r["src_full"]))
        except (OSError, ValueError, wave.Error) as e:
            log(f"[showcase] {r['clip']}: unreadable ({e})")
            continue
        keep.append(r)
    if not keep:
        return 0
    vecs = embed_segments(model, segs)
    written = 0
    for r, v in zip(keep, vecs):
        if not np.any(v):
            log(f"[showcase] {r['clip']}: encoder returned nothing")
            continue
        store_signature(conn, clip=r["clip"], sighting_id=r["id"],
                        sci=r["species_sci"], vec=v, src=r["src_kind"],
                        src_mtime=r["src_mtime"], now=now)
        written += 1
    conn.commit()
    return written


def embed_pass(conn, media_dir, *, limit=None, only_sci=None, force=False,
               model=None, log=print):
    """Embed every pool row that lacks a current signature, bounded.
    Returns counts. The model loads only if there is work."""
    todo = []
    for sci, _common in pool_species(conn):
        if only_sci is not None and sci != only_sci:
            continue
        p = plan_species(conn, media_dir, sci)
        todo.extend(p["rows"] if force else p["todo"])
    counts = {}
    if not todo:
        return counts
    if limit is not None and len(todo) > limit:
        counts["deferred"] = len(todo) - limit
        todo = todo[:limit]
    if model is None:
        model = load_model()
    counts["embedded"] = embed_rows(conn, todo, model, log=log)
    counts["unreadable"] = len(todo) - counts["embedded"]
    if not counts["unreadable"]:
        del counts["unreadable"]
    return counts


# --- The pick arm (numpy only) ------------------------------------------------

def apply_showcase(conn, sci, result, fp, now):
    conn.execute("DELETE FROM species_showcase WHERE species_sci = ?", (sci,))
    for slot, s in enumerate(result["slots"], 1):
        c = s["cand"]
        conn.execute(
            "INSERT INTO species_showcase (species_sci, slot, clip,"
            " sighting_id, confidence, vocal_fill, dist_json, radius, pool_n,"
            " pool_fp, chosen_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sci, slot, c["clip"], c.get("id"), c["confidence"],
             c.get("vocal_fill"), json.dumps([round(d, 4) for d in s["dists"]]),
             result["radius"], result["pool_n"], fp, int(now)))


def pick_species(conn, media_dir, sci, common, *, now=None, force=False,
                 log=print, planned=None):
    """Re-pick one species when its embedded pool changed. Returns a status
    word: 'unchanged' (fingerprint matched, nothing written), 'picked'
    (rows written; every slot change logged out loud), 'empty' (no clip
    on disk at all)."""
    now = time.time() if now is None else now
    p = planned if planned is not None else plan_species(conn, media_dir, sci)
    if not p["rows"]:
        return "empty"
    if not force and p["fp"] == stored_fingerprint(conn, sci):
        return "unchanged"
    cands = [r for r in p["rows"] if r["clip"] in p["vecs"]]
    if not cands:
        # Nothing embedded yet: slot 1 still stands on its own (bestClips
        # needs no vector), so the page's champion is protected from day one.
        cands = list(p["rows"])
        vecs = np.zeros((len(cands), 1), dtype=np.float32)
        result = select_showcase(cands, vecs, cmax=p["cmax"], min_pool=10 ** 9)
    else:
        vecs = np.stack([p["vecs"][c["clip"]] for c in cands])
        result = select_showcase(cands, vecs, cmax=p["cmax"])
    before = stored_slots(conn, sci)
    apply_showcase(conn, sci, result, p["fp"], now)
    conn.commit()
    after = {i + 1: s["cand"]["clip"] for i, s in enumerate(result["slots"])}
    for slot in range(1, SLOTS + 1):
        old, new = before.get(slot), after.get(slot)
        if old == new:
            continue
        if new is None:
            log(f"[showcase] {common}: slot {slot} emptied (was {old})")
        else:
            d = result["slots"][slot - 1]["dists"]
            dist = ", ".join(f"d{j + 1}={x:.2f}" for j, x in enumerate(d))
            log(f"[showcase] {common}: slot {slot} -> {new}"
                f"{' (' + dist + ')' if dist else ''}")
    return "picked"


def pick_pass(conn, media_dir, *, only_sci=None, force=False, now=None,
              log=print):
    counts = {}
    for sci, common in pool_species(conn):
        if only_sci is not None and sci != only_sci:
            continue
        status = pick_species(conn, media_dir, sci, common, now=now,
                              force=force, log=log)
        counts[status] = counts.get(status, 0) + 1
    return counts


# --- The listening report -------------------------------------------------------

def report(conn, media_dir, only_sci=None, out=print):
    """What is on the shelf and why, from stored vectors: each slot with
    its distances, and the far-enough candidates the loner rule declined."""
    for sci, common in pool_species(conn):
        if only_sci is not None and sci != only_sci:
            continue
        p = plan_species(conn, media_dir, sci)
        cands = [r for r in p["rows"] if r["clip"] in p["vecs"]]
        if not cands:
            out(f"\n## {common}: nothing embedded yet")
            continue
        vecs = np.stack([p["vecs"][c["clip"]] for c in cands])
        res = select_showcase(cands, vecs, cmax=p["cmax"])
        out(f"\n## {common}  (pool {res['pool_n']} embedded, radius "
            f"{res['radius']:.3f}, {len(p['todo'])} awaiting encoder)")
        for i, s in enumerate(res["slots"], 1):
            c = s["cand"]
            d = " ".join(f"d{j + 1}={x:.3f}" for j, x in enumerate(s["dists"]))
            fill = "null" if c.get("vocal_fill") is None else f"{c['vocal_fill']:.2f}"
            out(f"  slot {i}: conf {c['confidence']:.2f} fill {fill} "
                f"{c.get('source', '')} {d}\n          /clips/{c['clip']}")
        for l in res["loners"][:3]:
            c = l["cand"]
            out(f"  loner (far enough, {l['neighbours']} neighbours): conf "
                f"{c['confidence']:.2f}\n          /clips/{c['clip']}")


# --- CLI ------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="The species showcase: three best recordings, each a "
                    "different call (#551)")
    ap.add_argument("--db", default=None)
    ap.add_argument("--clips", default=None)
    ap.add_argument("--limit", type=int, default=None,
                    help="embed at most N clips this run")
    ap.add_argument("--refresh", metavar="SPECIES_SCI",
                    help="re-embed and re-pick this species only")
    ap.add_argument("--force", action="store_true",
                    help="re-embed every pool clip and re-pick everything")
    ap.add_argument("--embed-only", action="store_true",
                    help="encode step only (the enrichment loop's call)")
    ap.add_argument("--pick-only", action="store_true",
                    help="selection only, no encoder (numpy venv is enough)")
    ap.add_argument("--report", action="store_true",
                    help="print the listening report and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="say what would be embedded / re-picked; write nothing")
    args = ap.parse_args(argv)

    db = args.db or db_path()
    media = args.clips or clips_dir()
    conn = connect(db)
    try:
        if args.report:
            report(conn, media, only_sci=args.refresh)
            return 0
        if args.dry_run:
            n_todo, stale = plan(conn, media)
            print(f"[showcase] {n_todo} clips would be embedded; "
                  f"{len(stale)} species would be re-picked"
                  + (": " + ", ".join(c for _, c in stale) if stale else ""),
                  flush=True)
            return 0
        counts = {}
        if not args.pick_only:
            counts.update(embed_pass(conn, media, limit=args.limit,
                                     only_sci=args.refresh, force=args.force))
        if not args.embed_only:
            picks = pick_pass(conn, media, only_sci=args.refresh,
                              force=bool(args.refresh or args.force))
            counts.update({f"species {k}": v for k, v in picks.items()})
        if counts:
            print("[showcase] done: " + ", ".join(
                f"{k}={v}" for k, v in sorted(counts.items())), flush=True)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
