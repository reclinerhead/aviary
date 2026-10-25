# =============================================================================
# project-squirrel -- test_listener_species_analysis.py
#
# The analysis pass's computed half (issue #186) -- which is the whole point
# of the design: the LLM is forbidden from doing arithmetic, so every number
# the prose can speak is produced here, and a silent error in these functions
# would become a confident sentence on the page.
#
# The load-bearing cases: exposure-normalised rates (a bucket holding 40% of
# the visits AND 40% of the hours must read as NO effect, where a raw count
# would call it a pattern), the active-hours control that stops time of day
# masquerading as weather, sample gating, and the store's watermark.
# =============================================================================

import sqlite3
import time

import pytest

from listener import gate, species_analysis as sa


def at(year, month, day, hour, minute=0):
    """A local-time epoch, so tests read the same in any timezone."""
    return int(time.mktime((year, month, day, hour, minute, 0, 0, 0, -1)))


def obs(ts, condition="Clear", rain=0.0, temp=60.0):
    """One archive row, shaped like weather_archive's observations."""
    return {"ts": ts, "condition": condition, "rain_rate_inhr": rain,
            "temp_f": temp}


# --- visit grouping ----------------------------------------------------------

def test_group_visits_uses_the_listeners_own_gap():
    # The constant is imported, never restated -- if the listener's rule
    # moves, this moves with it.
    assert sa.gate.VISIT_GAP_S == gate.VISIT_GAP_S
    base = at(2026, 7, 18, 7)
    # A pre-#175 burst: 25 windows 4s apart is ONE visit.
    burst = [base + i * 4 for i in range(25)]
    assert sa.group_visits(burst) == [base]


def test_group_visits_splits_past_the_gap_and_sorts():
    base = at(2026, 7, 18, 7)
    later = base + gate.VISIT_GAP_S + 1
    assert sa.group_visits([later, base]) == [base, later]


# --- time-of-day shaping -----------------------------------------------------

def test_hour_histogram_counts_local_hours():
    hours = sa.hour_histogram([at(2026, 7, 18, 7), at(2026, 7, 18, 7, 30),
                               at(2026, 7, 18, 16)])
    assert hours[7] == 2 and hours[16] == 1 and sum(hours) == 3


def test_peak_window_finds_the_dawn_chorus():
    hours = [0] * 24
    hours[6], hours[7], hours[8] = 5, 20, 15   # 40 of 44 visits
    hours[15] = 4
    peak = sa.peak_window(hours, width=3)
    assert peak["start_hour"] == 6
    assert peak["visits"] == 40
    assert peak["share"] == pytest.approx(40 / 44, abs=0.01)


def test_peak_window_wraps_midnight():
    # An owl's peak must not be split by an arbitrary day boundary.
    hours = [0] * 24
    hours[23], hours[0], hours[1] = 10, 12, 8
    peak = sa.peak_window(hours, width=3)
    assert peak["start_hour"] == 23
    assert peak["visits"] == 30


def test_peak_window_is_none_without_visits():
    assert sa.peak_window([0] * 24) is None


def test_active_hours_covers_the_bulk_of_visits():
    hours = [0] * 24
    hours[7], hours[8] = 50, 40      # 90 of 100
    hours[13], hours[20] = 5, 5
    active = sa.active_hours(hours, coverage=0.9)
    assert active == {7, 8}          # the two hours that carry 90%
    # An hour nobody was ever heard in is never active.
    assert 3 not in active


def test_active_hours_is_empty_without_visits():
    assert sa.active_hours([0] * 24) == set()


def test_trend_needs_two_full_windows():
    now = at(2026, 7, 18, 12)
    # Only 5 days of record: no trend to state, and saying so beats
    # dividing by a window that doesn't exist.
    young = [now - i * 86400 for i in range(5)]
    assert sa.trend(young, now, window_days=7) is None
    old = [now - i * 86400 for i in range(20)]
    t = sa.trend(old, now, window_days=7)
    assert t["recent"] == 7 and t["prior"] == 7


def test_busiest_day_counts_distinct_local_days():
    visits = [at(2026, 7, 16, 8), at(2026, 7, 17, 8), at(2026, 7, 17, 9),
              at(2026, 7, 17, 10)]
    day = sa.busiest_day(visits)
    assert day["visits"] == 3 and day["days_observed"] == 2


# --- weather bucketing -------------------------------------------------------

def test_condition_bucket_lets_the_piezo_outrank_the_grid_cell():
    # The house rule: the driveway's own instrument beats OpenWeather's word.
    assert sa.condition_bucket("Clear", 0.05) == "rain"
    assert sa.condition_bucket("Clear", 0.0) == "clear"


def test_condition_bucket_maps_the_owm_vocabulary():
    assert sa.condition_bucket("Clouds", 0) == "cloudy"
    assert sa.condition_bucket("Fog", 0) == "cloudy"     # ConditionGlyph rule
    assert sa.condition_bucket("Drizzle", 0) == "rain"
    assert sa.condition_bucket("Thunderstorm", 0) == "rain"
    assert sa.condition_bucket("Snow", 0) == "snow"
    assert sa.condition_bucket(None, None) == "unknown"


def test_temp_bands():
    assert sa.temp_band(20) == "freezing"
    assert sa.temp_band(45) == "cold"
    assert sa.temp_band(65) == "mild"
    assert sa.temp_band(80) == "warm"
    assert sa.temp_band(95) == "hot"
    assert sa.temp_band(None) == "unknown"


def test_weather_at_snaps_to_the_nearest_row_within_tolerance():
    base = at(2026, 7, 18, 7)
    index = sa.observation_index([obs(base), obs(base + 300, "Clouds")])
    assert sa.weather_at(index, base + 60)["condition"] == "Clear"
    assert sa.weather_at(index, base + 280)["condition"] == "Clouds"


def test_weather_at_refuses_to_borrow_a_distant_reading():
    # A visit predating the archive gets None and sits out the weather
    # stats -- never a reading from hours away.
    base = at(2026, 7, 18, 7)
    index = sa.observation_index([obs(base)])
    assert sa.weather_at(index, base + 4 * 3600) is None
    assert sa.weather_at(sa.observation_index([]), base) is None


# --- the statistic that carries the feature ----------------------------------

def test_bucket_rates_report_no_effect_when_exposure_matches():
    """THE case. 40 of 100 visits in cloud, and 40% of the hours cloudy:
    a raw count would announce a pattern; the rate says there is none."""
    visits = {"cloudy": 40, "clear": 60}
    exposure = {"cloudy": 40.0, "clear": 60.0}
    by = {f["bucket"]: f for f in sa.bucket_rates(visits, exposure)}
    assert by["cloudy"]["effect"] == pytest.approx(0.0, abs=0.001)
    assert by["clear"]["effect"] == pytest.approx(0.0, abs=0.001)


def test_bucket_rates_find_a_real_effect():
    # Same 40 visits, but cloud covered only 20% of the hours: genuinely
    # twice as likely as this bird's own average.
    visits = {"cloudy": 40, "clear": 40}
    exposure = {"cloudy": 20.0, "clear": 60.0}
    by = {f["bucket"]: f for f in sa.bucket_rates(visits, exposure)}
    assert by["cloudy"]["effect"] == pytest.approx(1.0, abs=0.01)   # +100%
    assert by["clear"]["effect"] == pytest.approx(-1 / 3, abs=0.01)  # -33%


def test_bucket_rates_flag_thin_samples_without_dropping_them():
    visits = {"rain": 1, "clear": 50}
    exposure = {"rain": 2.0, "clear": 100.0}
    by = {f["bucket"]: f for f in sa.bucket_rates(visits, exposure)}
    assert by["rain"]["thin"] is True     # kept, so the prose can hedge
    assert by["clear"]["thin"] is False


def test_bucket_rates_skip_a_condition_that_never_happened():
    # No exposure means no claim -- not a divide by zero.
    out = sa.bucket_rates({"snow": 0}, {"snow": 0.0})
    assert out == []


def test_bucket_rates_count_silent_hours_in_the_baseline():
    """Sixty rainy hours with no bird are EVIDENCE, not an absence of data.
    Dropping them from the denominator would inflate the baseline and
    understate every effect measured against it."""
    findings = sa.bucket_rates({"clear": 40}, {"clear": 20.0, "rain": 60.0})
    by = {f["bucket"]: f for f in findings}
    # Baseline is 40 visits / 80 hours = 0.5/hr; clear runs at 2.0/hr.
    assert by["clear"]["effect"] == pytest.approx(3.0, abs=0.01)   # +300%
    assert by["rain"]["visits"] == 0
    assert by["rain"]["effect"] == pytest.approx(-1.0, abs=0.01)   # never


# --- the package -------------------------------------------------------------

def test_build_stats_measures_weather_only_within_active_hours():
    """The dawn-confound control. The bird is heard ONLY at 7am. It rains
    every afternoon and is clear every morning. Counting all exposure would
    show a huge "clear" effect that is really just the dawn chorus; counting
    only the 7am hours shows no weather effect at all, because within the
    hours the bird is actually heard, the weather never varied."""
    days = range(1, 15)
    visits = [at(2026, 7, d, 7) for d in days]
    observations = []
    for d in days:
        for h in range(24):
            for m in (0, 30):
                raining = h >= 12          # clear mornings, wet afternoons
                observations.append(obs(
                    at(2026, 7, d, h, m),
                    "Rain" if raining else "Clear",
                    0.1 if raining else 0.0))
    stats = sa.build_stats(visits, observations, at(2026, 7, 15, 12))

    assert stats["active_hours"] == [7]
    buckets = {f["bucket"]: f for f in stats["weather"]["conditions"]}
    # Only clear hours exist inside the active window, so rain is not a
    # finding at all -- rather than a fabricated "birds hate rain".
    assert "rain" not in buckets
    assert buckets["clear"]["effect"] == pytest.approx(0.0, abs=0.001)


def test_build_stats_reports_matched_visits_not_total():
    # Visits predating the archive must not inflate the weather sample.
    base = at(2026, 7, 18, 7)
    visits = [base - 40 * 86400, base]          # one long before the archive
    observations = [obs(base)]
    stats = sa.build_stats(visits, observations, base + 3600)
    assert stats["total_visits"] == 2
    assert stats["weather"]["visits_matched"] == 1


def test_build_stats_says_when_weather_evidence_is_too_thin():
    base = at(2026, 7, 18, 7)
    stats = sa.build_stats([base], [obs(base)], base + 3600)
    assert stats["weather"]["enough"] is False


def test_build_stats_survives_an_empty_archive():
    base = at(2026, 7, 18, 7)
    stats = sa.build_stats([base, base + 86400], [], base + 2 * 86400)
    assert stats["weather"]["visits_matched"] == 0
    assert stats["weather"]["conditions"] == []
    assert stats["total_visits"] == 2


# --- what the model is actually handed ---------------------------------------


def test_rules_forbid_copying_the_annotations_into_prose():
    """gemma3 wrote "appearances have been MORE OFTEN" once the labels went
    in -- the labels now say, in the rules, that they are never words."""
    assert "NEVER words to copy" in sa.RULES


# --- the store ---------------------------------------------------------------

@pytest.fixture
def conn():
    from listener import sightings
    c = sa.connect(":memory:")
    c.executescript(sightings.SCHEMA)
    return c


def seed(conn, sci, common, visit_times):
    conn.execute("INSERT OR IGNORE INTO life_list (species_sci, species_common, first_ts, first_source, first_clip) VALUES (?,?,?,?,?)",
                 (sci, common, min(visit_times), "house-rear", None))
    for ts in visit_times:
        conn.execute(
            "INSERT INTO sightings (ts, source, species_sci, species_common,"
            " confidence, clip, wind_suspect, rms) VALUES (?,?,?,?,?,?,?,?)",
            (ts, "house-rear", sci, common, 0.9, None, 0, 0.01))
    conn.commit()


def spaced_visits(n, base=None):
    """n visit openings a day apart -- above the gap, so each is its own visit."""
    base = base if base is not None else at(2026, 7, 18, 7)
    return [base + d * 86400 for d in range(n)]


def test_worklist_picks_up_a_species_with_no_analysis(conn):
    seed(conn, "A sci", "Robin", spaced_visits(sa.MIN_VISITS_FOR_ANALYSIS))
    assert [w[0] for w in sa.worklist(conn)] == ["A sci"]


def test_worklist_ignores_species_below_the_visit_floor(conn):
    # Issue #407: first contact (and two-window flukes) must not spin Ollama.
    # Floor is on grouped visits, and applies to every arm -- not just
    # "never written".
    base = at(2026, 7, 18, 7)
    seed(conn, "A sci", "Robin", [base])                          # 1 visit
    assert sa.worklist(conn) == []
    seed(conn, "A sci", "Robin", [base + 86400])                  # 2 visits
    assert sa.worklist(conn) == []
    # Even a prompt_version mismatch must not put a sub-floor species on
    # the list (otherwise a pre-#217 one-visit row would still burn GPU).
    note(conn, "A sci", watermark=1, version=sa.PROMPT_VERSION + 1)
    assert sa.worklist(conn) == []
    # Crossing the floor unlocks the existing arms (never written here
    # after clearing the mismatched row for a clean first-write case).
    conn.execute("DELETE FROM species_analysis WHERE species_sci = ?",
                 ("A sci",))
    conn.commit()
    seed(conn, "A sci", "Robin", [base + 2 * 86400])              # 3 visits
    assert [w[0] for w in sa.worklist(conn)] == ["A sci"]
    assert sa.worklist(conn)[0][2] == sa.MIN_VISITS_FOR_ANALYSIS


def test_worklist_is_a_noop_until_the_watermark_is_passed(conn):
    base = at(2026, 7, 1, 7)
    visits = [base + d * 86400 for d in range(10)]
    seed(conn, "A sci", "Robin", visits)
    # prompt_version rides along since #217 -- a version-less row is due via
    # its own arm (covered below), and this test is about the watermark.
    conn.execute(
        "INSERT INTO species_analysis (species_sci, rhythm_text,"
        " weather_text, stats_json, visits_watermark, model, generated_ts,"
        " prompt_version) VALUES (?,?,?,?,?,?,?,?)",
        ("A sci", "t", "w", "{}", 10, "gemma3:12b", 1, sa.PROMPT_VERSION))
    conn.commit()
    assert sa.worklist(conn, step=20) == []          # nothing new to say

    # Twenty more visits and it earns a rewrite.
    more = [base + (100 + d) * 86400 for d in range(20)]
    seed(conn, "A sci", "Robin", more)
    assert [w[0] for w in sa.worklist(conn, step=20)] == ["A sci"]


def test_worklist_counts_visits_not_rows(conn):
    # A pre-#175 burst is one visit per cluster. Three bursts far apart
    # are three visits despite many rows -- and only then clear the floor.
    base = at(2026, 7, 18, 7)
    rows = []
    for v in range(sa.MIN_VISITS_FOR_ANALYSIS):
        b = base + v * 86400
        rows.extend(b + i * 4 for i in range(30))
    seed(conn, "A sci", "Robin", rows)
    assert sa.worklist(conn)[0][2] == sa.MIN_VISITS_FOR_ANALYSIS
    # A single burst alone stays off the list (one visit, under the floor).
    conn.execute("DELETE FROM sightings")
    conn.execute("DELETE FROM life_list")
    conn.commit()
    seed(conn, "B sci", "Jay", [base + i * 4 for i in range(30)])
    assert sa.worklist(conn) == []


def test_analyze_species_writes_nothing_without_visits(conn):
    conn.execute("INSERT OR IGNORE INTO life_list (species_sci, species_common, first_ts, first_source, first_clip) VALUES (?,?,?,?,?)",
                 ("Ghost sci", "Ghost", 1, "house-rear", None))
    status, stats = sa.analyze_species(conn, "Ghost sci", "Ghost",
                                       ollama=None, weather_path=None)
    assert status == "no-visits" and stats is None


def test_analyze_species_dry_run_computes_without_writing(conn):
    seed(conn, "A sci", "Robin", [at(2026, 7, 18, 7)])
    status, stats = sa.analyze_species(conn, "A sci", "Robin", ollama=None,
                                       weather_path=None, dry_run=True)
    assert status == "stats-only"
    assert stats["total_visits"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM species_analysis"
                        ).fetchone()["c"] == 0


ANSWER = object()   # "reply the way a rule-abiding model would"


def answer(prompt):
    """A draft that passes the #612 validator: its first sentence states the
    figures of the fact it was told to open with, and nothing else."""
    from listener import notes_validate
    for marker in ("Open the note with this fact:\n",
                   "Open the note with this finding:\n",
                   "Open the entry with this fact:\n"):
        if marker in prompt:
            lede = prompt.split(marker, 1)[1].split("\n", 1)[0]
            return ("Some prose, "
                    + ", ".join(sorted(notes_validate.figures(lede))) + ".")
    return "Some prose."


class FakeOllama:
    model = "gemma3:12b"

    def __init__(self, reply=ANSWER):
        self.reply = reply
        self.calls = []

    def complete(self, system, prompt, **kw):
        self.calls.append(prompt)
        return answer(prompt) if self.reply is ANSWER else self.reply


def test_the_stats_stage_stores_the_figures_and_no_prose(conn):
    # #626: the sketch retired; this row is the margin figures' numbers.
    base = at(2026, 7, 1, 7)
    visits = [base + d * 86400 for d in range(12)]
    seed(conn, "A sci", "Robin", visits)
    llm = FakeOllama()
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=base + 20 * 86400)
    assert status == "written"
    assert llm.calls == []                           # never the model
    row = conn.execute("SELECT * FROM species_analysis").fetchone()
    assert row["rhythm_text"] is None and row["weather_text"] is None
    assert row["fallback"] == 0 and row["model"] is None
    assert row["visits_watermark"] == 12
    assert row["prompt_version"] == 5
    import json
    stored = json.loads(row["stats_json"])
    assert stored["total_visits"] == 12              # the margin figures' keys
    assert "sketch" not in stored


def test_the_stats_stage_carries_old_prose_forward(conn):
    # A pre-#626 row's sketch is the page's fallback until the first entry
    # lands: re-storing the figures neither erases nor re-dates it as prose.
    seed(conn, "A sci", "Robin", [at(2026, 7, 18, 7)])
    conn.execute(
        "INSERT INTO species_analysis (species_sci, rhythm_text,"
        " weather_text, stats_json, visits_watermark, model, generated_ts,"
        " prompt_version, fallback) VALUES (?,?,?,?,?,?,?,?,?)",
        ("A sci", "the good prose", "and its weather", "{}", 1, "m", 99, 4, 1))
    conn.commit()
    status, _ = sa.analyze_species(conn, "A sci", "Robin",
                                   ollama=FakeOllama(reply=None),
                                   weather_path=None, now=500)
    assert status == "written"                       # the version arm
    row = conn.execute("SELECT * FROM species_analysis").fetchone()
    assert row["rhythm_text"] == "the good prose"    # untouched
    assert row["weather_text"] == "and its weather"
    assert row["model"] == "m" and row["fallback"] == 1
    assert row["generated_ts"] == 500 and row["prompt_version"] == 5


# --- the #217 gates: model rank, fingerprint, worklist arms -------------------

def note(conn, sci, *, watermark, version=sa.PROMPT_VERSION,
         model="gemma3:12b", generated_ts=1000, fingerprint="fp",
         checked_ts=None):
    """A stored analysis row with the #217 columns under test's control."""
    conn.execute(
        "INSERT OR REPLACE INTO species_analysis (species_sci, rhythm_text,"
        " weather_text, stats_json, visits_watermark, model, generated_ts,"
        " stats_fingerprint, prompt_version, host, checked_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sci, "r", "w", "{}", watermark, model, generated_ts, fingerprint,
         version, "h:1", checked_ts))
    conn.commit()


def test_model_rank_list_env_override(monkeypatch):
    monkeypatch.setenv("MERLE_MODEL_RANK", " big , small ")
    assert sa.model_rank_list() == ["big", "small"]
    monkeypatch.delenv("MERLE_MODEL_RANK")
    assert sa.model_rank_list() == list(sa.MODEL_RANK)


def test_outranks_only_ever_triggers_upward():
    rank = ["big", "small"]
    assert sa.outranks("big", "small", rank) is True
    # THE thrash case: a lower-ranked host must never claw back a row a
    # higher-ranked one wrote -- this is what lets two hosts settle.
    assert sa.outranks("small", "big", rank) is False
    assert sa.outranks("small", "small", rank) is False   # equal never fires
    # Unknown models sort last: they never outrank, and anything ranked
    # outranks them.
    assert sa.outranks("mystery", "small", rank) is False
    assert sa.outranks("small", "mystery", rank) is True
    assert sa.outranks("mystery", "other-mystery", rank) is False
    # The default order holds without env: 12b sweeps a 4b archive upward.
    assert sa.MODEL_RANK == ("gemma3:12b", "gemma3:4b")
    assert sa.outranks("gemma3:12b", "gemma3:4b") is True
    assert sa.outranks("gemma3:4b", "gemma3:12b") is False


def test_dropped_27b_regenerates_downward_exactly_once():
    """#493: gemma3:27b left MODEL_RANK. A stored 27b row sorts as unknown,
    so the house model outranks it and re-narrates it once; after that the
    row says 12b and nothing outranks it again. The env override can still
    put 27b back on top without a deploy."""
    assert sa.outranks("gemma3:12b", "gemma3:27b") is True    # the wave
    assert sa.outranks("gemma3:27b", "gemma3:12b") is False   # never back
    assert sa.outranks("gemma3:12b", "gemma3:12b") is False   # then settles


def test_stats_fingerprint_is_stable_and_sensitive():
    base = at(2026, 7, 18, 7)
    now = at(2026, 7, 19, 12)
    stats = sa.build_stats([base], [], now)
    again = sa.build_stats([base], [], now)
    fp = sa.stats_fingerprint(stats, "A red bird.")
    assert sa.stats_fingerprint(again, "A red bird.") == fp
    moved = sa.build_stats([base, base + 86400], [], now)
    assert sa.stats_fingerprint(moved, "A red bird.") != fp


def test_stats_fingerprint_covers_the_description_snippet():
    stats = sa.build_stats([at(2026, 7, 18, 7)], [], at(2026, 7, 19, 12))
    none = sa.stats_fingerprint(stats, None)
    text = sa.stats_fingerprint(stats, "A red bird.")
    other = sa.stats_fingerprint(stats, "A blue bird.")
    assert len({none, text, other}) == 3     # None -> text -> edit all differ
    # Only the first 900 chars ride the prompt (rhythm_prompt's slice), so
    # only they ride the hash -- a change past the slice is not an input
    # change and must not trigger a regeneration.
    long_a = "x" * 900 + "tail A"
    long_b = "x" * 900 + "tail B"
    assert (sa.stats_fingerprint(stats, long_a)
            == sa.stats_fingerprint(stats, long_b))


def test_worklist_flags_a_prompt_version_mismatch(conn):
    seed(conn, "A sci", "Robin", spaced_visits(sa.MIN_VISITS_FOR_ANALYSIS))
    note(conn, "A sci", watermark=3, version=sa.PROMPT_VERSION + 1)
    assert [w[0] for w in sa.worklist(conn)] == ["A sci"]
    # Including the honest-NULL rows a pre-#217 store carries.
    note(conn, "A sci", watermark=3, version=None)
    assert [w[0] for w in sa.worklist(conn)] == ["A sci"]


def test_worklist_has_no_model_arm(conn):
    # #626: a stats row carries no prose a better model could improve, and
    # the NULL model it stores would re-queue every bird every tick.
    seed(conn, "A sci", "Robin", spaced_visits(sa.MIN_VISITS_FOR_ANALYSIS))
    note(conn, "A sci", watermark=3, model=None)
    assert sa.worklist(conn) == []
    note(conn, "A sci", watermark=3, model="gemma3:27b")
    assert sa.worklist(conn) == []


def test_worklist_flags_a_profile_touched_after_the_note(conn):
    from listener import species_profile
    conn.executescript(species_profile.SCHEMA)
    seed(conn, "A sci", "Robin", spaced_visits(sa.MIN_VISITS_FOR_ANALYSIS))
    note(conn, "A sci", watermark=3, generated_ts=1000)
    conn.execute(
        "INSERT INTO species_profile (species_sci, description, fetched_ts)"
        " VALUES (?,?,?)", ("A sci", "A red bird.", 2000))
    conn.commit()
    # The profile pass touched this species after the note: due -- the
    # fingerprint decides whether anything actually changed.
    assert [w[0] for w in sa.worklist(conn)] == ["A sci"]
    # A gate-check that found nothing changed settles it (checked_ts) --
    # without this, a dimensions-only backfill would park the species on
    # the worklist forever.
    note(conn, "A sci", watermark=3, generated_ts=1000, checked_ts=3000)
    assert sa.worklist(conn) == []


def test_worklist_leaves_a_settled_species_alone(conn):
    # The negative case: every arm quiet (and above the visit floor).
    seed(conn, "A sci", "Robin", spaced_visits(sa.MIN_VISITS_FOR_ANALYSIS))
    note(conn, "A sci", watermark=3)
    assert sa.worklist(conn) == []


def test_the_gate_skips_the_write_when_nothing_changed(conn):
    base = at(2026, 7, 1, 7)
    now = at(2026, 7, 20, 12)
    seed(conn, "A sci", "Robin", [base + d * 86400 for d in range(12)])
    llm = FakeOllama()
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=now)
    assert status == "written"
    # Same inputs, same version: the row is left alone.
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=now + 60)
    assert status == "current"
    assert llm.calls == []                            # never the model (#626)
    row = conn.execute("SELECT * FROM species_analysis").fetchone()
    # The skip is bookkeeping (checked_ts), never a freshness claim.
    assert row["checked_ts"] == now + 60
    assert row["generated_ts"] == now    # from the WRITE, untouched by the skip


def test_the_gate_restores_exactly_once_when_a_description_lands(conn):
    from listener import species_profile
    conn.executescript(species_profile.SCHEMA)
    base = at(2026, 7, 1, 7)
    now = at(2026, 7, 20, 12)
    seed(conn, "A sci", "Robin", [base + d * 86400 for d in range(12)])
    llm = FakeOllama()
    sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                       weather_path=None, now=now)
    conn.execute(
        "INSERT INTO species_profile (species_sci, description, fetched_ts)"
        " VALUES (?,?,?)", ("A sci", "A very red bird.", now))
    conn.commit()
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=now + 60)
    assert status == "written"                        # the description flipped it
    # Exactly once: the third look settles.
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=now + 120)
    assert status == "current"
    assert conn.execute("SELECT generated_ts FROM species_analysis"
                        ).fetchone()["generated_ts"] == now + 60


def test_force_pushes_past_the_gate(conn):
    now = at(2026, 7, 20, 12)
    seed(conn, "A sci", "Robin", [at(2026, 7, 18, 7)])
    llm = FakeOllama()
    sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                       weather_path=None, now=now)
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=now + 60, force=True)
    assert status == "written"                        # --refresh's contract
    assert conn.execute("SELECT generated_ts FROM species_analysis"
                        ).fetchone()["generated_ts"] == now + 60


def test_a_written_row_carries_its_provenance(conn):
    now = at(2026, 7, 20, 12)
    seed(conn, "A sci", "Robin", [at(2026, 7, 18, 7)])
    sa.analyze_species(conn, "A sci", "Robin", ollama=FakeOllama(),
                       weather_path=None, now=now, host="bluejay:11434")
    row = conn.execute("SELECT * FROM species_analysis").fetchone()
    assert row["host"] == "bluejay:11434"
    assert row["prompt_version"] == sa.PROMPT_VERSION
    assert row["stats_fingerprint"]
    assert row["model"] is None                       # no model wrote it (#626)


def test_a_pre217_row_regenerates_once_then_settles(conn):
    now = at(2026, 7, 20, 12)
    seed(conn, "A sci", "Robin", spaced_visits(sa.MIN_VISITS_FOR_ANALYSIS))
    # A #186-era row: no fingerprint, no version -- honest NULLs.
    conn.execute(
        "INSERT INTO species_analysis (species_sci, rhythm_text,"
        " weather_text, stats_json, visits_watermark, model, generated_ts)"
        " VALUES (?,?,?,?,?,?,?)",
        ("A sci", "old prose", "old weather", "{}", 3, "gemma3:12b", 99))
    conn.commit()
    assert [w[0] for w in sa.worklist(conn)] == ["A sci"]   # version arm
    llm = FakeOllama()
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=now)
    assert status == "written"                # once, to fill the new columns
    assert sa.worklist(conn) == []                           # then settles
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=now)
    assert status == "current"


# --- #612: hygiene ------------------------------------------------------------

import feeds  # noqa: E402 -- grouped with the tests that need it

HOUSE = {
    "house-rear": feeds.Place("the back of the house, over the driveway", True),
    "house-front": feeds.Place("the front of the house, by the front door", True),
    "garage-rear": feeds.Place("the back of the garage", True),
    "rover": feeds.Place("the rover, parked in the driveway", False),
    "Warbler32": feeds.Place("the back of the garage", False),
}


def test_the_shipped_house_places_are_the_ones_these_tests_use():
    assert feeds.places(feeds.DEFAULT_PATH) == HOUSE


def test_place_terms_come_from_the_registrys_phrases():
    # #614: the words a note could use to put a bird somewhere in this yard.
    assert sa.place_terms(HOUSE) == [
        "back of the garage", "back of the house", "door", "driveway",
        "front door", "front of the house", "garage", "rover"]
    assert "house" not in sa.place_terms(HOUSE)    # too generic alone
    station = {"window-mic": feeds.Place("the window, facing the feeder",
                                         True)}
    assert sa.place_terms(station) == ["feeder", "window"]
    assert sa.place_terms({}) == []


def test_driveway_is_allowed_only_by_a_place_phrase():
    assert sa.allow_driveway(HOUSE) is True
    assert sa.allow_driveway({"w": feeds.Place("the window", True)}) is False
    assert sa.allow_driveway({}) is False


class ScriptedOllama:
    """Answers each complete() with the next scripted reply."""
    model = "gemma3:12b"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, prompt, **kw):
        self.calls.append(prompt)
        reply = self.replies.pop(0) if self.replies else ANSWER
        return answer(prompt) if reply is ANSWER else reply


LEAK = "'Evidence: solid' -- they love mornings."


def test_a_model_dying_mid_retry_is_llm_down_and_touches_nothing(conn):
    seed(conn, "A sci", "Robin", spaced_visits(12))
    ctx = ctx_for(conn, at(2026, 8, 1, 12))
    llm = ScriptedOllama(LEAK, None)
    status, _ = sa.write_entry(conn, "A sci", "Robin", ollama=llm, ctx=ctx)
    assert status == "llm-down" and len(llm.calls) == 2
    assert conn.execute("SELECT COUNT(*) c FROM species_journal"
                        ).fetchone()["c"] == 0


def test_renaming_a_place_is_a_changed_input(conn):
    seed(conn, "A sci", "Robin", spaced_visits(12))
    llm = ScriptedOllama()
    now = at(2026, 8, 1, 12)
    sa.analyze_species(conn, "A sci", "Robin", ollama=llm, weather_path=None,
                       places=HOUSE, now=now)
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, places=HOUSE, now=now)
    assert status == "current"
    moved = dict(HOUSE, **{"house-rear": feeds.Place("the patio", True)})
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, places=moved, now=now)
    assert status == "written"


def test_due_step_grows_with_the_record():
    assert sa.due_step(20) == 5
    assert sa.due_step(3400) == 340
    assert sa.due_step(51) == 6                     # ceil, never rounds down
    assert sa.due_step(30, step=20) == 20


def test_a_big_regular_does_not_regenerate_on_five_visits(conn):
    base = at(2026, 7, 1, 7)
    seed(conn, "A sci", "Jay", [base + i * 120 for i in range(400)])
    note(conn, "A sci", watermark=395)
    assert sa.worklist(conn) == []                  # 5 new of 400: not due
    seed(conn, "A sci", "Jay", [base + 10**6 + i * 120 for i in range(40)])
    assert [w[0] for w in sa.worklist(conn)] == ["A sci"]   # 45 >= 44


def test_purge_drops_only_unreachable_rows(conn):
    base = at(2026, 7, 18, 7)
    seed(conn, "Frozen sci", "Thrasher", [base])               # 1 visit
    note(conn, "Frozen sci", watermark=1, version=1, model="gemma3:27b")
    seed(conn, "Kept sci", "Wren", [base])                     # refreshed
    note(conn, "Kept sci", watermark=1, version=sa.PROMPT_VERSION)
    seed(conn, "Big sci", "Jay", spaced_visits(5))             # above floor
    note(conn, "Big sci", watermark=5, version=1)
    assert sa.purge_stale(conn) == ["Frozen sci"]
    left = {r["species_sci"] for r in
            conn.execute("SELECT species_sci FROM species_analysis")}
    assert left == {"Kept sci", "Big sci"}
    assert sa.purge_stale(conn) == []                          # idempotent


def test_connect_upgrades_a_pre217_file(tmp_path):
    path = str(tmp_path / "earl.db")
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE species_analysis (species_sci TEXT PRIMARY KEY,"
        " rhythm_text TEXT, weather_text TEXT, stats_json TEXT,"
        " visits_watermark INTEGER NOT NULL DEFAULT 0, model TEXT,"
        " generated_ts INTEGER NOT NULL)")
    old.execute("INSERT INTO species_analysis VALUES (?,?,?,?,?,?,?)",
                ("A sci", "r", "w", "{}", 5, "m", 99))
    old.commit()
    old.close()
    c = sa.connect(path)   # the additive upgrade, same code as a fresh file
    row = c.execute("SELECT * FROM species_analysis").fetchone()
    assert row["rhythm_text"] == "r"                  # survived
    assert row["stats_fingerprint"] is None           # honestly NULL
    assert row["prompt_version"] is None
    c.close()


# --- #614 / #626: the dated entries -- the one note -------------------------

from listener import editor, notes_validate  # noqa: E402


def ctx_for(conn, now, places=HOUSE):
    return sa.journal_context(conn, None, now, places=places)


def test_the_note_prompt_speaks_in_the_yards_voice_and_stands_alone():
    lede = {"kind": "dawn", "fact_line": "First heard at dawn, 97 mornings.",
            "thin": False}
    rest = [{"kind": "season", "fact_line": "Heard on 28 of the last 28 days.",
             "thin": False}]
    prompt = sa.entry_prompt("Northern Cardinal", "A red bird.", lede, rest,
                             at(2026, 10, 25, 12))
    # No listening posts (#614): handed them, the model invented places.
    assert "listens from" not in prompt and "back of the house" not in prompt
    assert "Say where in the yard the bird was heard ONLY when a fact" in prompt
    assert "Open the note with this fact:\nFirst heard at dawn, 97 mornings." \
        in prompt
    assert "- Heard on 28 of the last 28 days." in prompt
    # #626: one note, read on its own, covering the bird's character.
    assert "read on its own" in prompt
    assert "when in the day the bird is heard, where it stands in the season" \
        in prompt
    assert "120 to 220 words" in prompt and "do not introduce" not in prompt
    assert "Three short paragraphs at most" in prompt
    assert "Use ONLY the figures given" in prompt
    assert 'Address the reader as "you"' in prompt
    assert "driveway" not in sa.VOICE and "driveway" not in sa.RULES
    assert sa.PROMPT_VERSION == 5      # the stats-only row re-stores once
    assert sa.JOURNAL_VERSION == 2     # the one-note prompt


def test_an_entry_naming_a_place_no_fact_names_is_retried(conn):
    seed(conn, "A sci", "Robin", spaced_visits(12))
    ctx = ctx_for(conn, at(2026, 8, 1, 12))
    invented = ("Heard on 12 separate days, mostly at the back of the "
                "garage, as you know.")
    llm = ScriptedOllama(invented, ANSWER)
    sa.write_entry(conn, "A sci", "Robin", ollama=llm, ctx=ctx)
    assert len(llm.calls) == 2
    assert "back of the garage" in llm.calls[1]      # the violation quoted
    row = conn.execute("SELECT text, fallback FROM species_journal").fetchone()
    assert row["fallback"] == 0 and "garage" not in row["text"]


def test_a_thin_fact_carries_the_hedge_marker_in_the_prompt_only():
    f = {"fact_line": "In rain, 44% MORE OFTEN.", "thin": True}
    assert sa.prompt_line(f).endswith("Evidence: THIN -- hedge this one.")
    assert sa.plain_line(f) == "In rain, 44% more often."


def test_a_rule_breaking_entry_is_retried_then_falls_back(conn):
    seed(conn, "A sci", "Robin", spaced_visits(12))
    ctx = ctx_for(conn, at(2026, 8, 1, 12))
    llm = ScriptedOllama(LEAK, ANSWER)
    sa.write_entry(conn, "A sci", "Robin", ollama=llm, ctx=ctx)
    assert len(llm.calls) == 2
    assert "Your previous draft broke these rules" in llm.calls[1]
    row = conn.execute("SELECT * FROM species_journal").fetchone()
    assert row["text"].startswith("Some prose") and row["fallback"] == 0
    llm = ScriptedOllama(LEAK, LEAK)
    sa.write_entry(conn, "A sci", "Robin", ollama=llm, ctx=ctx, force=True)
    row = conn.execute("SELECT * FROM species_journal ORDER BY id DESC"
                       ).fetchone()
    assert row["text"].startswith("Heard here since July 18, 2026, on 12 of "
                                  "the last 28 days (12 visits in all). "
                                  "Heard on 12 separate days.")
    assert row["fallback"] == 1


def test_the_first_entry_is_written_once_then_waits_for_news(conn):
    import json
    seed(conn, "A sci", "Robin", spaced_visits(12))
    now = at(2026, 8, 1, 12)
    ctx = ctx_for(conn, now)
    llm = FakeOllama()
    status, trigger = sa.write_entry(conn, "A sci", "Robin", ollama=llm,
                                     ctx=ctx, host="h:1")
    assert (status, trigger) == ("written", "first")
    assert "it is the first entry" in llm.calls[0]
    assert "Today is August 1, 2026." in llm.calls[0]
    row = conn.execute("SELECT * FROM species_journal").fetchone()
    # #626: a bird with no stronger story leads with its season standing.
    assert row["trigger"] == "first" and row["lede_kind"] == "season"
    assert row["title"] == "the season so far"
    assert row["text"].startswith("Some prose, ")
    assert row["host"] == "h:1" and row["prompt_version"] == sa.JOURNAL_VERSION
    stored = json.loads(row["findings_json"])
    assert [f["kind"] for f in stored] == ["season", "record"]  # lede first
    # Nothing has happened since: not due, no call.
    assert sa.write_entry(conn, "A sci", "Robin", ollama=llm,
                          ctx=ctx) == ("not-due", None)
    assert len(llm.calls) == 1
    # The species page's link pushes past the editor.
    status, trigger = sa.write_entry(conn, "A sci", "Robin", ollama=llm,
                                     ctx=ctx, force=True)
    assert (status, trigger) == ("written", "refresh")
    # #626: every note stands alone -- the earlier entries are behind it.
    assert "assume they were not read" in llm.calls[1]
    assert "it is the first entry" not in llm.calls[1]


def test_a_dead_model_writes_no_entry(conn):
    seed(conn, "A sci", "Robin", spaced_visits(12))
    ctx = ctx_for(conn, at(2026, 8, 1, 12))
    status, _ = sa.write_entry(conn, "A sci", "Robin",
                               ollama=FakeOllama(reply=None), ctx=ctx)
    assert status == "llm-down"
    assert conn.execute("SELECT COUNT(*) c FROM species_journal"
                        ).fetchone()["c"] == 0
    assert sa.write_entry(conn, "A sci", "Robin", ollama=None,
                          ctx=ctx) == ("stats-only", "first")


def test_an_anomaly_entry_is_written_as_a_caution(conn):
    # 30 visits, all at 11pm: a "swallow" heard only at night.
    seed(conn, "Stelgidopteryx serripennis", "Swallow",
         [at(2026, 7, 20, 23) + d * 86400 for d in range(30)])
    ctx = ctx_for(conn, at(2026, 8, 20, 12))
    llm = FakeOllama()
    status, trigger = sa.write_entry(conn, "Stelgidopteryx serripennis",
                                     "Swallow", ollama=llm, ctx=ctx)
    assert status == "written"
    assert "This entry is a caution" in llm.calls[0]
    row = conn.execute("SELECT * FROM species_journal").fetchone()
    assert row["lede_kind"] == "anomaly"
    assert row["title"] == "heard at an odd hour"


def test_the_journal_worklist_holds_the_floor_and_puts_news_first(conn):
    now = at(2026, 10, 24, 12)
    seed(conn, "One sci", "Fluke", [at(2026, 9, 1, 7)])          # sub-floor
    seed(conn, "Gone sci", "Martin",
         [at(2026, 7, 27, 9) + d * 86400 for d in range(30)])   # departed
    seed(conn, "Here sci", "Cardinal",
         [now - d * 86400 for d in range(1, 40)])                # resident
    ctx = ctx_for(conn, now)
    # Both above the floor are due a first entry (by common name: Cardinal,
    # Martin); the fluke, under the #407 floor, gets no journal at all.
    first = sa.journal_worklist(conn, ctx)
    assert [(x[0], x[2][0]) for x in first] == \
        [("Here sci", "first"), ("Gone sci", "first")]
    llm = FakeOllama()
    for sci, common, due in sa.journal_worklist(conn, ctx):
        sa.write_entry(conn, sci, common, ollama=llm, ctx=ctx, due=due)
    assert {r["species_sci"] for r in conn.execute(
        "SELECT species_sci FROM species_journal")} == {"Gone sci", "Here sci"}
    # A week on, the Martin's silence is news it has not had an entry for;
    # it leads the queue ahead of a weekly entry.
    later = ctx_for(conn, now + 8 * 86400)
    conn.execute("UPDATE species_journal SET findings_json = '[]'"
                 " WHERE species_sci = 'Gone sci'")   # forget what was said
    conn.commit()
    queue = sa.journal_worklist(conn, later)
    assert queue[0][0] == "Gone sci" and queue[0][2][0] == "departure"


def test_the_plain_records_pass_their_own_validator(conn):
    seed(conn, "A sci", "Robin", spaced_visits(12))
    ctx = ctx_for(conn, at(2026, 8, 1, 12))
    _, found, _ = sa.species_findings(conn, "A sci", ctx)
    lede, rest = editor.pick(found)
    e_prompt = sa.entry_prompt("Robin", None, lede, rest, ctx["now"])
    assert notes_validate.validate(sa.plain_entry(lede, rest), e_prompt, True,
                                   lede["fact_line"]) == []


def test_audit_reports_stored_entry_violations(conn):
    import json
    seed(conn, "A sci", "Robin", spaced_visits(12))
    ctx = ctx_for(conn, at(2026, 8, 1, 12))
    sa.analyze_species(conn, "A sci", "Robin", ollama=FakeOllama(),
                       weather_path=None, ctx=ctx)
    sa.write_entry(conn, "A sci", "Robin", ollama=FakeOllama(), ctx=ctx)
    assert sa.audit(conn, places=HOUSE) == []                 # clean store
    conn.execute("UPDATE species_journal SET text = 'It is THIN here.'")
    # An old sketch carried forward on the stats row is a fallback, not a
    # claim this version made: never audited (#626).
    conn.execute("UPDATE species_analysis SET rhythm_text ="
                 " 'Our microphone heard it 99 times.'")
    conn.commit()
    found = sa.audit(conn, places=HOUSE)
    assert [b for _, b, _ in found] == ["entry 1"]
    # An older-version entry was written to an older prompt: not audited.
    conn.execute("UPDATE species_journal SET prompt_version = 1")
    conn.commit()
    assert sa.audit(conn, places=HOUSE) == []
    assert json.loads(conn.execute("SELECT findings_json FROM species_journal"
                                   ).fetchone()[0])
