# =============================================================================
# project-squirrel -- test_bus.py
#
# broker_address() is the one place MERLE_MQTT is interpreted, and its failure
# mode matters: the broker lives on pearl, so a silent localhost fallback used
# to produce a daemon that looked healthy while publishing into the void.
# Missing/blank config must raise, not guess.
# =============================================================================

import pytest

import bus


def test_host_and_port(monkeypatch):
    monkeypatch.setenv("MERLE_MQTT", "192.168.1.64:1883")
    assert bus.broker_address() == ("192.168.1.64", 1883)


def test_host_only_defaults_port_1883(monkeypatch):
    monkeypatch.setenv("MERLE_MQTT", "pearl")
    assert bus.broker_address() == ("pearl", 1883)


def test_missing_env_raises(monkeypatch):
    monkeypatch.delenv("MERLE_MQTT", raising=False)
    with pytest.raises(RuntimeError, match="MERLE_MQTT"):
        bus.broker_address()


def test_blank_env_raises(monkeypatch):
    monkeypatch.setenv("MERLE_MQTT", "   ")
    with pytest.raises(RuntimeError, match="MERLE_MQTT"):
        bus.broker_address()


def test_narration_journal_topic_is_namespaced_under_the_wildcard():
    # Issue #80: per-narrator retained journal windows. The helper and the
    # dashboard's wildcard must agree, or a narrator publishes into the void.
    assert bus.narration_journal_topic("marlin") == "narration/journal/marlin"
    assert bus.NARRATION_JOURNAL_WILDCARD == "narration/journal/+"


def test_narrator_status_topic_matches_the_wildcard_shape():
    assert bus.narrator_status_topic("jim") == "narrators/jim/status"
    assert bus.NARRATOR_STATUS_WILDCARD == "narrators/+/status"


def test_narrator_status_id_round_trips_the_topic():
    # Issue #88: a deferring narrator parses colleague presence topics; the
    # parser and the builder must agree.
    assert bus.narrator_status_id(bus.narrator_status_topic("jim")) == "jim"


def test_narrator_status_id_rejects_other_topics():
    assert bus.narrator_status_id("narration/lines") is None
    assert bus.narrator_status_id("weather/status") is None
    assert bus.narrator_status_id("narrators/jim/mood") is None
    assert bus.narrator_status_id("narrators/a/b/status") is None


def test_frame_topic_round_trips_through_the_parser():
    # Issue #90: the daemon builds the topic, the archiver parses it back.
    # The pair must agree, or every frame publishes into the void.
    topic = bus.frame_topic("20260714_081530_arrival_0007", "full")
    assert topic == "driveway/frames/20260714_081530_arrival_0007/full"
    assert bus.frame_topic_parts(topic) == ("20260714_081530_arrival_0007", "full")
    thumb = bus.frame_topic("x", "thumb")
    assert bus.frame_topic_parts(thumb) == ("x", "thumb")


def test_frame_topic_rejects_unknown_variants():
    with pytest.raises(ValueError):
        bus.frame_topic("id", "medium")


def test_frame_topic_parts_rejects_other_topics():
    # The archiver derives FILENAMES from the parsed id: anything shaped
    # wrong -- extra slashes, unknown variants, foreign topics -- must die
    # here, never reach the filesystem.
    assert bus.frame_topic_parts("driveway/events") is None
    assert bus.frame_topic_parts("driveway/frames/id/medium") is None
    assert bus.frame_topic_parts("driveway/frames/a/b/full") is None
    assert bus.frame_topic_parts("driveway/frames/full") is None
    assert bus.frame_topic_parts("narration/lines") is None
