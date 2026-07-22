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
#   python -m listener.earl        (from the earl venv -- see Servers/Pearl.md)
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
# Sources produce s16le/48kHz/mono on stdout (gate.WINDOW_BYTES per window):
#   amcrest  ffmpeg audio-only RTSP pull from the driveway cam's main stream
#            (-allowed_media_types audio: the 48kHz AAC track alone, none of
#            the 4K video's bandwidth). Camera-side audio must be enabled in
#            the cam UI (done 2026-07-18; the Save button is load-bearing).
#   rover    arecord on merle piped over ssh (the camera's own USB mic --
#            plughw:0,0; Phase 0 D5: the JMTek dongle is silent). Overridable
#            whole via MERLE_EARL_ROVER_CMD for the day the rover moves.
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
#   MERLE_EARL_SOURCES    comma list from {amcrest, rover} (default "amcrest")
#   MERLE_RTSP_URL        full RTSP URL for the amcrest source -- the normal
#                         posture since Frigate (issue #247): the go2rtc
#                         restream (rtsp://127.0.0.1:8554/driveway on pearl),
#                         credential-free, so MERLE_RTSP_* below go unused
#   MERLE_RTSP_HOST       the Amcrest (default 192.168.1.102) -- the direct
#                         fallback form, used only when MERLE_RTSP_URL unset
#   MERLE_RTSP_USER       (default admin)
#   MERLE_RTSP_PASS       REQUIRED when amcrest is a source and MERLE_RTSP_URL
#                         is unset (never committed, never logged --
#                         rtsp_argv redacts)
#   MERLE_EARL_ROVER_CMD  full rover capture command (default: the ssh+arecord
#                         one-liner; pearl->merle ssh keys are a deploy step)
#   MERLE_EARL_THRESHOLD  BirdNET confidence floor (default
#                         gate.DEFAULT_THRESHOLD)
#   MERLE_EARL_GATE_FLOOR YAMNet routing floor for bird/notable (default
#                         gate.DEFAULT_GATE_FLOOR; speech kills at the
#                         lower gate.SPEECH_FLOOR regardless -- the
#                         invariant is not configurable)
#   MERLE_EARL_CLIPS      clip dir (default "clips" under WorkingDirectory;
#                         the unit points it at /srv/media-cache/earl)
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
from listener import gate

CLIENT_ID = "earl"
DEFAULT_RTSP_HOST = "192.168.1.102"
DEFAULT_ROVER_CMD = ("ssh -o BatchMode=yes -o ConnectTimeout=5 "
                     "-o ServerAliveInterval=5 -o ServerAliveCountMax=3 "
                     "todd@merle "
                     "arecord -D plughw:0,0 -f S16_LE -r 48000 -c 1 -t raw -q")
RESTART_BACKOFF_S = (3, 10, 30, 60)   # then stays at 60 forever
STALL_TIMEOUT_S = 15                  # no bytes at all -> the capture is hung
SOCKET_TIMEOUT_US = STALL_TIMEOUT_S * 1_000_000   # same bar, ffmpeg's units
SILENT_AFTER_WINDOWS = 20             # ~1 min of rms-nothing -> "silent"
SILENT_RMS = 0.001
WEATHER_STALE_S = 30 * 60             # the narrator's staleness rule
GEO_MIN_CONFIDENCE = 0.03             # birdnet's own default, kept explicit
YAMNET_HANDLE = "https://tfhub.dev/google/yamnet/1"   # hub caches locally
GATE_TOP_K = 7                        # classes handed to gate.route per window
GATE_STATS_WINDOWS = 100              # gate counter log cadence (~5 min)

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


def rtsp_argv(host, user, password, url=None):
    """The driveway audio-only pull, as argv (no shell -- the password never
    meets a shell). Returns (argv, redacted_string_for_logs) -- the
    frames.rtsp_url() convention: build once, redact at the same counter.
    `url` overrides the whole construction (issue #247: the Frigate/go2rtc
    restream carries no credentials, so host/user/password go unused and an
    empty password redacts nothing)."""
    if url is None:
        url = (f"rtsp://{user}:{password}@{host}:554/"
               "cam/realmonitor?channel=1&subtype=0")
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
    redacted = " ".join(argv)
    if password:
        redacted = redacted.replace(password, "***")
    return argv, redacted


def source_commands():
    """MERLE_EARL_SOURCES -> {name: (argv, redacted)}. Unknown names and an
    amcrest without MERLE_RTSP_PASS fail at startup, not at first window --
    the env_float ethos: never run half-configured while looking healthy."""
    names = [s.strip() for s in
             os.environ.get("MERLE_EARL_SOURCES", "amcrest").split(",")
             if s.strip()]
    commands = {}
    for name in names:
        if name == "amcrest":
            override = os.environ.get("MERLE_RTSP_URL", "").strip()
            if override:
                # Issue #247: the Frigate/go2rtc restream. No credentials in
                # a restream URL, so no password requirement -- the camera
                # session is Frigate's to hold, Earl just listens to the copy.
                commands[name] = rtsp_argv(None, None, "", url=override)
                continue
            password = os.environ.get("MERLE_RTSP_PASS", "")
            if not password:
                raise RuntimeError(
                    "MERLE_RTSP_PASS is not set and amcrest is a configured "
                    "source (and MERLE_RTSP_URL is unset) -- the camera "
                    "will not open.")
            commands[name] = rtsp_argv(
                os.environ.get("MERLE_RTSP_HOST", DEFAULT_RTSP_HOST),
                os.environ.get("MERLE_RTSP_USER", "admin"), password)
        elif name == "rover":
            cmd = os.environ.get("MERLE_EARL_ROVER_CMD", DEFAULT_ROVER_CMD)
            commands[name] = (shlex.split(cmd), cmd)
        else:
            raise RuntimeError(f"MERLE_EARL_SOURCES: unknown source {name!r} "
                               "(known: amcrest, rover)")
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


def allowed_species_set(lat, lon, week):
    """The D4 mask: geo-model species for here-and-now, as a set of
    scientific names. Raises on any failure -- the caller decides how loud
    to be and runs unmasked (never dead)."""
    import birdnet
    geo = birdnet.load("geo", "2.4", "tf")
    df = geo.predict(lat, lon, week=week,
                     min_confidence=GEO_MIN_CONFIDENCE).to_dataframe()
    return {gate.split_label(label)[0] for label in df["species_name"]}


def load_yamnet(np, log):
    """The front gate, and the 2b seam (issue #174): returns
    classify(audio48k) -> [(class_name, score)] best-first, or None when the
    gate can't load -- Earl then runs UNGATED (Phase 1 behavior) and says so
    in every stats line, never silently. The Coral phase (2b) swaps this
    function's implementation for a TPU delegate; nothing upstream changes.

    CPU cost, measured on pearl: ~56-80 ms warm per 3 s window -- a few
    percent of the budget, and additive now rather than a saving, since
    every non-speech window still reaches BirdNET. Input is our native
    48 kHz mono; YAMNet wants 16 kHz, and a mean-of-3 decimation is a
    good-enough low-pass for classification (BirdNET always sees the
    full-rate audio)."""
    try:
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
                  clips_dir, queue, wind_mph):
    """Worker main (spawn target). Owns: the capture subprocess (restarted
    with backoff forever), the YAMNet gate, the BirdNET session, the geo
    mask, the clip ring. Reports to the parent over `queue` as
    ("state", name, state) and ("event", name, payload). Reads current wind
    from `wind_mph` (a shared Value; negative means unknown)."""
    import numpy as np

    import birdnet

    log = lambda msg: print(f"[{name}] {msg}", flush=True)
    log(f"worker up: {redacted}")

    model = birdnet.load("acoustic", "2.4", "tf")
    classify = load_yamnet(np, log)

    week = birdnet_week()
    try:
        allowed = allowed_species_set(*latlon, week)
        log(f"region mask: {len(allowed)} species for week {week}")
    except Exception as e:
        allowed = None
        log(f"REGION MASK UNAVAILABLE ({e}) -- running unmasked, D4 is off")

    backoff = 0
    visits = gate.VisitTracker()         # bird visits (#175)
    sound_visits = gate.VisitTracker()   # notable-sound visits (#174) --
    # separate tracker: AudioSet class names and species names are different
    # vocabularies and must not share a debounce namespace
    counters = {"windows": 0, "speech": 0, "notable": 0, "listen": 0,
                "ungated": 0, "gate_ms": 0.0}
    with model.predict_session(top_k=3, show_stats=None) as session:
        while True:
            if birdnet_week() != week:
                week = birdnet_week()
                try:
                    allowed = allowed_species_set(*latlon, week)
                    log(f"region mask refreshed: {len(allowed)} species, "
                        f"week {week}")
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
                for tracker in (visits, sound_visits):
                    for closed in tracker.expire(ts):
                        _, common = gate.split_label(closed["label"])
                        log(f"visit closed: {common}, {closed['windows']} "
                            f"windows, {closed['duration_s']:.0f}s, "
                            f"best {closed['best_conf']:.2f}")

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
                        action, relpath = sound_visits.observe(
                            klass, ts, score,
                            gate.clip_relpath(name, ts, klass))
                        if action == "extend":
                            continue
                        pending.append((relpath, list(context)))
                        if action == "open":
                            payload = gate.shape_sound_event(
                                source=name, ts=ts, klass=klass,
                                confidence=score, clip_relpath=relpath,
                                windy=windy, rms=rms)
                            queue.put(("event", name, payload))
                            log(f"sound: {klass} {score:.2f}"
                                f"{' (wind-suspect)' if windy else ''}")
                        else:
                            log(f"sound: {klass} {score:.2f} -- visit best, "
                                f"clip upgraded")

                # Every non-speech window reaches BirdNET -- the Phase 1
                # path, byte-identical from here (including its own human
                # check, the invariant's second layer).
                df = session.run_arrays((audio, gate.SAMPLE_RATE)).to_dataframe()
                predictions = list(zip(df["species_name"], df["confidence"]))
                accepted, windy = gate.decide(
                    predictions, threshold=threshold, wind_mph=wind,
                    allowed_species=allowed)
                if not accepted:
                    prev_window = buf
                    continue

                for label, conf in accepted:
                    action, relpath = visits.observe(
                        label, ts, conf, gate.clip_relpath(name, ts, label))
                    if action == "extend":
                        continue
                    pending.append((relpath, list(context)))
                    common = gate.split_label(label)[1]
                    if action == "open":
                        payload = gate.shape_event(
                            source=name, ts=ts, label=label, confidence=conf,
                            clip_relpath=relpath, windy=windy, rms=rms)
                        queue.put(("event", name, payload))
                        log(f"{common} {conf:.2f}"
                            f"{' (wind-suspect)' if windy else ''}")
                    else:   # "best": rewrite the visit's clip in place
                        log(f"{common} {conf:.2f} -- visit best, "
                            f"clip upgraded")
                prev_window = buf

            # EOF: finish clips with the context in hand (a 6s clip beats a
            # dangling event pointer). Visits deliberately survive the drop.
            for relpath, chunks in pending:
                try:
                    write_clip(clips_dir, relpath, chunks)
                except OSError as e:
                    log(f"clip write failed ({relpath}): {e}")
            pending = []

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

    def __init__(self, commands, latlon, threshold, gate_floor, clips_dir):
        self._ctx = multiprocessing.get_context("spawn")
        self._queue = self._ctx.Queue()
        self._wind = self._ctx.Value("d", -1.0)   # <0 = unknown
        self._wind_ts = 0.0
        self._commands = commands
        self._latlon = latlon
        self._threshold = threshold
        self._gate_floor = gate_floor
        self._clips_dir = clips_dir
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
                  self._wind),
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
        print(f"[earl] listening: {', '.join(self._commands)} "
              f"(threshold {self._threshold}, lat/lon {self._latlon[0]},"
              f"{self._latlon[1]})", flush=True)

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
    commands = source_commands()
    bus.broker_address()   # fail now, not after the model loads
    Earl(commands, latlon, threshold, gate_floor, clips_dir).run()


if __name__ == "__main__":
    main()
