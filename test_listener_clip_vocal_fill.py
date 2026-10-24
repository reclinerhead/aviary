# =============================================================================
# project-squirrel -- test_listener_clip_vocal_fill.py
#
# Vocal-fill score + backfill worklist (issue #442). Pure DSP + pure SQL
# worklist — silent regressions would crown the wrong "best recording".
# Synthesized signals only; never real yard clips.
# =============================================================================

import os
import wave

import numpy as np
import pytest

from listener import clip_vocal_fill as vf
from listener import gate
from listener import sightings

SR = gate.SAMPLE_RATE


def tone(hz, seconds=1.0, amp=1.0, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    return amp * np.sin(2 * np.pi * hz * t)


def write_mono_wav(path, samples, sr=SR):
    pcm = np.clip(np.asarray(samples) * 32768.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


# --- bird band ---------------------------------------------------------------

def test_bird_band_kills_low_frequency_noise():
    """200 Hz is the plane / furnace — must not count as song."""
    out = vf.bird_band(tone(200, seconds=1.0, amp=0.5), SR)
    assert float(np.sqrt(np.mean(out ** 2))) < 0.02


def test_bird_band_passes_song_frequency():
    bird = tone(4000, seconds=1.0, amp=0.3)
    out = vf.bird_band(bird, SR)
    assert float(np.sqrt(np.mean(out ** 2))) == pytest.approx(
        float(np.sqrt(np.mean(bird ** 2))), rel=0.05)


# --- vocal_fill --------------------------------------------------------------

def test_silence_scores_zero():
    assert vf.vocal_fill(np.zeros(SR * 3), SR) == 0.0


def test_empty_scores_zero():
    assert vf.vocal_fill(np.array([]), SR) == 0.0


def test_full_song_scores_high():
    """Bird-band tone for the whole clip (including where pre-roll would
    be) still scores high: continuous song is continuous song. Floor from
    pre-roll that also has song is higher, but the whole clip clears it."""
    # Pre-roll quiet, then continuous bird — classic full visit after open.
    clip = np.concatenate([
        np.zeros(int(3 * SR)),
        tone(4000, seconds=6.0, amp=0.2),
    ])
    fill = vf.vocal_fill(clip, SR)
    assert fill >= 0.55  # ~6/9 of the clip is song


def test_short_chirp_scores_low():
    """Single warble in an otherwise quiet clip — the product bug #442
    exists to demote. Pre-roll silence, brief chirp, silence."""
    chirp = tone(4000, seconds=0.15, amp=0.25)
    pad = np.zeros(int(SR * 0.15))
    clip = np.concatenate([
        np.zeros(int(3 * SR)),           # pre-roll
        pad, chirp, pad,
        np.zeros(int(5.5 * SR)),         # rest quiet
    ])
    # Trim/pad to ~9 s
    n = int(9 * SR)
    if clip.size < n:
        clip = np.concatenate([clip, np.zeros(n - clip.size)])
    else:
        clip = clip[:n]
    fill = vf.vocal_fill(clip, SR)
    assert fill < 0.25


def test_half_duration_song_is_middling():
    half = int(4.5 * SR)
    clip = np.concatenate([
        np.zeros(half),
        tone(3500, seconds=4.5, amp=0.2),
    ])
    fill = vf.vocal_fill(clip, SR)
    assert 0.35 <= fill <= 0.70


def test_low_frequency_noise_does_not_score_as_full():
    """Busy road noise must not look like a full song."""
    noise = tone(150, seconds=9.0, amp=0.8)
    fill = vf.vocal_fill(noise, SR)
    assert fill < 0.15


def test_chirp_loses_to_full_song_on_fill():
    """The ranking premise: fuller clip has higher fill than a warble."""
    n = int(9 * SR)
    chirp = np.zeros(n)
    c0 = int(3.2 * SR)
    chirp[c0:c0 + int(0.12 * SR)] = tone(4000, seconds=0.12, amp=0.3)

    full = np.zeros(n)
    full[int(3 * SR):] = tone(4000, seconds=6.0, amp=0.2)

    assert vf.vocal_fill(full, SR) > vf.vocal_fill(chirp, SR) + 0.3


# --- worklist / schema -------------------------------------------------------

@pytest.fixture
def conn():
    return sightings.connect(":memory:")


def test_schema_has_vocal_fill_column(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sightings)")}
    assert "vocal_fill" in cols


def test_upgrade_adds_vocal_fill_to_legacy_store(tmp_path):
    """Pre-#442 file gets the column on connect without disturbing rows."""
    path = tmp_path / "legacy.db"
    import sqlite3
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE sightings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            source TEXT NOT NULL,
            species_sci TEXT NOT NULL,
            species_common TEXT NOT NULL,
            confidence REAL NOT NULL,
            clip TEXT,
            wind_suspect INTEGER NOT NULL DEFAULT 0,
            rms REAL,
            location_id INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE life_list (
            species_sci TEXT NOT NULL,
            species_common TEXT NOT NULL,
            first_ts INTEGER NOT NULL,
            first_source TEXT NOT NULL,
            first_clip TEXT,
            location_id INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (species_sci, location_id)
        );
        CREATE TABLE locations (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            created_ts INTEGER NOT NULL
        );
        INSERT INTO locations VALUES (1, 'Home', 0, 0, 0);
        INSERT INTO sightings (ts, source, species_sci, species_common,
            confidence, clip, wind_suspect, location_id)
            VALUES (100, 'amcrest', 'A a', 'A', 0.9, 'a.wav', 0, 1);
    """)
    raw.commit()
    raw.close()

    conn = sightings.connect(str(path))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sightings)")}
    assert "vocal_fill" in cols
    row = conn.execute("SELECT clip, vocal_fill FROM sightings").fetchone()
    assert row["clip"] == "a.wav"
    assert row["vocal_fill"] is None
    conn.close()


def test_worklist_skips_scored_rows_unless_force(conn):
    conn.execute(
        "INSERT INTO sightings (ts, source, species_sci, species_common,"
        " confidence, clip, wind_suspect, vocal_fill)"
        " VALUES (1, 's', 'A a', 'A', 0.9, 'a.wav', 0, 0.5),"
        "        (2, 's', 'A a', 'A', 0.8, 'b.wav', 0, NULL),"
        "        (3, 's', 'A a', 'A', 0.7, NULL, 0, NULL)"
    )
    conn.commit()
    todo = vf.worklist(conn)
    assert todo == [(2, "b.wav")]

    forced = vf.worklist(conn, force=True)
    assert forced == [(1, "a.wav"), (2, "b.wav")]


def test_worklist_refresh_by_clip(conn):
    conn.execute(
        "INSERT INTO sightings (ts, source, species_sci, species_common,"
        " confidence, clip, wind_suspect, vocal_fill)"
        " VALUES (1, 's', 'A a', 'A', 0.9, 'a.wav', 0, 0.5),"
        "        (2, 's', 'A a', 'A', 0.8, 'b.wav', 0, NULL)"
    )
    conn.commit()
    assert vf.worklist(conn, refresh_clip="a.wav") == [(1, "a.wav")]


def test_score_row_writes_fill(tmp_path, conn):
    media = tmp_path / "clips"
    media.mkdir()
    samples = np.concatenate([
        np.zeros(int(3 * SR)),
        tone(4000, seconds=6.0, amp=0.2),
    ])
    rel = "amcrest/100-Cardinal.wav"
    full = media / "amcrest"
    full.mkdir()
    write_mono_wav(full / "100-Cardinal.wav", samples)

    conn.execute(
        "INSERT INTO sightings (ts, source, species_sci, species_common,"
        " confidence, clip, wind_suspect)"
        " VALUES (100, 'amcrest', 'Cardinalis cardinalis', 'Northern Cardinal',"
        " 0.95, ?, 0)",
        (rel,),
    )
    conn.commit()
    sid = conn.execute("SELECT id FROM sightings").fetchone()["id"]
    status = vf.score_row(conn, str(media), sid, rel)
    assert status == "scored"
    conn.commit()
    fill = conn.execute(
        "SELECT vocal_fill FROM sightings WHERE id = ?", (sid,)
    ).fetchone()["vocal_fill"]
    assert fill is not None and fill >= 0.5


def test_score_row_missing_file(tmp_path, conn):
    conn.execute(
        "INSERT INTO sightings (ts, source, species_sci, species_common,"
        " confidence, clip, wind_suspect)"
        " VALUES (1, 's', 'A a', 'A', 0.9, 'gone.wav', 0)"
    )
    conn.commit()
    sid = conn.execute("SELECT id FROM sightings").fetchone()["id"]
    assert vf.score_row(conn, str(tmp_path), sid, "gone.wav") == "missing"
    conn.commit()
    # Pruned path still leaves the worklist (0.0 written — not NULL forever).
    fill = conn.execute(
        "SELECT vocal_fill FROM sightings WHERE id = ?", (sid,)
    ).fetchone()["vocal_fill"]
    assert fill == 0.0
    assert vf.worklist(conn) == []


def test_an_empty_file_scores_none_not_a_crash(tmp_path):
    """#601: wave.open raises EOFError on a zero-byte file, which the
    reader's guard did not catch. Unreadable means None, never an exception
    out of the pass."""
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    assert vf.score_wav(str(empty)) is None


def test_second_run_is_noop(tmp_path, conn):
    media = tmp_path / "clips"
    (media / "x").mkdir(parents=True)
    write_mono_wav(media / "x" / "a.wav", tone(4000, seconds=2.0, amp=0.2))
    conn.execute(
        "INSERT INTO sightings (ts, source, species_sci, species_common,"
        " confidence, clip, wind_suspect)"
        " VALUES (1, 's', 'A a', 'A', 0.9, 'x/a.wav', 0)"
    )
    conn.commit()
    sid = 1
    assert vf.score_row(conn, str(media), sid, "x/a.wav") == "scored"
    conn.commit()
    assert vf.worklist(conn) == []
