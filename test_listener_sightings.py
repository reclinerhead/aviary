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
import os

import pytest

from listener import earl, sightings


def event(ts=1784390000, source="amcrest",
          sci="Poecile atricapillus", common="Black-capped Chickadee",
          confidence=0.88, clip="amcrest/1784390000-x.wav", wind=False,
          **extra):
    return json.dumps({
        "ts": ts, "source": source, "kind": "detection",
        "species_sci": sci, "species_common": common,
        "confidence": confidence, "window_s": 3,
        "clip": clip, "wind_suspect": wind, **extra,
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


# --- locations & per-place first-heard (issue #232) --------------------------

def _make_pre232_db(path):
    """A post-#175, pre-Field-Mode earl.db: sightings has rms but no
    location_id, life_list is single-PK, and there is real data in both. The
    upgrade path has to carry all of it forward as Home."""
    raw = sightings.sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE sightings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
            source TEXT NOT NULL, species_sci TEXT NOT NULL,
            species_common TEXT NOT NULL, confidence REAL NOT NULL,
            clip TEXT, wind_suspect INTEGER NOT NULL DEFAULT 0, rms REAL);
        CREATE INDEX sightings_ts ON sightings(ts);
        CREATE INDEX sightings_species ON sightings(species_sci, ts);
        CREATE TABLE life_list (
            species_sci TEXT PRIMARY KEY, species_common TEXT NOT NULL,
            first_ts INTEGER NOT NULL, first_source TEXT NOT NULL,
            first_clip TEXT);
    """)
    raw.execute("INSERT INTO sightings (ts, source, species_sci,"
                " species_common, confidence, clip, rms) VALUES"
                " (100,'amcrest','Poecile atricapillus',"
                "'Black-capped Chickadee',0.9,'amcrest/100-x.wav',0.02)")
    raw.execute("INSERT INTO life_list (species_sci, species_common,"
                " first_ts, first_source, first_clip) VALUES"
                " ('Poecile atricapillus','Black-capped Chickadee',100,"
                "'amcrest','amcrest/100-x.wav')")
    raw.commit()
    raw.close()


def _fingerprint(conn):
    """Every table's columns (name, type, notnull, default, pk position) --
    the shape a fresh db and an upgraded old one must land on identically."""
    fp = {}
    for t in ("locations", "sightings", "life_list"):
        fp[t] = [(r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"])
                 for r in conn.execute("PRAGMA table_info(%s)" % t)]
    return fp


def test_fresh_db_seeds_home(conn):
    row = conn.execute("SELECT name FROM locations WHERE id=1").fetchone()
    assert row["name"] == "Home"


def test_home_seeded_from_latlon():
    conn = sightings.connect(":memory:", home_latlon=(42.96, -85.67))
    row = conn.execute("SELECT lat, lon FROM locations WHERE id=1").fetchone()
    assert (row["lat"], row["lon"]) == (42.96, -85.67)


def test_home_placeholder_self_heals_when_coords_arrive(tmp_path):
    # First upgrade with no MERLE_LATLON -> placeholder; a later restart WITH
    # the env corrects it, so deploy ordering doesn't matter.
    path = str(tmp_path / "earl.db")
    sightings.connect(path).close()
    assert sightings.connect(path).execute(
        "SELECT lat FROM locations WHERE id=1").fetchone()["lat"] == 0.0
    conn = sightings.connect(path, home_latlon=(42.96, -85.67))
    row = conn.execute("SELECT lat, lon FROM locations WHERE id=1").fetchone()
    assert (row["lat"], row["lon"]) == (42.96, -85.67)


def test_sighting_defaults_to_home(conn):
    sightings.record(conn, sightings.parse_event(event()))
    assert conn.execute(
        "SELECT location_id FROM sightings").fetchone()["location_id"] == 1


def test_sighting_lands_at_its_location(conn):
    sightings.record(conn, sightings.parse_event(event(location_id=2)))
    assert conn.execute(
        "SELECT location_id FROM sightings").fetchone()["location_id"] == 2


def test_per_location_first_heard(conn):
    # Same species, two places -> two lifers, each with its own first_ts.
    a = sightings.record(conn, sightings.parse_event(event(ts=100)))
    b = sightings.record(conn, sightings.parse_event(
        event(ts=200, location_id=2)))
    assert a is True and b is True
    rows = conn.execute("SELECT location_id, first_ts FROM life_list"
                        " ORDER BY location_id").fetchall()
    assert [(r["location_id"], r["first_ts"]) for r in rows] == \
        [(1, 100), (2, 200)]


def test_park_lifer_does_not_touch_home(conn):
    # Acceptance criterion: a park session produces a park lifer without
    # disturbing Home's roster -- the whole point of the model.
    sightings.record(conn, sightings.parse_event(event(ts=100)))
    sightings.record(conn, sightings.parse_event(event(ts=150)))
    is_lifer = sightings.record(conn, sightings.parse_event(
        event(ts=900, location_id=2, clip="rover/900-Chickadee.wav")))
    assert is_lifer is True
    home = conn.execute("SELECT first_ts, first_clip FROM life_list"
                        " WHERE location_id=1").fetchone()
    assert home["first_ts"] == 100                       # unmoved
    assert home["first_clip"] == "amcrest/1784390000-x.wav"
    assert conn.execute(
        "SELECT COUNT(*) c FROM life_list").fetchone()["c"] == 2


def test_retention_exemption_is_per_location(conn):
    # exempt_clips must spare the first clip of each species AT EACH location.
    sightings.record(conn, sightings.parse_event(
        event(ts=100, clip="amcrest/home-first.wav")))
    sightings.record(conn, sightings.parse_event(
        event(ts=900, location_id=2, clip="rover/park-first.wav")))
    assert sightings.exempt_clips(conn) == \
        {"amcrest/home-first.wav", "rover/park-first.wav"}


def test_pre232_db_upgrades_in_place(tmp_path):
    path = str(tmp_path / "old-earl.db")
    _make_pre232_db(path)
    for _ in range(2):   # idempotent: the upgrade is a restart, not a script
        conn = sightings.connect(path)
        scols = {r["name"] for r in
                 conn.execute("PRAGMA table_info(sightings)")}
        assert "location_id" in scols
        # the pre-existing sighting reads as Home by the column default
        assert conn.execute(
            "SELECT location_id FROM sightings").fetchone()["location_id"] == 1
        # life_list re-keyed, its one row preserved as Home, first-heard intact
        lcols = {r["name"] for r in
                 conn.execute("PRAGMA table_info(life_list)")}
        assert "location_id" in lcols
        life = conn.execute("SELECT location_id, first_ts, first_clip,"
                            " first_source FROM life_list").fetchone()
        assert life["location_id"] == 1
        assert life["first_ts"] == 100
        assert life["first_clip"] == "amcrest/100-x.wav"
        assert life["first_source"] == "amcrest"
        assert conn.execute(
            "SELECT name FROM locations WHERE id=1").fetchone()["name"] == "Home"
        conn.close()


def test_fresh_and_upgraded_schema_are_identical(tmp_path):
    old_path = str(tmp_path / "old-earl.db")
    _make_pre232_db(old_path)
    assert _fingerprint(sightings.connect(old_path)) == \
        _fingerprint(sightings.connect(":memory:"))


def test_upgraded_db_still_takes_a_park_session(tmp_path):
    # End to end: after upgrading a real old file, a park event records
    # cleanly and stays out of Home's life list.
    path = str(tmp_path / "old-earl.db")
    _make_pre232_db(path)
    conn = sightings.connect(path)
    is_lifer = sightings.record(conn, sightings.parse_event(
        event(ts=900, location_id=2, sci="Haemorhous mexicanus",
              common="House Finch", clip="rover/900-Finch.wav")))
    assert is_lifer is True
    assert conn.execute("SELECT COUNT(*) c FROM life_list WHERE location_id=1"
                        ).fetchone()["c"] == 1   # Home untouched (chickadee)
    assert conn.execute("SELECT COUNT(*) c FROM life_list WHERE location_id=2"
                        ).fetchone()["c"] == 1   # the park finch


# --- the wire ----------------------------------------------------------------

def test_parse_event_roundtrip():
    row = sightings.parse_event(event(wind=True, rms=0.0153))
    assert row == {
        "ts": 1784390000, "source": "amcrest",
        "species_sci": "Poecile atricapillus",
        "species_common": "Black-capped Chickadee",
        "confidence": 0.88, "clip": "amcrest/1784390000-x.wav",
        "wind_suspect": 1, "rms": 0.0153, "location_id": 1,
    }


def test_parse_event_without_location_id_is_home():
    # Live Earl never sets location_id; those detections are Home (issue #232).
    assert sightings.parse_event(event())["location_id"] == 1


def test_parse_event_carries_an_explicit_location_id():
    # The import pass (Phase 3) stamps a park's id on replayed events.
    assert sightings.parse_event(event(location_id=2))["location_id"] == 2


def test_parse_event_without_rms_is_null_not_rejected():
    # Pre-#175 producers emit no rms; those events must keep landing.
    row = sightings.parse_event(event())
    assert row["rms"] is None


def test_rms_roundtrips_to_the_store(conn):
    sightings.record(conn, sightings.parse_event(event(ts=100, rms=0.0153)))
    sightings.record(conn, sightings.parse_event(
        event(ts=200, sci="Haemorhous mexicanus", common="House Finch")))
    rows = conn.execute("SELECT rms FROM sightings ORDER BY ts").fetchall()
    assert rows[0]["rms"] == 0.0153
    assert rows[1]["rms"] is None


def test_rms_column_added_to_a_pre175_store(tmp_path):
    # A day-one earl.db lacks the column; connect() must upgrade it in
    # place, idempotently -- a restart, not a migration script.
    path = str(tmp_path / "old-earl.db")
    raw = sightings.sqlite3.connect(path)
    raw.execute("""CREATE TABLE sightings (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
        source TEXT NOT NULL, species_sci TEXT NOT NULL,
        species_common TEXT NOT NULL, confidence REAL NOT NULL,
        clip TEXT, wind_suspect INTEGER NOT NULL DEFAULT 0)""")
    raw.execute("INSERT INTO sightings (ts, source, species_sci,"
                " species_common, confidence) VALUES (1,'a','x','y',0.9)")
    raw.commit()
    raw.close()

    for _ in range(2):   # and running the upgrade twice is a no-op
        conn = sightings.connect(path)
        columns = {r["name"] for r in
                   conn.execute("PRAGMA table_info(sightings)")}
        assert "rms" in columns
        row = conn.execute("SELECT rms, confidence FROM sightings").fetchone()
        assert row["rms"] is None       # old rows honestly NULL
        assert row["confidence"] == 0.9  # and untouched
        conn.close()


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


# --- clip retention (issue #175) ---------------------------------------------

DAY = 86400


def test_prune_selection_age_math():
    now = 100 * DAY
    files = [("a/old.wav", now - 91 * DAY), ("a/young.wav", now - 89 * DAY)]
    assert sightings.prune_selection(files, now, 90, set()) == ["a/old.wav"]


def test_species_portraits_are_never_pruned():
    # The species/ shelf (issue #184) shares the clips dir but is a permanent
    # collection, not a rolling window -- a portrait aged past any horizon
    # stays; the identically-aged clip beside it goes.
    now = 1000 * DAY
    files = [("species/Cardinalis_cardinalis.jpg", 0),
             ("amcrest/2-Common.wav", 0)]
    assert sightings.prune_selection(files, now, 90, set()) == \
        ["amcrest/2-Common.wav"]


def test_lifer_first_clips_survive_forever():
    # The sacred exemption: a lifer's first recording outlives any horizon;
    # the identical path un-lifered would not.
    now = 1000 * DAY
    files = [("amcrest/1-Lifer.wav", 0), ("amcrest/2-Common.wav", 0)]
    exempt = {"amcrest/1-Lifer.wav"}
    assert sightings.prune_selection(files, now, 90, exempt) == \
        ["amcrest/2-Common.wav"]


def test_a_doomed_clip_takes_its_enhanced_sibling_with_it():
    # Issue #190: the enhancement pass writes a sibling per clip. A pass that
    # quietly doubles disk growth on a shared 48G LV is a bug, so the sibling
    # goes when its original goes -- even though it is always the NEWER file
    # (written by a later pass run) and would survive an age test of its own.
    now = 1000 * DAY
    files = [("amcrest/2-Common.wav", 0),
             ("amcrest/2-Common-enh.wav", now - DAY)]
    assert sorted(sightings.prune_selection(files, now, 90, set())) == \
        ["amcrest/2-Common-enh.wav", "amcrest/2-Common.wav"]


def test_a_lifers_enhanced_sibling_is_exempt_too():
    # The exemption is about the RECORDING, not one file: pruning the
    # enhanced half of a permanent lifer clip would quietly degrade the
    # permanent record to its least listenable version.
    now = 1000 * DAY
    files = [("amcrest/1-Lifer.wav", 0), ("amcrest/1-Lifer-enh.wav", 0)]
    assert sightings.prune_selection(files, now, 90,
                                     {"amcrest/1-Lifer.wav"}) == []


def test_a_young_clips_sibling_survives_with_it():
    now = 1000 * DAY
    files = [("amcrest/3-Recent.wav", now - DAY),
             ("amcrest/3-Recent-enh.wav", now - DAY)]
    assert sightings.prune_selection(files, now, 90, set()) == []


def test_an_orphaned_sibling_ages_out_on_its_own():
    # Its original is already gone (a hand-deleted file, say), so there is
    # nothing to inherit a verdict from -- the horizon applies directly.
    now = 1000 * DAY
    files = [("amcrest/4-Gone-enh.wav", 0), ("amcrest/5-Fresh-enh.wav", now)]
    assert sightings.prune_selection(files, now, 90, set()) == \
        ["amcrest/4-Gone-enh.wav"]


def test_exempt_clips_reads_the_life_list(conn):
    sightings.record(conn, sightings.parse_event(
        event(clip="amcrest/1-first.wav")))
    sightings.record(conn, sightings.parse_event(
        event(ts=2, clip="amcrest/2-later.wav")))   # same species: not first
    assert sightings.exempt_clips(conn) == {"amcrest/1-first.wav"}


def test_prune_clips_end_to_end(tmp_path):
    # Real files, real store: the old commoner dies, the equally old lifer
    # and the young file live.
    clips = tmp_path / "clips"
    (clips / "amcrest").mkdir(parents=True)
    for name in ("1-Lifer.wav", "2-Common.wav", "3-Young.wav"):
        (clips / "amcrest" / name).write_bytes(b"RIFF")
    old = 1000.0
    os.utime(clips / "amcrest" / "1-Lifer.wav", (old, old))
    os.utime(clips / "amcrest" / "2-Common.wav", (old, old))

    store = str(tmp_path / "earl.db")
    conn = sightings.connect(store)
    sightings.record(conn, sightings.parse_event(
        event(clip="amcrest/1-Lifer.wav")))
    conn.close()

    now = old + 200 * DAY
    pruned = sightings.prune_clips(str(clips), store, 90, now_ts=now)
    assert pruned == 1
    assert (clips / "amcrest" / "1-Lifer.wav").exists()
    assert not (clips / "amcrest" / "2-Common.wav").exists()
    assert (clips / "amcrest" / "3-Young.wav").exists()


def test_prune_clips_missing_dir_is_zero_not_error(tmp_path):
    store = str(tmp_path / "earl.db")
    sightings.connect(store).close()
    assert sightings.prune_clips(str(tmp_path / "nope"), store, 90) == 0


# --- earl.py's pure helpers --------------------------------------------------

def test_week_of_the_48_week_year():
    assert earl.week_of(1, 1) == 1
    assert earl.week_of(1, 7) == 1
    assert earl.week_of(1, 8) == 2
    assert earl.week_of(1, 22) == 4
    assert earl.week_of(1, 31) == 4     # days 22-31 pool in week 4
    assert earl.week_of(7, 18) == 27
    assert earl.week_of(12, 31) == 48   # the year is exactly 48 weeks


def test_rtsp_argv_is_an_audio_only_pull():
    argv, _redacted = earl.rtsp_argv("rtsp://pearl:8554/house-rear")
    assert argv[0] == "ffmpeg" and argv[-1] == "-"
    assert "-allowed_media_types" in argv        # audio-only, no 4K bandwidth


# source_commands() and rtsp_argv's redaction moved to test_listener_earl.py
# with #270: sources come from the feed registry (feeds.yml, covered by
# test_feeds.py), and the old env-driven source list -- including the
# MERLE_EARL_ROVER_CMD override, now just the registry's cmd field -- is gone.
