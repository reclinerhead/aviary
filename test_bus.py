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
