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
#   narration/journal        narrator -> world   the field journal: the last 50
#                                                spoken lines, RETAINED and
#                                                republished whole (issue #58) --
#                                                a fresh dashboard tab gets the
#                                                journal back the way a late
#                                                joiner gets the weather
#   narrators/<id>/status    narrator presence   "online"/"offline", RETAINED
#                            ("offline" is each narrator's MQTT Last Will, so a
#                            crash flips it without anyone noticing the crash)
#   weather/current          weather -> world    latest conditions, RETAINED
#   weather/forecast         weather -> world    shaped forecast series, RETAINED
#   weather/history          weather -> world    rolling 48h observed window at
#                                                5-min resolution, RETAINED
#                                                (republished whole)
#   weather/report           weather -> world    Willard's on-air segment (issue
#                                                #45): LLM-narrated conditions +
#                                                outlook, RETAINED, ~every 30 min;
#                                                absent entirely when the LLM
#                                                tier is off (MERLE_OLLAMA unset)
#   weather/status           weather presence    "online"/"offline", RETAINED --
#                            same contract as narrators/<id>/status ("offline"
#                            is the Last Will), but in the weather namespace:
#                            Willard is a reporter, not a narrator, and must
#                            not light up the Field Journal's presence wildcard
#
# The weather topics are retained on purpose: weather is *state*, not a moment.
# A late joiner (dashboard tab, restarted narrator) gets the latest report from
# the broker instantly, so nobody needs an HTTP path or a poll loop. Nothing is
# archived -- the station keeps reporting and a dropped report is refetched on
# the next poll, so the bus stays live-transport-only.
# =============================================================================

import json
import os

import paho.mqtt.client as mqtt

EVENTS_TOPIC = "driveway/events"
NARRATION_TOPIC = "narration/lines"
NARRATION_JOURNAL_TOPIC = "narration/journal"
NARRATOR_STATUS_WILDCARD = "narrators/+/status"
WEATHER_CURRENT_TOPIC = "weather/current"
WEATHER_FORECAST_TOPIC = "weather/forecast"
WEATHER_HISTORY_TOPIC = "weather/history"
WEATHER_REPORT_TOPIC = "weather/report"
WEATHER_STATUS_TOPIC = "weather/status"


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

    def __init__(self, client_id, status_topic=None):
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=client_id)
        self._status_topic = status_topic
        if status_topic:
            # Presence, the narrator contract: retained "online" on every
            # (re)connect, retained "offline" as the Last Will. The will fires
            # whenever the socket dies without an MQTT DISCONNECT -- a crash,
            # but also systemd's SIGTERM -- so `systemctl stop` flips the
            # dashboard lamp instantly with no signal handling here. Raw
            # strings, not JSON: the status topics predate this and the
            # dashboard renders them verbatim.
            self._client.will_set(status_topic, "offline", retain=True)
            self._client.on_connect = self._announce

    def _announce(self, client, userdata, flags, reason_code, properties):
        client.publish(self._status_topic, "online", qos=0, retain=True)

    def start(self):
        host, port = broker_address()
        self._client.connect_async(host, port)
        self._client.loop_start()
        return self

    def publish(self, topic, payload, retain=False):
        # retain=True marks the payload as broker-held *state* (weather topics):
        # late subscribers get the latest one immediately. Event-shaped topics
        # stay retain=False -- replaying a stale event to every new subscriber
        # would be worse than dropping it.
        self._client.publish(topic, json.dumps(payload), qos=0, retain=retain)

    def close(self):
        if self._status_topic:
            # Graceful sign-off: say offline ourselves -- a clean DISCONNECT
            # suppresses the Last Will (that's for crashes). wait_for_publish
            # gives the network thread a beat to flush before we stop it;
            # timing out just means the will does the job instead.
            info = self._client.publish(self._status_topic, "offline",
                                        qos=0, retain=True)
            try:
                info.wait_for_publish(timeout=2)
            except (ValueError, RuntimeError):
                pass  # no connection -> nothing to flush, nothing retained to fix
        self._client.loop_stop()
        self._client.disconnect()
