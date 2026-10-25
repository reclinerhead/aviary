# =============================================================================
# project-squirrel -- narration/gateway.py
#
# The cloud writer (issue #632): a Claude model behind the Vercel AI
# Gateway, with the narrator's Ollama client's shape --
# complete(system, prompt, num_predict, temperature, timeout) -> text | None
# -- so the field-journal pass (listener/species_analysis.py) hands it the
# same prompt, runs the same validator over the answer, and stores the
# entry with the same provenance columns (model, host).
#
# ON DEMAND ONLY, BY CONSTRUCTION. Nothing builds a Gateway except the
# pass's CLI under `--refresh <sci> --writer gateway` -- the species page's
# "write a fresh entry (claude)" control. The enrichment loop constructs
# Ollama and nothing else (test_listener_enrichment_loop pins it: a tick
# with AI_GATEWAY_API_KEY in its environment and a Gateway that explodes on
# construction still writes its entries), so a tick can never spend a cent
# whatever env a unit carries. The key belongs on orchid's mcc-dashboard
# unit, which spawns the refresh; the earl-enrichment unit has no use for
# it. #617 is where any automatic use of this writer would be decided.
#
# One POST, no SDK: the Anthropic Messages API at the gateway's base URL
# (verified against the gateway's docs on 2026-10-25), a bearer token, a
# JSON body -- the Ollama client's own idiom (urllib, stdlib only), so no
# box that deploys narration/ grows a dependency. The body carries no
# sampling settings (Claude Opus 5.5 rejects them) and no thinking settings
# (thinking is always on at the default effort, which suits a 200-word
# note); its usage is printed per call so the bill is visible in the MCC
# journal under [refresh].
#
# Config (env):
#   AI_GATEWAY_API_KEY    the gateway's key (BYOK in the Vercel dashboard
#                         makes the Anthropic account the payer); unset =
#                         no cloud writer, the pass says so and writes
#                         nothing
#   MERLE_GATEWAY_MODEL   a gateway model slug (dots in the version:
#                         anthropic/claude-sonnet-5.5), default
#                         DEFAULT_MODEL; the unauthenticated list is at
#                         https://ai-gateway.vercel.sh/v1/models
# =============================================================================

import json
import os
import urllib.error
import urllib.request

GATEWAY_HOST = "ai-gateway.vercel.sh"
GATEWAY_URL = f"https://{GATEWAY_HOST}/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "anthropic/claude-opus-5.5"
# A patient ceiling: one entry is a few hundred output tokens plus the
# model's own thinking, well under a minute; two is slack for a slow day.
GATEWAY_TIMEOUT_S = 120
# The request's max_tokens. NOT the pass's NUM_PREDICT (448, a backstop
# for gemma's visible output): Claude thinks before it writes and the
# thinking counts against the same ceiling, so handed 448 it spent the
# whole budget thinking and answered with no text at all -- three presses
# on 2026-10-25, each stop_reason max_tokens, nothing written, a cent and
# a half apiece. 8192 is room for the thinking and a 220-word note, and a
# bounded worst case (about 16 cents on Opus 5.5).
GATEWAY_MAX_TOKENS = 8192

# What the journal says for a status the gateway answers with, in the
# reader's words -- each is a different thing for Todd to go and fix.
STATUS_WORDS = {
    400: "the gateway rejected the request",
    401: "the gateway refused the key (AI_GATEWAY_API_KEY)",
    402: "the gateway has no credits to bill",
    403: "the gateway key is not allowed to do this",
    404: "the gateway knows no such model",
    429: "the gateway rate-limited the request",
}


def gateway_config(environ=None):
    """(key, model) from the environment, or None when there is no key --
    the pass then has no cloud writer and says so."""
    env = os.environ if environ is None else environ
    key = (env.get("AI_GATEWAY_API_KEY") or "").strip()
    if not key:
        return None
    model = (env.get("MERLE_GATEWAY_MODEL") or "").strip() or DEFAULT_MODEL
    return key, model


def gateway_payload(system, prompt, model, max_tokens=GATEWAY_MAX_TOKENS):
    """EXACTLY what leaves the box (#620's rule, applied here first): the
    voice, the prompt the pass built, the model slug and a token ceiling.
    Nothing is added -- no sampling, no thinking settings, no metadata --
    and test_narration_gateway pins the body to these four keys."""
    return {"model": model, "max_tokens": int(max_tokens), "system": system,
            "messages": [{"role": "user", "content": prompt}]}


def parse_reply(body):
    """The answer's text blocks joined, or None: a refusal, a cut-off
    (stop_reason max_tokens -- a note truncated mid-sentence must never
    reach the validator as if it were whole), a body that isn't a message,
    or no text at all -- all 'nothing written' to the caller, the way a
    dead Ollama is."""
    if not isinstance(body, dict):
        return None
    if body.get("stop_reason") in ("refusal", "max_tokens"):
        return None
    parts = [b.get("text") or "" for b in body.get("content") or []
             if isinstance(b, dict) and b.get("type") == "text"]
    text = "".join(parts).strip()
    return text or None


def status_line(code, detail=""):
    """One journal line for an HTTP status, naming what to fix."""
    words = STATUS_WORDS.get(code)
    if words is None:
        words = ("the gateway answered a server error"
                 if code >= 500 else "the gateway answered")
    line = f"{words} ({code})"
    if detail:
        line += f": {detail}"
    return line


class Gateway:
    """One blocking completion per call through the AI Gateway's Anthropic
    Messages endpoint. Returns None instead of raising, like Ollama: the
    pass treats None as llm-down and writes nothing."""

    host = GATEWAY_HOST

    def __init__(self, key, model=DEFAULT_MODEL):
        self.key = key
        self.model = model

    def complete(self, system, prompt, num_predict=120, temperature=0.9,
                 timeout=None):
        """Raw model text, or None on any failure. `temperature` and
        `num_predict` are the Ollama signature's and are NOT sent: Claude
        Opus 5.5 rejects non-default sampling, and the pass's output
        backstop is far too small once thinking counts against it (see
        GATEWAY_MAX_TOKENS). The note's length is the prompt's word
        budget and the validator's business, as it is for Ollama."""
        del temperature, num_predict
        payload = gateway_payload(system, prompt, self.model)
        req = urllib.request.Request(
            GATEWAY_URL, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.key}",
                     "anthropic-version": ANTHROPIC_VERSION})
        try:
            with urllib.request.urlopen(
                    req, timeout=timeout or GATEWAY_TIMEOUT_S) as resp:
                body = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = (json.loads(e.read().decode("utf-8"))
                          .get("error", {}).get("message", ""))
            except Exception:
                pass
            print(f"[gateway] {status_line(e.code, detail)} -- nothing "
                  "written", flush=True)
            return None
        except Exception as e:   # unreachable, timed out, unreadable body
            print(f"[gateway] {GATEWAY_HOST} unreachable "
                  f"({type(e).__name__}: {e}) -- nothing written", flush=True)
            return None
        usage = body.get("usage") if isinstance(body, dict) else None
        if isinstance(usage, dict):
            stop = body.get("stop_reason")
            print(f"[gateway] {self.model}: {usage.get('input_tokens', '?')} "
                  f"tokens in, {usage.get('output_tokens', '?')} out"
                  f" (stop_reason {stop!r})", flush=True)
        text = parse_reply(body)
        if text is None:
            stop = body.get("stop_reason") if isinstance(body, dict) else None
            print(f"[gateway] {self.model} answered without usable text "
                  f"(stop_reason {stop!r}) -- nothing written", flush=True)
        return text
