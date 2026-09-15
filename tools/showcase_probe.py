# =============================================================================
# project-squirrel -- tools/showcase_probe.py
#
# Phase 0 of issue #551 (species showcase: three best recordings per species,
# each a different call). A ONE-SHOT MEASUREMENT, not a service: does
# BirdNET 2.4's embedding separate one species' calls from each other, and
# does it separate them by CALL rather than by MICROPHONE?
#
# Writes nothing to earl.db. Reads the store and the clips, caches the
# vectors to an .npz so threshold re-runs are instant, prints a report.
#
#   cd ~/project-squirrel
#   MERLE_EARL_DB=... MERLE_EARL_CLIPS=... ~/earl-venv/bin/python \
#       tools/showcase_probe.py --out ~/showcase-probe
#   ... --taus 0.15,0.25,0.35 --report-only     # re-slice the cache
#
# What it prints, per species:
#   1. the candidate pool (how many clips, which mics)
#   2. pairwise cosine-distance percentiles
#   3. same-mic vs cross-mic mean distance -- the trap check: if cross-mic
#      pairs are far and same-mic pairs close, the vector hears the room
#   4. the greedy top-3 at each tau, with the nearest REJECTED runner-up per
#      slot, as playable /clips/ paths -- the listening list
#
# Embedding input: the center 3 s of the clip (prev+event+next -> the event
# window) from the -enh sibling when present, else raw. "loudest" (the 3 s
# segment with the most bird-band energy) and "mean" (all segments) are
# computed too so the report can say which behaves.
# =============================================================================

from __future__ import annotations

import argparse
import itertools
import json
import os
import sqlite3
import sys
import time
import wave
from pathlib import Path

import numpy as np

SR = 48_000
SEG = 3 * SR
DIM = 1024

# The bestClips rule from mcc/lib/aviary.ts (#333, #442), ported verbatim.
BEST_CLIP_CONF_WINDOW = 0.05
BEST_CLIP_CONF_FLOOR = 0.90

DEFAULT_SPECIES = (
    "Northern Cardinal",
    "Blue Jay",
    "Black-capped Chickadee",
    "House Finch",
    "Red-bellied Woodpecker",
)


# --- store ---------------------------------------------------------------------

def load_candidates(db, common, floor, per_species, window_extra):
    """Top `per_species` by fill among conf >= floor, UNION the top
    `window_extra` by fill inside the bestClips confidence window, so the
    true champion is always in the pool (it is the max-fill row of the
    window set by definition)."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cmax = conn.execute(
        "SELECT MAX(confidence) FROM sightings WHERE species_common=? "
        "AND clip IS NOT NULL", (common,)).fetchone()[0]
    if cmax is None:
        return [], None
    base = ("SELECT id, ts, source, species_sci, species_common, confidence,"
            " clip, vocal_fill FROM sightings WHERE species_common=? AND"
            " clip IS NOT NULL AND confidence >= ? ORDER BY"
            " vocal_fill DESC, confidence DESC, ts ASC LIMIT ?")
    rows = {}
    for r in conn.execute(base, (common, floor, per_species)):
        rows[r["id"]] = dict(r)
    # bestClips eligibility is (conf >= cmax - window) OR (conf >= floor):
    # one lower bound, the smaller of the two.
    lo = min(cmax - BEST_CLIP_CONF_WINDOW, BEST_CLIP_CONF_FLOOR)
    for r in conn.execute(base, (common, lo, window_extra)):
        rows.setdefault(r["id"], dict(r))
    conn.close()
    return list(rows.values()), cmax


# --- audio ---------------------------------------------------------------------

def read_wav(path):
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        sw = w.getsampwidth()
        ch = w.getnchannels()
        raw = w.readframes(n)
    if sw == 2:
        x = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        x = np.frombuffer(raw, np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"sampwidth {sw}")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def enh_path(clips_dir, relpath):
    p = Path(clips_dir) / relpath
    e = p.with_name(p.stem + "-enh.wav")
    return e if e.exists() else None


def band_energy(x, sr, lo=1500.0, hi=8000.0):
    X = np.fft.rfft(x.astype(np.float64))
    f = np.fft.rfftfreq(x.size, 1.0 / sr)
    m = (f >= lo) & (f <= hi)
    return float(np.sqrt(np.mean(np.abs(X[m]) ** 2))) if m.any() else 0.0


def segments(x):
    """Whole 3 s segments of the clip; a trailing partial is dropped."""
    if x.size < SEG:
        return [np.pad(x, (0, SEG - x.size))]
    n = x.size // SEG
    return [x[i * SEG:(i + 1) * SEG] for i in range(n)]


# --- embedding -----------------------------------------------------------------

def embed_all(model, segs, batch_size):
    """segs: list of 3 s float32 arrays -> (n, 1024) L2-normalized."""
    # ONE call per variant: each encode_arrays call spins up a session (and
    # forks workers), so per-chunk calls would pay that ~seconds cost each.
    inp = [(s.astype(np.float32), SR) for s in segs]
    r = model.encode_arrays(inp, batch_size=batch_size)
    out = np.asarray(r.embeddings[:, 0, :], np.float32)
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return out / norms


def cosine_dist(a, b):
    return float(1.0 - np.dot(a, b))


# --- selection (the #551 rule) --------------------------------------------------

def best_clip(cands):
    """bestClips, ported. cands: dicts with confidence, vocal_fill, ts."""
    any_fill = any(c["vocal_fill"] is not None for c in cands)
    if not any_fill:
        best = cands[0]
        for c in cands:
            if c["confidence"] > best["confidence"] or (
                    c["confidence"] == best["confidence"] and c["ts"] < best["ts"]):
                best = c
        return best
    cmax = max(c["confidence"] for c in cands)
    elig = [c for c in cands if c["confidence"] >= cmax - BEST_CLIP_CONF_WINDOW
            or c["confidence"] >= BEST_CLIP_CONF_FLOOR]
    best = elig[0]
    for c in elig:
        f = c["vocal_fill"] if c["vocal_fill"] is not None else -1
        bf = best["vocal_fill"] if best["vocal_fill"] is not None else -1
        if f > bf:
            best = c
        elif f == bf and (c["confidence"] > best["confidence"] or (
                c["confidence"] == best["confidence"] and c["ts"] < best["ts"])):
            best = c
    return best


def select_showcase(cands, vecs, tau, floor):
    """Greedy: slot 1 = bestClips; slots 2/3 = first in fill-order with
    dist >= tau from every filled slot. Returns (slots, rejected) where
    rejected[slot] is the highest-ranked candidate that failed that slot's
    tau test (the nearest 'repeat' for the listener to compare)."""
    idx = {c["id"]: i for i, c in enumerate(cands)}
    s1 = best_clip(cands)
    slots = [s1]
    pool = [c for c in cands if c["id"] != s1["id"] and c["confidence"] >= floor]
    pool.sort(key=lambda c: (-(c["vocal_fill"] or -1), -c["confidence"], c["ts"]))
    rejected = {}
    for slot in (2, 3):
        pick = None
        for c in pool:
            if any(c["id"] == s["id"] for s in slots):
                continue
            d = min(cosine_dist(vecs[idx[c["id"]]], vecs[idx[s["id"]]]) for s in slots)
            if d >= tau:
                pick = c
                break
            if slot not in rejected:
                rejected[slot] = (c, d)
        if pick is None:
            break
        slots.append(pick)
    return slots, rejected


# --- report --------------------------------------------------------------------

def pct(d, ps=(5, 25, 50, 75, 95)):
    return " ".join(f"p{p}={np.percentile(d, p):.3f}" for p in ps)


def species_report(common, cands, vecs_by_variant, taus, floor, cmax, lines):
    lines.append(f"\n## {common}  (pool {len(cands)}, cmax {cmax:.2f})")
    srcs = {}
    for c in cands:
        srcs[c["source"]] = srcs.get(c["source"], 0) + 1
    lines.append("mics: " + ", ".join(f"{k} {v}" for k, v in sorted(srcs.items())))
    n = len(cands)
    for variant, vecs in vecs_by_variant.items():
        if n < 3:
            lines.append(f"[{variant}] too few candidates")
            continue
        same, cross, alld = [], [], []
        for i, j in itertools.combinations(range(n), 2):
            d = cosine_dist(vecs[i], vecs[j])
            alld.append(d)
            (same if cands[i]["source"] == cands[j]["source"] else cross).append(d)
        lines.append(f"[{variant}] pairwise: {pct(alld)}")
        if same and cross:
            lines.append(f"[{variant}] same-mic mean {np.mean(same):.3f} (n={len(same)})"
                         f"  cross-mic mean {np.mean(cross):.3f} (n={len(cross)})"
                         f"  ratio {np.mean(cross) / max(np.mean(same), 1e-9):.2f}")
        else:
            lines.append(f"[{variant}] one mic only; same/cross test moot")
    # The listening list uses the primary variant.
    vecs = vecs_by_variant["center"]
    for tau in taus:
        slots, rejected = select_showcase(cands, vecs, tau, floor)
        lines.append(f"\n[center] tau={tau:.2f} -> {len(slots)} slot(s)")
        idx = {c["id"]: i for i, c in enumerate(cands)}
        for k, s in enumerate(slots, 1):
            dists = " ".join(f"d{j + 1}={cosine_dist(vecs[idx[s['id']]], vecs[idx[o['id']]]):.3f}"
                             for j, o in enumerate(slots[:k - 1]))
            lines.append(f"  slot {k}: conf {s['confidence']:.2f} fill {s['vocal_fill'] if s['vocal_fill'] is not None else 'null'}"
                         f" {s['source']} {time.strftime('%Y-%m-%d %H:%M', time.localtime(s['ts']))}"
                         f" {dists}\n          /clips/{s['clip']}")
        for slot, (c, d) in sorted(rejected.items()):
            lines.append(f"  rejected for slot {slot} (repeat?): conf {c['confidence']:.2f}"
                         f" fill {c['vocal_fill'] if c['vocal_fill'] is not None else 'null'}"
                         f" {c['source']} min-dist {d:.3f}\n          /clips/{c['clip']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("MERLE_EARL_DB", "earl.db"))
    ap.add_argument("--clips", default=os.environ.get("MERLE_EARL_CLIPS", "clips"))
    ap.add_argument("--species", default=",".join(DEFAULT_SPECIES))
    ap.add_argument("--per-species", type=int, default=100)
    ap.add_argument("--window-extra", type=int, default=30)
    ap.add_argument("--floor", type=float, default=0.80)
    ap.add_argument("--taus", default="0.2,0.3,0.4")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--out", default="showcase-probe")
    ap.add_argument("--report-only", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = out / "vectors.npz"
    meta_path = out / "meta.json"
    taus = [float(t) for t in args.taus.split(",")]
    species = [s.strip() for s in args.species.split(",") if s.strip()]

    if args.report_only and cache.exists():
        z = np.load(cache)
        meta = json.loads(meta_path.read_text())
    else:
        import birdnet
        t0 = time.time()
        model = birdnet.load("acoustic", "2.4", "tf")
        print(f"model loaded {time.time() - t0:.1f}s", flush=True)
        meta = {}
        arrays = {}
        for common in species:
            cands, cmax = load_candidates(args.db, common, args.floor,
                                          args.per_species, args.window_extra)
            kept, center, loudest, allsegs, seg_owner = [], [], [], [], []
            n_enh = 0
            for c in cands:
                p = enh_path(args.clips, c["clip"])
                if p is not None:
                    n_enh += 1
                else:
                    p = Path(args.clips) / c["clip"]
                    if not p.exists():
                        continue
                try:
                    x, sr = read_wav(p)
                except Exception as e:
                    print(f"  skip {c['clip']}: {e}")
                    continue
                if sr != SR:
                    print(f"  skip {c['clip']}: sr {sr}")
                    continue
                segs = segments(x)
                c = dict(c, n_seg=len(segs), enh=p.name.endswith("-enh.wav"))
                kept.append(c)
                center.append(segs[len(segs) // 2])
                loudest.append(max(segs, key=lambda s: band_energy(s, sr)))
                for s in segs:
                    allsegs.append(s)
                    seg_owner.append(len(kept) - 1)
            if not kept:
                print(f"{common}: no readable candidates")
                continue
            t1 = time.time()
            v_center = embed_all(model, center, args.batch)
            v_loud = embed_all(model, loudest, args.batch)
            v_all = embed_all(model, allsegs, args.batch)
            v_mean = np.zeros_like(v_center)
            owner = np.asarray(seg_owner)
            for i in range(len(kept)):
                m = v_all[owner == i].mean(axis=0)
                v_mean[i] = m / max(np.linalg.norm(m), 1e-9)
            print(f"{common}: {len(kept)} clips ({n_enh} enh), "
                  f"{len(allsegs)} segments, embedded in {time.time() - t1:.1f}s",
                  flush=True)
            key = common.replace(" ", "_")
            arrays[f"{key}__center"] = v_center
            arrays[f"{key}__loudest"] = v_loud
            arrays[f"{key}__mean"] = v_mean
            meta[common] = {"cands": kept, "cmax": cmax}
        np.savez(cache, **arrays)
        meta_path.write_text(json.dumps(meta))
        z = np.load(cache)

    lines = [f"# showcase probe  floor={args.floor} taus={taus}  db={args.db}"]
    for common, m in meta.items():
        key = common.replace(" ", "_")
        vbv = {v: z[f"{key}__{v}"] for v in ("center", "loudest", "mean")}
        species_report(common, m["cands"], vbv, taus, args.floor, m["cmax"], lines)
    report = "\n".join(lines)
    (out / "report.md").write_text(report)
    print(report)


if __name__ == "__main__":
    sys.exit(main())
