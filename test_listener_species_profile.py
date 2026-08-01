# =============================================================================
# project-squirrel -- test_listener_species_profile.py
#
# The species enrichment pass's guarantees (issue #184): Wikipedia responses
# parse honestly (missing pages, missing images, disambiguation pages all
# come back as clean nothings), the filename scrub matches the clips rule,
# the worklist is exactly the un-profiled life list, and -- the rule the
# whole provenance column exists for -- a row whose image_source is 'owner'
# survives every pass and every --refresh untouched.
# =============================================================================

import os
import sqlite3

import pytest

from listener import sightings, species_profile


@pytest.fixture
def conn():
    c = species_profile.connect(":memory:")
    # The pass reads the life list sightings.py owns; give the shared
    # in-memory store both schemas the way earl.db carries both on pearl.
    c.executescript(sightings.SCHEMA)
    return c


@pytest.fixture(autouse=True)
def no_adw_network(monkeypatch):
    """No test may reach animaldiversity.org, including by forgetting to.

    #318's tier fires whenever Wikipedia has no voice prose, which is the
    DEFAULT shape of summary_page() -- so without this guard most of the
    tests in this file would quietly make a real HTTP request (and sleep
    eight seconds doing it). The listener's own CI lesson, one source over:
    a test that reaches the network is a test that fails on somebody else's
    outage. Tests that mean to exercise the tier inject `fetch_text`.

    The crawl-delay stamp is module state, so it is reset here too -- one
    test's request must not make the next one sit out its eight seconds."""
    def refuse(url):
        raise AssertionError(f"test tried to fetch {url}")
    monkeypatch.setattr(species_profile, "get_text", refuse)
    monkeypatch.setattr(species_profile, "_adw_last_request", [None])


def life(conn, sci, common):
    conn.execute(
        "INSERT OR IGNORE INTO life_list (species_sci, species_common, first_ts, first_source, first_clip) VALUES (?,?,?,?,?)",
        (sci, common, 100, "house-rear", None))


def summary_page(**over):
    page = {
        "title": "Northern cardinal",
        "extract": "The northern cardinal is a bird in the genus Cardinalis.",
        "thumbnail": {"source": "https://upload.wikimedia.org/x/900px-C.jpg",
                      "width": 900, "height": 600},  # landscape by default

        "pageimage": "Cardinalis_cardinalis_male.jpg",
        **over,
    }
    return {"query": {"pages": [page]}}


def imageinfo_page(license_name="CC BY-SA 4.0",
                   artist='<a href="//commons.wikimedia.org/wiki/User:J">Jocelyn</a>'):
    meta = {}
    if license_name is not None:
        meta["LicenseShortName"] = {"value": license_name}
    if artist is not None:
        meta["Artist"] = {"value": artist}
    return {"query": {"pages": [{"imageinfo": [{"extmetadata": meta}]}]}}


# --- schema ------------------------------------------------------------------

def test_connect_is_idempotent(tmp_path):
    path = str(tmp_path / "earl.db")
    species_profile.connect(path).close()
    c = species_profile.connect(path)   # second open: same code, no error
    cols = {r["name"] for r in c.execute("PRAGMA table_info(species_profile)")}
    assert {"species_sci", "description", "image_file", "image_source",
            "image_attribution", "fetched_ts"} <= cols
    c.close()


# --- pure shaping ------------------------------------------------------------

def test_image_filename_mirrors_the_clips_scrub():
    assert species_profile.image_filename(
        "Cardinalis cardinalis") == "Cardinalis_cardinalis.jpg"
    assert species_profile.image_filename(
        "../x/../y") == "x_y.jpg"          # hostile input scrubs, never walks
    assert species_profile.image_filename("   ") is None


def test_summary_url_asks_for_everything_in_one_call():
    url = species_profile.summary_url("Cardinalis cardinalis")
    for needle in ("extracts%7Cpageimages%7Cpageprops", "redirects=1",
                   "maxlag=", "Cardinalis+cardinalis"):
        assert needle in url


def test_parse_summary_full_page():
    got = species_profile.parse_summary(summary_page())
    assert got["description"].startswith("The northern cardinal")
    assert got["image_url"].endswith("900px-C.jpg")
    assert got["image_name"] == "Cardinalis_cardinalis_male.jpg"


def test_parse_summary_missing_page_and_disambiguation_are_nothing():
    missing = {"query": {"pages": [{"title": "Nope", "missing": True}]}}
    disambig = summary_page(pageprops={"disambiguation": ""})
    for d in (missing, disambig, None, {}):
        got = species_profile.parse_summary(d)
        assert got == {"description": None, "voice": None,
                       "voice_source": None, "image_url": None,
                       "image_name": None, "image_w": None, "image_h": None}


def test_parse_summary_survives_missing_pieces():
    no_image = summary_page()
    del no_image["query"]["pages"][0]["thumbnail"]
    del no_image["query"]["pages"][0]["pageimage"]
    got = species_profile.parse_summary(no_image)
    assert got["description"] is not None and got["image_url"] is None

    no_text = summary_page(extract="")
    got = species_profile.parse_summary(no_text)
    assert got["description"] is None and got["image_url"] is not None


def test_clean_extract_drops_section_stubs_and_collapses_blanks():
    raw = "One.\n\n\n\nTwo &amp; three.\n== See also ==\njunk"
    assert species_profile.clean_extract(raw) == "One.\n\nTwo & three."


# --- what it sounds like (issue #315) ----------------------------------------

# A bird article shaped like the real ones: lead, some sections, a nested
# subsection inside the voice section, and a sibling section after it.
ARTICLE = """The northern cardinal is a bird in the genus Cardinalis.
It is a mid-sized songbird.

== Description ==
The male is red.

== Songs ==
Both sexes sing clear, whistled phrases &amp; repeat them.

=== Duetting ===
Mated pairs may share song phrases.

== Diet ==
Mostly seeds.
"""


def test_dropping_exintro_leaves_the_description_byte_identical():
    """The entire justification for asking Wikipedia for the whole article:
    clean_extract already cut at the first heading, so the lead it returns
    from a full extract equals what exintro=1 used to hand back."""
    intro_only = ("The northern cardinal is a bird in the genus Cardinalis.\n"
                  "It is a mid-sized songbird.")
    assert species_profile.clean_extract(ARTICLE) == intro_only
    assert species_profile.summary_url("X").find("exintro") == -1


def test_voice_section_reads_the_vocalization_prose():
    got = species_profile.voice_section(ARTICLE)
    # Entities unescaped like any other prose we store...
    assert got.startswith("Both sexes sing clear, whistled phrases & repeat")
    # ...the subsection's prose kept, its heading scaffolding dropped...
    assert "Mated pairs may share song phrases." in got
    assert "Duetting" not in got
    # ...and the next same-level section is somebody else's business.
    assert "seeds" not in got


def test_voice_section_finds_a_nested_heading_without_swallowing_siblings():
    """The Common Grackle case: its section is 'Vocals', nested under
    'Ecology and behavior' -- so a bare-'Vocals' spelling must match, and the
    section must end at its SIBLING, not at its parent's next sibling."""
    nested = ("Lead.\n\n== Ecology and behavior ==\nFlocks in winter.\n\n"
              "=== Vocals ===\nThe song is particularly harsh.\n\n"
              "=== Foraging ===\nEats waste corn.\n\n== Breeding ==\nNests.\n")
    got = species_profile.voice_section(nested)
    assert got == "The song is particularly harsh."


def test_voice_section_matches_every_observed_heading_spelling():
    # Measured across 30 yard species; all of these are real headings.
    for heading in ("Vocalization", "Vocalizations", "Vocalisation", "Vocals",
                    "Voice", "Song", "Songs", "Song and calls",
                    "Call and flight", "Sounds", "Drumming"):
        art = f"Lead.\n\n== {heading} ==\nIt goes cheer-cheer.\n"
        assert species_profile.voice_section(art) == "It goes cheer-cheer.", \
            heading


def test_voice_section_is_none_when_the_article_has_no_such_section():
    """The honest blank. Note 'Description' contains the letters c-r-i and
    'Discovery' contains 'cove' -- word boundaries are what keep an
    unanchored substring from claiming a hit on an ordinary section."""
    plain = ("Lead.\n\n== Description ==\nA small grey bird.\n\n"
             "== Distribution and habitat ==\nWoodland edges.\n")
    assert species_profile.voice_section(plain) is None
    assert species_profile.voice_section("") is None
    assert species_profile.voice_section(None) is None


def test_voice_section_ignores_the_in_culture_trap():
    """Measured on the real Eastern Bluebird article: its only 'song' words
    live in an 'In culture' paragraph about The Wizard of Oz. A heading-scoped
    reader must not surface that -- wrong prose on a false-positive check is
    worse than none."""
    art = ("Lead.\n\n== In culture ==\nBluebirds appear in the "
           "\"Over the Rainbow\" song from The Wizard of Oz.\n")
    assert species_profile.voice_section(art) is None


# --- the sentence-level fallback (issue #317) --------------------------------

# A Veery-shaped article: no vocalization heading anywhere, but real sound
# prose stranded in Description. This is the shape #315 could not read.
#
# NOTE THE PARAGRAPHS ARE SINGLE LINES. That is what the API actually
# returns -- explaintext puts one paragraph on one line and never hard-wraps
# -- and hand-wrapped fixtures quietly test a shape that cannot occur.
BURIED = """The veery is a small North American thrush.

== Description ==
The most common call is a harsh, descending vee-er, which gave the bird its name. The song is a breezy, downward-spiraling series of flute-like notes delivered from a low and concealed perch.

== Feeding ==
It gleans insects from the leaf litter and takes berries in late summer.
"""

# The trap that got the paragraph-density scan rejected in #315: a
# similarity passage dense with "vocal"-adjacent words that is entirely
# about what the bird LOOKS like.
PLUMAGE_TRAP = """The eastern phoebe is a small flycatcher.

== Similar species ==
The eastern phoebe is similar, particularly in the worn plumage after breeding. It always lacks clearly defined wingbars, however, and bobs its tail frequently. It has a shorter primary projection than its relatives, and the underparts are a duller colour overall.
"""


def test_voice_sentences_recovers_prose_buried_in_description():
    got = species_profile.voice_sentences(BURIED)
    assert "vee-er" in got and "flute-like notes" in got
    # Article order preserved...
    assert got.index("vee-er") < got.index("flute-like")
    # ...and the neighbouring section is somebody else's business.
    assert "berries" not in got and "leaf litter" not in got


def test_voice_sentences_refuses_the_plumage_trap():
    """The whole reason this is a SENTENCE reader and not the paragraph
    scan #315 rejected: that scan scored this passage at four 'vocal' words
    and would have served it as a description of a bird's song."""
    assert species_profile.voice_sentences(PLUMAGE_TRAP) is None


def test_voice_sentences_needs_more_than_one_qualifying_sentence():
    """A mention is not a description, and the floor is what keeps this
    quiet on the thin articles it will mostly be pointed at."""
    one = ("A bird.\n\n== Habits ==\nThe song is a thin high whistle repeated "
           "three or four times. It nests in hedgerows and feeds on seeds "
           "throughout the winter months.\n")
    assert species_profile.voice_sentences(one) is None
    # Same text, floor lowered: proves it is the FLOOR rejecting it and not
    # a failure to recognise the sentence at all.
    assert species_profile.voice_sentences(one, minimum=1) is not None


def test_voice_sentences_is_none_when_nothing_claims_a_sound():
    plain = ("A bird.\n\n== Description ==\nA small grey bird of woodland "
             "edges.\n\n== Habitat ==\nIt prefers dense cover.\n")
    assert species_profile.voice_sentences(plain) is None
    assert species_profile.voice_sentences("") is None
    assert species_profile.voice_sentences(None) is None


def test_read_voice_always_prefers_a_real_section():
    """Precedence is absolute, never a length comparison. Measured on the
    real Carolina Wren: 2,308 characters of section against 1,069 the
    sentence reader would assemble -- and the section wins regardless."""
    both = ARTICLE + ("\n== Behaviour ==\nThe song is a clear whistle. "
                      "The call is a sharp chip-chip repeated often.\n")
    prose, source = species_profile.read_voice(both)
    assert source == "section"
    assert prose.startswith("Both sexes sing")      # the Songs section
    assert "chip-chip" not in prose                 # not the gathered lines


def test_read_voice_reports_which_reader_answered():
    assert species_profile.read_voice(ARTICLE)[1] == "section"
    assert species_profile.read_voice(BURIED)[1] == "sentences"
    assert species_profile.read_voice(PLUMAGE_TRAP) == (None, None)


def test_enrich_records_the_voice_source(conn, tmp_path):
    fake_json, fake_bytes, _ = fetchers(summary=summary_page(extract=BURIED))
    species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                   fetch_json=fake_json,
                                   fetch_bytes=fake_bytes)
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    assert row["voice_source"] == "sentences"
    assert "vee-er" in row["voice"]
    assert row["voice_version"] == species_profile.VOICE_VERSION


def test_enrich_stamps_the_version_even_with_no_voice(conn, tmp_path):
    """'Generation 2 looked and found nothing' is precisely what generation
    3 needs to know in order to try again."""
    fake_json, fake_bytes, _ = fetchers()          # summary_page(): silent
    species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                   fetch_json=fake_json,
                                   fetch_bytes=fake_bytes)
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    assert row["voice"] is None and row["voice_source"] is None
    assert row["voice_version"] == species_profile.VOICE_VERSION


def test_worklist_backfills_rows_read_by_an_older_generation(conn, tmp_path):
    """The #317 arm. A #315-era row was read by generation 1 and settled
    under the old rule; keying on the VERSION is what brings it back for the
    new reader without a hand --refresh-all, and one pass settles it again."""
    life(conn, "A sci", "Robin")
    conn.execute(
        "INSERT INTO species_profile (species_sci, description, image_file,"
        " image_source, image_attribution, image_w, image_h, voice,"
        " voice_version, voice_checked_ts, fetched_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("A sci", "prose", "A_sci.jpg", "wikipedia", "cc", 900, 600, None,
         1, 5000, 1))                              # generation 1 = #315
    assert species_profile.worklist(conn) == [("A sci", "Robin")]

    fake_json, fake_bytes, _ = fetchers()
    species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                   fetch_json=fake_json,
                                   fetch_bytes=fake_bytes)
    assert species_profile.worklist(conn) == []    # and stays settled


def test_worklist_version_arm_still_refuses_an_owner_row(conn):
    """An owner row at generation 1 must NOT be dragged back by the version
    bump -- the provenance rule outranks every backfill."""
    life(conn, "A sci", "Robin")
    conn.execute(
        "INSERT INTO species_profile (species_sci, description, image_file,"
        " image_source, image_attribution, image_w, image_h,"
        " voice_version, fetched_ts) VALUES (?,?,?,?,?,?,?,?,?)",
        ("A sci", "todd's", "A_sci.jpg", "owner", "photo: Todd", 900, 600,
         1, 1))
    assert species_profile.worklist(conn) == []


def test_parse_summary_carries_the_voice():
    got = species_profile.parse_summary(summary_page(extract=ARTICLE))
    assert got["voice"].startswith("Both sexes sing")
    assert got["description"].startswith("The northern cardinal is a bird")
    # An article with no vocalization section is an honest NULL, not a guess.
    assert species_profile.parse_summary(summary_page())["voice"] is None


def test_enrich_stamps_voice_checked_even_when_there_is_no_section(conn,
                                                                   tmp_path):
    """The termination guarantee. Without the stamp, the ~27% of species
    whose article has no vocalization section would sit on the backfill arm
    and be re-asked forever."""
    fake_json, fake_bytes, _ = fetchers()          # summary_page(): no section
    species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                   fetch_json=fake_json,
                                   fetch_bytes=fake_bytes,
                                   now=lambda: 4242)
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    assert row["voice"] is None                    # honestly nothing found
    assert row["voice_checked_ts"] == 4242         # but we did look


def test_enrich_stores_the_voice(conn, tmp_path):
    fake_json, fake_bytes, _ = fetchers(summary=summary_page(extract=ARTICLE))
    species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                   fetch_json=fake_json,
                                   fetch_bytes=fake_bytes)
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    assert row["voice"].startswith("Both sexes sing")
    assert row["voice_checked_ts"] is not None


def test_worklist_backfills_unchecked_rows_exactly_once(conn, tmp_path):
    """The #315 backfill arm: a pre-voice row comes back for one look, and
    then -- section or no section -- settles permanently."""
    life(conn, "A sci", "Robin")
    conn.execute(
        "INSERT INTO species_profile (species_sci, description, image_file,"
        " image_source, image_attribution, image_w, image_h, fetched_ts)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("A sci", "prose", "A_sci.jpg", "wikipedia", "cc", 900, 600, 1))
    assert species_profile.worklist(conn) == [("A sci", "Robin")]

    fake_json, fake_bytes, _ = fetchers()          # a page with no section
    species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                   fetch_json=fake_json,
                                   fetch_bytes=fake_bytes)
    assert species_profile.worklist(conn) == []    # and stays empty


def test_all_species_ignores_every_gate(conn):
    """--refresh-all is the recreate-the-lot path: fully enriched rows and
    all. It must not consult the worklist, or it could never re-fetch a
    species that is already settled."""
    life(conn, "A sci", "Robin")
    life(conn, "B sci", "Cardinal")
    conn.execute(
        "INSERT INTO species_profile (species_sci, description, image_file,"
        " image_source, image_attribution, image_w, image_h,"
        " voice, voice_source, voice_version, voice_checked_ts, fetched_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("A sci", "prose", "A_sci.jpg", "wikipedia", "cc", 900, 600,
         "it sings", "section", species_profile.VOICE_VERSION, 1, 1))
    assert species_profile.worklist(conn) == [("B sci", "Cardinal")]
    assert species_profile.all_species(conn) == [
        ("B sci", "Cardinal"), ("A sci", "Robin")]   # common-name order


def test_refresh_all_still_refuses_an_owner_row(conn, tmp_path):
    """all_species() lists it -- the guard lives one layer down, so the
    provenance rule holds on the recreate path too."""
    life(conn, "A sci", "Robin")
    conn.execute(
        "INSERT INTO species_profile (species_sci, description, image_file,"
        " image_source, image_attribution, fetched_ts) VALUES (?,?,?,?,?,?)",
        ("A sci", "todd's", "A_sci.jpg", "owner", "photo: Todd", 1))
    assert species_profile.all_species(conn) == [("A sci", "Robin")]

    fake_json, fake_bytes, calls = fetchers()
    status = species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                            fetch_json=fake_json,
                                            fetch_bytes=fake_bytes)
    assert status == "owner"
    assert calls == []                             # not even one request
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    assert row["description"] == "todd's"          # untouched


def test_parse_imageinfo_strips_credit_html():
    got = species_profile.parse_imageinfo(imageinfo_page())
    assert got == {"license": "CC BY-SA 4.0", "artist": "Jocelyn"}
    silent = species_profile.parse_imageinfo({"query": {"pages": [{}]}})
    assert silent == {"license": None, "artist": None}


def test_attribution_line():
    assert species_profile.attribution("CC BY-SA 4.0", "Jocelyn") == \
        "photo: Jocelyn · CC BY-SA 4.0 · via Wikipedia"
    assert species_profile.attribution("CC0", None) == "CC0 · via Wikipedia"
    assert species_profile.attribution(None, None) is None


# --- Animal Diversity Web, the third tier (issue #318) -----------------------

# VERBATIM ADW markup, captured from the real blue grosbeak account page on
# 2026-07-30 and trimmed only by shortening the two neighbouring sections'
# prose. Everything the extractor has to cope with is here as it really
# ships: the stable h2 id, the pretty-printer putting every tag on its own
# line, inline <a> links inside a sentence, a <refs> citation block, and the
# keyword <ul>s that are a taxonomy widget rather than prose.
ADW_PAGE = """<section>
       <h2 id="behavior">
        Behavior
       </h2>
       <p audience="advanced">
        In the spring, male blue grosbeaks arrive on the breeding grounds before females.
       </p>
      </section>
      <section>
       <h2 id="communication">
        Communication and Perception
       </h2>
       <p audience="advanced">
        Only male blue grosbeaks are known to sing. Their song is a long, rich warble lasting
            around 2.5 seconds. Notably, the song lacks burry qualities common in related species,
            such as
        <a class="taxon-link rank--" href="/accounts/Carpodacus_mexicanus/">
         house finches
        </a>
        and
        <a class="taxon-link rank--" href="/accounts/Passerina_cyanea/">
         indigo buntings
        </a>
        . Their flight call is described as 'zit-zit-zu-zit-zit-zu zoo-zieet zieet zieet'.
        <refs>
         <ref href="06545D87-6413-11E2-931C-002500F14F28">
         </ref>
        </refs>
       </p>
       <ul class="keywords wide">
        <li class="keywords-header">
         Communication Channels
        </li>
        <li>
         <a class="gloss-link popover-replace" href="/glossary/e/#_20020904145822">
          acoustic
         </a>
        </li>
       </ul>
      </section>
      <section>
       <h2 id="food_habits">
        Food Habits
       </h2>
       <p audience="advanced">
        Blue grosbeaks are omnivores, eating mainly insects and seeds.
       </p>
      </section>"""

# A missing ADW account. THE TRAP THIS FIXTURE EXISTS FOR: the site answers
# HTTP **200** for an account that does not exist, so the status code proves
# nothing and the absence of the section is the only honest signal.
# (Measured on Baeolophus_bicolor, the tufted titmouse, 2026-07-30.)
ADW_NO_ACCOUNT = """<main>
       <h2 id="label">
        404 - Page not found
       </h2>
       <p>
        The page you requested could not be found.
       </p>
      </main>"""


def adw_fetcher(page=ADW_PAGE):
    """A stand-in for the ADW wire that records what it was asked for."""
    calls = []

    def fake_text(url):
        calls.append(url)
        return page

    return fake_text, calls


def test_adw_section_reads_the_communication_prose():
    got = species_profile.adw_section(ADW_PAGE)
    assert got.startswith("Only male blue grosbeaks are known to sing.")
    # Linked species names are KEPT and flattened into the sentence -- the
    # comparison against the birds this one gets confused with is the whole
    # reason this tier is worth having, and those names are links.
    assert "such as house finches and indigo buntings." in got
    # The pretty-printer's newline before the period must not survive as a
    # space, or every linked sentence reads "indigo buntings ."
    assert " ." not in got
    # Citations carry no prose and their markup is not prose either.
    assert "ref" not in got and "06545D87" not in got
    # The keyword widget is a taxonomy control; under a sound description it
    # would read as gibberish.
    assert "Communication Channels" not in got and "acoustic" not in got
    # ...and the neighbouring sections belong to somebody else.
    assert "breeding grounds" not in got and "omnivores" not in got


def test_adw_section_is_none_when_there_is_no_such_section():
    """A missing account, an account without the section, and a page from a
    site that has been redesigned are one answer to this pass: nothing."""
    assert species_profile.adw_section(ADW_NO_ACCOUNT) is None
    assert species_profile.adw_section("") is None
    assert species_profile.adw_section(None) is None


def test_adw_section_degrades_to_none_on_changed_markup():
    """The brittleness this tier accepts, bounded. Every one of these is a
    plausible redesign, and none of them may raise -- an HTML-parsed source
    must not be able to fail a run that is also fetching portraits."""
    changed = [
        # the heading survives but the prose moved out of <p>
        '<h2 id="communication">Communication</h2><div>It sings.</div>',
        # the id was renamed
        '<h2 id="comms">Communication and Perception</h2><p>It sings.</p>',
        # unclosed everything
        '<h2 id="communication"><p>It sings.',
        # not even HTML
        "{'json':'now'}",
    ]
    for page in changed:
        assert species_profile.adw_section(page) is None, page


def test_adw_url_scrubs_like_the_clips_rule():
    assert species_profile.adw_url("Passerina caerulea") == \
        "https://animaldiversity.org/accounts/Passerina_caerulea/"
    # A hostile species name scrubs; it never walks out of /accounts/.
    assert species_profile.adw_url("../../etc/passwd") == \
        "https://animaldiversity.org/accounts/etc_passwd/"
    assert species_profile.adw_url("   ") is None


def test_read_adw_never_raises():
    """The contract, stated as a test: read_adw swallows everything."""
    def explode(url):
        raise OSError("the site is down")
    assert species_profile.read_adw("A sci", fetch_text=explode,
                                    wait=lambda: None) is None


def test_adw_wait_honours_the_published_crawl_delay():
    """robots.txt says Crawl-delay: 8 and we say it back. Deliberately NOT
    THROTTLE_S -- that one is our manners, this one is their published rule."""
    slept = []
    clock = [1000.0]

    def wait():
        species_profile.adw_wait(monotonic=lambda: clock[0],
                                 sleep=slept.append)

    wait()                       # first request of the run: no wait owed
    assert slept == []
    clock[0] += 3.0
    wait()                       # 3 s later: the balance of the eight
    assert slept == [pytest.approx(5.0)]
    clock[0] += 100.0
    wait()                       # long past it: nothing owed
    assert slept == [pytest.approx(5.0)]
    assert species_profile.ADW_THROTTLE_S == 8.0
    assert species_profile.ADW_THROTTLE_S != species_profile.THROTTLE_S


def test_enrich_never_asks_adw_when_wikipedia_has_a_section(conn, tmp_path):
    """Wikipedia always wins, and winning means ADW is not even ASKED --
    a species must never trade good CC BY-SA prose for a non-commercial
    source, and we should not spend a request finding that out."""
    fake_json, fake_bytes, _ = fetchers(summary=summary_page(extract=ARTICLE))
    fake_text, adw_calls = adw_fetcher()
    species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                   fetch_json=fake_json,
                                   fetch_bytes=fake_bytes,
                                   fetch_text=fake_text)
    assert adw_calls == []                       # not one request
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    assert row["voice_source"] == "section"
    assert row["voice_adw"] is None              # and nothing stored either
    assert row["voice_adw_attribution"] is None


def test_enrich_falls_through_to_adw_and_credits_it_separately(conn, tmp_path):
    """The six species #318 exists for: a real Wikipedia article that says
    nothing about sound anywhere in it."""
    fake_json, fake_bytes, _ = fetchers()        # summary_page(): silent lead
    fake_text, adw_calls = adw_fetcher()
    species_profile.enrich_species(conn, str(tmp_path), "Passerina caerulea",
                                   fetch_json=fake_json,
                                   fetch_bytes=fake_bytes,
                                   fetch_text=fake_text, now=lambda: 777)
    assert adw_calls == [
        "https://animaldiversity.org/accounts/Passerina_caerulea/"]
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    assert row["voice_source"] == "adw"
    assert "burry qualities" in row["voice_adw"]
    # The attribution is STORED, not derived (#184's rule), and it names both
    # the source and the licence -- ADW prose under a "via Wikipedia" stamp
    # would be a licensing failure as well as a lie.
    assert row["voice_adw_attribution"] == \
        "via Animal Diversity Web · CC BY-NC-SA 3.0"
    assert "CC BY-NC-SA" in row["voice_adw_attribution"]
    assert row["voice_adw_checked_ts"] == 777


def test_adw_prose_is_never_merged_with_wikipedia_prose(conn, tmp_path):
    """The licences are not compatible, so the two texts live in two columns
    and are never concatenated into one blob. Asserted from both sides."""
    fake_json, fake_bytes, _ = fetchers()
    fake_text, _ = adw_fetcher()
    species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                   fetch_json=fake_json,
                                   fetch_bytes=fake_bytes,
                                   fetch_text=fake_text)
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    # ADW answered: its prose is in its OWN column and `voice` stays empty.
    assert row["voice"] is None
    assert row["voice_adw"] is not None

    # And the other direction: Wikipedia answering must clear any ADW text
    # already standing, rather than leave an orphan second blob in the row.
    fake_json2, fake_bytes2, _ = fetchers(
        summary=summary_page(extract=ARTICLE))
    fake_text2, adw_calls2 = adw_fetcher()
    species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                   fetch_json=fake_json2,
                                   fetch_bytes=fake_bytes2,
                                   fetch_text=fake_text2)
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    assert row["voice"].startswith("Both sexes sing")
    assert row["voice_adw"] is None and row["voice_adw_attribution"] is None
    assert adw_calls2 == []


def test_adw_attempt_stamp_settles_a_species_with_no_account(conn, tmp_path):
    """The voice_checked_ts lesson, second occurrence. Six species have no
    ADW account; without a stamp the tier would ask the site about each of
    them on every pass forever, at eight seconds a go."""
    life(conn, "A sci", "Robin")
    fake_json, fake_bytes, _ = fetchers()
    fake_text, adw_calls = adw_fetcher(page=ADW_NO_ACCOUNT)
    species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                   fetch_json=fake_json,
                                   fetch_bytes=fake_bytes,
                                   fetch_text=fake_text, now=lambda: 500)
    assert len(adw_calls) == 1
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    assert row["voice_adw"] is None              # honestly nothing found...
    assert row["voice_adw_checked_ts"] == 500    # ...but we did look
    assert species_profile.worklist(conn) == []  # and it settles

    # A second pass over the same species asks Wikipedia again but NOT ADW.
    fake_json2, fake_bytes2, _ = fetchers()
    fake_text2, adw_calls2 = adw_fetcher(page=ADW_NO_ACCOUNT)
    species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                   fetch_json=fake_json2,
                                   fetch_bytes=fake_bytes2,
                                   fetch_text=fake_text2, now=lambda: 900)
    assert adw_calls2 == []
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    assert row["voice_adw_checked_ts"] == 500    # the ORIGINAL look, kept


def test_a_version_bump_is_what_re_asks_adw(conn, tmp_path):
    """The other half of #317's lesson: a stamp that only tests for existence
    settles a species permanently, including past an improvement to the
    reader. adw_prior() trusts a stored look only at the CURRENT generation,
    so bumping VOICE_VERSION brings the no-account species back for one look
    and nothing else does."""
    conn.execute(
        "INSERT INTO species_profile (species_sci, voice_version,"
        " voice_adw_checked_ts, fetched_ts) VALUES (?,?,?,?)",
        ("A sci", species_profile.VOICE_VERSION - 1, 500, 1))
    assert species_profile.adw_prior(conn, "A sci") is None     # stale look

    conn.execute("UPDATE species_profile SET voice_version = ?",
                 (species_profile.VOICE_VERSION,))
    assert species_profile.adw_prior(conn, "A sci") == (None, None, 500)


def test_connect_adds_the_adw_columns_in_place(tmp_path):
    """A #317-era file gains #318's columns on open -- the repeatable-pass
    rule, never a migration script."""
    path = str(tmp_path / "earl.db")
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE species_profile (species_sci TEXT PRIMARY KEY,"
        " description TEXT, image_file TEXT, image_source TEXT,"
        " image_attribution TEXT, image_w INTEGER, image_h INTEGER,"
        " voice TEXT, voice_source TEXT, voice_version INTEGER,"
        " voice_checked_ts INTEGER, fetched_ts INTEGER NOT NULL)")
    old.execute(
        "INSERT INTO species_profile (species_sci, description, voice,"
        " voice_source, voice_version, fetched_ts) VALUES (?,?,?,?,?,?)",
        ("A sci", "prose", "it sings", "section", 2, 1))
    old.commit()
    old.close()

    c = species_profile.connect(path)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(species_profile)")}
    assert {"voice_adw", "voice_adw_attribution",
            "voice_adw_checked_ts"} <= cols
    row = c.execute("SELECT * FROM species_profile").fetchone()
    assert row["voice"] == "it sings"            # nothing lost
    assert row["voice_adw"] is None              # honestly unknown, for now
    species_profile.connect(path).close()        # and idempotent
    c.close()


# --- worklist ----------------------------------------------------------------

def test_connect_backfills_the_dimension_columns_in_place(tmp_path):
    # A #184-era file: the table exists without image_w/image_h. Opening it
    # with today's code adds them (the repeatable-pass rule -- an upgrade is
    # a restart, not a migration script) and keeps the existing row.
    path = str(tmp_path / "earl.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE species_profile (
            species_sci TEXT PRIMARY KEY, description TEXT, image_file TEXT,
            image_source TEXT, image_attribution TEXT,
            fetched_ts INTEGER NOT NULL);
    """)
    old.execute("INSERT INTO species_profile VALUES (?,?,?,?,?,?)",
                ("A sci", "prose", "A_sci.jpg", "wikipedia", "cc", 1))
    old.commit()
    old.close()

    c = species_profile.connect(path)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(species_profile)")}
    assert {"image_w", "image_h"} <= cols
    row = c.execute("SELECT * FROM species_profile").fetchone()
    assert row["description"] == "prose"          # nothing lost
    assert row["image_w"] is None                 # honestly unknown, for now
    species_profile.connect(path).close()         # and idempotent
    c.close()


def test_parse_summary_carries_the_thumbnail_dimensions():
    got = species_profile.parse_summary(summary_page())
    assert (got["image_w"], got["image_h"]) == (900, 600)
    # A portrait-orientation bird -- the case that motivated #185.
    tall = summary_page(thumbnail={"source": "https://x/900px-C.jpg",
                                   "width": 675, "height": 900})
    got = species_profile.parse_summary(tall)
    assert (got["image_w"], got["image_h"]) == (675, 900)


def test_enrich_stores_the_dimensions(conn, tmp_path):
    fake_json, fake_bytes, _ = fetchers()
    species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                   fetch_json=fake_json,
                                   fetch_bytes=fake_bytes)
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    assert (row["image_w"], row["image_h"]) == (900, 600)


def test_enrich_claims_no_shape_without_a_file(conn, tmp_path):
    # Prose but no portrait: dimensions describe a file we never wrote, so
    # they stay NULL rather than describing nothing.
    no_image = summary_page()
    del no_image["query"]["pages"][0]["thumbnail"]
    del no_image["query"]["pages"][0]["pageimage"]
    fake_json, fake_bytes, _ = fetchers(summary=no_image)
    species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                   fetch_json=fake_json,
                                   fetch_bytes=fake_bytes)
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    assert row["image_w"] is None and row["image_h"] is None


def test_worklist_backfills_dimensionless_rows(conn):
    # The #185 backfill arm: a #184-era wikipedia row with an image but no
    # dimensions comes back onto the worklist, so ONE ordinary re-run heals
    # the whole life list.
    life(conn, "A sci", "Robin")
    life(conn, "B sci", "Cardinal")
    # Both rows are settled for voice at the CURRENT reader generation
    # (#315/#317), so this asserts the DIMENSION arm alone rather than
    # accidentally riding the voice backfill.
    settled = species_profile.VOICE_VERSION
    conn.execute(
        "INSERT INTO species_profile (species_sci, description, image_file,"
        " image_source, image_attribution, image_w, image_h,"
        " voice_version, voice_checked_ts, fetched_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("A sci", "t", "A_sci.jpg", "wikipedia", "cc", None, None,
         settled, 1, 1))
    conn.execute(
        "INSERT INTO species_profile (species_sci, description, image_file,"
        " image_source, image_attribution, image_w, image_h,"
        " voice_version, voice_checked_ts, fetched_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("B sci", "t", "B_sci.jpg", "wikipedia", "cc", 900, 600,
         settled, 1, 1))
    assert species_profile.worklist(conn) == [("A sci", "Robin")]


def test_worklist_never_backfills_an_owner_row(conn):
    # An owner photo has no Wikipedia dimensions to fetch and must never be
    # spent a request on -- the provenance rule, enforced in the SQL.
    # Deliberately left with NO voice_checked_ts: the owner guard has to hold
    # on the #315 arm too, or an owner row would be re-fetched forever.
    life(conn, "A sci", "Robin")
    conn.execute(
        "INSERT INTO species_profile (species_sci, description, image_file,"
        " image_source, image_attribution, image_w, image_h, fetched_ts)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("A sci", "todd's", "A_sci.jpg", "owner", "photo: Todd", None, None, 1))
    assert species_profile.worklist(conn) == []


def test_worklist_leaves_prose_only_rows_alone(conn):
    # No image_file means there is no portrait to measure; re-fetching would
    # be a fresh look at a page that had nothing, not a backfill.
    life(conn, "A sci", "Robin")
    conn.execute(
        "INSERT INTO species_profile (species_sci, description, image_file,"
        " image_source, image_attribution, image_w, image_h,"
        " voice_version, voice_checked_ts, fetched_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("A sci", "prose", None, None, None, None, None,
         species_profile.VOICE_VERSION, 1, 1))
    assert species_profile.worklist(conn) == []


def test_worklist_is_the_unprofiled_life_list(conn):
    life(conn, "A sci", "Robin")
    life(conn, "B sci", "Cardinal")
    life(conn, "C sci", "Jay")
    conn.execute(
        "INSERT INTO species_profile (species_sci, description, image_file,"
        " image_source, image_attribution, image_w, image_h,"
        " voice_version, voice_checked_ts, fetched_ts)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("B sci", "text", None, None, None, None, None,
         species_profile.VOICE_VERSION, 1, 1))
    assert species_profile.worklist(conn) == [
        ("C sci", "Jay"), ("A sci", "Robin")]   # common-name order


# --- the per-species function ------------------------------------------------

def fetchers(summary=None, info=None, image=b"jpegbytes"):
    calls = []

    def fake_json(url):
        calls.append(url)
        return (info or imageinfo_page()) if "imageinfo" in url else \
            (summary or summary_page())

    def fake_bytes(url):
        calls.append(url)
        return image

    return fake_json, fake_bytes, calls


def test_enrich_writes_row_and_portrait(conn, tmp_path):
    fake_json, fake_bytes, _ = fetchers()
    status = species_profile.enrich_species(
        conn, str(tmp_path), "Cardinalis cardinalis",
        fetch_json=fake_json, fetch_bytes=fake_bytes, now=lambda: 42)
    assert status == "enriched"
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    assert row["image_file"] == "Cardinalis_cardinalis.jpg"
    assert row["image_source"] == "wikipedia"
    assert row["image_attribution"] == \
        "photo: Jocelyn · CC BY-SA 4.0 · via Wikipedia"
    assert row["fetched_ts"] == 42
    shelf = tmp_path / "species" / "Cardinalis_cardinalis.jpg"
    assert shelf.read_bytes() == b"jpegbytes"


def test_enrich_no_page_writes_nothing_and_stays_on_worklist(conn, tmp_path):
    life(conn, "Ghostus birdus", "Ghost Bird")
    fake_json, fake_bytes, _ = fetchers(
        summary={"query": {"pages": [{"missing": True}]}})
    status = species_profile.enrich_species(
        conn, str(tmp_path), "Ghostus birdus",
        fetch_json=fake_json, fetch_bytes=fake_bytes)
    assert status == "no-page"
    assert conn.execute("SELECT COUNT(*) c FROM species_profile"
                        ).fetchone()["c"] == 0
    assert ("Ghostus birdus", "Ghost Bird") in species_profile.worklist(conn)


def test_enrich_prose_without_portrait_is_honest_nulls(conn, tmp_path):
    no_image = summary_page()
    del no_image["query"]["pages"][0]["thumbnail"]
    del no_image["query"]["pages"][0]["pageimage"]
    fake_json, fake_bytes, calls = fetchers(summary=no_image)
    status = species_profile.enrich_species(
        conn, str(tmp_path), "A sci",
        fetch_json=fake_json, fetch_bytes=fake_bytes)
    assert status == "no-image"
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    assert row["description"] is not None
    assert row["image_file"] is None and row["image_source"] is None
    assert len(calls) == 1              # no imageinfo call, no byte fetch
    assert not (tmp_path / "species").exists()


def test_owner_row_survives_refresh_untouched(conn, tmp_path):
    conn.execute(
        "INSERT INTO species_profile (species_sci, description, image_file,"
        " image_source, image_attribution, image_w, image_h, fetched_ts)"
        " VALUES (?,?,?,?,?,?,?,?)",
        ("A sci", "todd's own words", "A_sci.jpg", "owner", "photo: Todd",
         None, None, 7))
    fake_json, fake_bytes, calls = fetchers()
    status = species_profile.enrich_species(
        conn, str(tmp_path), "A sci",
        fetch_json=fake_json, fetch_bytes=fake_bytes)
    assert status == "owner"
    assert calls == []                  # not even a fetch -- fully skipped
    row = conn.execute("SELECT * FROM species_profile").fetchone()
    assert row["description"] == "todd's own words"
    assert row["image_source"] == "owner" and row["fetched_ts"] == 7


def test_refresh_overwrites_a_wikipedia_row(conn, tmp_path):
    fake_json, fake_bytes, _ = fetchers()
    species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                   fetch_json=fake_json,
                                   fetch_bytes=fake_bytes, now=lambda: 1)
    better = summary_page(extract="A better article now.")
    fake_json2, fake_bytes2, _ = fetchers(summary=better)
    species_profile.enrich_species(conn, str(tmp_path), "A sci",
                                   fetch_json=fake_json2,
                                   fetch_bytes=fake_bytes2, now=lambda: 2)
    rows = conn.execute("SELECT * FROM species_profile").fetchall()
    assert len(rows) == 1               # OR REPLACE, not a second row
    assert rows[0]["description"] == "A better article now."
    assert rows[0]["fetched_ts"] == 2


# --- the enrichment loop's retry gate (issue #217) ----------------------------

def test_attempt_due_gates_by_age_never_by_existence():
    now = 1_000_000
    # A brand-new lifer has never been attempted: due immediately.
    assert species_profile.attempt_due(None, now) is True
    assert species_profile.attempt_due(now - 60, now) is False
    assert species_profile.attempt_due(
        now - species_profile.PROFILE_RETRY_S, now) is True


def test_record_attempt_upserts_one_row(conn):
    species_profile.record_attempt(conn, "A sci", 123)
    species_profile.record_attempt(conn, "A sci", 456)
    assert species_profile.attempts_map(conn) == {"A sci": 456}


def test_worklist_never_consults_attempts(conn):
    # The do-not-change rule: the retry gate WRAPS the worklist in the loop;
    # the worklist itself -- and therefore the hand-run CLI -- keeps its
    # another-look-for-free semantics untouched.
    life(conn, "A sci", "Robin")
    species_profile.record_attempt(conn, "A sci", 10**10)   # "just attempted"
    assert [w[0] for w in species_profile.worklist(conn)] == ["A sci"]


def test_connect_adds_the_attempts_table_to_an_existing_file(tmp_path):
    path = str(tmp_path / "earl.db")
    # A pre-#217 file: the profile table alone.
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE species_profile (species_sci TEXT PRIMARY KEY,"
        " description TEXT, image_file TEXT, image_source TEXT,"
        " image_attribution TEXT, image_w INTEGER, image_h INTEGER,"
        " fetched_ts INTEGER NOT NULL)")
    old.commit()
    old.close()
    c = species_profile.connect(path)   # same code as a fresh file
    assert c.execute("SELECT COUNT(*) c FROM profile_attempts"
                     ).fetchone()["c"] == 0
    c.close()
