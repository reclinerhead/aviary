# Tests for weather_archive.py -- the pure logic (row mapping, backfill
# selection, range clamping) plus real-disk round-trips through tmp_path, the
# way test_weather.py exercises the JSON window. The live service on pearl and
# the MCC's read route are desk-tested per the testing policy.

import sqlite3

import pytest

import weather
import weather_archive


def pt(ts, **over):
    """A history point the way weather.history_point() shapes one."""
    point = {
        "ts": ts, "temp_f": 71.0, "wind_mph": 4.0, "wind_gust_mph": 9.0,
        "condition": "Clouds", "humidity_pct": 60.0, "dew_point_f": 56.0,
        "pressure_rel_inhg": 29.92, "rain_rate_inhr": 0.0,
        "rain_day_in": 0.0, "solar_wm2": 120.0, "uv_index": 1.0,
    }
    point.update(over)
    return point


@pytest.fixture
def conn():
    c = weather_archive.connect(":memory:")
    yield c
    c.close()


# --- the archive stores what the window stores ---------------------------------

def test_columns_are_exactly_the_windows_fields():
    # One source of truth for "what an observation is". The archive keeps the
    # record the window keeps -- if HISTORY_FIELDS ever grows a 13th field,
    # this fails until the schema grows with it, rather than the archive
    # quietly dropping data forever.
    assert weather_archive.COLUMNS == weather.HISTORY_FIELDS


# --- row mapping ----------------------------------------------------------------

def test_observation_row_is_the_point_in_column_order():
    row = weather_archive.observation_row(pt(100))
    assert row[0] == 100                       # ts leads, the PK
    assert row == (100, 71.0, 4.0, 9.0, "Clouds", 60.0, 56.0, 29.92,
                   0.0, 0.0, 120.0, 1.0)


def test_observation_row_maps_missing_fields_to_null():
    # A pre-#51 payload replayed through here: five fields, not twelve. NULLs,
    # never a crash -- a gap is real data (the station couldn't say).
    old = {"ts": 100, "temp_f": 71.0, "wind_mph": 4.0,
           "wind_gust_mph": None, "condition": "Clouds"}
    row = weather_archive.observation_row(old)
    assert row[:5] == (100, 71.0, 4.0, None, "Clouds")
    assert row[5:] == (None,) * 7


def test_archivable_drops_tsless_points():
    # ts is an INTEGER PRIMARY KEY: SQLite would autoassign a rowid for a NULL
    # one and file the observation under a moment that never happened.
    assert weather_archive.archivable(
        [pt(100), {"ts": None, "temp_f": 60.0}, pt(200)]
    ) == [pt(100), pt(200)]


# --- idempotent insert ----------------------------------------------------------

def test_record_writes_one_row(conn):
    assert weather_archive.record(conn, pt(100)) is True
    assert weather_archive.observations(conn, 0, 1000) == [pt(100)]


def test_recording_the_same_ts_twice_leaves_one_row(conn):
    # test_roll_history_dedupes_same_ts's invariant, now the PK's job: a
    # restart replaying a point the archive has is a no-op, not a conflict.
    assert weather_archive.record(conn, pt(100, temp_f=71.0)) is True
    assert weather_archive.record(conn, pt(100, temp_f=99.0)) is False
    rows = weather_archive.observations(conn, 0, 1000)
    assert len(rows) == 1
    assert rows[0]["temp_f"] == 71.0   # append-only: first write wins


def test_record_ignores_a_tsless_point(conn):
    assert weather_archive.record(conn, {"ts": None, "temp_f": 60.0}) is False
    assert weather_archive.observations(conn, 0, 1000) == []


# --- range query ----------------------------------------------------------------

def test_observations_bounds_are_inclusive_at_both_ends(conn):
    for ts in (100, 200, 300, 400):
        weather_archive.record(conn, pt(ts))
    # 200 and 300 are exactly the bounds: both kept.
    assert [p["ts"] for p in weather_archive.observations(conn, 200, 300)] \
        == [200, 300]


def test_observations_come_back_oldest_first(conn):
    for ts in (300, 100, 200):        # recorded out of order
        weather_archive.record(conn, pt(ts))
    assert [p["ts"] for p in weather_archive.observations(conn, 0, 1000)] \
        == [100, 200, 300]


def test_observations_of_an_empty_range_is_empty_not_an_error(conn):
    weather_archive.record(conn, pt(100))
    assert weather_archive.observations(conn, 500, 900) == []


def test_observations_of_an_inverted_range_is_empty(conn):
    # A range that contains no time selects nothing -- the honest answer.
    weather_archive.record(conn, pt(100))
    assert weather_archive.observations(conn, 900, 500) == []


def test_observations_of_an_empty_archive_is_empty(conn):
    assert weather_archive.observations(conn, 0, 1000) == []


def test_observations_round_trip_the_full_record(conn):
    weather_archive.record(conn, pt(100))
    assert weather_archive.observations(conn, 0, 1000)[0] == pt(100)


# --- range clamping -------------------------------------------------------------

def test_clamp_leaves_a_sane_range_alone():
    week = 7 * 86400
    assert weather_archive.clamp_range(1000, 1000 + week) == (1000, 1000 + week)


def test_clamp_holds_the_newest_span_of_an_absurd_range():
    # "from the epoch to now" gets the newest 90 days, not a table scan (the
    # /history rule: a typo can't bucket ten years). `to` is the anchor.
    now = 2_000_000_000
    frm, to = weather_archive.clamp_range(0, now)
    assert to == now
    assert frm == now - weather_archive.MAX_SPAN_S


def test_clamp_at_exactly_the_max_span_is_untouched():
    now = 2_000_000_000
    frm = now - weather_archive.MAX_SPAN_S
    assert weather_archive.clamp_range(frm, now) == (frm, now)


def test_clamp_leaves_an_inverted_range_inverted():
    assert weather_archive.clamp_range(900, 500) == (900, 500)


def test_observations_clamps_before_it_scans(conn):
    now = 2_000_000_000
    weather_archive.record(conn, pt(now - 200 * 86400))   # outside the clamp
    weather_archive.record(conn, pt(now - 1 * 86400))     # inside it
    got = weather_archive.observations(conn, 0, now)
    assert [p["ts"] for p in got] == [now - 86400]


# --- backfill -------------------------------------------------------------------

def test_backfill_selection_takes_only_whats_missing():
    window = [pt(100), pt(200), pt(300)]
    missing = weather_archive.backfill_selection(window, known={100, 300})
    assert [p["ts"] for p in missing] == [200]


def test_backfill_selection_of_a_fully_known_window_is_empty():
    assert weather_archive.backfill_selection([pt(100)], known={100}) == []


def test_backfill_selection_drops_tsless_points():
    window = [pt(100), {"ts": None, "temp_f": 60.0}]
    assert weather_archive.backfill_selection(window, known=set()) == [pt(100)]


def test_backfill_fills_an_empty_archive_from_the_window(conn):
    window = [pt(100), pt(200), pt(300)]
    assert weather_archive.backfill(conn, window) == 3
    assert [p["ts"] for p in weather_archive.observations(conn, 0, 1000)] \
        == [100, 200, 300]


def test_backfill_is_idempotent_across_restarts(conn):
    # The startup path runs on EVERY restart, not just the first: replaying a
    # window the archive already holds must insert nothing and claim nothing.
    window = [pt(100), pt(200)]
    assert weather_archive.backfill(conn, window) == 2
    assert weather_archive.backfill(conn, window) == 0
    assert len(weather_archive.observations(conn, 0, 1000)) == 2


def test_backfill_inserts_only_the_new_points(conn):
    weather_archive.backfill(conn, [pt(100), pt(200)])
    # The service ran on: the window has since rolled forward.
    assert weather_archive.backfill(conn, [pt(200), pt(300)]) == 1
    assert [p["ts"] for p in weather_archive.observations(conn, 0, 1000)] \
        == [100, 200, 300]


def test_backfill_of_an_empty_window_does_nothing(conn):
    assert weather_archive.backfill(conn, []) == 0


def test_known_ts_of_nothing_asks_the_db_nothing(conn):
    assert weather_archive.known_ts(conn, []) == set()


# --- config + real disk ----------------------------------------------------------

def test_db_path_default_and_override(monkeypatch):
    monkeypatch.delenv("MERLE_WEATHER_DB", raising=False)
    assert weather_archive.db_path() == "weather.db"
    monkeypatch.setenv("MERLE_WEATHER_DB", "/mnt/nas/weather.db")
    assert weather_archive.db_path() == "/mnt/nas/weather.db"


def test_db_path_blank_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("MERLE_WEATHER_DB", "   ")
    assert weather_archive.db_path() == "weather.db"


def test_an_unopenable_path_fails_at_startup(tmp_path):
    # There is nothing to "parse" in a path, so the env_float contract lands
    # here instead: connect() runs at startup, so a misconfigured archive
    # kills the service at launch rather than looking healthy for a week while
    # recording nothing.
    with pytest.raises(sqlite3.Error):
        weather_archive.connect(str(tmp_path / "no_such_dir" / "weather.db"))


def test_archive_round_trips_on_real_disk(tmp_path):
    path = str(tmp_path / "weather.db")
    conn = weather_archive.connect(path)
    weather_archive.record(conn, pt(100))
    weather_archive.record(conn, pt(400))
    conn.close()

    # A restart: same file, same schema, nothing lost and nothing duplicated.
    again = weather_archive.connect(path)
    assert weather_archive.backfill(again, [pt(100), pt(400)]) == 0
    assert [p["ts"] for p in weather_archive.observations(again, 0, 1000)] \
        == [100, 400]
    again.close()


def test_connect_is_idempotent_on_an_existing_archive(tmp_path):
    path = str(tmp_path / "weather.db")
    conn = weather_archive.connect(path)
    weather_archive.record(conn, pt(100))
    conn.close()
    again = weather_archive.connect(path)      # CREATE TABLE IF NOT EXISTS
    assert len(weather_archive.observations(again, 0, 1000)) == 1
    again.close()
