# =============================================================================
# project-squirrel -- listener/solar.py
#
# Sunrise and sunset for one place and one date (issue #614): the frame a
# bird's day is actually lived in. The field notes used to bucket visits by
# CLOCK hour, and the clock is the wrong ruler for a dawn singer -- over the
# house record the sun moved from 6:22 to 8:06 EDT, so "busiest 7-10am" in
# July and in October are different moments for the bird. Measured against
# sunrise, the Northern Cardinal's first song sits ~17 minutes before it on
# nearly every morning of the record; against the clock it was invisible.
#
# The NOAA solar-position algorithm (the Global Monitoring Laboratory's
# spreadsheet formulas): solar declination and the equation of time from the
# Julian century, the hour angle at the standard -0.833 degree altitude
# (refraction + the sun's radius), refined once at the event's own time.
# Checked against api.sunrise-sunset.org for the house on three dates in
# test_listener_solar.py -- within a minute, where the single-pass formula
# this replaced ran two early.
#
# Epoch seconds in, epoch seconds out: no local-time arithmetic happens
# here, so DST and the CI box's timezone cannot touch it. Callers decide
# which local DATE a visit belongs to.
# =============================================================================

import calendar
import math

ZENITH = 90.833   # degrees: the sun's upper limb on a refracted horizon


def _julian_day(year, month, day):
    """Julian day at 0h UT of a Gregorian date."""
    return calendar.timegm((year, month, day, 0, 0, 0)) / 86400 + 2440587.5


def _sun(jc):
    """(equation of time in minutes, declination in radians) at Julian
    century `jc` -- the NOAA spreadsheet, term for term."""
    l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    mr = math.radians(m)
    c = (math.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
         + math.sin(2 * mr) * (0.019993 - 0.000101 * jc)
         + math.sin(3 * mr) * 0.000289)
    omega = math.radians(125.04 - 1934.136 * jc)
    app_long = math.radians(l0 + c - 0.00569 - 0.00478 * math.sin(omega))
    mean_obliq = 23 + (26 + (21.448 - jc * (46.815 + jc * (
        0.00059 - jc * 0.001813))) / 60) / 60
    obliq = math.radians(mean_obliq + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(obliq) * math.sin(app_long))
    y = math.tan(obliq / 2) ** 2
    l0r = math.radians(l0)
    eqtime = 4 * math.degrees(
        y * math.sin(2 * l0r)
        - 2 * e * math.sin(mr)
        + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r)
        - 1.25 * e * e * math.sin(2 * mr))
    return eqtime, decl


def _event(lat, lon, year, month, day, rising):
    """Epoch seconds of sunrise (rising) or sunset on that date at that
    place, or None when the sun doesn't cross the horizon that day."""
    jd = _julian_day(year, month, day)
    minutes = 720.0                       # first guess: solar noon, UTC-ish
    for _ in range(2):                    # once to place it, once to refine
        jc = (jd + minutes / 1440 - 2451545) / 36525
        eqtime, decl = _sun(jc)
        latr = math.radians(lat)
        cos_ha = (math.cos(math.radians(ZENITH))
                  / (math.cos(latr) * math.cos(decl))
                  - math.tan(latr) * math.tan(decl))
        if not -1 <= cos_ha <= 1:
            return None
        ha = math.degrees(math.acos(cos_ha))
        minutes = 720 - 4 * (lon + (ha if rising else -ha)) - eqtime
    return calendar.timegm((year, month, day, 0, 0, 0)) + minutes * 60


def sunrise(lat, lon, year, month, day):
    """Epoch seconds of sunrise on that (local) date, or None."""
    return _event(lat, lon, year, month, day, rising=True)


def sunset(lat, lon, year, month, day):
    """Epoch seconds of sunset on that (local) date, or None. In a western
    timezone a summer sunset lands after 0h UTC -- the returned instant is
    still that local evening's."""
    return _event(lat, lon, year, month, day, rising=False)
