# =============================================================================
# project-squirrel -- weather.py
#
# The weather post (issue #25): a pearl-resident service that polls OpenWeather
# for conditions at the station and publishes them to the bus. It needs neither
# the GPU nor the camera -- just the bus and an internet connection -- so it
# lives on pearl next to the broker and the narrator, 24/7.
#
#   python weather.py
#
# Bus contract (topics in bus.py) -- all three RETAINED, because weather is
# *state*, not a moment: a late joiner (dashboard tab, restarted narrator) gets
# the latest report straight from the broker, so nobody needs an HTTP path or
# a poll loop of their own. Nothing is archived; OpenWeather is the archive of
# record and a dropped report is refetched on the next poll.
#
#   publishes  weather/current    latest observed conditions, one JSON object
#   publishes  weather/forecast   5-day/3-hour series shaped for charting
#   publishes  weather/history    rolling 48h window of observations,
#                                 republished whole every poll
#   publishes  weather/report     Willard's on-air segment (issue #45): the
#                                 conditions + outlook narrated by the LLM in
#                                 an exaggerated Willard Scott voice, ~every
#                                 30 min. Only when MERLE_OLLAMA is set --
#                                 the narrator's kill switch, reused here
#   publishes  weather/status     presence, "online"/"offline" (issue #31):
#                                 the narrator contract -- "offline" is the
#                                 Last Will, so crashes and systemctl stop
#                                 flip the dashboard immediately
#
# The history window is the one piece of state this service owns: ~288 points
# at the 10-minute cadence, persisted to a small JSON file so a restart doesn't
# blank the dashboard's observed-trend trail. Deliberately NOT SQLite -- the
# window is bounded and rewritten whole, so a file is the honest data
# structure. (A seasonal archive, if we ever want one, is a follow-up issue.)
#
# Config (env, following the MERLE_MQTT conventions):
#   MERLE_OWM_KEY          OpenWeather API key. REQUIRED, no default -- a
#                          weather service with no key has no job (same
#                          fail-at-startup philosophy as MERLE_MQTT).
#   MERLE_WEATHER_LOC      "zip", "zip,CC", or "lat,lon" (default: 49001,US --
#                          the station's home turf, Kalamazoo MI).
#   MERLE_WEATHER_HISTORY  history file path (default: weather_history.json)
#   MERLE_MQTT             the broker, required as everywhere else (bus.py).
#   MERLE_OLLAMA           Ollama "host" or "host:port" for Willard's on-air
#                          segment (issue #45). OPTIONAL -- unset means no
#                          weather/report topic at all, and everything above
#                          runs exactly as before. Same var, same semantics,
#                          same client code as the narrator's LLM tier.
#   MERLE_OLLAMA_MODEL     model name (default: narrator.OLLAMA_DEFAULT_MODEL).
#
# We call the CLASSIC free APIs (api.openweathermap.org/data/2.5 weather +
# forecast), not One Call 3.0 -- the classic pair needs no credit card and its
# free limits (60 calls/min, 1M/month) dwarf our load: current every 10 min +
# forecast every hour is ~170 calls/day. The 3-hour forecast step is plenty
# for a what's-coming chart.
#
# Timestamps on the bus are UNIX EPOCH SECONDS (OpenWeather's native `dt`),
# not ISO strings: the dashboard formats them in the viewer's locale and the
# narrator compares against time.time() for staleness -- nobody wants to parse.
# =============================================================================

import json
import os
import time
import urllib.parse
import urllib.request

import bus
# The narrator owns the Ollama plumbing (endpoint config, the blocking
# non-streaming client, the model default); Willard borrows it rather than
# growing a second copy that could drift -- same reasoning as perception.py.
from narrator import OLLAMA_DEFAULT_MODEL, Ollama, ollama_address

CURRENT_INTERVAL_S = 600     # weather doesn't change faster than this
FORECAST_INTERVAL_S = 3600   # the 3-hour-step forecast changes even slower
HISTORY_WINDOW_S = 48 * 3600  # the observed trail the dashboard charts
FETCH_TIMEOUT_S = 20

REPORT_INTERVAL_S = 1800     # a broadcast every half hour is plenty of Willard
REPORT_HORIZON_S = 24 * 3600  # the outlook paragraph covers the next day
# A current report older than this isn't worth narrating (the narrator's
# WEATHER_STALE_S reasoning): if fetches have failed for 3 straight polls,
# skip the broadcast rather than narrate yesterday's rain.
REPORT_MAX_AGE_S = 30 * 60
# A segment is longer than a narrator line (conditions + outlook), so it gets
# more headroom than narrate()'s 120; the temperature keeps two half-hour
# broadcasts over the same numbers from reading like reruns.
REPORT_NUM_PREDICT = 220
REPORT_TEMPERATURE = 0.9

OWM_BASE = "https://api.openweathermap.org/data/2.5"
DEFAULT_LOC = "49001,US"
DEFAULT_HISTORY_PATH = "weather_history.json"


def owm_key():
    """OpenWeather API key from MERLE_OWM_KEY. REQUIRED -- a weather service
    without a key would poll, get 401s, and publish nothing while looking
    healthy. Fail at startup instead (the MERLE_MQTT philosophy)."""
    key = os.environ.get("MERLE_OWM_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "MERLE_OWM_KEY is not set. Create a free API key at "
            "https://openweathermap.org/api and export it before starting "
            "the weather service."
        )
    return key


def location_params(raw):
    """MERLE_WEATHER_LOC -> OpenWeather query params. Three accepted shapes:
    "49001" (zip, country defaults to US), "49001,US" (zip,country), and
    "42.29,-85.59" (lat,lon -- recognized because BOTH halves parse as
    floats, which a country code never does)."""
    loc = (raw or "").strip() or DEFAULT_LOC
    first, _, second = (p.strip() for p in loc.partition(","))
    if second:
        try:
            return {"lat": str(float(first)), "lon": str(float(second))}
        except ValueError:
            return {"zip": f"{first},{second}"}
    return {"zip": f"{first},US"}


def owm_url(endpoint, loc_params, key):
    """URL for a classic-API endpoint ("weather" or "forecast"), imperial
    units (the station reports in Fahrenheit and mph)."""
    params = {**loc_params, "appid": key, "units": "imperial"}
    return f"{OWM_BASE}/{endpoint}?{urllib.parse.urlencode(params)}"


def parse_current(raw):
    """OpenWeather /weather response -> the weather/current bus payload.
    Field names carry their units so no consumer has to guess. `ts` is
    OpenWeather's own observation time, not our fetch time."""
    main = raw.get("main") or {}
    wind = raw.get("wind") or {}
    sys = raw.get("sys") or {}
    weather = (raw.get("weather") or [{}])[0]
    return {
        "ts": raw.get("dt"),
        "temp_f": main.get("temp"),
        "feels_like_f": main.get("feels_like"),
        "humidity_pct": main.get("humidity"),
        "wind_mph": wind.get("speed"),
        "wind_gust_mph": wind.get("gust"),   # absent on calm reads -> None
        "wind_deg": wind.get("deg"),
        "condition": weather.get("main"),        # "Clouds"
        "description": weather.get("description"),  # "overcast clouds"
        "sunrise": sys.get("sunrise"),
        "sunset": sys.get("sunset"),
    }


def parse_forecast(raw):
    """OpenWeather /forecast response (5 days, 3-hour steps, 40 points) ->
    the weather/forecast bus payload: just what the trends chart draws, one
    compact point per step. `pop` is precipitation probability 0..1."""
    points = []
    for step in raw.get("list") or []:
        main = step.get("main") or {}
        wind = step.get("wind") or {}
        weather = (step.get("weather") or [{}])[0]
        points.append({
            "ts": step.get("dt"),
            "temp_f": main.get("temp"),
            "wind_mph": wind.get("speed"),
            "wind_gust_mph": wind.get("gust"),
            "condition": weather.get("main"),
            "pop": step.get("pop", 0),
        })
    return {"points": points}


def history_point(current):
    """The compact per-observation record the rolling window keeps -- the
    trend chart needs temp + wind + condition, not the full report."""
    return {
        "ts": current["ts"],
        "temp_f": current["temp_f"],
        "wind_mph": current["wind_mph"],
        "wind_gust_mph": current["wind_gust_mph"],
        "condition": current["condition"],
    }


def roll_history(window, point, max_age_s=HISTORY_WINDOW_S):
    """Append an observation and prune the trail to the window, purely.
    Deduped by ts (a restart re-fetches a report the file already has) and
    age is measured from the NEWEST point, not the wall clock, so the
    function stays deterministic for tests."""
    if point.get("ts") is None:
        return list(window)
    merged = {p["ts"]: p for p in window if p.get("ts") is not None}
    merged[point["ts"]] = point
    newest = max(merged)
    return [merged[ts] for ts in sorted(merged) if ts >= newest - max_age_s]


def load_history(path):
    """The persisted window, or a fresh one. Missing and corrupt files both
    mean "start over" -- the window refills in 48h and is not a record of
    record, so failing loudly would cost more than it protects."""
    try:
        with open(path, encoding="utf-8") as f:
            points = json.load(f)
        return points if isinstance(points, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_history(path, window):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(window, f)


# --- Willard's on-air segment (issue #45) ------------------------------------
# The persona is a constant, not a personas/*.yaml: that system carries
# narrator-only machinery (mqtt_id, tts_voice, pacing knobs) and Willard is a
# reporter, not a narrator. Output rules ride separately, the LINE_RULES
# split: the persona stays pure character.

WILLARD_PERSONALITY = (
    "You are Willard, the weatherman of a beloved wildlife television program "
    "broadcasting from a suburban driveway station. Your delivery is an "
    "affectionate, EXAGGERATED homage to Willard Scott, the legendary NBC "
    "weatherman: booming folksy warmth, corny showmanship, groan-worthy puns, "
    "and utter delight in perfectly ordinary weather. You flatter the "
    "sunshine, gently scold the wind, and treat a passing shower like news "
    "from a dear old friend. Your loyal viewers are the squirrels and turkeys "
    "of the driveway, and you look out for them -- a hot afternoon means "
    "advising the squirrels to take it easy, a cold snap means fluffing up "
    "those feathers. Now and then you salute a distinguished local on a "
    "milestone -- a hundred-year-old oak, a venerable acorn -- in the grand "
    "birthday-wishes tradition. You are never mean and never bored: there is "
    "no such thing as dull weather, only weather that hasn't been properly "
    "introduced."
)

REPORT_RULES = (
    "Deliver your on-air weather segment in ONE or TWO short paragraphs, four "
    "to six sentences in all: current conditions first, then what's on the "
    "way. Use the measurements you are given -- never invent numbers. Spoken "
    "words only: no stage directions, no quotation marks, no emoji, no "
    "markdown, no preamble, no sign-off name. Never break character."
)

# Labeled paragraphs, the narrator's desk-tested prompt shape (issue #26):
# bare facts buried in prose get ignored; labeled blocks get woven in.
REPORT_CURRENT_HEADER = "Current conditions at the station:"
REPORT_OUTLOOK_HEADER = "The next 24 hours:"


def report_system_prompt():
    return f"{WILLARD_PERSONALITY}\n\n{REPORT_RULES}"


def clock_12h(ts):
    """Epoch seconds -> "6:14 am" in the service's local time (the service
    runs where the station is, so local time IS station time). Prompt color
    only -- the bus keeps raw epochs."""
    return time.strftime("%I:%M %p", time.localtime(ts)).lstrip("0").lower()


def current_facts(current):
    """The observed conditions as one dry factual paragraph -- the LLM's raw
    material, deliberately plain so all the flavor comes from the persona.
    "" when the report has no temperature (nothing worth a broadcast)."""
    temp = current.get("temp_f")
    if temp is None:
        return ""
    sentence = f"It is {round(temp)}F"
    feels = current.get("feels_like_f")
    if feels is not None and round(feels) != round(temp):
        sentence += f", feels like {round(feels)}F"
    parts = [sentence + "."]
    if current.get("description"):
        parts.append(f"Sky: {current['description']}.")
    wind = current.get("wind_mph")
    if wind is not None:
        gust = current.get("wind_gust_mph")
        parts.append(f"Wind {round(wind)} mph"
                     + (f", gusting to {round(gust)}" if gust is not None else "")
                     + ".")
    if current.get("humidity_pct") is not None:
        parts.append(f"Humidity {round(current['humidity_pct'])} percent.")
    if current.get("sunrise") is not None and current.get("sunset") is not None:
        parts.append(f"Sunrise {clock_12h(current['sunrise'])}, "
                     f"sunset {clock_12h(current['sunset'])}.")
    return " ".join(parts)


def forecast_digest(points, now, horizon_s=REPORT_HORIZON_S):
    """Boil the 3-hour forecast steps ahead of `now` down to what one outlook
    paragraph needs: the temperature span, the strongest wind or gust, the
    peak precipitation chance, and the conditions in order of appearance.
    None when no temperature-bearing point falls inside the horizon."""
    ahead = [p for p in points
             if p.get("ts") is not None and now < p["ts"] <= now + horizon_s]
    temps = [p["temp_f"] for p in ahead if p.get("temp_f") is not None]
    if not temps:
        return None
    winds = [w for p in ahead
             for w in (p.get("wind_mph"), p.get("wind_gust_mph"))
             if w is not None]
    conditions = []
    for p in ahead:
        if p.get("condition") and p["condition"] not in conditions:
            conditions.append(p["condition"])
    return {
        "high_f": max(temps),
        "low_f": min(temps),
        "max_wind_mph": max(winds) if winds else None,
        "max_pop": max((p.get("pop") or 0) for p in ahead),
        "conditions": conditions,
    }


def outlook_facts(digest):
    """The forecast digest as the same dry-paragraph shape as current_facts."""
    parts = [f"Temperatures from {round(digest['low_f'])}F "
             f"to {round(digest['high_f'])}F."]
    if digest["max_wind_mph"] is not None:
        parts.append(f"Winds up to {round(digest['max_wind_mph'])} mph.")
    if digest["max_pop"] > 0:
        parts.append("Chance of precipitation up to "
                     f"{round(digest['max_pop'] * 100)} percent.")
    if digest["conditions"]:
        parts.append("Conditions: "
                     + ", ".join(c.lower() for c in digest["conditions"]) + ".")
    return " ".join(parts)


def build_report_prompt(current, forecast_points, now):
    """The full user prompt for one segment: labeled conditions paragraph,
    labeled outlook paragraph (when the forecast has anything ahead), then
    the cue. None when there's nothing fresh worth narrating -- a missing or
    stale current report skips the broadcast entirely rather than letting
    Willard ad-lib around a hole."""
    if not current or current.get("ts") is None \
            or now - current["ts"] > REPORT_MAX_AGE_S:
        return None
    facts = current_facts(current)
    if not facts:
        return None
    # The clock rides the conditions paragraph: without it the model guesses
    # the time of day and greets the noon audience with "good morning"
    # (desk-tested on the very first generation).
    parts = [f"{REPORT_CURRENT_HEADER} The station clock reads "
             f"{clock_12h(now)}. {facts}"]
    digest = forecast_digest(forecast_points, now)
    if digest:
        parts.append(f"{REPORT_OUTLOOK_HEADER} {outlook_facts(digest)}")
    parts.append("Your on-air weather segment:")
    return "\n\n".join(parts)


def sanitize_report(text):
    """LLM output -> broadcast text, or None if unusable. The narrator's
    sanitize_line flattens everything to one line; a segment may legitimately
    be two paragraphs, so this collapses whitespace WITHIN each paragraph and
    keeps the break (the dashboard renders it). Strips the markdown bold and
    wrapping quotes the model sometimes adds despite the rules."""
    paras = [" ".join(p.split()) for p in (text or "").replace("**", "").split("\n\n")]
    paras = [p for p in (q.strip('"“” ') for q in paras) if p]
    return "\n\n".join(paras) or None


def broadcast(ollama, current, forecast_points, now=None):
    """One Willard segment: prompt -> Ollama -> sanitized text, or None when
    there is nothing fresh to narrate or the LLM is unreachable (the caller
    retries on the next poll pass -- never a dead service)."""
    now = time.time() if now is None else now
    prompt = build_report_prompt(current, forecast_points, now)
    if prompt is None:
        return None
    return sanitize_report(ollama.complete(
        report_system_prompt(), prompt,
        num_predict=REPORT_NUM_PREDICT, temperature=REPORT_TEMPERATURE))


def fetch(url):
    """One OpenWeather call -> parsed JSON, or None on ANY failure. A flaky
    network or a rate-limit blip must mean a skipped report (the next poll
    retries), never a dead service -- same never-raise posture as the
    narrator's Ollama call. The URL carries the API key, so log only the
    error, never the address."""
    try:
        with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_S) as resp:
            return json.load(resp)
    except Exception as e:
        print(f"[weather] fetch failed: {type(e).__name__}: {e} -- will retry")
        return None


def main():
    key = owm_key()
    loc = location_params(os.environ.get("MERLE_WEATHER_LOC"))
    history_path = os.environ.get("MERLE_WEATHER_HISTORY", "").strip() \
        or DEFAULT_HISTORY_PATH
    current_url = owm_url("weather", loc, key)
    forecast_url = owm_url("forecast", loc, key)

    # Willard's voice (issue #45): the narrator's LLM tier, same env vars,
    # same kill switch -- MERLE_OLLAMA unset means no weather/report topic
    # and this service runs exactly as it did before the segment existed.
    ollama = None
    addr = ollama_address()
    if addr:
        model = os.environ.get("MERLE_OLLAMA_MODEL", "").strip() \
            or OLLAMA_DEFAULT_MODEL
        ollama = Ollama(*addr, model)
        print(f"[weather] willard's voice: LLM ({model} via "
              f"{addr[0]}:{addr[1]}), a segment every "
              f"{REPORT_INTERVAL_S // 60} min")
    else:
        print("[weather] willard's voice: off (MERLE_OLLAMA not set)")

    window = load_history(history_path)
    # status_topic gives Willard the narrator presence contract (issue #31):
    # retained online/offline with the Last Will covering crashes AND
    # systemctl stop, so the dashboard masthead flips within seconds instead
    # of waiting out the 30-minute staleness window.
    publisher = bus.EventPublisher(
        "weather", status_topic=bus.WEATHER_STATUS_TOPIC).start()
    print(f"[weather] on duty: loc={loc}, {len(window)} points of history, "
          f"polling every {CURRENT_INTERVAL_S // 60} min")

    current = None           # the freshest observed report, for the segment
    forecast_points = []     # the freshest forecast series, for the outlook
    next_forecast_at = 0.0   # 0 -> the first loop pass fetches the forecast
    next_report_at = 0.0     # 0 -> Willard goes on the air on the first pass
    try:
        while True:
            raw = fetch(current_url)
            if raw is not None:
                current = parse_current(raw)
                publisher.publish(bus.WEATHER_CURRENT_TOPIC, current,
                                  retain=True)
                window = roll_history(window, history_point(current))
                save_history(history_path, window)
                publisher.publish(bus.WEATHER_HISTORY_TOPIC,
                                  {"points": window}, retain=True)
                print(f"[weather] {current['temp_f']}F, "
                      f"{current['description']}, "
                      f"wind {current['wind_mph']} mph "
                      f"({len(window)} points in the window)")

            if time.time() >= next_forecast_at:
                raw = fetch(forecast_url)
                if raw is not None:
                    forecast = parse_forecast(raw)
                    forecast_points = forecast["points"]
                    publisher.publish(bus.WEATHER_FORECAST_TOPIC,
                                      forecast, retain=True)
                    next_forecast_at = time.time() + FORECAST_INTERVAL_S
                # on failure next_forecast_at stays put, so the NEXT
                # current-poll pass retries the forecast instead of waiting
                # out a full hour

            if ollama is not None and time.time() >= next_report_at:
                # Blocking (up to OLLAMA_TIMEOUT_S) is fine here: this is the
                # main loop's own thread, not paho's, and a poll cycle has
                # 10 minutes of slack. Retained, like the rest of the weather
                # set -- a fresh dashboard tab paints the segment instantly.
                text = broadcast(ollama, current, forecast_points)
                if text:
                    publisher.publish(
                        bus.WEATHER_REPORT_TOPIC,
                        {"ts": int(time.time()), "text": text,
                         "model": ollama.model},
                        retain=True)
                    next_report_at = time.time() + REPORT_INTERVAL_S
                    print(f"[weather] willard on the air: {text.splitlines()[0][:80]}...")
                # on failure (LLM down, no fresh report) next_report_at stays
                # put -- the forecast-retry pattern: the next 10-min pass
                # tries again instead of waiting out the half hour

            time.sleep(CURRENT_INTERVAL_S)
    except KeyboardInterrupt:
        # Manual desk runs: sign off cleanly (close() publishes the retained
        # offline). Under systemd this never runs -- SIGTERM fires the will.
        publisher.close()
        print("\n[weather] off duty.")


if __name__ == "__main__":
    main()
