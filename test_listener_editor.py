# =============================================================================
# project-squirrel -- test_listener_editor.py
#
# The editor (#614): which finding leads, which support it, and whether an
# entry is due at all. Its regressions are silent -- a bird written up every
# day, a departure never written, the same lede three weeks running -- so
# every trigger and every discount is pinned here.
# =============================================================================

import time

import feeds
from listener import editor as ed
from listener.findings import finding

NOW = 2_000_000_000
DAY = 86400


def f(kind, effect=0.8, support=60, key=None, thin=False, **fields):
    return finding(kind, fields, support, effect, f"{kind} line.",
                   key or f"{kind}:x", thin=thin)


def test_interest_weights_discounts_and_news():
    dawn = f("dawn", 0.8)
    assert ed.interest(dawn) == 1.2 * 0.8                     # full support
    assert ed.interest(f("dawn", 0.8, support=15)) == 1.2 * 0.8 * 0.5
    assert ed.interest(f("dawn", 0.8, thin=True)) == 1.2 * 0.8 * 0.3
    assert ed.interest(dawn, {"dawn:x"}) == 1.2 * 0.8 * 0.2   # said lately
    assert ed.interest(f("weather_sky", 3.0)) == 1.5          # effect capped
    # Unsaid news leads; said news is just a finding again.
    dep = f("departure", 1.0)
    assert ed.interest(dep) == 21.0
    assert ed.interest(dep, {"departure:x"}) < 1


def test_pick_leads_with_the_best_and_never_repeats_a_kind():
    fs = [f("place", 0.5), f("dawn", 0.8), f("record", 0.3),
          f("weather_sky", 0.4), f("weather_temp", 0.6), f("trend", 0.2)]
    lede, rest = ed.pick(fs)
    assert lede["kind"] == "dawn"
    assert len(rest) == 5 and lede not in rest          # #626: five beside it
    assert len({x["kind"] for x in [lede, *rest]}) == 6
    # Theme first: temperature (the stronger weather fact) and place and
    # record each cover a theme before sky, the second weather fact.
    assert [x["kind"] for x in rest] == \
        ["weather_temp", "place", "record", "weather_sky", "trend"]
    assert ed.pick([]) == (None, [])


def test_pick_covers_every_theme_before_doubling_up():
    # #626: a weak season fact beats a second strong timing fact, so the
    # one note always says where the bird stands in the season.
    fs = [f("dawn", 1.0), f("dusk", 1.0), f("night", 0.7),
          f("weather_sky", 0.9), f("record", 0.95), f("trend", 0.3),
          f("season", 0.5), f("place", 0.2)]
    lede, rest = ed.pick(fs)
    assert lede["kind"] == "dawn"
    assert [x["kind"] for x in rest] == \
        ["weather_sky", "record", "season", "place", "dusk"]
    assert ed.pick(fs, n=3)[1] == rest[:2]


def test_supporting_facts_repeat_what_was_said_lately():
    # The LEDE rotates away from what the last entries said; the character
    # beside it does not -- the one note is all the page says (#626).
    said = {"dawn:x", "place:x"}
    lede, rest = ed.pick([f("dawn", 1.0), f("place", 0.9), f("record", 0.3)],
                         said)
    assert lede["kind"] == "record"
    assert [x["kind"] for x in rest] == ["dawn", "place"]


def test_an_anomaly_outranks_a_departure_outranks_the_habits():
    # The Rough-winged Swallow: gone since September AND suspect at night.
    lede, rest = ed.pick([f("dawn", 1.0), f("departure", 1.0),
                          f("anomaly", 1.0), f("weather_sky", 1.0),
                          f("record", 0.3)])
    assert lede["kind"] == "anomaly"
    # Its habits ARE the suspect detections: none may support the flag.
    assert [x["kind"] for x in rest] == ["departure", "record"]


def test_the_first_entry_is_always_due():
    assert ed.due([f("dawn")], [], NOW, 0) == "first"
    assert ed.due([], [], NOW, 5) is None                     # nothing to say


def entry(ts, *found):
    return {"entry_ts": ts, "findings": list(found)}


def test_season_news_fires_once():
    arrival = f("arrival", 1.0, key="arrival:(2026, 10, 11)")
    older = entry(NOW - DAY, f("dawn"))
    assert ed.due([arrival, f("dawn")], [older], NOW, 3) == "arrival"
    written = entry(NOW - 3600, arrival)
    assert ed.due([arrival, f("dawn")], [written, older], NOW, 3) is None
    dep = f("departure", 1.0, key="departure:(2026, 9, 23)")
    assert ed.due([dep], [older], NOW, 0) == "departure"


def test_an_anomaly_is_flagged_once_and_before_anything_else():
    anomaly = f("anomaly", 1.0, key="anomaly:night")
    dep = f("departure", 1.0, key="departure:d")
    assert ed.due([dep, anomaly], [entry(NOW - DAY)], NOW, 0) == "anomaly"
    flagged = entry(NOW - DAY, anomaly)
    assert ed.due([dep, anomaly], [flagged], NOW, 0) == "departure"


def rec(busiest, longest, day=None):
    return f("record", 0.3, busiest=busiest, longest=longest, current=0,
             busiest_day=day)


def local_day(ts):
    lt = time.localtime(ts)
    return [lt.tm_year, lt.tm_mon, lt.tm_mday]


def test_a_record_is_a_new_busiest_day_or_a_streak_milestone():
    last = entry(NOW - DAY, rec(115, 40))
    assert ed.due([rec(116, 40)], [last], NOW, 5) == "record"
    assert ed.due([rec(115, 59)], [last], NOW, 5) is None     # same 30-band
    assert ed.due([rec(115, 60)], [last], NOW, 5) == "record"


def test_a_busiest_day_is_news_once_it_is_over():
    # #650: a record day still under way broke "busiest" on every visit --
    # thirteen entries for one Rusty Blackbird afternoon. It waits for
    # local midnight and is written up once, with the final count.
    today = local_day(NOW)
    last = entry(NOW - 2 * DAY, rec(5, 3))
    for n in range(6, 20):
        assert ed.due([rec(n, 3, today)], [last], NOW, 5) is None
    assert ed.due([rec(19, 3, today)], [last], NOW, 5) is None
    tomorrow = NOW + DAY
    assert ed.due([rec(19, 3, today)], [last], tomorrow, 5) == "record"
    written = entry(tomorrow, rec(19, 3, today))
    assert ed.due([rec(19, 3, today)], [written, last],
                  tomorrow + 3600, 5) is None
    # The streak milestone does not wait for the day to end.
    assert ed.due([rec(19, 60, today)], [entry(NOW - DAY, rec(5, 40))],
                  NOW, 5) == "record"


def test_routine_needs_28_days_a_visit_and_something_new(monkeypatch):
    monkeypatch.delenv("MERLE_NOTES_ROUTINE_DAYS", raising=False)
    dawn = f("dawn", key="dawn:a dawn singer:-20")
    last = entry(NOW - 27 * DAY, f("place", key="place:house-rear"))
    assert ed.due([dawn], [last], NOW, 10) is None            # under 28 days
    last = entry(NOW - 29 * DAY, f("place", key="place:house-rear"))
    assert ed.due([dawn], [last], NOW, 0) is None             # not heard
    assert ed.due([dawn], [last], NOW, 10) == "routine"
    # The same lede as the last entry: nothing new to say, no entry.
    said = ed.said_recently([entry(NOW - 29 * DAY, dawn)])
    assert ed.due([dawn], [entry(NOW - 29 * DAY, dawn)], NOW, 10,
                  said) is None
    # A week was the #614 cadence (#638): a hand-requested entry eight days
    # old now holds the page -- and the clock runs from ANY newest entry.
    assert ed.due([dawn], [entry(NOW - 8 * DAY, f("place"))], NOW, 10) is None


def test_the_routine_window_is_an_env_knob():
    assert ed.routine_s({}) == 28 * 86400
    assert ed.routine_s({"MERLE_NOTES_ROUTINE_DAYS": "7"}) == 7 * 86400
    assert ed.routine_s({"MERLE_NOTES_ROUTINE_DAYS": "14.5"}) == 14.5 * 86400
    for bad in ("", "soon", "0", "-3"):
        assert ed.routine_s({"MERLE_NOTES_ROUTINE_DAYS": bad}) == 28 * 86400


def test_said_recently_covers_the_last_three_entries():
    es = [entry(NOW - i * DAY, f("dawn", key=f"k{i}")) for i in range(5)]
    assert ed.said_recently(es) == {"k0", "k1", "k2"}


def test_titles_are_made_from_the_lede():
    places = {"Warbler32": feeds.Place("the back of the garage", False)}
    assert ed.title(f("arrival", first_ever=True)) == "first heard"
    assert ed.title(f("arrival", first_ever=False)) == "back again"
    assert ed.title(f("dawn", label="a dawn singer, starting before "
                                    "sunrise")) == "up before the sun"
    assert ed.title(f("dawn", label=None)) == "the first song"
    assert ed.title(f("place", source="Warbler32"), places) == \
        "at the back of the garage"
    assert ed.title(f("trend", recent=3, prior=9)) == "a quiet week"
    assert ed.title(f("record", current=99)) == "every day for 99 days"
    assert ed.title(f("record", current=0, days=4)) == "heard on 4 days"
    assert ed.title(f("weather_sky", bucket="rain")) == "a rain bird"
    assert ed.title(f("anomaly")) == "heard at an odd hour"
    assert ed.title(f("season", days_heard=20)) == "the season so far"
    assert ed.title(f("season", days_heard=0)) == "not heard lately"
