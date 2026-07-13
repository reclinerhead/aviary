# =============================================================================
# project-squirrel -- test_weather.py
#
# Pure logic of the weather service: config parsing, station and OpenWeather
# response -> bus payload mapping, and the rolling history window. The fetch
# loop and the MQTT plumbing are I/O and get desk-tested against the real
# services (the testing policy: cover the deterministic parts, not the
# boundary).
#
# The fixtures are trimmed-down but structurally faithful copies of real
# responses -- the Ecowitt one is lifted from the actual GW2000B on the LAN,
# unit-suffixed strings and opaque ids and all. If a provider renames a
# field, these tests keep passing (they pin OUR mapping, not their API); the
# desk test catches that.
# =============================================================================

import json

import pytest

import weather

# --- fixtures ----------------------------------------------------------------

OWM_CURRENT = {
    "dt": 1752148800,
    "main": {"temp": 78.3, "feels_like": 79.1, "humidity": 62,
             "pressure": 1014},
    "wind": {"speed": 8.5, "deg": 240, "gust": 17.2},
    "weather": [{"id": 803, "main": "Clouds",
                 "description": "broken clouds", "icon": "04d"}],
    "sys": {"sunrise": 1752116400, "sunset": 1752170700, "country": "US"},
    "name": "Kalamazoo",
}

OWM_FORECAST = {
    "cnt": 2,
    "list": [
        {
            "dt": 1752159600,
            "main": {"temp": 81.0, "feels_like": 83.2, "humidity": 55},
            "wind": {"speed": 10.1, "deg": 250, "gust": 19.9},
            "weather": [{"main": "Rain", "description": "light rain"}],
            "pop": 0.4,
            "rain": {"3h": 7.62},
        },
        {
            "dt": 1752170400,
            "main": {"temp": 74.5, "humidity": 70},
            "wind": {"speed": 5.0, "deg": 200},
            "weather": [{"main": "Clear", "description": "clear sky"}],
        },
    ],
}


# A structurally faithful /get_livedata_info, values verbatim from the real
# GW2000B (2026-07-12, the sensor pod on the workbench).
ECOWITT_LIVE = {
    "common_list": [
        {"id": "0x02", "val": "78.8", "unit": "F"},
        {"id": "0x07", "val": "46%"},
        {"id": "3", "val": "78.8", "unit": "F"},
        {"id": "5", "val": "0.536 inHg"},
        {"id": "0x03", "val": "56.3", "unit": "F"},
        {"id": "0x0B", "val": "0.00 mph"},
        {"id": "0x0C", "val": "0.00 mph"},
        {"id": "0x19", "val": "1.79 mph"},
        {"id": "0x15", "val": "0.63 W/m2"},
        {"id": "0x17", "val": "0"},
        {"id": "0x0A", "val": "224"},
    ],
    "piezoRain": [
        {"id": "srain_piezo", "val": "0"},
        {"id": "0x0D", "val": "0.00 in"},
        {"id": "0x0E", "val": "0.12 in/Hr"},
        {"id": "0x10", "val": "0.25 in"},
        {"id": "0x11", "val": "0.25 in"},
        {"id": "0x12", "val": "1.50 in"},
        {"id": "0x13", "val": "12.00 in", "battery": "5", "voltage": "3.20"},
    ],
    "wh25": [{"intemp": "72.9", "unit": "F", "inhumi": "55%",
              "abs": "29.24 inHg", "rel": "29.24 inHg"}],
    "debug": [{"heap": "100000", "runtime": "1176"}],
}

# A trimmed /get_sensors_info roster: one registered WH90, one unregistered
# slot (id FFFFFFFF, the firmware's "nothing paired here").
ECOWITT_SENSORS = [
    {"img": "wh85", "type": "49", "name": "Wind & Rain",
     "id": "FFFFFFFF", "batt": "9", "signal": "0", "idst": "1"},
    {"img": "wh90", "type": "48", "name": "Temp & Humidity & Solar & Wind & Rain",
     "id": "12F9D", "batt": "5", "signal": "4", "idst": "1"},
    {"img": "wh25", "type": "4", "name": "Temp & Humidity & Pressure",
     "id": "FFFFFFFE", "batt": "9", "signal": "0", "idst": "0"},
]


def pt(ts, temp=70.0):
    return {"ts": ts, "temp_f": temp, "wind_mph": 5.0,
            "wind_gust_mph": None, "condition": "Clear"}


# --- config ------------------------------------------------------------------

def test_owm_key_missing_raises(monkeypatch):
    monkeypatch.delenv("MERLE_OWM_KEY", raising=False)
    with pytest.raises(RuntimeError, match="MERLE_OWM_KEY"):
        weather.owm_key()


def test_owm_key_blank_raises(monkeypatch):
    monkeypatch.setenv("MERLE_OWM_KEY", "   ")
    with pytest.raises(RuntimeError, match="MERLE_OWM_KEY"):
        weather.owm_key()


def test_location_bare_zip_defaults_us():
    assert weather.location_params("49001") == {"zip": "49001,US"}


def test_location_zip_with_country():
    assert weather.location_params("49001,US") == {"zip": "49001,US"}


def test_location_lat_lon():
    assert weather.location_params("42.29,-85.59") == \
        {"lat": "42.29", "lon": "-85.59"}


def test_location_empty_falls_back_to_kalamazoo():
    assert weather.location_params(None) == {"zip": "49001,US"}
    assert weather.location_params("  ") == {"zip": "49001,US"}


def test_owm_url_carries_key_units_and_location():
    url = weather.owm_url("weather", {"zip": "49001,US"}, "SECRET")
    assert url.startswith(f"{weather.OWM_BASE}/weather?")
    assert "zip=49001%2CUS" in url
    assert "appid=SECRET" in url
    assert "units=imperial" in url


def test_ecowitt_base_missing_raises(monkeypatch):
    monkeypatch.delenv("MERLE_ECOWITT", raising=False)
    with pytest.raises(RuntimeError, match="MERLE_ECOWITT"):
        weather.ecowitt_base()


def test_ecowitt_base_host_and_port(monkeypatch):
    monkeypatch.setenv("MERLE_ECOWITT", "192.168.1.210")
    assert weather.ecowitt_base() == "http://192.168.1.210"
    monkeypatch.setenv("MERLE_ECOWITT", "gw2000.local:8080")
    assert weather.ecowitt_base() == "http://gw2000.local:8080"


# --- station parsing (issue #51) -------------------------------------------------

def test_station_num_strips_units_and_percent():
    assert weather.station_num("78.8") == 78.8
    assert weather.station_num("46%") == 46.0
    assert weather.station_num("0.12 in/Hr") == 0.12
    assert weather.station_num("29.24 inHg") == 29.24
    assert weather.station_num("0.63 W/m2") == 0.63


def test_station_num_junk_is_none():
    assert weather.station_num(None) is None
    assert weather.station_num("") is None
    assert weather.station_num("--") is None
    assert weather.station_num("  ") is None


def test_parse_station_maps_the_live_data():
    got = weather.parse_station(ECOWITT_LIVE, ts=1752300000)
    assert got == {
        "ts": 1752300000,
        "temp_f": 78.8,
        "feels_like_f": 78.8,
        "dew_point_f": 56.3,
        "vpd_inhg": 0.536,
        "humidity_pct": 46.0,
        "wind_mph": 0.0,
        "wind_gust_mph": 0.0,
        "wind_max_daily_gust_mph": 1.79,
        "wind_deg": 224.0,
        "solar_wm2": 0.63,
        "uv_index": 0.0,
        "raining": 0,
        "rain_event_in": 0.0,
        "rain_rate_inhr": 0.12,
        "rain_day_in": 0.25,
        "rain_week_in": 0.25,
        "rain_month_in": 1.5,
        "rain_year_in": 12.0,
        "station_battery": 5,
        "station_voltage": 3.2,
        "indoor_temp_f": 72.9,
        "indoor_humidity_pct": 55.0,
        "pressure_abs_inhg": 29.24,
        "pressure_rel_inhg": 29.24,
    }


def test_parse_station_tolerates_missing_sections():
    got = weather.parse_station(
        {"common_list": [{"id": "0x02", "val": "70.0", "unit": "F"}]},
        ts=100)
    assert got["temp_f"] == 70.0
    assert got["rain_day_in"] is None
    assert got["indoor_temp_f"] is None
    assert got["station_battery"] is None


def test_parse_station_all_junk_is_a_failed_poll():
    assert weather.parse_station({}, ts=100) is None
    assert weather.parse_station({"common_list": [], "piezoRain": [],
                                  "wh25": []}, ts=100) is None
    assert weather.parse_station(
        {"common_list": [{"id": "0x02", "val": "--"}]}, ts=100) is None


def test_parse_station_raining_is_a_bit():
    live = {"piezoRain": [{"id": "srain_piezo", "val": "1"}]}
    assert weather.parse_station(live, ts=100)["raining"] == 1


def test_parse_sensors_reads_the_wh90_signal():
    assert weather.parse_sensors(ECOWITT_SENSORS) == {"station_signal": 4}


def test_parse_sensors_no_registered_wh90_is_none():
    assert weather.parse_sensors([]) == {"station_signal": None}
    assert weather.parse_sensors("garbage") == {"station_signal": None}
    unregistered = [{"img": "wh90", "id": "FFFFFFFF", "signal": "0",
                     "idst": "0"}]
    assert weather.parse_sensors(unregistered) == {"station_signal": None}


def test_merge_current_station_wins_garnish_rides():
    station = weather.parse_station(ECOWITT_LIVE, ts=1752300000)
    garnish = weather.parse_current(OWM_CURRENT)   # temp 78.3 -- must NOT win
    got = weather.merge_current(station, garnish, {"station_signal": 4})
    assert got["temp_f"] == 78.8              # the station's number
    assert got["condition"] == "Clouds"       # OWM's garnish
    assert got["description"] == "broken clouds"
    assert got["sunrise"] == 1752116400
    assert got["sunset"] == 1752170700
    assert got["station_signal"] == 4
    assert got["ts"] == 1752300000            # fetch time, not OWM's dt


def test_merge_current_before_first_garnish_or_roster():
    station = weather.parse_station(ECOWITT_LIVE, ts=100)
    got = weather.merge_current(station, None, None)
    assert got["condition"] is None
    assert got["sunrise"] is None
    assert got["station_signal"] is None


# --- response parsing ----------------------------------------------------------

def test_parse_current_maps_the_report():
    got = weather.parse_current(OWM_CURRENT)
    assert got == {
        "ts": 1752148800,
        "temp_f": 78.3,
        "feels_like_f": 79.1,
        "humidity_pct": 62,
        "wind_mph": 8.5,
        "wind_gust_mph": 17.2,
        "wind_deg": 240,
        "condition": "Clouds",
        "description": "broken clouds",
        "sunrise": 1752116400,
        "sunset": 1752170700,
    }


def test_parse_current_tolerates_missing_sections():
    got = weather.parse_current({"dt": 5})
    assert got["ts"] == 5
    assert got["temp_f"] is None
    assert got["wind_gust_mph"] is None
    assert got["condition"] is None


def test_parse_forecast_shapes_chart_points():
    got = weather.parse_forecast(OWM_FORECAST)
    assert [p["ts"] for p in got["points"]] == [1752159600, 1752170400]
    first, second = got["points"]
    # 7.62 mm over the 3h step = 0.3 in total = 0.1 in/hr average (issue #56)
    assert first == {"ts": 1752159600, "temp_f": 81.0, "wind_mph": 10.1,
                     "wind_gust_mph": 19.9, "condition": "Rain", "pop": 0.4,
                     "rain_rate_inhr": 0.1}
    # calm step: no gust key, no pop key, no rain/snow -> None gust, 0 pop,
    # 0 rate (a dry forecast step is a real zero, not a gap)
    assert second["wind_gust_mph"] is None
    assert second["pop"] == 0
    assert second["rain_rate_inhr"] == 0


def test_parse_forecast_sums_rain_and_snow():
    # wintry mix: both volumes are water-equivalent mm, so they add
    got = weather.parse_forecast({"list": [
        {"dt": 100, "rain": {"3h": 3.81}, "snow": {"3h": 3.81}},
    ]})
    assert got["points"][0]["rain_rate_inhr"] == 0.1


def test_parse_forecast_empty_response():
    assert weather.parse_forecast({}) == {"points": []}


def test_history_point_is_the_charted_subset():
    station = weather.parse_station(ECOWITT_LIVE, ts=1752300000)
    got = weather.history_point(
        weather.merge_current(station, weather.parse_current(OWM_CURRENT),
                              {"station_signal": 4}))
    assert set(got) == set(weather.HISTORY_FIELDS)
    assert got["ts"] == 1752300000
    assert got["temp_f"] == 78.8
    assert got["pressure_rel_inhg"] == 29.24
    assert got["rain_day_in"] == 0.25
    assert got["condition"] == "Clouds"
    # the full report's extras stay out of the trail
    assert "station_signal" not in got
    assert "indoor_temp_f" not in got


def test_history_point_tolerates_old_payloads():
    # a pre-#51 payload replayed through a restart lacks the new fields
    got = weather.history_point({"ts": 5, "temp_f": 70.0})
    assert got["ts"] == 5
    assert got["pressure_rel_inhg"] is None


def test_should_record_keeps_five_minute_resolution():
    assert weather.should_record([], pt(1000)) is True
    window = [pt(1000)]
    assert weather.should_record(window, pt(1000 + 60)) is False
    assert weather.should_record(window, pt(1000 + weather.HISTORY_STEP_S)) \
        is True


def test_should_record_ignores_tsless_points():
    assert weather.should_record([], {"ts": None}) is False
    # a corrupt window entry without a ts doesn't crash the newest-point scan
    assert weather.should_record([{"ts": None}], pt(1000)) is True


# --- the rolling window --------------------------------------------------------

def test_roll_history_appends_in_ts_order():
    window = weather.roll_history([pt(100)], pt(200))
    assert [p["ts"] for p in window] == [100, 200]


def test_roll_history_prunes_beyond_window():
    old, recent, new = pt(0), pt(500), pt(1000)
    window = weather.roll_history([old, recent], new, max_age_s=600)
    assert [p["ts"] for p in window] == [500, 1000]


def test_roll_history_age_measured_from_newest_not_wall_clock():
    # a 2019 archive rolled with a 2019 point keeps 2019 data -- deterministic
    window = weather.roll_history([pt(1_500_000_000)], pt(1_500_000_100),
                                  max_age_s=600)
    assert len(window) == 2


def test_roll_history_dedupes_same_ts():
    # restart re-fetches an observation the persisted file already holds;
    # the fresh copy wins
    window = weather.roll_history([pt(100, temp=70.0)], pt(100, temp=71.0))
    assert len(window) == 1
    assert window[0]["temp_f"] == 71.0


def test_roll_history_ignores_tsless_point():
    window = weather.roll_history([pt(100)], {"ts": None, "temp_f": 60.0})
    assert [p["ts"] for p in window] == [100]


# --- persistence ---------------------------------------------------------------

def test_history_round_trip(tmp_path):
    path = tmp_path / "history.json"
    window = [pt(100), pt(200)]
    weather.save_history(path, window)
    assert weather.load_history(path) == window


def test_load_history_missing_file(tmp_path):
    assert weather.load_history(tmp_path / "nope.json") == []


def test_load_history_corrupt_file(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("{not json", encoding="utf-8")
    assert weather.load_history(path) == []


def test_load_history_wrong_shape(tmp_path):
    path = tmp_path / "history.json"
    path.write_text(json.dumps({"points": []}), encoding="utf-8")
    assert weather.load_history(path) == []


# --- willard's on-air segment (issue #45) --------------------------------------
# Pure prompt assembly and sanitation only; the live Ollama call is I/O and
# stays desk-tested (the narrator's testing policy). Sun-time strings render
# in the machine's local timezone, so assertions check presence, never the
# clock face.

NOW = 1_000_000


def fpt(ts, temp=70.0, wind=5.0, gust=None, cond="Clear", pop=0):
    return {"ts": ts, "temp_f": temp, "wind_mph": wind, "wind_gust_mph": gust,
            "condition": cond, "pop": pop}


def fresh_current(**over):
    cur = weather.parse_current(OWM_CURRENT)
    cur["ts"] = NOW - 60
    cur.update(over)
    return cur


class StubOllama:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete(self, system, prompt, num_predict=None, temperature=None):
        self.calls.append({"system": system, "prompt": prompt,
                           "num_predict": num_predict,
                           "temperature": temperature})
        return self.reply


def test_forecast_digest_clips_to_horizon():
    points = [fpt(NOW - 100, temp=10.0),                        # behind now
              fpt(NOW + 3600, temp=60.0),
              fpt(NOW + weather.REPORT_HORIZON_S, temp=80.0),   # boundary: in
              fpt(NOW + weather.REPORT_HORIZON_S + 1, temp=99.0)]
    digest = weather.forecast_digest(points, NOW)
    assert digest["low_f"] == 60.0
    assert digest["high_f"] == 80.0


def test_forecast_digest_wind_pop_conditions():
    points = [fpt(NOW + 3600, wind=5.0, gust=19.9, cond="Rain", pop=0.4),
              fpt(NOW + 7200, wind=12.0, cond="Clear"),
              fpt(NOW + 10800, cond="Rain", pop=0.1)]
    digest = weather.forecast_digest(points, NOW)
    assert digest["max_wind_mph"] == 19.9   # the gust beats every speed
    assert digest["max_pop"] == 0.4
    assert digest["conditions"] == ["Rain", "Clear"]   # order of appearance


def test_forecast_digest_nothing_ahead():
    assert weather.forecast_digest([], NOW) is None
    assert weather.forecast_digest([fpt(NOW - 100)], NOW) is None
    # points in the window but none carrying a temperature
    assert weather.forecast_digest([fpt(NOW + 3600, temp=None)], NOW) is None


def test_current_facts_reads_the_report():
    facts = weather.current_facts(fresh_current())
    assert "It is 78F, feels like 79F." in facts
    assert "Sky: broken clouds." in facts
    assert "Wind 8 mph, gusting to 17." in facts
    assert "Humidity 62 percent." in facts
    assert "Sunrise " in facts and "sunset " in facts


def test_current_facts_skips_matching_feels_like():
    facts = weather.current_facts(fresh_current(feels_like_f=78.4))
    assert "feels like" not in facts


def test_current_facts_no_temp_is_no_broadcast():
    assert weather.current_facts(fresh_current(temp_f=None)) == ""


def test_current_facts_station_extras():
    facts = weather.current_facts(fresh_current(
        dew_point_f=56.3, raining=1, rain_rate_inhr=0.12, rain_day_in=0.25,
        uv_index=7.0, pressure_rel_inhg=29.24))
    assert "Dew point 56F." in facts
    assert "It is raining right now, 0.12 inches an hour." in facts
    assert "Rainfall today: 0.25 inches." in facts
    assert "UV index 7." in facts
    assert "Barometer 29.24 inches." in facts


def test_current_facts_extras_skipped_when_absent():
    # an OWM-shaped report (no station fields) reads exactly as before
    facts = weather.current_facts(fresh_current())
    for word in ("Dew point", "raining", "Rainfall", "UV", "Barometer"):
        assert word not in facts


def test_current_facts_dry_day_and_zero_uv_stay_quiet():
    facts = weather.current_facts(fresh_current(
        raining=0, rain_rate_inhr=0.0, rain_day_in=0.0, uv_index=0.0))
    assert "raining" not in facts
    assert "Rainfall" not in facts
    assert "UV" not in facts


def test_build_report_prompt_labeled_paragraphs_then_cue():
    prompt = weather.build_report_prompt(
        fresh_current(), [fpt(NOW + 3600, temp=61.0, pop=0.4)], NOW)
    paras = prompt.split("\n\n")
    assert paras[0].startswith(weather.REPORT_CURRENT_HEADER)
    # the clock face is timezone-dependent; pin the label, not the time
    assert "The station clock reads " in paras[0]
    assert paras[1].startswith(weather.REPORT_OUTLOOK_HEADER)
    assert "Chance of precipitation up to 40 percent." in paras[1]
    assert paras[-1] == "Your on-air weather segment:"


def test_build_report_prompt_without_forecast_drops_the_outlook():
    prompt = weather.build_report_prompt(fresh_current(), [], NOW)
    assert weather.REPORT_OUTLOOK_HEADER not in prompt
    assert prompt.endswith("Your on-air weather segment:")


def test_build_report_prompt_stale_or_missing_current_is_none():
    stale = fresh_current(ts=NOW - weather.REPORT_MAX_AGE_S - 1)
    assert weather.build_report_prompt(stale, [], NOW) is None
    assert weather.build_report_prompt(None, [], NOW) is None
    assert weather.build_report_prompt(fresh_current(ts=None), [], NOW) is None


def test_report_system_prompt_is_persona_plus_rules():
    got = weather.report_system_prompt()
    assert got.startswith(weather.WILLARD_PERSONALITY)
    assert got.endswith(weather.REPORT_RULES)


def test_sanitize_report_strips_markdown_and_quotes():
    assert weather.sanitize_report('"**WELL** hello there!"') == \
        "WELL hello there!"


def test_sanitize_report_keeps_the_paragraph_break():
    got = weather.sanitize_report("Now the\nconditions.\n\nAnd the outlook.")
    assert got == "Now the conditions.\n\nAnd the outlook."


def test_sanitize_report_unusable_is_none():
    assert weather.sanitize_report("") is None
    assert weather.sanitize_report(None) is None
    assert weather.sanitize_report('  "" \n\n  ') is None


def test_broadcast_sends_the_prompts_and_sanitizes():
    stub = StubOllama('"A **gorgeous** day out there!"')
    got = weather.broadcast(stub, fresh_current(), [fpt(NOW + 3600)], now=NOW)
    assert got == "A gorgeous day out there!"
    call = stub.calls[0]
    assert call["system"] == weather.report_system_prompt()
    assert call["prompt"].startswith(weather.REPORT_CURRENT_HEADER)
    assert call["num_predict"] == weather.REPORT_NUM_PREDICT
    assert call["temperature"] == weather.REPORT_TEMPERATURE


def test_broadcast_dead_llm_is_none():
    assert weather.broadcast(StubOllama(None), fresh_current(), [],
                             now=NOW) is None


def test_broadcast_nothing_to_say_never_calls_the_llm():
    stub = StubOllama("unused")
    assert weather.broadcast(stub, None, [], now=NOW) is None
    assert stub.calls == []
