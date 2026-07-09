# =============================================================================
# project-squirrel -- bus.py
#
# The Merle event bus: names and helpers for the MQTT (Mosquitto) topics that
# connect the daemon, the narrator(s), the replay script, and the MCC dashboard.
# Every process that touches the bus imports its topic names from here so a
# typo'd topic string can't silently split the system in two.
#
# Architecture rule (epic + issue #9): presence and events live on the bus;
# nobody needs to know who else exists. The bus is the LIVE transport only --
# SQLite remains the durable archive, so a message dropped while the broker is
# down is never a lost record, just a moment nobody narrated.
#
# Topics:
#   driveway/events          daemon -> world     one JSON object per event
#   narration/lines          narrator -> world   one JSON object per spoken line
#   narrators/<id>/status    narrator presence   "online"/"offline", RETAINED
#                            ("offline" is each narrator's MQTT Last Will, so a
#                            crash flips it without anyone noticing the crash)
# =============================================================================

import json
import os

import paho.mqtt.client as mqtt

EVENTS_TOPIC = "driveway/events"
NARRATION_TOPIC = "narration/lines"
NARRATOR_STATUS_WILDCARD = "narrators/+/status"


def narrator_status_topic(mqtt_id):
    return f"narrators/{mqtt_id}/status"


def broker_address():
    """Broker host/port from MERLE_MQTT ("host" or "host:port"). REQUIRED --
    no localhost default. The broker lives on pearl, not on the machine
    running this code, so a silent localhost fallback meant a process that
    looked healthy while publishing into the void. Missing config fails at
    startup; a broker that's merely down is still tolerated (see
    EventPublisher)."""
    raw = os.environ.get("MERLE_MQTT", "").strip()
    if not raw:
        raise RuntimeError(
            "MERLE_MQTT is not set. The MQTT broker does not run on this "
            "machine -- set MERLE_MQTT=192.168.1.64:1883 (pearl) or the "
            "host:port of wherever the broker lives."
        )
    host, _, port = raw.partition(":")
    return host, int(port) if port else 1883


class EventPublisher:
    """A fire-and-forget JSON publisher for processes whose real job is not
    messaging (the daemon's perception loop, the replay script).

    Resilience contract: constructing it is inert; start() connects in the
    background and paho keeps retrying forever, so the owner runs identically
    whether the broker is up, down, or restarted mid-session. publish() never
    blocks and never raises -- QoS 0, so with no broker the message is simply
    dropped (SQLite is the archive; the bus is live transport only)."""

    def __init__(self, client_id):
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=client_id)

    def start(self):
        host, port = broker_address()
        self._client.connect_async(host, port)
        self._client.loop_start()
        return self

    def publish(self, topic, payload):
        self._client.publish(topic, json.dumps(payload), qos=0)

    def close(self):
        self._client.loop_stop()
        self._client.disconnect()
