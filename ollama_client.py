"""The one Ollama client in the codebase. The narrator's LLM tier, Willard's
on-air segment (issue #45) and the Aviary's field notes (#186, #217) all
build on this module; #628 lifted it out of narration/narrator.py, where
it grew, so the listener and the weather post no longer import the
narrator to reach it.

One blocking, non-streaming completion per call, and every failure
degrades to None instead of raising: a dead LLM means templates or a
skipped segment, never a dead caller. Standard library only, so the
narrator's two-package venv on merle and the stations' listeners carry no
new dependency.

Config (env, the MERLE_MQTT convention):
  MERLE_OLLAMA        comma-separated "host[:port]" candidates, best first
                      (port defaults to OLLAMA_DEFAULT_PORT). UNSET = the
                      LLM tier is off, which is both the kill switch and
                      what keeps a bare dev checkout working.
  MERLE_OLLAMA_MODEL  read by each caller; OLLAMA_DEFAULT_MODEL is the
                      default they share.

Not named ollama.py: that would shadow the PyPI `ollama` package the moment
someone installed it.
"""

import json
import os
import urllib.error
import urllib.request

OLLAMA_DEFAULT_PORT = 11434
OLLAMA_DEFAULT_MODEL = "gemma3:12b"
# Generation runs on the main pacing loop (issue #74 moved it off paho's
# network thread -- on_message only enqueues now, so a slow generation can no
# longer threaten the MQTT keepalive). Events arriving mid-generation wait in
# the queue and mostly collapse at the Editor afterwards -- which is the
# pacing we wanted anyway. Desk-tested: ~8s warm, ~15s with a cold model load,
# so 30s gives cold starts headroom without stalling the show for long.
OLLAMA_TIMEOUT_S = 30
# Context window, sent EXPLICITLY on every call (issue #493). Left unset,
# bluejay's Ollama loads each model at its maximum (131072 on gemma3 --
# measured 2026-08-21 on 12b, 27b and llama3.1:8b alike), and the KV cache
# for that window no longer fits beside the weights: gemma3:12b spilled to a
# 27/73 CPU/GPU split at 10 GB, 27b to 53/47 at 19 GB and ~33 s a call. This
# is the ONE Ollama client in the codebase (narrator, Willard, the Aviary's
# field notes), so one number sizes them all. Measured worst cases: the
# narrator's biggest persona with a full memory + heard block and weather is
# ~2.1k tokens including num_predict; the field notes ~1.1k; Willard under
# 1k. 8192 is a ~4x margin over the largest, and at that window 12b sits
# fully GPU-resident on a 16 GB card.
OLLAMA_NUM_CTX = 8192


def ollama_candidates():
    """MERLE_OLLAMA as a preference-ordered candidate list (issue #217):
    comma-separated "host[:port]" entries, best first. Today the list holds
    one entry; when the basement box exists it is prepended and nothing else
    changes. The enrichment loop probes these in order; the live show never
    does (see ollama_address). Unset means an empty list -- the kill switch,
    unchanged."""
    raw = os.environ.get("MERLE_OLLAMA", "").strip()
    out = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        host, _, port = entry.partition(":")
        out.append((host, int(port) if port else OLLAMA_DEFAULT_PORT))
    return out


def ollama_address():
    """Ollama endpoint from MERLE_OLLAMA ("host" or "host:port"). OPTIONAL,
    unlike MERLE_MQTT -- unset simply means the LLM tier is off, which is both
    the kill switch and what keeps a bare dev checkout working. With a #217
    candidate list set this is the FIRST (most preferred) entry: the live
    show wants one address immediately, never a probe loop."""
    candidates = ollama_candidates()
    return candidates[0] if candidates else None



class Ollama:
    """One blocking, non-streaming completion per call (the bus contract is
    one JSON object per spoken line, and the dashboard TTS speaks whole lines
    -- nothing downstream could use a token stream). complete() is the generic
    core, shared with the weather post's Willard segment (issue #45);
    narrate(), the narrator-shaped wrapper, is the subclass in
    narration/narrator.py. Both return None instead of raising, because a dead LLM must degrade (templates, a skipped segment),
    not kill the caller."""

    def __init__(self, host, port, model):
        self.url = f"http://{host}:{port}/api/generate"
        self.model = model
        # Transition state for the unreachable-host case (#335) -- see
        # complete(). Per-instance, which is what makes it work: every caller
        # that matters builds one client and keeps it (Willard for the life of
        # the service, the narrator for the life of the show).
        self._unreachable = False

    def complete(self, system, prompt, num_predict=120, temperature=0.9,
                 timeout=None):
        """Raw model text for one system+prompt pair, or None on any failure.
        num_predict caps a model that ignores the length rules; the
        temperature keeps repeat prompts from producing repeat prose.

        `timeout` defaults to OLLAMA_TIMEOUT_S, which is a LIVE-SHOW pacing
        number -- the narrator would rather drop a line than stall the pacing
        loop. Batch callers with no show to hold up (the Aviary's analysis
        pass, issue #186) pass their own patient ceiling."""
        payload = {
            "model": self.model,
            "system": system,
            "prompt": prompt,
            "stream": False,
            # Driveway events can be an hour apart; without keep_alive every
            # call would pay the cold model load (~2x latency, desk-tested).
            "keep_alive": "30m",
            # num_ctx must be explicit -- see OLLAMA_NUM_CTX (issue #493).
            "options": {"num_predict": num_predict, "temperature": temperature,
                        "num_ctx": OLLAMA_NUM_CTX},
        }
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(
                    req, timeout=timeout or OLLAMA_TIMEOUT_S) as resp:
                reply = json.load(resp)
        except Exception as e:   # any failure at all -> the caller's fallback
            # A HOST THAT ISN'T THERE IS TRANSITION-LOGGED (#335), the
            # autodeploy fetch_down idiom: one line when it goes away, one
            # when it returns, silence in between. Bluejay is a workstation
            # that is off or Ollama-less most of the time -- documented steady
            # state, not a fault -- and Willard retries every ~90 s, which
            # measured at 41 error lines an hour for a healthy service. That
            # is the #35 journal-spam disease: the noise buries the lines that
            # matter, and a wall of red makes a working system look broken.
            #
            # Anything ELSE stays loud every time. A host that answered with a
            # 500, or sent JSON we can't read, is a real anomaly rather than
            # an expected absence, and silencing those would be swallowing
            # errors instead of removing noise. HTTPError subclasses URLError,
            # hence the explicit exclusion.
            absent = (isinstance(e, (urllib.error.URLError, TimeoutError,
                                     ConnectionError))
                      and not isinstance(e, urllib.error.HTTPError))
            if not absent:
                print(f"[ollama] {type(e).__name__}: {e} -- generation "
                      "skipped")
            elif not self._unreachable:
                self._unreachable = True
                print(f"[ollama] {self.url} unreachable ({type(e).__name__})"
                      " -- generation skipped, and quiet until it returns")
            return None
        if self._unreachable:
            self._unreachable = False
            print(f"[ollama] {self.url} is back")
        return reply.get("response")
