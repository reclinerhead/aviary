# =============================================================================
# project-squirrel -- listener/findings.py
#
# The finding engine (issue #614, epic #611 Phase A): every fact the field
# journal may lead with, computed here, each as one Finding --
#
#   {"kind", "fields", "support", "effect", "thin", "fact_line",
#    "novelty_key", "flag"}
#
# -- so the editor (listener/editor.py) can rank them and the narrator can be
# handed exactly one lede and a few supporting lines. Same split as the
# field notes always had: Python computes, the model narrates. A fact_line
# is a FINISHED English sentence with every figure already in it; the model
# never derives a number, and notes_validate.py checks it didn't.
#
# Why these kinds, and not clock hours and weekdays: the 2026-10-24 review
# measured the house record and found the stories a naturalist would lead
# with -- the Cardinal's first song 17 minutes before sunrise on nearly
# every morning, twenty species gone south and nine arrived, an owl heard
# only behind the garage, a "swallow" heard 255 times after dark -- none of
# which the clock-hour notes could see. Each kind below is one of those.
#
#   arrival / departure / lingering   against the regional prior (BirdNET's
#                                     48-week range model, region_prior.py)
#   season                            where a present bird stands in its
#                                     season when none of the above is news
#                                     (#626): days heard lately, and the
#                                     model's calendar for it
#   dawn / dusk                       against computed sunrise/sunset
#                                     (solar.py), never the clock
#   place                             by the feeds registry's place phrases
#   night / anomaly                   a night voice, or a diurnal bird
#                                     mostly "heard" after dark -- the
#                                     latter flagged, not narrated as habit
#   record                            streaks and the busiest day
#   trend                             counting only microphones listening
#                                     in BOTH weeks (the Warbler32 era
#                                     inflated, then deflated, every count)
#   weather_sky / weather_temp        exposure-normalised rates; temperature
#                                     compared within the same calendar
#                                     month, so "warm" is not just "summer"
#
# `effect` is each kind's own 0..~1.5 magnitude; `support` is the evidence
# count in the kind's own unit (days, visits); `novelty_key` names the
# finding coarsely enough that the same fact next week is "already said".
# =============================================================================

import time

from listener import region_prior, solar

# A dawn/dusk claim needs this many mornings/evenings.
MIN_DAYS = 10
# Morning and evening windows (local hours) the first/last song is taken in.
DAWN_WINDOW = (2, 12)
DUSK_WINDOW = (12, 24)
# A season "starts" after a silence this long; an arrival is news this long.
SEASON_GAP_DAYS = 30
ARRIVAL_NEWS_DAYS = 14
# Departure: this silent, after at least this many visits.
DEPARTED_DAYS = 14
DEPARTED_MIN_VISITS = 10
# The prior below this means "the model's season here is over".
PRIOR_GONE = 0.03
PRIOR_SEASONAL = 0.10
# The season finding's "on N of the last M days" window (#626).
SEASON_WINDOW_DAYS = 28
# Place: one microphone's share, on at least this many detections.
PLACE_SHARE = 0.65
PLACE_MIN = 20
# ...and at least this far above that microphone's share of the whole yard.
PLACE_LIFT = 0.15
# Night: 10pm-4am share, on at least this many visits.
NIGHT_HOURS = (22, 4)
NIGHT_SHARE = 0.5
NIGHT_MIN = 20
# A heard-every-day streak worth saying.
STREAK_NEWS = 14
# Weather bucket floors (species_analysis's, restated as this module's).
MIN_BUCKET_VISITS = 3
MIN_BUCKET_HOURS = 6.0
MIN_EXPECTED = 3.0
WEATHER_NEWS = 0.10

# Birds for which a night voice is ordinary -- genus, or one species. A
# diurnal bird mostly "heard" after dark is far more likely an insect or a
# frog BirdNET mapped onto its call (the house's Rough-winged Swallow: 255
# of 301 detections between 9pm and 3am, on 25 straight July nights).
NOCTURNAL_GENERA = frozenset((
    "Strix", "Bubo", "Megascops", "Otus", "Asio", "Aegolius", "Tyto",
    "Athene", "Antrostomus", "Caprimulgus", "Chordeiles", "Nycticorax",
    "Nyctanassa", "Ardea", "Botaurus", "Ixobrychus", "Scolopax",
    "Gallinago", "Rallus", "Porzana", "Coturnicops", "Laterallus",
    "Catharus", "Branta", "Anser", "Cygnus", "Numenius", "Bartramia",
))
NOCTURNAL_SPECIES = frozenset(("Charadrius vociferus",
                               "Mimus polyglottos"))

MONTHS = ("January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December")


def finding(kind, fields, support, effect, fact_line, novelty_key,
            thin=False, flag=None):
    return {"kind": kind, "fields": fields, "support": int(support),
            "effect": round(float(effect), 3), "thin": bool(thin),
            "fact_line": fact_line, "novelty_key": novelty_key, "flag": flag}


# --- small shared shaping -----------------------------------------------------

def local_date(ts):
    lt = time.localtime(ts)
    return lt.tm_year, lt.tm_mon, lt.tm_mday


def date_text(ts):
    lt = time.localtime(ts)
    return f"{MONTHS[lt.tm_mon - 1]} {lt.tm_mday}, {lt.tm_year}"


def pct(x):
    """A prior or share as a whole percent, never "0%" for a real value."""
    p = round(x * 100)
    return "under 1" if 0 < x and p < 1 else str(p)


def week_phrase(week):
    """BirdNET week (1-48) -> "late August"."""
    month = MONTHS[(week - 1) // 4]
    part = ("early", "mid", "mid", "late")[(week - 1) % 4]
    return f"{part} {month}"


def minutes_phrase(m):
    """-17 -> "17 minutes before", 0 -> "right at", 5 -> "5 minutes after"."""
    m = int(round(m))
    if m == 0:
        return "right at"
    unit = "minute" if abs(m) == 1 else "minutes"
    return f"{abs(m)} {unit} {'before' if m < 0 else 'after'}"


def relative(m, event):
    """-17, "sunrise" -> "17 minutes before sunrise"; 0 -> "right at
    sunrise" (the desk run printed "a median of right at sunrise")."""
    return f"{minutes_phrase(m)} {event}"


def quartiles(values):
    """(q1, median, q3) by nearest rank -- the house analyses' rule."""
    v = sorted(values)
    n = len(v)
    med = (v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2)
    return v[n // 4], med, v[(3 * n) // 4]


def is_nocturnal(sci):
    return (sci in NOCTURNAL_SPECIES
            or (sci or "").split(" ")[0] in NOCTURNAL_GENERA)


def in_night(ts):
    h = time.localtime(ts).tm_hour
    return h >= NIGHT_HOURS[0] or h < NIGHT_HOURS[1]


# --- dawn and dusk ------------------------------------------------------------

def sun_offsets(visits, lat, lon, window, event):
    """Per local day, the first (dawn) or last (dusk) visit inside `window`,
    as minutes from that date's sunrise/sunset. Days the sun doesn't cross
    are skipped."""
    first = event == "dawn"
    by_day = {}
    for ts in visits:
        h = time.localtime(ts).tm_hour
        if not window[0] <= h < window[1]:
            continue
        day = local_date(ts)
        cur = by_day.get(day)
        if cur is None or (ts < cur if first else ts > cur):
            by_day[day] = ts
    fn = solar.sunrise if first else solar.sunset
    out = []
    for day, ts in by_day.items():
        sun = fn(lat, lon, *day)
        if sun is not None:
            out.append((ts - sun) / 60)
    return out


def dawn(visits, lat, lon):
    """When the first song comes, against the sun. A tight cluster before
    sunrise is "a dawn singer" -- the Cardinal's finding."""
    offs = sun_offsets(visits, lat, lon, DAWN_WINDOW, "dawn")
    if len(offs) < MIN_DAYS:
        return None
    q1, med, q3 = quartiles(offs)
    spread = q3 - q1
    if med <= -10 and spread <= 30:
        label = "a dawn singer, starting before sunrise"
    elif abs(med) <= 20 and spread <= 45:
        label = "up with the sun"
    elif med >= 90:
        label = "a late riser"
    else:
        label = None
    effect = 1 / (1 + spread / 30) if label else 0.2
    line = (f"First heard each morning, at the median, "
            f"{relative(med, 'sunrise')}, across {len(offs):,} mornings; on "
            f"half of them between {relative(q1, 'sunrise')} and "
            f"{relative(q3, 'sunrise')}.")
    if label:
        line += f" That makes it {label}."
    return finding("dawn", {"median_min": round(med), "q1": round(q1),
                            "q3": round(q3), "days": len(offs),
                            "label": label},
                   len(offs), effect, line,
                   f"dawn:{label or 'none'}:{round(med / 10) * 10}")


def dusk(visits, lat, lon):
    """When the last song comes, against sunset. Only a clear pattern is a
    finding: "sings until dark"."""
    offs = sun_offsets(visits, lat, lon, DUSK_WINDOW, "dusk")
    if len(offs) < MIN_DAYS:
        return None
    q1, med, q3 = quartiles(offs)
    if not (med >= -20 and q3 - q1 <= 60):
        return None
    line = (f"Last heard each evening, at the median, "
            f"{relative(med, 'sunset')}, across {len(offs):,} evenings: it "
            "sings until dark.")
    return finding("dusk", {"median_min": round(med), "days": len(offs)},
                   len(offs), 1 / (1 + (q3 - q1) / 60), line,
                   f"dusk:{round(med / 15) * 15}")


# --- the season ---------------------------------------------------------------

def season_start(visits):
    """The first visit after the last silence of SEASON_GAP_DAYS or more
    (the very first visit when there has been none)."""
    v = sorted(visits)
    start = v[0]
    for a, b in zip(v, v[1:]):
        if b - a >= SEASON_GAP_DAYS * 86400:
            start = b
    return start


def season_end(curve, week):
    """The first week after `week` (wrapping the 48-week year) in which the
    model rates the species as gone, or None when it never does -- the
    "expects it here all year" case."""
    for step in range(1, len(curve)):
        w = (week - 1 + step) % len(curve) + 1
        if curve[w - 1] < PRIOR_GONE:
            return w
    return None


def phenology(visits, now, curve):
    """Arrival, departure or lingering -- the season's news -- measured
    against BirdNET's own range model where the prior exists (`curve`,
    48 weekly scores), else against this yard's record alone. When none
    of those is news, the bird's standing in its season (#626): the one
    note's calendar sentence, so every present bird can say how often it
    has been heard lately and what the model's calendar holds for it."""
    if not visits:
        return None
    v = sorted(visits)
    last, n = v[-1], len(v)
    silent = int((now - last) // 86400)
    week = region_prior.week_at(now)
    prior_now = curve[week - 1] if curve else None
    peak = max(curve) if curve else None
    peak_week = curve.index(peak) + 1 if curve else None

    if silent >= DEPARTED_DAYS and n >= DEPARTED_MIN_VISITS:
        # Worded so it can't be read as a return: the first desk run turned
        # "Not heard for 30 days" into "popped up again after a thirty-day
        # silence".
        line = (f"Silent for the last {silent} days: it has not been heard "
                f"since {date_text(last)}, after {n:,} visits.")
        if curve:
            line += (f" BirdNET's range model rates it {pct(prior_now)}% "
                     f"likely here this week, against {pct(peak)}% at its "
                     f"peak in {week_phrase(peak_week)}.")
            if prior_now < PRIOR_GONE and peak >= PRIOR_SEASONAL:
                line += " By the model's calendar, its season here is over."
            elif prior_now >= 0.3:
                line += (" The model still expects it here, so the silence "
                         "itself is the news.")
        return finding("departure", {"last_ts": last, "silent_days": silent,
                                     "visits": n, "prior_now": prior_now,
                                     "prior_peak": peak},
                       n, 1.0, line, f"departure:{local_date(last)}")

    start = season_start(v)
    if now - start <= ARRIVAL_NEWS_DAYS * 86400:
        since = sum(1 for t in v if t >= start)
        first_ever = start == v[0]
        line = f"First heard {date_text(start)}"
        if first_ever:
            line += ", the first time ever in the yard"
        else:
            prev = max(t for t in v if t < start)
            line += f", back after {int((start - prev) // 86400)} days away"
        line += f"; {since:,} visit{'' if since == 1 else 's'} since."
        if curve:
            at = curve[region_prior.week_at(start) - 1]
            if at >= PRIOR_SEASONAL:
                line += (f" That is right on BirdNET's calendar: its range "
                         f"model rates it {pct(at)}% likely here that week.")
            elif at < PRIOR_GONE:
                line += (f" BirdNET's range model rates it only {pct(at)}% "
                         "likely here that week, so this is early, or off "
                         "its usual map.")
        return finding("arrival", {"start_ts": start, "first_ever": first_ever,
                                   "visits": since},
                       since, 1.0, line, f"arrival:{local_date(start)}")

    if (curve and silent <= 3 and prior_now < PRIOR_GONE
            and peak >= PRIOR_SEASONAL):
        line = (f"Still here, though BirdNET's range model rates it only "
                f"{pct(prior_now)}% likely this week (its peak is "
                f"{pct(peak)}% in {week_phrase(peak_week)}).")
        return finding("lingering", {"prior_now": prior_now,
                                     "prior_peak": peak},
                       n, 0.8, line, f"lingering:{week}")

    # Nothing newsier: where the bird stands in its season. Novelty is the
    # calendar month, so a quiet bird gets at most one season-led entry a
    # month, and the fact is still there to support any other lede.
    heard = len({local_date(t) for t in v
                 if t > now - SEASON_WINDOW_DAYS * 86400})
    line = f"Heard here since {date_text(v[0])}"
    if heard:
        line += (f", on {heard} of the last {SEASON_WINDOW_DAYS} days"
                 f" ({n:,} visits in all).")
    else:
        line += f" ({n:,} visits in all), but not in the last {silent} days."
    fields = {"first_ts": v[0], "days_heard": heard,
              "window_days": SEASON_WINDOW_DAYS, "visits": n,
              "prior_now": prior_now, "prior_peak": peak, "ends_week": None}
    if curve and peak < PRIOR_SEASONAL:
        line += (f" BirdNET's range model rates it no more than {pct(peak)}% "
                 "likely here in any week of the year, so your yard hears "
                 "more of it than the model expects.")
    elif curve:
        ends = season_end(curve, week)
        fields["ends_week"] = ends
        line += (f" BirdNET's range model rates it {pct(prior_now)}% likely "
                 f"here this week, with its peak of {pct(peak)}% in "
                 f"{week_phrase(peak_week)}")
        line += ("; it expects it here all year." if ends is None else
                 f", and expects its season here to end around "
                 f"{week_phrase(ends)}.")
    lt = time.localtime(now)
    return finding("season", fields, n, 0.5, line,
                   f"season:{lt.tm_year}-{lt.tm_mon:02d}")


# --- where, and when in the night --------------------------------------------

def place(sources, places, yard=None):
    """One microphone hearing most of a bird -- MORE than it hears of the
    yard as a whole. The house's rear camera hears ~71% of every bird, so
    "76% of the Cardinal's detections came from the back of the house" is
    the instrument, not the bird; the White-throated Sparrow at 75% from
    the FRONT door (which hears ~29% of the yard) is the finding. `yard` is
    {source: detections} across all species; without it the share alone
    decides. Judged over the microphones listening NOW; a bird only retired
    microphones ever heard says so."""
    from listener.species_analysis import is_live, mic_name
    if not sources:
        return None
    live = {k: v for k, v in sources.items() if v and is_live(k, places)}
    every = {k: v for k, v in sources.items() if v}
    if sum(live.values()) < PLACE_MIN:
        # Too little on the microphones listening now to judge them -- but
        # the bird may still have a place: the Barred Owl is 100 of 101
        # detections on the garage's retired first board, and judging its
        # one stray live detection alone would hide that.
        total = sum(every.values())
        if total < PLACE_MIN:
            return None
        top, n = max(sorted(every.items()), key=lambda kv: kv[1])
        if is_live(top, places) or n / total < PLACE_SHARE:
            return None
        line = (f"Heard almost only at {mic_name(top, places)}, which no "
                f"longer listens: {n:,} of {total:,} detections.")
        return finding("place", {"source": top, "share": round(n / total, 3),
                                 "live": False, "base": None},
                       total, 1.0, line, f"place:{top}")
    judged = live
    total = sum(judged.values())
    top, n = max(sorted(judged.items()), key=lambda kv: kv[1])
    share = n / total
    yard_live = {k: v for k, v in (yard or {}).items() if k in live}
    base = (yard_live.get(top, 0) / sum(yard_live.values())
            if yard_live and sum(yard_live.values()) else None)
    if share < PLACE_SHARE or (base is not None
                               and share - base < PLACE_LIFT):
        return None
    line = (f"Heard mostly at {mic_name(top, places)}: {n:,} of the "
            f"{total:,} detections from the microphones listening now.")
    if base is not None:
        line += (f" That microphone hears only {pct(base)}% of the yard's "
                 "birds overall, so this one favours that spot.")
        effect = (share - base) / (1 - base) if base < 1 else 0.0
    else:
        effect = (share - 0.5) * 2
    return finding("place", {"source": top, "share": round(share, 3),
                             "live": True,
                             "base": round(base, 3) if base is not None
                             else None},
                   total, effect, line, f"place:{top}")


def night(visits, sci):
    """A night voice. For a bird known to call at night it is a habit worth
    a line; for any other it is a flag -- the detections are suspect."""
    n = len(visits)
    if n < NIGHT_MIN:
        return None
    k = sum(1 for ts in visits if in_night(ts))
    share = k / n
    if share < NIGHT_SHARE:
        return None
    if is_nocturnal(sci):
        line = (f"Mostly a night voice: {pct(share)}% of its {n:,} visits "
                "fell between 10pm and 4am.")
        return finding("night", {"share": round(share, 3), "visits": n},
                       n, share, line, f"night:{round(share, 1)}")
    line = (f"{pct(share)}% of its {n:,} visits fell between 10pm and 4am, "
            "and this is not a bird known to call at night. Those detections "
            "are suspect: worth a listen before believing them.")
    return finding("anomaly", {"share": round(share, 3), "visits": n},
                   n, 1.0, line, "anomaly:night", flag="anomaly")


# --- records ------------------------------------------------------------------

def records(visits, now):
    """Streaks and the busiest day, in local days."""
    if not visits:
        return None
    import datetime
    days = sorted({datetime.date(*local_date(ts)) for ts in visits})
    longest = run = 1
    for a, b in zip(days, days[1:]):
        run = run + 1 if (b - a).days == 1 else 1
        longest = max(longest, run)
    today = datetime.date(*local_date(now))
    current = 0
    if (today - days[-1]).days <= 1:
        current = 1
        for a, b in zip(reversed(days[:-1]), reversed(days[1:])):
            if (b - a).days != 1:
                break
            current += 1
    counts = {}
    for ts in visits:
        counts[local_date(ts)] = counts.get(local_date(ts), 0) + 1
    best_day, best = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    parts = [f"Heard on {len(days):,} separate days."]
    if current >= STREAK_NEWS:
        parts.append(f"Heard every day for the last {current} days.")
    elif longest >= STREAK_NEWS:
        parts.append(f"Its longest run was {longest} days in a row.")
    y, m, d = best_day
    parts.append(f"Its busiest day was {MONTHS[m - 1]} {d}, {y}, with "
                 f"{best:,} visit{'' if best == 1 else 's'}.")
    effect = 0.7 if current >= STREAK_NEWS else 0.3
    return finding("record", {"days": len(days), "longest": longest,
                              "current": current, "busiest": best,
                              "busiest_day": list(best_day)},
                   len(days), effect, " ".join(parts),
                   f"record:{best}:{longest // 30}:{current // 7}")


# --- the week -----------------------------------------------------------------

def fair_sources(source_spans, now, window_days=7):
    """Microphones that were listening in BOTH trend windows, judged
    yard-wide from {source: (first_ts, last_ts)}: a mic that came online
    (or died) mid-fortnight would otherwise turn its own uptime into a
    trend for every bird it hears."""
    span = window_days * 86400
    return {s for s, (a, b) in source_spans.items()
            if a <= now - 2 * span + 86400 and b >= now - 86400}


def trend(visit_rows, now, fair, window_days=7):
    """7-vs-7 on the fair microphones only. A finding only when it clearly
    moved: the busier week holds at least TREND_MIN visits and the two
    differ by TREND_MOVED of it (species_analysis's constants)."""
    from listener.species_analysis import TREND_MIN, TREND_MOVED
    span = window_days * 86400
    rows = [ts for ts, src in visit_rows if src in fair]
    recent = sum(1 for ts in rows if ts > now - span)
    prior = sum(1 for ts in rows if now - 2 * span < ts <= now - span)
    hi = max(recent, prior)
    if hi < TREND_MIN or abs(recent - prior) < TREND_MOVED * hi:
        return None
    up = recent > prior
    # The fairness lives in the arithmetic, not the sentence: handed
    # "counting only microphones that were listening in both weeks", the
    # model wrote "only when the microphones at the back of the garage were
    # also listening" -- a place claim nobody made.
    line = (f"Last {window_days} days: {recent:,} visits, against {prior:,} "
            f"in the {window_days} days before. That is a clear "
            f"{'rise' if up else 'fall'}.")
    return finding("trend", {"recent": recent, "prior": prior},
                   hi, abs(recent - prior) / hi, line,
                   f"trend:{'up' if up else 'down'}:"
                   f"{region_prior.week_at(now)}")


# --- weather ------------------------------------------------------------------

SKY_WHEN = {"clear": "Under clear skies", "cloudy": "Under cloudy skies",
            "rain": "When it rains", "snow": "When it snows"}


def weather_sky(stats):
    """The strongest solid sky effect from build_stats (exposure-normalised
    within the bird's active hours), as a bird-subject sentence."""
    best = None
    for f in stats["weather"]["conditions"]:
        if (f["effect"] is None or f["bucket"] == "unknown" or f["thin"]
                or abs(f["effect"]) < WEATHER_NEWS):
            continue
        if best is None or abs(f["effect"]) > abs(best["effect"]):
            best = f
    if best is None:
        return None
    p = round(best["effect"] * 100)
    when = SKY_WHEN.get(best["bucket"], f"In {best['bucket']} weather")
    # Whole hours: handed "76.4 hours" the model writes "76 hours" -- a
    # figure it was never given -- and the validator throws the draft away.
    line = (f"{when}, this bird turns up {abs(p)}% "
            f"{'MORE OFTEN' if p > 0 else 'LESS OFTEN'} than its own average "
            f"rate, on {best['visits']:,} visits over "
            f"{round(best['hours']):,} hours of that weather.")
    return finding("weather_sky", {"bucket": best["bucket"], "effect": p},
                   best["visits"], min(abs(best["effect"]), 1.5), line,
                   f"sky:{best['bucket']}:{'+' if p > 0 else '-'}")


def temp_stratified(visit_ts, observations, active, band, bucket_month=None):
    """Temperature-band effects compared WITHIN each calendar month, then
    pooled (a Mantel-Haenszel-style standardisation): expected visits in a
    band = sum over months of (that month's own visit rate) x (hours of that
    band that month). "Warm" then means warmer than usual for the time of
    year, not just "summer" -- the confound the old notes could only
    caveat. -> [{bucket, observed, expected, hours, effect, thin}]."""
    from listener.species_analysis import observation_index, weather_at
    month_of = bucket_month or (lambda ts: time.localtime(ts).tm_mon)
    hours, visits = {}, {}
    for r in observations:
        if time.localtime(r["ts"]).tm_hour not in active:
            continue
        key = (month_of(r["ts"]), band(r["temp_f"]))
        hours[key] = hours.get(key, 0.0) + 5 / 60
    index = observation_index(observations)
    for ts in visit_ts:
        if time.localtime(ts).tm_hour not in active:
            continue
        obs = weather_at(index, ts)
        if obs is None:
            continue
        key = (month_of(ts), band(obs["temp_f"]))
        visits[key] = visits.get(key, 0) + 1
    months = {m for m, _ in hours}
    rate = {}
    for m in months:
        h = sum(v for (mm, _), v in hours.items() if mm == m)
        n = sum(v for (mm, _), v in visits.items() if mm == m)
        rate[m] = n / h if h else 0.0
    out = []
    for b in sorted({b for _, b in hours}):
        if b == "unknown":
            continue
        observed = sum(v for (m, bb), v in visits.items() if bb == b)
        expected = sum(rate[m] * v for (m, bb), v in hours.items() if bb == b)
        hrs = sum(v for (m, bb), v in hours.items() if bb == b)
        if expected <= 0:
            continue
        out.append({"bucket": b, "observed": observed,
                    "expected": round(expected, 1), "hours": round(hrs, 1),
                    "effect": round(observed / expected - 1, 3),
                    "thin": (observed < MIN_BUCKET_VISITS
                             or hrs < MIN_BUCKET_HOURS
                             or expected < MIN_EXPECTED)})
    return out


def weather_temp(rows):
    """The strongest solid month-adjusted temperature effect."""
    # Strongest first; on a tie the positive effect leads ("turns up more
    # in warm weather" is the line a reader can act on).
    best = None
    for f in rows:
        if f["thin"] or abs(f["effect"]) < WEATHER_NEWS:
            continue
        if best is None or ((abs(f["effect"]), f["effect"])
                            > (abs(best["effect"]), best["effect"])):
            best = f
    if best is None:
        return None
    p = round(best["effect"] * 100)
    line = (f"In {best['bucket']} weather, this bird turns up {abs(p)}% "
            f"{'MORE OFTEN' if p > 0 else 'LESS OFTEN'} than is usual for it "
            f"at the same time of year, on {best['observed']:,} visits over "
            f"{round(best['hours']):,} hours of that weather.")
    return finding("weather_temp", {"bucket": best["bucket"], "effect": p},
                   best["observed"], min(abs(best["effect"]), 1.5), line,
                   f"temp:{best['bucket']}:{'+' if p > 0 else '-'}")


# --- all of them ---------------------------------------------------------------

def compute(*, sci, visits, visit_rows, sources, stats, observations, now,
            latlon=None, curve=None, places=None, fair=None, yard=None):
    """Every finding for one species, in a stable order (None dropped). The
    pass gathers the inputs (all I/O); this is pure. `yard` is the store's
    detections per source across all species (for place)."""
    from listener.species_analysis import temp_band
    out = [phenology(visits, now, curve), night(visits, sci)]
    if latlon:
        out += [dawn(visits, *latlon), dusk(visits, *latlon)]
    out += [place(sources, places, yard), records(visits, now)]
    if fair is not None:
        out.append(trend(visit_rows, now, fair))
    if stats and stats["weather"]["enough"]:
        out.append(weather_sky(stats))
        active = set(stats["active_hours"])
        out.append(weather_temp(
            temp_stratified(visits, observations, active, temp_band)))
    return [f for f in out if f]
