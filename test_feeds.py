# =============================================================================
# project-squirrel -- test_feeds.py
#
# The feed registry loader (issue #270). Two kinds of coverage:
#
#   1. The loader's contract against synthetic registries: filtering,
#      ordering, and -- mostly -- the fail-loud validation, because the
#      registry's whole reason to exist is that a config typo dies at
#      startup with a named complaint, never runs as a silent default.
#
#   2. The SHIPPED feeds.yml itself. The rover's ssh keepalives and the
#      camera restream URLs used to be code (constants in earl.py) guarded
#      by tests; now they are data, so the guard follows them here. If a
#      registry edit drops ServerAliveInterval from the rover cmd, this file
#      is what goes red -- not a 2 a.m. discovery that merle's half-open
#      ssh waits on the kernel's 2-hour keepalive (issue #201).
# =============================================================================

import dataclasses
import re
import shlex

import pytest

import feeds


def write_registry(tmp_path, text):
    path = tmp_path / "feeds.yml"
    path.write_text(text, encoding="utf-8")
    return path


SAMPLE = """
feeds:
  cam-a:
    kind: rtsp
    url: rtsp://pearl:8554/cam-a
    earl: true
    naturalist: true
  cam-b:
    kind: rtsp
    url: rtsp://pearl:8554/cam-b
    earl: false
  mic-c:
    kind: command
    cmd: arecord -D plughw:0,0 -t raw -q
    earl: true
"""


# --- loading and filtering ----------------------------------------------------

def test_loads_and_filters_by_consumer(tmp_path):
    path = write_registry(tmp_path, SAMPLE)
    all_feeds = feeds.load_feeds(path)
    assert list(all_feeds) == ["cam-a", "cam-b", "mic-c"]   # file order
    assert [f.name for f in feeds.feeds_for("earl", path)] == ["cam-a", "mic-c"]
    assert feeds.feed_for("naturalist", path).url == "rtsp://pearl:8554/cam-a"


def test_flags_default_false(tmp_path):
    path = write_registry(tmp_path,
                          "feeds:\n  f:\n    kind: rtsp\n    url: rtsp://x\n")
    feed = feeds.load_feeds(path)["f"]
    assert feed.earl is False and feed.naturalist is False


def test_unknown_consumer_fails_loud(tmp_path):
    # A typo'd consumer name in CODE is as dangerous as one in config.
    path = write_registry(tmp_path, SAMPLE)
    with pytest.raises(feeds.FeedsError, match="unknown consumer"):
        feeds.feeds_for("naturallist", path)


def test_feed_for_zero_matches_is_as_loud_as_two(tmp_path):
    path = write_registry(tmp_path,
                          "feeds:\n  f:\n    kind: rtsp\n    url: rtsp://x\n")
    with pytest.raises(feeds.FeedsError, match="exactly one"):
        feeds.feed_for("naturalist", path)


# --- the fail-loud validation, one malformation at a time ---------------------

def test_missing_file_raises(tmp_path):
    with pytest.raises(feeds.FeedsError, match="unreadable"):
        feeds.load_feeds(tmp_path / "absent.yml")


def test_invalid_yaml_raises(tmp_path):
    path = write_registry(tmp_path, "feeds: [unclosed")
    with pytest.raises(feeds.FeedsError, match="not valid YAML"):
        feeds.load_feeds(path)


def test_no_feeds_mapping_raises(tmp_path):
    for text in ("", "feeds: {}\n", "cameras:\n  f:\n    kind: rtsp\n"):
        path = write_registry(tmp_path, text)
        with pytest.raises(feeds.FeedsError, match="top-level 'feeds:'"):
            feeds.load_feeds(path)


def test_unknown_kind_raises(tmp_path):
    path = write_registry(tmp_path,
                          "feeds:\n  f:\n    kind: mjpeg\n    url: http://x\n")
    with pytest.raises(feeds.FeedsError, match="kind 'mjpeg'"):
        feeds.load_feeds(path)


def test_rtsp_without_url_raises(tmp_path):
    path = write_registry(tmp_path, "feeds:\n  f:\n    kind: rtsp\n")
    with pytest.raises(feeds.FeedsError, match="needs a non-empty 'url'"):
        feeds.load_feeds(path)


def test_command_without_cmd_raises(tmp_path):
    path = write_registry(tmp_path,
                          "feeds:\n  f:\n    kind: command\n    cmd: ''\n")
    with pytest.raises(feeds.FeedsError, match="needs a non-empty 'cmd'"):
        feeds.load_feeds(path)


def test_unknown_key_raises_instead_of_reading_as_false(tmp_path):
    # The trap this rule exists for: a typo'd flag ("aerl: true") must not
    # quietly configure the feed OUT of its consumer.
    path = write_registry(tmp_path,
                          "feeds:\n  f:\n    kind: rtsp\n    url: rtsp://x\n"
                          "    aerl: true\n")
    with pytest.raises(feeds.FeedsError, match="unknown keys.*aerl"):
        feeds.load_feeds(path)


def test_non_boolean_flag_raises(tmp_path):
    # YAML reads bare `yes`/`no` as bools but a quoted "true" is a string;
    # accepting truthy strings would make "false" truthy. Bools only.
    path = write_registry(tmp_path,
                          "feeds:\n  f:\n    kind: rtsp\n    url: rtsp://x\n"
                          "    earl: 'true'\n")
    with pytest.raises(feeds.FeedsError, match="must be true or false"):
        feeds.load_feeds(path)


def test_duplicate_feed_names_raise(tmp_path):
    # PyYAML's default silently keeps the LAST duplicate -- exactly how a
    # copy-pasted camera block eats its neighbor. The strict loader refuses.
    path = write_registry(tmp_path,
                          "feeds:\n"
                          "  f:\n    kind: rtsp\n    url: rtsp://a\n"
                          "  f:\n    kind: rtsp\n    url: rtsp://b\n")
    with pytest.raises(feeds.FeedsError, match="duplicate key 'f'"):
        feeds.load_feeds(path)


def test_two_naturalist_feeds_raise(tmp_path):
    path = write_registry(tmp_path,
                          "feeds:\n"
                          "  a:\n    kind: rtsp\n    url: rtsp://a\n"
                          "    naturalist: true\n"
                          "  b:\n    kind: rtsp\n    url: rtsp://b\n"
                          "    naturalist: true\n")
    with pytest.raises(feeds.FeedsError, match="exactly one video source"):
        feeds.load_feeds(path)


# --- the redactor -------------------------------------------------------------

def test_redact_rtsp_masks_credentials_and_passes_restreams():
    assert feeds.redact_rtsp("rtsp://admin:sekrit@10.0.0.5:554/x") == \
        "rtsp://admin:***@10.0.0.5:554/x"
    assert feeds.redact_rtsp("rtsp://pearl:8554/house-rear") == \
        "rtsp://pearl:8554/house-rear"


# --- places (issue #612) -------------------------------------------------------

def test_places_default_to_none_without_the_keys(tmp_path):
    # A registry that predates #612 loads exactly as before: every feed has
    # no phrase (callers fall back to the label) and nothing is retired.
    path = write_registry(tmp_path, SAMPLE)
    got = feeds.places(path)
    assert list(got) == ["cam-a", "cam-b", "mic-c"]
    assert all(p.phrase is None for p in got.values())
    # current follows the earl flag: cam-b is in the registry but Earl
    # doesn't run it, so prose must not call it a live microphone.
    assert [k for k, p in got.items() if p.current] == ["cam-a", "mic-c"]


def test_places_read_phrases_and_retired_sources(tmp_path):
    path = write_registry(tmp_path, SAMPLE.replace(
        "    earl: true\n    naturalist: true",
        "    earl: true\n    naturalist: true\n    place: the back porch")
        + "retired_sources:\n  old-mic: the old shed\n")
    got = feeds.places(path)
    assert got["cam-a"] == feeds.Place("the back porch", True)
    assert got["old-mic"] == feeds.Place("the old shed", False)
    assert list(got)[-1] == "old-mic"              # retired after live feeds
    assert feeds.load_feeds(path)["cam-a"].place == "the back porch"


@pytest.mark.parametrize("bad", ["''", "7", "[a, b]"])
def test_a_blank_or_non_string_place_raises(tmp_path, bad):
    path = write_registry(tmp_path,
                          "feeds:\n  f:\n    kind: rtsp\n    url: rtsp://x/f\n"
                          f"    place: {bad}\n")
    with pytest.raises(feeds.FeedsError, match="place"):
        feeds.load_feeds(path)


def test_a_retired_label_that_is_still_live_raises(tmp_path):
    path = write_registry(tmp_path, SAMPLE
                          + "retired_sources:\n  cam-a: the porch\n")
    with pytest.raises(feeds.FeedsError, match="both a live feed"):
        feeds.places(path)


def test_a_retired_source_needs_a_phrase(tmp_path):
    path = write_registry(tmp_path, SAMPLE + "retired_sources:\n  old: ''\n")
    with pytest.raises(feeds.FeedsError, match="non-empty phrase"):
        feeds.places(path)


# --- the yard (issue #636) -----------------------------------------------------

def test_the_yard_is_optional_and_read_whole(tmp_path):
    assert feeds.yard(write_registry(tmp_path, SAMPLE)) is None
    path = write_registry(tmp_path, SAMPLE + "yard: >-\n  A city lot.\n"
                          "  Oaks over the back of the house,\n"
                          "  a creek beyond.\n")
    assert feeds.yard(path) == ("A city lot. Oaks over the back of the house, "
                                "a creek beyond.")
    assert feeds.load_feeds(path)                 # the feeds still parse


@pytest.mark.parametrize("bad", ["''", "7", "[a, b]", "'   '"])
def test_a_blank_or_non_string_yard_raises(tmp_path, bad):
    path = write_registry(tmp_path, SAMPLE + f"yard: {bad}\n")
    with pytest.raises(feeds.FeedsError, match="yard"):
        feeds.yard(path)


def test_the_shipped_house_yard_names_the_place_and_no_figure():
    # The house blurb: what a note may tie a habit to. No digit in it, since
    # anything in the prompt counts as "given" to the notes' validator; and
    # no microphone, since the place phrases carry those as facts.
    text = feeds.yard(feeds.DEFAULT_PATH)
    assert "Kalamazoo" in text and "feeders" in text and "creek" in text
    assert not any(ch.isdigit() for ch in text)
    assert "microphone" not in text.lower()
    # The stations' owners have not written one yet: absent, not an error.
    assert feeds.yard(feeds.DEFAULT_PATH.parent / "feeds.station.yml") is None
    assert feeds.yard(feeds.DEFAULT_PATH.parent / "feeds.kestrel.yml") is None


def test_every_shipped_registry_names_every_live_ear():
    """The field notes call each microphone by its phrase; a live ear with
    no phrase would be named by its raw label ("house-rear") on the page."""
    root = feeds.DEFAULT_PATH.parent
    for name in ("feeds.yml", "feeds.station.yml", "feeds.kestrel.yml"):
        got = feeds.places(root / name)
        unnamed = [k for k, p in got.items() if p.current and not p.phrase]
        assert unnamed == [], (name, unnamed)
    house = feeds.places(feeds.DEFAULT_PATH)
    # The garage trial board's rows (Sep 1 - Oct 13, 2026) are still in the
    # house record and must be named rightly, as a retired post.
    assert house["Warbler32"].current is False
    assert house["rover"].current is False


# --- the SHIPPED registry -----------------------------------------------------

def test_shipped_registry_is_valid_and_names_the_house():
    shipped = feeds.load_feeds(feeds.DEFAULT_PATH)
    assert set(shipped) >= {"house-rear", "house-front", "rover"}
    assert feeds.feed_for("naturalist", feeds.DEFAULT_PATH).name == "house-rear"
    # Feed names double as published source labels (the bird record, clip
    # dirs) and as go2rtc restream paths -- the URL/name join must hold.
    for name in ("house-rear", "house-front"):
        feed = shipped[name]
        assert feed.kind == "rtsp"
        assert feed.url.endswith(f"/{name}")


def test_shipped_garage_mic_is_found_by_mac():
    """The garage Warbler32 (#589) rides kestrel's mechanism (#580): a
    command feed that resolves the board's MAC on every capture restart. Its
    DHCP lease moved on its first day, so an address here would rot -- the
    registry must hold the MAC and nothing else about the LAN."""
    garage = feeds.load_feeds(feeds.DEFAULT_PATH)["garage-rear"]
    assert garage.kind == "command"
    assert garage.earl and not garage.naturalist
    argv = shlex.split(garage.cmd)
    assert argv[0].startswith("/") and argv[0].endswith("/python")
    assert argv[1:] == ["-m", "listener.mic_by_mac", "90:70:69:1a:1b:fc"]
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", garage.cmd), garage.cmd


def test_shipped_station_registry_is_a_working_ear():
    """The station registry (feeds.station.yml, issue #302): one local mic,
    Earl's only source on a box with no cameras and no rover. The capture
    contract is data now, so the guard lives here -- s16le/48kHz/mono on
    stdout is what earl.py's workers assume of every `command` feed."""
    station_path = feeds.DEFAULT_PATH.parent / "feeds.station.yml"
    station = feeds.load_feeds(station_path)
    assert [f.name for f in feeds.feeds_for("earl", station_path)] == \
        ["window-mic"]
    # A station runs no vision daemon; a naturalist flag here would send
    # feed_for("naturalist") looking for a camera that doesn't exist.
    assert not any(f.naturalist for f in station.values())
    argv = shlex.split(station["window-mic"].cmd)
    assert argv[0] == "arecord"
    assert "S16_LE" in argv and "48000" in argv
    assert "raw" in argv          # raw stream, not WAV-headered
    assert argv.count("1") >= 1   # mono (-c 1)
    # The device is addressed BY NAME, never by index (#320): ALSA card
    # numbers are a boot-time race, and a station that loses it captures a
    # healthy-looking nothing from the onboard jack. birdhouse's first cold
    # boot in the field proved it.
    device = argv[argv.index("-D") + 1]
    assert "CARD=" in device, device
    assert not re.search(r"plughw:\d", device), device
    # The capture's slack (#607): the worker classifies one window while the
    # next waits in the pipe (~0.68 s) plus ALSA's buffer. arecord's default
    # 0.5 s overran nearly every window on birdhouse (0.867x real time), so
    # the cmd must ask for at least 2 s. Kestrel's window-mic is held equal
    # to this entry below, so the guard covers both stations.
    buffer_us = None
    for i, arg in enumerate(argv):
        if arg == "-B":
            buffer_us = int(argv[i + 1])
        elif arg.startswith("--buffer-time="):
            buffer_us = int(arg.split("=", 1)[1])
    assert buffer_us is not None and buffer_us >= 2_000_000, \
        station["window-mic"].cmd


def test_shipped_kestrel_registry_is_two_ears():
    """Kestrel's own registry (feeds.kestrel.yml, issue #580): the shared
    station mic plus the rear Warbler32, found by MAC. The file ships on the
    station channel, so it must hold no LAN address -- an address is exactly
    what Kathy's router can change without telling anyone."""
    root = feeds.DEFAULT_PATH.parent
    kestrel_path = root / "feeds.kestrel.yml"
    kestrel = feeds.load_feeds(kestrel_path)
    assert [f.name for f in feeds.feeds_for("earl", kestrel_path)] == \
        ["window-mic", "house-rear"]
    assert not any(f.naturalist for f in kestrel.values())
    # The front ear is the shared station entry exactly, not a fork of it --
    # in every CAPTURE field. The place phrase (#612) is the one honest
    # difference: birdhouse's mic is in a window, kestrel's under an eave.
    station = feeds.load_feeds(root / "feeds.station.yml")
    assert dataclasses.replace(kestrel["window-mic"], place=None) == \
        dataclasses.replace(station["window-mic"], place=None)
    rear = kestrel["house-rear"]
    assert rear.kind == "command"
    argv = shlex.split(rear.cmd)
    assert argv[0].startswith("/") and argv[0].endswith("/python")
    assert argv[1:] == ["-m", "listener.mic_by_mac", "90:70:69:1a:1c:a8"]
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", rear.cmd), rear.cmd


def test_shipped_rover_cmd_carries_ssh_keepalives():
    """Layer 2 of the stall defenses (issue #201), now data: merle powered
    off mid-capture half-opens the TCP, and the kernel's own keepalive is
    ~2 hours away. ServerAlive* makes ssh exit in ~15s, landing the rover
    in the same restart path. BatchMode keeps a keyless deploy from hanging
    on a password prompt it can never answer."""
    rover = feeds.load_feeds(feeds.DEFAULT_PATH)["rover"]
    argv = shlex.split(rover.cmd)
    assert "ServerAliveInterval=5" in argv
    assert "ServerAliveCountMax=3" in argv
    assert "BatchMode=yes" in argv
