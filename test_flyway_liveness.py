# Tests for flyway/liveness.py -- the pure logic only: the MERLE_STATIONS
# parser (fail-loud, the topic-safety guard) and the debounce state machine.
# The HTTP probe and the MQTT plumbing are desk-tested per the testing policy
# (unplug the box, watch the lamp).

import pytest

import bus
from flyway import liveness


# --- MERLE_STATIONS parsing ----------------------------------------------------

def test_single_station_parses():
    assert liveness.parse_stations("birdhouse=http://100.92.248.83/aviary") == [
        ("birdhouse", "http://100.92.248.83/aviary")]


def test_multiple_stations_parse_with_whitespace_and_trailing_comma():
    raw = " birdhouse=http://100.92.248.83/aviary , kzoo=https://kzoo/aviary ,"
    assert liveness.parse_stations(raw) == [
        ("birdhouse", "http://100.92.248.83/aviary"),
        ("kzoo", "https://kzoo/aviary")]


@pytest.mark.parametrize("raw", [
    "",                                  # nothing to watch
    "   ,  ,",                           # only separators
    "birdhouse",                         # no url at all
    "birdhouse=",                        # empty url
    "=http://x/",                        # empty name
    "birdhouse=ftp://x/",                # not http(s)
    "bird house=http://x/",              # space in the topic segment
    "Birdhouse=http://x/",               # uppercase: not topic-convention-safe
    "bird/house=http://x/",              # path separator in a topic segment
    "bird+house=http://x/",              # MQTT single-level wildcard
    "bird#=http://x/",                   # MQTT multi-level wildcard
    "a=http://x/,a=http://y/",           # duplicate name: two verdicts, one topic
])
def test_malformed_station_specs_fail_loud(raw):
    with pytest.raises(ValueError):
        liveness.parse_stations(raw)


def test_stations_env_is_required_no_default(monkeypatch):
    monkeypatch.delenv("MERLE_STATIONS", raising=False)
    with pytest.raises(RuntimeError):
        liveness.stations_from_env()


def test_station_name_lands_in_the_network_namespace():
    # The parser's SAFE_NAME guard exists because the name becomes this topic.
    assert bus.station_status_topic("birdhouse") == "network/birdhouse/status"


# --- interval env (the env_float convention) -----------------------------------

def test_interval_defaults_when_unset_or_blank(monkeypatch):
    monkeypatch.delenv("MERLE_LIVENESS_INTERVAL_S", raising=False)
    assert liveness.interval_s() == liveness.DEFAULT_INTERVAL_S
    monkeypatch.setenv("MERLE_LIVENESS_INTERVAL_S", "  ")
    assert liveness.interval_s() == liveness.DEFAULT_INTERVAL_S


def test_interval_reads_the_env(monkeypatch):
    monkeypatch.setenv("MERLE_LIVENESS_INTERVAL_S", "30")
    assert liveness.interval_s() == 30.0


def test_malformed_interval_fails_loud(monkeypatch):
    monkeypatch.setenv("MERLE_LIVENESS_INTERVAL_S", "soon")
    with pytest.raises(ValueError):
        liveness.interval_s()


# --- the debounce state machine ------------------------------------------------

def run(state, results):
    """Feed a probe-result string ("ok"/"x" per character... explicitness
    beats cleverness: a list of booleans) through step(); return the final
    state and every verdict change in order."""
    changes = []
    for ok in results:
        state, changed = liveness.step(state, ok)
        if changed:
            changes.append(state[0])
    return state, changes


def test_unknown_flips_online_on_the_first_answer():
    state, changes = run(liveness.UNKNOWN, [True])
    assert state[0] == "online"
    assert changes == ["online"]


def test_unknown_stays_unknown_until_the_offline_threshold():
    almost = [False] * (liveness.OFFLINE_AFTER - 1)
    state, changes = run(liveness.UNKNOWN, almost)
    assert state[0] == "unknown"
    assert changes == []
    state, changes = run(state, [False])   # the threshold miss
    assert state[0] == "offline"
    assert changes == ["offline"]


def test_online_survives_all_but_the_threshold_miss():
    state, _ = run(liveness.UNKNOWN, [True])
    state, changes = run(state, [False] * (liveness.OFFLINE_AFTER - 1))
    assert state[0] == "online"
    assert changes == []


def test_online_goes_dark_exactly_at_the_threshold():
    state, _ = run(liveness.UNKNOWN, [True])
    state, changes = run(state, [False] * liveness.OFFLINE_AFTER)
    assert state[0] == "offline"
    assert changes == ["offline"]


def test_one_answer_resets_the_miss_count():
    # 4 misses, an answer, 4 more misses: never offline -- the lamp only goes
    # dark on OFFLINE_AFTER consecutive misses, which is the whole debounce.
    state, _ = run(liveness.UNKNOWN, [True])
    wobble = [False] * (liveness.OFFLINE_AFTER - 1) + [True] \
        + [False] * (liveness.OFFLINE_AFTER - 1)
    state, changes = run(state, wobble)
    assert state[0] == "online"
    assert changes == []


def test_offline_needs_consecutive_answers_to_relight():
    state, _ = run(liveness.UNKNOWN, [False] * liveness.OFFLINE_AFTER)
    state, changes = run(state, [True])          # one answer: not yet
    assert state[0] == "offline"
    assert changes == []
    state, changes = run(state, [True])          # the second: relit
    assert state[0] == "online"
    assert changes == ["online"]


def test_a_miss_resets_the_recovery_streak():
    state, _ = run(liveness.UNKNOWN, [False] * liveness.OFFLINE_AFTER)
    state, changes = run(state, [True, False, True])   # interrupted recovery
    assert state[0] == "offline"
    assert changes == []


def test_steady_online_never_republishes():
    state, changes = run(liveness.UNKNOWN, [True] * 10)
    assert state[0] == "online"
    assert changes == ["online"]   # exactly one verdict, on the first answer


def test_a_full_outage_and_recovery_publishes_exactly_two_changes():
    results = [True] * 3 + [False] * 8 + [True] * 3
    state, changes = run(liveness.UNKNOWN, results)
    assert changes == ["online", "offline", "online"]
    assert state[0] == "online"
