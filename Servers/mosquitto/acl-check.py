#!/usr/bin/env python3
"""Prove the anonymous-read-only ACL on a broker's WebSocket listener (#534).

Run ON the broker box (orchid, birdhouse, kestrel) with a python that has
paho-mqtt -- the Earl venv; mosquitto_pub cannot speak WebSockets:

    ~/earl-venv/bin/python ~/aviary/Servers/mosquitto/acl-check.py

Two directions, both must hold:

  1. an anonymous publish over WebSockets (:9001) never reaches a :1883
     subscriber (nor a :9001 one)
  2. a :1883 publish still reaches an anonymous WebSockets subscriber

Exit 0 when both hold, 1 otherwise. Uses a throwaway topic under acltest/
with a per-run id, so concurrent runs and stale retained state cannot
confuse it.
"""

import argparse
import sys
import threading
import time
import uuid

import paho.mqtt.client as mqtt


def client(transport):
    # paho 2.x requires the callback API version as the first argument;
    # paho 1.x has no such enum. The callbacks below accept both signatures.
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, transport=transport)
    except AttributeError:
        return mqtt.Client(transport=transport)


def connect(c, host, port, timeout, subscribe=None, sink=None):
    """Connect, optionally subscribe on CONNACK, and wait for the connection."""
    ready = threading.Event()

    def on_connect(cl, userdata, flags, *rest):
        if subscribe:
            cl.subscribe(subscribe)
        ready.set()

    c.on_connect = on_connect
    if sink is not None:
        c.on_message = lambda cl, userdata, msg: sink.append(msg.payload.decode())
    c.connect(host, port)
    c.loop_start()
    if not ready.wait(timeout):
        raise SystemExit(f"no CONNACK from {host}:{port} within {timeout}s")
    return c


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--ws-port", type=int, default=9001)
    ap.add_argument(
        "--wait", type=float, default=1.5, help="seconds to allow for delivery"
    )
    args = ap.parse_args()

    run = uuid.uuid4().hex[:8]
    topic_ws = f"acltest/{run}/from-ws"
    topic_plain = f"acltest/{run}/from-1883"
    got_plain, got_ws = [], []
    timeout = 5.0

    sub_plain = connect(
        client("tcp"), args.host, args.mqtt_port, timeout,
        subscribe=f"acltest/{run}/#", sink=got_plain,
    )
    sub_ws = connect(
        client("websockets"), args.host, args.ws_port, timeout,
        subscribe=f"acltest/{run}/#", sink=got_ws,
    )
    time.sleep(0.5)  # let both SUBACKs land

    # 1. anonymous publish over WebSockets -> must be dropped by the ACL
    pub_ws = connect(client("websockets"), args.host, args.ws_port, timeout)
    pub_ws.publish(topic_ws, "MUST-NOT-ARRIVE", qos=1)
    time.sleep(args.wait)
    pub_ws.loop_stop()
    pub_ws.disconnect()

    # 2. publish on 1883 -> must reach the WebSockets subscriber
    pub_plain = connect(client("tcp"), args.host, args.mqtt_port, timeout)
    pub_plain.publish(topic_plain, "reads-ok", qos=1)
    time.sleep(args.wait)
    pub_plain.loop_stop()
    pub_plain.disconnect()

    for s in (sub_plain, sub_ws):
        s.loop_stop()
        s.disconnect()

    ws_write_blocked = "MUST-NOT-ARRIVE" not in got_plain + got_ws
    ws_read_ok = "reads-ok" in got_ws
    print(
        f"anonymous publish over :{args.ws_port} dropped   : "
        + ("OK" if ws_write_blocked else "FAIL (it arrived)")
    )
    print(
        f"subscribe over :{args.ws_port} still receives    : "
        + ("OK" if ws_read_ok else "FAIL (nothing arrived)")
    )
    return 0 if ws_write_blocked and ws_read_ok else 1


if __name__ == "__main__":
    sys.exit(main())
