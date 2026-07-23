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
    assert "too few" in sa.describe_weather(stats)


def test_build_stats_survives_an_empty_archive():
    base = at(2026, 7, 18, 7)
    stats = sa.build_stats([base, base + 86400], [], base + 2 * 86400)
    assert stats["weather"]["visits_matched"] == 0
    assert stats["weather"]["conditions"] == []
    assert stats["total_visits"] == 2


# --- what the model is actually handed ---------------------------------------

def test_describe_stats_states_figures_the_model_never_has_to_derive():
    hours_visits = [at(2026, 7, 18, 7), at(2026, 7, 18, 7, 30),
                    at(2026, 7, 18, 8)]
    stats = sa.build_stats(hours_visits, [], at(2026, 7, 19, 12))
    text = sa.describe_stats(stats)
    assert "Total visits on record: 3." in text
    assert "First heard:" in text
    # The peak is given as a finished percentage, so no arithmetic is needed.
    assert "%" in text


def test_describe_weather_marks_thin_findings_for_the_prose():
    visits = {"rain": 1, "clear": 60}
    exposure = {"rain": 2.0, "clear": 100.0}
    stats = {"active_hours": [7, 8],
             "weather": {"visits_matched": 61, "enough": True,
                         "conditions": sa.bucket_rates(visits, exposure),
                         "temperature": []}}
    text = sa.describe_weather(stats)
    assert "Evidence: THIN" in text        # the hedge-this-one marker
    assert "Evidence: solid" in text       # and its opposite, on the same list
    # And it tells the model why time of day can't masquerade as weather.
    assert "active hours" in text


def test_describe_weather_labels_direction_as_a_keyword():
    """Desk-tested regression: handed "heard 82% more often", gemma3 wrote
    "notably quiet" -- it reasoned from bird lore instead of the figure. The
    direction is a keyword in front of the number for exactly that reason."""
    more = sa.bucket_rates({"clear": 40, "cloudy": 30},
                           {"clear": 20.0, "cloudy": 30.0})
    text = sa.describe_weather(
        {"active_hours": [7],
         "weather": {"visits_matched": 70, "enough": True,
                     "conditions": more, "temperature": []}})
    assert "MORE OFTEN" in text and "LESS OFTEN" in text
    # The direction sits in front of the number, not buried in a phrase.
    assert "SKY clear: MORE OFTEN" in text


def test_describe_stats_characterises_the_microphone_split_itself():
    """The other desk-tested regression: given a 744-to-1 source split the
    model called it "slightly more". Anything it can get wrong by describing,
    we describe for it. (Source labels are data straight off the sightings
    rows -- "amcrest" before #270's registry renamed it "house-rear".)"""
    base = at(2026, 7, 18, 7)
    stats = sa.build_stats([base], [], base + 3600,
                           sources={"house-rear": 744, "rover": 1})
    assert "Almost all of them came from the house-rear" in sa.describe_stats(stats)
    even = sa.build_stats([base], [], base + 3600,
                          sources={"house-rear": 50, "rover": 45})
    assert "broadly similar" in sa.describe_stats(even)


def test_rules_forbid_copying_the_annotations_into_prose():
    """gemma3 wrote "appearances have been MORE OFTEN" once the labels went
    in -- the labels now say, in the rules, that they are never words."""
    assert "NEVER words to copy" in sa.RULES


def test_prompts_forbid_invented_numbers():
    stats = sa.build_stats([at(2026, 7, 18, 7)], [], at(2026, 7, 19, 12))
    for prompt in (sa.rhythm_prompt("Northern Cardinal", "A red bird.", stats),
                   sa.weather_prompt("Northern Cardinal", stats)):
        assert "Use ONLY the figures given" in prompt
        assert "Never invent" in prompt


# --- the store ---------------------------------------------------------------

@pytest.fixture
def conn():
    from listener import sightings
    c = sa.connect(":memory:")
    c.executescript(sightings.SCHEMA)
    return c


def seed(conn, sci, common, visit_times):
    conn.execute("INSERT OR IGNORE INTO life_list VALUES (?,?,?,?,?)",
                 (sci, common, min(visit_times), "house-rear", None))
    for ts in visit_times:
        conn.execute(
            "INSERT INTO sightings (ts, source, species_sci, species_common,"
            " confidence, clip, wind_suspect, rms) VALUES (?,?,?,?,?,?,?,?)",
            (ts, "house-rear", sci, common, 0.9, None, 0, 0.01))
    conn.commit()


def test_worklist_picks_up_a_species_with_no_analysis(conn):
    seed(conn, "A sci", "Robin", [at(2026, 7, 18, 7)])
    assert [w[0] for w in sa.worklist(conn)] == ["A sci"]


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
    # A pre-#175 burst is one visit, so it must not trip the watermark.
    base = at(2026, 7, 18, 7)
    seed(conn, "A sci", "Robin", [base + i * 4 for i in range(30)])
    assert sa.worklist(conn)[0][2] == 1


def test_analyze_species_writes_nothing_without_visits(conn):
    conn.execute("INSERT OR IGNORE INTO life_list VALUES (?,?,?,?,?)",
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


class FakeOllama:
    model = "gemma3:12b"

    def __init__(self, reply="Some prose."):
        self.reply = reply
        self.calls = []

    def complete(self, system, prompt, **kw):
        self.calls.append(prompt)
        return self.reply


def test_analyze_species_stores_both_blocks_and_the_audit_trail(conn):
    base = at(2026, 7, 1, 7)
    visits = [base + d * 86400 for d in range(12)]
    seed(conn, "A sci", "Robin", visits)
    llm = FakeOllama()
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=base + 20 * 86400)
    assert status == "written"
    assert len(llm.calls) == 2                       # rhythm + weather
    row = conn.execute("SELECT * FROM species_analysis").fetchone()
    assert row["rhythm_text"] == "Some prose."
    assert row["visits_watermark"] == 12
    assert row["model"] == "gemma3:12b"
    # stats_json is the auditable record the prose was written from.
    import json
    assert json.loads(row["stats_json"])["total_visits"] == 12


def test_a_dead_model_leaves_existing_text_standing(conn):
    seed(conn, "A sci", "Robin", [at(2026, 7, 18, 7)])
    conn.execute(
        "INSERT INTO species_analysis (species_sci, rhythm_text,"
        " weather_text, stats_json, visits_watermark, model, generated_ts)"
        " VALUES (?,?,?,?,?,?,?)",
        ("A sci", "the good prose", "and its weather", "{}", 1, "m", 99))
    conn.commit()
    status, _ = sa.analyze_species(conn, "A sci", "Robin",
                                   ollama=FakeOllama(reply=None),
                                   weather_path=None)
    assert status == "llm-down"
    row = conn.execute("SELECT * FROM species_analysis").fetchone()
    assert row["rhythm_text"] == "the good prose"    # untouched
    assert row["generated_ts"] == 99


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
    # The default order holds without env: 27b sweeps a 12b archive upward.
    assert sa.outranks("gemma3:27b", "gemma3:12b") is True
    assert sa.outranks("gemma3:12b", "gemma3:27b") is False


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
    seed(conn, "A sci", "Robin", [at(2026, 7, 18, 7)])
    note(conn, "A sci", watermark=1, version=sa.PROMPT_VERSION + 1)
    assert [w[0] for w in sa.worklist(conn)] == ["A sci"]
    # Including the honest-NULL rows a pre-#217 store carries.
    note(conn, "A sci", watermark=1, version=None)
    assert [w[0] for w in sa.worklist(conn)] == ["A sci"]


def test_worklist_flags_only_an_outranking_model(conn, monkeypatch):
    monkeypatch.setenv("MERLE_MODEL_RANK", "big,small")
    seed(conn, "A sci", "Robin", [at(2026, 7, 18, 7)])
    note(conn, "A sci", watermark=1, model="small")
    assert [w[0] for w in sa.worklist(conn, model="big")] == ["A sci"]
    # Same model: settled. Lower-ranked host: settled (the no-thrash rule).
    assert sa.worklist(conn, model="small") == []
    note(conn, "A sci", watermark=1, model="big")
    assert sa.worklist(conn, model="small") == []
    # No model in play (dry-run / no LLM) disables the arm entirely.
    assert sa.worklist(conn) == []


def test_worklist_flags_a_profile_touched_after_the_note(conn):
    from listener import species_profile
    conn.executescript(species_profile.SCHEMA)
    seed(conn, "A sci", "Robin", [at(2026, 7, 18, 7)])
    note(conn, "A sci", watermark=1, generated_ts=1000)
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
    note(conn, "A sci", watermark=1, generated_ts=1000, checked_ts=3000)
    assert sa.worklist(conn) == []


def test_worklist_leaves_a_settled_species_alone(conn):
    # The negative case: every arm quiet.
    seed(conn, "A sci", "Robin", [at(2026, 7, 18, 7)])
    note(conn, "A sci", watermark=1)
    assert sa.worklist(conn, model="gemma3:12b") == []


def test_the_gate_skips_generation_when_nothing_changed(conn):
    base = at(2026, 7, 1, 7)
    now = at(2026, 7, 20, 12)
    seed(conn, "A sci", "Robin", [base + d * 86400 for d in range(12)])
    llm = FakeOllama()
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=now)
    assert status == "written" and len(llm.calls) == 2
    # Same inputs, same version, same model: verifiably NO LLM call.
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=now)
    assert status == "current"
    assert len(llm.calls) == 2
    row = conn.execute("SELECT * FROM species_analysis").fetchone()
    # The skip is bookkeeping (checked_ts), never a freshness claim.
    assert row["checked_ts"] == now
    assert row["generated_ts"] == now    # from the WRITE, untouched by the skip


def test_the_gate_regenerates_exactly_once_when_a_description_lands(conn):
    from listener import species_profile
    conn.executescript(species_profile.SCHEMA)
    base = at(2026, 7, 1, 7)
    now = at(2026, 7, 20, 12)
    seed(conn, "A sci", "Robin", [base + d * 86400 for d in range(12)])
    llm = FakeOllama()
    sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                       weather_path=None, now=now)
    assert len(llm.calls) == 2                        # written, no background
    conn.execute(
        "INSERT INTO species_profile (species_sci, description, fetched_ts)"
        " VALUES (?,?,?)", ("A sci", "A very red bird.", now))
    conn.commit()
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=now)
    assert status == "written"                        # the description flipped it
    assert len(llm.calls) == 4
    assert "A very red bird." in llm.calls[2]         # and rode the prompt
    # Exactly once: the third look settles.
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=now)
    assert status == "current" and len(llm.calls) == 4


def test_force_pushes_past_the_gate(conn):
    now = at(2026, 7, 20, 12)
    seed(conn, "A sci", "Robin", [at(2026, 7, 18, 7)])
    llm = FakeOllama()
    sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                       weather_path=None, now=now)
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=now, force=True)
    assert status == "written"                        # --refresh's contract
    assert len(llm.calls) == 4


def test_the_gate_defers_to_a_higher_ranked_stored_model(conn, monkeypatch):
    monkeypatch.setenv("MERLE_MODEL_RANK", "huge,big,small")
    now = at(2026, 7, 20, 12)
    seed(conn, "A sci", "Robin", [at(2026, 7, 18, 7)])
    big = FakeOllama()
    big.model = "big"
    sa.analyze_species(conn, "A sci", "Robin", ollama=big,
                       weather_path=None, now=now)
    # A lower-ranked host with identical inputs: skip, never a downgrade.
    small = FakeOllama()
    small.model = "small"
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=small,
                                   weather_path=None, now=now)
    assert status == "current" and small.calls == []
    assert conn.execute("SELECT model FROM species_analysis"
                        ).fetchone()["model"] == "big"
    # A higher-ranked one sweeps it upward.
    huge = FakeOllama()
    huge.model = "huge"
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=huge,
                                   weather_path=None, now=now)
    assert status == "written"
    assert conn.execute("SELECT model FROM species_analysis"
                        ).fetchone()["model"] == "huge"


def test_a_written_row_carries_its_provenance(conn):
    now = at(2026, 7, 20, 12)
    seed(conn, "A sci", "Robin", [at(2026, 7, 18, 7)])
    sa.analyze_species(conn, "A sci", "Robin", ollama=FakeOllama(),
                       weather_path=None, now=now, host="bluejay:11434")
    row = conn.execute("SELECT * FROM species_analysis").fetchone()
    assert row["host"] == "bluejay:11434"
    assert row["prompt_version"] == sa.PROMPT_VERSION
    assert row["stats_fingerprint"]


def test_a_pre217_row_regenerates_once_then_settles(conn):
    now = at(2026, 7, 20, 12)
    seed(conn, "A sci", "Robin", [at(2026, 7, 18, 7)])
    # A #186-era row: no fingerprint, no version -- honest NULLs.
    conn.execute(
        "INSERT INTO species_analysis (species_sci, rhythm_text,"
        " weather_text, stats_json, visits_watermark, model, generated_ts)"
        " VALUES (?,?,?,?,?,?,?)",
        ("A sci", "old prose", "old weather", "{}", 1, "gemma3:12b", 99))
    conn.commit()
    assert [w[0] for w in sa.worklist(conn)] == ["A sci"]   # version arm
    llm = FakeOllama()
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=now)
    assert status == "written"                # once, to fill the new columns
    assert sa.worklist(conn, model="gemma3:12b") == []      # then settles
    status, _ = sa.analyze_species(conn, "A sci", "Robin", ollama=llm,
                                   weather_path=None, now=now)
    assert status == "current"


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
