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
# `voice_checked_ts` is stamped whether or not a section was found. That was
# #315's termination guarantee -- without it the ~27% with no section would
# have sat on the worklist and been re-fetched forever, species_analysis's
# checked_ts vs generated_ts split for the same reason. Since #317 the
# backfill arm keys on `voice_version` instead (see the constant), because a
# bare "stamp is NULL" test can only ever fire once; this column survives
# purely as "when did we last look", for humans.
#
# A THIRD SOURCE (issue #318): Animal Diversity Web. After #315 and #317 a
# hard core of species remains where English Wikipedia says nothing about
# sound ANYWHERE in the article -- the Semipalmated Plover's entire article
# is 2,749 characters -- and no extraction change reaches them. ADW has a
# "Communication and Perception" section for six of the seven that defeated
# us, and it is onomatopoeic and comparative in exactly the way that made the
# Wikipedia sections worth having.
#
# ITS LICENCE IS THE WHOLE REASON THE DESIGN LOOKS LIKE THIS. ADW text is
# CC BY-NC-SA 3.0, which is NOT compatible with Wikipedia's CC BY-SA, so it
# gets its OWN column and its OWN stored attribution and is never
# concatenated with Wikipedia prose -- the image_source/image_attribution
# split, applied to prose. Wikipedia always wins: ADW is consulted only when
# both Wikipedia readers come back empty, so a species never loses good
# CC BY-SA prose to a non-commercial source. (The project is confirmed
# never-commercial, which is what makes NC acceptable at all; note that
# putting the text in earl.db is a one-way door.)
#
# There is NO API -- this is HTML parsing, and therefore the brittlest thing
# in the pass. It fails soft by construction: a layout change makes the
# section unfindable, which is an honest NULL and a log line, never an
# exception that kills a run.
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

# --- Animal Diversity Web, the third tier (issue #318) ------------------------

ADW_ACCOUNT = "https://animaldiversity.org/accounts/"

# The stored credit line. #184's rule: the attribution is STORED, not derived,
# because the GUI should stay dumb and the honesty should outlive whatever
# renders it. NEVER interpolate this into the Wikipedia credit -- two
# incompatible licences in one string is a licensing failure as well as a lie.
ADW_ATTRIBUTION = "via Animal Diversity Web · CC BY-NC-SA 3.0"

# animaldiversity.org/robots.txt: "User-agent: * / Crawl-delay: 8". Verified
# 2026-07-30, along with /accounts/ not being disallowed.
#
# DELIBERATELY NOT THROTTLE_S, and do not "harmonize" them later: that one is
# our own manners toward Wikipedia, this one is a number ADW publishes and we
# are obliged to honour. Eight seconds across a whole life list is ~5 minutes
# for a batch job behind a daily gate, which is nothing.
ADW_THROTTLE_S = 8.0

# The last ADW request, on the monotonic clock. Module state on purpose: the
# crawl delay is a promise to the SITE, so it has to hold across every caller
# -- the bulk CLI, the enrichment loop, and the future per-species refresh
# button alike -- not merely between iterations of one loop.
_adw_last_request = [None]

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
    voice_source      TEXT,
    voice_version     INTEGER,
    voice_checked_ts  INTEGER,
    voice_adw             TEXT,
    voice_adw_attribution TEXT,
    voice_adw_checked_ts  INTEGER,
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

# --- The sentence-level fallback (issue #317) ---------------------------------
#
# #315 shipped at 32/70 species. Checking ten of the failures showed the gap
# is two different problems: seven articles say nothing about sound anywhere
# (the Semipalmated Plover's whole article is 2,749 characters), but THREE
# carry real vocalization prose stranded in a Description or Similar species
# section, invisible to a heading-scoped reader:
#
#   Veery                  "a harsh, descending vee-er, which gave the bird
#                           its name"
#   Rose-breasted Grosbeak "The call is a sharp pink or pick"
#   Eastern Wood-Pewee     "The songs (fee-bee, fee-bee) and calls (chip) are
#                           quite different" -- a sound comparison against the
#                           very species it gets confused with
#
# THIS IS NOT THE PARAGRAPH SCAN #315 REJECTED, and the difference is the
# whole design. Density scored the Wood-Pewee's PLUMAGE paragraph at four
# "vocal" words and would have served it as a sound description. A sentence
# only survives here if it makes a CLAIM ABOUT SOUND -- which a sentence
# about wingbars cannot do however many times it says "similar".
#
# Measured before it was written: recall 3/3, precision 7/7 (silent on every
# article that genuinely says nothing), no-harm 3/3 (a real section still
# wins). A real section ALWAYS wins -- this runs only when voice_section()
# comes back empty, so good CC BY-SA prose is never traded for scraps.

# "the song is", "its call consists of", "their vocalizations include" -- a
# vocal noun as the subject of an assertion. Up to two words may sit between
# the article and the noun ("the most common call is").
_VOCAL_SUBJECT = re.compile(
    r"\b(?:the|its|their|his|her|a|an)\s+(?:\w+\s+){0,2}"
    r"(?:songs?|calls?|voice|vocali[sz]ations?|notes?|whistles?|trills?"
    r"|drumming|chatter)\b\s+"
    r"(?:is|are|was|were|consists?|consisted|includes?|included|has|have"
    r"|resembles?|sounds?|begins?|varies|can\s+be|may\s+be|is\s+a|are\s+a"
    r"|is\s+described|was\s+described)\b", re.I)

# The looser route: something audibly VERBING, but only when the sentence
# also carries a transcription. Either signal alone is far too loose.
_VOCAL_VERB = re.compile(
    r"\b(?:sings?|singing|calls?|calling|whistles?|trills?|drums?"
    r"|vocali[sz]es?|utters?|gives?\s+a\s+(?:harsh|sharp|soft|loud))\b", re.I)

# A written-down bird sound. "chick-a-dee", "fitz-bew", "cooOOoo", "vee-er".
# Loose on its own, which is exactly why it is never used on its own.
_ONOMATOPOEIA = re.compile(
    r"\b(?:[a-z]{2,}-){1,}[a-z]{2,}\b"
    r"|\b\w*(?:eee|ooo|urr|eer|ick|eep)\w*\b", re.I)

# The veto that makes this safe. Plumage vocabulary means the sentence is
# about what the bird LOOKS like, and no amount of "similar" or "differs"
# makes that a sound description. This single rule is what separates a
# sentence reader from the density scan that got rejected.
_PLUMAGE = re.compile(
    r"\b(?:plumage|wingbars?|feathers?|bill|breast|belly|underparts"
    r"|upperparts|tail\s+(?:is|are)|colou?r(?:ed|ing|ation)?|streaked"
    r"|spotted\s+with|iridescen\w+)\b", re.I)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'])")

# One qualifying sentence is a mention, not a description. Two is the floor
# that keeps this quiet on thin articles -- and thin articles are most of
# what it will be pointed at.
VOICE_MIN_SENTENCES = 2

# Bumping this is what puts every species back on the worklist for one more
# look (the PROMPT_VERSION idiom from species_analysis). Without it #317
# would only ever reach a species via a hand `--refresh-all`: after #315 ran,
# every row already has a voice_checked_ts, so nothing would call the new
# reader on a deployed box. 1 = #315, heading sections only. 2 = #317, the
# sentence fallback behind them. 3 = #318, Animal Diversity Web behind both.
#
# It is also what makes the ADW tier settle: adw_prior() only trusts a stored
# ADW look that was made at THIS generation, so one bump re-asks the site and
# no bump re-asks it never.
VOICE_VERSION = 3


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
                      ("voice", "TEXT"), ("voice_checked_ts", "INTEGER"),
                      ("voice_source", "TEXT"), ("voice_version", "INTEGER"),
                      # #318's tier. Its own columns because CC BY-NC-SA and
                      # CC BY-SA cannot share one, and its own stamp because
                      # a species with no ADW account has to settle.
                      ("voice_adw", "TEXT"),
                      ("voice_adw_attribution", "TEXT"),
                      ("voice_adw_checked_ts", "INTEGER")):
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


def voice_sentences(text, minimum=VOICE_MIN_SENTENCES):
    """Sentences anywhere in the article that make a claim about SOUND, in
    article order -- the #317 fallback for prose Wikipedia buries under a
    Description heading. None unless at least `minimum` sentences qualify.

    Never called while voice_section() has an answer; see read_voice()."""
    if not text:
        return None
    body = _HEADING.sub("\n", html.unescape(text))
    keep, seen = [], set()
    for para in body.split("\n"):
        para = para.strip()
        if not para:
            continue
        for s in _SENTENCE_SPLIT.split(para):
            s = s.strip()
            # Fragments carry no claim and 400+ characters is a paragraph
            # that lost its full stops, not a sentence.
            if not (40 <= len(s) <= 400) or s in seen:
                continue
            if _PLUMAGE.search(s):
                continue
            if (_VOCAL_SUBJECT.search(s)
                    or (_VOCAL_VERB.search(s) and _ONOMATOPOEIA.search(s))):
                seen.add(s)
                keep.append(s)
    return " ".join(keep) if len(keep) >= minimum else None


def read_voice(text):
    """The whole voice-reading policy in one place -> (prose, source).

    Precedence is absolute and deliberately not a comparison: a dedicated
    section always wins, even when the fallback would return more words.
    Measured on the Carolina Wren -- 2,308 characters of real section
    against 1,069 the sentence reader would assemble -- and the section is
    the better read every time, being the article's own account of the bird
    rather than lines gathered from wherever they fell.

    ('...', 'section') | ('...', 'sentences') | (None, None)"""
    section = voice_section(text)
    if section:
        return section, "section"
    gathered = voice_sentences(text)
    if gathered:
        return gathered, "sentences"
    return None, None


# --- Animal Diversity Web (issue #318) ---------------------------------------
#
# The markup, measured on real pages 2026-07-30 (blue grosbeak, solitary
# sandpiper): each topic is a <section> opening with an <h2> carrying a
# STABLE ID, and the prose is that section's <p> elements.
#
#     <section>
#      <h2 id="communication">Communication and Perception</h2>
#      <p audience="advanced">Only male blue grosbeaks are known to sing.
#         ... such as <a href="/accounts/Carpodacus_mexicanus/">house
#         finches</a> ... <refs><ref href="..."/></refs></p>
#      <ul class="keywords wide"><li>Communication Channels</li>...</ul>
#     </section>
#
# Anchoring on id="communication" rather than the heading TEXT is the whole
# robustness story: the id is what their own in-page navigation links to, and
# it survives the heading being reworded. Taking only <p> is what drops the
# chrome -- the keyword <ul>s ("visual", "acoustic") are a taxonomy widget,
# not prose, and would read as gibberish under a sound description.
#
# <a> text is KEPT (flattened): "lacks burry qualities common in related
# species, such as house finches and indigo buntings" is precisely the
# comparative-against-confusables sentence this tier exists for, and those
# species names are links.
_ADW_COMMUNICATION = re.compile(r"<h2[^>]*\bid=[\"']communication[\"'][^>]*>",
                                re.I)
# Where the section's prose stops: the next section, or the next heading if
# the page is ever flattened.
_ADW_BLOCK_END = re.compile(r"</section\b|<h2\b", re.I)
_ADW_PARAGRAPH = re.compile(r"<p\b[^>]*>(.*?)</p\s*>", re.I | re.S)
# Their citation markers. Dropped whole -- they carry no text, and left in
# they would still contribute their own whitespace.
_ADW_REFS = re.compile(r"<refs\b.*?</refs\s*>", re.I | re.S)
# The pretty-printed markup puts every tag on its own line, so flattening an
# inline <a> leaves "indigo buntings ." and "sandpipers , a similar species".
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.,;:!?])")
_WHITESPACE = re.compile(r"\s+")


def adw_url(sci):
    """'Passerina caerulea' -> the ADW account page, or None for a name that
    scrubs to nothing. The same allowlist scrub the portrait filename uses,
    so a hostile species name can never build a URL that walks anywhere."""
    safe = _SAFE_CHARS.sub("_", str(sci).strip()).strip("_")
    return f"{ADW_ACCOUNT}{safe}/" if safe else None


def adw_section(page):
    """An ADW account page -> its Communication and Perception prose, or None.

    None covers three real cases and deliberately does not distinguish them,
    because the caller does the same thing for all three: the species has no
    ADW account, the account has no such section (measured: the tufted
    titmouse), or their markup has changed under us.

    THE 404 TRAP: a missing account answers **HTTP 200** with a "Page not
    found" body, so the status code proves nothing and the absence of the
    section is the only honest signal. Measured on Baeolophus_bicolor."""
    if not page:
        return None
    m = _ADW_COMMUNICATION.search(page)
    if not m:
        return None
    rest = page[m.end():]
    stop = _ADW_BLOCK_END.search(rest)
    block = rest[:stop.start()] if stop else rest
    paragraphs = []
    for raw in _ADW_PARAGRAPH.findall(block):
        text = _ADW_REFS.sub(" ", raw)
        text = html.unescape(_HTML_TAG.sub(" ", text))
        text = _SPACE_BEFORE_PUNCT.sub(r"\1", _WHITESPACE.sub(" ", text))
        text = text.strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs) or None


def adw_wait(monotonic=time.monotonic, sleep=time.sleep):
    """Honour robots.txt's Crawl-delay: 8 before an ADW request.

    Lives here rather than in the CLI's loop so the promise holds for every
    caller (the reusable-pass rule): the loop service and a future
    per-species refresh button get the same spacing the bulk run does."""
    last = _adw_last_request[0]
    now = monotonic()
    if last is not None and now - last < ADW_THROTTLE_S:
        sleep(ADW_THROTTLE_S - (now - last))
        now = monotonic()
    _adw_last_request[0] = now


def read_adw(sci, *, fetch_text=None, wait=None):
    """One species' ADW vocalization prose, or None. NEVER RAISES.

    That is a contract, not defensiveness: this is the only HTML-parsed
    source in the pass, so a redesign of their site must degrade to an honest
    NULL and a log line rather than taking down a run that is also fetching
    portraits and descriptions for everyone else."""
    # Resolved at call time, not bound as a default, so a test can replace
    # either on the module without reaching into this signature.
    fetch_text = fetch_text or get_text
    wait = wait or adw_wait
    url = adw_url(sci)
    if not url:
        return None
    try:
        wait()
        page = fetch_text(url)
    except Exception as e:
        print(f"[species] ADW unreachable for {sci} ({e}) -- no ADW prose",
              flush=True)
        return None
    try:
        return adw_section(page)
    except Exception as e:
        print(f"[species] ADW markup unreadable for {sci} ({e}) -- treating "
              "as no section", flush=True)
        return None


def adw_prior(conn, sci):
    """A previous ADW look worth trusting -> (prose, attribution, checked_ts),
    else None meaning "go and ask".

    This is the attempt-stamp, and it is keyed on the reader GENERATION rather
    than on the stamp merely existing -- #317's lesson, second application. A
    bare `checked_ts IS NOT NULL` test would settle the six species with no
    ADW account permanently, including past a future improvement to this
    reader; keying on the version settles them until the version moves, which
    is exactly as long as the answer is still current."""
    row = conn.execute(
        "SELECT voice_adw, voice_adw_attribution, voice_adw_checked_ts,"
        " voice_version FROM species_profile WHERE species_sci = ?",
        (sci,)).fetchone()
    if row is None or row["voice_adw_checked_ts"] is None:
        return None
    if (row["voice_version"] or 0) < VOICE_VERSION:
        return None
    return (row["voice_adw"], row["voice_adw_attribution"],
            row["voice_adw_checked_ts"])


def parse_summary(d):
    """The summary query -> {description, image_url, image_name}, all
    honestly None when the wiki has nothing usable. A missing page and a
    disambiguation page both count as nothing: 'Cardinalis' the genus page
    listing three species is not a portrait of anybody."""
    pages = ((d or {}).get("query") or {}).get("pages") or []
    page = pages[0] if pages else None
    empty = {"description": None, "voice": None, "voice_source": None,
             "image_url": None, "image_name": None,
             "image_w": None, "image_h": None}
    if not page or page.get("missing"):
        return empty
    if "disambiguation" in (page.get("pageprops") or {}):
        return empty
    thumb = page.get("thumbnail") or {}
    # What it sounds like (#315), from the same response -- now via the
    # section-then-sentences policy (#317), which also reports which reader
    # answered so the page can credit it honestly.
    voice, voice_source = read_voice(page.get("extract"))
    return {
        "description": clean_extract(page.get("extract")),
        "voice": voice,
        "voice_source": voice_source,
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
    (3) BACKFILL (issues #315, #317, #318): rows whose voice was read by an
        older reader generation -- `voice_version` below VOICE_VERSION, which
        a never-read row satisfies too (NULL coalesces to 0). This replaced
        #315's `voice_checked_ts IS NULL` test, which could only ever fire
        once: after #315 ran, every row had a stamp, so #317's new reader
        would never have been called on a deployed box without a hand
        `--refresh-all`. Keying on the VERSION keeps the same termination
        guarantee -- one pass lifts every row to the current generation and
        the arm empties permanently -- while making the next improvement a
        one-line bump instead of a migration. `voice_checked_ts` is still
        written, now purely as "when did we last look" for humans.

    Owner rows are never in any arm: `image_source = 'owner'` is excluded
    outright, so the pass cannot even spend a fetch on one."""
    return [(r["species_sci"], r["species_common"]) for r in conn.execute(
        "SELECT l.species_sci, l.species_common FROM life_list l"
        " LEFT JOIN species_profile p ON p.species_sci = l.species_sci"
        " WHERE (p.species_sci IS NULL"
        "        OR (p.image_source = 'wikipedia' AND p.image_file IS NOT NULL"
        "            AND p.image_w IS NULL)"
        "        OR (COALESCE(p.voice_version, 0) < ?"
        "            AND COALESCE(p.image_source, '') <> 'owner'))"
        " ORDER BY l.species_common", (VOICE_VERSION,))]


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


def get_text(url):
    """An HTML page as text -- the ADW tier's wire (#318). No retry ladder:
    read_adw() treats every failure as "no ADW prose" anyway, and a site we
    are already crawling at one request per eight seconds does not want a
    second attempt stacked on top of a timeout."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html",
        "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
        return r.read().decode("utf-8", "replace")


# --- The per-species function (what a future refresh button calls) -----------

def enrich_species(conn, media_dir, sci, *, fetch_json=get_json,
                   fetch_bytes=get_bytes, fetch_text=None, now=time.time):
    """Fetch one species' prose + portrait and write its profile row.
    Returns a status word for the caller's log line:
      'owner'    -- row is owner-sourced; NOTHING touched (the rule)
      'no-page'  -- Wikipedia has nothing usable; no row written, species
                    stays on the worklist
      'no-image' -- prose only; row written with honest image NULLs
      'enriched' -- prose + portrait + attribution
    Exceptions propagate: the bulk loop logs and moves on, a single-species
    --refresh should fail loudly. The ADW tier is the exception and swallows
    its own (see read_adw) -- an HTML-parsed source must not be able to fail
    a run that is also fetching portraits for everyone else.
    """
    if owner_locked(conn, sci):
        return "owner"

    summary = parse_summary(fetch_json(summary_url(sci)))
    if not summary["description"] and not summary["image_url"]:
        # No article at all. ADW is deliberately NOT consulted here: this path
        # writes no row, so there would be nowhere to record the ADW attempt,
        # and the enrichment loop would then re-ask the site about the same
        # pageless species every day forever. The six species #318 is aimed at
        # all have Wikipedia articles -- what they lack is a word about sound
        # in them -- so the tier loses nothing by living past this return.
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

    # The third tier (#318). WIKIPEDIA ALWAYS WINS: this runs only when both
    # Wikipedia readers came back empty, so a species can never trade good
    # CC BY-SA prose for a non-commercial source. When Wikipedia does answer,
    # the ADW columns are written back as NULL rather than left standing --
    # an orphaned second prose blob that nothing renders is exactly the kind
    # of thing that gets concatenated by a well-meaning future edit.
    voice_source = summary["voice_source"]
    adw_text = adw_credit = adw_checked = None
    if not summary["voice"]:
        prior = adw_prior(conn, sci)
        if prior is not None:
            adw_text, adw_credit, adw_checked = prior
        else:
            adw_text = read_adw(sci, fetch_text=fetch_text)
            adw_credit = ADW_ATTRIBUTION if adw_text else None
            adw_checked = stamp        # we looked, found or not
        if adw_text:
            voice_source = "adw"

    conn.execute(
        "INSERT OR REPLACE INTO species_profile (species_sci, description,"
        " image_file, image_source, image_attribution, image_w, image_h,"
        " voice, voice_source, voice_version, voice_checked_ts,"
        " voice_adw, voice_adw_attribution, voice_adw_checked_ts, fetched_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sci, summary["description"], image_file, image_source, credit,
         # Dimensions belong to the file we actually wrote: no file, no
         # claim about its shape (the honest-NULL rule).
         summary["image_w"] if image_file else None,
         summary["image_h"] if image_file else None,
         # Wikipedia prose stays in `voice`; ADW prose never joins it here.
         # Separate columns are what keep the two licences separable, and
         # `voice_source` is what tells the GUI which pair to render.
         summary["voice"], voice_source,
         # The reader generation that produced this row (#317). Recorded
         # even when nothing was found, because "version 2 looked and found
         # nothing" is exactly what version 3 needs to know to try again.
         VOICE_VERSION,
         # Stamped even when voice is None -- "we looked and there was
         # nothing" is what keeps this species off the backfill arm (#315).
         stamp,
         adw_text, adw_credit, adw_checked,
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
                    "SELECT voice, voice_source FROM species_profile"
                    " WHERE species_sci = ?", (sci,)).fetchone()
                source = row["voice_source"] if row else None
                # Which reader answered is worth seeing in a bulk run: it is
                # the difference between the article having a section and us
                # having gathered lines out of one that didn't (#317).
                label += {
                    "section": " · voice (section)",
                    "sentences": " · voice (gathered)",
                    "adw": " · voice (Animal Diversity Web)",
                }.get(source, " · no voice found")
                if source:
                    counts["voice"] = counts.get("voice", 0) + 1
                    counts[f"via-{source}"] = counts.get(f"via-{source}", 0) + 1
            print(f"[species] {common}: {label}", flush=True)
        print(f"[species] done: " + ", ".join(
            f"{v} {k}" for k, v in sorted(counts.items())), flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
