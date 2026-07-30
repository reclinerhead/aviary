# =============================================================================
# project-squirrel -- flyway/liveness.py
#
# The station liveness watcher (issue #342, epic #212 Phase 1): a tiny
# pearl-resident service that probes each remote station's dashboard over the
# tailnet and publishes the verdict as retained "online"/"offline" on
# network/<station>/status -- the weather/status contract verbatim, spoken BY
# pearl ABOUT the station. Pearl speaks for the station because the station
# cannot speak for itself here: its own Last Will fires on its own loopback
# broker where pearl can't hear it (principle 1 of the epic), and the tailnet
# ACL correctly forbids the station pushing anything to pearl (principle 7).
# So pearl polls, in the one direction the ACL allows.
#
# Probe the dashboard, not the IP: an HTTP response from the Aviary through
# the station's Caddy proves box powered + network up + service actually
# serving. A ping only proves the kernel is alive.
#
# Debounced (the #175 visit-debounce lesson, one domain over): her power, her
# internet, and our internet are three different outages that look identical
# from here, and a flapping Homestead lamp is noise. OFFLINE_AFTER consecutive
# failed probes flip a station offline (~5 min at the default cadence);
# ONLINE_AFTER consecutive successes bring it back. A watcher restart starts
# from "unknown" and publishes its first verdict when the debounce earns one;
# until then the broker's previously-retained verdict stands, which is the
# honest best answer available.
#
# The audience is us, not her: she won't notice a dark dashboard, we need to.
# The lamp renders on OUR Homestead (launchpad/tiles.json grew a Birdhouse
# tile whose `presence` key carries the topic -- config-only, per #147).
#
# Config (the bus.py conventions):
#   MERLE_MQTT                 required, no default -- the broker's host[:port]
#   MERLE_STATIONS             required, no default -- comma-separated
#                              name=url pairs; the url is the station page to
#                              probe over the tailnet, e.g.
#                              birdhouse=http://100.92.248.83/aviary
#                              Names must be topic-safe lowercase [a-z0-9_-]
#                              -- each becomes network/<name>/status.
#   MERLE_LIVENESS_INTERVAL_S  probe cadence in seconds, default 60 (blank
#                              means default; malformed fails at startup --
#                              the env_float convention)
#
# Runs on pearl as the station-liveness unit -- see Servers/Pearl.md.
# =============================================================================

import http.client
import os
import re
import time
import urllib.request

import paho.mqtt.client as mqtt

import bus

DEFAULT_INTERVAL_S = 60.0
PROBE_TIMEOUT_S = 10.0
OFFLINE_AFTER = 5   # consecutive failed probes before "offline"
ONLINE_AFTER = 2    # consecutive good probes before "online" again

# Station names ride into topic paths (network/<name>/status), so the same
# never-trust-config posture as frame_archiver's SAFE_ID: lowercase
# letters/digits/underscore/dash only -- no separators, no MQTT wildcards.
SAFE_NAME = re.compile(r"[a-z0-9_-]+")

UNKNOWN = ("unknown", 0, 0)   # the starting debounce state: no verdict yet


def parse_stations(raw):
    """MERLE_STATIONS ("name=url[,name=url...]") -> [(name, url)]. Fail-loud
    on anything shaped wrong -- a half-parsed watch list is a station nobody
    is watching, which is precisely the silent failure this service exists
    to catch."""
    stations = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, sep, url = chunk.partition("=")
        name, url = name.strip(), url.strip()
        if not sep or not name or not url:
            raise ValueError(f"MERLE_STATIONS entry is not name=url: {chunk!r}")
        if not SAFE_NAME.fullmatch(name):
            raise ValueError(
                f"MERLE_STATIONS name {name!r} is not topic-safe "
                "(lowercase [a-z0-9_-] only -- it becomes network/<name>/status)")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"MERLE_STATIONS url for {name!r} is not http(s): {url!r}")
        stations.append((name, url))
    if not stations:
        raise ValueError("MERLE_STATIONS is empty -- nothing to watch")
    names = [n for n, _ in stations]
    if len(set(names)) != len(names):
        raise ValueError(f"MERLE_STATIONS repeats a station name: {names}")
    return stations


def stations_from_env():
    raw = os.environ.get("MERLE_STATIONS", "").strip()
    if not raw:
        raise RuntimeError(
            "MERLE_STATIONS is not set. There is deliberately no default -- "
            "set it to comma-separated name=url pairs, e.g. "
            "birdhouse=http://100.92.248.83/aviary")
    return parse_stations(raw)


def interval_s():
    """Blank means the default; a malformed value fails at startup (the
    env_float convention -- never run half-configured while looking healthy)."""
    raw = os.environ.get("MERLE_LIVENESS_INTERVAL_S", "").strip()
    return float(raw) if raw else DEFAULT_INTERVAL_S


def step(state, ok):
    """One probe result through the debounce. state is (verdict, ok_streak,
    fail_streak); returns (new_state, changed). Pure, so the thresholds are
    testable without a clock or a socket.

    The asymmetry is deliberate: "unknown" flips online on the FIRST good
    probe (startup generosity -- there is no flap risk in confirming a box
    that answers), but a known-offline station must answer ONLINE_AFTER times
    before the lamp relights, and any verdict takes OFFLINE_AFTER consecutive
    misses to go dark -- one dropped probe is never a verdict."""
    verdict, ok_streak, fail_streak = state
    if ok:
        ok_streak, fail_streak = ok_streak + 1, 0
        if verdict == "unknown" or (verdict == "offline"
                                    and ok_streak >= ONLINE_AFTER):
            new_verdict = "online"
        else:
            new_verdict = verdict
    else:
        ok_streak, fail_streak = 0, fail_streak + 1
        new_verdict = "offline" if fail_streak >= OFFLINE_AFTER else verdict
    return (new_verdict, ok_streak, fail_streak), new_verdict != verdict


def probe(url, timeout=PROBE_TIMEOUT_S):
    """One GET against the station's page: True on any 2xx/3xx. Everything
    that can go wrong on the way -- DNS, route, refused, timeout, a 4xx/5xx
    from a half-up box, a torn response -- is simply a failed probe; the
    debounce turns failures into a verdict, not this function."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except (OSError, http.client.HTTPException):
        return False


def main():
    stations = stations_from_env()
    every = interval_s()
    host, port = bus.broker_address()   # required, no default (bus.py)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="station-liveness")
    # The watcher's OWN presence, the house Last-Will contract -- so a dead
    # watcher is visible as itself, never mistaken for a dead station (the
    # station verdicts are retained and would simply go stale).
    own_status = bus.service_status_topic("station-liveness")
    client.will_set(own_status, "offline", retain=True)

    def on_connect(c, userdata, flags, reason_code, properties):
        c.publish(own_status, "online", qos=0, retain=True)

    client.on_connect = on_connect
    # connect() (not connect_async): a watcher with no bus has nowhere to put
    # a verdict, so fail loudly at launch. Once up, paho reconnects forever.
    client.connect(host, port)
    client.loop_start()

    states = {name: UNKNOWN for name, _ in stations}
    print(f"[flyway] watching {', '.join(n for n, _ in stations)} "
          f"every {every:g}s (offline after {OFFLINE_AFTER} misses, "
          f"online after {ONLINE_AFTER} answers)")
    try:
        while True:
            for name, url in stations:
                states[name], changed = step(states[name], probe(url))
                if changed:
                    verdict = states[name][0]
                    # Raw string, not JSON -- the status-topic contract; the
                    # Homestead lamps render the payload verbatim.
                    client.publish(bus.station_status_topic(name), verdict,
                                   qos=0, retain=True)
                    detail = (f"{OFFLINE_AFTER} consecutive missed probes"
                              if verdict == "offline" else "answering again"
                              if states[name][1] > 1 else "first answer")
                    print(f"[flyway] {name} is {verdict} ({detail})")
            time.sleep(every)
    except KeyboardInterrupt:
        # Manual desk runs; under systemd SIGTERM drops the socket and the
        # Last Will marks the watcher itself offline.
        client.loop_stop()
        client.disconnect()
        print("\n[flyway] off duty.")


if __name__ == "__main__":
    main()
