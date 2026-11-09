# Tests for flyway/netwatch.py -- the pure logic only: the episode tracker,
# which rung (if any) the ladder takes and when, the gateway rule that keeps a
# house-internet outage at rung 1, the boot grace, the reboot gap, and the
# USB device derived from the interface's sysfs link. The probes (DNS, TCP,
# ping) and the nmcli/sysfs/systemctl calls are the boundary, verified on a
# station per issue #686's acceptance criteria.

import pytest

from flyway import netwatch as nw

MIN = 60
BOOTED = 20 * MIN          # a box well past its boot grace
ROUND = 2 * MIN            # the timer's cadence


def run(rounds, start=BOOTED, reboot_ok=True, state=None):
    """Feed (internet_ok, gateway_clean) rounds every ROUND seconds; take
    whatever rung pick_rung chooses, as main() does. Returns (state,
    [(minutes since start, rung)], [events])."""
    state = state or nw.empty_state()
    taken, events = [], []
    for i, (internet_ok, gw_clean) in enumerate(rounds):
        now = start + i * ROUND
        state, event = nw.observe(state, internet_ok, gw_clean, now)
        if event:
            events.append(event)
        rung = nw.pick_rung(state, now, reboot_ok)
        if rung:
            state = nw.took(state, rung, now)
            taken.append(((now - start) // MIN, rung))
    return state, taken, events


HALF_DEAD = (False, False)   # no internet, gateway mostly silent
ISP_DOWN = (False, True)     # no internet, gateway answers 4/4
GOOD = (True, True)


def minutes(n):
    return n * MIN // ROUND


# --- the half-dead stick: the 2026-11-08 shape ---------------------------------

def test_half_dead_stick_climbs_all_three_rungs_once_each():
    _, taken, events = run([HALF_DEAD] * minutes(90))
    assert taken == [(10, 1), (30, 2), (60, 3)]
    assert events == ["dark"]


def test_rungs_are_never_repeated_in_one_episode():
    _, taken, _ = run([HALF_DEAD] * minutes(300), reboot_ok=True)
    assert [r for _, r in taken] == [1, 2, 3]


def test_no_rung_before_ten_minutes_dark():
    _, taken, _ = run([HALF_DEAD] * minutes(9))
    assert taken == []


def test_a_rung_that_works_ends_the_episode_after_two_good_rounds():
    rounds = [HALF_DEAD] * minutes(12) + [GOOD, GOOD]
    state, taken, events = run(rounds)
    assert taken == [(10, 1)]
    assert events == ["dark", "recovered"]
    assert state["dark_since"] is None and state["rungs"] == []


def test_one_lucky_good_round_does_not_reset_the_dark_clock():
    rounds = ([HALF_DEAD] * minutes(20) + [GOOD]
              + [HALF_DEAD] * minutes(20))
    _, taken, events = run(rounds)
    assert [r for _, r in taken] == [1, 2]
    assert taken[1][0] == 30          # still measured from the first dark round
    assert events == ["dark"]


# --- the house's internet is down, the LAN is fine -------------------------------

def test_isp_outage_takes_rung_1_and_nothing_more():
    _, taken, _ = run([ISP_DOWN] * minutes(300))
    assert taken == [(10, 1)]


def test_one_clean_gateway_round_caps_the_episode_at_rung_1():
    rounds = [HALF_DEAD] * minutes(8) + [ISP_DOWN] + [HALF_DEAD] * minutes(120)
    _, taken, _ = run(rounds)
    assert [r for _, r in taken] == [1]


def test_self_healing_blip_takes_at_most_rung_1():
    # The longest self-healing blip seen: 13 minutes (2026-09-27).
    rounds = [ISP_DOWN] * minutes(13) + [GOOD] * 3
    _, taken, events = run(rounds)
    assert [r for _, r in taken] == [1]
    assert events == ["dark", "recovered"]


# --- the boot grace -------------------------------------------------------------

def test_fresh_boot_takes_no_rung_in_its_grace_period():
    # Even a state claiming a long dark streak (it can't at boot; /run is
    # empty) never acts before BOOT_GRACE_S of uptime.
    state = dict(nw.empty_state(), dark_since=0, dark_rounds=99, last_round=0)
    for now in range(0, nw.BOOT_GRACE_S, ROUND):
        assert nw.pick_rung(state, now, True) is None


def test_dark_from_boot_earns_rung_1_after_grace_and_ten_minutes():
    _, taken, _ = run([HALF_DEAD] * minutes(30), start=3 * MIN)  # OnBootSec
    assert taken[0] == (10, 1)                 # uptime 13 min


# --- cooldowns ------------------------------------------------------------------

def test_rungs_are_spaced_by_the_gap_even_when_long_overdue():
    # A state that is already 60 min dark with nothing taken (say the iface
    # was missing) still climbs one rung per RUNG_GAP_S, never all at once.
    state = dict(nw.empty_state(), dark_since=BOOTED, dark_rounds=30,
                 last_round=BOOTED + 60 * MIN)
    now = BOOTED + 62 * MIN
    assert nw.pick_rung(state, now, True) == 1
    state = nw.took(state, 1, now)
    assert nw.pick_rung(state, now + ROUND, True) is None
    assert nw.pick_rung(state, now + nw.RUNG_GAP_S, True) == 2


def test_the_last_rung_time_survives_recovery_and_a_new_episode_rearms():
    rounds = [ISP_DOWN] * minutes(12) + [GOOD, GOOD]
    state, taken, events = run(rounds)
    assert events == ["dark", "recovered"]
    assert state["rungs"] == [] and state["last_rung_at"] == BOOTED + 10 * MIN
    # A fresh episode right away still respects the gap from that rung ...
    soon = dict(state, dark_since=0, dark_rounds=40)
    assert nw.pick_rung(soon, BOOTED + 15 * MIN, True) is None
    # ... and earns its own rung 1 once it has been dark long enough.
    state, taken, _ = run([ISP_DOWN] * minutes(12),
                          start=state["last_round"] + ROUND, state=state)
    assert [r for _, r in taken] == [1]


def test_reboot_waits_when_the_last_one_was_recent():
    _, taken, _ = run([HALF_DEAD] * minutes(180), reboot_ok=False)
    assert [r for _, r in taken] == [1, 2]


@pytest.mark.parametrize("last, now, ok", [
    (None, 1_000_000, True),
    (1_000_000, 1_000_000 + nw.REBOOT_GAP_S, True),
    (1_000_000, 1_000_000 + nw.REBOOT_GAP_S - 1, False),
    (1_000_000, 1_000_000 + 60, False),
    (1_000_000 + 600, 1_000_000, False),               # clock stepped back
    (1_000_000 + nw.REBOOT_GAP_S, 1_000_000, True),    # far-future stamp
])
def test_reboot_allowed(last, now, ok):
    assert nw.reboot_allowed(last, now) is ok


# --- stale state ----------------------------------------------------------------

def test_a_long_gap_between_rounds_starts_the_episode_over():
    state, _, _ = run([HALF_DEAD] * minutes(50))
    later = state["last_round"] + nw.STALE_AFTER_S + 1
    state, event = nw.observe(state, False, False, later)
    assert event == "dark" and state["dark_since"] == later
    assert nw.pick_rung(state, later, True) is None


# --- interface, gateway, USB device ----------------------------------------------

def link(ifname, *addrs, operstate="UP"):
    return {"ifname": ifname, "operstate": operstate,
            "addr_info": [{"family": "inet", "local": a, "scope": "global"}
                          for a in addrs]}


def test_wifi_iface_is_pick_lan_addrs_choice():
    addr_links = [link("lo"), link("wlx00c0cabcf4d1", "192.168.12.140"),
                  link("tailscale0", "100.92.248.83")]
    assert nw.wifi_iface(addr_links, []) == "wlx00c0cabcf4d1"


def test_wifi_iface_falls_back_to_a_stick_without_a_lease():
    addr_links = [link("enp1s0", "192.168.1.208")]           # rescue cable
    all_links = [{"ifname": "lo"}, {"ifname": "enp1s0"},
                 {"ifname": "p2p-dev-wlx00c0cabcf4d1"},
                 {"ifname": "wlx00c0cabcf4d1"}]
    assert nw.wifi_iface(addr_links, all_links) == "wlx00c0cabcf4d1"


def test_no_wifi_iface_at_all():
    assert nw.wifi_iface([link("enp1s0", "192.168.1.208")],
                         [{"ifname": "enp1s0"}]) is None


def test_default_gateway_prefers_the_wifi_route():
    routes = [{"dst": "default", "gateway": "192.168.1.1", "dev": "enp1s0"},
              {"dst": "default", "gateway": "192.168.12.1",
               "dev": "wlx00c0cabcf4d1"}]
    assert nw.default_gateway(routes, "wlx00c0cabcf4d1") == "192.168.12.1"
    assert nw.default_gateway(routes[:1], "wlx00c0cabcf4d1") == "192.168.1.1"
    assert nw.default_gateway([], "wlx00c0cabcf4d1") is None


@pytest.mark.parametrize("target, dev", [
    ("../../../1-1.1:1.0", "1-1.1"),                                 # kestrel's link
    ("/sys/devices/pci0000:00/0000:00:15.0/usb1/1-1/1-1.1/1-1.1:1.0", "1-1.1"),
    ("../../../2-3:1.0", "2-3"),
    ("../../../1-1.4.2:1.1", "1-1.4.2"),                             # behind two hubs
    ("../../../1-1.1", "1-1.1"),                                     # a device itself
    ("../../../0000:01:00.0", None),                                 # onboard PCI NIC
    ("../../../usb1", None),                                         # a root hub
    ("", None),
    (None, None),
])
def test_usb_device_from_the_sysfs_link(target, dev):
    assert nw.usb_device(target) == dev


@pytest.mark.parametrize("out, clean", [
    ("4 packets transmitted, 4 received, 0% packet loss, time 1502ms", True),
    ("4 packets transmitted, 1 received, 75% packet loss, time 1503ms", False),
    ("4 packets transmitted, 0 received, +4 errors, 100% packet loss", False),
    ("connect: Network is unreachable", False),
    ("", False),
])
def test_ping_clean(out, clean):
    assert nw.ping_clean(out) is clean
