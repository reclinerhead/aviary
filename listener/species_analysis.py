# =============================================================================
# project-squirrel -- listener/species_analysis.py
#
# The field-naturalist blocks (epic #182 Phase 4, issue #186): what our own
# visit record and our own weather archive say about a species, computed here
# and written up by the LLM.
#
#   python -m listener.species_analysis
#   python -m listener.species_analysis --refresh "Cardinalis cardinalis"
#   python -m listener.species_analysis --dry-run      (stats, no LLM, no write)
#
# THE SPLIT, which is the whole design: **statistics are computed in this
# file; the model only narrates them.** Hand any LLM raw event rows and ask
# "what's the pattern" and it will invent percentages -- a 12B local model,
# Opus, and Grok alike -- and the invention is invisible in the output, which
# is the worst property a feature can have. So every number the prose can use
# is computed here, pytest-covered, and stored alongside the text
# (`stats_json`) so any sentence can be audited after the fact. The prompt's
# first rule is that the model may not do arithmetic or invent figures.
#
# That split is also why the LOCAL model is the right call: "turn eight facts
# into two charming paragraphs" is exactly what gemma3:12b already does for
# Marlin and Willard. The client is the narrator's (borrowed, never copied --
# the weather post's precedent), so moving to a hosted model later is env
# config, not a rewrite.
#
# WHY A PASS AND NOT AN MCC ROUTE (the epic sketched a click-to-generate
# route): a local model is free, which removes the entire reason generation
# had to be click-gated, and #183's do-not-change list forbids the MCC
# writing earl.db at all. So this is an ordinary enrichment pass -- worklist
# driven, idempotent, per-species function with the CLI as a thin loop (the
# metadata-refresh rule) -- and the GUI only ever reads. Nothing is ever
# generated at render time.
#
# HONESTY RULES, encoded in the statistics rather than hoped for in the prose:
#   - Weather claims are RATES, never counts: "40% of visits were cloudy" is
#     meaningless if 40% of the hours were cloudy. Every bucket is visits per
#     hour of exposure to that condition, against the species' own baseline.
#     weather.db's 5-minute rows ARE the exposure denominator.
#   - Exposure is counted only during the species' ACTIVE HOURS. Birds sing at
#     dawn; if rain falls at dawn more often, a rain effect and a dawn effect
#     are otherwise indistinguishable. Comparing within the hours the bird is
#     actually heard removes the confound.
#   - WIND IS NEVER A BEHAVIOURAL CLAIM. Earl's own threshold rises above
#     15 mph (0.65 -> 0.75, issue #175), so "fewer birds when windy" partly
#     measures our instrument going deaf. It is reported as an instrument
#     note or not at all.
#   - Every finding carries its sample size, and thin findings are marked
#     rather than dropped -- a bird heard twice is still worth reading about
#     as long as the page says that's all we have.
#
# Config (env):
#   MERLE_EARL_DB       the bird record (default "earl.db")
#   MERLE_WEATHER_DB    the weather archive (default "weather.db") -- read
#                       only; unset/missing simply means no weather block
#   MERLE_OLLAMA        Ollama "host" or "host:port"; UNSET = no generation
#                       (the narrator's kill switch, same semantics)
#   MERLE_OLLAMA_MODEL  model name (default: the narrator's)
# =============================================================================

import argparse
import bisect
import json
import os
import sqlite3
import time

from listener import gate
# The narrator owns the Ollama plumbing (endpoint config, the blocking
# non-streaming client, the model default); this borrows it rather than
# growing a second copy that could drift -- the weather post's precedent.
from narration.narrator import OLLAMA_DEFAULT_MODEL, Ollama, ollama_address

DEFAULT_DB_PATH = "earl.db"
DEFAULT_WEATHER_DB = "weather.db"

# A visit is matched to the observation nearest it within this window. The
# archive ticks every 5 minutes, so 15 covers a couple of missed ticks; past
# that the weather at the visit is genuinely unknown and the visit sits out
# of the weather stats rather than borrowing a stale reading.
WEATHER_MATCH_S = 900
# The hours that together account for this much of a species' visits are its
# "active hours" -- the window weather exposure is measured within.
ACTIVE_COVERAGE = 0.9
# Below these, a bucket is reported but flagged thin (show-with-hedging).
MIN_BUCKET_VISITS = 3
MIN_BUCKET_HOURS = 6.0
# Below this, the weather block says so instead of pretending.
MIN_VISITS_FOR_WEATHER = 12
# Regenerate once a species has this many new visits since its last write.
WATERMARK_STEP = 20
# Two short paragraphs; the cap is a backstop for a model ignoring the rules.
NUM_PREDICT = 320
# Analysis is a long, patient job (one call per species, ~30-90s on CPU), not
# a live show like the narrator's 30s pacing gate.
ANALYSIS_TIMEOUT_S = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS species_analysis (
    species_sci      TEXT PRIMARY KEY,
    rhythm_text      TEXT,
    weather_text     TEXT,
    stats_json       TEXT,
    visits_watermark INTEGER NOT NULL DEFAULT 0,
    model            TEXT,
    generated_ts     INTEGER NOT NULL
);
"""

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday")


def db_path():
    return os.environ.get("MERLE_EARL_DB", "").strip() or DEFAULT_DB_PATH


def weather_db_path():
    return os.environ.get("MERLE_WEATHER_DB", "").strip() or DEFAULT_WEATHER_DB


def connect(path):
    """The species_profile.connect() shape: WAL, idempotent schema, this
    pass owning only its own table."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# --- Visit shaping -----------------------------------------------------------

def group_visits(timestamps, gap_s=gate.VISIT_GAP_S):
    """Sighting timestamps -> visit OPENINGS. gate.VISIT_GAP_S is imported,
    not restated, so the 60-second rule can never drift from the listener's
    (or the chart's). Pre-#175 per-window rows collapse here exactly as they
    do in the read path."""
    opens = []
    last = None
    for ts in sorted(timestamps):
        if last is None or ts - last > gap_s:
            opens.append(ts)
        last = ts
    return opens


def hour_histogram(visit_ts):
    """Visits per local hour of day (24 buckets). Local, because "dawn" is a
    claim about the viewer's sky, not UTC's."""
    hours = [0] * 24
    for ts in visit_ts:
        hours[time.localtime(ts).tm_hour] += 1
    return hours


def weekday_histogram(visit_ts):
    """Visits per local weekday, Monday first (time.localtime's tm_wday)."""
    days = [0] * 7
    for ts in visit_ts:
        days[time.localtime(ts).tm_wday] += 1
    return days


def peak_window(hours, width=3):
    """The `width`-hour stretch holding the most visits -- the "75% of robin
    visits land between 7 and 9am" finding. Wraps midnight, because an owl's
    peak shouldn't be split by an arbitrary day boundary. None when there's
    nothing to peak."""
    total = sum(hours)
    if total == 0:
        return None
    best_start, best_count = 0, -1
    for start in range(24):
        count = sum(hours[(start + i) % 24] for i in range(width))
        if count > best_count:
            best_start, best_count = start, count
    return {
        "start_hour": best_start,
        "end_hour": (best_start + width) % 24,
        "visits": best_count,
        "share": round(best_count / total, 3),
    }


def active_hours(hours, coverage=ACTIVE_COVERAGE):
    """The set of local hours accounting for `coverage` of all visits, taken
    busiest-first. This is the window weather exposure is measured within --
    the dawn-confound control: comparing a rainy 3am against a clear 7am
    would attribute the dawn chorus to the weather."""
    total = sum(hours)
    if total == 0:
        return set()
    order = sorted(range(24), key=lambda h: (-hours[h], h))
    picked, running = set(), 0
    for h in order:
        if hours[h] == 0:
            break
        picked.add(h)
        running += hours[h]
        if running >= coverage * total:
            break
    return picked


def trend(visit_ts, now, window_days=7):
    """Visits in the last `window_days` against the `window_days` before
    them. Returns None when the record isn't old enough to have both halves
    -- a species heard for three days has no trend, and saying so is better
    than dividing by a window that doesn't exist."""
    span = window_days * 86400
    if not visit_ts or now - min(visit_ts) < 2 * span:
        return None
    recent = sum(1 for ts in visit_ts if ts > now - span)
    prior = sum(1 for ts in visit_ts if now - 2 * span < ts <= now - span)
    return {"recent": recent, "prior": prior, "window_days": window_days}


def busiest_day(visit_ts):
    """The single local day with the most visits, as (day_start, count)."""
    counts = {}
    for ts in visit_ts:
        lt = time.localtime(ts)
        key = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                               0, 0, 0, 0, 0, -1)))
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    day, count = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    return {"day_ts": day, "visits": count, "days_observed": len(counts)}


# --- Weather ------------------------------------------------------------------

def condition_bucket(condition, rain_rate_inhr):
    """One observation -> a coarse sky bucket.

    The piezo outranks OpenWeather's word (the house rule the condition icon
    already follows): if the driveway measured rain falling, it was raining,
    whatever the grid cell said. Otherwise OWM's `main` decides, and note
    the granularity honestly -- the archive stores "Clouds", not "overcast"
    vs "few clouds", so this can distinguish cloudy from clear and nothing
    finer."""
    if rain_rate_inhr is not None and rain_rate_inhr > 0:
        return "rain"
    word = (condition or "").strip().lower()
    if word in ("rain", "drizzle", "thunderstorm", "squall"):
        return "rain"
    if word == "snow":
        return "snow"
    if word == "clear":
        return "clear"
    if word in ("clouds", "mist", "fog", "haze", "smoke", "dust", "sand",
                "ash", "tornado"):
        # Mist/fog/haze read as cloudy, the ConditionGlyph precedent.
        return "cloudy"
    return "unknown"


def temp_band(temp_f):
    """Coarse temperature bands. Wide on purpose: a driveway's bird activity
    doesn't resolve at one-degree granularity, and narrow bands would shatter
    an already-small sample."""
    if temp_f is None:
        return "unknown"
    if temp_f < 32:
        return "freezing"
    if temp_f < 50:
        return "cold"
    if temp_f < 70:
        return "mild"
    if temp_f < 85:
        return "warm"
    return "hot"


def observation_index(rows):
    """(sorted ts list, row list) for bisect lookups. The archive is one row
    per 5 minutes, so a season is a few thousand rows -- cheap to hold."""
    ordered = sorted(rows, key=lambda r: r["ts"])
    return [r["ts"] for r in ordered], ordered


def weather_at(index, ts, tolerance_s=WEATHER_MATCH_S):
    """The observation nearest `ts`, or None when the archive has nothing
    close. None is the honest answer for a visit predating the archive --
    that visit sits out the weather stats rather than borrowing a reading
    from hours away."""
    stamps, rows = index
    if not stamps:
        return None
    i = bisect.bisect_left(stamps, ts)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(stamps):
            delta = abs(stamps[j] - ts)
            if delta <= tolerance_s and (best is None or delta < best[0]):
                best = (delta, rows[j])
    return best[1] if best else None


def exposure_hours(rows, hours_of_day, key):
    """Hours of exposure per bucket, counted ONLY during the species' active
    hours -- the denominator that turns counts into rates. Each archived row
    stands for 5 minutes of weather (the archive's tick).

    `key` maps a row to its bucket, so conditions and temperature bands share
    one implementation."""
    per = {}
    for r in rows:
        if time.localtime(r["ts"]).tm_hour not in hours_of_day:
            continue
        per[key(r)] = per.get(key(r), 0) + 5 / 60
    return per


def bucket_rates(visit_buckets, exposure, min_visits=MIN_BUCKET_VISITS,
                 min_hours=MIN_BUCKET_HOURS):
    """Exposure-normalised findings, one per bucket.

    `effect` is the bucket's visits-per-hour against the species' overall
    visits-per-hour across all measured exposure: +0.8 means "80% more likely
    than usual", -0.2 means "20% less". THIS is the number the prose is
    allowed to speak, and the reason it's meaningful: a bucket holding 40% of
    the visits and 40% of the hours lands at 0.0 -- no effect -- where a raw
    count would have called it a pattern.

    `thin` marks a bucket whose sample can't support a confident claim; it is
    kept, not dropped, so the prose can hedge honestly instead of going
    silent."""
    total_visits = sum(visit_buckets.values())
    # The denominator is EVERY analysed hour, including hours in conditions
    # the bird was never heard in. Summing only the buckets that have visits
    # would drop (say) sixty silent rainy hours from the baseline, inflating
    # it and understating every effect measured against it -- and the silence
    # in those hours is precisely the evidence.
    buckets = sorted(set(visit_buckets) | set(exposure))
    total_hours = sum(exposure.get(b, 0.0) for b in buckets)
    findings = []
    for bucket in buckets:
        visits = visit_buckets.get(bucket, 0)
        hours = exposure.get(bucket, 0.0)
        if hours <= 0:
            continue
        rate = visits / hours
        baseline = (total_visits / total_hours) if total_hours > 0 else 0
        findings.append({
            "bucket": bucket,
            "visits": visits,
            "hours": round(hours, 1),
            "per_hour": round(rate, 4),
            "effect": round(rate / baseline - 1, 3) if baseline > 0 else None,
            "thin": visits < min_visits or hours < min_hours,
        })
    return findings


# --- The package the model is handed -----------------------------------------

def build_stats(visit_ts, observations, now, first_ts=None, sources=None):
    """Every computed finding for one species, in one auditable dict. Stored
    verbatim as stats_json beside the prose, so any sentence can be checked
    against the numbers it was written from."""
    hours = hour_histogram(visit_ts)
    active = active_hours(hours)
    index = observation_index(observations)

    # Match each visit to its weather, then bucket both the visits and the
    # exposure by the SAME rule.
    cond_visits, temp_visits, matched = {}, {}, 0
    for ts in visit_ts:
        if time.localtime(ts).tm_hour not in active:
            continue   # outside the analysed window; exposure excludes it too
        obs = weather_at(index, ts)
        if obs is None:
            continue
        matched += 1
        c = condition_bucket(obs["condition"], obs["rain_rate_inhr"])
        cond_visits[c] = cond_visits.get(c, 0) + 1
        t = temp_band(obs["temp_f"])
        temp_visits[t] = temp_visits.get(t, 0) + 1

    cond_exposure = exposure_hours(
        observations, active,
        lambda r: condition_bucket(r["condition"], r["rain_rate_inhr"]))
    temp_exposure = exposure_hours(
        observations, active, lambda r: temp_band(r["temp_f"]))

    weekdays = weekday_histogram(visit_ts)
    return {
        "total_visits": len(visit_ts),
        "first_ts": first_ts if first_ts is not None else (
            min(visit_ts) if visit_ts else None),
        "last_ts": max(visit_ts) if visit_ts else None,
        "hours": hours,
        "peak_window": peak_window(hours),
        "active_hours": sorted(active),
        "weekdays": weekdays,
        "busiest_weekday": (WEEKDAYS[weekdays.index(max(weekdays))]
                            if sum(weekdays) else None),
        "busiest_day": busiest_day(visit_ts),
        "trend": trend(visit_ts, now),
        "sources": sources or {},
        # The weather half; `matched` is what the weather claims actually rest
        # on, which is smaller than total_visits whenever the record predates
        # the archive -- the prose must never cite total_visits for a weather
        # claim, so it is reported separately.
        "weather": {
            "visits_matched": matched,
            "conditions": bucket_rates(cond_visits, cond_exposure),
            "temperature": bucket_rates(temp_visits, temp_exposure),
            "enough": matched >= MIN_VISITS_FOR_WEATHER,
        },
    }


def plural(n, singular, suffix="s"):
    """"1 visit" / "2 visits". The model reads these lines as its source of
    truth, and ungrammatical input invites ungrammatical prose."""
    return f"{n} {singular}{'' if n == 1 else suffix}"


def hour_label(h):
    """0 -> "12am", 13 -> "1pm" -- the voice the prose speaks in."""
    suffix = "am" if h < 12 else "pm"
    hour = h % 12 or 12
    return f"{hour}{suffix}"


def describe_stats(stats):
    """The findings as flat English lines. Prose in, prose out: the model
    reads sentences better than it reads JSON, and every number it could
    possibly use is already here, pre-computed, so it never has to derive
    one."""
    thin_overall = stats["total_visits"] < MIN_VISITS_FOR_WEATHER
    lines = [f"Total visits on record: {stats['total_visits']}."
             + (" Evidence: THIN throughout -- this bird has barely been "
                "heard, and the whole note should say so." if thin_overall
                else "")]
    if stats["first_ts"]:
        # "%-d" is glibc-only and this pass has to run on the desk too.
        first = time.localtime(stats["first_ts"])
        lines.append("First heard: "
                     + f"{time.strftime('%B', first)} {first.tm_mday}, "
                     + f"{first.tm_year}.")
    peak = stats["peak_window"]
    if peak:
        lines.append(
            f"Busiest stretch of the day: {hour_label(peak['start_hour'])} to "
            f"{hour_label(peak['end_hour'])}, holding "
            f"{plural(peak['visits'], 'visit')} "
            f"({round(peak['share'] * 100)}% of all of them).")
    # The weekday claim carries its own numbers, because "most visits fall on
    # a friday" is a very different statement at 2 visits than at 200 -- and
    # the model can only hedge on what it can see. A weekday needs a long
    # record before it means anything at all: seven buckets split thin, and
    # birds do not own calendars.
    if stats["busiest_weekday"]:
        top = max(stats["weekdays"])
        weekday_thin = stats["total_visits"] < 7 * MIN_BUCKET_VISITS
        lines.append(
            f"Busiest weekday: {stats['busiest_weekday']}, with "
            f"{plural(top, 'visit')} of {stats['total_visits']}. "
            f"Evidence: {evidence(weekday_thin)}.")
    day = stats["busiest_day"]
    if day:
        lines.append(
            f"Heard on {plural(day['days_observed'], 'separate day')}; the "
            f"busiest single day had {plural(day['visits'], 'visit')}.")
    t = stats["trend"]
    if t:
        lines.append(
            f"Last {t['window_days']} days: {plural(t['recent'], 'visit')}, "
            f"against {t['prior']} in the {t['window_days']} days before "
            "that.")
    if stats["sources"]:
        # The MAGNITUDE is characterised here rather than left to the model:
        # handed "amcrest: 744, rover: 1", gemma3 wrote "slightly more from
        # the amcrest microphone". Anything it can get wrong by describing,
        # describe for it.
        parts = ", ".join(f"{k}: {v}" for k, v in sorted(stats["sources"].items()))
        total = sum(stats["sources"].values())
        top, top_n = max(stats["sources"].items(), key=lambda kv: kv[1])
        share = top_n / total if total else 0
        if len(stats["sources"]) == 1:
            shape = f"Only one microphone ({top}) has ever picked this bird up."
        elif share >= 0.9:
            shape = (f"Almost all of them came from the {top} microphone "
                     f"({top_n} of {total}) -- the others barely hear this "
                     "bird at all.")
        elif share >= 0.65:
            shape = f"The {top} microphone hears this bird clearly the most."
        else:
            shape = "The microphones hear this bird at broadly similar rates."
        # A bird both mics hear is one visit overall but one apiece here, so
        # these deliberately need not sum to the total. Said out loud, or a
        # model noticing the mismatch would try to reconcile it.
        lines.append(
            f"Detections per microphone -- {parts}. {shape} (A bird heard by "
            "both counts once for each, so these need not add up to the "
            "total above.)")
    return "\n".join(lines)


def describe_weather(stats):
    """The weather findings as English, effects already computed as
    percentages so the model never multiplies anything."""
    w = stats["weather"]
    if not w["enough"]:
        return (f"Only {w['visits_matched']} visits line up with the weather "
                "archive so far -- too few to claim a pattern.")
    lines = [f"These weather figures rest on {w['visits_matched']} visits "
             "that line up with the weather archive.",
             "Hours are only counted during this bird's own active hours ("
             + ", ".join(hour_label(h) for h in stats["active_hours"])
             + "), so time of day cannot masquerade as weather."]
    # Temperature bands track the calendar as much as the thermometer -- a
    # "warm" figure over a year-long record is partly just summer. Said here
    # so the prose can describe the association without implying the bird is
    # responding to the temperature itself.
    if any(f["effect"] is not None for f in w["temperature"]):
        lines.append("Note: temperature bands follow the seasons, so a "
                     "temperature figure may be describing the time of year "
                     "as much as the warmth itself.")
    # Direction is a LABEL, not a phrase to parse. Desk-tested: given
    # "heard 82% more often", gemma3 still wrote "notably quiet" -- it
    # reasoned from what it assumes about birds instead of from the figure.
    # An uppercase keyword in front of the number is much harder to invert.
    for group, label in (("conditions", "sky"), ("temperature", "temperature")):
        for f in w[group]:
            if f["effect"] is None:
                continue
            pct = round(f["effect"] * 100)
            if abs(pct) < 10:
                verdict = "ABOUT AVERAGE"
            else:
                verdict = (f"{'MORE OFTEN' if pct > 0 else 'LESS OFTEN'} "
                           f"({abs(pct)}% {'above' if pct > 0 else 'below'} "
                           "this bird's own average)")
            lines.append(
                f"- {label.upper()} {f['bucket']}: {verdict}. Based on "
                f"{plural(f['visits'], 'visit')} across {f['hours']} hours. "
                f"Evidence: {evidence(f['thin'])}.")
    return "\n".join(lines)


# --- The prompts --------------------------------------------------------------

VOICE = (
    "You are a field naturalist writing a short note for a backyard "
    "birdwatching page. Your reader keeps a microphone in their driveway and "
    "loves birds; they are not a scientist. Be warm, specific and a little "
    "wry. Plain prose, no lists, no headings, no emoji."
)

RULES = (
    "Hard rules:\n"
    "- Use ONLY the figures given. Never invent, estimate, round differently, "
    "or calculate a new number. If a figure is not listed, do not state it.\n"
    "- The notes use ANNOTATIONS in capitals -- MORE OFTEN, LESS OFTEN, "
    "ABOUT AVERAGE, 'Evidence: solid', 'Evidence: THIN'. These are labels "
    "for you to read, NEVER words to copy. They must not appear anywhere in "
    "what you write; say it in ordinary English instead.\n"
    "- Never reverse a direction. If a finding is labelled MORE OFTEN, the "
    "bird was heard MORE -- write it that way, whatever you think you know "
    "about birds.\n"
    "- A finding labelled THIN gets a hedge in your own words, tied to that "
    "finding alone. A finding labelled solid must NOT be hedged, and a hedge "
    "must never carry from one sentence into the next.\n"
    "- Do not assert WHY a bird behaves as the figures show -- you were given "
    "no causes. You may wonder aloud ONCE, clearly marked as a guess "
    "(\"perhaps\"), and you may repeat habits stated in the background.\n"
    "- Two short paragraphs at most. No preamble, no sign-off, no restating "
    "the bird's name as a heading."
)


def evidence(thin):
    """The one phrase the model keys its hedging off. A literal example hedge
    in the rules turned out to be worse than none: gemma3 lifted the sample
    wording verbatim and stapled it onto findings resting on hundreds of
    visits, so the rules now name the LABEL and leave the words to the
    model."""
    return "THIN -- hedge this one" if thin else "solid"


def rhythm_prompt(common, description, stats):
    return (
        f"The bird: {common}.\n"
        + (f"Background: {description.strip()[:900]}\n" if description else "")
        + "\nWhat our own driveway microphone has recorded:\n"
        + describe_stats(stats)
        + "\n\nWrite the note about WHEN this bird shows up -- its rhythm "
          "through the day and week, and whether it is coming around more or "
          "less lately.\n\n" + RULES
    )


def weather_prompt(common, stats):
    return (
        f"The bird: {common}.\n"
        "\nWhat our own weather station and microphone say together:\n"
        + describe_weather(stats)
        + "\n\nWrite the note about how WEATHER and conditions move this "
          "bird's odds of turning up. If the evidence is too thin to say "
          "much, say that in one honest, good-humoured sentence and stop -- "
          "do not pad it.\n\n" + RULES
    )


# --- Store --------------------------------------------------------------------

def worklist(conn, step=WATERMARK_STEP):
    """Species needing an analysis: never written, or grown by `step` visits
    since the last write. The watermark is a visit COUNT rather than an age --
    a bird nobody has heard since June has nothing new to say about itself,
    and regenerating it would just burn a paragraph."""
    rows = conn.execute(
        "SELECT l.species_sci, l.species_common, a.visits_watermark"
        " FROM life_list l"
        " LEFT JOIN species_analysis a ON a.species_sci = l.species_sci"
        " ORDER BY l.species_common").fetchall()
    out = []
    for r in rows:
        visits = len(group_visits(
            [s["ts"] for s in conn.execute(
                "SELECT ts FROM sightings WHERE species_sci = ? ORDER BY ts",
                (r["species_sci"],))]))
        mark = r["visits_watermark"]
        if mark is None or visits - mark >= step:
            out.append((r["species_sci"], r["species_common"], visits))
    return out


def load_observations(path, since, until):
    """The archive rows covering the visit record. Read-only, opened per
    call, and a missing/unreadable archive is an empty list -- no weather
    block, never an error (the /weather/history posture)."""
    if not path or since is None:
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        return conn.execute(
            "SELECT ts, temp_f, condition, rain_rate_inhr FROM observations"
            " WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (int(since), int(until))).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def analyze_species(conn, sci, common, *, ollama, weather_path,
                    now=None, dry_run=False):
    """One species, end to end. Returns a status word:
      'no-visits'  -- nothing on record yet; nothing written
      'stats-only' -- --dry-run, or no LLM configured
      'llm-down'   -- generation failed; any existing row LEFT UNTOUCHED
      'written'    -- both blocks generated and stored
    """
    now = now or time.time()
    rows = [r["ts"] for r in conn.execute(
        "SELECT ts FROM sightings WHERE species_sci = ? ORDER BY ts", (sci,))]
    visits = group_visits(rows)
    if not visits:
        return "no-visits", None

    sources = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) n FROM sightings WHERE species_sci = ?"
        " GROUP BY source", (sci,))}
    first = conn.execute(
        "SELECT first_ts FROM life_list WHERE species_sci = ?", (sci,)).fetchone()
    observations = load_observations(weather_path, min(visits), max(visits))
    stats = build_stats(visits, observations, now,
                        first_ts=first["first_ts"] if first else None,
                        sources=sources)

    if dry_run or ollama is None:
        return "stats-only", stats

    # Phase 2's description is optional context, so a store where that pass
    # has never run (no species_profile table at all) still gets its
    # analysis -- just without the background paragraph.
    try:
        profile = conn.execute(
            "SELECT description FROM species_profile WHERE species_sci = ?",
            (sci,)).fetchone()
        description = profile["description"] if profile else None
    except sqlite3.Error:
        description = None

    # The narrator's default ceiling is a live-show pacing number; this is a
    # patient batch job, so it passes its own.
    rhythm = ollama.complete(VOICE, rhythm_prompt(common, description, stats),
                             num_predict=NUM_PREDICT, temperature=0.7,
                             timeout=ANALYSIS_TIMEOUT_S)
    weather = ollama.complete(VOICE, weather_prompt(common, stats),
                              num_predict=NUM_PREDICT, temperature=0.7,
                              timeout=ANALYSIS_TIMEOUT_S)
    # A dead or slow model leaves the existing row exactly as it was -- the
    # narrator's degrade-never-crash ethos, and the species stays on the
    # worklist for the next run.
    if not rhythm or not weather:
        return "llm-down", stats

    conn.execute(
        "INSERT OR REPLACE INTO species_analysis (species_sci, rhythm_text,"
        " weather_text, stats_json, visits_watermark, model, generated_ts)"
        " VALUES (?,?,?,?,?,?,?)",
        (sci, rhythm.strip(), weather.strip(), json.dumps(stats),
         len(visits), ollama.model, int(now)))
    conn.commit()
    return "written", stats


def main():
    ap = argparse.ArgumentParser(
        description="Write the Aviary's field-naturalist blocks (issue #186)")
    ap.add_argument("--refresh", metavar="SPECIES_SCI",
                    help="re-analyze one species regardless of its watermark")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print the findings; no LLM, no writes")
    args = ap.parse_args()

    path = db_path()
    conn = connect(path)
    ollama = None
    if not args.dry_run:
        addr = ollama_address()
        if addr:
            model = os.environ.get("MERLE_OLLAMA_MODEL", "").strip() \
                or OLLAMA_DEFAULT_MODEL
            ollama = Ollama(*addr, model)
            print(f"[analysis] narrating with {model} via "
                  f"{addr[0]}:{addr[1]}", flush=True)
        else:
            print("[analysis] MERLE_OLLAMA not set -- stats only", flush=True)

    try:
        if args.refresh:
            row = conn.execute(
                "SELECT species_common FROM life_list WHERE species_sci = ?",
                (args.refresh,)).fetchone()
            todo = [(args.refresh,
                     row["species_common"] if row else args.refresh, 0)]
        else:
            todo = worklist(conn)
        if not todo:
            print("[analysis] nothing to do -- every species is current",
                  flush=True)
            return
        print(f"[analysis] {len(todo)} species to write up -> {path}",
              flush=True)
        counts = {}
        for sci, common, _visits in todo:
            try:
                status, stats = analyze_species(
                    conn, sci, common, ollama=ollama,
                    weather_path=weather_db_path(), dry_run=args.dry_run)
            except Exception as e:
                if args.refresh:
                    raise
                print(f"[analysis] {common}: FAILED ({e})", flush=True)
                counts["failed"] = counts.get("failed", 0) + 1
                continue
            counts[status] = counts.get(status, 0) + 1
            print(f"[analysis] {common}: {status}", flush=True)
            if args.dry_run and stats:
                print(describe_stats(stats), flush=True)
                print(describe_weather(stats), flush=True)
                print("", flush=True)
        print("[analysis] done: " + ", ".join(
            f"{v} {k}" for k, v in sorted(counts.items())), flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
