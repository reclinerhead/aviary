# =============================================================================
# project-squirrel -- listener/earl.py
#
# Earl, the ears of the house (epic #133, issue #172): a pearl-resident daemon
# that pulls raw audio from the yard's microphones, runs Cornell's BirdNET on
# every 3-second window, and publishes what he heard to the bus. Earl is
# domain-agnostic on purpose: he reports "Baeolophus bicolor, 0.89, from the
# driveway cam" -- the sightings consumer (sightings.py) decides what that
# means for the bird record, and future consumers decide other things. He
# never transcribes and never records speech (design invariant, enforced in
# gate.decide -- see listener/gate.py rule 1).
#
#   python -m listener.earl        (from the earl venv -- see toddtech-infrastructure/servers/Pearl.md)
#
# Bus contract (topics in bus.py):
#   publishes  audio/events    one JSON object per accepted event, two kinds
#                              (issue #174's two-tier schema): kind
#                              "detection" (species, the BirdNET tier) and
#                              kind "sound" (a coarse AudioSet class --
#                              "Dog", "Siren", "Thunder" -- the YAMNet
#                              tier). Epoch-seconds ts, non-retained:
#                              both are moments, not state. The sightings
#                              consumer archives detections and ignores
#                              the rest by design
#   publishes  audio/status    "online"/"offline", RETAINED, "offline" is the
#                              Last Will -- the weather/status contract,
#                              verbatim. Deliberately a raw string like every
#                              other status topic (the epic sketched per-source
#                              detail in this payload; the house convention
#                              won -- detail lives on audio/sources)
#   publishes  audio/sources   per-source detail, RETAINED JSON (state per
#                              source: starting/online/silent/offline, last-
#                              window ts), republished on every change -- the
#                              weather/current reasoning: source health is
#                              state, and a late-joining dashboard needs it
#                              without a poll loop
#   publishes  audio/mask      the region mask in use (issue #593), RETAINED
#                              JSON: week, masked/unmasked, and every species
#                              the geo model allows here this week with its
#                              geo score -- state, the audio/sources
#                              reasoning. A worker reports its mask up the
#                              queue at startup and on the week tick; the
#                              report is best-effort and can never fail the
#                              gate (the mask itself is in hand first).
#                              sightings persists it so the Aviary's species
#                              mask page can show what the yard could hear
#   subscribes weather/current wind_mph feeds the gate's wind rules; a stale
#                              or absent report degrades to the calm-day
#                              rules (never a dead daemon -- the narrator's
#                              WEATHER_STALE_S reasoning)
#
# Process model -- shaped by two measured Phase 0 facts (see issue #172):
# BirdNET inference is ~1.2s per 3s window per stream (real time, one core),
# and birdnet's predict calls FORK worker children, which deadlocks silently
# if any thread in the forking process holds a lock (paho's network thread
# qualifies). So: the parent owns ALL MQTT and never loads birdnet; each
# source runs in its own SPAWNED worker process (fresh interpreter, no
# inherited threads or locks) that is single-threaded around its predict
# session -- exactly the shape Phase 0 proved for hours. Workers talk back
# over a spawn-context Queue; wind rides a shared Value the other way.
#
# Sources come from the project feed registry (feeds.yml, issue #270): every
# feed flagged earl: true becomes a worker, dispatched on its KIND -- never
# its name, so adding a camera or retiring a mic is a registry edit, not a
# code change here. All sources produce s16le/48kHz/mono on stdout
# (gate.WINDOW_BYTES per window):
#   rtsp     ffmpeg audio-only pull of the feed's url (-allowed_media_types
#            audio: the 48kHz AAC track alone, none of the 4K video's
#            bandwidth). Normally Frigate's credential-free go2rtc restream
#            (#247); camera-side audio must be enabled in the cam's web UI
#            (the Save button is load-bearing -- learned 2026-07-18).
#   command  the feed's cmd verbatim (the rover's ssh+arecord pull of its
#            USB camera mic -- plughw:0,0; Phase 0 D5: the JMTek dongle is
#            silent).
# A dead source restarts with backoff forever; a source that can't start is
# "offline" on audio/sources, never a dead Earl (per-source liveness is the
# entire point of the sources topic).
#
# The region mask (D4, mandatory -- Phase 0 measured why): BirdNET's geo
# model, given MERLE_LATLON and the BirdNET week (48-week year, 4 per month),
# yields the species that plausibly occur here now; the gate discards
# everything else. Recomputed when the week ticks over. If the geo model
# can't load (it downloads on first use), Earl runs UNMASKED and says so
# loudly every status interval -- a missing mask thins nothing silently.
#
# Clips: a per-source ring of raw windows; an accepted window is written as
# prev+event+next (9s of context) to MERLE_EARL_CLIPS/<source>/<ts>-<species>
# .wav, and the event carries the relative path (D1: files + paths -- same
# box, and clips are 10-100x frame size; publish_bytes stays the frames'
# idiom). Events from one window share one clip. A failed write is a clip-
# less event, never a dead worker. Speech windows are dead before any of
# this code runs (gate rule 1) -- no clip, no event, by construction.
#
# Config (env, the MERLE_MQTT conventions):
#   MERLE_MQTT            the broker, REQUIRED (bus.py raises without it)
#   MERLE_LATLON          "lat,lon", REQUIRED (gate.parse_latlon raises --
#                         the D4 mask needs a place to stand)
#   MERLE_FEEDS           alternate feed registry path (default: the repo's
#                         feeds.yml -- see feeds.py). Which sources run, and
#                         how each is captured, lives THERE, not in env.
#                         Also the break-glass for a Frigate that's down: a
#                         local uncommitted registry whose rtsp feed carries
#                         the direct camera URL, credentials and all --
#                         rtsp_argv redacts them from every log line.
#                         (pearl->merle ssh keys for the rover feed remain a
#                         deploy step.)
#   MERLE_EARL_THRESHOLD  BirdNET confidence floor (default
#                         gate.DEFAULT_THRESHOLD)
#   MERLE_EARL_GATE_FLOOR YAMNet routing floor for bird/notable (default
#                         gate.DEFAULT_GATE_FLOOR; speech kills at the
#                         lower gate.SPEECH_FLOOR regardless -- the
#                         invariant is not configurable)
#   MERLE_EARL_CONFIRM_WINDOWS
#                         accepted windows needed before a mid-band species
#                         publishes (default gate.CONFIRM_WINDOWS = 2).
#                         1 is the break-glass that restores publish-on-
#                         first-window everywhere (issue #506) -- for a
#                         station whose yard the hold turns too quiet.
#   MERLE_EARL_CONFIRM_SKIP
#                         confidence at/above which the confirm hold is
#                         skipped and the first window publishes (default
#                         gate.CONFIRM_SKIP = 0.85 -- above Phase 0's
#                         false-positive ceiling with margin; true locals
#                         one-shot exactly as before #506)
#   MERLE_EARL_DB         the bird record, READ here for the species_rules
#                         table (issue #506: the operator's per-species
#                         exclude/threshold/map-override rules, written by
#                         the Aviary, re-read every ~60 s). Read-only open;
#                         unset means no rules, never an error -- the
#                         listener keeps its no-default posture for the
#                         things it OWNS, and this file is sightings'.
#   MERLE_EARL_CLIPS      clip dir (default "clips" under WorkingDirectory;
#                         the unit points it at /srv/media-cache/earl)
#   MERLE_BIRDNET_LIBRARY birdnet.load(..., library=...) for BOTH model
#                         sites (#299). Stations without AVX set "litert" --
#                         the J5005 class SIGILLs on the default path's full
#                         TF, and ai-edge-litert runs the same models at the
#                         same measured speed. Unset omits the kwarg
#                         entirely, so pearl's calls are byte-for-byte
#                         untouched.
#   MERLE_YAMNET_TFLITE   path to the yamnet.tflite classification model:
#                         the gate's LiteRT arm (#300), for the same no-AVX
#                         boxes -- the hub arm needs full TF. Unset runs the
#                         hub arm exactly as before. Either arm failing to
#                         load runs Earl UNGATED and loud, never dead.
# =============================================================================

import json
import multiprocessing
import os
import select
import shlex
import subprocess
import time
import wave
from pathlib import Path

import paho.mqtt.client as mqtt

import bus
import feeds
from listener import gate

CLIENT_ID = "earl"
RESTART_BACKOFF_S = (3, 10, 30, 60)   # then stays at 60 forever
STALL_TIMEOUT_S = 15                  # no bytes at all -> the capture is hung
SOCKET_TIMEOUT_US = STALL_TIMEOUT_S * 1_000_000   # same bar, ffmpeg's units
SILENT_AFTER_WINDOWS = 20             # ~1 min of rms-nothing -> "silent"
SILENT_RMS = 0.001
WEATHER_STALE_S = 30 * 60             # the narrator's staleness rule
GEO_MIN_CONFIDENCE = 0.03             # birdnet's own default, kept explicit
YAMNET_HANDLE = "https://tfhub.dev/google/yamnet/1"   # hub caches locally
YAMNET_FRAME_SAMPLES = 15600   # the tflite variant's FIXED input: 0.975 s @ 16 kHz
YAMNET_HOP_SAMPLES = 7800      # half-frame hop, the variant's convention
YAMNET_LABEL_MEMBER = "yamnet_label_list.txt"   # rides inside the .tflite (zip)
GATE_TOP_K = 7                        # classes handed to gate.route per window
GATE_STATS_WINDOWS = 100              # gate counter log cadence (~5 min)
RULES_RELOAD_S = 60                   # species_rules re-read cadence (#506)

# read_exact's third answer, distinct from EOF's None: the pipe is still open
# but nothing is coming. A sentinel object rather than a falsy value so the
# caller's `is` check can never be confused with an empty read.
STALLED = object()


def week_of(month, day):
    """BirdNET's 48-week year: 4 weeks per month, week 1 = Jan 1-7, days
    22-31 all land in a month's 4th week. Pure so the mask-refresh boundary
    is testable without a clock."""
    return (month - 1) * 4 + min(3, (day - 1) // 7) + 1


def birdnet_week(t=None):
    lt = time.localtime(t if t is not None else time.time())
    return week_of(lt.tm_mon, lt.tm_mday)


def rtsp_argv(url):
    """An audio-only ffmpeg pull of `url`, as argv (no shell -- credentials
    never meet one). Returns (argv, redacted_string_for_logs) -- the
    frames.rtsp_url() convention: build once, redact at the same counter.
    Normally `url` is Frigate's credential-free go2rtc restream (#247) and
    the redaction is a no-op; an alternate registry (MERLE_FEEDS) carrying
    a direct camera URL gets its password masked, never logged."""
    # -timeout is the RTSP demuxer's socket-I/O bar in MICROseconds (issue
    # #201). It is what makes a dropped-but-unreset session END: the camera
    # going away mid-stream sends no FIN, so without it ffmpeg blocks on a
    # socket that stays ESTAB forever and the source dies silently. Measured
    # on pearl's ffmpeg 8.0.1 against a socket that accepts and never speaks:
    # flagged, it exits in ~9 s; unflagged, it was still waiting at 20 s.
    # (NOT -rw_timeout, which the rtsp demuxer does not carry.)
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error",
            "-timeout", str(SOCKET_TIMEOUT_US),
            "-rtsp_transport", "tcp", "-allowed_media_types", "audio",
            "-i", url, "-vn", "-f", "s16le",
            "-ar", str(gate.SAMPLE_RATE), "-ac", "1", "-"]
    redacted = " ".join(feeds.redact_rtsp(a) if a is url else a for a in argv)
    return argv, redacted


def source_commands():
    """The feed registry -> {name: (argv, redacted)} for every feed flagged
    earl: true (issue #270). Dispatch is on a feed's KIND, never its name --
    which sources exist, and how each is captured, is feeds.yml's business
    alone; this function only knows how to turn a kind into a capture. A
    malformed registry, a kind Earl can't capture, or an empty roster all
    fail at startup, not at first window -- the env_float ethos: never run
    half-configured while looking healthy."""
    commands = {}
    for feed in feeds.feeds_for("earl"):
        if feed.kind == "rtsp":
            commands[feed.name] = rtsp_argv(feed.url)
        elif feed.kind == "command":
            commands[feed.name] = (shlex.split(feed.cmd), feed.cmd)
        else:
            # feeds.py validates kinds, but a future kind it knows and Earl
            # doesn't (the rover's MJPEG video, #236) must fail here, loudly,
            # not run as a mystery.
            raise RuntimeError(
                f"feeds.yml: feed {feed.name!r} has kind {feed.kind!r}, "
                "which Earl has no capture for (known: rtsp, command)")
    if not commands:
        raise RuntimeError(
            "no feed is flagged earl: true in the registry "
            f"({feeds.registry_path()}) -- Earl has nothing to listen to")
    return commands


# --- the worker: one source, one process, single-threaded ---------------------

def write_clip(clips_dir, relpath, windows):
    """prev+event+next raw windows -> one WAV. Returns relpath, or None on
    any failure (logged by the caller; a missing clip is a gap, never a dead
    worker)."""
    path = Path(clips_dir) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(gate.BYTES_PER_SAMPLE)
        w.setframerate(gate.SAMPLE_RATE)
        for chunk in windows:
            w.writeframes(chunk)
    return relpath


def read_exact(stream, n, timeout=None):
    """Exactly n bytes from a pipe via os.read; None on EOF, STALLED if
    `timeout` seconds pass with no bytes arriving at all. (The Phase 0
    listener's loop: os.read, not sys.stdin.buffer -- no buffered-reader lock
    for birdnet's fork to inherit, even though this process also keeps no
    other threads. The timeout is select on the fd for that same reason: a
    watchdog thread here would be the fork trap, so the wait itself has to be
    the thing that expires.)

    The bar is bytes, not windows: the clock restarts on every chunk, so a
    stream delivering anything at all is never called stalled -- only one
    that has gone completely quiet, which a live 48 kHz source cannot do."""
    fd = stream.fileno()
    parts, got = [], 0
    while got < n:
        if timeout is not None and not select.select([fd], [], [], timeout)[0]:
            return STALLED
        chunk = os.read(fd, n - got)
        if not chunk:
            return None
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def birdnet_load_kwargs(environ=None):
    """Extra kwargs for both birdnet.load() sites, from MERLE_BIRDNET_LIBRARY
    (issue #299). No-AVX stations (#212's birdhouse class) set "litert";
    unset or blank returns {} so the kwarg is ABSENT, not library=None --
    pearl's calls stay exactly what they were, and the package default
    remains the package's business. Reads the env at call time: the load
    happens in spawned workers, and spawn hands each child the parent's
    environment."""
    environ = os.environ if environ is None else environ
    library = environ.get("MERLE_BIRDNET_LIBRARY", "").strip()
    return {"library": library} if library else {}


def load_species_rules(db_path):
    """The operator's per-species rules (issue #506) from THIS box's
    earl.db -- the species_rules table the Aviary writes (its second write
    after #216) and sightings' connect() creates. Returns exactly what
    gate.decide's `rules` argument takes: {species_sci: {"exclude": bool,
    "threshold": float|None, "always_include": bool}}.

    Read-only URI open, deliberately: the listener may neither create the
    store (the removal route's write-mode lesson, one process over) nor
    hold anything against the two processes that do write it. A missing
    file, an unset path, or a pre-#506 store without the table is an empty
    map -- a box before its first rule, not an error. Anything else raises
    and the caller keeps the rules it had."""
    if not db_path or not os.path.exists(db_path):
        return {}
    import sqlite3
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro",
                           uri=True)
    try:
        try:
            rows = conn.execute(
                "SELECT species_sci, exclude, threshold, always_include"
                " FROM species_rules").fetchall()
        except sqlite3.OperationalError:
            return {}   # pre-#506 store: no table yet
        return {sci: {"exclude": bool(exclude),
                      "threshold": (float(floor) if floor is not None
                                    else None),
                      "always_include": bool(always)}
                for sci, exclude, floor, always in rows}
    finally:
        conn.close()


def region_mask(lat, lon, week):
    """The D4 mask: geo-model species for here-and-now, as (allowed, rows).
    `allowed` is the set of scientific names the gate uses -- computed
    exactly as it always was, from the label column alone -- and raises on
    any failure, the caller deciding how loud to be and running unmasked
    (never dead). `rows` is the same prediction shaped for the bus (issue
    #593: sci, common, geo score, sorted by sci) and is best-effort: a
    column the report wants that the gate doesn't is never a reason to
    thin the gate, so a failure there answers None and the mask stands."""
    import birdnet
    geo = birdnet.load("geo", "2.4", "tf", **birdnet_load_kwargs())
    df = geo.predict(lat, lon, week=week,
                     min_confidence=GEO_MIN_CONFIDENCE).to_dataframe()
    allowed = {gate.split_label(label)[0] for label in df["species_name"]}
    try:
        rows = mask_rows_from(zip(df["species_name"], df["confidence"]))
    except Exception:
        rows = None
    return allowed, rows


def allowed_species_set(lat, lon, week):
    """The gate's set alone (fieldmode/preview.py's caller)."""
    return region_mask(lat, lon, week)[0]


def mask_rows_from(pairs):
    """(label, geo score) pairs -> the audio/mask species rows: BirdNET's
    "Scientific_Common" split into the two names, the score rounded to
    four places (the geo model's own precision is three), sorted by
    scientific name so identical masks serialize identically. A label
    repeated by the model keeps its best score. Pure, so the shape on the
    wire is tested rather than eyeballed."""
    best = {}
    for label, conf in pairs:
        sci, common = gate.split_label(label)
        geo = round(float(conf), 4)
        prior = best.get(sci)
        if prior is None or geo > prior["geo"]:
            best[sci] = {"species_sci": sci, "species_common": common,
                         "geo": geo}
    return [best[sci] for sci in sorted(best)]


def mask_payload(latlon, week, rows, error=None, now=None):
    """The audio/mask body (issue #593). `rows` None means the geo model
    could not be asked at all -- status "unmasked", an empty species list,
    and the error text the journal already prints, so the Aviary can say
    "running unmasked since ..." instead of showing an empty grid. Pure
    (injected clock) for the same reason as mask_rows_from."""
    lat, lon = latlon
    return {
        "ts": int(now if now is not None else time.time()),
        "week": int(week),
        "lat": float(lat),
        "lon": float(lon),
        "status": "masked" if rows is not None else "unmasked",
        "species": list(rows) if rows is not None else [],
        "error": str(error) if error else None,
    }


def report_mask(queue, name, payload, log):
    """Hand the worker's mask to the parent (who owns MQTT) -- best-effort
    by contract: the gate already holds its set, and a report that cannot
    be queued logs one line and leaves the worker exactly where it was. A
    report failure must never cost a detection, let alone a worker."""
    try:
        queue.put(("mask", name, payload))
        return True
    except Exception as e:
        log(f"mask report failed ({e}) -- the mask itself is unaffected")
        return False


def yamnet_frame_starts(n_samples):
    """Start offsets of the full YAMNET_FRAME_SAMPLES-sized frames a 16 kHz
    window yields at half-frame hop. Full frames only: the tail a fixed
    framing can't cover (~75 ms of a 3 s window) goes unscored -- speech
    longer than that cannot hide in it, and the hub model's own patch
    framing drops partial patches too. Pure so the fencepost is testable."""
    if n_samples < YAMNET_FRAME_SAMPLES:
        return []
    return list(range(0, n_samples - YAMNET_FRAME_SAMPLES + 1,
                      YAMNET_HOP_SAMPLES))


def yamnet_labels(model_path):
    """The 521 display names ride INSIDE the .tflite, as zip member
    yamnet_label_list.txt (a tflite file with metadata is also a valid zip
    read from the end -- stdlib zipfile opens it as-is). One name per line,
    same display-name vocabulary as the hub arm's CSV column, so the gate
    map needs no translation."""
    import io
    import zipfile
    with zipfile.ZipFile(model_path) as z:
        with z.open(YAMNET_LABEL_MEMBER) as f:
            return [line.strip()
                    for line in io.TextIOWrapper(f, encoding="utf-8")
                    if line.strip()]


def _load_yamnet_litert(np, log, model_path):
    """The seam's LiteRT arm (#300), for boxes where full TF cannot import
    (#212's no-AVX stations). Same classify contract as the hub arm. The
    tflite classification variant takes a FIXED 0.975 s frame, so the 3 s
    window is framed at half-frame hop and per-class scores are MEANED over
    frames -- the hub arm's scores.mean(axis=0) reproduced one level up, so
    the gate's tuned floors keep their meaning. Measured on birdhouse:
    9.7 ms per frame, ~50 ms per 3 s window -- pearl's hub-arm class."""
    from ai_edge_litert.interpreter import Interpreter

    names = yamnet_labels(model_path)
    unknown = gate.unknown_gate_classes(names)
    if unknown:
        log(f"GATE MAP WARNING: routing entries the model doesn't know "
            f"(they will never match): {unknown}")

    interp = Interpreter(model_path=model_path)
    interp.allocate_tensors()
    inp = interp.get_input_details()[0]["index"]
    out = interp.get_output_details()[0]["index"]

    def classify(audio48k):
        wave = (audio48k[: len(audio48k) // 3 * 3]
                .reshape(-1, 3).mean(axis=1).astype(np.float32))
        starts = yamnet_frame_starts(len(wave))
        if not starts:
            return []   # shorter than one frame -> no verdict; route() reads
                        # an empty list as "listen", the do-no-harm arm
        scores = None
        for s in starts:
            interp.set_tensor(inp, wave[s:s + YAMNET_FRAME_SAMPLES])
            interp.invoke()
            frame = np.array(interp.get_tensor(out)).reshape(-1)
            scores = frame if scores is None else scores + frame
        scores = scores / len(starts)
        top = scores.argsort()[-GATE_TOP_K:][::-1]
        return [(names[i], float(scores[i])) for i in top]

    log(f"yamnet gate: litert arm ({model_path})")
    return classify


def load_yamnet(np, log):
    """The front gate, and the 2b seam (issue #174): returns
    classify(audio48k) -> [(class_name, score)] best-first, or None when the
    gate can't load -- Earl then runs UNGATED (Phase 1 behavior) and says so
    in every stats line, never silently. Two arms behind the one seam --
    MERLE_YAMNET_TFLITE set routes to LiteRT (#300, the no-AVX stations);
    unset is the hub arm, untouched. The Coral phase (2b) adds a third the
    same way; nothing upstream changes.

    CPU cost, measured on pearl: ~56-80 ms warm per 3 s window -- a few
    percent of the budget, and additive now rather than a saving, since
    every non-speech window still reaches BirdNET. Input is our native
    48 kHz mono; YAMNet wants 16 kHz, and a mean-of-3 decimation is a
    good-enough low-pass for classification (BirdNET always sees the
    full-rate audio)."""
    try:
        tflite_path = os.environ.get("MERLE_YAMNET_TFLITE", "").strip()
        if tflite_path:
            return _load_yamnet_litert(np, log, tflite_path)

        import csv

        import tensorflow as tf
        import tensorflow_hub as hub

        model = hub.load(YAMNET_HANDLE)
        with tf.io.gfile.GFile(model.class_map_path().numpy().decode()) as f:
            names = [row["display_name"] for row in csv.DictReader(f)]
        unknown = gate.unknown_gate_classes(names)
        if unknown:
            log(f"GATE MAP WARNING: routing entries the model doesn't know "
                f"(they will never match): {unknown}")

        def classify(audio48k):
            wave = (audio48k[: len(audio48k) // 3 * 3]
                    .reshape(-1, 3).mean(axis=1).astype(np.float32))
            scores = model(wave)[0].numpy().mean(axis=0)
            top = scores.argsort()[-GATE_TOP_K:][::-1]
            return [(names[i], float(scores[i])) for i in top]

        return classify
    except Exception as e:
        log(f"YAMNET GATE UNAVAILABLE ({e}) -- running ungated: every "
            "window goes to BirdNET, speech relies on BirdNET's human "
            "check alone (the Phase 1 posture)")
        return None


def source_worker(name, argv, redacted, latlon, threshold, gate_floor,
                  clips_dir, queue, wind_mph, confirm_windows, confirm_skip,
                  rules_db):
    """Worker main (spawn target). Owns: the capture subprocess (restarted
    with backoff forever), the YAMNet gate, the BirdNET session, the geo
    mask, the clip ring, the confirm hold and the species-rules reload
    (#506). Reports to the parent over `queue` as ("state", name, state)
    and ("event", name, payload). Reads current wind from `wind_mph` (a
    shared Value; negative means unknown)."""
    import numpy as np

    import birdnet

    log = lambda msg: print(f"[{name}] {msg}", flush=True)
    log(f"worker up: {redacted}")

    model = birdnet.load("acoustic", "2.4", "tf", **birdnet_load_kwargs())
    classify = load_yamnet(np, log)

    week = birdnet_week()
    try:
        allowed, mask = region_mask(*latlon, week)
        log(f"region mask: {len(allowed)} species for week {week}")
    except Exception as e:
        allowed = None
        mask = None
        log(f"REGION MASK UNAVAILABLE ({e}) -- running unmasked, D4 is off")
        report_mask(queue, name, mask_payload(latlon, week, None, error=e),
                    log)
    # The report rides AFTER the gate has its set (#593): nothing here can
    # change `allowed`. No rows for a working mask (the best-effort shaping
    # failed) is logged, not reported -- a retained "masked, 0 species"
    # would be a lie about a mask that is in fact thinning the yard.
    if allowed is not None:
        if mask is None:
            log("mask report skipped: rows unavailable (mask itself is on)")
        else:
            report_mask(queue, name, mask_payload(latlon, week, mask), log)

    backoff = 0
    visits = gate.VisitTracker()         # bird visits (#175)
    sound_visits = gate.VisitTracker()   # notable-sound visits (#174) --
    # separate tracker: AudioSet class names and species names are different
    # vocabularies and must not share a debounce namespace
    confirms = gate.ConfirmTracker(confirm_windows, confirm_skip)   # #506
    # A held window's raw audio, keyed by its candidate clip path, so the
    # clip can still be written if a second window confirms -- and simply
    # dropped with the hold if none does (a one-off 0.72 leaves no file).
    # Outside the capture loop, like the trackers: a hold rides out a brief
    # source blip exactly as an open visit does.
    held_clips = {}
    held_await = []   # held paths still waiting on their post-roll window
    counters = {"windows": 0, "speech": 0, "notable": 0, "listen": 0,
                "ungated": 0, "gate_ms": 0.0}

    # The operator's per-species rules (#506), re-read every RULES_RELOAD_S
    # so an Aviary edit lands within a minute without a restart. No
    # MERLE_EARL_DB on the unit means no rules -- said once, out loud.
    rules = {}
    rules_ts = 0.0
    if not rules_db:
        log("species rules: MERLE_EARL_DB not set on this unit -- running "
            "without per-species rules")
    with model.predict_session(top_k=3, show_stats=None) as session:
        while True:
            if birdnet_week() != week:
                week = birdnet_week()
                try:
                    allowed, mask = region_mask(*latlon, week)
                    log(f"region mask refreshed: {len(allowed)} species, "
                        f"week {week}")
                    # A refresh that kept the old mask reports nothing: the
                    # retained payload still describes the mask in use.
                    if mask is not None:
                        report_mask(queue, name,
                                    mask_payload(latlon, week, mask), log)
                except Exception as e:
                    log(f"region mask refresh failed ({e}) -- keeping the "
                        "old mask")

            queue.put(("state", name, "starting"))
            try:
                capture = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL)
            except OSError as e:
                queue.put(("state", name, "offline"))
                delay = RESTART_BACKOFF_S[min(backoff,
                                              len(RESTART_BACKOFF_S) - 1)]
                backoff += 1
                log(f"capture failed to start ({e}); retry in {delay}s")
                time.sleep(delay)
                continue

            state = "starting"
            stalled = False
            quiet_windows = 0
            prev_window = None
            # Clip writes waiting on their post-roll window (issue #175:
            # a list, since one window can open/upgrade several visits).
            pending = []
            while True:
                buf = read_exact(capture.stdout, gate.WINDOW_BYTES,
                                 timeout=STALL_TIMEOUT_S)
                if buf is None:
                    break
                if buf is STALLED:
                    # The device went away without closing the pipe (a camera
                    # unplugged mid-session, a rover powered down to charge).
                    # Kill the capture so the restart path below is the same
                    # one a clean exit takes -- one way back online, not two.
                    stalled = True
                    capture.kill()
                    break

                if state == "starting":
                    state = "online"
                    backoff = 0
                    queue.put(("state", name, state))

                for relpath, chunks in pending:
                    chunks.append(buf)
                    try:
                        write_clip(clips_dir, relpath, chunks)
                    except OSError as e:
                        log(f"clip write failed ({relpath}): {e}")
                pending = []

                # Held windows collect their post-roll the same way pending
                # writes do (#506) -- in memory, though: nothing touches
                # disk until a second window confirms the species.
                for relpath in held_await:
                    chunks = held_clips.get(relpath)
                    if chunks is not None:
                        chunks.append(buf)
                held_await = []

                audio = (np.frombuffer(buf, np.int16)
                         .astype(np.float32) / 32768.0)
                rms = float(np.sqrt((audio ** 2).mean()))
                quiet_windows = quiet_windows + 1 if rms < SILENT_RMS else 0
                if quiet_windows >= SILENT_AFTER_WINDOWS and state != "silent":
                    state = "silent"
                    queue.put(("state", name, state))
                elif quiet_windows == 0 and state == "silent":
                    state = "online"
                    queue.put(("state", name, state))

                ts = time.time()
                if rules_db and ts - rules_ts >= RULES_RELOAD_S:
                    first = rules_ts == 0.0
                    rules_ts = ts
                    try:
                        fresh = load_species_rules(rules_db)
                    except Exception as e:
                        log(f"species rules reload failed ({e}) -- keeping "
                            f"the {len(rules)} in hand")
                    else:
                        if first or fresh != rules:
                            excluded = sum(1 for r in fresh.values()
                                           if r["exclude"])
                            log(f"species rules: {len(fresh)} loaded "
                                f"({excluded} excluded)")
                        rules = fresh

                for tracker in (visits, sound_visits):
                    for closed in tracker.expire(ts):
                        _, common = gate.split_label(closed["label"])
                        log(f"visit closed: {common}, {closed['windows']} "
                            f"windows, {closed['duration_s']:.0f}s, "
                            f"best {closed['best_conf']:.2f}")
                # Unconfirmed holds past the gap are dropped -- no event,
                # no clip, no life-list row (#506) -- and the journal says
                # so, because a bird the hold silenced should be findable.
                for gone in confirms.expire(ts):
                    _, common = gate.split_label(gone["label"])
                    for clip in gone["clips"]:
                        held_clips.pop(clip, None)
                    n = gone["windows"]
                    log(f"confirm dropped: {common}, {n} window"
                        f"{'' if n == 1 else 's'}, "
                        f"best {gone['best_conf']:.2f}")

                wind = wind_mph.value if wind_mph.value >= 0 else None
                windy = wind is not None and wind > gate.WIND_GATE_MPH
                context = [w for w in (prev_window, buf) if w is not None]

                # YAMNet first (issue #174): it kills speech and names
                # notable sounds. It does NOT decide whether BirdNET runs --
                # see gate.route's docstring for the afternoon that taught
                # us why. Speech is the only verdict that stops anything.
                counters["windows"] += 1
                if classify is not None:
                    t0 = time.perf_counter()
                    verdict, hits = gate.route(classify(audio),
                                               floor=gate_floor)
                    counters["gate_ms"] += (time.perf_counter() - t0) * 1000
                    counters[verdict] += 1
                else:
                    verdict, hits = "listen", []
                    counters["ungated"] += 1

                if counters["windows"] % GATE_STATS_WINDOWS == 0:
                    ms = counters["gate_ms"] / max(
                        counters["windows"] - counters["ungated"], 1)
                    log(f"gate stats: {counters['windows']} windows -- "
                        f"{counters['listen']} listen, "
                        f"{counters['speech']} speech-killed, "
                        f"{counters['notable']} notable, "
                        f"{counters['ungated']} ungated; "
                        f"gate {ms:.0f} ms avg")

                if verdict == "speech":
                    prev_window = buf
                    continue

                # A notable sound gets its own event -- and then the window
                # STILL goes to BirdNET below: a dog barking doesn't mean no
                # bird is singing.
                if verdict == "notable":
                    for klass, score in hits:
                        # A sound visit writes NO clip (#327). The tracker
                        # still debounces and tracks the visit's best for
                        # the "visit closed" line, but the candidate path is
                        # None because nothing would ever read the file:
                        # sightings.py never persists kind:"sound" (the #174
                        # two-tier design), so once the live ticker moves on,
                        # no route, table, or query can reach it. Measured on
                        # birdhouse's first outdoor evening: crickets alone
                        # were 43% of the clip tree, all of it unreachable.
                        # The EVENT still flows -- class, confidence, rms --
                        # so the ticker, gate stats, and the future training
                        # loop (#278) keep every observation. Only the orphan
                        # bytes stop. Clips return if sound events ever gain
                        # a persistent consumer.
                        action, _ = sound_visits.observe(
                            klass, ts, score, None)
                        if action != "open":
                            continue
                        payload = gate.shape_sound_event(
                            source=name, ts=ts, klass=klass,
                            confidence=score, clip_relpath=None,
                            windy=windy, rms=rms)
                        queue.put(("event", name, payload))
                        log(f"sound: {klass} {score:.2f}"
                            f"{' (wind-suspect)' if windy else ''}")

                # Every non-speech window reaches BirdNET -- the Phase 1
                # path, byte-identical from here (including its own human
                # check, the invariant's second layer).
                df = session.run_arrays((audio, gate.SAMPLE_RATE)).to_dataframe()
                predictions = list(zip(df["species_name"], df["confidence"]))
                accepted, windy = gate.decide(
                    predictions, threshold=threshold, wind_mph=wind,
                    allowed_species=allowed, rules=rules)
                if not accepted:
                    prev_window = buf
                    continue

                for label, conf in accepted:
                    candidate = gate.clip_relpath(name, ts, label)
                    common = gate.split_label(label)[1]
                    # The confirm hold (#506) sits between decide() and the
                    # visit tracker: a mid-band window waits for a second
                    # accepted window of its species before anything
                    # publishes; the skip bar and the break-glass config
                    # pass straight through. Speech never gets here --
                    # decide() already killed it (rule 1 is upstream of
                    # everything, including this).
                    verdict, held = confirms.observe(
                        label, ts, conf, candidate, rms=rms, windy=windy)
                    if verdict == "hold":
                        held_clips[candidate] = list(context)
                        held_await.append(candidate)
                        log(f"{common} {conf:.2f} -- held for a second "
                            "window")
                        continue
                    if verdict == "publish":
                        window_batch = [{"label": label, "ts": ts,
                                         "confidence": conf,
                                         "clip": candidate, "rms": rms,
                                         "windy": windy}]
                    else:   # "confirm": the best held window opens the
                        # visit and publishes; the rest feed through as
                        # ordinary observations so the visit's clock and
                        # stats stay honest (they extend, never publish).
                        best = max(held, key=lambda w: w["confidence"])
                        window_batch = ([best]
                                        + [w for w in held if w is not best])
                    for w in window_batch:
                        action, relpath = visits.observe(
                            w["label"], w["ts"], w["confidence"], w["clip"])
                        if action == "extend":
                            held_clips.pop(w["clip"], None)
                            continue
                        held_bytes = held_clips.pop(w["clip"], None)
                        if held_bytes is not None:
                            # A held window's bytes, post-roll already in
                            # hand: the clip the hold delayed is written
                            # the moment its species confirms.
                            try:
                                write_clip(clips_dir, relpath, held_bytes)
                            except OSError as e:
                                log(f"clip write failed ({relpath}): {e}")
                        else:
                            pending.append((relpath, list(context)))
                        w_common = gate.split_label(w["label"])[1]
                        if action == "open":
                            payload = gate.shape_event(
                                source=name, ts=w["ts"], label=w["label"],
                                confidence=w["confidence"],
                                clip_relpath=relpath, windy=w["windy"],
                                rms=w["rms"])
                            queue.put(("event", name, payload))
                            log(f"{w_common} {w['confidence']:.2f}"
                                f"{' (wind-suspect)' if w['windy'] else ''}")
                        else:   # "best": rewrite the visit's clip in place
                            log(f"{w_common} {w['confidence']:.2f} -- visit "
                                "best, clip upgraded")
                prev_window = buf

            # EOF: finish clips with the context in hand (a 6s clip beats a
            # dangling event pointer). Visits deliberately survive the drop,
            # and so do confirm holds (#506) -- their bytes just stop
            # waiting for a post-roll the dead capture will never deliver
            # (a confirmed release writes the 6s it has, the pending-at-EOF
            # rule one stage earlier).
            for relpath, chunks in pending:
                try:
                    write_clip(clips_dir, relpath, chunks)
                except OSError as e:
                    log(f"clip write failed ({relpath}): {e}")
            pending = []
            held_await = []

            capture.wait()
            queue.put(("state", name, "offline"))
            delay = RESTART_BACKOFF_S[min(backoff, len(RESTART_BACKOFF_S) - 1)]
            backoff += 1
            why = (f"capture stalled (no audio for {STALL_TIMEOUT_S}s)"
                   if stalled else f"capture ended (rc {capture.returncode})")
            log(f"{why}; retry in {delay}s")
            time.sleep(delay)


# --- the parent: MQTT in, MQTT out, workers supervised ------------------------

class Earl:
    """The orchestrator. All MQTT lives here (paho's network thread never
    shares a process with birdnet's forks); all audio lives in the workers."""

    def __init__(self, commands, latlon, threshold, gate_floor, clips_dir,
                 confirm_windows=gate.CONFIRM_WINDOWS,
                 confirm_skip=gate.CONFIRM_SKIP, rules_db=None):
        self._ctx = multiprocessing.get_context("spawn")
        self._queue = self._ctx.Queue()
        self._wind = self._ctx.Value("d", -1.0)   # <0 = unknown
        self._wind_ts = 0.0
        self._commands = commands
        self._latlon = latlon
        self._threshold = threshold
        self._gate_floor = gate_floor
        self._clips_dir = clips_dir
        self._confirm_windows = confirm_windows
        self._confirm_skip = confirm_skip
        self._rules_db = rules_db
        self._states = {name: "starting" for name in commands}
        self._last_window = {name: None for name in commands}
        self._workers = {}

        # pid-suffixed client id (issue #175): a broker disconnects the older
        # of two clients sharing an id, so a hand-run Earl beside the unit
        # would kick the unit off the bus in a loop (a desk twin did exactly
        # this to the sightings consumer). QoS-0 clean sessions make the id
        # cosmetic; uniqueness is free insurance. Presence stays on the
        # explicit status topic, which is not client-id-derived.
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=f"{CLIENT_ID}-{os.getpid()}")
        self._client.will_set(bus.AUDIO_STATUS_TOPIC, "offline", retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_weather

    # MQTT callbacks (paho's network thread -- keep them assignment-cheap,
    # the narrator's latest_weather pattern)
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        client.publish(bus.AUDIO_STATUS_TOPIC, "online", qos=0, retain=True)
        client.subscribe(bus.WEATHER_CURRENT_TOPIC)
        self._publish_sources()

    def _on_weather(self, client, userdata, message):
        try:
            report = json.loads(message.payload)
            wind = float(report["wind_mph"])
            ts = float(report.get("ts", time.time()))
        except (ValueError, TypeError, KeyError):
            return
        self._wind_ts = ts
        self._wind.value = wind

    def _publish_sources(self):
        payload = {
            "ts": int(time.time()),
            "sources": {
                name: {"state": self._states[name],
                       "last_window_ts": self._last_window[name]}
                for name in self._commands
            },
        }
        self._client.publish(bus.AUDIO_SOURCES_TOPIC, json.dumps(payload),
                             qos=0, retain=True)

    def _spawn(self, name):
        argv, redacted = self._commands[name]
        # daemon=False is load-bearing: birdnet's predict session spawns its
        # own child processes, and Python forbids daemonic processes from
        # having children. The cost is owning shutdown ourselves -- _close()
        # terminates and joins; under systemd the cgroup is the backstop.
        worker = self._ctx.Process(
            target=source_worker, name=f"earl-{name}",
            args=(name, argv, redacted, self._latlon, self._threshold,
                  self._gate_floor, self._clips_dir, self._queue,
                  self._wind, self._confirm_windows, self._confirm_skip,
                  self._rules_db),
            daemon=False)
        worker.start()
        self._workers[name] = worker
        return worker

    def run(self):
        host, port = bus.broker_address()
        self._client.connect_async(host, port)
        self._client.loop_start()
        for name in self._commands:
            self._spawn(name)
        confirm = ("off (publish on first window)"
                   if self._confirm_windows <= 1
                   else f"{self._confirm_windows} windows, "
                        f"skip at {self._confirm_skip}")
        print(f"[earl] listening: {', '.join(self._commands)} "
              f"(threshold {self._threshold}, confirm {confirm}, "
              f"lat/lon {self._latlon[0]},{self._latlon[1]})", flush=True)

        try:
            while True:
                self._tick()
        except KeyboardInterrupt:
            print("[earl] signing off", flush=True)
        finally:
            self._close()

    def _tick(self):
        # Wind staleness: an expired report degrades to the calm-day rules.
        if self._wind.value >= 0 and time.time() - self._wind_ts > WEATHER_STALE_S:
            self._wind.value = -1.0

        # A dead worker is a bug (workers restart their own captures), but
        # Earl outlives it: respawn and say so.
        for name, worker in list(self._workers.items()):
            if not worker.is_alive():
                print(f"[earl] worker {name} died (rc {worker.exitcode}); "
                      "respawning", flush=True)
                self._states[name] = "offline"
                self._publish_sources()
                self._spawn(name)

        try:
            kind, name, value = self._queue.get(timeout=1.0)
        except Exception:   # queue.Empty (spawn ctx re-exports it)
            return
        if kind == "state":
            if value == "online":
                self._last_window[name] = int(time.time())
            self._states[name] = value
            self._publish_sources()
        elif kind == "event":
            self._last_window[name] = value["ts"]
            self._client.publish(bus.AUDIO_EVENTS_TOPIC, json.dumps(value),
                                 qos=0, retain=False)
        elif kind == "mask":
            # Retained: the mask is state (#593). Every worker on a unit
            # computes the same mask (one MERLE_LATLON per box), so the last
            # report wins and the duplicates are byte-identical. Wrapped
            # because the parent IS Earl -- a bad payload may cost one
            # journal line, never the daemon.
            try:
                self._client.publish(bus.AUDIO_MASK_TOPIC, json.dumps(value),
                                     qos=0, retain=True)
            except Exception as e:
                print(f"[earl] mask publish failed ({e})", flush=True)

    def _close(self):
        for worker in self._workers.values():
            worker.terminate()
        for worker in self._workers.values():
            worker.join(timeout=5)
            if worker.is_alive():
                worker.kill()
        # Graceful sign-off, the EventPublisher.close() reasoning: a clean
        # DISCONNECT suppresses the Last Will, so say offline ourselves.
        info = self._client.publish(bus.AUDIO_STATUS_TOPIC, "offline",
                                    qos=0, retain=True)
        try:
            info.wait_for_publish(timeout=2)
        except (ValueError, RuntimeError):
            pass
        self._client.loop_stop()
        self._client.disconnect()


def main():
    latlon = gate.parse_latlon(os.environ.get("MERLE_LATLON"))
    threshold = float(os.environ.get("MERLE_EARL_THRESHOLD",
                                     gate.DEFAULT_THRESHOLD))
    gate_floor = float(os.environ.get("MERLE_EARL_GATE_FLOOR",
                                      gate.DEFAULT_GATE_FLOOR))
    clips_dir = os.environ.get("MERLE_EARL_CLIPS", "").strip() or "clips"
    confirm_windows = int(os.environ.get("MERLE_EARL_CONFIRM_WINDOWS",
                                         gate.CONFIRM_WINDOWS))
    confirm_skip = float(os.environ.get("MERLE_EARL_CONFIRM_SKIP",
                                        gate.CONFIRM_SKIP))
    # The bird record, read here only for species_rules (#506). Optional on
    # purpose: the file is sightings' to own and create, and a unit without
    # the env runs ruleless (each worker says so at startup).
    rules_db = os.environ.get("MERLE_EARL_DB", "").strip() or None
    commands = source_commands()
    bus.broker_address()   # fail now, not after the model loads
    Earl(commands, latlon, threshold, gate_floor, clips_dir,
         confirm_windows=confirm_windows, confirm_skip=confirm_skip,
         rules_db=rules_db).run()


if __name__ == "__main__":
    main()
