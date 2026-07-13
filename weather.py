# =============================================================================
# project-squirrel -- weather.py
#
# The weather post (issue #25, re-instrumented in #51): a pearl-resident
# service that reads the driveway's OWN weather station -- an Ecowitt GW2000B
# gateway + WH90 sensor array on the LAN -- and publishes it to the bus. The
# station is the system of truth for everything it can measure; OpenWeather
# keeps exactly two jobs it can't: the forecast, and the sky garnish
# (condition text, sunrise/sunset). It needs neither the GPU nor the camera --
# just the bus, the gateway, and an internet connection -- so it lives on
# pearl next to the broker and the narrator, 24/7.
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
#   publishes  weather/history    rolling 48h window of observations at 5-min
#                                 resolution, republished whole every append
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
# The history window is the one piece of state this service owns: the station
# reports every 60 seconds but the retained trail keeps 5-minute resolution
# (~576 points over 48h -- fresh enough to chart, bounded enough to republish
# whole every append), persisted to a small JSON file so a restart doesn't
# blank the dashboard's observed-trend trail. Deliberately NOT SQLite -- the
# window is bounded and rewritten whole, so a file is the honest data
# structure. (A seasonal archive, if we ever want one, is a follow-up issue.)
#
# Config (env, following the MERLE_MQTT conventions):
#   MERLE_ECOWITT          the GW2000B gateway, "host" or "host:port".
#                          REQUIRED, no default -- the station is the system
#                          of truth now, and a weather service that can't
#                          reach it has no job (the MERLE_MQTT philosophy).
#   MERLE_OWM_KEY          OpenWeather API key. REQUIRED, no default -- the
#                          forecast still rides on it.
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
# free limits (60 calls/min, 1M/month) dwarf our load: garnish every 10 min +
# forecast every hour is ~170 calls/day. The 3-hour forecast step is plenty
# for a what's-coming chart.
#
# The gateway speaks unauthenticated local HTTP JSON: /get_livedata_info is
# every live reading as unit-suffixed strings keyed by opaque ids ("0x02" ->
# {"val": "78.8", "unit": "F"}), /get_sensors_info the attached-hardware
# roster (battery, radio signal). The measured fields NEVER fall back to
# OpenWeather -- one system of truth, and a silent source switch would lie on
# the chart. A gateway that can't be reached means a skipped poll and a gap,
# which the dashboard draws as a gap.
#
# Timestamps on the bus are UNIX EPOCH SECONDS, not ISO strings: the dashboard
# formats them in the viewer's locale and the narrator compares against
# time.time() for staleness -- nobody wants to parse. The station has no
# observation clock of its own, so weather/current's `ts` is our fetch time.
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

STATION_INTERVAL_S = 60      # the gateway refreshes its live data every 60s
GARNISH_INTERVAL_S = 600     # OWM sky/sun garnish; the old current cadence
FORECAST_INTERVAL_S = 3600   # the 3-hour-step forecast changes even slower
HISTORY_WINDOW_S = 48 * 3600  # the observed trail the dashboard charts
HISTORY_STEP_S = 300         # the trail's resolution: keep 1 point per 5 min
FETCH_TIMEOUT_S = 20

REPORT_INTERVAL_S = 1800     # a broadcast every half hour is plenty of Willard
REPORT_HORIZON_S = 24 * 3600  # the outlook paragraph covers the next day
# A current report older than this isn't worth narrating (the narrator's
# WEATHER_STALE_S reasoning): if the station has been unreachable this long,
# skip the broadcast rather than narrate yesterday's rain.
REPORT_MAX_AGE_S = 30 * 60
# A segment is longer than a narrator line (conditions + outlook + a look at
# the days ahead, issue #60), so it gets more headroom than narrate()'s 120;
# the temperature keeps two half-hour broadcasts over the same numbers from
# reading like reruns.
REPORT_NUM_PREDICT = 280
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


# --- the station (issue #51) --------------------------------------------------
# The GW2000B's live-data JSON keys everything by opaque ids and wraps every
# number in a unit-suffixed string ("0.00 in/Hr", "46%"). The maps below are
# the Rosetta stone, verified against the real device; an id the firmware
# stops sending simply parses to None, same posture as a missing OWM section.

def ecowitt_base():
    """Gateway base URL from MERLE_ECOWITT ("host" or "host:port").
    REQUIRED -- the station is the system of truth, so a service that can't
    name it fails at startup (the MERLE_MQTT philosophy)."""
    raw = os.environ.get("MERLE_ECOWITT", "").strip()
    if not raw:
        raise RuntimeError(
            "MERLE_ECOWITT is not set. Point it at the Ecowitt gateway "
            "(\"host\" or \"host:port\", e.g. 192.168.1.210) before starting "
            "the weather service."
        )
    return f"http://{raw}"


# common_list ids -> bus field names (units ride in the names, the house rule)
STATION_COMMON_IDS = {
    "0x02": "temp_f",
    "3": "feels_like_f",             # the station computes its own feels-like
    "0x03": "dew_point_f",
    "5": "vpd_inhg",                 # vapor pressure deficit
    "0x07": "humidity_pct",
    "0x0B": "wind_mph",
    "0x0C": "wind_gust_mph",
    "0x19": "wind_max_daily_gust_mph",
    "0x0A": "wind_deg",
    "0x15": "solar_wm2",
    "0x17": "uv_index",
}

# piezoRain ids -> bus field names ("srain_piezo" is the raining-right-now bit)
STATION_RAIN_IDS = {
    "srain_piezo": "raining",
    "0x0D": "rain_event_in",
    "0x0E": "rain_rate_inhr",
    "0x10": "rain_day_in",
    "0x11": "rain_week_in",
    "0x12": "rain_month_in",
    "0x13": "rain_year_in",
}

# The four fields the station cannot measure -- OpenWeather's remaining
# real-time job, merged into the payload as garnish.
GARNISH_FIELDS = ("condition", "description", "sunrise", "sunset")


def station_num(val):
    """One gateway value string -> float, or None. The firmware suffixes
    units ("29.24 inHg", "0.00 in/Hr") and percent signs ("46%"); the number
    is always the leading token."""
    if val is None:
        return None
    token = str(val).split()[0].rstrip("%") if str(val).split() else ""
    try:
        return float(token)
    except ValueError:
        return None


def parse_station(raw, ts):
    """Gateway /get_livedata_info response -> the measured half of the
    weather/current payload, every field a float or None. `ts` is injected
    (our fetch time -- the station has no observation clock). None when no
    measured field parsed at all: an empty shell is a failed poll, not a
    report of forty Nones."""
    common = {e.get("id"): e.get("val") for e in raw.get("common_list") or []}
    rain = {e.get("id"): e for e in raw.get("piezoRain") or []}
    wh25 = (raw.get("wh25") or [{}])[0]

    out = {field: station_num(common.get(id_))
           for id_, field in STATION_COMMON_IDS.items()}
    for id_, field in STATION_RAIN_IDS.items():
        out[field] = station_num((rain.get(id_) or {}).get("val"))
    # the raining flag is a bit, not a measurement
    if out["raining"] is not None:
        out["raining"] = int(out["raining"])

    # the WH90's battery/voltage ride the yearly-rain entry, of all places
    batt = rain.get("0x13") or {}
    battery = station_num(batt.get("battery"))
    out["station_battery"] = int(battery) if battery is not None else None
    out["station_voltage"] = station_num(batt.get("voltage"))

    # the gateway's own WH25: indoor climate + the barometer (pressure is
    # sky, not room -- it groups with outdoor on every consumer)
    out["indoor_temp_f"] = station_num(wh25.get("intemp"))
    out["indoor_humidity_pct"] = station_num(wh25.get("inhumi"))
    out["pressure_abs_inhg"] = station_num(wh25.get("abs"))
    out["pressure_rel_inhg"] = station_num(wh25.get("rel"))

    if all(v is None for v in out.values()):
        return None
    out["ts"] = ts
    return out


def parse_sensors(raw):
    """Gateway /get_sensors_info response -> the WH90's radio-link health
    ({station_signal: 0-4}, or None value when the roster doesn't list a
    registered WH90). Battery lives in the live data; signal only here."""
    for entry in raw if isinstance(raw, list) else []:
        if entry.get("img") == "wh90" and entry.get("idst") == "1":
            signal = station_num(entry.get("signal"))
            return {"station_signal": int(signal) if signal is not None
                    else None}
    return {"station_signal": None}


def merge_current(station, garnish, signal):
    """One weather/current payload: the station's measurements, OWM's sky
    garnish (Nones until the first garnish fetch lands), the radio-link
    health. The station half always wins -- measured fields NEVER come from
    OpenWeather."""
    current = dict(station)
    for field in GARNISH_FIELDS:
        current[field] = (garnish or {}).get(field)
    current.update(signal or {"station_signal": None})
    return current


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
    compact point per step. `pop` is precipitation probability 0..1.
    `rain_rate_inhr` (issue #56) is the step's RAIN volume -- rain["3h"] mm,
    omitted entirely on dry steps -- as an average in/hr rate, so it shares
    the rain strip's scale with the station's observed piezo trail.
    `snow_3h_in` (issue #65, which un-summed #56's rain+snow) is the step's
    snow volume as plain inches -- its own strip charts it per step, and no
    station instrument shares a scale with it (the piezo is snow-blind).
    Both default 0, not None: a step the forecast left dry is a real
    forecast of zero, unlike a station gap (which stays an honest hole in
    the observed trail)."""
    points = []
    for step in raw.get("list") or []:
        main = step.get("main") or {}
        wind = step.get("wind") or {}
        weather = (step.get("weather") or [{}])[0]
        rain_mm = (step.get("rain") or {}).get("3h") or 0
        snow_mm = (step.get("snow") or {}).get("3h") or 0
        points.append({
            "ts": step.get("dt"),
            "temp_f": main.get("temp"),
            "wind_mph": wind.get("speed"),
            "wind_gust_mph": wind.get("gust"),
            "condition": weather.get("main"),
            "pop": step.get("pop", 0),
            "rain_rate_inhr": round(rain_mm / 25.4 / 3, 4),
            "snow_3h_in": round(snow_mm / 25.4, 4),
        })
    return {"points": points}


# What the rolling window keeps per observation: what the charts draw, not
# the full report. Grew from 5 fields to 12 with the station (issue #51) --
# pressure, rain, solar and company are exactly what the big chart is FOR.
HISTORY_FIELDS = (
    "ts", "temp_f", "wind_mph", "wind_gust_mph", "condition",
    "humidity_pct", "dew_point_f", "pressure_rel_inhg",
    "rain_rate_inhr", "rain_day_in", "solar_wm2", "uv_index",
)


def history_point(current):
    """The compact per-observation record the rolling window keeps. .get():
    a pre-#51 payload replayed through here simply lacks the new fields."""
    return {k: current.get(k) for k in HISTORY_FIELDS}


def should_record(window, point, step_s=HISTORY_STEP_S):
    """Whether the trail wants this observation: polls run every 60s, the
    window keeps 5-minute resolution (~576 points over 48h -- fresh enough
    to chart, bounded enough to republish whole). Age is measured from the
    newest kept point, never the wall clock (the roll_history rule)."""
    if point.get("ts") is None:
        return False
    newest = max((p["ts"] for p in window if p.get("ts") is not None),
                 default=None)
    return newest is None or point["ts"] - newest >= step_s


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
    "Deliver your on-air weather segment in ONE or TWO short paragraphs, five "
    "to eight sentences in all: current conditions first, then what's on the "
    "way. When you are given the days ahead, close with a sentence or two "
    "about them -- pick out what matters (the hot one, the wet one, the one "
    "for gathering acorns), never recite the whole list. Use the "
    "measurements you are given -- never invent numbers. Spoken words only: "
    "no stage directions, no quotation marks, no emoji, no markdown, no "
    "preamble, no sign-off name. Never break character."
)

# Labeled paragraphs, the narrator's desk-tested prompt shape (issue #26):
# bare facts buried in prose get ignored; labeled blocks get woven in.
REPORT_CURRENT_HEADER = "Current conditions at the station:"
REPORT_OUTLOOK_HEADER = "The next 24 hours:"
REPORT_EXTENDED_HEADER = "The days ahead:"


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
    # The station's extras (issue #51), each only when measured -- Willard
    # never narrates a hole. Dew point is the mugginess number, rain is news
    # the moment it falls, UV matters when there IS sun to speak of, and the
    # barometer is the oldest trick in the weather desk's book.
    if current.get("dew_point_f") is not None:
        parts.append(f"Dew point {round(current['dew_point_f'])}F.")
    if current.get("raining"):
        rate = current.get("rain_rate_inhr")
        parts.append("It is raining right now"
                     + (f", {rate:.2f} inches an hour" if rate else "")
                     + ".")
    rain_day = current.get("rain_day_in")
    if rain_day:
        parts.append(f"Rainfall today: {rain_day:.2f} inches.")
    uv = current.get("uv_index")
    if uv is not None and uv >= 1:
        parts.append(f"UV index {round(uv)}.")
    if current.get("pressure_rel_inhg") is not None:
        parts.append(
            f"Barometer {current['pressure_rel_inhg']:.2f} inches.")
    if current.get("sunrise") is not None and current.get("sunset") is not None:
        parts.append(f"Sunrise {clock_12h(current['sunrise'])}, "
                     f"sunset {clock_12h(current['sunset'])}.")
    return " ".join(parts)


def _digest(steps):
    """Aggregate one window of forecast steps into what an outlook sentence
    needs: temperature span, strongest wind or gust, peak precipitation
    chance, expected rainfall, and the conditions in order of appearance.
    None when nothing in the window carries a temperature. Two callers --
    the next-24h outlook and the per-day extended digest (issue #60)."""
    temps = [p["temp_f"] for p in steps if p.get("temp_f") is not None]
    if not temps:
        return None
    winds = [w for p in steps
             for w in (p.get("wind_mph"), p.get("wind_gust_mph"))
             if w is not None]
    conditions = []
    for p in steps:
        if p.get("condition") and p["condition"] not in conditions:
            conditions.append(p["condition"])
    return {
        "high_f": max(temps),
        "low_f": min(temps),
        "max_wind_mph": max(winds) if winds else None,
        "max_pop": max((p.get("pop") or 0) for p in steps),
        # each step's rate (issue #56) is an average over its 3 hours, so
        # rate x 3 is the step's volume and the sum is the window's expected
        # rainfall; `or 0` rides through pre-#56 payloads without a field.
        # Snow (issue #65) is its own ledger -- the fields were un-summed,
        # and Willard should say "two inches of snow", never fold it into
        # the rain number.
        "rain_total_in": round(sum((p.get("rain_rate_inhr") or 0) * 3
                                   for p in steps), 2),
        "snow_total_in": round(sum((p.get("snow_3h_in") or 0)
                                   for p in steps), 2),
        "conditions": conditions,
    }


def forecast_digest(points, now, horizon_s=REPORT_HORIZON_S):
    """The next-24h outlook: boil the 3-hour forecast steps ahead of `now`
    down to what one paragraph needs. None when no temperature-bearing point
    falls inside the horizon."""
    return _digest([p for p in points
                    if p.get("ts") is not None
                    and now < p["ts"] <= now + horizon_s])


def local_day(ts):
    """Epoch seconds -> local calendar-day key (the service runs where the
    station is, the clock_12h reasoning)."""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def extended_digest(points, now):
    """The days ahead (issue #60): one digest per LOCAL calendar day after
    the day holding `now`, [(weekday_name, digest), ...] in order -- the
    whole 5-day series the free API publishes, not just the outlook's 24h.
    Tomorrow overlaps the rolling 24h outlook on purpose: that's how a
    broadcast desk talks, today in detail and the week at a glance."""
    by_day = {}
    for p in points:
        ts = p.get("ts")
        if ts is None or ts <= now or local_day(ts) == local_day(now):
            continue
        by_day.setdefault(local_day(ts), []).append(p)
    out = []
    for day in sorted(by_day):
        d = _digest(by_day[day])
        if d:
            name = time.strftime("%A", time.localtime(by_day[day][0]["ts"]))
            out.append((name, d))
    return out


def outlook_facts(digest):
    """The forecast digest as the same dry-paragraph shape as current_facts."""
    parts = [f"Temperatures from {round(digest['low_f'])}F "
             f"to {round(digest['high_f'])}F."]
    if digest["max_wind_mph"] is not None:
        parts.append(f"Winds up to {round(digest['max_wind_mph'])} mph.")
    if digest["max_pop"] > 0:
        parts.append("Chance of precipitation up to "
                     f"{round(digest['max_pop'] * 100)} percent.")
    if digest["rain_total_in"] >= 0.01:
        parts.append(f"Expected rainfall about "
                     f"{digest['rain_total_in']:.2f} inches.")
    if digest["snow_total_in"] >= 0.01:
        parts.append(f"Expected snow about "
                     f"{digest['snow_total_in']:.1f} inches.")
    if digest["conditions"]:
        parts.append("Conditions: "
                     + ", ".join(c.lower() for c in digest["conditions"]) + ".")
    return " ".join(parts)


def extended_facts(days):
    """The per-day digests as one dry paragraph, weekday-labeled and terser
    than the 24h outlook -- raw material for a sentence or two of week-ahead
    color, not five mini-reports."""
    parts = []
    for name, d in days:
        bits = [f"{name}: {round(d['low_f'])}F to {round(d['high_f'])}F"]
        if d["max_pop"] > 0:
            bits.append("precipitation chance "
                        f"{round(d['max_pop'] * 100)} percent")
        if d["rain_total_in"] >= 0.01:
            bits.append(f"about {d['rain_total_in']:.2f} inches of rain")
        if d["snow_total_in"] >= 0.01:
            bits.append(f"about {d['snow_total_in']:.1f} inches of snow")
        if d["conditions"]:
            bits.append(", ".join(c.lower() for c in d["conditions"]))
        parts.append("; ".join(bits) + ".")
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
    extended = extended_digest(forecast_points, now)
    if extended:
        parts.append(f"{REPORT_EXTENDED_HEADER} {extended_facts(extended)}")
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
    station_base = ecowitt_base()
    key = owm_key()
    loc = location_params(os.environ.get("MERLE_WEATHER_LOC"))
    history_path = os.environ.get("MERLE_WEATHER_HISTORY", "").strip() \
        or DEFAULT_HISTORY_PATH
    livedata_url = f"{station_base}/get_livedata_info"
    sensors_url = f"{station_base}/get_sensors_info?page=1"
    garnish_url = owm_url("weather", loc, key)
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
    print(f"[weather] on duty: station={station_base}, loc={loc}, "
          f"{len(window)} points of history, "
          f"polling every {STATION_INTERVAL_S} s")

    current = None           # the freshest observed report, for the segment
    garnish = None           # OWM's sky/sun fields, merged into every current
    signal = None            # the WH90's radio-link health, sensors-info pace
    forecast_points = []     # the freshest forecast series, for the outlook
    next_garnish_at = 0.0    # 0 -> the first loop pass fetches everything
    next_forecast_at = 0.0
    next_report_at = 0.0     # 0 -> Willard goes on the air on the first pass
    try:
        while True:
            # The garnish rides the OLD current cadence (10 min): condition
            # text and sun times drift slowly, and OWM's free tier deserves
            # mercy. Sensors-info (radio signal) is a LAN call but changes
            # just as slowly, so it shares the timer. On failure the timer
            # stays put and the next 60s pass retries (the forecast pattern).
            if time.time() >= next_garnish_at:
                raw = fetch(garnish_url)
                raw_sensors = fetch(sensors_url)
                if raw_sensors is not None:
                    signal = parse_sensors(raw_sensors)
                if raw is not None:
                    garnish = parse_current(raw)
                    next_garnish_at = time.time() + GARNISH_INTERVAL_S

            raw = fetch(livedata_url)
            station = parse_station(raw, int(time.time())) \
                if raw is not None else None
            if station is not None:
                current = merge_current(station, garnish, signal)
                publisher.publish(bus.WEATHER_CURRENT_TOPIC, current,
                                  retain=True)
                point = history_point(current)
                if should_record(window, point):
                    window = roll_history(window, point)
                    save_history(history_path, window)
                    publisher.publish(bus.WEATHER_HISTORY_TOPIC,
                                      {"points": window}, retain=True)
                print(f"[weather] {current['temp_f']}F, "
                      f"wind {current['wind_mph']} mph, "
                      f"rain today {current['rain_day_in']} in "
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
                # station-poll pass retries the forecast instead of waiting
                # out a full hour

            if ollama is not None and time.time() >= next_report_at:
                # Blocking (up to OLLAMA_TIMEOUT_S) is fine here: this is the
                # main loop's own thread, not paho's, and a delayed station
                # poll costs one 60s beat, not a report. Retained, like the
                # rest of the weather set -- a fresh dashboard tab paints the
                # segment instantly.
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
                # put -- the forecast-retry pattern: the next 60s pass
                # tries again instead of waiting out the half hour

            time.sleep(STATION_INTERVAL_S)
    except KeyboardInterrupt:
        # Manual desk runs: sign off cleanly (close() publishes the retained
        # offline). Under systemd this never runs -- SIGTERM fires the will.
        publisher.close()
        print("\n[weather] off duty.")


if __name__ == "__main__":
    main()
