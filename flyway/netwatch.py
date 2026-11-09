# =============================================================================
# project-squirrel -- flyway/netwatch.py
#
# The station Wi-Fi watchdog (issue #686): the station noticing, from inside,
# that its link has gone half-dead, and climbing a recovery ladder instead of
# waiting for a relative to pull the plug.
#
# The fault it exists for (kestrel 2026-11-08, 6.5 hours; 2026-10-11, 90
# minutes): the MT7612U stick on mt76x2u stays associated and hears beacons,
# but data frames are mostly lost. wpa_supplicant, NetworkManager and the
# driver all report a healthy link, so nothing on the box reacts, and pearl's
# liveness watcher (#342) can only report it. So the link is judged by what
# it can DO, not by what the driver says. Each round:
#
#   internet   DNS lookup of the front door's host, then a TCP connect to it
#              on 443 -- both must work. (A resolver cache can hide a dead
#              DNS path for one TTL; it cannot fake the TCP handshake.)
#   gateway    4 pings to the default gateway, the LAN-only control. "Clean"
#              means every one came back. A healthy LAN answers 4/4; a
#              half-dead stick dropping three frames in four answers 4/4
#              about once in 256 rounds.
#
# A run of failed internet rounds is an episode ("dark"); two good rounds in
# a row end it. The ladder, one rung at a time, each rung only once per
# episode and never within RUNG_GAP_S of the previous one:
#
#   1  NetworkManager disconnect + connect of the Wi-Fi interface, after
#      ~10 min dark. Cheap; the only rung a house-internet outage can earn.
#   2  USB unbind + rebind of the stick (reloads the mt76 firmware), after
#      ~30 min dark -- only if the gateway never answered clean this episode.
#   3  Reboot, after ~60 min dark -- same gateway rule, and never twice
#      within REBOOT_GAP_S (the stamp survives the reboot in StateDirectory).
#
# No rung fires in the first BOOT_GRACE_S after boot. A good round costs
# nothing and logs nothing; a dark round logs one line, each rung one more.
# A successful rung 1 or 2 re-raises the lease, so the existing dispatcher
# hook (90-station-checkin, `up`) reports the address to the front door with
# no code here.
#
# Clocks: every in-episode time is CLOCK_BOOTTIME (seconds since boot) --
# immune to NTP stepping the wall clock at boot, and reset by a reboot along
# with the /run state it is stored beside. Only the reboot stamp is wall-clock,
# because it must outlive the boot.
#
# Runs as the station-netwatch oneshot (Servers/station-netwatch.service),
# every 2 minutes from its timer, as todd. The three privileged actions go
# through `sudo -n` against exact command lines in
# Servers/station-netwatch.sudoers -- nothing else this file does is root.
#
# Config (station.env): MERLE_FRONTDOOR_URL, required -- its host is the
# probe target (a host the station already talks to; principle 7 untouched).
# The thresholds are code, not config (the liveness.py posture).
#
# Desk check: `python -m flyway.netwatch --dry-run` runs one round, prints
# what it saw and which rung it would take, and changes nothing.
#
# Stdlib only. Pure logic covered by test_flyway_netwatch.py.
# =============================================================================

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse

from flyway.checkin import pick_lan_addr

MIN = 60

RUNG_AFTER_S = {1: 10 * MIN, 2: 30 * MIN, 3: 60 * MIN}  # dark time per rung
RUNG_GAP_S = 10 * MIN        # let the previous rung work before the next
BOOT_GRACE_S = 10 * MIN      # no rung this soon after boot
REBOOT_GAP_S = 6 * 60 * MIN  # never reboot twice within this (wall clock)
RECOVER_AFTER = 2            # consecutive good rounds that end an episode
STALE_AFTER_S = 20 * MIN     # a longer gap between rounds starts over

PING_COUNT = 4
TCP_TIMEOUT_S = 5.0
USB_SETTLE_S = 3.0

RUNG_NAMES = {1: "reconnect", 2: "usb rebind", 3: "reboot"}


def empty_state():
    return {
        "dark_since": None,    # boottime of the episode's first failed round
        "last_round": None,    # boottime of the last round observed
        "ok_streak": 0,        # consecutive good rounds inside an episode
        "dark_rounds": 0,      # failed rounds this episode
        "clean_rounds": 0,     # rounds this episode the gateway answered 4/4
        "rungs": [],           # rungs taken this episode, in order
        "last_rung_at": None,  # boottime of the last rung, kept across episodes
    }


# --- the pure logic ------------------------------------------------------------

def observe(state, internet_ok, gateway_clean, now):
    """One round through the episode tracker. Returns (new_state, event),
    event being "dark" (an episode began), "recovered" (one ended) or None.

    The gateway result only matters inside an episode: a clean gateway on
    any dark round marks the episode as the house's internet, not the stick,
    which caps it at rung 1."""
    s = dict(state)
    s["rungs"] = list(state.get("rungs", []))
    if s["last_round"] is not None and now - s["last_round"] > STALE_AFTER_S:
        # The timer stopped (unit off, box suspended): an old streak says
        # nothing about now. last_rung_at survives -- the gap still applies.
        kept = s["last_rung_at"]
        s = empty_state()
        s["last_rung_at"] = kept
    s["last_round"] = now

    if internet_ok:
        if s["dark_since"] is None:
            return s, None
        s["ok_streak"] += 1
        if s["ok_streak"] >= RECOVER_AFTER:
            kept = s["last_rung_at"]
            s = empty_state()
            s["last_round"], s["last_rung_at"] = now, kept
            return s, "recovered"
        return s, None

    event = None
    if s["dark_since"] is None:
        s["dark_since"] = now
        event = "dark"
    s["ok_streak"] = 0
    s["dark_rounds"] += 1
    if gateway_clean:
        s["clean_rounds"] += 1
    return s, event


def reboot_allowed(last_reboot_wall, now_wall):
    """At most one watchdog reboot per REBOOT_GAP_S. A stamp from the future
    (the wall clock stepped back) counts as recent within the same gap."""
    if last_reboot_wall is None:
        return True
    return abs(now_wall - last_reboot_wall) >= REBOOT_GAP_S


def pick_rung(state, now, reboot_ok):
    """Which rung to take this round (1, 2, 3) or None. `now` is seconds
    since boot. Rungs climb in order, once each per episode."""
    if state["dark_since"] is None:
        return None
    if now < BOOT_GRACE_S:
        return None
    last = state["last_rung_at"]
    if last is not None and now - last < RUNG_GAP_S:
        return None
    rung = len(state["rungs"]) + 1
    if rung > 3:
        return None
    if now - state["dark_since"] < RUNG_AFTER_S[rung]:
        return None
    if rung >= 2 and state["clean_rounds"] > 0:
        return None   # the gateway answers: it is the house's internet
    if rung == 3 and not reboot_ok:
        return None
    return rung


def took(state, rung, now):
    s = dict(state)
    s["rungs"] = list(state["rungs"]) + [rung]
    s["last_rung_at"] = now
    return s


def wifi_iface(addr_links, all_links):
    """The interface the ladder acts on: pick_lan_addr's choice when it is
    the Wi-Fi, else any wl* link at all -- a stick that lost its lease is
    exactly the one to bounce. `addr_links` is `ip -j -4 addr show` (lists
    only links holding an IPv4 address), `all_links` is `ip -j link show`."""
    picked = pick_lan_addr(addr_links)
    if picked and picked[1].startswith("wl"):
        return picked[1]
    for link in all_links or []:
        name = link.get("ifname") or ""
        if name.startswith("wl"):
            return name
    return None


def default_gateway(routes, iface):
    """The default route's gateway from `ip -j -4 route show default`,
    preferring the route out of `iface`."""
    gws = [(r.get("dev"), r.get("gateway")) for r in routes or []
           if r.get("dst") == "default" and r.get("gateway")]
    for dev, gw in gws:
        if dev == iface:
            return gw
    return gws[0][1] if gws else None


_USB_IFACE = re.compile(r"(\d+-\d+(?:\.\d+)*):\d+\.\d+")
_USB_DEVICE = re.compile(r"\d+-\d+(?:\.\d+)*")


def usb_device(device_link):
    """The USB device name to unbind ("1-1.1") from the target of
    /sys/class/net/<iface>/device -- relative ("../../../1-1.1:1.0") or
    resolved. The link names the USB *interface* (1-1.1:1.0); the driver
    binds the *device* (1-1.1). None for anything that is not USB (a PCI
    NIC's 0000:01:00.0, a root hub's usb1)."""
    last = (device_link or "").rstrip("/").rsplit("/", 1)[-1]
    m = _USB_IFACE.fullmatch(last)
    if m:
        return m.group(1)
    if _USB_DEVICE.fullmatch(last):
        return last
    return None


_RECEIVED = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")


def ping_clean(output):
    """True when `ping -q` reports every packet answered."""
    m = _RECEIVED.search(output or "")
    return bool(m) and int(m.group(1)) > 0 and m.group(1) == m.group(2)


# --- the boundary (I/O; desk-verified on a station, not unit-tested) ------------

def _run(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


def _ip_json(*args):
    code, out = _run(["ip", "-j", *args], timeout=10)
    if code != 0:
        return []
    try:
        return json.loads(out)
    except ValueError:
        return []


def probe_internet(host):
    """(ok, detail): DNS for the front door's host, then TCP 443 to it."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as e:
        return False, f"dns failed ({e})"
    err = "no address"
    for family, socktype, proto, _, addr in infos[:2]:
        try:
            with socket.socket(family, socktype, proto) as s:
                s.settimeout(TCP_TIMEOUT_S)
                s.connect(addr)
            return True, "ok"
        except OSError as e:
            err = e
    return False, f"dns ok, tcp 443 failed ({err})"


def probe_gateway(gw):
    if not gw:
        return False, "no default route"
    code, out = _run(["ping", "-n", "-q", "-c", str(PING_COUNT), "-i", "0.5",
                      "-W", "2", gw], timeout=30)
    m = _RECEIVED.search(out)
    got = f"{m.group(2)}/{m.group(1)}" if m else "no reply"
    return ping_clean(out), f"gateway {gw} {got}"


def _sudo(*cmd):
    return _run(["sudo", "-n", *cmd])


def rung_reconnect(iface):
    code, out = _sudo("/usr/bin/nmcli", "device", "disconnect", iface)
    if code != 0:
        return f"disconnect failed: {out[:200]}"
    code, out = _sudo("/usr/bin/nmcli", "device", "connect", iface)
    return "reconnected" if code == 0 else f"connect failed: {out[:200]}"


def _sysfs_write(path, value):
    try:
        p = subprocess.run(["sudo", "-n", "/usr/bin/tee", path], input=value,
                           capture_output=True, text=True, timeout=30)
        return p.returncode, p.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return -1, str(e)


def rung_usb_rebind(iface):
    try:
        target = os.readlink(f"/sys/class/net/{iface}/device")
    except OSError as e:
        return f"no sysfs device for {iface} ({e})"
    dev = usb_device(target)
    if not dev:
        return f"{iface} is not a USB device ({target}); skipped"
    code, err = _sysfs_write("/sys/bus/usb/drivers/usb/unbind", dev)
    if code != 0:
        return f"unbind {dev} failed: {err[:200]}"
    time.sleep(USB_SETTLE_S)
    code, err = _sysfs_write("/sys/bus/usb/drivers/usb/bind", dev)
    return f"rebound {dev}" if code == 0 else f"bind {dev} failed: {err[:200]}"


def rung_reboot(stamp_path):
    try:
        with open(stamp_path, "w") as f:
            f.write(f"{time.time():.0f}\n")
    except OSError as e:
        # Without the stamp the once-per-REBOOT_GAP_S promise can't hold.
        return f"not rebooting: cannot write {stamp_path} ({e})"
    code, out = _sudo("/usr/bin/systemctl", "reboot")
    return "rebooting" if code == 0 else f"reboot failed: {out[:200]}"


def _read_reboot_stamp(path):
    try:
        with open(path) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return None


def load_state(path):
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return empty_state()
    state = empty_state()
    if isinstance(raw, dict):
        state.update({k: raw[k] for k in state if k in raw})
    return state


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    dry = "--dry-run" in argv

    url = os.environ.get("MERLE_FRONTDOOR_URL", "").strip()
    host = urllib.parse.urlsplit(url).hostname if url else None
    if not host:
        raise SystemExit("netwatch: MERLE_FRONTDOOR_URL is required (station.env)")

    run_dir = os.environ.get("RUNTIME_DIRECTORY", "/run/station-netwatch")
    state_dir = os.environ.get("STATE_DIRECTORY", "/var/lib/station-netwatch")
    state_path = os.path.join(run_dir, "state.json")
    stamp_path = os.path.join(state_dir, "last-reboot")

    now = time.clock_gettime(time.CLOCK_BOOTTIME)
    iface = wifi_iface(_ip_json("-4", "addr", "show"), _ip_json("link", "show"))
    gw = default_gateway(_ip_json("-4", "route", "show", "default"), iface)
    internet_ok, net_detail = probe_internet(host)
    # The gateway is the control, only worth asking when the internet fails.
    gw_clean, gw_detail = (probe_gateway(gw) if not internet_ok or dry
                           else (True, "not probed"))

    state, event = observe(load_state(state_path), internet_ok, gw_clean, now)
    reboot_ok = reboot_allowed(_read_reboot_stamp(stamp_path), time.time())
    rung = pick_rung(state, now, reboot_ok)

    if dry:
        usb = None
        if iface:
            try:
                usb = usb_device(os.readlink(f"/sys/class/net/{iface}/device"))
            except OSError:
                pass
        print(f"netwatch (dry run): iface {iface}, usb {usb}, {host}: "
              f"{net_detail}; {gw_detail}; uptime {now / MIN:.0f} min; "
              f"would take: {RUNG_NAMES.get(rung, 'nothing')}")
        return 0

    if event == "recovered":
        print(f"netwatch: {host} reachable again; episode over")
    elif not internet_ok:
        dark_min = (now - state["dark_since"]) / MIN
        lead = "dark: " if event == "dark" else f"dark {dark_min:.0f} min: "
        print(f"netwatch: {lead}{host} {net_detail}; {gw_detail}")

    if rung:
        state = took(state, rung, now)
        save_state(state_path, state)   # before acting: a reboot ends the run
        dark_min = (now - state["dark_since"]) / MIN
        target = "" if rung == 3 else f" of {iface}"
        print(f"netwatch: rung {rung}, {RUNG_NAMES[rung]}{target}, after "
              f"{dark_min:.0f} min dark")
        if rung == 1:
            result = rung_reconnect(iface) if iface else "no Wi-Fi interface; skipped"
        elif rung == 2:
            result = rung_usb_rebind(iface) if iface else "no Wi-Fi interface; skipped"
        else:
            result = rung_reboot(stamp_path)
        print(f"netwatch: rung {rung}: {result}")
        return 0

    save_state(state_path, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
