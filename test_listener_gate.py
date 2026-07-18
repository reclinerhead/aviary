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
        "rms": None,   # additive in #175; None when the caller has none
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


# --- the YAMNet front gate (issue #174) --------------------------------------
# route() decides which windows die, which get BirdNET, and which become
# coarse sound events. Precedence is the invariant's teeth: speech beats
# everything, at a LOWER floor than everything.

def test_speech_beats_bird_and_notable():
    verdict, hits = gate.route([("Speech", 0.5), ("Bird", 0.9), ("Dog", 0.9)])
    assert verdict == "speech"


def test_speech_kills_at_its_lower_floor():
    # 0.15 is under the routing floor but over SPEECH_FLOOR: still dead.
    assert gate.SPEECH_FLOOR < gate.DEFAULT_GATE_FLOOR
    verdict, _ = gate.route([("Speech", 0.15)])
    assert verdict == "speech"
    verdict, _ = gate.route([("Speech", gate.SPEECH_FLOOR - 0.01)])
    assert verdict == "quiet"


def test_bird_routes_to_birdnet():
    verdict, hits = gate.route([("Bird", 0.52), ("Wild animals", 0.49)])
    assert verdict == "bird"


def test_notable_only_becomes_a_sound_event():
    verdict, hits = gate.route([("Dog", 0.8), ("Bark", 0.6)])
    assert verdict == "notable"
    assert hits == [("Dog", 0.8), ("Bark", 0.6)]   # best-first


def test_below_floor_and_unknown_classes_are_quiet():
    verdict, _ = gate.route([("Bird", 0.29)])           # under 0.3
    assert verdict == "quiet"
    verdict, _ = gate.route([("Silence", 1.0), ("Music", 0.9)])  # unmapped
    assert verdict == "quiet"


def test_route_floor_is_adjustable_except_for_speech():
    verdict, _ = gate.route([("Bird", 0.35)], floor=0.5)
    assert verdict == "quiet"
    verdict, _ = gate.route([("Speech", 0.15)], floor=0.5)
    assert verdict == "speech"   # the invariant doesn't take arguments


def test_no_class_sits_in_two_routes():
    assert not (gate.SPEECH_CLASSES & gate.BIRD_CLASSES)
    assert not (gate.SPEECH_CLASSES & gate.NOTABLE_CLASSES)
    assert not (gate.BIRD_CLASSES & gate.NOTABLE_CLASSES)


def test_unknown_gate_classes_flags_typos():
    model_names = list(gate.SPEECH_CLASSES | gate.BIRD_CLASSES
                       | gate.NOTABLE_CLASSES)
    assert gate.unknown_gate_classes(model_names) == []
    assert gate.unknown_gate_classes(model_names[1:]) == [model_names[0]]


def test_shape_sound_event_shape():
    payload = gate.shape_sound_event(source="rover", ts=1784400000.9,
                                     klass="Dog", confidence=0.812,
                                     clip_relpath="rover/1784400000-Dog.wav",
                                     windy=True, rms=0.0421)
    assert payload == {
        "ts": 1784400000, "source": "rover", "kind": "sound",
        "class": "Dog", "confidence": 0.812, "window_s": 3,
        "clip": "rover/1784400000-Dog.wav", "wind_suspect": True,
        "rms": 0.0421,
    }
    assert "species_sci" not in payload and "species_common" not in payload


def test_sightings_ignores_sound_events():
    # The #172 guard is the two-tier schema's compatibility mechanism --
    # pin it from this side too.
    import json

    from listener import sightings
    payload = gate.shape_sound_event(source="rover", ts=1, klass="Dog",
                                     confidence=0.8, clip_relpath=None,
                                     windy=False)
    assert sightings.parse_event(json.dumps(payload)) is None


# --- visits (issue #175) -----------------------------------------------------
# Day one measured one singing cardinal = 25 events/rows/clips. The visit
# tracker collapses that to one; these pin the lifecycle that does it.

def test_first_detection_opens_a_visit():
    v = gate.VisitTracker()
    action, clip = v.observe(CHICKADEE, 1000, 0.70, "a/1000-c.wav")
    assert (action, clip) == ("open", "a/1000-c.wav")


def test_same_species_within_gap_is_suppressed():
    v = gate.VisitTracker()
    v.observe(CHICKADEE, 1000, 0.70, "a/1000-c.wav")
    action, clip = v.observe(CHICKADEE, 1003, 0.65, "a/1003-c.wav")
    # Suppressed, and the clip stays the VISIT's -- the path the published
    # event already carries, never the candidate's.
    assert (action, clip) == ("extend", "a/1000-c.wav")


def test_better_window_upgrades_the_visits_clip():
    v = gate.VisitTracker()
    v.observe(CHICKADEE, 1000, 0.70, "a/1000-c.wav")
    action, clip = v.observe(CHICKADEE, 1003, 0.90, "a/1003-c.wav")
    assert (action, clip) == ("best", "a/1000-c.wav")
    # Equal is not better -- no pointless rewrite.
    action, _ = v.observe(CHICKADEE, 1006, 0.90, "a/1006-c.wav")
    assert action == "extend"


def test_past_gap_reopens_with_a_new_clip():
    v = gate.VisitTracker(gap_s=60)
    v.observe(CHICKADEE, 1000, 0.70, "a/1000-c.wav")
    action, clip = v.observe(CHICKADEE, 1061, 0.70, "a/1061-c.wav")
    assert (action, clip) == ("open", "a/1061-c.wav")


def test_species_interleave_independently():
    # Day one had exactly this: a cardinal and a woodpecker both mid-visit.
    v = gate.VisitTracker()
    assert v.observe(CHICKADEE, 1000, 0.7, "c1")[0] == "open"
    assert v.observe(FINCH, 1003, 0.8, "f1")[0] == "open"
    assert v.observe(CHICKADEE, 1006, 0.6, "c2")[0] == "extend"
    assert v.observe(FINCH, 1009, 0.9, "f2")[0] == "best"


def test_expire_closes_and_reports_stats():
    v = gate.VisitTracker(gap_s=60)
    v.observe(CHICKADEE, 1000, 0.70, "c1")
    v.observe(CHICKADEE, 1030, 0.90, "c2")
    v.observe(FINCH, 1080, 0.80, "f1")
    closed = v.expire(1095)   # chickadee's gap passed; finch's has not
    assert len(closed) == 1
    assert closed[0] == {"label": CHICKADEE, "windows": 2,
                         "duration_s": 30, "best_conf": 0.90}
    # Closed means gone: the species reopens fresh.
    assert v.observe(CHICKADEE, 1096, 0.5, "c3")[0] == "open"
    # And the finch is untouched.
    assert v.observe(FINCH, 1100, 0.5, "f2")[0] == "extend"


def test_expire_before_gap_closes_nothing():
    v = gate.VisitTracker(gap_s=60)
    v.observe(CHICKADEE, 1000, 0.7, "c1")
    assert v.expire(1060) == []


def test_shape_event_carries_rms():
    event = gate.shape_event(source="rover", ts=1, label=FINCH,
                             confidence=0.92, clip_relpath=None, windy=False,
                             rms=0.031459)
    assert event["rms"] == 0.03146
    # And stays honest when the caller has none (compat with old shapes).
    event = gate.shape_event(source="rover", ts=1, label=FINCH,
                             confidence=0.92, clip_relpath=None, windy=False)
    assert event["rms"] is None


# --- config parsing ----------------------------------------------------------

def test_parse_latlon_happy():
    assert gate.parse_latlon("42.29,-85.59") == (42.29, -85.59)
    assert gate.parse_latlon(" 42.29 , -85.59 ") == (42.29, -85.59)


@pytest.mark.parametrize("raw", [None, "", "42.29", "a,b", "91,0", "0,181"])
def test_parse_latlon_rejects(raw):
    with pytest.raises(RuntimeError, match="MERLE_LATLON"):
        gate.parse_latlon(raw)
