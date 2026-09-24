# =============================================================================
# project-squirrel -- flyway/checkin.py
#
# The station front-door check-in (issue #545): a station telling the public
# front door (aviary.toddtech.llc) where it lives on its home LAN, so the
# link a relative bookmarks never changes while the router's lease does.
#
# Runs as the station-checkin oneshot unit -- on boot, every 15 minutes from
# the timer, and immediately when NetworkManager reports a new lease (the
# dispatcher hook in toddtech-infrastructure/servers/Birdhouse.md starts the same unit; it carries no
# secrets, the unit's EnvironmentFile does). One POST per run, then exit:
# non-zero on any failure so the journal says so.
#
# Outbound plain HTTPS to one public endpoint -- the "ordinary internet
# egress" class epic #212 already uses for autodeploy and the courier. Not
# tailnet traffic; principle 7 and the ACL are untouched.
#
# Which address: the RFC 1918 IPv4 on the Wi-Fi interface (wl*) first, else
# any RFC 1918 address. NEVER 169.254/16 (link-local) and NEVER 100.64/10
# (the tailnet) -- neither is reachable from a relative's browser, and the
# front door would happily redirect into it. The picker is a pure function
# over `ip -j -4 addr show` output, covered by test_flyway_checkin.py.
#
# Config (station.env, required, no default -- the station contract):
#   MERLE_STATION_ID       the slug in the public URL (aviary.toddtech.llc/<id>)
#   MERLE_FRONTDOOR_URL    the check-in endpoint, e.g.
#                          https://aviary.toddtech.llc/api/checkin
#   MERLE_FRONTDOOR_TOKEN  this station's bearer token (matches the server's
#                          FRONTDOOR_STATIONS roster entry)
#
# Stdlib only: runs from the repo venv or the system python3 alike.
# =============================================================================

import ipaddress
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

PRIVATE_NETS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)

POST_TIMEOUT_S = 15.0


def is_private_ipv4(ip):
    """RFC 1918 only -- not link-local, not the tailnet's 100.64/10."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.version != 4:
        return False
    return any(addr in net for net in PRIVATE_NETS)


def _global_v4_addrs(link):
    for info in link.get("addr_info", []) or []:
        if info.get("family") != "inet":
            continue
        if info.get("scope", "global") != "global":
            continue
        local = info.get("local")
        if local and is_private_ipv4(local):
            yield local


def pick_lan_addr(links):
    """(ip, ifname) for the address a relative's browser can reach, or None.

    `links` is the parsed JSON of `ip -j -4 addr show`. Wi-Fi (wl*) wins
    because a station's shipping link is the USB stick; the onboard port is
    the rescue path and only holds a lease when a cable is plugged in.
    """
    wifi, other = [], []
    for link in links or []:
        name = link.get("ifname") or ""
        if not name or name == "lo":
            continue
        if link.get("operstate") == "DOWN":
            continue
        for ip in _global_v4_addrs(link):
            (wifi if name.startswith("wl") else other).append((ip, name))
    if wifi:
        return wifi[0]
    if other:
        return other[0]
    return None


def build_payload(station, ip, iface, sha, now=None):
    """The check-in body -- exactly the keys the front door documents."""
    now = now or datetime.now(timezone.utc)
    payload = {
        "station": station,
        "ip": ip,
        "iface": iface,
        "ts": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    if sha:
        payload["sha"] = sha
    return payload


# --- the boundary -------------------------------------------------------------

def _require(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"checkin: {name} is required (station.env)")
    return value


def _read_links():
    out = subprocess.run(["ip", "-j", "-4", "addr", "show"],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _git_sha():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=True,
                              timeout=10).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _post(url, token, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}",
                 "User-Agent": "project-squirrel station-checkin"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=POST_TIMEOUT_S) as resp:
        return resp.status, resp.read().decode(errors="replace")


def main():
    station = _require("MERLE_STATION_ID")
    url = _require("MERLE_FRONTDOOR_URL")
    token = _require("MERLE_FRONTDOOR_TOKEN")

    picked = pick_lan_addr(_read_links())
    if not picked:
        print("checkin: no private IPv4 address on any interface; nothing to report")
        return 1
    ip, iface = picked
    payload = build_payload(station, ip, iface, _git_sha())

    try:
        status, body = _post(url, token, payload)
    except urllib.error.HTTPError as e:
        print(f"checkin: {station} at {ip} ({iface}) -> HTTP {e.code}: "
              f"{e.read().decode(errors='replace')[:200]}")
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"checkin: {station} at {ip} ({iface}) -> unreachable: {e}")
        return 1
    print(f"checkin: {station} at {ip} ({iface}) -> HTTP {status} {body[:200]}")
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
