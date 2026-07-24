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
#   MERLE_LATLON               "lat,lon" -- used ONLY to seed location 1 (Home)
#                              at first upgrade (issue #232). Unlike earl.py,
#                              sightings does NOT require it: a consumer that
#                              never does geo work has no business dying over a
#                              missing locator, so an absent/invalid value logs
#                              a warning and seeds Home with placeholder coords
#                              (self-healed the moment a real value arrives).
#                              Set it on the earl-sightings unit -- Servers/
#                              Pearl.md -- so Home's coords are right from day
#                              one; the Phase 3 import pass reads them.
# =============================================================================

import json
import os
import sqlite3
import time

import paho.mqtt.client as mqtt

import bus
from listener import gate   # pure (re only) -- safe on the lean venv, and the
                            # boundary test poisons numpy against exactly this

CLIENT_ID = "earl-sightings"
DEFAULT_DB_PATH = "earl.db"
DEFAULT_KEEP_DAYS = 90.0
PRUNE_INTERVAL_S = 3600
HOME_LOCATION_ID = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    lat            REAL NOT NULL,
    lon            REAL NOT NULL,
    created_ts     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sightings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             INTEGER NOT NULL,
    source         TEXT NOT NULL,
    species_sci    TEXT NOT NULL,
    species_common TEXT NOT NULL,
    confidence     REAL NOT NULL,
    clip           TEXT,
    wind_suspect   INTEGER NOT NULL DEFAULT 0,
    rms            REAL,
    location_id    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS sightings_ts ON sightings(ts);
CREATE INDEX IF NOT EXISTS sightings_species ON sightings(species_sci, ts);

-- life_list is keyed on (species_sci, location_id): first-heard is a
-- per-PLACE fact (issue #232), so a robin at the park is a park lifer even
-- though Home has heard hundreds. The sightings_location index is created in
-- _upgrade(), not here -- an upgraded pre-#232 file adds the column by ALTER
-- *after* this script runs, so indexing it here would fail on that path.
CREATE TABLE IF NOT EXISTS life_list (
    species_sci    TEXT NOT NULL,
    species_common TEXT NOT NULL,
    first_ts       INTEGER NOT NULL,
    first_source   TEXT NOT NULL,
    first_clip     TEXT,
    location_id    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (species_sci, location_id)
);
"""


def db_path():
    return os.environ.get("MERLE_EARL_DB", "").strip() or DEFAULT_DB_PATH


def connect(path, home_latlon=None):
    """weather_archive.connect(), same reasons: WAL so a future reader never
    sees "database is locked", idempotent schema so a fresh pearl and an old
    file take the same path. ":memory:" for tests.

    `home_latlon` (a (lat, lon) tuple, or None) seeds location 1's coords the
    first time this runs against a store -- main() passes MERLE_LATLON; tests
    and the prune connection pass nothing and get a placeholder, harmless
    because Home's coords are read only by the Phase 3 import pass, never by
    live Earl."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _upgrade(conn, home_latlon)
    conn.commit()
    return conn


def _upgrade(conn, home_latlon):
    """Bring a pre-existing store up to the current shape in place -- the
    repeatable-pass rule (an upgrade is a restart, never a one-time migration
    script). Every step is idempotent, so a fresh pearl and a day-one earl.db
    land on byte-identical schema. Called after SCHEMA, which has already
    created anything missing for a fresh file."""
    scols = {r["name"] for r in conn.execute("PRAGMA table_info(sightings)")}
    # Issue #175: a pre-#175 file lacks the rms column.
    if "rms" not in scols:
        conn.execute("ALTER TABLE sightings ADD COLUMN rms REAL")
    # Issue #232: a pre-Field-Mode file lacks location_id -- every existing
    # sighting is Home, so the column default backfills the whole table for
    # free (no UPDATE, no row rewrite).
    if "location_id" not in scols:
        conn.execute("ALTER TABLE sightings ADD COLUMN location_id "
                     "INTEGER NOT NULL DEFAULT 1")
    conn.execute("CREATE INDEX IF NOT EXISTS sightings_location "
                 "ON sightings(location_id, ts)")

    # Issue #232: re-key life_list to (species_sci, location_id). An old file's
    # life_list has species_sci as the sole PRIMARY KEY and no location_id, and
    # SQLite can't ALTER a primary key -- so rebuild the table and copy every
    # existing row in as Home, first_ts/source/clip untouched. A fresh file
    # already carries the composite-key table from SCHEMA, so the missing
    # location_id column is exactly the "this is an old file" signal.
    lcols = {r["name"] for r in conn.execute("PRAGMA table_info(life_list)")}
    if "location_id" not in lcols:
        conn.executescript("""
            CREATE TABLE life_list_new (
                species_sci    TEXT NOT NULL,
                species_common TEXT NOT NULL,
                first_ts       INTEGER NOT NULL,
                first_source   TEXT NOT NULL,
                first_clip     TEXT,
                location_id    INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (species_sci, location_id)
            );
            INSERT INTO life_list_new
                (species_sci, species_common, first_ts, first_source,
                 first_clip, location_id)
                SELECT species_sci, species_common, first_ts, first_source,
                       first_clip, 1 FROM life_list;
            DROP TABLE life_list;
            ALTER TABLE life_list_new RENAME TO life_list;
        """)

    _ensure_home(conn, home_latlon)


def _ensure_home(conn, home_latlon):
    """Location 1 is Home. INSERT OR IGNORE makes the row part of the shape
    every store lands on (fresh or upgraded); the coords come from
    MERLE_LATLON when main() supplies them. When it doesn't (tests, the prune
    connection, or a unit whose env isn't set yet) Home gets a (0,0)
    placeholder -- and the UPDATE self-heals that placeholder the first time
    real coords do arrive, so deploy ordering (set the env before or after the
    first upgrade) doesn't matter."""
    lat, lon = home_latlon if home_latlon else (0.0, 0.0)
    conn.execute(
        "INSERT OR IGNORE INTO locations (id, name, lat, lon, created_ts)"
        " VALUES (?, 'Home', ?, ?, ?)",
        (HOME_LOCATION_ID, lat, lon, int(time.time())))
    if home_latlon:
        conn.execute(
            "UPDATE locations SET lat=?, lon=? "
            "WHERE id=? AND lat=0.0 AND lon=0.0",
            (lat, lon, HOME_LOCATION_ID))


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
        loc = event.get("location_id")
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
            # Issue #232: optional on the wire. Live Earl never sets it, so its
            # detections land as Home; the Phase 3 import pass is the first
            # publisher that stamps a park's id.
            "location_id": int(loc) if loc is not None else HOME_LOCATION_ID,
        }
    except (ValueError, TypeError, KeyError):
        return None


def record(conn, row):
    """Write one sighting; returns True when this species is NEW to the life
    list AT THIS LOCATION (the caller's log line -- a lifer deserves ink).
    INSERT OR IGNORE on the (species_sci, location_id) key makes first-heard
    the store's guarantee: replays and restarts can never move a first_ts, and
    a species heard at Home is still a fresh lifer the first time the park
    hears it (issue #232)."""
    location_id = row.get("location_id", HOME_LOCATION_ID)
    conn.execute(
        "INSERT INTO sightings (ts, source, species_sci, species_common,"
        " confidence, clip, wind_suspect, rms, location_id)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (row["ts"], row["source"], row["species_sci"], row["species_common"],
         row["confidence"], row["clip"], row["wind_suspect"],
         row.get("rms"), location_id))
    cur = conn.execute(
        "INSERT OR IGNORE INTO life_list (species_sci, species_common,"
        " first_ts, first_source, first_clip, location_id)"
        " VALUES (?,?,?,?,?,?)",
        (row["species_sci"], row["species_common"], row["ts"], row["source"],
         row["clip"], location_id))
    conn.commit()
    return cur.rowcount == 1


# --- clip retention (issue #175) ---------------------------------------------

# Mirror of clip_enhance.ENH_SUFFIX (issue #190). Deliberately duplicated
# rather than imported: clip_enhance needs numpy, and this consumer runs from
# the LEAN repo venv (paho only) -- test_import_boundary.py enforces exactly
# that, and would fail the moment this file reached for the pass. Two lines of
# string handling is a far better trade than dragging numpy onto a unit that
# never does arithmetic.
ENH_SUFFIX = "-enh.wav"


def prune_selection(files, now_ts, keep_days, exempt):
    """Which clips to delete: older than the horizon AND not a lifer's first
    recording. `files` is [(relpath, mtime_ts)]; pure with an injected clock
    -- the frame_archiver.prune_selection precedent, plus the exemption.
    The species/ shelf (issue #184's portraits) shares the clips dir but is
    a permanent collection, not a rolling window -- never selected, whatever
    its age.

    ENHANCED SIBLINGS (issue #190) share their original's fate exactly: a
    doomed clip takes its <stem>-enh.wav with it, and a lifer's exempt first
    clip keeps its sibling too. Judging a sibling on its OWN age would be
    subtly wrong in both directions -- it is always newer than its original
    (written by a later pass run), so it would outlive the evidence it
    enhances, and an exempt lifer's sibling would eventually age out from
    under a clip that is supposed to be permanent. A sibling whose original
    is already gone is an orphan -- nothing to inherit from, so it ages out
    on its own."""
    horizon = now_ts - keep_days * 86400
    present = {relpath for relpath, _ in files}
    doomed = []
    for relpath, mtime in files:
        if relpath.startswith("species/"):
            continue
        if relpath.endswith(ENH_SUFFIX):
            original = relpath[:-len(ENH_SUFFIX)] + ".wav"
            if original in present:
                continue          # goes when its original goes, below
            if mtime < horizon:   # orphan: its original is already gone
                doomed.append(relpath)
            continue
        if mtime < horizon and relpath not in exempt:
            doomed.append(relpath)
            sibling = relpath[:-len(".wav")] + ENH_SUFFIX
            if relpath.endswith(".wav") and sibling in present:
                doomed.append(sibling)
    return doomed


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
    """The sacred set: life_list.first_clip paths survive forever. With the
    #232 composite key this is automatically PER-LOCATION -- one row per
    (species, location) means a park lifer's first clip and Home's first clip
    for the same species are both in the set, so the 90-day sweep spares the
    first recording of each species at each place, no extra logic needed."""
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


def home_latlon_from_env():
    """MERLE_LATLON -> (lat, lon) for seeding Home, or None. Best-effort by
    design (issue #232): sightings is a bus consumer that does no geo work, so
    a missing or malformed locator warns and moves on -- it must never keep the
    bird record from starting the way it (rightly) stops earl.py."""
    raw = os.environ.get("MERLE_LATLON", "").strip()
    if not raw:
        print("[sightings] MERLE_LATLON not set -- location 1 (Home) seeded "
              "with placeholder coords; set it on this unit (Servers/Pearl.md) "
              "so the import pass can mask Home correctly", flush=True)
        return None
    try:
        return gate.parse_latlon(raw)
    except RuntimeError as e:
        print(f"[sightings] MERLE_LATLON invalid ({e}) -- Home seeded with "
              "placeholder coords", flush=True)
        return None


def main():
    path = db_path()
    # An unopenable path fails here, at launch.
    conn = connect(path, home_latlon=home_latlon_from_env())
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
