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
# Config (env):
#   MERLE_MQTT       the broker, REQUIRED (bus.py raises without it)
#   MERLE_EARL_DB    the store's path (default "earl.db" under the unit's
#                    WorkingDirectory -- the MERLE_WEATHER_DB convention; any
#                    future MCC route gets an absolute path to the SAME file)
# =============================================================================

import json
import os
import sqlite3
import time

import paho.mqtt.client as mqtt

import bus

CLIENT_ID = "earl-sightings"
DEFAULT_DB_PATH = "earl.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sightings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             INTEGER NOT NULL,
    source         TEXT NOT NULL,
    species_sci    TEXT NOT NULL,
    species_common TEXT NOT NULL,
    confidence     REAL NOT NULL,
    clip           TEXT,
    wind_suspect   INTEGER NOT NULL DEFAULT 0
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
        return {
            "ts": int(event["ts"]),
            "source": str(event["source"]),
            "species_sci": str(event["species_sci"]),
            "species_common": str(event["species_common"]),
            "confidence": float(event["confidence"]),
            "clip": event.get("clip"),
            "wind_suspect": 1 if event.get("wind_suspect") else 0,
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
        " confidence, clip, wind_suspect) VALUES (?,?,?,?,?,?,?)",
        (row["ts"], row["source"], row["species_sci"], row["species_common"],
         row["confidence"], row["clip"], row["wind_suspect"]))
    cur = conn.execute(
        "INSERT OR IGNORE INTO life_list (species_sci, species_common,"
        " first_ts, first_source, first_clip) VALUES (?,?,?,?,?)",
        (row["species_sci"], row["species_common"], row["ts"], row["source"],
         row["clip"]))
    conn.commit()
    return cur.rowcount == 1


def main():
    path = db_path()
    conn = connect(path)   # an unopenable path fails here, at launch
    print(f"[sightings] recording to {path}", flush=True)

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

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=CLIENT_ID)
    client.on_connect = on_connect
    client.on_message = on_message
    host, port = bus.broker_address()
    client.connect_async(host, port)
    try:
        client.loop_forever(retry_first_connection=True)
    except KeyboardInterrupt:
        print("[sightings] signing off", flush=True)
        client.disconnect()


if __name__ == "__main__":
    main()
