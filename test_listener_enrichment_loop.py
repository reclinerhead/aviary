# =============================================================================
# project-squirrel -- test_listener_enrichment_loop.py
#
# The crank that turns itself (issue #217) -- the loop's pure logic and its
# wiring of the two passes' gates, with every wire faked. The load-bearing
# cases: model picking by rank, per-host caps, the 30-minute retry gate around
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
    # No Home and no BirdNET venv: the regional prior (#614) stays quiet
    # unless a test asks for it.
    monkeypatch.delenv("MERLE_LATLON", raising=False)
    monkeypatch.delenv("MERLE_SHOWCASE_PYTHON", raising=False)
    conn = species_profile.connect(path)
    conn.executescript(sightings.SCHEMA)
    conn.executescript(species_analysis.SCHEMA)
    yield conn
    conn.close()


def seed(conn, sci, common, visit_times):
    conn.execute("INSERT OR IGNORE INTO life_list (species_sci, species_common, first_ts, first_source, first_clip) VALUES (?,?,?,?,?)",
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
        # voice_version/voice_checked_ts mirror the real pass: written
        # whether or not vocalization prose was found (#315, #317), which is
        # what takes the species off the worklist for good.
        conn.execute(
            "INSERT OR REPLACE INTO species_profile (species_sci,"
            " description, voice_version, voice_checked_ts, fetched_ts)"
            " VALUES (?,?,?,?,?)",
            (sci, description, species_profile.VOICE_VERSION, NOW, NOW))
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
        # #407: above the analysis visit floor so all three are notes-eligible.
        seed(store, sci, common,
             [at(2026, 7, 18 + d, 7 + i) for d in range(3)])
        # Pre-filled profiles keep the Wikipedia half off the wire entirely
        # -- which now requires voice_version at the CURRENT generation
        # (#315, #317), or the voice backfill arm would put these three
        # straight back on the worklist and this test would make real HTTP
        # calls. Bumping VOICE_VERSION without updating this seed is exactly
        # how that regresses, so it reads the constant rather than a literal.
        store.execute(
            "INSERT INTO species_profile (species_sci, description,"
            " image_file, image_source, image_w, image_h, voice_version,"
            " voice_checked_ts, fetched_ts) VALUES (?,?,?,?,?,?,?,?,?)",
            (sci, "prose", "x.jpg", "wikipedia", 900, 600,
             species_profile.VOICE_VERSION, 1, 1))
    store.commit()
    monkeypatch.setenv("MERLE_OLLAMA", "fakehost")
    monkeypatch.setenv("MERLE_ENRICH_CAP", "1")
    monkeypatch.setattr(loop, "probe", lambda h, p, timeout=None: ["gemma3:12b"])

    results = loop.tick(now=NOW)

    # Three first entries and three sketches (#614) share the one cap. The
    # FIRST ENTRY goes first: a page becomes a journal only once its bird
    # has an entry, and the deploy that ran sketches first left their pages
    # in the old layout with an empty facing page. Five wait -- honestly
    # counted, logged.
    assert store.execute("SELECT COUNT(*) c FROM species_journal"
                         ).fetchone()["c"] == 1       # one generation, not six
    assert store.execute("SELECT COUNT(*) c FROM species_analysis"
                         ).fetchone()["c"] == 0       # the sketches wait
    assert results["analysis"]["written"] == 1
    assert results["analysis"]["deferred"] == 5


def test_a_cap_of_zero_skips_the_host_entirely(store, monkeypatch, fake_llm):
    seed(store, "A sci", "Avocet",
         [at(2026, 7, 18 + d, 7) for d in range(3)])
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


def make_clip(clips_dir, relpath, seconds=9.0):
    """A real little WAV under the clips tree -- quiet noise with a synthetic
    warble in the event window, enough samples for the pass's FFT."""
    import numpy as np
    from listener import clip_enhance, gate
    sr = gate.SAMPLE_RATE
    t = np.arange(int(seconds * sr)) / sr
    x = 0.01 * np.sin(2 * np.pi * 4000 * t) * (t > 3.0)
    path = clips_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    clip_enhance.write_wav(str(path), x.astype(np.float64), sr)
    return path


def test_clip_pass_enhances_and_second_tick_is_quiet(store, monkeypatch,
                                                     tmp_path, capsys):
    """#325: a clip with no sibling gets one within a tick, via the same
    per-clip function the CLI calls; the next tick finds a current sibling
    and -- the journal rule -- says nothing at all."""
    clips = tmp_path / "clips"
    make_clip(clips, "window-mic/100-Blue_Jay.wav")
    monkeypatch.setattr(loop, "probe", lambda h, p, timeout=None: None)

    results = loop.tick(now=NOW)

    assert results["clips"] == {"enhanced": 1}
    assert (clips / "window-mic/100-Blue_Jay-enh.wav").exists()
    assert "clips: 1 enhanced" in capsys.readouterr().out

    capsys.readouterr()
    assert loop.tick(now=NOW)["clips"] == {}
    assert capsys.readouterr().out == ""              # idempotent AND quiet


def test_clip_pass_bounds_a_backlog_and_counts_the_deferred(store, monkeypatch,
                                                            tmp_path):
    """The first drain of a weeks-deep backlog must not own the tick: the
    cap defers the rest LOUDLY (no silent caps), and the idempotent worklist
    hands the remainder to the next tick."""
    clips = tmp_path / "clips"
    for i in range(3):
        make_clip(clips, f"window-mic/{i}-Wren.wav")
    monkeypatch.setattr(loop, "probe", lambda h, p, timeout=None: None)
    monkeypatch.setattr(loop, "CLIP_TICK_CAP", 2)

    first = loop.tick(now=NOW)["clips"]
    assert first == {"enhanced": 2, "deferred": 1}
    assert loop.tick(now=NOW)["clips"] == {"enhanced": 1}


def test_vocal_fill_pass_scores_null_rows_and_is_quiet_next(store, monkeypatch,
                                                            tmp_path, capsys):
    """#447 Phase 0: unscored sightings get vocal_fill within a tick; the
    next tick finds nothing and (journal rule) says nothing."""
    clips = tmp_path / "clips"
    rel = "window-mic/100-Blue_Jay.wav"
    make_clip(clips, rel)
    store.execute(
        "INSERT INTO sightings (ts, source, species_sci, species_common,"
        " confidence, clip, wind_suspect, rms, location_id, vocal_fill)"
        " VALUES (?,?,?,?,?,?,?,?,?,NULL)",
        (NOW, "window-mic", "Cyanocitta cristata", "Blue Jay",
         0.9, rel, 0, 0.01, 0))
    store.commit()
    monkeypatch.setattr(loop, "probe", lambda h, p, timeout=None: None)

    results = loop.tick(now=NOW)
    assert results.get("vocal_fill") == {"scored": 1}
    fill = store.execute(
        "SELECT vocal_fill FROM sightings WHERE clip = ?", (rel,)
    ).fetchone()[0]
    assert fill is not None
    assert 0.0 <= float(fill) <= 1.0
    assert "vocal-fill:" in capsys.readouterr().out

    capsys.readouterr()
    assert loop.tick(now=NOW).get("vocal_fill") == {}
    assert "vocal-fill:" not in capsys.readouterr().out


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
    assert len(calls) == 2                            # retry interval later: another look


def test_a_failed_fetch_is_stamped_and_the_tick_survives(store, monkeypatch,
                                                         capsys):
    seed(store, "A sci", "Avocet", [at(2026, 7, 18, 7)])

    def boom(conn, media, sci):
        raise OSError("wire cut")

    monkeypatch.setattr(loop.species_profile, "enrich_species", boom)

    results = loop.tick(now=NOW)

    assert results["profile"]["failed"] == 1
    assert "FAILED" in capsys.readouterr().out        # loud, once
    # Stamped like any attempt, so a persistent failure is a 30-minute
    # retry, never a per-tick hammer.
    assert species_profile.attempts_map(store) == {"A sci": NOW}

# --- the showcase pass (#551): plan here, encode over there --------------------

def _clip_store(store, tmp_path, n=3):
    """Sightings rows WITH clips on disk, so the showcase pass has a pool."""
    import wave
    import numpy as np
    from listener import clip_showcase as sc
    media = tmp_path / "clips" / "rear"
    media.mkdir(parents=True)
    for i in range(n):
        clip = f"rear/{1000 + i}-x.wav"
        store.execute(
            "INSERT INTO sightings (ts, source, species_sci, species_common,"
            " confidence, clip, wind_suspect, rms, vocal_fill)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (1000 + i, "rear", "P sci", "Pewee", 0.95, clip, 0, 0.01, 0.5))
        pcm = np.zeros(9 * sc.SR, "<i2")
        with wave.open(str(tmp_path / "clips" / clip), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sc.SR)
            w.writeframes(pcm.tobytes())
    store.commit()


def test_showcase_without_an_encoder_says_so_once_and_still_crowns_slot_one(
        store, tmp_path, monkeypatch, capsys):
    from listener import clip_showcase as sc
    _clip_store(store, tmp_path)
    monkeypatch.delenv("MERLE_SHOWCASE_PYTHON", raising=False)
    monkeypatch.setattr(loop, "_showcase_warned", False)
    monkeypatch.setattr(loop, "probe", lambda h, p, timeout=None: None)

    results = loop.tick(now=NOW)
    out = capsys.readouterr().out
    assert results["showcase"] == {"picked": 1}
    assert "MERLE_SHOWCASE_PYTHON not set" in out and "3 clips" in out
    conn = sc.connect(sc.db_path())
    assert list(sc.stored_slots(conn, "P sci")) == [1]   # champion protected
    conn.close()

    loop.tick(now=NOW + 900)
    assert "MERLE_SHOWCASE_PYTHON" not in capsys.readouterr().out  # once


def test_showcase_spawns_the_encoder_under_the_birdnet_venv(
        store, tmp_path, monkeypatch, capsys):
    _clip_store(store, tmp_path)
    monkeypatch.setenv("MERLE_SHOWCASE_PYTHON", "/fake/earl-venv/bin/python")
    monkeypatch.setattr(loop, "_showcase_warned", False)
    monkeypatch.setattr(loop, "probe", lambda h, p, timeout=None: None)
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess_result(0, "[showcase] done: embedded=3\n", "")

    monkeypatch.setattr(loop.subprocess, "run", fake_run)
    results = loop.tick(now=NOW)
    (cmd, kw), = calls
    assert cmd[:4] == ["/fake/earl-venv/bin/python", "-m",
                       "listener.clip_showcase", "--embed-only"]
    assert cmd[-2:] == ["--limit", str(loop.SHOWCASE_TICK_CAP)]
    assert kw["cwd"] == loop.repo_root() and kw["timeout"] == loop.SHOWCASE_TIMEOUT_S
    assert results["showcase"]["embedded"] == 3
    assert "[enrich] done: embedded=3" in capsys.readouterr().out


def test_showcase_encoder_failure_is_loud_and_the_tick_survives(
        store, tmp_path, monkeypatch, capsys):
    _clip_store(store, tmp_path)
    monkeypatch.setenv("MERLE_SHOWCASE_PYTHON", "/fake/python")
    monkeypatch.setattr(loop, "_showcase_warned", False)
    monkeypatch.setattr(loop, "probe", lambda h, p, timeout=None: None)
    monkeypatch.setattr(
        loop.subprocess, "run",
        lambda cmd, **kw: subprocess_result(1, "", "ImportError: no birdnet"))
    results = loop.tick(now=NOW)
    assert results["showcase"]["encoder-failed"] == 1
    assert "exited 1 (ImportError: no birdnet)" in capsys.readouterr().out


def subprocess_result(rc, out, err):
    class R:
        returncode = rc
        stdout = out
        stderr = err
    return R()
