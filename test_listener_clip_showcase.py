# =============================================================================
# project-squirrel -- test_listener_clip_showcase.py
#
# The species showcase's pure half (issue #551): cosine distance, the
# bestClips port pinned to the shared fixture both suites read, the greedy
# three-slot rule with the loner test, the species-relative radius, the
# embed worklist's mtime idempotence, and the pick's fingerprint gate.
# Vectors are synthetic and low-dimensional -- the rule is dimension-blind
# -- and the encoder is a fake; TensorFlow never enters the test process.
# =============================================================================

import json
import os
import random
import types
import wave

import numpy as np
import pytest

from listener import clip_enhance as ce
from listener import clip_showcase as sc
from listener import sightings

SR = sc.SR
HERE = os.path.dirname(os.path.abspath(__file__))


# --- helpers -----------------------------------------------------------------

def cand(i, conf=0.95, fill=0.5, ts=None, clip=None):
    return {"id": i, "clip": clip or f"rear/{i}.wav", "confidence": conf,
            "vocal_fill": fill, "ts": ts if ts is not None else 1000 + i}


def cluster(center, n, eps, rng):
    """n unit vectors near `center` (a unit vector), noise scale eps."""
    out = []
    center = np.asarray(center, float)
    for _ in range(n):
        v = center + rng.normal(0, eps, size=center.size)
        out.append(v / np.linalg.norm(v))
    return out


E1, E2, E3, E4 = np.eye(4)


def yard(eps=0.05, seed=1):
    """A pool with the measured shape: a song cluster (holds the champion),
    a chip cluster, and one contaminated loner. Returns (cands, vecs)."""
    rng = np.random.default_rng(seed)
    cands, vecs = [], []
    for i, v in enumerate(cluster(E1, 6, eps, rng)):          # song
        cands.append(cand(i, conf=0.95, fill=0.6 - 0.01 * i))
        vecs.append(v)
    for i, v in enumerate(cluster(E2, 5, eps, rng), start=10):  # chip
        cands.append(cand(i, conf=0.88, fill=0.5 - 0.01 * (i - 10)))
        vecs.append(v)
    cands.append(cand(20, conf=0.90, fill=0.55))              # loner
    vecs.append(E3)
    return cands, np.stack(vecs)


def write_clip(path, seconds=9.0, hz=4000.0, amp=0.2):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    t = np.arange(int(seconds * SR)) / SR
    pcm = (amp * np.sin(2 * np.pi * hz * t) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


# --- distance ------------------------------------------------------------------

def test_cosine_distance_basics():
    assert sc.cosine_dist([1, 0, 0], [1, 0, 0]) == pytest.approx(0.0)
    assert sc.cosine_dist([1, 0, 0], [0, 1, 0]) == pytest.approx(1.0)
    assert sc.cosine_dist([1, 0, 0], [-1, 0, 0]) == pytest.approx(2.0)
    # scale-invariant: the encoder's magnitude carries no meaning here
    assert sc.cosine_dist([3, 0, 0], [0.5, 0, 0]) == pytest.approx(0.0)


def test_pairwise_is_symmetric_with_zero_diagonal():
    D = sc.pairwise(np.array([[1, 0], [0, 1], [1, 1]], float))
    assert np.allclose(D, D.T)
    assert np.allclose(np.diag(D), 0)
    assert D[0, 1] == pytest.approx(1.0)
    assert D[0, 2] == pytest.approx(1 - 1 / np.sqrt(2))


def test_min_neighbours_scales_with_the_pool_and_never_drops_below_one():
    assert sc.min_neighbours(100) == 3       # the measured contract
    assert sc.min_neighbours(12) == 1        # a small pool: heard twice
    assert sc.min_neighbours(2) == 1
    assert sc.min_neighbours(200) == 6


def test_neighbour_radius_scales_with_the_pool():
    D = sc.pairwise(yard()[1])
    assert sc.neighbour_radius(2 * D) == pytest.approx(2 * sc.neighbour_radius(D))
    assert sc.neighbour_radius(np.zeros((1, 1))) == 0.0


# --- bestClips, pinned to the shared fixture ----------------------------------

def test_best_clip_matches_the_shared_fixture():
    """fixtures/best_clips.json is the contract: mcc/lib/aviary.test.ts runs
    the same cases through bestClips. Slot 1 can never drift from the page."""
    with open(os.path.join(HERE, "fixtures", "best_clips.json"),
              encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    assert len(cases) > 5
    for case in cases:
        by_sci = {}
        for r in case["rows"]:
            if not r.get("clip") or not isinstance(r.get("confidence"), (int, float)):
                continue
            by_sci.setdefault(r["species_sci"], []).append(dict(r))
        got = {}
        for sci, rows in by_sci.items():
            b = sc.best_clip(rows)
            got[sci] = {"clip": b["clip"], "confidence": b["confidence"],
                        "ts": b["ts"]}
        assert got == case["expected"], case["name"]


def test_best_clip_honours_a_supplied_species_cmax():
    # The pool is a slice; the window must be the species' real one. With
    # the pool's own cmax (0.91) the 0.88 row is inside the window and its
    # fill wins; with the species' cmax of 1.0 it is outside and below the
    # floor, so the 0.91 row is the only eligible one.
    rows = [cand(1, conf=0.88, fill=0.9), cand(2, conf=0.91, fill=0.3)]
    assert sc.best_clip(rows)["id"] == 1            # pool's own window
    assert sc.best_clip(rows, cmax=1.0)["id"] == 2  # species' window


# --- the rule ------------------------------------------------------------------

def test_three_slots_song_chip_and_no_loner():
    cands, vecs = yard()
    res = sc.select_showcase(cands, vecs)
    slots = [s["cand"]["id"] for s in res["slots"]]
    assert slots[0] == 0                 # the fullest song = bestClips
    assert slots[1] == 10                # the fullest chip: far and clean
    assert len(slots) == 2               # the loner is far but has no neighbours
    assert [l["cand"]["id"] for l in res["loners"]] == [20]
    assert res["slots"][1]["dists"][0] >= sc.TAU


def test_a_loner_is_skipped_even_when_far_enough():
    # Give the loner the best fill of all (at a confidence that keeps it
    # out of the slot-1 window): it still cannot take a slot.
    cands, vecs = yard()
    cands[-1]["vocal_fill"] = 0.99
    cands[-1]["confidence"] = 0.85
    res = sc.select_showcase(cands, vecs)
    assert all(s["cand"]["id"] != 20 for s in res["slots"])
    # ...but with two friends it is a real call type
    cands = cands + [cand(21, conf=0.9, fill=0.4), cand(22, conf=0.9, fill=0.4)]
    rng = np.random.default_rng(3)
    vecs = np.vstack([vecs] + cluster(E3, 2, 0.05, rng))
    res = sc.select_showcase(cands, vecs)
    assert [s["cand"]["id"] for s in res["slots"]] == [0, 20, 10]


def test_slot_three_must_be_far_from_both_earlier_slots():
    # A clean cluster sitting between song and chip (near both) never fills
    # slot 3, however full it is. (conf 0.85: under the slot-1 window, over
    # the slot-2/3 floor.)
    cands, vecs = yard()
    rng = np.random.default_rng(5)
    mid = (E1 + E2) / np.sqrt(2)
    extra = [cand(30 + i, conf=0.85, fill=0.99) for i in range(4)]
    vecs = np.vstack([vecs] + cluster(mid, 4, 0.02, rng))
    res = sc.select_showcase(cands + extra, vecs)
    assert [s["cand"]["id"] for s in res["slots"]] == [0, 10]


def test_slots_two_and_three_use_the_absolute_floor_not_the_window():
    # The chip cluster at 0.79 is under the floor: slot 2 stays empty. At
    # 0.80 it competes even though it is far below the song's 0.95.
    cands, vecs = yard()
    for c in cands:
        if 10 <= c["id"] < 20:
            c["confidence"] = 0.79
    assert len(sc.select_showcase(cands, vecs)["slots"]) == 1
    for c in cands:
        if 10 <= c["id"] < 20:
            c["confidence"] = 0.80
    assert len(sc.select_showcase(cands, vecs)["slots"]) == 2


def test_a_small_pool_fills_slot_one_only():
    cands, vecs = yard()
    res = sc.select_showcase(cands[:5], vecs[:5])
    assert [s["cand"]["id"] for s in res["slots"]] == [0]
    assert res["radius"] == 0.0


def test_the_radius_is_species_relative():
    # The same layout at four times the spread: same picks. A fixed radius
    # would call the wider species' clips loners (the woodpecker lesson).
    tight = sc.select_showcase(*yard(eps=0.02))
    wide = sc.select_showcase(*yard(eps=0.08))
    assert [s["cand"]["id"] for s in tight["slots"]] == \
        [s["cand"]["id"] for s in wide["slots"]]
    assert wide["radius"] > 2 * tight["radius"]


def test_selection_is_deterministic_under_shuffle():
    cands, vecs = yard()
    order = list(range(len(cands)))
    random.Random(7).shuffle(order)
    shuffled = [cands[i] for i in order]
    res = sc.select_showcase(shuffled, vecs[order])
    assert [s["cand"]["id"] for s in res["slots"]] == [0, 10]


def test_a_new_champion_revalidates_the_other_slots():
    # The champion moves from the song cluster to the chip cluster (a chip
    # clip gains the top fill inside the window): slot 2 flips to the song.
    cands, vecs = yard()
    chip = next(c for c in cands if c["id"] == 10)
    chip["confidence"] = 0.95
    chip["vocal_fill"] = 0.99
    res = sc.select_showcase(cands, vecs)
    assert [s["cand"]["id"] for s in res["slots"]] == [10, 0]


def test_empty_pool_is_empty():
    assert sc.select_showcase([], np.zeros((0, 4)))["slots"] == []


# --- the store side ------------------------------------------------------------

def event(ts, clip, conf=0.9, sci="Poecile atricapillus",
          common="Black-capped Chickadee", source="rear"):
    return json.dumps({
        "ts": ts, "source": source, "kind": "detection",
        "species_sci": sci, "species_common": common,
        "confidence": conf, "window_s": 3, "clip": clip,
        "wind_suspect": False,
    })


@pytest.fixture
def store(tmp_path):
    """A real earl.db with the sightings schema AND the pass's tables, plus
    a clips dir. Returns (conn, media_dir, path)."""
    path = str(tmp_path / "earl.db")
    conn = sightings.connect(path)
    conn.close()
    conn = sc.connect(path)
    media = str(tmp_path / "clips")
    os.makedirs(media)
    return conn, media, path


def seed(conn, media, n=12, sci="Poecile atricapillus", write=True,
         conf=0.9, start_ts=1000):
    """n sightings rows with clips on disk and fills descending by ts."""
    for i in range(n):
        clip = f"rear/{start_ts + i}-x.wav"
        sightings.record(conn, sightings.parse_event(
            event(start_ts + i, clip, conf=conf, sci=sci)))
        conn.execute("UPDATE sightings SET vocal_fill = ? WHERE clip = ?",
                     (0.9 - 0.01 * i, clip))
        if write:
            write_clip(os.path.join(media, clip))
    conn.commit()


def fake_vec(i, dim=8):
    v = np.zeros(dim, np.float32)
    v[i % dim] = 1.0
    return v


def test_worklist_wants_every_pool_clip_on_disk_once(store):
    conn, media, _ = store
    seed(conn, media, n=4)
    seed(conn, media, n=1, start_ts=2000, write=False)   # pruned: not a row
    p = sc.plan_species(conn, media, "Poecile atricapillus")
    assert len(p["rows"]) == 4
    assert [r["clip"] for r in p["todo"]] == [r["clip"] for r in p["rows"]]
    assert all(r["src_kind"] == "raw" for r in p["rows"])
    n_todo, stale = sc.plan(conn, media)
    assert n_todo == 4 and stale == [("Poecile atricapillus",
                                      "Black-capped Chickadee")]


def test_worklist_is_idempotent_by_mtime_and_source(store):
    conn, media, _ = store
    seed(conn, media, n=3)
    p = sc.plan_species(conn, media, "Poecile atricapillus")
    for i, r in enumerate(p["rows"]):
        sc.store_signature(conn, clip=r["clip"], sighting_id=r["id"],
                           sci=r["species_sci"], vec=fake_vec(i),
                           src=r["src_kind"], src_mtime=r["src_mtime"], now=5)
    conn.commit()
    assert sc.plan_species(conn, media, "Poecile atricapillus")["todo"] == []

    # A `best` rewrite bumps the file's mtime: re-embed that one.
    rewritten = p["rows"][1]["clip"]
    full = os.path.join(media, rewritten)
    os.utime(full, (p["rows"][1]["src_mtime"] + 60,) * 2)
    todo = sc.plan_species(conn, media, "Poecile atricapillus")["todo"]
    assert [r["clip"] for r in todo] == [rewritten]

    # The enhanced sibling landing is a different source: re-embed too.
    write_clip(os.path.join(media, ce.enhanced_relpath(p["rows"][2]["clip"])))
    todo = sc.plan_species(conn, media, "Poecile atricapillus")["todo"]
    assert sorted(r["clip"] for r in todo) == sorted(
        [rewritten, p["rows"][2]["clip"]])
    assert [r["src_kind"] for r in todo if r["clip"] != rewritten] == ["enh"]


def test_embed_rows_stores_unit_vectors_and_reads_the_center_window(store):
    conn, media, _ = store
    seed(conn, media, n=2)
    p = sc.plan_species(conn, media, "Poecile atricapillus")
    seen = {}

    class Model:
        def encode_arrays(self, inp, batch_size=16):
            for x, sr in inp:
                assert sr == SR and x.shape == (sc.SEG,) and x.dtype == np.float32
            seen["n"] = len(inp)
            emb = np.stack([np.full((1, 8), 3.0, np.float32) for _ in inp])
            return types.SimpleNamespace(embeddings=emb)

    assert sc.embed_rows(conn, p["todo"], Model(), now=7) == 2
    assert seen["n"] == 2
    sigs = sc.load_signatures(conn, "Poecile atricapillus")
    assert len(sigs) == 2
    for src, mtime, vec in sigs.values():
        assert src == "raw" and vec.shape == (8,)
        assert np.linalg.norm(vec) == pytest.approx(1.0)
    assert sc.plan_species(conn, media, "Poecile atricapillus")["todo"] == []


def test_an_empty_file_costs_one_line_not_the_pass(store):
    """#601: wave.open raises EOFError -- not wave.Error -- on an empty or
    truncated file, and one such file exited the encoder every tick for
    twenty days on kestrel. The row is logged and skipped; the rest embed."""
    conn, media, _ = store
    seed(conn, media, n=3)
    p = sc.plan_species(conn, media, "Poecile atricapillus")
    empty = p["todo"][1]
    open(empty["src_full"], "wb").close()          # zero bytes, like kestrel's
    lines = []

    class Model:
        def encode_arrays(self, inp, batch_size=16):
            emb = np.stack([np.full((1, 8), 3.0, np.float32) for _ in inp])
            return types.SimpleNamespace(embeddings=emb)

    assert sc.embed_rows(conn, p["todo"], Model(), now=7, log=lines.append) == 2
    assert [l for l in lines if "unreadable" in l and empty["clip"] in l]
    assert empty["clip"] not in sc.load_signatures(conn, "Poecile atricapillus")


def test_an_empty_sibling_is_no_sibling_for_the_encoder(store):
    """#601: source_for() falls through to the original when the -enh
    sibling is zero bytes, instead of handing the encoder the empty file."""
    conn, media, _ = store
    seed(conn, media, n=1)
    clip = sc.plan_species(conn, media, "Poecile atricapillus")["rows"][0]["clip"]
    open(os.path.join(media, ce.enhanced_relpath(clip)), "wb").close()
    full, kind, _ = sc.source_for(media, clip)
    assert kind == "raw" and full == os.path.join(media, clip)
    write_clip(os.path.join(media, ce.enhanced_relpath(clip)))
    assert sc.source_for(media, clip)[1] == "enh"


def test_read_center_takes_the_middle_segment_and_pads_a_short_clip(tmp_path):
    path = str(tmp_path / "c.wav")
    # 9 s: silence, tone, silence -- the center is the tone.
    t = np.arange(3 * SR) / SR
    tone = (0.5 * np.sin(2 * np.pi * 1000 * t) * 32767).astype("<i2")
    pcm = np.concatenate([np.zeros(3 * SR, "<i2"), tone, np.zeros(3 * SR, "<i2")])
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    x = sc.read_center(path)
    assert x.shape == (sc.SEG,) and float(np.abs(x).max()) > 0.4
    write_clip(str(tmp_path / "short.wav"), seconds=1.0)
    assert sc.read_center(str(tmp_path / "short.wav")).shape == (sc.SEG,)


def test_pick_writes_slots_then_settles_until_the_pool_changes(store, capsys):
    conn, media, _ = store
    seed(conn, media, n=12)
    p = sc.plan_species(conn, media, "Poecile atricapillus")
    rng = np.random.default_rng(2)
    song = cluster(np.r_[E1, 0, 0, 0, 0], 7, 0.05, rng)
    chip = cluster(np.r_[E2, 0, 0, 0, 0], 5, 0.05, rng)
    for r, v in zip(p["rows"], song + chip):
        sc.store_signature(conn, clip=r["clip"], sighting_id=r["id"],
                           sci=r["species_sci"], vec=v, src="raw",
                           src_mtime=r["src_mtime"], now=1)
    conn.commit()
    assert sc.pick_pass(conn, media, now=100) == {"picked": 1}
    slots = sc.stored_slots(conn, "Poecile atricapillus")
    assert slots[1] == p["rows"][0]["clip"]          # the fullest song
    assert slots[2] == p["rows"][7]["clip"]          # the fullest chip
    assert 3 not in slots
    out = capsys.readouterr().out
    assert "slot 1 ->" in out and "slot 2 ->" in out
    # retention now spares both
    assert {slots[1], slots[2]} <= sightings.exempt_clips(conn)

    # Same pool: nothing written, nothing said.
    assert sc.pick_pass(conn, media, now=200) == {"unchanged": 1}
    assert capsys.readouterr().out == ""

    # A new signature changes the pool's fingerprint: re-pick, and a slot
    # that does not move is not announced.
    seed(conn, media, n=1, start_ts=5000)
    r = sc.plan_species(conn, media, "Poecile atricapillus")["todo"][0]
    sc.store_signature(conn, clip=r["clip"], sighting_id=r["id"],
                       sci=r["species_sci"], vec=chip[0], src="raw",
                       src_mtime=r["src_mtime"], now=2)
    conn.commit()
    assert sc.pick_pass(conn, media, now=300) == {"picked": 1}
    assert "slot 1 ->" not in capsys.readouterr().out


def test_slot_one_is_written_before_any_vector_exists(store):
    # The champion is protected from retention from the first tick, encoder
    # or not: bestClips needs no signature.
    conn, media, _ = store
    seed(conn, media, n=3)
    assert sc.pick_pass(conn, media, now=1) == {"picked": 1}
    slots = sc.stored_slots(conn, "Poecile atricapillus")
    assert list(slots) == [1]
    assert slots[1] in sightings.exempt_clips(conn)


def test_a_species_with_no_clip_on_disk_is_empty(store):
    conn, media, _ = store
    seed(conn, media, n=2, write=False)
    assert sc.pick_pass(conn, media) == {"empty": 1}
    assert sc.stored_slots(conn, "Poecile atricapillus") == {}
