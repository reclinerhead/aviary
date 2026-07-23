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
