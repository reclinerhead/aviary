# =============================================================================
# project-squirrel -- test_listener_gate.py
#
# Earl's accept/reject decision (issue #172) is exactly the kind of pure logic
# whose regressions are silent: a wrong verdict survives startup, the bus, and
# a day of listening -- it just quietly publishes an African flycatcher or,
# worse, persists a speech clip. The rules under test are the issue's
# contract:
#   1. speech kills the window outright (design invariant, not a setting)
#   2. the region mask discards out-of-range species (D4 -- mandatory)
#   3. the threshold, raised (never lowered) by wind
# =============================================================================

import pytest

from listener import gate

CHICKADEE = "Poecile atricapillus_Black-capped Chickadee"
FINCH = "Haemorhous mexicanus_House Finch"
AFRICAN = "Melaenornis pammelaina_Southern Black-Flycatcher"
SPEECH = "Human vocal_Human vocal"
NON_VOCAL = "Human non-vocal_Human non-vocal"
ENGINE = "Engine_Engine"
DOG = "Canis familiaris_Dog"

LOCALS = {"Poecile atricapillus", "Haemorhous mexicanus", "Canis familiaris"}


# --- rule 1: speech ----------------------------------------------------------

def test_speech_on_top_kills_the_whole_window():
    # Even a confident bird below it dies with the window: the invariant
    # outranks everything, and a bird behind a conversation is the price.
    accepted, windy = gate.decide(
        [(SPEECH, 0.97), (CHICKADEE, 0.88)], allowed_species=None)
    assert accepted == []
    assert windy is False


def test_speech_below_a_bird_is_dropped_but_the_bird_survives():
    accepted, _ = gate.decide(
        [(CHICKADEE, 0.88), (SPEECH, 0.70)], allowed_species=None)
    assert accepted == [(CHICKADEE, 0.88)]


def test_human_matching_is_prefix_shaped_not_exact():
    # "Human non-vocal", "Human whistle"... anything Human-shaped is rule 1.
    accepted, _ = gate.decide([(NON_VOCAL, 0.90)], allowed_species=None)
    assert accepted == []
    assert gate.is_human("Human whistle_Human whistle")
    # A hypothetical species with "human" mid-name must NOT trip it.
    assert not gate.is_human("Struthidea humana_Made-up Bird")


# --- rule 2: the region mask -------------------------------------------------

def test_out_of_region_species_is_discarded():
    # The Phase 0 measurement, as a regression test: the African flycatcher
    # never reaches the bus once the mask is up.
    accepted, _ = gate.decide(
        [(AFRICAN, 0.80), (CHICKADEE, 0.88)], allowed_species=LOCALS)
    assert accepted == [(CHICKADEE, 0.88)]


def test_no_mask_means_unmasked_not_empty():
    # allowed_species=None is the geo-model-unavailable fallback: Earl runs
    # loud and unmasked, never silently eventless.
    accepted, _ = gate.decide([(AFRICAN, 0.80)], allowed_species=None)
    assert accepted == [(AFRICAN, 0.80)]


def test_mask_matches_on_the_scientific_half():
    accepted, _ = gate.decide([(FINCH, 0.92)],
                              allowed_species={"Haemorhous mexicanus"})
    assert accepted == [(FINCH, 0.92)]


# --- rule 3: threshold and wind ----------------------------------------------

def test_default_threshold_cuts_phase0_false_positive_range():
    # Phase 0: false positives clustered 0.25-0.61, true locals 0.89-0.92.
    # The default must sit between.
    assert 0.61 < gate.DEFAULT_THRESHOLD < 0.89
    accepted, _ = gate.decide([(CHICKADEE, 0.61)], allowed_species=LOCALS)
    assert accepted == []
    accepted, _ = gate.decide([(CHICKADEE, 0.89)], allowed_species=LOCALS)
    assert accepted == [(CHICKADEE, 0.89)]


def test_wind_raises_the_bar_and_flags_survivors():
    windy_mph = gate.WIND_GATE_MPH + 5
    mid = (gate.DEFAULT_THRESHOLD + gate.WINDY_THRESHOLD) / 2  # 0.65..0.75
    accepted, windy = gate.decide([(CHICKADEE, mid)],
                                  wind_mph=windy_mph, allowed_species=LOCALS)
    assert accepted == [] and windy is True
    strong = gate.WINDY_THRESHOLD + 0.05
    accepted, windy = gate.decide([(CHICKADEE, strong)],
                                  wind_mph=windy_mph, allowed_species=LOCALS)
    assert accepted == [(CHICKADEE, strong)] and windy is True


def test_wind_never_lowers_a_stricter_threshold():
    assert gate.effective_threshold(0.9, gate.WIND_GATE_MPH + 10) == 0.9


def test_calm_or_unknown_wind_is_the_calm_day():
    assert gate.effective_threshold(0.65, None) == 0.65
    assert gate.effective_threshold(0.65, gate.WIND_GATE_MPH) == 0.65


def test_noise_labels_never_event():
    accepted, _ = gate.decide([(ENGINE, 0.95)], allowed_species=None)
    assert accepted == []


def test_nonbird_wildlife_stays_eventable():
    # Earl is domain-agnostic: a dog (or coyote) in range is an event.
    accepted, _ = gate.decide([(DOG, 0.80)], allowed_species=LOCALS)
    assert accepted == [(DOG, 0.80)]


def test_multiple_accepted_come_back_best_first():
    accepted, _ = gate.decide(
        [(CHICKADEE, 0.70), (FINCH, 0.92)], allowed_species=LOCALS)
    assert accepted == [(FINCH, 0.92), (CHICKADEE, 0.70)]


# --- event shaping and clip paths --------------------------------------------

def test_shape_event_shape():
    event = gate.shape_event(source="amcrest", ts=1784390000.7,
                             label=CHICKADEE, confidence=0.884,
                             clip_relpath="amcrest/1784390000-x.wav",
                             windy=False)
    assert event == {
        "ts": 1784390000, "source": "amcrest", "kind": "detection",
        "species_sci": "Poecile atricapillus",
        "species_common": "Black-capped Chickadee",
        "confidence": 0.884, "window_s": 3,
        "clip": "amcrest/1784390000-x.wav", "wind_suspect": False,
    }


def test_shape_event_survives_a_missing_clip():
    event = gate.shape_event(source="rover", ts=1, label=FINCH,
                             confidence=0.92, clip_relpath=None, windy=True)
    assert event["clip"] is None and event["wind_suspect"] is True


def test_clip_relpath_is_filesystem_safe():
    # The frame_archiver rule: derived filenames get an allowlist scrub.
    path = gate.clip_relpath("amcrest", 1784390000,
                             "Evil/../../Species_Name With Spaces!")
    assert path == "amcrest/1784390000-Name_With_Spaces_.wav"
    assert ".." not in path


def test_split_label_without_underscore_doubles():
    assert gate.split_label("Engine") == ("Engine", "Engine")


# --- config parsing ----------------------------------------------------------

def test_parse_latlon_happy():
    assert gate.parse_latlon("42.29,-85.59") == (42.29, -85.59)
    assert gate.parse_latlon(" 42.29 , -85.59 ") == (42.29, -85.59)


@pytest.mark.parametrize("raw", [None, "", "42.29", "a,b", "91,0", "0,181"])
def test_parse_latlon_rejects(raw):
    with pytest.raises(RuntimeError, match="MERLE_LATLON"):
        gate.parse_latlon(raw)
