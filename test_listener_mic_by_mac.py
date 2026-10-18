# =============================================================================
# project-squirrel -- test_listener_mic_by_mac.py
#
# The MAC lookup behind kestrel's rear Warbler32 (issue #580). Two pure
# pieces: pick_ip(), which reads `ip neigh` output, and sweep_targets(), which
# decides what the ping sweep covers. A wrong pick is silent in the worst
# way -- ffmpeg pulls from some other box, or a stale lease, and the source
# just reads `offline` a hundred miles away -- so the table's edge cases are
# pinned here. The sweep itself and the exec into ffmpeg are I/O, desk-
# checked on orchid.
#
# main()'s exits around the exec are pinned too, after kestrel's install day
# (2026-10-18): stations had no ffmpeg, every exec died on FileNotFoundError,
# and Earl's discarded stderr left the journal saying only `rc 1`. syslog is
# faked through sys.modules (it is POSIX-only and imported inside main), the
# neighbor table is canned, and os.execvp is replaced, so nothing here
# touches the network or replaces the test process.
# =============================================================================

import sys

import pytest

from listener import earl, mic_by_mac

MAC = "90:70:69:1a:1c:a8"

# Shaped like real `ip neigh` output: a router flag before the state, an
# IPv6 link-local for the same board, and the states the kernel prints.
TABLE = """\
192.168.12.1 dev wlx00c0cabcf4d1 lladdr 18:a5:ff:d4:59:a6 router REACHABLE
192.168.12.77 dev wlx00c0cabcf4d1 lladdr 90:70:69:1a:1c:a8 REACHABLE
192.168.12.120 dev wlx00c0cabcf4d1 lladdr 3c:22:fb:01:02:03 STALE
fe80::9270:69ff:fe1a:1ca8 dev wlx00c0cabcf4d1 lladdr 90:70:69:1a:1c:a8 STALE
"""


def test_hit():
    assert mic_by_mac.pick_ip(TABLE, MAC) == "192.168.12.77"


def test_miss():
    assert mic_by_mac.pick_ip(TABLE, "aa:bb:cc:dd:ee:ff") is None
    assert mic_by_mac.pick_ip("", MAC) is None


def test_mac_case_does_not_matter():
    # The registry might carry it either way, and so might the table.
    assert mic_by_mac.pick_ip(TABLE, MAC.upper()) == "192.168.12.77"
    shouty = TABLE.replace(MAC, MAC.upper())
    assert mic_by_mac.pick_ip(shouty, MAC) == "192.168.12.77"


def test_failed_and_incomplete_are_never_picked():
    table = ("192.168.12.77 dev wlan0 lladdr 90:70:69:1a:1c:a8 FAILED\n"
             "192.168.12.78 dev wlan0 INCOMPLETE\n"
             "192.168.12.79 dev wlan0 lladdr 90:70:69:1a:1c:a8 INCOMPLETE\n")
    assert mic_by_mac.pick_ip(table, MAC) is None


def test_fresh_beats_stale_whichever_comes_first():
    # After a lease change the old address lingers STALE beside the new one.
    # Table order is the kernel's hash order, so the stale line may lead.
    table = ("192.168.12.40 dev wlan0 lladdr 90:70:69:1a:1c:a8 STALE\n"
             "192.168.12.77 dev wlan0 lladdr 90:70:69:1a:1c:a8 REACHABLE\n")
    assert mic_by_mac.pick_ip(table, MAC) == "192.168.12.77"
    for state in ("DELAY", "PROBE"):
        swapped = table.replace("REACHABLE", state)
        assert mic_by_mac.pick_ip(swapped, MAC) == "192.168.12.77"


def test_stale_alone_is_used_unless_refused():
    # main() first asks with allow_stale=False: a stale-only hit sends it to
    # the sweep (which refreshes the truth) before settling for STALE.
    table = "192.168.12.40 dev wlan0 lladdr 90:70:69:1a:1c:a8 STALE\n"
    assert mic_by_mac.pick_ip(table, MAC) == "192.168.12.40"
    assert mic_by_mac.pick_ip(table, MAC, allow_stale=False) is None


def test_ipv6_entries_are_skipped():
    table = "fe80::9270:69ff:fe1a:1ca8 dev wlan0 lladdr 90:70:69:1a:1c:a8 REACHABLE\n"
    assert mic_by_mac.pick_ip(table, MAC) is None


def test_malformed_lines_are_skipped():
    table = ("\n"
             "garbage\n"
             "192.168.12.5 dev wlan0 lladdr\n"
             "not-an-ip dev wlan0 lladdr 90:70:69:1a:1c:a8 REACHABLE\n"
             "192.168.12.77 dev wlan0 lladdr 90:70:69:1a:1c:a8 REACHABLE\n")
    assert mic_by_mac.pick_ip(table, MAC) == "192.168.12.77"


def test_sweep_covers_the_slash_24_minus_this_box():
    targets = mic_by_mac.sweep_targets("192.168.12.50/24")
    assert len(targets) == 253
    assert "192.168.12.50" not in targets
    assert targets[0] == "192.168.12.1" and targets[-1] == "192.168.12.254"


def test_a_wider_prefix_is_narrowed_to_the_slash_24():
    targets = mic_by_mac.sweep_targets("10.0.5.9/16")
    assert len(targets) == 253
    assert all(t.startswith("10.0.5.") for t in targets)


# --- main()'s exits around the exec -------------------------------------------

class FakeSyslog:
    """Stands in for the POSIX-only syslog module; keeps (priority, line)."""
    LOG_PERROR, LOG_ERR, LOG_WARNING, LOG_INFO = 0x20, 3, 4, 6

    def __init__(self):
        self.lines = []

    def openlog(self, *args, **kwargs):
        pass

    def syslog(self, *args):
        priority = args[0] if len(args) == 2 else self.LOG_INFO
        self.lines.append((priority, args[-1]))


@pytest.fixture
def wired(monkeypatch):
    """main() on a box whose neighbor table already holds the mic (TABLE:
    192.168.12.77, REACHABLE), with exec recorded instead of performed."""
    log = FakeSyslog()
    monkeypatch.setitem(sys.modules, "syslog", log)
    monkeypatch.setattr(mic_by_mac, "_run", lambda argv: TABLE)
    execs = []
    monkeypatch.setattr(mic_by_mac.os, "execvp",
                        lambda file, argv: execs.append(argv))
    return log, execs


def test_found_mic_execs_earls_rtsp_argv(wired, monkeypatch):
    log, execs = wired
    monkeypatch.setattr(mic_by_mac.shutil, "which", lambda f: "/usr/bin/" + f)
    mic_by_mac.main([MAC])
    assert execs == [earl.rtsp_argv("rtsp://192.168.12.77/audio")[0]]
    assert any("at 192.168.12.77; exec ffmpeg" in line for _, line in log.lines)


def test_missing_ffmpeg_is_a_named_error_not_an_exec(wired, monkeypatch):
    # The install-day bug: say what's missing, through the channel that
    # reaches the journal, and never attempt the exec.
    log, execs = wired
    monkeypatch.setattr(mic_by_mac.shutil, "which", lambda f: None)
    assert mic_by_mac.main([MAC]) == 1
    assert execs == []
    errors = [line for priority, line in log.lines
              if priority == FakeSyslog.LOG_ERR]
    assert len(errors) == 1
    assert "ffmpeg is not installed" in errors[0]
    assert "apt-get install" in errors[0]


def test_exec_failure_is_logged_and_returns_1(wired, monkeypatch):
    # which() can say yes and the exec still fail (permissions, a broken
    # binary): that is a logged exit too, never a bare traceback.
    log, _ = wired
    monkeypatch.setattr(mic_by_mac.shutil, "which", lambda f: "/usr/bin/" + f)

    def refuse(file, argv):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(mic_by_mac.os, "execvp", refuse)
    assert mic_by_mac.main([MAC]) == 1
    assert any(priority == FakeSyslog.LOG_ERR and "exec ffmpeg failed" in line
               for priority, line in log.lines)
