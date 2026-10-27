# Tests for ollama_client.py -- the pure logic only (endpoint config, the
# candidate list, the transition-logged unreachable host, the explicit
# context window). The HTTP call itself is I/O and stays desk-tested live,
# per the testing policy; urlopen is the one thing faked here. Moved from
# test_narrator.py by #628, along with the client.

import io
import json
import urllib.error

import ollama_client


# --- endpoint config ---------------------------------------------------------

def test_ollama_address_unset_means_tier_off(monkeypatch):
    monkeypatch.delenv("MERLE_OLLAMA", raising=False)
    assert ollama_client.ollama_address() is None


def test_ollama_address_default_port(monkeypatch):
    monkeypatch.setenv("MERLE_OLLAMA", "192.168.1.79")
    assert ollama_client.ollama_address() == ("192.168.1.79", 11434)


def test_ollama_address_explicit_port(monkeypatch):
    monkeypatch.setenv("MERLE_OLLAMA", "192.168.1.79:8080")
    assert ollama_client.ollama_address() == ("192.168.1.79", 8080)


def ollama_raising(monkeypatch, exc):
    """An Ollama whose HTTP call always fails with `exc` -- the wire is the
    one thing not unit-tested here, so it's the one thing faked."""
    client = ollama_client.Ollama("nowhere", 11434, "m")

    def boom(*a, **kw):
        raise exc

    monkeypatch.setattr(ollama_client.urllib.request, "urlopen", boom)
    return client


def test_an_unreachable_host_is_logged_once_not_every_call(monkeypatch, capsys):
    """#335: bluejay being off is documented steady state, and Willard
    retries every ~90s -- measured at 41 error lines an hour for a service
    that is working fine. One line when it goes, silence after."""
    client = ollama_raising(monkeypatch, urllib.error.URLError("refused"))
    for _ in range(5):
        assert client.complete("sys", "prompt") is None
    out = capsys.readouterr().out
    assert out.count("unreachable") == 1
    assert "quiet until it returns" in out


def test_the_host_coming_back_says_so(monkeypatch, capsys):
    client = ollama_raising(monkeypatch, urllib.error.URLError("refused"))
    client.complete("sys", "prompt")
    capsys.readouterr()

    class Resp:
        def read(self):
            return b'{"response": "prose"}'

        def __enter__(self):
            return io.StringIO('{"response": "prose"}')

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ollama_client.urllib.request, "urlopen",
                        lambda *a, **kw: Resp())
    assert client.complete("sys", "prompt") == "prose"
    assert "is back" in capsys.readouterr().out


def test_a_server_that_answers_badly_stays_loud(monkeypatch, capsys):
    """Silencing an expected absence must not become swallowing errors: a
    host that answered with a 500 is a real anomaly, every time."""
    err = urllib.error.HTTPError("http://x", 500, "boom", {}, None)
    client = ollama_raising(monkeypatch, err)
    for _ in range(3):
        client.complete("sys", "prompt")
    assert capsys.readouterr().out.count("HTTPError") == 3


def test_every_call_sends_an_explicit_context_window(monkeypatch):
    """#493: bluejay's Ollama loads a model at its MAXIMUM context when the
    caller names none (131k on gemma3), and the KV cache for that window
    spills the weights to CPU. This is the one Ollama client in the codebase,
    so the payload -- not a caller -- carries num_ctx, beside the knobs each
    caller does set."""
    client = ollama_client.Ollama("nowhere", 11434, "m")
    sent = {}

    class Resp:
        def __enter__(self):
            return io.StringIO('{"response": "prose"}')

        def __exit__(self, *a):
            return False

    def capture(req, timeout=None):
        sent.update(json.loads(req.data.decode("utf-8")))
        return Resp()

    monkeypatch.setattr(ollama_client.urllib.request, "urlopen", capture)
    assert client.complete("sys", "prompt", num_predict=7, temperature=0.2) == "prose"
    assert sent["options"] == {"num_predict": 7, "temperature": 0.2,
                               "num_ctx": ollama_client.OLLAMA_NUM_CTX}
    # Sane, and not the model max: the number is the whole point.
    assert 2048 <= ollama_client.OLLAMA_NUM_CTX <= 16384



# --- the ollama candidate list (issue #217) -----------------------------------

def test_ollama_candidates_back_compat_with_a_single_host(monkeypatch):
    # Today's value, byte for byte -- one entry, port default applied.
    monkeypatch.setenv("MERLE_OLLAMA", "192.168.1.79:11434")
    assert ollama_client.ollama_candidates() == [("192.168.1.79", 11434)]
    monkeypatch.setenv("MERLE_OLLAMA", "bluejay")
    assert ollama_client.ollama_candidates() == [("bluejay", 11434)]


def test_ollama_candidates_preserve_preference_order(monkeypatch):
    monkeypatch.setenv("MERLE_OLLAMA", " basement:11434 , 192.168.1.79 ,")
    assert ollama_client.ollama_candidates() == [("basement", 11434),
                                            ("192.168.1.79", 11434)]


def test_ollama_candidates_unset_is_the_kill_switch(monkeypatch):
    monkeypatch.delenv("MERLE_OLLAMA", raising=False)
    assert ollama_client.ollama_candidates() == []
    assert ollama_client.ollama_address() is None
    monkeypatch.setenv("MERLE_OLLAMA", "  ")
    assert ollama_client.ollama_candidates() == []


def test_ollama_address_is_the_first_candidate(monkeypatch):
    # The live show's contract: ONE address, immediately, never a probe.
    monkeypatch.setenv("MERLE_OLLAMA", "basement,192.168.1.79:11434")
    assert ollama_client.ollama_address() == ("basement", 11434)
    monkeypatch.setenv("MERLE_OLLAMA", "bluejay:9999")
    assert ollama_client.ollama_address() == ("bluejay", 9999)

