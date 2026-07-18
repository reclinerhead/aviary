# =============================================================================
# project-squirrel -- listener/species_profile.py
#
# The species enrichment pass (epic #182 Phase 2, issue #184): real photos
# and descriptions for the Aviary, from Wikipedia, keyed by the scientific
# name BirdNET already gives us. The enrichment-pass ethos throughout:
# worklist-driven, idempotent, a per-species function with the bulk CLI as a
# thin loop over it (the metadata-refresh rule -- a future per-species
# "refresh" button calls exactly what --refresh calls today).
#
#   python -m listener.species_profile               # fill what's missing
#   python -m listener.species_profile --refresh "Cardinalis cardinalis"
#
# Runs on pearl from the REPO venv -- stdlib HTTP only, nothing heavy, so
# test_import_boundary.py stays true and the lean venv stays lean.
#
# THE SOURCE: the Action API (en.wikipedia.org/w/api.php), deliberately NOT
# the rest_v1/page/summary endpoint the issue sketched. music_bio.py already
# litigated this: rest_v1 entered gradual deprecation in July 2026, while
# prop=extracts is the long-stable path and takes maxlag (what a
# non-interactive job is supposed to send). And the summary endpoint never
# returned the image's license anyway -- attribution needs the imageinfo
# call regardless, so "one keyless call" was never on the table. Two calls
# per species, one more for the bytes:
#   1. extracts|pageimages|pageprops  -> lead prose, thumbnail URL, file name
#   2. imageinfo extmetadata          -> license + author (CC-BY requires
#      attribution, and honesty is house style, so it's stored not implied)
# BirdNET's scientific names resolve via redirects=1 -- bird articles live
# at common-name titles with taxonomic redirects pointing in.
#
# THE STORE: a `species_profile` table in earl.db, created idempotently here
# (the connect() upgrade idiom -- the pass owns its table; sightings.py's
# schema is untouched). Provenance ships from day one: `image_source` says
# where each portrait came from, and A PASS NEVER TOUCHES A ROW WHOSE
# image_source IS 'owner' -- Todd's own feeder-cam shot, when it exists,
# survives every re-run and every --refresh (the user-overridable-artist-
# image rule, one stack over). Images land under
# MERLE_EARL_CLIPS/species/<scrubbed_sci>.jpg -- the clips allowlist scrub;
# sightings.py's retention pass deliberately skips the species/ shelf
# (portraits are a permanent collection, not a rolling window).
#
# No page, no extract, no image: honest NULLs. A no-page species stays OFF
# the profile table and therefore ON the worklist -- a re-run gets another
# look for free (the music_art precedent). iNaturalist fallback is a noted
# follow-up, not built here.
#
# Config (env):
#   MERLE_EARL_DB     the store (default "earl.db" -- the sightings default)
#   MERLE_EARL_CLIPS  the media dir; portraits go in its species/ subdir
#                     (default "clips")
# =============================================================================

import argparse
import html
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = ("MerleEarl/1.0 "
              "( https://github.com/reclinerhead/project-squirrel )")
WIKI_MAXLAG_S = 5
FETCH_TIMEOUT_S = 20
THROTTLE_S = 1.0     # ~17 species today; a polite crawl, not a hammer
THUMB_PX = 900       # wide enough for the profile page, bounded on disk

DEFAULT_DB_PATH = "earl.db"
SPECIES_DIR = "species"

SCHEMA = """
CREATE TABLE IF NOT EXISTS species_profile (
    species_sci       TEXT PRIMARY KEY,
    description       TEXT,
    image_file        TEXT,
    image_source      TEXT,
    image_attribution TEXT,
    image_w           INTEGER,
    image_h           INTEGER,
    fetched_ts        INTEGER NOT NULL
);
"""

# gate.clip_relpath's scrub, verbatim: derived names are allowlisted before
# they touch a filesystem, and the MCC's portrait route re-derives the same
# filename from the URL's species name -- the two must agree byte-for-byte.
_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]+")

# The leftover section scaffolding a truncated extract can end on, and the
# blank-line runs the extractor leaves between paragraphs (music_bio's
# clean_wikipedia, trimmed to what bird leads actually need).
_SECTION_STUB = re.compile(r"\n==.*$", re.DOTALL)
_BLANK_RUNS = re.compile(r"\n{3,}")
_HTML_TAG = re.compile(r"<[^>]+>")


def db_path():
    return os.environ.get("MERLE_EARL_DB", "").strip() or DEFAULT_DB_PATH


def clips_dir():
    return os.environ.get("MERLE_EARL_CLIPS", "").strip() or "clips"


def connect(path):
    """sightings.connect()'s shape: WAL for peaceful readers, idempotent
    schema so a fresh pearl and an old file take the same path. The pass
    owns this table; opening the store never touches the sightings schema."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    # Issue #185's upgrade, the repeatable-pass rule (never a one-time
    # migration script): a #184-era file lacks the image dimensions. Add
    # them in place; fresh files get them from SCHEMA. Both paths idempotent,
    # so a fresh pearl and a week-old earl.db take the same code. Existing
    # rows honestly NULL until the backfill arm of the worklist refills them.
    columns = {r["name"] for r in
               conn.execute("PRAGMA table_info(species_profile)")}
    for col in ("image_w", "image_h"):
        if col not in columns:
            conn.execute(f"ALTER TABLE species_profile ADD COLUMN {col} INTEGER")
    conn.commit()
    return conn


# --- Pure shaping (test_listener_species_profile.py) -------------------------

def image_filename(sci):
    """'Cardinalis cardinalis' -> 'Cardinalis_cardinalis.jpg', or None for a
    name that scrubs to nothing. Must match the MCC route's mirror exactly."""
    safe = _SAFE_CHARS.sub("_", str(sci).strip()).strip("_")
    return f"{safe}.jpg" if safe else None


def summary_url(sci):
    """Call 1: the lead extract, the page image, and the disambiguation
    marker in one query. redirects=1 is what makes scientific-name titles
    land on the common-name articles; maxlag is fetch-job manners."""
    params = urllib.parse.urlencode({
        "action": "query", "format": "json", "formatversion": 2,
        "redirects": 1, "maxlag": WIKI_MAXLAG_S,
        "prop": "extracts|pageimages|pageprops",
        "exintro": 1, "explaintext": 1,
        "piprop": "thumbnail|name", "pithumbsize": THUMB_PX,
        "ppprop": "disambiguation",
        "titles": sci,
    })
    return f"{WIKIPEDIA_API}?{params}"


def imageinfo_url(image_name):
    """Call 2: the page image's license and author, from Commons metadata."""
    params = urllib.parse.urlencode({
        "action": "query", "format": "json", "formatversion": 2,
        "maxlag": WIKI_MAXLAG_S,
        "prop": "imageinfo", "iiprop": "extmetadata",
        "titles": f"File:{image_name}",
    })
    return f"{WIKIPEDIA_API}?{params}"


def clean_extract(text):
    """A lead extract -> the paragraphs a page renders: entities unescaped,
    trailing section scaffolding dropped, blank-line runs collapsed."""
    if not text:
        return None
    text = _SECTION_STUB.sub("", html.unescape(text))
    text = _BLANK_RUNS.sub("\n\n", text).strip()
    return text or None


def parse_summary(d):
    """The summary query -> {description, image_url, image_name}, all
    honestly None when the wiki has nothing usable. A missing page and a
    disambiguation page both count as nothing: 'Cardinalis' the genus page
    listing three species is not a portrait of anybody."""
    pages = ((d or {}).get("query") or {}).get("pages") or []
    page = pages[0] if pages else None
    empty = {"description": None, "image_url": None, "image_name": None,
             "image_w": None, "image_h": None}
    if not page or page.get("missing"):
        return empty
    if "disambiguation" in (page.get("pageprops") or {}):
        return empty
    thumb = page.get("thumbnail") or {}
    return {
        "description": clean_extract(page.get("extract")),
        "image_url": thumb.get("source"),
        "image_name": page.get("pageimage"),
        # The thumbnail's real dimensions (issue #185). Already in this
        # response and previously discarded, which is what left the GUI
        # center-cropping portrait-orientation birds into landscape boxes and
        # cutting off their heads. Stored so the browser can frame honestly.
        "image_w": thumb.get("width"),
        "image_h": thumb.get("height"),
    }


def parse_imageinfo(d):
    """The imageinfo query -> {license, artist}, None where Commons metadata
    is silent. extmetadata values are HTML (photographer credits love
    <a> tags); tags are stripped because the store holds prose, not markup."""
    pages = ((d or {}).get("query") or {}).get("pages") or []
    infos = (pages[0].get("imageinfo") or []) if pages else []
    meta = (infos[0].get("extmetadata") or {}) if infos else {}

    def field(key):
        value = (meta.get(key) or {}).get("value")
        if not isinstance(value, str):
            return None
        text = html.unescape(_HTML_TAG.sub("", value)).strip()
        return text or None

    return {"license": field("LicenseShortName"), "artist": field("Artist")}


def attribution(license_name, artist):
    """The credit line the profile page renders. CC-BY requires it; storing
    the finished string keeps the GUI dumb and the honesty durable."""
    parts = [p for p in (
        f"photo: {artist}" if artist else None,
        license_name,
        "via Wikipedia",
    ) if p]
    return " · ".join(parts) if len(parts) > 1 else None


def worklist(conn):
    """What this run should fetch, common-name order. Two arms:

    (1) Life-list species with NO profile row -- the original worklist. A
        no-page species writes no row and so stays here; re-runs get another
        look for free.
    (2) BACKFILL (issue #185): rows that have a fetched image but no
        dimensions, i.e. everything #184 wrote before the columns existed.
        One ordinary re-run heals the whole life list -- an upgrade is a
        re-run, never a migration script.

    Owner rows are never in either arm: `image_source = 'owner'` is excluded
    outright, so the pass cannot even spend a fetch on one."""
    return [(r["species_sci"], r["species_common"]) for r in conn.execute(
        "SELECT l.species_sci, l.species_common FROM life_list l"
        " LEFT JOIN species_profile p ON p.species_sci = l.species_sci"
        " WHERE (p.species_sci IS NULL"
        "        OR (p.image_source = 'wikipedia' AND p.image_file IS NOT NULL"
        "            AND p.image_w IS NULL))"
        " ORDER BY l.species_common")]


def owner_locked(conn, sci):
    """The provenance rule: an owner-sourced row is Todd's, not the pass's."""
    row = conn.execute(
        "SELECT image_source FROM species_profile WHERE species_sci = ?",
        (sci,)).fetchone()
    return row is not None and row["image_source"] == "owner"


# --- The wire ----------------------------------------------------------------

def get_json(url):
    """music_bio's fetch idiom, trimmed: identified client, one patient
    retry on 503 (maxlag's answer arrives as one)."""
    for attempt in (1, 2):
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "identity"})
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 503 and attempt == 1:
                print("[species] 503 (maxlag/rate) -- backing off", flush=True)
                time.sleep(3)
                continue
            raise
    return None


def get_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
        return r.read()


# --- The per-species function (what a future refresh button calls) -----------

def enrich_species(conn, media_dir, sci, *, fetch_json=get_json,
                   fetch_bytes=get_bytes, now=time.time):
    """Fetch one species' prose + portrait and write its profile row.
    Returns a status word for the caller's log line:
      'owner'    -- row is owner-sourced; NOTHING touched (the rule)
      'no-page'  -- Wikipedia has nothing usable; no row written, species
                    stays on the worklist
      'no-image' -- prose only; row written with honest image NULLs
      'enriched' -- prose + portrait + attribution
    Exceptions propagate: the bulk loop logs and moves on, a single-species
    --refresh should fail loudly.
    """
    if owner_locked(conn, sci):
        return "owner"

    summary = parse_summary(fetch_json(summary_url(sci)))
    if not summary["description"] and not summary["image_url"]:
        return "no-page"

    image_file = None
    image_source = None
    credit = None
    if summary["image_url"] and summary["image_name"]:
        info = parse_imageinfo(fetch_json(imageinfo_url(summary["image_name"])))
        filename = image_filename(sci)
        if filename:
            shelf = os.path.join(media_dir, SPECIES_DIR)
            os.makedirs(shelf, exist_ok=True)
            with open(os.path.join(shelf, filename), "wb") as f:
                f.write(fetch_bytes(summary["image_url"]))
            image_file = filename
            image_source = "wikipedia"
            credit = attribution(info["license"], info["artist"])

    conn.execute(
        "INSERT OR REPLACE INTO species_profile (species_sci, description,"
        " image_file, image_source, image_attribution, image_w, image_h,"
        " fetched_ts) VALUES (?,?,?,?,?,?,?,?)",
        (sci, summary["description"], image_file, image_source, credit,
         # Dimensions belong to the file we actually wrote: no file, no
         # claim about its shape (the honest-NULL rule).
         summary["image_w"] if image_file else None,
         summary["image_h"] if image_file else None,
         int(now())))
    conn.commit()
    return "enriched" if image_file else "no-image"


# --- The bulk CLI (a thin loop, per the reusable-pass rule) ------------------

def main():
    ap = argparse.ArgumentParser(
        description="Fill species_profile from Wikipedia (issue #184)")
    ap.add_argument("--refresh", metavar="SPECIES_SCI",
                    help="re-fetch one species (owner rows still skipped)")
    args = ap.parse_args()

    path = db_path()
    media = clips_dir()
    conn = connect(path)
    try:
        if args.refresh:
            todo = [(args.refresh, args.refresh)]
        else:
            todo = worklist(conn)
        if not todo:
            print("[species] nothing to do -- every lifer has a profile",
                  flush=True)
            return
        print(f"[species] {len(todo)} species to enrich -> {path}, images "
              f"under {os.path.join(media, SPECIES_DIR)}", flush=True)
        counts = {}
        for i, (sci, common) in enumerate(todo):
            if i:
                time.sleep(THROTTLE_S)
            try:
                status = enrich_species(conn, media, sci)
            except Exception as e:
                if args.refresh:
                    raise
                print(f"[species] {common}: FAILED ({e}) -- staying on the "
                      "worklist", flush=True)
                counts["failed"] = counts.get("failed", 0) + 1
                continue
            counts[status] = counts.get(status, 0) + 1
            label = {"owner": "owner photo -- untouched (the rule)",
                     "no-page": "no usable Wikipedia page",
                     "no-image": "prose, no portrait",
                     "enriched": "portrait + prose"}[status]
            print(f"[species] {common}: {label}", flush=True)
        print(f"[species] done: " + ", ".join(
            f"{v} {k}" for k, v in sorted(counts.items())), flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
