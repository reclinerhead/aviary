# =============================================================================
# project-squirrel -- test_weather.py
#
# Pure logic of the weather service: config parsing, OpenWeather response ->
# bus payload mapping, and the rolling history window. The fetch loop and the
# MQTT plumbing are I/O and get desk-tested against the real services (the
# testing policy: cover the deterministic parts, not the boundary).
#
# The OWM fixtures are trimmed-down but structurally faithful copies of real
# classic-API responses -- if OpenWeather renames a field, these tests keep
# passing (they pin OUR mapping, not their API); the desk test catches that.
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
        },
        {
            "dt": 1752170400,
            "main": {"temp": 74.5, "humidity": 70},
            "wind": {"speed": 5.0, "deg": 200},
            "weather": [{"main": "Clear", "description": "clear sky"}],
        },
    ],
}


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
    assert first == {"ts": 1752159600, "temp_f": 81.0, "wind_mph": 10.1,
                     "wind_gust_mph": 19.9, "condition": "Rain", "pop": 0.4}
    # calm step: no gust key, no pop key -> None gust, 0 pop
    assert second["wind_gust_mph"] is None
    assert second["pop"] == 0


def test_parse_forecast_empty_response():
    assert weather.parse_forecast({}) == {"points": []}


def test_history_point_is_the_compact_subset():
    got = weather.history_point(weather.parse_current(OWM_CURRENT))
    assert got == {"ts": 1752148800, "temp_f": 78.3, "wind_mph": 8.5,
                   "wind_gust_mph": 17.2, "condition": "Clouds"}


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
