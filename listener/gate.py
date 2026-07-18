# =============================================================================
# project-squirrel -- listener/gate.py
#
# Earl's pure decision core (issue #172): given what BirdNET said about one
# 3-second window, decide what -- if anything -- goes on the bus. No I/O, no
# model, no clock of its own: everything arrives as arguments so pytest can
# hold every rule to the light (the perception.py/gate ethos).
#
# The rules, in order:
#   1. SPEECH IS NEVER AN EVENT. If the window's top label is a human class,
#      the window produces nothing -- no event, no clip, regardless of every
#      other rule. This is epic #133's design invariant #1: Earl classifies,
#      he never records conversations. It is not a setting.
#   2. The region mask (D4): a species not in the allowed set (BirdNET's geo
#      model for our lat/lon + week) is discarded. Phase 0 measured why this
#      is mandatory: an unfiltered day produced sub-Saharan and western-US
#      species at 0.25-0.61 while true locals scored 0.89-0.92.
#   3. The confidence threshold (default 0.65, from the same Phase 0 numbers:
#      comfortably above every false positive we saw, comfortably below every
#      true local). Wind raises the bar instead of closing the shop: above
#      WIND_GATE_MPH the effective threshold becomes WINDY_THRESHOLD and
#      surviving events carry wind_suspect=True, so a gusty afternoon thins
#      the record honestly rather than silently fabricating or suppressing.
#
# Everything here speaks plain Python types (label/confidence pairs), not
# DataFrames -- the daemon flattens BirdNET's result before calling in, and
# the tests never need pandas.
# =============================================================================

import re

WINDOW_S = 3            # BirdNET's native segment length
SAMPLE_RATE = 48000     # both proven sources deliver this natively
BYTES_PER_SAMPLE = 2    # s16le mono
WINDOW_BYTES = SAMPLE_RATE * WINDOW_S * BYTES_PER_SAMPLE

DEFAULT_THRESHOLD = 0.65
WIND_GATE_MPH = 15.0    # above this, the wind rules kick in
WINDY_THRESHOLD = 0.75  # the raised bar while they do

# BirdNET's non-bird "species" labels for people. Matched against the label's
# scientific half, prefix-wise, because the taxonomy spells them "Human vocal",
# "Human non-vocal", "Human whistle" -- anything Human-shaped means rule 1.
_HUMAN_RE = re.compile(r"^human\b", re.IGNORECASE)

# Non-bird environmental labels BirdNET also emits ("Engine", "Siren", "Dog",
# "Gun"...). Only Engine/Siren/Noise are dropped as pure noise-floor chatter;
# the rest ("Dog", "Coyote"...) stay eventable -- Earl is domain-agnostic and
# a coyote in the driveway is exactly the kind of thing the bus exists for.
_NOISE_LABELS = frozenset({"engine", "environmental", "noise", "siren"})


def split_label(label):
    """BirdNET's "Scientific_Common" -> (scientific, common). A label without
    the underscore (some non-bird classes) doubles as both halves."""
    sci, _, common = str(label).partition("_")
    return sci, (common or sci)


def is_human(label):
    sci, _ = split_label(label)
    return bool(_HUMAN_RE.match(sci))


def is_noise(label):
    sci, _ = split_label(label)
    return sci.lower() in _NOISE_LABELS


def effective_threshold(threshold, wind_mph):
    """Wind raises the bar (never lowers it): a calm-day threshold of 0.8
    stays 0.8 in a gale."""
    if wind_mph is not None and wind_mph > WIND_GATE_MPH:
        return max(threshold, WINDY_THRESHOLD)
    return threshold


def decide(predictions, *, threshold=DEFAULT_THRESHOLD, wind_mph=None,
           allowed_species=None):
    """The gate. `predictions` is [(label, confidence), ...] for ONE window
    (BirdNET's top-k, any order); returns (accepted, windy) where `accepted`
    is the filtered [(label, confidence), ...] best-first and `windy` says
    whether the wind rules were in force (the event's wind_suspect flag).

    Speech: if the window's TOP prediction (by confidence, before any other
    filtering) is human, the whole window is dead -- rule 1 outranks the
    mask and the threshold, and a bird faintly audible behind a conversation
    is a price of the invariant, not a bug. Lower-ranked human labels are
    simply dropped: the window was about the bird, the person was incidental
    and still never becomes an event or a clip.
    """
    ranked = sorted(predictions, key=lambda p: -p[1])
    if ranked and is_human(ranked[0][0]):
        return [], False

    windy = wind_mph is not None and wind_mph > WIND_GATE_MPH
    bar = effective_threshold(threshold, wind_mph)
    accepted = []
    for label, conf in ranked:
        if conf < bar:
            break  # ranked best-first: nothing below clears either
        if is_human(label) or is_noise(label):
            continue
        sci, _ = split_label(label)
        if allowed_species is not None and sci not in allowed_species:
            continue
        accepted.append((label, conf))
    return accepted, windy


def shape_event(*, source, ts, label, confidence, clip_relpath, windy):
    """One audio/events payload. `ts` is unix epoch seconds (the weather-
    namespace convention: consumers compare against time.time() for staleness
    and the dashboard formats locale-side -- nobody wants to parse ISO).
    `clip_relpath` may be None (clip write failed: the event is still real --
    the skipped-report ethos, a missing clip is a gap, never a dead daemon).
    """
    sci, common = split_label(label)
    return {
        "ts": int(ts),
        "source": source,
        "kind": "detection",
        "species_sci": sci,
        "species_common": common,
        "confidence": round(float(confidence), 3),
        "window_s": WINDOW_S,
        "clip": clip_relpath,
        "wind_suspect": bool(windy),
    }


_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


def clip_relpath(source, ts, label):
    """Relative clip path: <source>/<epoch>-<Common_name>.wav. Derived parts
    are scrubbed to a strict allowlist (the frame_archiver rule: filenames
    that come from the wire are scrubbed before they touch a filesystem --
    label text rides MQTT onward, and the sightings store round-trips this
    path back from the bus)."""
    _, common = split_label(label)
    safe_source = _SAFE_CHARS.sub("_", str(source)) or "source"
    safe_common = _SAFE_CHARS.sub("_", common.strip()) or "unknown"
    return f"{safe_source}/{int(ts)}-{safe_common}.wav"


def parse_latlon(raw):
    """MERLE_LATLON "lat,lon" -> (float, float). Raises with the variable's
    name on anything else -- the MERLE_MQTT philosophy: a locator Earl can't
    have has no job guessing (D4 made the mask mandatory)."""
    parts = [p.strip() for p in str(raw or "").split(",")]
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except (IndexError, ValueError):
        raise RuntimeError(
            'MERLE_LATLON must be "lat,lon" (e.g. "42.29,-85.59"). It is '
            "required: BirdNET's region mask (issue #172, D4) needs a place "
            "to stand, and Phase 0 measured what happens without it."
        ) from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise RuntimeError(f"MERLE_LATLON out of range: {lat},{lon}")
    return lat, lon
