# =============================================================================
# project-squirrel -- test_listener_region_prior.py
#
# The regional prior's pure half (#614): the due check that decides when the
# loop spawns the BirdNET-venv half, the row shaping that makes every
# life-list species present for every week, and the curve reader. The geo
# model call itself is I/O at the boundary, desk-run on orchid.
# =============================================================================

import datetime
import sqlite3

from listener import region_prior as rp
from listener import sightings


def store():
    c = sqlite3.connect(":memory:")
    c.executescript(sightings.SCHEMA)
    c.executescript(rp.SCHEMA)
    return c


def lifer(c, sci):
    c.execute("INSERT INTO life_list (species_sci, species_common, first_ts,"
              " first_source) VALUES (?,?,?,?)", (sci, sci, 1, "m"))


def test_week_of_matches_earl_on_every_day_of_a_year():
    # Restated, not imported (earl pulls paho into the lean venv) -- so the
    # two are held equal here, every day of a leap year.
    import ast
    import pathlib
    src = pathlib.Path(rp.__file__).with_name("earl.py").read_text("utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "week_of")
    scope = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "earl", "exec"),
         scope)
    day = datetime.date(2028, 1, 1)
    while day.year == 2028:
        assert rp.week_of(day.month, day.day) == \
            scope["week_of"](day.month, day.day)
        day += datetime.timedelta(days=1)


def test_home_prefers_the_locations_row_then_the_env():
    c = store()
    assert rp.home(c, {"MERLE_LATLON": "42.29,-85.59"}) == (42.29, -85.59)
    # sightings' (0,0) placeholder means "unknown", never a place.
    c.execute("INSERT INTO locations (id, name, lat, lon, created_ts)"
              " VALUES (1, 'Home', 0.0, 0.0, 1)")
    assert rp.home(c, {"MERLE_LATLON": "42.29,-85.59"}) == (42.29, -85.59)
    assert rp.home(c, {}) is None
    c.execute("UPDATE locations SET lat = 42.26, lon = -85.57 WHERE id = 1")
    assert rp.home(c, {"MERLE_LATLON": "1,2"}) == (42.26, -85.57)
    assert rp.home(store(), {}) is None


def test_due_until_computed_then_on_a_lifer_or_a_move():
    c = store()
    env = {"MERLE_LATLON": "42.26,-85.57"}
    lifer(c, "A sci")
    assert rp.prior_due(c, env) is True                       # never run
    c.executemany("INSERT INTO region_prior VALUES (?,?,?)",
                  rp.prior_rows({}, ["A sci"]))
    c.execute("INSERT INTO region_prior_meta VALUES (1, 42.26, -85.57, 1)")
    assert rp.prior_due(c, env) is False                      # settled
    lifer(c, "B sci")
    assert rp.prior_due(c, env) is True                       # a new lifer
    c.executemany("INSERT INTO region_prior VALUES (?,?,?)",
                  rp.prior_rows({}, ["B sci"]))
    assert rp.prior_due(c, env) is False
    assert rp.prior_due(c, {"MERLE_LATLON": "43.0,-85.57"}) is True  # moved
    assert rp.prior_due(store(), {}) is False                 # nowhere


def test_every_species_gets_every_week_and_silence_is_zero():
    rows = rp.prior_rows({1: {"A sci": 0.81234}, 2: {"Z sci": 0.5}},
                         ["A sci", "Vagrant sci"])
    assert len(rows) == 2 * rp.PRIOR_WEEKS
    by = {(s, w): g for s, w, g in rows}
    assert by[("A sci", 1)] == 0.8123
    assert by[("A sci", 2)] == 0.0
    assert by[("Vagrant sci", 30)] == 0.0      # never listed: still present
    assert ("Z sci", 2) not in by              # not on the life list


def test_the_curve_reader():
    c = store()
    assert rp.prior_curve(c, "A sci") is None
    c.executemany("INSERT INTO region_prior VALUES (?,?,?)",
                  rp.prior_rows({40: {"A sci": 0.3}}, ["A sci"]))
    curve = rp.prior_curve(c, "A sci")
    assert len(curve) == 48 and curve[39] == 0.3 and curve[0] == 0.0
    assert rp.prior_curve(sqlite3.connect(":memory:"), "A sci") is None
