# =============================================================================
# project-squirrel -- test_listener_enrichment_loop.py
#
# The crank that turns itself (issue #217) -- the loop's pure logic and its
# wiring of the two passes' gates, with every wire faked. The load-bearing
# cases: model picking by rank, per-host caps, the ~daily retry gate around
# the profile worklist, and THE tick-ordering guarantee -- a brand-new lifer
# gets its Wikipedia background written BEFORE its first field note is
# generated, so the note's prompt can carry it.
#
# Host probing, real HTTP, and the sleep between ticks are I/O at the
# boundary -- desk-tested, not unit tested (the standing policy).
# =============================================================================

import time

import pytest

from listener import (enrichment_loop as loop, sightings, species_analysis,
                      species_profile)


def at(year, month, day, hour, minute=0):
    """A local-time epoch, so tests read the same in any timezone."""
    return int(time.mktime((year, month, day, hour, minute, 0, 0, 0, -1)))


NOW = at(2026, 7, 20, 12)


# --- pure shaping ------------------------------------------------------------

def test_parse_tags_reads_the_models_and_shrugs_at_garbage():
    d = {"models": [{"name": "gemma3:12b"}, {"name": "big"}, {"junk": 1}]}
    assert loop.parse_tags(d) == ["gemma3:12b", "big"]
    assert loop.parse_tags({}) == []
    assert loop.parse_tags(None) == []
    assert loop.parse_tags({"models": "garbage"}) == []


def test_pick_model_takes_the_best_ranked_the_host_holds():
    rank = ["huge", "big", "small"]
    assert loop.pick_model(["small", "big"], rank) == "big"
    # A host holding only unranked models still serves -- outranks()
    # guarantees an unknown model can never claw back a ranked row.
    assert loop.pick_model(["mystery", "other"], rank) == "mystery"
    assert loop.pick_model([], rank) is None


def test_tick_cap_by_position_with_the_last_entry_extending():
    default = loop.DEFAULT_TICK_CAP
    assert loop.tick_cap("", 0) == default
    assert loop.tick_cap("7", 0) == 7
    assert loop.tick_cap("7", 3) == 7           # one value covers every host
    assert loop.tick_cap("20,2", 0) == 20
    assert loop.tick_cap("20,2", 1) == 2
    assert loop.tick_cap("20,2", 5) == 2        # past the end: last extends
    assert loop.tick_cap("0", 0) == 0           # 0 = never generate here
    assert loop.tick_cap("banana", 0) == default
    assert loop.tick_cap("-3", 0) == default


# --- the tick, every wire faked ----------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    """A file-backed earl.db (the loop opens its own connections) carrying
    all three schemas, plus a quiet env: no weather archive, no ambient
    MERLE_OLLAMA leaking in from the desk."""
    path = str(tmp_path / "earl.db")
    monkeypatch.setenv("MERLE_EARL_DB", path)
    monkeypatch.setenv("MERLE_EARL_CLIPS", str(tmp_path / "clips"))
    monkeypatch.setenv("MERLE_WEATHER_DB", str(tmp_path / "no-weather.db"))
    monkeypatch.delenv("MERLE_OLLAMA", raising=False)
    monkeypatch.delenv("MERLE_ENRICH_CAP", raising=False)
    monkeypatch.delenv("MERLE_MODEL_RANK", raising=False)
    conn = species_profile.connect(path)
    conn.executescript(sightings.SCHEMA)
    conn.executescript(species_analysis.SCHEMA)
    yield conn
    conn.close()


def seed(conn, sci, common, visit_times):
    conn.execute("INSERT OR IGNORE INTO life_list VALUES (?,?,?,?,?)",
                 (sci, common, min(visit_times), "amcrest", None))
    for ts in visit_times:
        conn.execute(
            "INSERT INTO sightings (ts, source, species_sci, species_common,"
            " confidence, clip, wind_suspect, rms) VALUES (?,?,?,?,?,?,?,?)",
            (ts, "amcrest", sci, common, 0.9, None, 0, 0.01))
    conn.commit()


class FakeOllama:
    def __init__(self, host, port, model):
        self.host, self.port, self.model = host, port, model
        self.prompts = []

    def complete(self, system, prompt, **kw):
        self.prompts.append(prompt)
        return "Prose."


@pytest.fixture
def fake_llm(monkeypatch):
    """Substitute the Ollama client the drain constructs; returns the made
    instances so tests can read the prompts that were actually sent."""
    made = []

    def factory(host, port, model):
        client = FakeOllama(host, port, model)
        made.append(client)
        return client

    monkeypatch.setattr(loop, "Ollama", factory)
    return made


def profile_writer(description="A very red bird."):
    """A fake enrich_species that writes a prose-only row, the way the real
    pass does for a bird whose article has no usable photo."""
    calls = []

    def fake(conn, media, sci):
        calls.append(sci)
        conn.execute(
            "INSERT OR REPLACE INTO species_profile (species_sci,"
            " description, fetched_ts) VALUES (?,?,?)",
            (sci, description, NOW))
        conn.commit()
        return "no-image"

    return fake, calls


def test_a_lifer_gets_profile_then_notes_in_one_tick(store, monkeypatch,
                                                     fake_llm):
    """THE ordering guarantee: profile first, so the note's rhythm prompt
    carries the background the profile pass just wrote -- same tick."""
    seed(store, "Cardinalis cardinalis", "Northern Cardinal",
         [at(2026, 7, d, 7) for d in range(1, 13)])
    fake_enrich, _ = profile_writer("A very red bird.")
    monkeypatch.setattr(loop.species_profile, "enrich_species", fake_enrich)
    monkeypatch.setenv("MERLE_OLLAMA", "fakehost")
    monkeypatch.setattr(loop, "probe", lambda h, p, timeout=None: ["gemma3:12b"])

    loop.tick(now=NOW)

    row = store.execute("SELECT * FROM species_analysis").fetchone()
    assert row is not None
    assert row["host"] == "fakehost:11434"            # provenance
    assert row["model"] == "gemma3:12b"
    assert row["prompt_version"] == species_analysis.PROMPT_VERSION
    # The tick's whole point: the description written moments earlier rode
    # the very first note's prompt.
    assert any("A very red bird." in p for p in fake_llm[0].prompts)


def test_profiles_still_run_when_no_host_answers(store, monkeypatch, capsys):
    seed(store, "Cardinalis cardinalis", "Northern Cardinal",
         [at(2026, 7, 18, 7)])
    fake_enrich, calls = profile_writer()
    monkeypatch.setattr(loop.species_profile, "enrich_species", fake_enrich)
    monkeypatch.setenv("MERLE_OLLAMA", "fakehost")
    monkeypatch.setattr(loop, "probe", lambda h, p, timeout=None: None)

    loop.tick(now=NOW)

    assert calls == ["Cardinalis cardinalis"]         # Wikipedia half ran
    assert store.execute("SELECT COUNT(*) c FROM species_analysis"
                         ).fetchone()["c"] == 0       # notes half wrote nothing
    # And the down host is a NORMAL state: not one journal line about it.
    assert "notes" not in capsys.readouterr().out


def test_an_empty_tick_logs_nothing_at_all(store, monkeypatch, capsys):
    # No lifers, no hosts: the quiet no-op the journal rule demands.
    monkeypatch.setattr(loop, "probe", lambda h, p, timeout=None: None)
    loop.tick(now=NOW)
    assert capsys.readouterr().out == ""


def test_the_cap_bounds_generations_per_tick(store, monkeypatch, fake_llm):
    for i, (sci, common) in enumerate([("A sci", "Avocet"),
                                       ("B sci", "Bunting"),
                                       ("C sci", "Cardinal")]):
        seed(store, sci, common, [at(2026, 7, 18, 7 + i)])
        # Pre-filled profiles keep the Wikipedia half off the wire entirely.
        store.execute(
            "INSERT INTO species_profile (species_sci, description,"
            " image_file, image_source, image_w, image_h, fetched_ts)"
            " VALUES (?,?,?,?,?,?,?)",
            (sci, "prose", "x.jpg", "wikipedia", 900, 600, 1))
    store.commit()
    monkeypatch.setenv("MERLE_OLLAMA", "fakehost")
    monkeypatch.setenv("MERLE_ENRICH_CAP", "1")
    monkeypatch.setattr(loop, "probe", lambda h, p, timeout=None: ["gemma3:12b"])

    results = loop.tick(now=NOW)

    assert store.execute("SELECT COUNT(*) c FROM species_analysis"
                         ).fetchone()["c"] == 1       # one generation, not three
    assert results["analysis"]["written"] == 1
    assert results["analysis"]["deferred"] == 2       # honestly counted, logged


def test_a_cap_of_zero_skips_the_host_entirely(store, monkeypatch, fake_llm):
    seed(store, "A sci", "Avocet", [at(2026, 7, 18, 7)])
    fake_enrich, _ = profile_writer()
    monkeypatch.setattr(loop.species_profile, "enrich_species", fake_enrich)
    monkeypatch.setenv("MERLE_OLLAMA", "fakehost")
    monkeypatch.setenv("MERLE_ENRICH_CAP", "0")
    probed = []
    monkeypatch.setattr(loop, "probe",
                        lambda h, p, timeout=None: probed.append(h) or ["m"])

    loop.tick(now=NOW)

    assert probed == []                               # not even a probe spent
    assert fake_llm == []


def test_no_page_species_retry_daily_not_per_tick(store, monkeypatch):
    seed(store, "Ghost sci", "Ghost", [at(2026, 7, 18, 7)])
    calls = []

    def no_page(conn, media, sci):
        calls.append(sci)
        return "no-page"

    monkeypatch.setattr(loop.species_profile, "enrich_species", no_page)

    loop.tick(now=1_000_000)
    loop.tick(now=1_000_900)                          # 15 minutes later: gated
    assert len(calls) == 1
    loop.tick(now=1_000_000 + species_profile.PROFILE_RETRY_S)
    assert len(calls) == 2                            # a day later: another look


def test_a_failed_fetch_is_stamped_and_the_tick_survives(store, monkeypatch,
                                                         capsys):
    seed(store, "A sci", "Avocet", [at(2026, 7, 18, 7)])

    def boom(conn, media, sci):
        raise OSError("wire cut")

    monkeypatch.setattr(loop.species_profile, "enrich_species", boom)

    results = loop.tick(now=NOW)

    assert results["profile"]["failed"] == 1
    assert "FAILED" in capsys.readouterr().out        # loud, once
    # Stamped like any attempt, so a persistent failure is a daily retry,
    # never a per-tick hammer.
    assert species_profile.attempts_map(store) == {"A sci": NOW}