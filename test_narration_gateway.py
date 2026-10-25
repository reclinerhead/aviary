# =============================================================================
# project-squirrel -- test_narration_gateway.py
#
# The cloud writer (#632). What is pinned: the body that leaves the box is
# exactly four keys and nothing else; the reply parser and every failure
# answer None (the pass's llm-down, nothing written); each status gets its
# one journal line; the config reads the key and the model override. The
# HTTP call itself is desk-checked on orchid.
# =============================================================================

import io
import json
import urllib.error

from narration import gateway as gw


def test_config_needs_a_key_and_takes_a_model_override():
    assert gw.gateway_config({}) is None
    assert gw.gateway_config({"AI_GATEWAY_API_KEY": "  "}) is None
    assert gw.gateway_config({"AI_GATEWAY_API_KEY": "k"}) == \
        ("k", gw.DEFAULT_MODEL)
    assert gw.gateway_config({"AI_GATEWAY_API_KEY": "k",
                              "MERLE_GATEWAY_MODEL": "anthropic/claude-sonnet-5.5"
                              }) == ("k", "anthropic/claude-sonnet-5.5")
    assert gw.DEFAULT_MODEL == "anthropic/claude-opus-5.5"   # dots, not dashes


def test_the_payload_is_the_prompt_and_nothing_else():
    # The #620 rule: what leaves the box is the voice, the prompt, the model
    # and a ceiling. A planted hostname, address, position or path in the
    # prompt rides it verbatim -- the pass builds the prompt, this never
    # adds to it -- and nothing else is sent.
    prompt = "orchid.lan 192.168.1.148 42.26,-85.57 /home/todd/earl.db"
    body = gw.gateway_payload("voice", prompt, "anthropic/claude-opus-5.5")
    assert body == {"model": "anthropic/claude-opus-5.5", "max_tokens": 8192,
                    "system": "voice",
                    "messages": [{"role": "user", "content": prompt}]}
    assert "temperature" not in body and "thinking" not in body
    # The ceiling is the writer's own, never the pass's 448-token backstop:
    # Claude's thinking counts against it, and 448 bought no text at all.
    assert gw.GATEWAY_MAX_TOKENS >= 4096


def test_the_reply_is_the_text_blocks_or_nothing():
    reply = {"content": [{"type": "thinking", "thinking": ""},
                         {"type": "text", "text": "One. "},
                         {"type": "text", "text": "Two."}],
             "stop_reason": "end_turn"}
    assert gw.parse_reply(reply) == "One. Two."
    assert gw.parse_reply(dict(reply, stop_reason="refusal")) is None
    # A cut-off note is nothing written, even with a partial text block.
    assert gw.parse_reply(dict(reply, stop_reason="max_tokens")) is None
    assert gw.parse_reply({"content": [], "stop_reason": "end_turn"}) is None
    assert gw.parse_reply({"type": "error"}) is None
    assert gw.parse_reply(["not", "a", "message"]) is None


def test_each_status_names_what_to_fix():
    assert gw.status_line(401).startswith("the gateway refused the key")
    assert "no credits" in gw.status_line(402)
    assert "rate-limited" in gw.status_line(429)
    assert gw.status_line(503) == "the gateway answered a server error (503)"
    assert gw.status_line(400, "bad model") == \
        "the gateway rejected the request (400): bad model"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_complete_posts_the_payload_and_returns_the_text(monkeypatch, capsys):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data.decode("utf-8"))
        seen["timeout"] = timeout
        return FakeResponse(json.dumps({
            "content": [{"type": "text", "text": "A note."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1500, "output_tokens": 900}}).encode())

    monkeypatch.setattr(gw.urllib.request, "urlopen", fake_urlopen)
    client = gw.Gateway("sk-test", "anthropic/claude-opus-5.5")
    assert client.host == "ai-gateway.vercel.sh"
    text = client.complete("voice", "prompt", num_predict=448,
                           temperature=0.7, timeout=300)
    assert text == "A note."
    assert seen["url"] == "https://ai-gateway.vercel.sh/v1/messages"
    assert seen["headers"]["authorization"] == "Bearer sk-test"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["body"] == gw.gateway_payload("voice", "prompt",
                                              "anthropic/claude-opus-5.5")
    assert seen["body"]["max_tokens"] == gw.GATEWAY_MAX_TOKENS   # not 448
    assert seen["timeout"] == 300
    # The bill, and why the model stopped, are visible in the journal.
    assert "1500 tokens in, 900 out (stop_reason 'end_turn')" \
        in capsys.readouterr().out


def test_complete_answers_none_and_one_line_on_every_failure(monkeypatch,
                                                              capsys):
    client = gw.Gateway("sk-test")

    def http_error(code, message):
        def fake(req, timeout=None):
            raise urllib.error.HTTPError(
                gw.GATEWAY_URL, code, "x", {},
                io.BytesIO(json.dumps({"error": {"message": message}}).encode()))
        return fake

    monkeypatch.setattr(gw.urllib.request, "urlopen",
                        http_error(402, "insufficient credits"))
    assert client.complete("v", "p") is None
    out = capsys.readouterr().out
    assert "no credits" in out and "insufficient credits" in out
    assert out.count("\n") == 1

    def unreachable(req, timeout=None):
        raise urllib.error.URLError("name or service not known")

    monkeypatch.setattr(gw.urllib.request, "urlopen", unreachable)
    assert client.complete("v", "p") is None
    assert "unreachable" in capsys.readouterr().out

    def refused(req, timeout=None):
        return FakeResponse(json.dumps({"content": [],
                                        "stop_reason": "refusal"}).encode())

    monkeypatch.setattr(gw.urllib.request, "urlopen", refused)
    assert client.complete("v", "p") is None
    assert "'refusal'" in capsys.readouterr().out

    # The 2026-10-25 failure, verbatim: the budget spent on thinking.
    def cut_off(req, timeout=None):
        return FakeResponse(json.dumps({
            "content": [{"type": "thinking", "thinking": ""}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 1574, "output_tokens": 448}}).encode())

    monkeypatch.setattr(gw.urllib.request, "urlopen", cut_off)
    assert client.complete("v", "p") is None
    out = capsys.readouterr().out
    assert "(stop_reason 'max_tokens')" in out and "nothing written" in out
