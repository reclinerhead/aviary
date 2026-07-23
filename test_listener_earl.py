# =============================================================================
# project-squirrel -- test_listener_earl.py
#
# The capture read's three answers (issue #201). Earl only ever recovered
# from a capture that EXITED; a capture that stalls without exiting hung its
# source forever, silently -- an Amcrest repositioned mid-session leaves a
# socket that stays ESTAB with nothing on it, and a rover powered down to
# charge leaves a half-open ssh. So read_exact grew a third answer between
# "here is your window" and "the pipe closed": STALLED, meaning the pipe is
# open and nothing is coming.
#
# Real os.pipe() fds, no hardware and no clock injection -- the timeouts here
# are small on purpose so the suite stays fast while still exercising the
# genuine select path. earl.py's other pure helpers live in
# test_listener_sightings.py; the worker's reaction to STALLED (kill, offline,
# backoff) is I/O orchestration, desk-tested on pearl like every capture loop.
# =============================================================================

import os
import threading
import time

import pytest

import feeds
from listener import earl


@pytest.fixture
def pipe():
    """A read/write fd pair, closed however the test leaves them.

    Skipped on Windows, and the skip lives HERE so every future pipe test
    inherits it without remembering to: `select.select` accepts only sockets
    on Windows, so these raise WinError on bluejay while passing on pearl and
    in CI (both Linux). That is the right trade rather than reshaping the
    production read around a platform Earl never runs on -- he is a pearl
    daemon, and the fd he actually reads is always a POSIX pipe."""
    if os.name == "nt":
        pytest.skip("select() on pipes is POSIX-only; Earl runs on pearl")
    r, w = os.pipe()
    reader = os.fdopen(r, "rb", buffering=0)
    yield reader, w
    reader.close()
    try:
        os.close(w)
    except OSError:
        pass        # the test closed the writer itself (the EOF cases)


# --- the happy path -----------------------------------------------------------

def test_returns_exactly_n_bytes_and_leaves_the_rest(pipe):
    reader, w = pipe
    os.write(w, b"abcdefghij")
    assert earl.read_exact(reader, 4, timeout=5) == b"abcd"
    assert earl.read_exact(reader, 6, timeout=5) == b"efghij"


def test_reassembles_a_window_from_dribbled_chunks(pipe):
    """A pipe hands over what it has, not what was asked for -- the loop has
    to keep reading. This is the normal case for a 288 kB audio window."""
    reader, w = pipe

    def dribble():
        for part in (b"aa", b"bb", b"cc"):
            time.sleep(0.02)
            os.write(w, part)

    t = threading.Thread(target=dribble)
    t.start()
    assert earl.read_exact(reader, 6, timeout=5) == b"aabbcc"
    t.join()


def test_no_timeout_still_blocks_the_old_way(pipe):
    """timeout=None is the pre-#201 contract, and stays the default: callers
    that never want a stall verdict get the original blocking read."""
    reader, w = pipe
    os.write(w, b"xyz")
    assert earl.read_exact(reader, 3) == b"xyz"


# --- EOF, which must stay distinguishable from a stall ------------------------

def test_closed_writer_is_eof_not_a_stall(pipe):
    reader, w = pipe
    os.close(w)
    assert earl.read_exact(reader, 4, timeout=5) is None


def test_partial_window_then_eof_is_eof(pipe):
    """A capture dying mid-window is still a clean exit -- the restart path
    for it already existed and must not be rerouted through the stall log."""
    reader, w = pipe
    os.write(w, b"ab")
    os.close(w)
    assert earl.read_exact(reader, 4, timeout=5) is None


# --- the stall, the reason this file exists -----------------------------------

def test_silent_open_pipe_stalls(pipe):
    """The 2026-07-19 failure: writer alive, holding the pipe open, sending
    nothing. Before #201 this blocked forever."""
    reader, _w = pipe
    started = time.monotonic()
    assert earl.read_exact(reader, 4, timeout=0.15) is earl.STALLED
    assert time.monotonic() - started >= 0.15   # it waited, not gave up early


def test_stall_verdict_is_not_confusable_with_eof(pipe):
    """`is STALLED` vs `is None` is the caller's whole branch -- a sentinel
    that compared equal to None or to b"" would silently take the EOF path."""
    reader, _w = pipe
    assert earl.read_exact(reader, 4, timeout=0.05) is not None
    assert earl.STALLED is not None and earl.STALLED != b""


def test_a_window_half_delivered_then_silence_stalls(pipe):
    """The nastier shape: bytes arrive, then the device vanishes mid-window.
    The partial read must not buy the source unlimited patience."""
    reader, w = pipe
    os.write(w, b"ab")
    assert earl.read_exact(reader, 4, timeout=0.15) is earl.STALLED


def test_slow_but_alive_is_never_called_stalled(pipe):
    """The false-trip guard, and the reason the clock restarts per chunk: a
    source dribbling slower than the timeout still never goes quiet FOR the
    timeout, so it keeps its window."""
    reader, w = pipe

    def slow():
        for part in (b"a", b"b", b"c", b"d"):
            time.sleep(0.06)        # each gap under the 0.15 bar
            os.write(w, part)

    t = threading.Thread(target=slow)
    t.start()
    # Total delivery (~0.24s) exceeds the timeout; no single gap does.
    assert earl.read_exact(reader, 4, timeout=0.15) == b"abcd"
    t.join()


# --- the capture commands carry their own timeouts ----------------------------

def test_rtsp_argv_carries_the_socket_timeout():
    """Layer 1: without this, a camera that goes away without a FIN leaves
    ffmpeg blocked on a socket that stays ESTAB. It is -timeout (the rtsp
    demuxer's, in microseconds), NOT -rw_timeout, which that demuxer has no
    such option for -- verified against pearl's ffmpeg 8.0.1."""
    argv, _redacted = earl.rtsp_argv("rtsp://pearl:8554/house-rear")
    assert "-timeout" in argv
    assert argv[argv.index("-timeout") + 1] == str(earl.SOCKET_TIMEOUT_US)
    assert argv.index("-timeout") < argv.index("-i")    # an INPUT option
    assert "-rw_timeout" not in argv


def test_socket_timeout_matches_the_worker_bar():
    """One number, two units: ffmpeg's microseconds and the watchdog's
    seconds must not drift apart into two different definitions of stalled."""
    assert earl.SOCKET_TIMEOUT_US == earl.STALL_TIMEOUT_S * 1_000_000


def test_rtsp_argv_redacts_a_credentialed_url():
    """The break-glass registry (MERLE_FEEDS with a direct camera URL) is
    the one place credentials can reach this argv -- the redacted twin,
    which is what every log line carries, must mask them."""
    argv, redacted = earl.rtsp_argv("rtsp://admin:sekrit@192.168.1.102:554/x")
    assert "sekrit" in " ".join(argv)       # ffmpeg needs the real thing
    assert "sekrit" not in redacted
    assert "admin:***@" in redacted


# --- source_commands: registry -> capture, dispatched on kind (issue #270) ----

def test_source_commands_dispatches_on_kind_never_name(monkeypatch, tmp_path):
    """The point of #270: which sources exist is feeds.yml's business; Earl
    only turns a KIND into a capture. Feed names here are deliberately ones
    Earl has never heard of."""
    registry = tmp_path / "feeds.yml"
    registry.write_text(
        "feeds:\n"
        "  gazebo:\n"
        "    kind: rtsp\n"
        "    url: rtsp://pearl:8554/gazebo\n"
        "    earl: true\n"
        "  birdbath-mic:\n"
        "    kind: command\n"
        "    cmd: arecord -D plughw:1,0 -t raw -q\n"
        "    earl: true\n"
        "  spare-cam:\n"
        "    kind: rtsp\n"
        "    url: rtsp://pearl:8554/spare-cam\n"
        "    earl: false\n")
    monkeypatch.setenv("MERLE_FEEDS", str(registry))
    commands = earl.source_commands()
    assert set(commands) == {"gazebo", "birdbath-mic"}   # earl: false excluded
    gazebo_argv, _ = commands["gazebo"]
    assert gazebo_argv[0] == "ffmpeg"
    assert "rtsp://pearl:8554/gazebo" in gazebo_argv
    assert "-allowed_media_types" in gazebo_argv          # audio-only pull
    mic_argv, _ = commands["birdbath-mic"]
    assert mic_argv == ["arecord", "-D", "plughw:1,0", "-t", "raw", "-q"]


def test_source_commands_shipped_registry_runs_the_house(monkeypatch):
    """The default path is the repo's own feeds.yml -- both cameras and the
    rover, every rtsp pull carrying the stall bar (issue #201's layer 1)."""
    monkeypatch.delenv("MERLE_FEEDS", raising=False)
    commands = earl.source_commands()
    assert set(commands) == {"house-rear", "house-front", "rover"}
    for name in ("house-rear", "house-front"):
        argv, _ = commands[name]
        assert "-timeout" in argv
    rover_argv, _ = commands["rover"]
    assert "ServerAliveInterval=5" in rover_argv          # #201's layer 2
    assert "ServerAliveCountMax=3" in rover_argv


def test_source_commands_empty_roster_fails_loud(monkeypatch, tmp_path):
    """An Earl with nothing to listen to is a misconfiguration, not a
    healthy-looking daemon that never publishes."""
    registry = tmp_path / "feeds.yml"
    registry.write_text(
        "feeds:\n"
        "  cam:\n"
        "    kind: rtsp\n"
        "    url: rtsp://pearl:8554/cam\n"
        "    earl: false\n")
    monkeypatch.setenv("MERLE_FEEDS", str(registry))
    with pytest.raises(RuntimeError, match="nothing to listen to"):
        earl.source_commands()


def test_source_commands_refuses_a_kind_it_cannot_capture(monkeypatch):
    """feeds.py may learn kinds Earl can't capture (the rover's MJPEG video,
    #236) -- those must fail at startup with a name, never run as a
    mystery source."""
    fake = feeds.Feed(name="rover-video", kind="mjpeg",
                      url="http://merle:5000/video_feed", earl=True)
    monkeypatch.setattr(earl.feeds, "feeds_for", lambda consumer: [fake])
    with pytest.raises(RuntimeError, match="no capture for"):
        earl.source_commands()
