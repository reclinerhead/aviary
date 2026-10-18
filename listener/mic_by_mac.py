# =============================================================================
# project-squirrel -- listener/mic_by_mac.py
#
# A network mic found by its MAC, not its address (issue #580). The Warbler32
# at Kathy's announces nothing (no mDNS in its firmware, and `.local` failed
# at both station houses anyway), her router has no login for a DHCP
# reservation, and the LAN is 100 miles from a keyboard. What follows the
# board onto any network is its MAC -- so every time Earl (re)starts the
# capture, this looks the MAC up in the kernel's neighbor table, ping-sweeps
# the LAN's /24 when the table has nothing fresh, and then BECOMES the ffmpeg
# pull of rtsp://<ip>/audio via exec.
#
#   python -m listener.mic_by_mac 90:70:69:1a:1c:a8
#
# It is a feeds registry `kind: command` cmd (feeds.kestrel.yml), not a new
# capture kind: to Earl it is just a command that writes s16le/48kHz/mono to
# stdout. The argv is earl.rtsp_argv() verbatim, so the #201 socket -timeout
# can never drift between the two capture paths. And because the worker's
# restart backoff (3/10/30/60 s, then 60 s forever) re-runs the cmd, a new
# DHCP lease costs at most about a minute of `offline`; while the mic is
# unplugged the cost is one ~2 s sweep a minute.
#
# Logging goes to syslog with LOG_PERROR: Earl's worker sends a command's
# stderr to /dev/null, so a stderr-only "not found" would never reach the
# journal. journald files syslog lines under the sender's unit, so
# `journalctl -u earl-listener` shows which address the mic was found at, or
# that it wasn't; a hand run prints the same lines to the terminal.
#
# The capture binary is ffmpeg, which a station only has if someone installed
# it (window-mic is arecord): a missing one is a named LOG_ERR line, not a
# silent `rc 1`.
#
# Pure logic -- the neighbor pick and the sweep's targets -- is under
# test_listener_mic_by_mac.py, along with main()'s exits around the exec.
# The sweep and the exec itself are I/O at the boundary, desk-checked on
# orchid (#580 step 1).
# =============================================================================

import ipaddress
import os
import shutil
import subprocess
import sys
import time

# Neighbor states that mean "answered recently". STALE is usable but second
# best: after a lease change the old address can linger STALE beside the new
# one. FAILED/INCOMPLETE (and NONE) are never picked.
FRESH_STATES = {"REACHABLE", "DELAY", "PROBE", "PERMANENT", "NOARP"}
STALE_STATE = "STALE"

SWEEP_DEADLINE_S = 3   # ping -W1 plus process start-up for ~254 pings
RTSP_PATH = "/audio"   # the Warbler32 firmware's stream path


def pick_ip(neigh_text, mac, allow_stale=True):
    """`ip neigh` output -> the IPv4 address holding `mac`, or None.

    Case-insensitive on the MAC. A fresh entry beats a STALE one for the same
    MAC wherever it sits in the table; a STALE one is returned only when
    `allow_stale` and nothing fresher exists. IPv6 entries are skipped (the
    board may hold a link-local address too, and rtsp://fe80::.../audio is
    not a URL ffmpeg can use), as are malformed lines."""
    mac = mac.strip().lower()
    stale = None
    for line in neigh_text.splitlines():
        fields = line.split()
        if "lladdr" not in fields:
            continue
        at = fields.index("lladdr")
        if at + 1 >= len(fields) or fields[at + 1].lower() != mac:
            continue
        try:
            addr = ipaddress.ip_address(fields[0])
        except ValueError:
            continue
        if addr.version != 4:
            continue
        state = fields[-1].upper()
        if state in FRESH_STATES:
            return fields[0]
        if state == STALE_STATE and stale is None:
            stale = fields[0]
    return stale if allow_stale else None


def sweep_targets(iface_cidr):
    """This box's LAN address ("192.168.12.50/24") -> every other host on its
    /24, as strings. A wider prefix is narrowed to the /24 around the address:
    a /16 sweep would be 65k pings a minute while the mic is unplugged, and
    every LAN this runs on is a home /24."""
    iface = ipaddress.ip_interface(iface_cidr)
    net = iface.network
    if net.prefixlen < 24:
        net = ipaddress.ip_network(f"{iface.ip}/24", strict=False)
    return [str(host) for host in net.hosts() if host != iface.ip]


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True,
                          check=False).stdout


def lan_cidr():
    """The address/prefix on the default route's interface -- the LAN, never
    tailscale0 (no default route there). None when the box has no LAN."""
    route = _run(["ip", "-4", "-o", "route", "show", "default"]).split()
    if "dev" not in route or route.index("dev") + 1 >= len(route):
        return None
    dev = route[route.index("dev") + 1]
    addr = _run(["ip", "-4", "-o", "addr", "show", "dev", dev]).split()
    if "inet" not in addr or addr.index("inet") + 1 >= len(addr):
        return None
    return addr[addr.index("inet") + 1]


def sweep(targets):
    """One parallel ping per target. The replies don't matter: resolving each
    address fills the kernel's neighbor table as a side effect, which is the
    whole point (find-warbler.sh's trick)."""
    procs = [subprocess.Popen(["ping", "-c1", "-W1", ip],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
             for ip in targets]
    deadline = time.monotonic() + SWEEP_DEADLINE_S
    for proc in procs:
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def main(args=None):
    import syslog   # POSIX-only; kept out of import so the tests run anywhere
    args = sys.argv[1:] if args is None else args
    syslog.openlog("mic_by_mac", syslog.LOG_PERROR)
    if len(args) != 1:
        syslog.syslog(syslog.LOG_ERR,
                      "usage: python -m listener.mic_by_mac <MAC>")
        return 2
    mac = args[0]

    ip = pick_ip(_run(["ip", "-4", "neigh", "show"]), mac, allow_stale=False)
    if ip is None:
        cidr = lan_cidr()
        if cidr is None:
            syslog.syslog(syslog.LOG_WARNING,
                          f"{mac}: no LAN address on the default route; "
                          "nothing to sweep")
            return 1
        sweep(sweep_targets(cidr))
        ip = pick_ip(_run(["ip", "-4", "neigh", "show"]), mac)
        if ip is None:
            syslog.syslog(syslog.LOG_WARNING,
                          f"{mac} not on {cidr} after a sweep -- mic "
                          "unplugged, off Wi-Fi, or on another network")
            return 1

    from listener import earl   # lazy: the sweep path never needs paho
    argv, redacted = earl.rtsp_argv(f"rtsp://{ip}{RTSP_PATH}")
    # Stations never needed ffmpeg (window-mic is arecord), and kestrel's
    # install day found every exec dying on FileNotFoundError -- a traceback
    # on the stderr Earl discards, so the journal showed only `rc 1`. Say
    # what's missing, through the channel that reaches the journal.
    if shutil.which(argv[0]) is None:
        syslog.syslog(syslog.LOG_ERR,
                      f"{mac} at {ip}, but {argv[0]} is not installed -- "
                      f"sudo apt-get install --no-install-recommends "
                      f"{argv[0]}")
        return 1
    syslog.syslog(f"{mac} at {ip}; exec {redacted}")
    try:
        os.execvp(argv[0], argv)
    except OSError as e:
        syslog.syslog(syslog.LOG_ERR, f"{mac} at {ip}: exec {argv[0]} "
                                      f"failed ({e})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
