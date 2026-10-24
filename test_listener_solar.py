# =============================================================================
# project-squirrel -- test_listener_solar.py
#
# The outside judge for listener/solar.py (#614): published times for the
# house's own coordinates (the earl.db `locations` Home row), fetched from
# api.sunrise-sunset.org on 2026-10-24. A summer date with its sunset past
# 0h UTC, today, and the winter solstice in standard time. All expected
# values are UTC instants, so this passes on a box in any timezone.
# =============================================================================

import calendar
import time

import pytest

from listener import solar

HOME = (42.26, -85.57)


def utc(text):
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%S"))


PUBLISHED = [
    # (local date, sunrise UTC, sunset UTC)
    ((2026, 7, 18), "2026-07-18T10:22:01", "2026-07-19T01:15:08"),
    ((2026, 10, 24), "2026-10-24T12:05:58", "2026-10-24T22:46:55"),
    ((2026, 12, 21), "2026-12-21T13:07:49", "2026-12-21T22:13:07"),
]


@pytest.mark.parametrize("date,rise,set_", PUBLISHED)
def test_sunrise_and_sunset_match_the_published_times(date, rise, set_):
    assert abs(solar.sunrise(*HOME, *date) - utc(rise)) < 60
    assert abs(solar.sunset(*HOME, *date) - utc(set_)) < 60


def test_the_sun_that_never_rises_is_none():
    # Tromsø in midwinter: polar night, no crossing at all.
    assert solar.sunrise(69.65, 18.96, 2026, 12, 21) is None
    assert solar.sunset(69.65, 18.96, 2026, 6, 21) is None   # midnight sun
