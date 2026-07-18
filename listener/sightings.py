# =============================================================================
# project-squirrel -- listener/sightings.py
#
# The bird record (issue #172): a tiny pearl-resident consumer -- the
# frame_archiver.py mold -- that subscribes to audio/events and writes every
# detection to a SQLite store that never prunes. Earl stays domain-agnostic
# (he reports what he heard); THIS is where "what he heard" becomes a bird
# record: the sightings table, and the life list -- every species ever heard,
# with its first-heard moment, the number the whole birdwatcher epic exists
# to produce.
#
#   python -m listener.sightings     (earl venv NOT required -- this consumer
#                                     needs only paho; it runs fine from the
#                                     repo venv like frame_archiver)
#
# Why a separate process and not code inside earl.py: epic #133 principle 2
# (one event stream, many consumers -- the birdwatcher interprets bird
# events, a future security consumer interprets scary ones, neither lives
# inside Earl), and the frame_archiver precedent: the writer that owns the
# durable record should be small, boring, and separately restartable. The
# bus is live transport; a sighting missed while this unit restarts is a
# moment nobody archived -- and unlike frames, the clip file Earl already
# wrote is still on disk, so a future backfill pass over clips/ can heal
# the record (the enrichment-pass ethos: worklist-driven, idempotent).
#
# Schema decisions (the weather_archive.py reasoning, applied to birds):
#   - On pearl, beside the writer. Epoch-seconds timestamps -- the audio
#     namespace is epoch end to end, converting at boundaries is two lies.
#   - `sightings` is append-only, one row per audio/events message, id
#     AUTOINCREMENT (two detections can share a timestamp -- two species in
#     one window -- so ts can't be the key the way weather's can).
#   - `life_list` is one row per species, INSERT OR IGNORE on every sighting:
#     the first insert wins and later ones are no-ops, so first-heard is
#     enforced by the store, not by application logic that could race or
#     drift (the ts-PRIMARY-KEY dedupe reasoning).
#   - This file is the second irreplaceable store the stack owns (the weather
#     archive's honor): a deleted first-heard date is a moment no one sells
#     back. Servers/Pearl.md says so where it can be acted on.
#
# Clip retention (issue #175) also lives HERE, not in earl.py -- deliberate:
# the store is the thing that knows which clips are irreplaceable, so the
# store does the pruning. Hourly, the frame_archiver pattern (pure selection,
# injected clock), with one sacred exemption: every clip named in
# life_list.first_clip survives FOREVER -- a lifer's first recording is part
# of the permanent record. Day one measured ~0.70 GB/day pre-debounce (the
# LV full in ~62 days); with the visit debounce and a 90-day horizon the
# steady state is ~20 GB, comfortable beside the media-cache's other tenants.
# Pruned rows keep their clip path -- append-only store, and a missing file
# is an honest gap (the Field Journal's pruned-thumbnail precedent).
#
# Config (env):
#   MERLE_MQTT                 the broker, REQUIRED (bus.py raises without it)
#   MERLE_EARL_DB              the store's path (default "earl.db" under the
#                              unit's WorkingDirectory -- the MERLE_WEATHER_DB
#                              convention; any future MCC route gets an
#                              absolute path to the SAME file)
#   MERLE_EARL_CLIPS           Earl's clip dir (same value as the earl unit's;
#                              default "clips"). Missing dir = nothing pruned.
#   MERLE_EARL_CLIPS_KEEP_DAYS retention horizon (default 90)
# =============================================================================

import json
import os
import sqlite3
import time

import paho.mqtt.client as mqtt

import bus

CLIENT_ID = "earl-sightings"
DEFAULT_DB_PATH = "earl.db"
DEFAULT_KEEP_DAYS = 90.0
PRUNE_INTERVAL_S = 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             INTEGER NOT NULL,
    source         TEXT NOT NULL,
    species_sci    TEXT NOT NULL,
    species_common TEXT NOT NULL,
    confidence     REAL NOT NULL,
    clip           TEXT,
    wind_suspect   INTEGER NOT NULL DEFAULT 0,
    rms            REAL
);
CREATE INDEX IF NOT EXISTS sightings_ts ON sightings(ts);
CREATE INDEX IF NOT EXISTS sightings_species ON sightings(species_sci, ts);

CREATE TABLE IF NOT EXISTS life_list (
    species_sci    TEXT PRIMARY KEY,
    species_common TEXT NOT NULL,
    first_ts       INTEGER NOT NULL,
    first_source   TEXT NOT NULL,
    first_clip     TEXT
);
"""


def db_path():
    return os.environ.get("MERLE_EARL_DB", "").strip() or DEFAULT_DB_PATH


def connect(path):
    """weather_archive.connect(), same reasons: WAL so a future reader never
    sees "database is locked", idempotent schema so a fresh pearl and an old
    file take the same path. ":memory:" for tests."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    # Issue #175 upgrade, the repeatable-pass rule (never a one-time
    # migration): a pre-#175 file lacks the rms column; add it in place.
    # Fresh files get it from SCHEMA; both paths are idempotent, so a fresh
    # pearl and a day-one earl.db take the same code with no script to run.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(sightings)")}
    if "rms" not in columns:
        conn.execute("ALTER TABLE sightings ADD COLUMN rms REAL")
    conn.commit()
    return conn


def parse_event(payload):
    """One audio/events message -> the row dict, or None for anything that
    isn't a well-formed detection (never trust the wire -- the frame_archiver
    rule; a malformed message is logged by the caller and dropped, never a
    dead consumer)."""
    try:
        event = json.loads(payload)
        if event.get("kind") != "detection":
            return None
        rms = event.get("rms")
        return {
            "ts": int(event["ts"]),
            "source": str(event["source"]),
            "species_sci": str(event["species_sci"]),
            "species_common": str(event["species_common"]),
            "confidence": float(event["confidence"]),
            "clip": event.get("clip"),
            "wind_suspect": 1 if event.get("wind_suspect") else 0,
            # Absent on pre-#175 events; NULL is the honest value then.
            "rms": float(rms) if rms is not None else None,
        }
    except (ValueError, TypeError, KeyError):
        return None


def record(conn, row):
    """Write one sighting; returns True when this species is NEW to the life
    list (the caller's log line -- a lifer deserves ink). INSERT OR IGNORE
    makes first-heard the store's guarantee: replays and restarts can never
    move a first_ts."""
    conn.execute(
        "INSERT INTO sightings (ts, source, species_sci, species_common,"
        " confidence, clip, wind_suspect, rms) VALUES (?,?,?,?,?,?,?,?)",
        (row["ts"], row["source"], row["species_sci"], row["species_common"],
         row["confidence"], row["clip"], row["wind_suspect"],
         row.get("rms")))
    cur = conn.execute(
        "INSERT OR IGNORE INTO life_list (species_sci, species_common,"
        " first_ts, first_source, first_clip) VALUES (?,?,?,?,?)",
        (row["species_sci"], row["species_common"], row["ts"], row["source"],
         row["clip"]))
    conn.commit()
    return cur.rowcount == 1


# --- clip retention (issue #175) ---------------------------------------------

def prune_selection(files, now_ts, keep_days, exempt):
    """Which clips to delete: older than the horizon AND not a lifer's first
    recording. `files` is [(relpath, mtime_ts)]; pure with an injected clock
    -- the frame_archiver.prune_selection precedent, plus the exemption.
    The species/ shelf (issue #184's portraits) shares the clips dir but is
    a permanent collection, not a rolling window -- never selected, whatever
    its age."""
    horizon = now_ts - keep_days * 86400
    return [relpath for relpath, mtime in files
            if mtime < horizon and relpath not in exempt
            and not relpath.startswith("species/")]


def list_clips(clips_dir):
    """Every file under the clips dir as (posix relpath, mtime). A missing
    dir is an empty list -- a box that never wrote a clip has nothing to
    prune, not an error."""
    out = []
    for root, _, names in os.walk(clips_dir):
        for f in names:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, clips_dir).replace(os.sep, "/")
            out.append((rel, os.path.getmtime(full)))
    return out


def exempt_clips(conn):
    """The sacred set: life_list.first_clip paths survive forever."""
    return {r["first_clip"] for r in
            conn.execute("SELECT first_clip FROM life_list")
            if r["first_clip"]}


def prune_clips(clips_dir, store_path, keep_days, now_ts=None):
    """One retention pass. Own short-lived read connection -- the paho
    callback thread owns the writer, and two threads on one sqlite handle
    is a fight not worth having. Returns how many files went."""
    conn = connect(store_path)
    try:
        exempt = exempt_clips(conn)
    finally:
        conn.close()
    doomed = prune_selection(list_clips(clips_dir),
                             now_ts if now_ts is not None else time.time(),
                             keep_days, exempt)
    for relpath in doomed:
        try:
            os.remove(os.path.join(clips_dir, relpath))
        except OSError:
            pass   # already gone or busy; next hour's pass will retry
    return len(doomed)


def main():
    path = db_path()
    conn = connect(path)   # an unopenable path fails here, at launch
    clips_dir = os.environ.get("MERLE_EARL_CLIPS", "").strip() or "clips"
    keep_days = float(os.environ.get("MERLE_EARL_CLIPS_KEEP_DAYS",
                                     DEFAULT_KEEP_DAYS))
    print(f"[sightings] recording to {path}; pruning {clips_dir} past "
          f"{keep_days:.0f} days (lifer first-clips exempt)", flush=True)

    def on_connect(client, userdata, flags, reason_code, properties):
        client.subscribe(bus.AUDIO_EVENTS_TOPIC)
        print(f"[sightings] subscribed to {bus.AUDIO_EVENTS_TOPIC}",
              flush=True)

    def on_message(client, userdata, message):
        row = parse_event(message.payload)
        if row is None:
            print(f"[sightings] dropped malformed message "
                  f"({message.payload[:80]!r})", flush=True)
            return
        try:
            is_lifer = record(conn, row)
        except sqlite3.Error as e:
            print(f"[sightings] write failed ({e}) -- sighting dropped",
                  flush=True)
            return
        if is_lifer:
            print(f"[sightings] LIFER: {row['species_common']} "
                  f"({row['species_sci']}) via {row['source']}", flush=True)

    # pid-suffixed: two instances sharing a client id kick each other off the
    # broker in a loop (a desk twin fought the production unit; the earl.py
    # note has the full story). QoS 0 + clean session = the id is cosmetic.
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"{CLIENT_ID}-{os.getpid()}")
    client.on_connect = on_connect
    client.on_message = on_message
    host, port = bus.broker_address()
    client.connect_async(host, port)
    # loop_start (not loop_forever, the pre-#175 shape): paho keeps the bus
    # on its own thread while the main thread owns the hourly retention tick.
    # First pass runs at startup so a long-down consumer heals immediately.
    client.loop_start()
    next_prune = time.time()
    try:
        while True:
            time.sleep(1)
            if time.time() < next_prune:
                continue
            next_prune = time.time() + PRUNE_INTERVAL_S
            try:
                pruned = prune_clips(clips_dir, path, keep_days)
                if pruned:
                    print(f"[sightings] pruned {pruned} clips past the "
                          f"{keep_days:.0f}-day horizon", flush=True)
            except Exception as e:
                print(f"[sightings] prune failed ({e}) -- retrying next "
                      "hour", flush=True)
    except KeyboardInterrupt:
        print("[sightings] signing off", flush=True)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
