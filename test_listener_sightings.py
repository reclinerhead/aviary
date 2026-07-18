# =============================================================================
# project-squirrel -- test_listener_sightings.py
#
# The bird record's two guarantees (issue #172): every well-formed detection
# becomes a sightings row, and first-heard is enforced BY THE STORE (INSERT
# OR IGNORE on life_list) -- a replayed event, a restart, or a second source
# hearing the same lifer seconds later can never move a first_ts. Plus the
# wire-parsing rule: malformed messages are dropped, never a dead consumer.
# Also covers earl.py's pure config/shaping helpers (week math, the redacted
# RTSP argv, source parsing) -- the daemon loop itself is desk-tested on
# pearl like every other bus process.
# =============================================================================

import json

import pytest

from listener import earl, sightings


def event(ts=1784390000, source="amcrest",
          sci="Poecile atricapillus", common="Black-capped Chickadee",
          confidence=0.88, clip="amcrest/1784390000-x.wav", wind=False):
    return json.dumps({
        "ts": ts, "source": source, "kind": "detection",
        "species_sci": sci, "species_common": common,
        "confidence": confidence, "window_s": 3,
        "clip": clip, "wind_suspect": wind,
    })


@pytest.fixture
def conn():
    return sightings.connect(":memory:")


# --- recording and the life list ---------------------------------------------

def test_first_sighting_is_a_lifer_second_is_not(conn):
    first = sightings.record(conn, sightings.parse_event(event(ts=100)))
    again = sightings.record(conn, sightings.parse_event(event(ts=200)))
    assert first is True and again is False
    rows = conn.execute("SELECT ts FROM sightings ORDER BY ts").fetchall()
    assert [r["ts"] for r in rows] == [100, 200]
    life = conn.execute("SELECT * FROM life_list").fetchall()
    assert len(life) == 1
    assert life[0]["first_ts"] == 100


def test_first_heard_never_moves(conn):
    # A replay of an OLDER event after the fact must not rewrite history
    # backwards either -- first insert wins, whatever its ts. (The bus is
    # live transport: what the store first heard IS the record.)
    sightings.record(conn, sightings.parse_event(event(ts=500)))
    sightings.record(conn, sightings.parse_event(event(ts=100)))
    life = conn.execute("SELECT first_ts FROM life_list").fetchone()
    assert life["first_ts"] == 500


def test_two_species_two_lifers(conn):
    a = sightings.record(conn, sightings.parse_event(event()))
    b = sightings.record(conn, sightings.parse_event(
        event(sci="Haemorhous mexicanus", common="House Finch")))
    assert a is True and b is True
    assert conn.execute("SELECT COUNT(*) c FROM life_list").fetchone()["c"] == 2


def test_same_timestamp_two_rows(conn):
    # Two species in one window share a ts -- both rows must land (why the
    # sightings key is an id, not ts like weather's).
    sightings.record(conn, sightings.parse_event(event(ts=100)))
    sightings.record(conn, sightings.parse_event(
        event(ts=100, sci="Haemorhous mexicanus", common="House Finch")))
    assert conn.execute("SELECT COUNT(*) c FROM sightings").fetchone()["c"] == 2


def test_lifer_carries_its_first_clip_and_source(conn):
    sightings.record(conn, sightings.parse_event(
        event(source="rover", clip="rover/1-Finch.wav",
              sci="Haemorhous mexicanus", common="House Finch")))
    life = conn.execute("SELECT * FROM life_list").fetchone()
    assert life["first_source"] == "rover"
    assert life["first_clip"] == "rover/1-Finch.wav"


def test_clipless_event_still_records(conn):
    row = sightings.parse_event(event(clip=None))
    assert sightings.record(conn, row) is True
    assert conn.execute("SELECT clip FROM sightings").fetchone()["clip"] is None


# --- the wire ----------------------------------------------------------------

def test_parse_event_roundtrip():
    row = sightings.parse_event(event(wind=True))
    assert row == {
        "ts": 1784390000, "source": "amcrest",
        "species_sci": "Poecile atricapillus",
        "species_common": "Black-capped Chickadee",
        "confidence": 0.88, "clip": "amcrest/1784390000-x.wav",
        "wind_suspect": 1,
    }


@pytest.mark.parametrize("payload", [
    b"not json",
    b"{}",
    json.dumps({"kind": "detection"}).encode(),          # missing fields
    json.dumps({"kind": "status", "ts": 1}).encode(),    # not a detection
    json.dumps({"kind": "detection", "ts": "noon", "source": "a",
                "species_sci": "x", "species_common": "y",
                "confidence": 0.9}).encode(),            # unparseable ts
])
def test_parse_event_rejects_malformed(payload):
    assert sightings.parse_event(payload) is None


# --- earl.py's pure helpers --------------------------------------------------

def test_week_of_the_48_week_year():
    assert earl.week_of(1, 1) == 1
    assert earl.week_of(1, 7) == 1
    assert earl.week_of(1, 8) == 2
    assert earl.week_of(1, 22) == 4
    assert earl.week_of(1, 31) == 4     # days 22-31 pool in week 4
    assert earl.week_of(7, 18) == 27
    assert earl.week_of(12, 31) == 48   # the year is exactly 48 weeks


def test_rtsp_argv_redacts_the_password():
    argv, redacted = earl.rtsp_argv("192.168.1.102", "admin", "sekrit")
    assert any("sekrit" in a for a in argv)      # the real argv works
    assert "sekrit" not in redacted              # the loggable twin doesn't
    assert "***" in redacted
    assert argv[0] == "ffmpeg" and argv[-1] == "-"
    assert "-allowed_media_types" in argv        # audio-only, no 4K bandwidth


def test_source_commands_default_is_amcrest(monkeypatch):
    monkeypatch.delenv("MERLE_EARL_SOURCES", raising=False)
    monkeypatch.setenv("MERLE_RTSP_PASS", "x")
    assert list(earl.source_commands()) == ["amcrest"]


def test_source_commands_amcrest_requires_password(monkeypatch):
    monkeypatch.setenv("MERLE_EARL_SOURCES", "amcrest")
    monkeypatch.delenv("MERLE_RTSP_PASS", raising=False)
    with pytest.raises(RuntimeError, match="MERLE_RTSP_PASS"):
        earl.source_commands()


def test_source_commands_unknown_source_fails_at_startup(monkeypatch):
    monkeypatch.setenv("MERLE_EARL_SOURCES", "amcrest,webcam")
    monkeypatch.setenv("MERLE_RTSP_PASS", "x")
    with pytest.raises(RuntimeError, match="webcam"):
        earl.source_commands()


def test_source_commands_rover_is_overridable(monkeypatch):
    monkeypatch.setenv("MERLE_EARL_SOURCES", "rover")
    monkeypatch.setenv("MERLE_EARL_ROVER_CMD", "arecord -D hw:1,0 -t raw")
    argv, redacted = earl.source_commands()["rover"]
    assert argv == ["arecord", "-D", "hw:1,0", "-t", "raw"]
