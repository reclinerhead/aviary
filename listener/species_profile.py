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
#   1. extracts|pageimages|pageprops  -> article text, thumbnail URL, name
#   2. imageinfo extmetadata          -> license + author (CC-BY requires
#      attribution, and honesty is house style, so it's stored not implied)
# BirdNET's scientific names resolve via redirects=1 -- bird articles live
# at common-name titles with taxonomic redirects pointing in.
#
# WHAT IT SOUNDS LIKE (issue #315): call 1 no longer sends exintro=1, so it
# returns the WHOLE article rather than just the lead, and `voice` is the
# article's vocalization section -- the prose a person actually needs when
# they are staring at a 9-second clip deciding whether BirdNET was right.
# The lead is plumage and range; it never answers that question.
#
# Costing nothing is what makes it worth doing: clean_extract() already cuts
# at the first section heading, so the SAME call and the SAME function still
# yield byte-identical `description` prose -- measured, and asserted by a
# test. Still two calls per species. The payload grows ~1.7 KB -> ~15 KB,
# which against a life list this size behind a ~daily gate is nothing.
#
# ONLY A REAL HEADING COUNTS. Measured on 30 yard species: 22 have a section
# whose heading names a sound (Vocalization/Vocalizations/Vocals/Voice/Song/
# Songs/Song and calls/Call and flight), at top level and nested alike.
# Scanning loose paragraphs for the other 8 was tried and REJECTED: it hands
# back the Eastern Bluebird's "In culture" paragraph about "Over the Rainbow"
# and the cowbird's brood-parasitism neurology. On a panel whose whole job is
# helping someone judge a false positive, confidently wrong prose is worse
# than an honest blank -- so no section means NULL, same as no page, no image.
#
# `voice_checked_ts` is stamped whether or not a section was found, and that
# is load-bearing rather than bookkeeping: the backfill arm keys on it being
# NULL, so without the stamp the ~27% with no section would sit on the
# worklist and be re-fetched forever. It is species_analysis's checked_ts
# vs generated_ts split, for exactly the same reason.
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

# How long the ENRICHMENT LOOP (issue #217) waits before giving a
# no-page/failed species another look. The worklist deliberately keeps such
# a species listed forever ("re-runs get another look for free") -- right
# for a hand-run pass, but under a 15-minute loop that would be ~100
# Wikipedia queries a day for a bird with no article. Attempts live in
# their own table rather than a stub profile row, because "no profile row"
# is the semantic both the worklist and the roster's LEFT JOIN rely on.
# The CLI never consults this -- a hand run still gets its look for free.
PROFILE_RETRY_S = 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS species_profile (
    species_sci       TEXT PRIMARY KEY,
    description       TEXT,
    image_file        TEXT,
    image_source      TEXT,
    image_attribution TEXT,
    image_w           INTEGER,
    image_h           INTEGER,
    voice             TEXT,
    voice_checked_ts  INTEGER,
    fetched_ts        INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_attempts (
    species_sci     TEXT PRIMARY KEY,
    last_attempt_ts INTEGER NOT NULL
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

# An explaintext heading line, with its own level captured: "== Song ==" is
# level 2 (a top-level section), "=== Duetting ===" level 3.
_HEADING = re.compile(r"^(=+)\s*(.+?)\s*=+\s*$", re.M)

# A heading that names a sound (issue #315). WORD-BOUNDED, and that is not
# fussiness: an unanchored "cri" matches inside "Description", which is
# exactly how the first cut of this silently claimed a hit on every French
# article it saw. Covers every spelling observed across 30 yard species --
# note bare "Vocals" (the Common Grackle's, nested under Ecology and
# behavior), which a vocali[sz]ation-only pattern misses.
_VOICE_HEADING = re.compile(
    r"\b(?:vocali[sz]ations?|vocals?|voice|songs?|calls?|sounds?"
    r"|drumming|singing)\b", re.I)


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
    # migration script): a #184-era file lacks the image dimensions, a
    # pre-#315 one lacks the voice columns. Add them in place; fresh files
    # get them from SCHEMA. Both paths idempotent, so a fresh pearl and a
    # week-old earl.db take the same code. Existing rows are honestly NULL
    # until the matching backfill arm of the worklist refills them.
    columns = {r["name"] for r in
               conn.execute("PRAGMA table_info(species_profile)")}
    for col, decl in (("image_w", "INTEGER"), ("image_h", "INTEGER"),
                      ("voice", "TEXT"), ("voice_checked_ts", "INTEGER")):
        if col not in columns:
            conn.execute(f"ALTER TABLE species_profile ADD COLUMN {col} {decl}")
    conn.commit()
    return conn


# --- Pure shaping (test_listener_species_profile.py) -------------------------

def image_filename(sci):
    """'Cardinalis cardinalis' -> 'Cardinalis_cardinalis.jpg', or None for a
    name that scrubs to nothing. Must match the MCC route's mirror exactly."""
    safe = _SAFE_CHARS.sub("_", str(sci).strip()).strip("_")
    return f"{safe}.jpg" if safe else None


def summary_url(sci):
    """Call 1: the article text, the page image, and the disambiguation
    marker in one query. redirects=1 is what makes scientific-name titles
    land on the common-name articles; maxlag is fetch-job manners.

    NO exintro (#315): the lead alone can't say what a bird sounds like, and
    the full extract costs no extra call. `description` is unaffected --
    clean_extract() cuts at the first heading either way."""
    params = urllib.parse.urlencode({
        "action": "query", "format": "json", "formatversion": 2,
        "redirects": 1, "maxlag": WIKI_MAXLAG_S,
        "prop": "extracts|pageimages|pageprops",
        "explaintext": 1,
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
    """An article extract -> the LEAD paragraphs a page renders: entities
    unescaped, everything from the first section heading dropped, blank-line
    runs collapsed.

    Unchanged by #315 and deliberately so. It already cut at the first
    heading to survive a truncated extract, which means handing it the whole
    article instead of an exintro one yields the same bytes -- the reason
    dropping exintro is free rather than a migration."""
    if not text:
        return None
    text = _SECTION_STUB.sub("", html.unescape(text))
    text = _BLANK_RUNS.sub("\n\n", text).strip()
    return text or None


def clean_section(text):
    """A section body -> readable prose. Unlike clean_extract this must NOT
    cut at a heading (that would truncate a "Song and calls" section at its
    first subsection); sub-headings are dropped as scaffolding and their
    prose kept."""
    if not text:
        return None
    text = _HEADING.sub("", html.unescape(text))
    text = _BLANK_RUNS.sub("\n\n", text).strip()
    return text or None


def voice_section(text):
    """The first section whose heading names a sound -> its cleaned prose,
    or None when the article has no such section (the honest blank -- see
    the module header on why loose-paragraph scanning was rejected).

    A section runs until the next heading at the SAME OR HIGHER level, so a
    nested "Vocals" under "Ecology and behavior" yields its own paragraphs
    and not its parent's remaining siblings."""
    if not text:
        return None
    text = html.unescape(text)
    for m in _HEADING.finditer(text):
        if not _VOICE_HEADING.search(m.group(2)):
            continue
        level = len(m.group(1))
        rest = text[m.end():]
        end = len(rest)
        for nxt in _HEADING.finditer(rest):
            if len(nxt.group(1)) <= level:
                end = nxt.start()
                break
        return clean_section(rest[:end])
    return None


def parse_summary(d):
    """The summary query -> {description, image_url, image_name}, all
    honestly None when the wiki has nothing usable. A missing page and a
    disambiguation page both count as nothing: 'Cardinalis' the genus page
    listing three species is not a portrait of anybody."""
    pages = ((d or {}).get("query") or {}).get("pages") or []
    page = pages[0] if pages else None
    empty = {"description": None, "voice": None, "image_url": None,
             "image_name": None, "image_w": None, "image_h": None}
    if not page or page.get("missing"):
        return empty
    if "disambiguation" in (page.get("pageprops") or {}):
        return empty
    thumb = page.get("thumbnail") or {}
    return {
        "description": clean_extract(page.get("extract")),
        # What it sounds like (#315), from the same response.
        "voice": voice_section(page.get("extract")),
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
    (3) BACKFILL (issue #315): rows never yet asked about a vocalization
        section, i.e. everything written before `voice` existed. Keyed on
        voice_checked_ts being NULL rather than on `voice` being NULL,
        because a bird whose article HAS no such section must settle: the
        stamp is written either way, so this arm empties permanently after
        one pass instead of re-asking Wikipedia forever about the ~27% that
        will never have an answer.

    Owner rows are never in any arm: `image_source = 'owner'` is excluded
    outright, so the pass cannot even spend a fetch on one."""
    return [(r["species_sci"], r["species_common"]) for r in conn.execute(
        "SELECT l.species_sci, l.species_common FROM life_list l"
        " LEFT JOIN species_profile p ON p.species_sci = l.species_sci"
        " WHERE (p.species_sci IS NULL"
        "        OR (p.image_source = 'wikipedia' AND p.image_file IS NOT NULL"
        "            AND p.image_w IS NULL)"
        "        OR (p.voice_checked_ts IS NULL"
        "            AND COALESCE(p.image_source, '') <> 'owner'))"
        " ORDER BY l.species_common")]


def all_species(conn):
    """Every species on the life list, common-name order -- what --refresh-all
    walks. Deliberately NOT the worklist: this is the "re-fetch the lot" path
    (a prompt-version bump on our side, a Wikipedia article that has since
    grown a Vocalizations section), so it must ignore every gate. Owner rows
    are still refused, one layer down in enrich_species()."""
    return [(r["species_sci"], r["species_common"]) for r in conn.execute(
        "SELECT species_sci, species_common FROM life_list"
        " ORDER BY species_common")]


def owner_locked(conn, sci):
    """The provenance rule: an owner-sourced row is Todd's, not the pass's."""
    row = conn.execute(
        "SELECT image_source FROM species_profile WHERE species_sci = ?",
        (sci,)).fetchone()
    return row is not None and row["image_source"] == "owner"


# --- The loop's retry gate (issue #217) ---------------------------------------
#
# These three exist for listener/enrichment_loop.py, which wraps worklist()
# with them; worklist() itself is deliberately untouched, and so is the CLI.

def attempt_due(last_ts, now, retry_s=PROFILE_RETRY_S):
    """Whether a species has earned another Wikipedia look. Never-attempted
    (a brand-new lifer) is due immediately -- the whole point of the loop."""
    return last_ts is None or now - last_ts >= retry_s


def attempts_map(conn):
    """species_sci -> last attempt epoch, for filtering a whole worklist in
    one query instead of one per species."""
    return {r["species_sci"]: r["last_attempt_ts"] for r in conn.execute(
        "SELECT species_sci, last_attempt_ts FROM profile_attempts")}


def record_attempt(conn, sci, now):
    """Stamp one attempt, success or failure alike -- a species that gained
    a row leaves the worklist anyway, so the stamp only ever delays retries
    of species Wikipedia had nothing (or an error) for."""
    conn.execute(
        "INSERT OR REPLACE INTO profile_attempts (species_sci,"
        " last_attempt_ts) VALUES (?,?)", (sci, int(now)))
    conn.commit()


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

    stamp = int(now())
    conn.execute(
        "INSERT OR REPLACE INTO species_profile (species_sci, description,"
        " image_file, image_source, image_attribution, image_w, image_h,"
        " voice, voice_checked_ts, fetched_ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sci, summary["description"], image_file, image_source, credit,
         # Dimensions belong to the file we actually wrote: no file, no
         # claim about its shape (the honest-NULL rule).
         summary["image_w"] if image_file else None,
         summary["image_h"] if image_file else None,
         summary["voice"],
         # Stamped even when voice is None -- "we looked and there was
         # nothing" is what keeps this species off the backfill arm (#315).
         stamp,
         stamp))
    conn.commit()
    return "enriched" if image_file else "no-image"


# --- The bulk CLI (a thin loop, per the reusable-pass rule) ------------------

def main():
    ap = argparse.ArgumentParser(
        description="Fill species_profile from Wikipedia (issues #184, #315)")
    one = ap.add_mutually_exclusive_group()
    one.add_argument("--refresh", metavar="SPECIES_SCI",
                     help="re-fetch one species (owner rows still skipped)")
    one.add_argument("--refresh-all", action="store_true",
                     help="re-fetch EVERY life-list species, ignoring the "
                          "worklist gates (owner rows still skipped)")
    args = ap.parse_args()

    path = db_path()
    media = clips_dir()
    conn = connect(path)
    try:
        if args.refresh:
            todo = [(args.refresh, args.refresh)]
        elif args.refresh_all:
            todo = all_species(conn)
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
            # Read back rather than widen enrich_species's return: the loop
            # service unpacks a bare status word from it, and a run of the
            # CLI is not worth changing a contract two callers share.
            if status in ("no-image", "enriched"):
                row = conn.execute(
                    "SELECT voice FROM species_profile WHERE species_sci = ?",
                    (sci,)).fetchone()
                got = bool(row and row["voice"])
                label += " · voice" if got else " · no voice section"
                counts["voice"] = counts.get("voice", 0) + (1 if got else 0)
            print(f"[species] {common}: {label}", flush=True)
        print(f"[species] done: " + ", ".join(
            f"{v} {k}" for k, v in sorted(counts.items())), flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
