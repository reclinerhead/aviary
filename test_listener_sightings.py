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
    for t in ("locations", "sightings", "life_list", "species_rules",
              "region_mask", "region_mask_meta"):
        fp[t] = [(r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"])
                 for r in conn.execute("PRAGMA table_info(%s)" % t)]
    return fp


def test_fresh_db_seeds_home(conn):
    row = conn.execute("SELECT name FROM locations WHERE id=1").fetchone()
    assert row["name"] == "Home"


def test_file_store_syncs_every_commit(tmp_path):
    """Issue #322: the life list is the file a power cut must not eat.
    synchronous=FULL (2) fsyncs each commit; NORMAL (1) defers durability to
    the next checkpoint, which a yard writing a few rows a day can go days
    without -- measured, on birdhouse's first cold power-off, as an empty
    store. This is the guard against someone "optimizing" it back."""
    conn = sightings.connect(str(tmp_path / "earl.db"))
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_connect_folds_the_wal_into_the_store(tmp_path):
    """Issue #322's second half: a restart leaves nothing pending in the WAL,
    so an unclean shutdown can't strand committed rows outside the main file
    (and the recovery an unclean boot leaves pending happens at startup, not
    mid-write). A row written, then re-connected, is in the store proper."""
    path = str(tmp_path / "earl.db")
    conn = sightings.connect(path)
    sightings.record(conn, sightings.parse_event(event(ts=100)))
    conn.close()

    wal = tmp_path / "earl.db-wal"
    before = wal.stat().st_size if wal.exists() else 0
    conn = sightings.connect(path)
    after = wal.stat().st_size if wal.exists() else 0
    assert after < before or after == 0     # truncated, not merely appended
    assert conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0] == 1


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


# --- per-species rules (issue #506) ------------------------------------------
# connect() creates species_rules as part of the shape every store lands on
# (the fingerprint test asserts fresh == upgraded); the Aviary writes it and
# the listener reads it through earl.load_species_rules -- covered here
# because this file already owns the store's schema and earl's pure helpers.

def test_species_rules_table_exists_and_roundtrips(conn):
    conn.execute(
        "INSERT INTO species_rules (species_sci, exclude, threshold,"
        " always_include, updated_ts) VALUES ('Aix sponsa', 1, NULL, 0, 5)")
    conn.commit()
    row = conn.execute("SELECT * FROM species_rules").fetchone()
    assert (row["species_sci"], row["exclude"], row["threshold"],
            row["always_include"]) == ("Aix sponsa", 1, None, 0)


def test_species_rules_created_on_an_upgraded_old_store(tmp_path):
    # A pre-#506 (indeed pre-#232) file gains the table on connect -- the
    # CREATE IF NOT EXISTS is the whole upgrade for a new table.
    path = str(tmp_path / "old-earl.db")
    _make_pre232_db(path)
    conn = sightings.connect(path)
    assert conn.execute("SELECT COUNT(*) c FROM species_rules"
                        ).fetchone()["c"] == 0
    conn.close()


def test_load_species_rules_maps_rows_to_decide_shape(tmp_path):
    path = str(tmp_path / "earl.db")
    conn = sightings.connect(path)
    conn.executescript("""
        INSERT INTO species_rules VALUES ('Aix sponsa', 1, NULL, 0, 5);
        INSERT INTO species_rules VALUES ('Poecile atricapillus', 0, 0.8, 0, 5);
        INSERT INTO species_rules VALUES ('Antigone canadensis', 0, NULL, 1, 5);
    """)
    conn.commit()
    conn.close()
    assert earl.load_species_rules(path) == {
        "Aix sponsa": {"exclude": True, "threshold": None,
                       "always_include": False},
        "Poecile atricapillus": {"exclude": False, "threshold": 0.8,
                                 "always_include": False},
        "Antigone canadensis": {"exclude": False, "threshold": None,
                                "always_include": True},
    }


def test_load_species_rules_quiet_on_missing_store_or_table(tmp_path):
    # Unset path, a file that doesn't exist yet, and a pre-#506 store
    # without the table are all "no rules yet", never an error -- and the
    # read-only open must not create the store as a side effect.
    assert earl.load_species_rules(None) == {}
    missing = str(tmp_path / "nope.db")
    assert earl.load_species_rules(missing) == {}
    assert not os.path.exists(missing)
    bare = str(tmp_path / "pre506.db")
    raw = sightings.sqlite3.connect(bare)
    raw.execute("CREATE TABLE sightings (id INTEGER PRIMARY KEY)")
    raw.commit()
    raw.close()
    assert earl.load_species_rules(bare) == {}


# --- the region mask (issue #593) --------------------------------------------
# earl.py shapes the mask for the bus (pure helpers, covered here beside its
# other pure helpers); sightings persists it as current state only. The
# gate's own set is deliberately NOT derived from these rows -- region_mask()
# builds it from the label column exactly as before and shapes the rows
# second, best-effort.

def _mask_rows():
    return [
        {"species_sci": "Poecile atricapillus",
         "species_common": "Black-capped Chickadee", "geo": 0.97},
        {"species_sci": "Strix varia", "species_common": "Barred Owl",
         "geo": 0.41},
    ]


def test_mask_rows_split_sort_round_and_dedupe():
    rows = earl.mask_rows_from([
        ("Strix varia_Barred Owl", 0.41234),
        ("Poecile atricapillus_Black-capped Chickadee", 0.9712),
        ("Strix varia_Barred Owl", 0.2),       # a repeat keeps its best
        ("Noise", 0.5),                        # no underscore: both halves
    ])
    assert rows == [
        {"species_sci": "Noise", "species_common": "Noise", "geo": 0.5},
        {"species_sci": "Poecile atricapillus",
         "species_common": "Black-capped Chickadee", "geo": 0.9712},
        {"species_sci": "Strix varia", "species_common": "Barred Owl",
         "geo": 0.4123},
    ]


def test_mask_rows_from_nothing_is_an_empty_list():
    assert earl.mask_rows_from([]) == []


def test_mask_payload_masked_shape():
    body = earl.mask_payload((42.29, -85.59), 43, _mask_rows(), now=1700)
    assert body == {
        "ts": 1700, "week": 43, "lat": 42.29, "lon": -85.59,
        "status": "masked", "species": _mask_rows(), "error": None,
    }
    json.dumps(body)   # must be wire-safe as-is


def test_mask_payload_unmasked_carries_the_error_and_no_species():
    body = earl.mask_payload((1.0, 2.0), 7, None,
                             error=RuntimeError("no geo model"), now=5)
    assert body["status"] == "unmasked"
    assert body["species"] == []
    assert body["error"] == "no geo model"
    assert body["week"] == 7


def test_report_mask_hands_the_payload_up_the_queue():
    class Q:
        def __init__(self):
            self.items = []

        def put(self, item):
            self.items.append(item)

    q = Q()
    lines = []
    assert earl.report_mask(q, "house-front", {"week": 1}, lines.append)
    assert q.items == [("mask", "house-front", {"week": 1})]
    assert lines == []


def test_report_mask_failure_is_one_line_never_a_raise():
    class BrokenQ:
        def put(self, item):
            raise OSError("pipe closed")

    lines = []
    assert earl.report_mask(BrokenQ(), "house-front", {}, lines.append) is False
    assert len(lines) == 1 and "mask report failed" in lines[0]


def _mask_msg(**over):
    body = {"ts": 1700, "week": 43, "lat": 42.29, "lon": -85.59,
            "status": "masked", "species": _mask_rows(), "error": None}
    body.update(over)
    return json.dumps(body)


def test_parse_mask_roundtrip():
    assert sightings.parse_mask(_mask_msg()) == {
        "ts": 1700, "week": 43, "status": "masked", "error": None,
        "species": _mask_rows(),
    }


def test_parse_mask_ignores_extra_keys_on_a_row():
    rows = [dict(_mask_rows()[0], extra="ignored")]
    parsed = sightings.parse_mask(_mask_msg(species=rows))
    assert parsed["species"] == [_mask_rows()[0]]


def test_parse_mask_unmasked():
    parsed = sightings.parse_mask(
        _mask_msg(status="unmasked", species=[], error="no geo model"))
    assert parsed["status"] == "unmasked"
    assert parsed["species"] == []
    assert parsed["error"] == "no geo model"


@pytest.mark.parametrize("payload", [
    b"not json",
    b"[]",
    _mask_msg(week=None),
    _mask_msg(status="partial"),
    _mask_msg(species={"a": 1}),
    _mask_msg(species=[{"species_common": "Barred Owl", "geo": 0.4}]),
    _mask_msg(species=[{"species_sci": "Strix varia",
                        "species_common": "Barred Owl", "geo": "high"}]),
    event(),   # a detection on the wrong topic is not a mask
])
def test_parse_mask_rejects_malformed(payload):
    assert sightings.parse_mask(payload) is None


def _stored_mask(conn):
    rows = [(r["species_sci"], r["species_common"], r["geo"])
            for r in conn.execute("SELECT * FROM region_mask"
                                  " ORDER BY species_sci")]
    meta = conn.execute("SELECT * FROM region_mask_meta").fetchone()
    return rows, (dict(meta) if meta else None)


def test_record_mask_lands_rows_and_meta(conn):
    sightings.record_mask(conn, sightings.parse_mask(_mask_msg()))
    rows, meta = _stored_mask(conn)
    assert rows == [("Poecile atricapillus", "Black-capped Chickadee", 0.97),
                    ("Strix varia", "Barred Owl", 0.41)]
    assert meta == {"id": 1, "ts": 1700, "week": 43, "status": "masked",
                    "error": None}


def test_record_mask_replaces_the_whole_table(conn):
    sightings.record_mask(conn, sightings.parse_mask(_mask_msg()))
    nxt = [{"species_sci": "Cardinalis cardinalis",
            "species_common": "Northern Cardinal", "geo": 0.99}]
    sightings.record_mask(
        conn, sightings.parse_mask(_mask_msg(ts=1800, week=44, species=nxt)))
    rows, meta = _stored_mask(conn)
    assert rows == [("Cardinalis cardinalis", "Northern Cardinal", 0.99)]
    assert (meta["ts"], meta["week"]) == (1800, 44)


def test_record_mask_unmasked_empties_the_table_and_says_why(conn):
    sightings.record_mask(conn, sightings.parse_mask(_mask_msg()))
    sightings.record_mask(conn, sightings.parse_mask(
        _mask_msg(ts=1900, status="unmasked", species=[],
                  error="no geo model")))
    rows, meta = _stored_mask(conn)
    assert rows == []
    assert (meta["status"], meta["error"]) == ("unmasked", "no geo model")


def test_record_mask_never_touches_the_record(conn):
    sightings.record(conn, sightings.parse_event(event()))
    sightings.record_mask(conn, sightings.parse_mask(_mask_msg()))
    assert conn.execute("SELECT COUNT(*) c FROM sightings").fetchone()["c"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM life_list").fetchone()["c"] == 1


def test_mask_tables_created_on_an_upgraded_old_store(tmp_path):
    path = str(tmp_path / "old-earl.db")
    _make_pre232_db(path)
    conn = sightings.connect(path)
    assert conn.execute("SELECT COUNT(*) c FROM region_mask"
                        ).fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM region_mask_meta"
                        ).fetchone()["c"] == 0
    conn.close()


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


# --- what earns a journal line (issue #324) ----------------------------------

def test_sound_events_are_ours_to_ignore_silently():
    """#174's coarse tier is bus-only by design, so a cricket must not be
    announced as a malformed message -- a summer yard produces them all
    night and the noise buries the lines that matter."""
    payload = json.dumps({"ts": 1, "source": "window-mic", "kind": "sound",
                          "class": "Cricket", "confidence": 0.34})
    assert sightings.parse_event(payload) is None    # not persisted
    assert sightings.is_other_kind(payload) is True  # ...and not an incident


def test_an_unknown_future_kind_is_also_ignored_silently():
    # Ignoring unknown kinds IS the two-tier schema's forward-compatibility
    # guard (#172): a kind invented after this code shipped must flow past
    # without a word, exactly like "sound" does.
    assert sightings.is_other_kind(json.dumps({"kind": "seismic"})) is True


def test_real_garbage_still_earns_its_loud_line():
    # The cases that mean something is actually wrong stay loud: bytes that
    # aren't JSON, and a DETECTION missing the fields a detection must have.
    assert sightings.is_other_kind(b"not json at all") is False
    assert sightings.is_other_kind(json.dumps([1, 2, 3])) is False
    broken = json.dumps({"kind": "detection", "ts": "yesterday"})
    assert sightings.parse_event(broken) is None
    assert sightings.is_other_kind(broken) is False


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


def test_quarantine_shelf_is_never_pruned():
    # The quarantine/ shelf (issue #281) holds a removal's evidence -- the
    # negatives-in-waiting for the false-positive loop (epic #278). Aged far
    # past any horizon it stays, its -enh sibling with it; the
    # identically-aged clip beside the shelf goes.
    now = 1000 * DAY
    files = [("quarantine/amcrest/1-Aix_sponsa.wav", 0),
             ("quarantine/amcrest/1-Aix_sponsa-enh.wav", 0),
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


def test_exempt_clips_includes_the_showcase_when_the_pass_has_run(conn):
    # #551: the three best recordings per species are part of the permanent
    # record too. The table is the showcase pass's, created by its connect();
    # here it is created by hand so this test stays paho-only.
    sightings.record(conn, sightings.parse_event(
        event(clip="amcrest/1-first.wav")))
    conn.execute("CREATE TABLE species_showcase (species_sci TEXT, slot "
                 "INTEGER, clip TEXT, chosen_ts INTEGER)")
    conn.execute("INSERT INTO species_showcase VALUES "
                 "('Poecile atricapillus', 1, 'amcrest/9-song.wav', 1), "
                 "('Poecile atricapillus', 2, 'amcrest/8-chip.wav', 1), "
                 "('Poecile atricapillus', 3, NULL, 1)")
    assert sightings.exempt_clips(conn) == {
        "amcrest/1-first.wav", "amcrest/9-song.wav", "amcrest/8-chip.wav"}


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
    aged, over, still_over = sightings.prune_clips(
        str(clips), store, 90, now_ts=now)
    assert (aged, over, still_over) == (1, 0, False)
    assert (clips / "amcrest" / "1-Lifer.wav").exists()
    assert not (clips / "amcrest" / "2-Common.wav").exists()
    assert (clips / "amcrest" / "3-Young.wav").exists()


def test_prune_clips_missing_dir_is_zero_not_error(tmp_path):
    store = str(tmp_path / "earl.db")
    sightings.connect(store).close()
    assert sightings.prune_clips(str(tmp_path / "nope"), store, 90) == \
        (0, 0, False)


# --- the disk budget (issue #330) --------------------------------------------

def sized(*rows):
    """(relpath, mtime, size) triples, the budget arm's input shape."""
    return list(rows)


def test_budget_evicts_oldest_first_until_it_fits():
    files = sized(("a/1-old.wav", 100, 40), ("a/2-mid.wav", 200, 40),
                  ("a/3-new.wav", 300, 40))
    # 120 bytes on disk, cap 100: the oldest single file closes the gap.
    assert sightings.budget_selection(files, set(), 100) == ["a/1-old.wav"]
    # Cap 50 needs two of them, still oldest-first.
    assert sightings.budget_selection(files, set(), 50) == \
        ["a/1-old.wav", "a/2-mid.wav"]


def test_budget_does_nothing_when_the_tree_fits():
    files = sized(("a/1.wav", 100, 10), ("a/2.wav", 200, 10))
    assert sightings.budget_selection(files, set(), 1000) == []


def test_no_cap_means_no_budget_arm():
    # Unset cap is the pre-#330 behavior: age horizon only, whatever the size.
    files = sized(("a/1.wav", 100, 10 ** 9))
    assert sightings.budget_selection(files, set(), None) == []
    assert sightings.budget_selection(files, set(), 0) == []


def test_budget_never_evicts_a_lifer_first_clip():
    """The exemption is absolute under pressure -- the whole point. A disk
    number must never cost a first-heard recording."""
    files = sized(("a/1-lifer.wav", 100, 60), ("a/2-common.wav", 200, 60))
    assert sightings.budget_selection(files, {"a/1-lifer.wav"}, 50) == \
        ["a/2-common.wav"]


def test_budget_reports_what_it_can_and_leaves_the_sacred_set():
    """When the exempt clips ALONE exceed the cap, take everything allowed
    and stop -- the caller logs that the tree is still over. Never a
    delete-the-evidence-to-hit-a-number path."""
    files = sized(("a/1-lifer.wav", 100, 500), ("a/2-common.wav", 200, 10))
    assert sightings.budget_selection(files, {"a/1-lifer.wav"}, 50) == \
        ["a/2-common.wav"]


def test_budget_takes_the_enhanced_sibling_with_its_original():
    files = sized(("a/1.wav", 100, 30), ("a/1-enh.wav", 150, 30),
                  ("a/2.wav", 200, 30))
    # Evicting the pair frees 60 of the 90 on disk, landing under a 40 cap.
    assert sightings.budget_selection(files, set(), 40) == \
        ["a/1.wav", "a/1-enh.wav"]


def test_budget_counts_but_never_evicts_portraits():
    """The species/ shelf is permanent (#184) -- yet it occupies the volume,
    so it must COUNT. Measuring only the evictable half would make the cap a
    lie the moment the permanent half grew."""
    files = sized(("species/Cardinalis_cardinalis.jpg", 50, 80),
                  ("a/1.wav", 100, 30))
    # 110 on disk against a 100 cap: the portrait is untouchable, so the
    # clip goes even though the clips alone would have fit.
    assert sightings.budget_selection(files, set(), 100) == ["a/1.wav"]


def test_budget_counts_but_never_evicts_quarantine():
    """The quarantine/ shelf (#281) is the species/ rule's second tenant:
    removal evidence occupies the volume, so it counts against the cap, but
    the budget arm may never evict it -- deleting a hand-labeled negative to
    satisfy a disk number would silently undo the operator's work."""
    files = sized(("quarantine/amcrest/1-Aix_sponsa.wav", 50, 80),
                  ("a/1.wav", 100, 30))
    assert sightings.budget_selection(files, set(), 100) == ["a/1.wav"]


def test_budget_skips_what_the_age_sweep_already_doomed():
    """The two arms compose: whatever the horizon claimed is already gone
    from the budget's arithmetic, so it can't be double-counted or
    double-deleted."""
    files = sized(("a/1-old.wav", 100, 60), ("a/2-new.wav", 300, 30))
    assert sightings.budget_selection(files, set(), 50,
                                      already_doomed=["a/1-old.wav"]) == []


def test_budget_and_age_arms_compose_end_to_end(tmp_path):
    clips = tmp_path / "clips"
    (clips / "amcrest").mkdir(parents=True)
    for name in ("1-Lifer.wav", "2-Old.wav", "3-New.wav"):
        (clips / "amcrest" / name).write_bytes(b"x" * 40)
    old = 1000.0
    for name in ("1-Lifer.wav", "2-Old.wav"):
        os.utime(clips / "amcrest" / name, (old, old))

    store = str(tmp_path / "earl.db")
    conn = sightings.connect(store)
    sightings.record(conn, sightings.parse_event(
        event(clip="amcrest/1-Lifer.wav")))
    conn.close()

    now = old + 200 * DAY
    # Cap 30 against a 40-byte lifer: even after taking everything it is
    # allowed to take, the sacred set alone busts the budget.
    aged, over, still_over = sightings.prune_clips(
        str(clips), store, 90, now_ts=now, cap_bytes=30)
    # Age took the old commoner; the budget then took the young one; the
    # lifer survived both arms and the tree is honestly still over cap.
    assert (aged, over) == (1, 1)
    assert still_over is True
    assert (clips / "amcrest" / "1-Lifer.wav").exists()
    assert not (clips / "amcrest" / "2-Old.wav").exists()
    assert not (clips / "amcrest" / "3-New.wav").exists()


def test_clips_cap_env_parses_and_fails_loud(monkeypatch):
    monkeypatch.delenv("MERLE_EARL_CLIPS_CAP_GB", raising=False)
    assert sightings.clips_cap_bytes() is None      # unset = no cap
    monkeypatch.setenv("MERLE_EARL_CLIPS_CAP_GB", "40")
    assert sightings.clips_cap_bytes() == 40_000_000_000
    for bad in ("banana", "-5", "0"):
        monkeypatch.setenv("MERLE_EARL_CLIPS_CAP_GB", bad)
        with pytest.raises(RuntimeError, match="CLIPS_CAP_GB"):
            sightings.clips_cap_bytes()


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
