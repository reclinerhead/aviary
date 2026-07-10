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

CURRENT_INTERVAL_S = 600     # weather doesn't change faster than this
FORECAST_INTERVAL_S = 3600   # the 3-hour-step forecast changes even slower
HISTORY_WINDOW_S = 48 * 3600  # the observed trail the dashboard charts
FETCH_TIMEOUT_S = 20

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

    window = load_history(history_path)
    publisher = bus.EventPublisher("weather").start()
    print(f"[weather] on duty: loc={loc}, {len(window)} points of history, "
          f"polling every {CURRENT_INTERVAL_S // 60} min")

    next_forecast_at = 0.0   # 0 -> the first loop pass fetches the forecast
    while True:
        raw = fetch(current_url)
        if raw is not None:
            current = parse_current(raw)
            publisher.publish(bus.WEATHER_CURRENT_TOPIC, current, retain=True)
            window = roll_history(window, history_point(current))
            save_history(history_path, window)
            publisher.publish(bus.WEATHER_HISTORY_TOPIC, {"points": window},
                              retain=True)
            print(f"[weather] {current['temp_f']}F, {current['description']}, "
                  f"wind {current['wind_mph']} mph "
                  f"({len(window)} points in the window)")

        if time.time() >= next_forecast_at:
            raw = fetch(forecast_url)
            if raw is not None:
                publisher.publish(bus.WEATHER_FORECAST_TOPIC,
                                  parse_forecast(raw), retain=True)
                next_forecast_at = time.time() + FORECAST_INTERVAL_S
            # on failure next_forecast_at stays put, so the NEXT current-poll
            # pass retries the forecast instead of waiting out a full hour

        time.sleep(CURRENT_INTERVAL_S)


if __name__ == "__main__":
    main()
