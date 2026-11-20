# =============================================================================
# aviary -- bus.py
#
# The Aviary's event bus: names and helpers for the MQTT (Mosquitto) topics
# that connect Earl, the bird record, Willard, the liveness watcher and the
# web app. Every process that touches the bus imports its topic names from
# here so a typo'd topic string can't silently split the system in two.
#
# Architecture rule: presence and events live on the bus; nobody needs to
# know who else exists. The bus is the LIVE transport only -- SQLite remains
# the durable archive, so a message dropped while the broker is down is a
# moment nobody heard live, never a lost record.
#
# Every box runs its own broker: the house's on orchid (mqtt.lan), a
# station's on its own loopback. Nothing crosses between them on the bus.
#
# This file came out of project-squirrel, where the same bus also carried the
# vision daemon's driveway/* topics and the narrators' narration/* and
# narrators/* topics. Those stay in squirrel's copy (#691); the namespaces
# below are the ones the Aviary speaks, unchanged.
#
# Topics:
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
#   weather/status           weather presence    "online"/"offline", RETAINED
#                            ("offline" is Willard's MQTT Last Will, so a
#                            crash flips it without anyone noticing the crash)
#   audio/events             Earl -> world       one JSON object per accepted
#                                                audio detection (issue #172):
#                                                species, confidence, source,
#                                                clip path, epoch-seconds ts
#                                                (the weather-namespace time
#                                                convention). Non-retained --
#                                                detections are moments. The
#                                                sightings consumer archives
#                                                them; the bus stays live
#                                                transport only
#   audio/status             Earl presence       "online"/"offline", RETAINED
#                                                ("offline" is the Last Will)
#                                                -- the weather/status
#                                                contract verbatim, in its
#                                                own namespace
#   audio/sources            Earl -> world       per-source health, RETAINED
#                                                JSON ({state, last_window_ts}
#                                                per source) republished on
#                                                every change -- source health
#                                                is *state* (the weather
#                                                reasoning), and one daemon
#                                                with several microphones
#                                                needs finer liveness than
#                                                one status string
#   audio/mask               Earl -> world       the region mask in use (issue
#                                                #593): RETAINED JSON -- week,
#                                                status masked/unmasked, and
#                                                every species the geo model
#                                                allows here this week with
#                                                its geo score. State, not a
#                                                moment (the audio/sources
#                                                reasoning): republished at
#                                                startup and on the week tick.
#                                                The sightings consumer
#                                                persists it (region_mask) so
#                                                the Aviary can show what the
#                                                yard could be hearing
#   services/<name>/status   service presence    "online"/"offline", RETAINED --
#                            the house-wide namespace (issue #147) for a
#                            service that is neither Earl nor Willard. Same
#                            contract as the status topics above. The older
#                            namespaces stay where they are on purpose: each
#                            was split so a wildcard subscriber picks up
#                            exactly its own kind, and this one is additive,
#                            never a migration target. The Aviary's tenant is
#                            services/station-liveness/status (the watcher's
#                            own presence, so a dead watcher is never
#                            mistaken for a dead station)
#   network/<station>/status station liveness    "online"/"offline", RETAINED --
#                            spoken BY the house ABOUT a remote station (epic
#                            #212: a station's own Last Will fires on its own
#                            loopback broker where the house can't hear it,
#                            and the tailnet ACL forbids the station pushing
#                            anything here -- so orchid probes the station's
#                            dashboard over the tailnet and publishes the
#                            debounced verdict itself; flyway/liveness.py,
#                            issue #342). Same payload contract as every
#                            status topic above
#
# The weather topics are retained on purpose: weather is *state*, not a moment.
# A late joiner (a browser tab, a restarted Earl reading the wind) gets the
# latest report from the broker instantly, so nobody needs an HTTP path or a
# poll loop. Nothing is archived from the bus -- Willard keeps reporting and a
# dropped report is refetched on the next poll.
# =============================================================================

import json
import os

import paho.mqtt.client as mqtt

WEATHER_CURRENT_TOPIC = "weather/current"
WEATHER_FORECAST_TOPIC = "weather/forecast"
WEATHER_HISTORY_TOPIC = "weather/history"
WEATHER_REPORT_TOPIC = "weather/report"
WEATHER_STATUS_TOPIC = "weather/status"
AUDIO_EVENTS_TOPIC = "audio/events"
AUDIO_STATUS_TOPIC = "audio/status"
AUDIO_SOURCES_TOPIC = "audio/sources"
AUDIO_MASK_TOPIC = "audio/mask"
SERVICE_STATUS_WILDCARD = "services/+/status"
NETWORK_STATUS_WILDCARD = "network/+/status"


def service_status_topic(name):
    return f"services/{name}/status"


def station_status_topic(name):
    """The liveness verdict the house publishes about one remote station (epic
    #212; flyway/liveness.py). The name is validated by the watcher's
    SAFE_NAME guard before it ever reaches a topic path."""
    return f"network/{name}/status"


def broker_address():
    """Broker host/port from MERLE_MQTT ("host" or "host:port"). REQUIRED --
    no localhost default. The broker may live on another machine entirely
    (a desk session publishing to the house's; a station's is loopback), so a
    silent localhost fallback meant a process that looked healthy while
    publishing into the void. Missing config fails at startup; a broker
    that's merely down is still tolerated (see EventPublisher)."""
    raw = os.environ.get("MERLE_MQTT", "").strip()
    if not raw:
        raise RuntimeError(
            "MERLE_MQTT is not set. There is deliberately no default -- set "
            "it to the host:port of this deployment's broker "
            "(localhost:1883 where the broker is local; the hub's address "
            "from a satellite box)."
        )
    host, _, port = raw.partition(":")
    return host, int(port) if port else 1883


class EventPublisher:
    """A fire-and-forget JSON publisher for processes whose real job is not
    messaging (Willard's poll loop, the liveness watcher's probes).

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
            # Presence, the status-topic contract: retained "online" on every
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

    def publish_bytes(self, topic, payload):
        # Raw bytes on the wire -- same fire-and-forget contract as
        # publish(), just without the JSON encoding a binary payload must
        # not go through. Nothing in the Aviary publishes bytes today (clips
        # are files + paths, D1 in listener/earl.py); kept as the idiom.
        self._client.publish(topic, payload, qos=0, retain=False)

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
