# =============================================================================
# project-squirrel -- listener/region_prior.py
#
# The regional prior (issue #614): BirdNET's own range model asked, once,
# how likely each life-list species is HERE in each of its 48 weeks. That
# curve is what turns "not heard for 19 days" into a phenology finding --
# "BirdNET's range model stopped expecting it here in early October; it has
# almost certainly gone south" -- and what makes an arrival "on schedule".
# The listener already asks the same model for THIS week (the D4 mask,
# #593); this asks for all of them.
#
#   ~/earl-venv/bin/python -m listener.region_prior          (the loop does it)
#
# TWO HALVES, TWO VENVS (the showcase pass's shape, #551): the pure half --
# the table, the due check, the curve reader -- imports nothing heavy and
# runs in the enrichment loop's repo venv; main() loads the BirdNET geo
# model, which only the BirdNET venv has, so the loop spawns this module
# under MERLE_SHOWCASE_PYTHON when prior_due() says so. `birdnet` is
# imported inside main() for that reason.
#
# Every life-list species gets all 48 weeks, a 0.0 where the model does not
# list it at all -- so "missing from the table" means only "a lifer newer
# than the last run", and the due check cannot loop forever on a vagrant
# the model has never heard of.
#
# Place: the store's own `locations` row 1 (Home), else MERLE_LATLON. A
# station's store carries its own Home, so a station's prior is its own.
# =============================================================================

import os
import sqlite3
import time

PRIOR_WEEKS = 48
# The geo model's own floor is 0.03 (earl.GEO_MIN_CONFIDENCE); asking below
# it is what lets a species fading out of its season read as "0.01 now"
# rather than vanish. Not zero: the library treats 0 as "unset" on some
# versions.
PRIOR_MIN_CONFIDENCE = 0.0001
# Home moving further than this (degrees) re-asks the model.
MOVED_DEG = 0.01

SCHEMA = """
CREATE TABLE IF NOT EXISTS region_prior (
    species_sci TEXT NOT NULL,
    week        INTEGER NOT NULL,
    geo         REAL NOT NULL,
    PRIMARY KEY (species_sci, week)
);
CREATE TABLE IF NOT EXISTS region_prior_meta (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    computed_ts INTEGER NOT NULL
);
"""


def week_of(month, day):
    """BirdNET's 48-week year -- listener/earl.py's week_of, restated here
    because importing earl would pull paho and the bus into the loop's lean
    venv. test_listener_region_prior.py holds the two equal on every day of
    a year, so they cannot drift."""
    return (month - 1) * 4 + min(3, (day - 1) // 7) + 1


def week_at(ts):
    lt = time.localtime(ts)
    return week_of(lt.tm_mon, lt.tm_mday)


def home(conn, environ=None):
    """(lat, lon) for this store's Home, or None. The locations row first
    (#232 seeds it on every store), MERLE_LATLON as the fallback."""
    try:
        row = conn.execute(
            "SELECT lat, lon FROM locations WHERE id = 1").fetchone()
        # (0, 0) is sightings._ensure_home's placeholder for "not known
        # yet", never a real Home -- asking the range model about the Gulf
        # of Guinea would fill the prior with seabirds.
        if row is not None and (row[0], row[1]) != (0.0, 0.0):
            return float(row[0]), float(row[1])
    except sqlite3.Error:
        pass
    raw = (environ if environ is not None else os.environ).get(
        "MERLE_LATLON", "")
    try:
        lat, lon = (float(p) for p in raw.split(","))
        return lat, lon
    except ValueError:
        return None


def prior_due(conn, environ=None):
    """True when the prior should be (re)computed: never computed, Home
    moved, or a life-list species has no rows yet (a new lifer). Pure SQL,
    cheap enough to ask every tick."""
    where = home(conn, environ)
    if where is None:
        return False                      # nowhere to stand; nothing to ask
    try:
        meta = conn.execute(
            "SELECT lat, lon FROM region_prior_meta WHERE id = 1").fetchone()
    except sqlite3.Error:
        return True                       # tables not created yet
    if meta is None:
        return True
    if (abs(meta[0] - where[0]) > MOVED_DEG
            or abs(meta[1] - where[1]) > MOVED_DEG):
        return True
    missing = conn.execute(
        "SELECT 1 FROM life_list l WHERE NOT EXISTS (SELECT 1 FROM"
        " region_prior p WHERE p.species_sci = l.species_sci) LIMIT 1"
    ).fetchone()
    return missing is not None


def prior_rows(by_week, species):
    """{week: {sci: geo}} + the life list -> [(sci, week, geo)] covering
    every species for every week, 0.0 where the model is silent. Pure."""
    return [(sci, week, round(float(by_week.get(week, {}).get(sci, 0.0)), 4))
            for sci in sorted(species)
            for week in range(1, PRIOR_WEEKS + 1)]


def prior_curve(conn, sci):
    """The species' 48-week curve as a list (index 0 = week 1), or None
    when the table or the species is absent -- callers then make
    own-record claims only."""
    try:
        rows = conn.execute(
            "SELECT week, geo FROM region_prior WHERE species_sci = ?",
            (sci,)).fetchall()
    except sqlite3.Error:
        return None
    if len(rows) != PRIOR_WEEKS:
        return None
    curve = [0.0] * PRIOR_WEEKS
    for week, geo in rows:
        curve[int(week) - 1] = float(geo)
    return curve


def main():
    """Ask the geo model for all 48 weeks and replace the table. Runs under
    the BirdNET venv (the loop spawns it); prints one [prior] line."""
    from listener import gate
    from listener.earl import birdnet_load_kwargs
    import birdnet

    path = os.environ.get("MERLE_EARL_DB", "").strip() or "earl.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        where = home(conn)
        if where is None:
            print("[prior] no Home location and no MERLE_LATLON -- skipped",
                  flush=True)
            return
        species = [r[0] for r in conn.execute(
            "SELECT DISTINCT species_sci FROM life_list")]
        geo = birdnet.load("geo", "2.4", "tf", **birdnet_load_kwargs())
        by_week = {}
        for week in range(1, PRIOR_WEEKS + 1):
            df = geo.predict(where[0], where[1], week=week,
                             min_confidence=PRIOR_MIN_CONFIDENCE
                             ).to_dataframe()
            scores = {}
            for label, conf in zip(df["species_name"], df["confidence"]):
                sci = gate.split_label(label)[0]
                scores[sci] = max(scores.get(sci, 0.0), float(conf))
            by_week[week] = scores
        rows = prior_rows(by_week, species)
        with conn:
            conn.execute("DELETE FROM region_prior")
            conn.executemany(
                "INSERT INTO region_prior (species_sci, week, geo)"
                " VALUES (?,?,?)", rows)
            conn.execute(
                "INSERT INTO region_prior_meta (id, lat, lon, computed_ts)"
                " VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET"
                " lat=excluded.lat, lon=excluded.lon,"
                " computed_ts=excluded.computed_ts",
                (where[0], where[1], int(time.time())))
        print(f"[prior] {len(species)} species x {PRIOR_WEEKS} weeks for "
              f"{where[0]:.3f},{where[1]:.3f}", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
