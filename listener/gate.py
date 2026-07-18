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
VISIT_GAP_S = 60        # same-species detections closer than this are one visit

# BirdNET's non-bird "species" labels for people. Matched against the label's
# scientific half, prefix-wise, because the taxonomy spells them "Human vocal",
# "Human non-vocal", "Human whistle" -- anything Human-shaped means rule 1.
_HUMAN_RE = re.compile(r"^human\b", re.IGNORECASE)

# Non-bird environmental labels BirdNET also emits ("Engine", "Siren", "Dog",
# "Gun"...). Only Engine/Siren/Noise are dropped as pure noise-floor chatter;
# the rest ("Dog", "Coyote"...) stay eventable -- Earl is domain-agnostic and
# a coyote in the driveway is exactly the kind of thing the bus exists for.
_NOISE_LABELS = frozenset({"engine", "environmental", "noise", "siren"})

# --- the YAMNet front gate (issue #174, corrected) ---------------------------
# AudioSet display names, verified against the real 521-class map on pearl
# (2026-07-18) -- a typo'd name here silently never matches, the topic-
# constants lesson, so earl.py warns at startup about any entry the loaded
# model doesn't know. A curated, reviewable table (the genre-vocabulary
# ethos), not scattered ifs.
#
# THE GATE DOES NOT VETO BIRDNET, and that is the whole lesson of this
# module's first day in production. Shipped as a true gate (only bird-routed
# windows reached BirdNET), it ate 17 of 25 CONFIRMED yard detections in one
# afternoon -- a 0.97 pheasant, a 0.88 waxwing, a 0.86 nighthawk, all
# silently dropped before the bird model ever saw them. Two errors, one
# root:
#
#   1. It was desk-validated against a close-mic'd CLIP (YAMNet "Bird"=0.52,
#      comfortably over the 0.3 floor). Real distant yard birds score a
#      MEDIAN of 0.189, and several (pheasant, sandpiper) put no bird class
#      in the top-7 AT ALL -- no floor value rescues those.
#   2. AudioSet is a HIERARCHY. On faint yard audio the parent classes fire
#      hardest ("Animal"=0.505 while "Bird"=0.463), and this table was built
#      from names that sounded right instead of from measured behavior.
#
# The deeper point: gating BirdNET was only ever justified by CPU savings,
# and pearl idles at load 0.00 running two ears -- the premise was already
# dead when the gate shipped. Vocabulary and a speech-first invariant were
# the real value, and NEITHER needs a veto. So the routing survives and its
# authority does not: speech kills the window, notable ALSO emits a sound
# event, and every non-speech window reaches BirdNET exactly as in Phase 1.
# Cost: ~1.3s per 3s window instead of 1.2s. Still real time, still single
# digits of one core.

SPEECH_FLOOR = 0.1         # speech kills at a low bar -- err toward the invariant
DEFAULT_GATE_FLOOR = 0.3   # floor for NOTABLE sound events (MERLE_EARL_GATE_FLOOR)

SPEECH_CLASSES = frozenset({
    "Speech", "Child speech, kid speaking", "Conversation",
    "Narration, monologue", "Whispering", "Shout", "Laughter",
    "Singing", "Whistling", "Humming", "Baby cry, infant cry",
})

# Kept for reporting only (the "bird-ish" note on a window, and the startup
# name check) -- NOT a filter, never again a gate. Includes the parent
# classes that actually fire on real yard birds, learned the hard way.
BIRD_CLASSES = frozenset({
    "Animal", "Wild animals",
    "Bird", "Bird vocalization, bird call, bird song", "Chirp, tweet",
    "Squawk", "Pigeon, dove", "Crow", "Owl", "Fowl", "Turkey", "Gobble",
    "Duck", "Goose", "Bird flight, flapping wings",
})

# Non-bird sounds worth an event of their own. "Wild animals"/"Animal" moved
# OUT of here into BIRD_CLASSES: they fire on birds constantly, so leaving
# them here turned yard birds into "sound" events.
NOTABLE_CLASSES = frozenset({
    "Dog", "Bark", "Howl", "Cat", "Meow",
    "Glass", "Shatter", "Siren", "Smoke detector, smoke alarm",
    "Thunder", "Gunshot, gunfire", "Explosion",
    "Vehicle horn, car horn, honking", "Car alarm", "Chainsaw",
    "Engine", "Helicopter", "Cricket", "Frog",
    "Rodents, rats, mice",
})


def route(predictions, floor=DEFAULT_GATE_FLOOR):
    """One window's verdict. `predictions` is YAMNet's [(class, score), ...];
    returns (verdict, hits).

      "speech"  any speech class >= SPEECH_FLOOR. The window is DEAD -- no
                BirdNET, no event, no clip (epic invariant 1, enforced by a
                model with real speech classes, FIRST). The only verdict
                that stops anything.
      "notable" a curated non-bird class >= floor: emit a kind:"sound"
                event, AND still run BirdNET (a dog barking does not mean
                no bird is singing). hits is [(class, score)] best-first.
      "listen"  everything else -- run BirdNET, publish nothing extra. This
                replaced the old "bird"/"quiet" split, which existed only to
                decide what NOT to send to BirdNET; nothing needs that
                decision now.

    Note there is deliberately no bird-score threshold anywhere: BirdNET is
    the bird detector, its own confidence threshold and region mask are the
    filters, and YAMNet's opinion about whether a faint chirp is "Bird
    enough" was exactly the thing that ate two thirds of the yard.
    """
    speech = [(c, s) for c, s in predictions
              if s >= SPEECH_FLOOR and c in SPEECH_CLASSES]
    if speech:
        return "speech", sorted(speech, key=lambda p: -p[1])
    notable = [(c, s) for c, s in predictions
               if s >= floor and c in NOTABLE_CLASSES]
    if notable:
        return "notable", sorted(notable, key=lambda p: -p[1])
    return "listen", []


def unknown_gate_classes(model_class_names):
    """Map entries the loaded model doesn't know -- earl.py logs these LOUDLY
    at startup (a misspelled class silently never routes; never trust a
    string the model didn't confirm)."""
    known = set(model_class_names)
    return sorted((SPEECH_CLASSES | BIRD_CLASSES | NOTABLE_CLASSES) - known)


def shape_sound_event(*, source, ts, klass, confidence, clip_relpath, windy,
                      rms=None):
    """One kind:"sound" payload (issue #174): the two-tier schema's coarse
    tier -- an AudioSet class, no species fields. sightings.py ignores
    non-"detection" kinds by design (the #172 guard is the compatibility
    mechanism), so these flow to future consumers without touching the
    bird record."""
    return {
        "ts": int(ts),
        "source": source,
        "kind": "sound",
        "class": str(klass),
        "confidence": round(float(confidence), 3),
        "window_s": WINDOW_S,
        "clip": clip_relpath,
        "wind_suspect": bool(windy),
        "rms": round(float(rms), 5) if rms is not None else None,
    }


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


def shape_event(*, source, ts, label, confidence, clip_relpath, windy,
                rms=None):
    """One audio/events payload. `ts` is unix epoch seconds (the weather-
    namespace convention: consumers compare against time.time() for staleness
    and the dashboard formats locale-side -- nobody wants to parse ISO).
    `clip_relpath` may be None (clip write failed: the event is still real --
    the skipped-report ethos, a missing clip is a gap, never a dead daemon).
    `rms` (issue #175) is the window's raw signal level, 0..1 -- the number
    Phase 5's loudness ranking needs and confidence is not (confidence is
    model certainty). Additive and optional so old payloads stay parseable.
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
        "rms": round(float(rms), 5) if rms is not None else None,
    }


class VisitTracker:
    """Species-level visit debounce (issue #175) -- the SpeciesPresence idea
    in Earl's idiom. One singing cardinal is one VISIT, not 25 events: day
    one measured 3.2x event redundancy and a disk burn 10x the estimate,
    and this is the fix for both.

    A visit opens on a detection of species S with no S-detection in the
    preceding gap, and stays open while S-detections keep arriving within
    it. Pure: timestamps come in as arguments, the caller owns clocks and
    I/O. One tracker per source worker; species interleave independently.

    Per accepted detection, observe() answers what the caller should do:
      "open"    new visit -- publish the event, write this window's clip
      "best"    suppressed, but this window beats the visit's best --
                REWRITE the visit's clip in place (same path the published
                event already carries; the life list's first_clip ends up
                holding the bird's best moment, not its first mumble)
      "extend"  suppressed, nothing to write
    Call expire(ts) once per window BEFORE observing so a stale visit
    closes (and logs) before the same species can reopen; observe() itself
    also treats a past-gap survivor as a new visit, so the two can't
    disagree about the boundary.

    Deliberately NOT flushed when a capture drops: visits ride out a brief
    source blip (the reconnected stream continues the visit if it's still
    inside the gap), and a worker that dies takes nothing durable with it
    -- the store already has the open event.
    """

    def __init__(self, gap_s=VISIT_GAP_S):
        self._gap = gap_s
        self._open = {}   # species_sci -> visit dict

    def observe(self, label, ts, confidence, candidate_clip):
        """One accepted detection. Returns (action, clip_relpath) where the
        clip path is the VISIT's (stable for its whole life)."""
        sci, _ = split_label(label)
        visit = self._open.get(sci)
        if visit is None or ts - visit["last_ts"] > self._gap:
            self._open[sci] = {
                "label": label, "opened_ts": ts, "last_ts": ts,
                "windows": 1, "best_conf": confidence,
                "clip": candidate_clip,
            }
            return "open", candidate_clip
        visit["last_ts"] = ts
        visit["windows"] += 1
        if confidence > visit["best_conf"]:
            visit["best_conf"] = confidence
            return "best", visit["clip"]
        return "extend", visit["clip"]

    def expire(self, now_ts):
        """Close every visit whose gap has passed; returns their stats
        (label, windows, duration_s, best_conf) for the caller's log line --
        the journal is where a visit's true best confidence lives, since the
        published event carried the opening window's (append-only store, no
        UPDATE path; accepted trade-off in issue #175)."""
        closed = []
        for sci, visit in list(self._open.items()):
            if now_ts - visit["last_ts"] > self._gap:
                closed.append({
                    "label": visit["label"], "windows": visit["windows"],
                    "duration_s": visit["last_ts"] - visit["opened_ts"],
                    "best_conf": visit["best_conf"],
                })
                del self._open[sci]
        return closed


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
