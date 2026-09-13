# Tests for flyway/checkin.py -- the pure logic only: which address a station
# reports to the front door, and the payload it sends. The POST, the timer and
# the NetworkManager dispatcher hook are verified on a real station against
# issue #545's acceptance criteria.

from datetime import datetime, timezone

import pytest

from flyway import checkin


def link(ifname, *addrs, operstate="UP", scope="global"):
    return {
        "ifname": ifname,
        "operstate": operstate,
        "addr_info": [{"family": "inet", "local": a, "scope": scope} for a in addrs],
    }


# --- is_private_ipv4 -----------------------------------------------------------

@pytest.mark.parametrize("ip", ["10.0.0.1", "172.16.0.1", "172.31.255.254", "192.168.1.47"])
def test_private_ranges_accepted(ip):
    assert checkin.is_private_ipv4(ip)


@pytest.mark.parametrize("ip", [
    "169.254.7.7",     # link-local
    "100.92.248.83",   # tailnet (100.64/10)
    "172.32.0.1",      # just outside 172.16/12
    "8.8.8.8",
    "127.0.0.1",
    "fd00::1",
    "not-an-ip",
    "",
])
def test_everything_else_rejected(ip):
    assert not checkin.is_private_ipv4(ip)


# --- pick_lan_addr -------------------------------------------------------------

def test_wifi_wins_over_ethernet_even_when_listed_second():
    links = [
        link("lo", "127.0.0.1", scope="host"),
        link("enp1s0", "192.168.1.208"),
        link("wlx00c0cabcf4d1", "192.168.1.243"),
        link("tailscale0", "100.92.248.83"),
    ]
    assert checkin.pick_lan_addr(links) == ("192.168.1.243", "wlx00c0cabcf4d1")


def test_ethernet_is_the_fallback_when_wifi_has_no_lease():
    links = [
        link("enp1s0", "192.168.1.208"),
        link("wlx00c0cabcf4d1"),           # up, no address yet
        link("tailscale0", "100.92.248.83"),
    ]
    assert checkin.pick_lan_addr(links) == ("192.168.1.208", "enp1s0")


def test_tailnet_and_link_local_never_reported():
    links = [
        link("wlx00c0cabcf4d1", "169.254.7.7"),
        link("tailscale0", "100.92.248.83"),
    ]
    assert checkin.pick_lan_addr(links) is None


def test_down_interfaces_and_loopback_skipped():
    links = [
        link("lo", "127.0.0.1", scope="host"),
        link("enp1s0", "192.168.1.208", operstate="DOWN"),
    ]
    assert checkin.pick_lan_addr(links) is None


def test_non_global_scope_skipped():
    assert checkin.pick_lan_addr([link("wlan0", "192.168.1.5", scope="link")]) is None


def test_empty_and_missing_fields_survive():
    assert checkin.pick_lan_addr([]) is None
    assert checkin.pick_lan_addr(None) is None
    assert checkin.pick_lan_addr([{"ifname": "wlan0"}, {}]) is None


def test_first_wifi_address_wins_when_several():
    links = [link("wlan0", "192.168.1.5", "192.168.1.6"), link("wlan1", "10.0.0.9")]
    assert checkin.pick_lan_addr(links) == ("192.168.1.5", "wlan0")


# --- build_payload -------------------------------------------------------------

def test_payload_has_exactly_the_documented_keys():
    now = datetime(2026, 9, 13, 12, 0, 0, 123456, tzinfo=timezone.utc)
    p = checkin.build_payload("kestrel", "192.168.4.37", "wlx00c0cabcf4d1", "abc1234", now=now)
    assert p == {
        "station": "kestrel",
        "ip": "192.168.4.37",
        "iface": "wlx00c0cabcf4d1",
        "sha": "abc1234",
        "ts": "2026-09-13T12:00:00Z",
    }


def test_payload_omits_sha_when_git_is_unavailable():
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    p = checkin.build_payload("birdhouse", "192.168.1.47", "wlan0", None, now=now)
    assert "sha" not in p
    assert set(p) == {"station", "ip", "iface", "ts"}
