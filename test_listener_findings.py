# =============================================================================
# project-squirrel -- test_listener_findings.py
#
# The finding engine (#614). Every kind is tested the way species_analysis's
# bucket_rates always was: PLANT an effect in a fixture, recover its value.
# A finding the journal leads with is a sentence a reader believes, so a
# silent error here is a confident wrong sentence on the page.
# =============================================================================

import os
import time

import pytest

import feeds
from listener import findings as fd
from listener import region_prior, solar

HOME = (42.26, -85.57)


@pytest.fixture(autouse=True)
def home_timezone():
    """The findings read local days and hours from the BOX's clock, which on
    orchid and every station is the location's own timezone. These tests
    build Kalamazoo sunrises and sunsets, so they must run on Kalamazoo's
    clock too -- on a UTC runner a 9:15pm EDT sunset is 1:15am and falls
    outside the evening window. (Windows has no tzset; the desk is already
    on Eastern time.)"""
    if not hasattr(time, "tzset"):
        yield
        return
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Detroit"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def at(y, m, d, h, mi=0):
    return int(time.mktime((y, m, d, h, mi, 0, 0, 0, -1)))


def days(start, n):
    """n consecutive local dates from `start` (y, m, d), noon-safe."""
    base = at(*start, 12)
    return [time.localtime(base + i * 86400)[:3] for i in range(n)]


# --- small shaping ------------------------------------------------------------

def test_minutes_phrase_and_pct():
    assert fd.minutes_phrase(-17) == "17 minutes before"
    assert fd.minutes_phrase(1) == "1 minute after"
    assert fd.minutes_phrase(0.2) == "right at"
    assert fd.pct(0.037) == "4"
    assert fd.pct(0.004) == "under 1"
    assert fd.pct(0.0) == "0"


def test_week_phrase():
    assert fd.week_phrase(1) == "early January"
    assert fd.week_phrase(32) == "late August"
    assert fd.week_phrase(38) == "mid October"


# --- dawn and dusk ------------------------------------------------------------

def test_a_planted_dawn_offset_is_recovered():
    # Every morning for 30 days, first song exactly 17 minutes before that
    # date's own sunrise -- the Cardinal's measured habit.
    visits = []
    for y, m, d in days((2026, 7, 20), 30):
        rise = solar.sunrise(*HOME, y, m, d)
        visits += [int(rise - 17 * 60), int(rise + 3600)]
    f = fd.dawn(visits, *HOME)
    assert f["fields"]["median_min"] == -17
    assert f["fields"]["days"] == 30
    assert f["fields"]["label"].startswith("a dawn singer")
    # The claim keeps its figure; the mornings and the spread are evidence,
    # for the writer to weigh and never to write (#630).
    assert f["fact_line"].startswith("First heard each morning around 17 "
                                     "minutes before sunrise (evidence: 30 "
                                     "mornings; the middle half between ")
    assert f["effect"] > 0.9                      # a tight cluster


def test_dawn_needs_ten_mornings_and_ignores_the_afternoon():
    visits = [at(2026, 7, 20 + i, 15) for i in range(30)]   # afternoons only
    assert fd.dawn(visits, *HOME) is None
    early = []
    for y, m, d in days((2026, 7, 20), 9):
        early.append(int(solar.sunrise(*HOME, y, m, d) - 600))
    assert fd.dawn(early, *HOME) is None


def test_a_scattered_morning_has_no_label_and_little_effect():
    visits = []
    for i, (y, m, d) in enumerate(days((2026, 8, 1), 20)):
        # -120..+120 minutes around sunrise: centred, but all over the place
        offset = ((i % 5) - 2) * 60 * 60
        visits.append(int(solar.sunrise(*HOME, y, m, d) + offset))
    f = fd.dawn(visits, *HOME)
    assert f["fields"]["label"] is None and f["effect"] == 0.2


def test_dusk_sings_until_dark_only_when_clear():
    visits = []
    for y, m, d in days((2026, 7, 20), 15):
        visits.append(int(solar.sunset(*HOME, y, m, d) - 5 * 60))
    f = fd.dusk(visits, *HOME)
    assert f["fields"]["median_min"] == -5 and "until dark" in f["fact_line"]
    early = [int(solar.sunset(*HOME, y, m, d) - 4 * 3600)
             for y, m, d in days((2026, 7, 20), 15)]
    assert fd.dusk(early, *HOME) is None


# --- the season ---------------------------------------------------------------

def curve_with(peak_week, peak, now_week, now_value):
    c = [0.0] * 48
    c[peak_week - 1] = peak
    c[now_week - 1] = now_value
    return c


def test_a_departure_cites_the_prior():
    # The Purple Martin: 30 visits through August, silent since Aug 25.
    visits = [at(2026, 7, 27, 9) + i * 86400 for i in range(30)]
    now = at(2026, 10, 24, 12)
    week = region_prior.week_at(now)
    f = fd.phenology(visits, now, curve_with(28, 0.19, week, 0.0))
    assert f["kind"] == "departure"
    assert f["fields"]["silent_days"] == (now - visits[-1]) // 86400
    assert "BirdNET's range model rates it 0% likely here this week" \
        in f["fact_line"]
    assert "19% at its peak in late July" in f["fact_line"]   # week 28
    assert "its season here is over" in f["fact_line"]


def test_a_resident_gone_quiet_is_news_in_itself():
    visits = [at(2026, 7, 1, 9) + i * 86400 for i in range(40)]
    now = at(2026, 10, 24, 12)
    week = region_prior.week_at(now)
    f = fd.phenology(visits, now, curve_with(20, 0.9, week, 0.85))
    assert "the silence itself is the news" in f["fact_line"]


def test_an_arrival_is_on_schedule_or_early():
    now = at(2026, 10, 14, 12)
    visits = [at(2026, 10, 11, 8) + i * 3600 * 20 for i in range(4)]
    week = region_prior.week_at(visits[0])
    f = fd.phenology(visits, now, curve_with(48, 0.9, week, 0.55))
    assert f["kind"] == "arrival" and f["fields"]["first_ever"]
    assert "the first time ever in the yard" in f["fact_line"]
    assert "right on BirdNET's calendar" in f["fact_line"]
    early = fd.phenology(visits, now, curve_with(48, 0.9, week, 0.01))
    assert "this is early, or off its usual map" in early["fact_line"]


def test_a_return_after_a_gap_is_back_again():
    now = at(2026, 10, 14, 12)
    visits = [at(2026, 7, 1, 8), at(2026, 7, 2, 8), at(2026, 10, 10, 8)]
    f = fd.phenology(visits, now, None)
    assert f["kind"] == "arrival" and not f["fields"]["first_ever"]
    assert "back after 100 days away" in f["fact_line"]
    assert "BirdNET" not in f["fact_line"]        # no prior: no prior claims


def test_lingering_past_the_models_season():
    now = at(2026, 10, 24, 12)
    visits = [at(2026, 7, 1, 8) + i * 86400 * 3 for i in range(40)]
    visits.append(now - 3600)
    week = region_prior.week_at(now)
    f = fd.phenology(visits, now, curve_with(30, 0.6, week, 0.01))
    assert f["kind"] == "lingering"


def test_an_ordinary_regular_stands_in_its_season():
    # #626: no news, but the one note still gets a calendar sentence --
    # heard how often lately, and what the model's year holds for it.
    now = at(2026, 10, 24, 12)
    visits = [now - i * 86400 for i in range(90)]
    f = fd.phenology(visits, now, [0.8] * 48)
    assert f["kind"] == "season" and f["effect"] == 0.5
    assert f["support"] == 90 and f["fields"]["days_heard"] == 28
    # On a streak the line carries no day count: the records finding
    # says "every day for the last 90 days", and one note must not state
    # the frequency twice (#630).
    assert f["fact_line"] == ("Heard here since July 27, 2026, and every day "
                              "lately. BirdNET's range model expects it here "
                              "all year.")
    assert f["novelty_key"] == "season:2026-10"        # once a month at most
    # A migrant the model expects to leave: the week its curve drops, in
    # words -- the percentages stay in the fields for the audit trail.
    week = region_prior.week_at(now)
    curve = [0.6] * 48
    for w in range(44, 49):                             # mid-Nov onward
        curve[w - 1] = 0.0
    f = fd.phenology(visits, now, curve)
    assert f["fields"]["ends_week"] == 44
    assert f["fields"]["prior_now"] == 0.6 and f["fields"]["prior_peak"] == 0.6
    assert f["fact_line"].endswith("BirdNET's range model expects its season "
                                   "here to end around late November.")
    assert "60%" not in f["fact_line"]
    assert fd.season_end(curve, week) == 44
    assert fd.season_end([0.5] * 48, week) is None
    # A bird the model barely expects at all: the yard out-hears the model.
    f = fd.phenology(visits, now, [0.02] * 48)
    assert "no more than 2% likely here in any week" in f["fact_line"]
    # No prior (a station): own-record claims only.
    f = fd.phenology(visits, now, None)
    assert "BirdNET" not in f["fact_line"]


def test_a_thin_visitor_gone_quiet_says_so():
    # Under DEPARTED_MIN_VISITS, so never a departure: still a true fact.
    now = at(2026, 10, 24, 12)
    visits = [at(2026, 8, 3, 9), at(2026, 8, 5, 9), at(2026, 8, 9, 9)]
    f = fd.phenology(visits, now, None)
    assert f["kind"] == "season" and f["fields"]["days_heard"] == 0
    assert f["fact_line"] == ("Heard here since August 3, 2026, but not in "
                              "the last 76 days.")


def test_the_28_day_window_is_28_calendar_days():
    # #630: "present on 29 of the last 28 days" -- a window of 28 x 24 hours
    # ending at noon touches 29 dates. The count is today and the 27 days
    # before it, so it can never exceed 28.
    now = at(2026, 10, 24, 12)
    early = [at(2026, 10, 24, 0, 30) - i * 86400 for i in range(40)]
    late = [at(2026, 10, 23, 23, 30) - i * 86400 for i in range(40)]
    f = fd.phenology(early + late, now, None)
    assert f["fields"]["days_heard"] == 28
    assert "every day lately" in f["fact_line"]
    # With gaps, the honest count of the 28 calendar days.
    gappy = [t for i, t in enumerate(early) if i % 3]    # every third day out
    f = fd.phenology(gappy, now, None)
    assert f["fields"]["days_heard"] == 18
    assert "on 18 of the last 28 days" in f["fact_line"]


# --- place and night ----------------------------------------------------------

PLACES = {"house-rear": feeds.Place("the back of the house", True),
          "house-front": feeds.Place("the front door", True),
          "Warbler32": feeds.Place("the back of the garage", False)}


def test_place_judges_live_microphones_only():
    f = fd.place({"house-rear": 80, "house-front": 20, "Warbler32": 400},
                 PLACES)
    assert f["fields"]["source"] == "house-rear"
    assert ("80% of its detections from the microphones listening now "
            "(evidence: 80 of 100)") in f["fact_line"]
    assert f["effect"] == 0.6                      # (0.8 - 0.5) * 2
    assert fd.place({"house-rear": 50, "house-front": 50}, PLACES) is None
    assert fd.place({"house-rear": 10}, PLACES) is None       # too few


def test_place_is_judged_against_what_the_mic_hears_of_the_whole_yard():
    # The house numbers: the rear mic hears ~71% of everything.
    yard = {"house-rear": 14563, "house-front": 5969, "Warbler32": 4710}
    # The Cardinal at 76% rear is the instrument, not the bird.
    assert fd.place({"house-rear": 2545, "house-front": 800}, PLACES,
                    yard) is None
    # The White-throated Sparrow at 75% FRONT is the finding.
    f = fd.place({"house-front": 18, "house-rear": 6}, PLACES, yard)
    assert f["fields"]["source"] == "house-front"
    assert f["fields"]["base"] == 0.291
    assert "hears only 29% of the yard's birds overall" in f["fact_line"]
    assert 0.6 < f["effect"] < 0.8


def test_a_bird_only_a_retired_microphone_heard():
    # The Barred Owl: 100 of 101 on the first garage board, now retired --
    # and the one stray live detection must not hide that (#614 desk run).
    f = fd.place({"Warbler32": 100, "house-rear": 1}, PLACES)
    assert f["fact_line"] == ("Heard almost only at the old microphone at the "
                              "back of the garage, which no longer listens: "
                              "100 of 101 detections.")
    assert f["fields"]["live"] is False
    # A live mic that is merely thin, with no retired place either: nothing.
    assert fd.place({"house-rear": 12, "Warbler32": 9}, PLACES) is None


def test_a_night_voice_is_habit_for_an_owl_and_an_anomaly_for_a_swallow():
    nights = [at(2026, 7, 20 + i % 10, 23) + i for i in range(30)]
    days_ = [at(2026, 7, 20 + i % 10, 9) + i for i in range(10)]
    owl = fd.night(nights + days_, "Strix varia")
    assert owl["kind"] == "night" and owl["fields"]["share"] == 0.75
    swallow = fd.night(nights + days_, "Stelgidopteryx serripennis")
    assert swallow["kind"] == "anomaly" and swallow["flag"] == "anomaly"
    assert "not a bird known to call at night" in swallow["fact_line"]
    assert fd.night(days_ * 3, "Strix varia") is None          # day bird
    assert fd.night(nights[:10], "Strix varia") is None        # too few


# --- records ------------------------------------------------------------------

def test_records_streaks_and_busiest_day():
    now = at(2026, 10, 24, 12)
    visits = [now - i * 86400 for i in range(20)]              # 20-day run
    visits += [at(2026, 8, 1, 7) + i * 300 for i in range(9)]  # 9 on Aug 1
    f = fd.records(visits, now)
    assert f["fields"]["current"] == 20
    assert f["fields"]["longest"] == 20
    assert f["fields"]["busiest"] == 9
    assert "Heard every day for the last 20 days." in f["fact_line"]
    assert "Its busiest day was August 1, 2026, with 9 visits." in f["fact_line"]
    stale = fd.records(visits, now + 10 * 86400)               # run broken
    assert stale["fields"]["current"] == 0
    assert "Its longest run was 20 days in a row." in stale["fact_line"]


# --- the week -----------------------------------------------------------------

def test_fair_sources_need_both_weeks():
    now = at(2026, 10, 24, 12)
    spans = {"house-rear": (now - 90 * 86400, now - 3600),
             "Warbler32": (now - 50 * 86400, now - 11 * 86400),   # died
             "garage-rear": (now - 2 * 86400, now - 600)}         # new
    assert fd.fair_sources(spans, now) == {"house-rear"}


def test_trend_counts_only_fair_microphones():
    now = at(2026, 10, 24, 12)
    # The rear mic hears 10 a week both weeks; a dead mic made the prior
    # week look busier. Counted fairly, nothing moved.
    rows = ([(now - (i + 1) * 14 * 3600, "house-rear") for i in range(10)]
            + [(now - 8 * 86400 - i * 14 * 3600, "house-rear")
               for i in range(10)]
            + [(now - 9 * 86400 - i * 600, "Warbler32") for i in range(30)])
    assert fd.trend(rows, now, set()) is None
    assert fd.trend(rows, now, {"house-rear"}) is None
    assert fd.trend(rows, now, {"house-rear", "Warbler32"})["kind"] == "trend"
    up = rows + [(now - i * 3600, "house-rear") for i in range(1, 30)]
    f = fd.trend(up, now, {"house-rear"})
    assert f["kind"] == "trend" and "clear rise" in f["fact_line"]


# --- weather ------------------------------------------------------------------

def obs(ts, temp, cond="Clear"):
    return {"ts": ts, "temp_f": temp, "condition": cond,
            "rain_rate_inhr": 0.0}


def test_temperature_compared_within_the_month_removes_the_season():
    """THE confound. July is warm, October is cold. The bird is heard twice
    as often per hour in July -- a SEASONAL effect. Pooled naively, "warm"
    looks like +50%; compared within each month, warm is no different from
    the month's own rate, so there is no temperature effect at all."""
    from listener.species_analysis import temp_band
    observations, visits = [], []
    for mon, temp, per_day in ((7, 80.0, 4), (10, 45.0, 2)):
        for d in range(1, 21):
            for k in range(12):                     # 7am-8am, 5-min rows
                observations.append(obs(at(2026, mon, d, 7, k * 5), temp))
            visits += [at(2026, mon, d, 7, 3 + 12 * i) for i in range(per_day)]
    rows = fd.temp_stratified(visits, observations, {7}, temp_band)
    by = {r["bucket"]: r for r in rows}
    assert by["warm"]["effect"] == 0.0 and by["cold"]["effect"] == 0.0
    assert fd.weather_temp(rows) is None


def test_a_real_within_month_temperature_effect_is_found():
    from listener.species_analysis import temp_band
    observations, visits = [], []
    for d in range(1, 21):
        temp = 80.0 if d % 2 else 60.0              # alternate warm/mild days
        for k in range(12):
            observations.append(obs(at(2026, 8, d, 7, k * 5), temp))
        n = 3 if d % 2 else 1                       # 3x as often when warm
        visits += [at(2026, 8, d, 7, 3 + 12 * i) for i in range(n)]
    rows = fd.temp_stratified(visits, observations, {7}, temp_band)
    by = {r["bucket"]: r for r in rows}
    assert by["warm"]["effect"] == 0.5               # 30 seen, 20 expected
    assert by["mild"]["effect"] == -0.5
    f = fd.weather_temp(rows)
    assert f["fields"]["bucket"] == "warm"
    assert "at the same time of year" in f["fact_line"]


def test_weather_sky_takes_the_strongest_solid_bucket():
    stats = {"weather": {"conditions": [
        {"bucket": "rain", "effect": 0.44, "thin": False, "visits": 281,
         "hours": 53.4},
        {"bucket": "unknown", "effect": 2.0, "thin": False, "visits": 9,
         "hours": 9.0},
        {"bucket": "clear", "effect": 0.9, "thin": True, "visits": 2,
         "hours": 3.0}]}}
    f = fd.weather_sky(stats)
    assert f["fields"] == {"bucket": "rain", "effect": 44}
    assert f["fact_line"] == ("When it rains, this bird turns up 44% MORE "
                              "OFTEN than its own average rate (evidence: 281 "
                              "visits over 53 hours of that weather).")
