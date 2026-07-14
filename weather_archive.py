# =============================================================================
# project-squirrel -- weather_archive.py
#
# The seasonal archive (issue #105): every observation the station reports,
# kept forever, at the same 5-minute resolution the rolling window keeps.
# weather.py's 48h JSON window is a bounded, rewritten-whole payload for the
# retained bus topic -- an honest data structure for what it does, and it
# prunes itself on every append. Nothing older than two days survived it.
# This is the other half: the same tick, written to a store that never prunes.
#
# The two are NOT alternatives. The window stays exactly as it is (the bus
# contract: a dashboard tab rehydrates its trail from the broker with no
# fetch); the archive is a second consumer of the same `should_record` tick.
#
# Why 5-minute rows and not an hourly rollup (issue #72's shape): 5-minute
# data is a strict superset -- hourly means, min/max, and vector-averaged wind
# all derive from it at read time -- and it's what the station already records
# and what the charts already draw. Hourly would throw away resolution we have
# and cap any pressure curve at a day's zoom. ~105k rows/year, which SQLite
# does not notice.
#
# Why pearl and not merle.db: `merle.db` lives on bluejay next to the daemon,
# and daemon-down is the STEADY STATE for the 24/7 dashboard -- archiving
# weather behind the daemon's HTTP surface would put holes in the record
# exactly when bluejay naps, which is most of the time. weather.py runs on
# pearl, 24/7, so the rows live where the writer lives. This is
# frame_archiver.py's reasoning, applied to bytes of a different kind: a
# pearl-resident writer, a pearl-local store, and an MCC route that reads it
# off local disk (never a /daemon/* endpoint).
#
# Timestamps are UNIX EPOCH SECONDS, deliberately departing from storage.py's
# ISO-8601 convention: the weather bus is epoch end to end (weather.py:75-78),
# the points arrive as epoch, and the chart consumes epoch. Converting to ISO
# at the store boundary and back at the route boundary would be two lies for
# zero benefit.
#
# Config (env, the MERLE_WEATHER_HISTORY convention):
#   MERLE_WEATHER_DB   the archive's path (default: weather.db, relative to
#                      the process's WorkingDirectory). The MCC's
#                      /weather/history route must be given the SAME FILE --
#                      and note the two units' WorkingDirectory differ, so
#                      the route takes an absolute path and has no default.
#
# Append-only: no UPDATE path, and the archive is the one irreplaceable file
# the stack owns -- unlike weather_history.json, it never refills.
# =============================================================================

import os
import sqlite3

DEFAULT_DB_PATH = "weather.db"

# The archive's columns ARE weather.HISTORY_FIELDS -- one source of truth for
# "what an observation is": the archive keeps the record the window keeps,
# never a second opinion about it. Spelled out here rather than imported
# because weather.py imports this module and importing it back would be a
# cycle; test_weather_archive.py asserts the two can never drift apart.
COLUMNS = (
    "ts", "temp_f", "wind_mph", "wind_gust_mph", "condition",
    "humidity_pct", "dew_point_f", "pressure_rel_inhg",
    "rain_rate_inhr", "rain_day_in", "solar_wm2", "uv_index",
)

# One row per recorded observation: exactly the points the rolling window
# keeps, but never pruned. `ts` is the PRIMARY KEY, so a restart re-recording
# a point it already has is a no-op -- roll_history's dedupe rule, enforced by
# the store instead of by a function. It's also an INTEGER PRIMARY KEY, which
# SQLite aliases to the rowid, so the range scan the chart does is already
# indexed and a second index would just be a second copy.
SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    ts                INTEGER PRIMARY KEY,
    temp_f            REAL,
    wind_mph          REAL,
    wind_gust_mph     REAL,
    condition         TEXT,
    humidity_pct      REAL,
    dew_point_f       REAL,
    pressure_rel_inhg REAL,
    rain_rate_inhr    REAL,
    rain_day_in       REAL,
    solar_wm2         REAL,
    uv_index          REAL
);
"""

# The widest range a caller may ask for, the /history clamp precedent
# (merle_daemon.py:528 -- "a typo can't bucket ten years"). At 5-minute
# resolution 90 days is ~26k points, which is already more than any chart can
# draw honestly; a bad `from` gets the newest 90 days, never a table scan.
MAX_SPAN_S = 90 * 86400


def db_path():
    """MERLE_WEATHER_DB: unset/blank means the default, relative to the
    process's WorkingDirectory (the MERLE_WEATHER_HISTORY convention). A path
    that can't be opened raises in connect() at startup rather than failing
    quietly on the first write -- the env_float ethos: never run
    half-configured while looking healthy."""
    return os.environ.get("MERLE_WEATHER_DB", "").strip() or DEFAULT_DB_PATH


def connect(path):
    """Open (creating if needed) the archive and ensure the schema exists.
    `path` may be ":memory:" for tests. storage.py's connection handling,
    same reasons: WAL so the MCC route can read while weather.py writes
    without "database is locked", and an idempotent schema so a fresh pearl
    and a five-year-old file take the same path."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# --- pure: what to write, and what range to read ------------------------------

def observation_row(point):
    """A history point -> its INSERT params, in COLUMNS order. .get(): a
    pre-#51 payload replayed through here maps its missing fields to NULL
    rather than crashing -- the history_point() rule, and a gap is real data
    (the station couldn't say)."""
    return tuple(point.get(c) for c in COLUMNS)


def archivable(points):
    """The points the archive can hold: a point with no ts has no identity.
    This is not pedantry -- `ts` is an INTEGER PRIMARY KEY, so SQLite would
    happily autoassign a rowid for a NULL one and INVENT a timestamp, filing
    an observation under a moment that never happened."""
    return [p for p in points if p.get("ts") is not None]


def backfill_selection(points, known):
    """Which of these points the archive is missing, given the timestamps it
    already has. Pure -- injected `known` -- so the boundary is testable.
    Insertion is idempotent anyway (the PK does that job); this exists so the
    startup line can say how many points were genuinely new instead of
    claiming credit for 576 rows it already had."""
    return [p for p in archivable(points) if p["ts"] not in known]


def clamp_range(frm, to, max_span_s=MAX_SPAN_S):
    """Clamp a requested range to MAX_SPAN_S, anchored at `to`: ask for ten
    years and you get the newest 90 days of it, not a table scan. An inverted
    range (to < frm) is deliberately left alone -- it selects nothing, which
    is the honest answer to a range that contains no time."""
    if to - frm > max_span_s:
        frm = to - max_span_s
    return frm, to


# --- I/O: the thin half -------------------------------------------------------

def record(conn, point):
    """Archive one observation. INSERT OR IGNORE because `ts` is the PK: a
    restart replaying a point the archive already has is a no-op, not a
    conflict. Returns True if a row actually landed."""
    if point.get("ts") is None:
        return False
    cols = ", ".join(COLUMNS)
    marks = ", ".join("?" * len(COLUMNS))
    # f-string SQL is safe here: COLUMNS is a module constant, never input.
    cur = conn.execute(
        f"INSERT OR IGNORE INTO observations ({cols}) VALUES ({marks})",
        observation_row(point))
    conn.commit()
    return cur.rowcount > 0


def known_ts(conn, timestamps):
    """Which of these timestamps the archive already holds."""
    timestamps = list(timestamps)
    if not timestamps:
        return set()
    marks = ", ".join("?" * len(timestamps))
    rows = conn.execute(
        f"SELECT ts FROM observations WHERE ts IN ({marks})", timestamps)
    return {r["ts"] for r in rows}


def backfill(conn, points):
    """Insert everything in `points` the archive doesn't have yet, returning
    how many rows landed. Called with the rolling window at startup: the file
    holds up to 48h of real readings, so a fresh archive gets a free head
    start instead of beginning from empty. Idempotent by the PK, so this is
    safe on EVERY restart, not just the first."""
    missing = backfill_selection(points, known_ts(
        conn, [p["ts"] for p in archivable(points)]))
    if missing:
        cols = ", ".join(COLUMNS)
        marks = ", ".join("?" * len(COLUMNS))
        conn.executemany(
            f"INSERT OR IGNORE INTO observations ({cols}) VALUES ({marks})",
            [observation_row(p) for p in missing])
        conn.commit()
    return len(missing)


def observations(conn, frm, to, max_span_s=MAX_SPAN_S):
    """The archived observations in [frm, to] -- BOTH ENDS INCLUSIVE -- oldest
    first, shaped exactly like the window's points so a chart can't tell the
    two apart. The range is clamped, never trusted."""
    frm, to = clamp_range(frm, to, max_span_s)
    cols = ", ".join(COLUMNS)
    rows = conn.execute(
        f"SELECT {cols} FROM observations "
        "WHERE ts >= ? AND ts <= ? ORDER BY ts", (frm, to))
    return [dict(r) for r in rows]
