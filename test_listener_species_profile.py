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

import pytest

from listener import sightings, species_profile


@pytest.fixture
def conn():
    c = species_profile.connect(":memory:")
    # The pass reads the life list sightings.py owns; give the shared
    # in-memory store both schemas the way earl.db carries both on pearl.
    c.executescript(sightings.SCHEMA)
    return c


def life(conn, sci, common):
    conn.execute(
        "INSERT OR IGNORE INTO life_list VALUES (?,?,?,?,?)",
        (sci, common, 100, "amcrest", None))


def summary_page(**over):
    page = {
        "title": "Northern cardinal",
        "extract": "The northern cardinal is a bird in the genus Cardinalis.",
        "thumbnail": {"source": "https://upload.wikimedia.org/x/900px-C.jpg",
                      "width": 900, "height": 600},
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
        assert got == {"description": None, "image_url": None,
                       "image_name": None}


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


# --- worklist ----------------------------------------------------------------

def test_worklist_is_the_unprofiled_life_list(conn):
    life(conn, "A sci", "Robin")
    life(conn, "B sci", "Cardinal")
    life(conn, "C sci", "Jay")
    conn.execute("INSERT INTO species_profile VALUES (?,?,?,?,?,?)",
                 ("B sci", "text", None, None, None, 1))
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
    conn.execute("INSERT INTO species_profile VALUES (?,?,?,?,?,?)",
                 ("A sci", "todd's own words", "A_sci.jpg", "owner",
                  "photo: Todd", 7))
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
