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
# HYGIENE (issue #612), the 2026-10-24 review's fixes:
#   - NOTHING IS STORED UNCHECKED. Every block goes through
#     listener/notes_validate.py (figures in the prose must appear in the
#     prompt; no annotation labels, no `unknown`, no "our microphone", no
#     "driveway" unless a place phrase says so, no stock opener). One retry
#     with the violations quoted; a second failure stores the PLAIN RECORD
#     (plain_rhythm/plain_weather -- the facts as sentences, no model) and
#     marks the row `fallback = 1` so the page can say so.
#   - THE YARD'S OWN VOCABULARY. Microphones are named by their `place:`
#     phrase from the feeds registry (feeds.places()), retired labels are
#     summarised as "formerly", and nothing calls the yard a driveway --
#     on a station the phrases come from that station's own registry.
#   - NOISE LINES OUT. No weekday line (birds own no calendar, and fourteen
#     weeks of it read as a preference in nearly every note); no `unknown`
#     weather bucket (a sensor dropout is not a fact about a bird).
#   - LEDE FIRST. The fact lines are ordered so the model opens on the most
#     newsworthy one -- a clearly moved trend, else the peak -- instead of
#     the same total-visits sentence for every bird.
#   - FROZEN ROWS PURGED. A sub-floor species' note at an old prompt version
#     can never regenerate (the #407 floor runs first), so purge_stale()
#     deletes it and the page shows the honest no-notes state.
#   - PACING. The regeneration step grows with the record (due_step): a
#     3,400-visit regular no longer re-narrates the same facts daily.
#
# Config (env):
#   MERLE_EARL_DB       the bird record (default "earl.db")
#   MERLE_WEATHER_DB    the weather archive (default "weather.db") -- read
#                       only; unset/missing simply means no weather block
#   MERLE_OLLAMA        Ollama "host[:port]" -- or, since #217, a comma-
#                       separated preference-ordered list of them; this CLI
#                       uses the first entry, the enrichment loop probes them
#                       in order. UNSET = no generation (the narrator's kill
#                       switch, same semantics)
#   MERLE_OLLAMA_MODEL  model name (default: the narrator's) -- CLI only;
#                       the loop picks by MODEL_RANK from what a host holds
#   MERLE_MODEL_RANK    override MODEL_RANK (comma-separated, best first)
# =============================================================================

import argparse
import bisect
import hashlib
import json
import math
import os
import sqlite3
import time

import feeds
from listener import gate, notes_validate
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
# Floor before a species is eligible for ANY field-notes generation
# (issue #407). The worklist's "never written" arm used to fire on first
# contact, which spun Ollama for one-and-done low-confidence false
# positives. Three grouped visits is enough to skip flukes without
# starving a real morning visitor of notes. Separate from
# MIN_BUCKET_VISITS (bucket thinness) on purpose -- same number, different
# job. --refresh / force still bypasses; this is worklist-only.
MIN_VISITS_FOR_ANALYSIS = 3
# Regenerate once a species has this many new visits since its last write.
# Dropped from 20 with issue #217: the fingerprint gate below catches
# anything this wider net lets through before an LLM call is spent, so the
# watermark no longer has to be the only thing standing between a rare bird
# and months of stale notes.
WATERMARK_STEP = 5
# ...and the step grows with the record (issue #612): due_step() is the
# larger of WATERMARK_STEP and this share of the species' visits. A 20-visit
# bird still regenerates after 5 new visits; a 3,400-visit regular after
# 340, instead of re-narrating identical facts every single day (141
# generations in the 30 days before #612, nearly all of them regulars).
WATERMARK_SHARE = 0.10
# A trend is the note's lede only when it clearly moved: the larger window
# holds at least TREND_MIN visits and the two differ by TREND_MOVED of it.
TREND_MIN = 5
TREND_MOVED = 0.30
# Two short paragraphs; the cap is a backstop for a model ignoring the rules.
NUM_PREDICT = 320
# Analysis is a long, patient job (one call per species, ~30-90s on CPU), not
# a live show like the narrator's 30s pacing gate.
ANALYSIS_TIMEOUT_S = 300

# Bumped BY HAND whenever the prompts or the stats shape change (issue #217).
# A stored row whose prompt_version differs is due for regeneration on the
# next tick a model is up -- which is how a prompt edit re-narrates the whole
# archive with no migration and no manual pass.
PROMPT_VERSION = 3

# Model preference, best first (issue #217). Two hosts running different
# models would otherwise thrash -- the basement box narrates everything on a
# big model, a tick lands on bluejay, and every row "mismatches" back down to
# gemma3:12b, endlessly. So regeneration only ever triggers UPWARD: a model
# regenerates a row only when it OUTRANKS the stored one (see outranks()).
# Unknown models sort last. Override without a deploy via MERLE_MODEL_RANK
# (comma-separated, best first).
#
# gemma3:27b was dropped from the rank (issue #493). This task does not need
# it: the arithmetic is done above, and "eight fact lines into two capped
# paragraphs" is prompt-bound, not model-bound. It cost ~33 s a note at
# 19 GB split across CPU and GPU on bluejay, and evicted every other tenant.
# Staying in the gemma3 family is deliberate -- the prompts below are
# hand-tuned against gemma3 quirks (hedge-lifting, magnitude phrasing). A
# stored 27b row now sorts as UNKNOWN, i.e. last, so 12b outranks it and the
# archive re-narrates downward exactly once, paced by the loop's tick cap.
MODEL_RANK = ("gemma3:12b", "gemma3:4b")

SCHEMA = """
CREATE TABLE IF NOT EXISTS species_analysis (
    species_sci       TEXT PRIMARY KEY,
    rhythm_text       TEXT,
    weather_text      TEXT,
    stats_json        TEXT,
    visits_watermark  INTEGER NOT NULL DEFAULT 0,
    model             TEXT,
    generated_ts      INTEGER NOT NULL,
    stats_fingerprint TEXT,
    prompt_version    INTEGER,
    host              TEXT,
    checked_ts        INTEGER,
    fallback          INTEGER
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
    # Issue #217's upgrade, the repeatable-pass rule (species_profile's
    # image_w idiom): a #186-era file lacks the gate columns. Added in place;
    # fresh files get them from SCHEMA. A row left honestly NULL here is
    # picked up by the prompt-version arm of the worklist, regenerates once
    # to fill them, then settles.
    columns = {r["name"] for r in
               conn.execute("PRAGMA table_info(species_analysis)")}
    for col, decl in (("stats_fingerprint", "TEXT"),
                      ("prompt_version", "INTEGER"),
                      ("host", "TEXT"),
                      ("checked_ts", "INTEGER"),
                      # #612: 1 when either block is the plain record
                      # because the model broke the rules twice; NULL on
                      # older rows reads as 0.
                      ("fallback", "INTEGER")):
        if col not in columns:
            conn.execute(
                f"ALTER TABLE species_analysis ADD COLUMN {col} {decl}")
    conn.commit()
    return conn


def model_rank_list():
    """MODEL_RANK, overridable via MERLE_MODEL_RANK (comma-separated, best
    first) so a new basement-box model can be slotted in without a deploy."""
    raw = os.environ.get("MERLE_MODEL_RANK", "").strip()
    if not raw:
        return list(MODEL_RANK)
    return [m.strip() for m in raw.split(",") if m.strip()]


def outranks(candidate, stored, rank=None):
    """True when `candidate` is strictly better-ranked than `stored` -- the
    only direction a model change may trigger regeneration. Unknown models
    sort last; two unknowns tie, and a tie never triggers. The consequence
    that matters: a lower-ranked host can never claw back a row a
    higher-ranked one wrote, so two hosts holding different models settle
    instead of thrashing."""
    if candidate == stored:
        return False
    order = model_rank_list() if rank is None else list(rank)

    def idx(model):
        try:
            return order.index(model)
        except ValueError:
            return len(order)

    return idx(candidate) < idx(stored)


def stats_fingerprint(stats, description, context=""):
    """The identity of one species' PROMPT INPUTS (issue #217): the full
    stats package plus the exact description snippet rhythm_prompt() feeds
    the model. Identical fingerprint + matching prompt version + a model
    that doesn't outrank means a regeneration would only rephrase the same
    facts at temperature 0.7 -- so it doesn't happen. The description rides
    the hash because it is a real input the stats can't see: the profile
    pass landing a Wikipedia background later must read as changed inputs.
    `context` is the same idea for the registry's place phrases (#612,
    places_key()): renaming where a microphone listens is a changed input."""
    snippet = description.strip()[:900] if description else ""
    payload = json.dumps(stats, sort_keys=True) + "\x00" + snippet
    if context:
        payload += "\x00" + context
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- Places (issue #612) ------------------------------------------------------

def load_places():
    """The registry's place phrases (feeds.places()), or {} when the
    registry can't be read -- the notes then name microphones by their raw
    labels, as before #612. Never a reason to stop writing notes: Earl
    itself fails loud on a broken registry, which is where it belongs."""
    try:
        return feeds.places()
    except feeds.FeedsError as e:
        print(f"[analysis] feeds registry unreadable for place names ({e}) "
              "-- naming microphones by label", flush=True)
        return {}


def places_key(places):
    """A stable string of the place phrases, for the fingerprint."""
    return "|".join(f"{k}={p.phrase}:{int(p.current)}"
                    for k, p in sorted((places or {}).items()))


def allow_driveway(places):
    """True only when one of THIS registry's phrases says "driveway" -- the
    word is a fact about the house's rear camera, never a name for a yard."""
    return any(p.phrase and "driveway" in p.phrase.lower()
               for p in (places or {}).values())


def join_and(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def listening_posts(places):
    """One sentence naming where the reader listens from (the live feeds
    only), or "" without a registry. Semicolons, because the phrases carry
    their own commas ("the back of the house, over the driveway")."""
    live = [p.phrase or f'"{k}"' for k, p in (places or {}).items()
            if p.current]
    if not live:
        return ""
    if len(live) == 1:
        return f"Your reader listens from one place: {live[0]}."
    return (f"Your reader listens from {len(live)} places: "
            + "; ".join(live[:-1]) + "; and " + live[-1] + ".")


def mic_name(label, places):
    """How the fact lines name one source: by its place phrase when the
    registry has one, else as "the <label> microphone" (the pre-#612
    wording, which tests and a registry-less dev checkout still see). A
    source that no longer listens is "the old microphone at ..." -- the
    house's two garage boards share a place, and the first desk run of
    #612 garbled them when only the phrase told them apart."""
    p = (places or {}).get(label)
    if p is not None and p.phrase:
        return (f"the microphone at {p.phrase}" if p.current
                else f"the old microphone at {p.phrase}")
    return f"the {label} microphone"


def is_live(label, places):
    """A source still listening. With no registry at all every source
    counts as live (no claim either way); with one, a label the registry
    does not run -- retired, Earl-off like the rover, or unknown -- is not."""
    if not places:
        return True
    p = places.get(label)
    return bool(p and p.current)


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
    return f"{n:,} {singular}{'' if n == 1 else suffix}"


def hour_label(h):
    """0 -> "12am", 13 -> "1pm" -- the voice the prose speaks in."""
    suffix = "am" if h < 12 else "pm"
    hour = h % 12 or 12
    return f"{hour}{suffix}"


def trend_moved(t):
    """True when the 7-vs-7 trend is news: the busier window holds at least
    TREND_MIN visits and the two differ by TREND_MOVED of it. Pure, so the
    lede rule is tested rather than eyeballed."""
    if not t:
        return False
    hi = max(t["recent"], t["prior"])
    return hi >= TREND_MIN and abs(t["recent"] - t["prior"]) >= TREND_MOVED * hi


def gone_quiet(t):
    """True when both trend windows are empty: the bird has not been heard
    for two full windows. Said in Python as its own fact ("not heard at all
    in the last 14 days; last heard ...") and made the lede -- a migrant
    that left is the most interesting thing its note can say, and handed
    "0 visits, against 0" the model wrote "mirroring the 7 days before"."""
    return bool(t) and t["recent"] == 0 and t["prior"] == 0


def first_heard_text(ts):
    # "%-d" is glibc-only and this pass has to run on the desk too.
    first = time.localtime(ts)
    return f"{time.strftime('%B', first)} {first.tm_mday}, {first.tm_year}"


def describe_sources(sources, places=None):
    """The microphone line, or None. The MAGNITUDE is characterised here
    rather than left to the model: handed "amcrest: 744, rover: 1", gemma3
    wrote "slightly more from the amcrest microphone". Anything it can get
    wrong by describing, describe for it.

    Since #612 each source is named by its registry place phrase, and a
    source that no longer listens (retired, Earl-off, unknown to the
    registry) is summarised as "formerly", never as a current microphone.
    The shape is judged over the LIVE microphones when any heard the bird --
    "which part of the yard hears it" is a claim about today's yard."""
    if not sources:
        return None
    live = {k: v for k, v in sources.items() if is_live(k, places)}
    gone = {k: v for k, v in sources.items() if k not in live}
    judged = live or sources
    total = sum(judged.values())
    top, top_n = max(sorted(judged.items()), key=lambda kv: kv[1])
    share = top_n / total if total else 0
    if len(judged) == 1:
        shape = f"Only one microphone has ever heard this bird: {mic_name(top, places)}."
    elif share >= 0.9:
        shape = (f"Almost all of them came from {mic_name(top, places)} "
                 f"({top_n:,} of {total:,}) -- the others barely hear this "
                 "bird at all.")
    elif share >= 0.65:
        name = mic_name(top, places)
        shape = f"{name[0].upper()}{name[1:]} hears this bird clearly the most."
    else:
        shape = "The microphones hear this bird at broadly similar rates."
    by_count = lambda d: sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))
    parts = "; ".join(f"{mic_name(k, places)}: {v:,}"
                      for k, v in by_count(judged))
    line = f"Detections per microphone -- {parts}. {shape}"
    if live and gone:
        line += (" Formerly also heard by "
                 + join_and(f"{mic_name(k, places)} ({v:,})"
                            for k, v in by_count(gone))
                 + ", which no longer listen"
                 + ("s" if len(gone) == 1 else "") + ".")
    elif not live and places:
        line += " None of these microphones is listening any more."
    # A bird two mics hear is one visit overall but one apiece here, so
    # these deliberately need not sum to the total. Said out loud, or a
    # model noticing the mismatch would try to reconcile it.
    return line + (" (A bird heard by two microphones counts once for each, "
                   "so these need not add up to the total above.)")


def describe_stats(stats, places=None):
    """The findings as flat English lines. Prose in, prose out: the model
    reads sentences better than it reads JSON, and every number it could
    possibly use is already here, pre-computed, so it never has to derive
    one.

    ORDER IS THE LEDE (#612): the rules say "open with the first fact
    given", so the first line is the most newsworthy -- a clearly moved
    trend, else the busiest stretch of the day -- and the identical
    total-visits opener every note used to share goes second. The weekday
    line is gone: fourteen weeks split seven ways is noise, and it read as a
    preference ("Saturdays are their preferred day") in nearly every note."""
    thin_overall = stats["total_visits"] < MIN_VISITS_FOR_WEATHER
    total = (f"Total visits on record: {stats['total_visits']:,}."
             + (" Evidence: THIN throughout -- this bird has barely been "
                "heard, and the whole note should say so." if thin_overall
                else ""))
    first = (f"First heard: {first_heard_text(stats['first_ts'])}."
             if stats["first_ts"] else None)
    peak = stats["peak_window"]
    peak_line = (
        f"Busiest stretch of the day: {hour_label(peak['start_hour'])} to "
        f"{hour_label(peak['end_hour'])}, holding "
        f"{plural(peak['visits'], 'visit')} "
        f"({round(peak['share'] * 100)}% of all of them)." if peak else None)
    day = stats["busiest_day"]
    day_line = (
        f"Heard on {plural(day['days_observed'], 'separate day')}; the "
        f"busiest single day had {plural(day['visits'], 'visit')}."
        if day else None)
    t = stats["trend"]
    trend_line = None
    if gone_quiet(t):
        last = (f"; last heard {first_heard_text(stats['last_ts'])}"
                if stats["last_ts"] else "")
        trend_line = (f"Not heard at all in the last "
                      f"{2 * t['window_days']} days{last}.")
    elif t:
        trend_line = (
            f"Last {t['window_days']} days: {plural(t['recent'], 'visit')}, "
            f"against {t['prior']:,} in the {t['window_days']} days before "
            "that.")
        if trend_moved(t):
            trend_line += (" That is a clear rise." if t["recent"] > t["prior"]
                           else " That is a clear fall.")
    news = trend_moved(t) or gone_quiet(t)
    lede = trend_line if news else (peak_line or total)
    rest = [total, first, peak_line, day_line, trend_line,
            describe_sources(stats["sources"], places)]
    return "\n".join([lede] + [x for x in rest if x and x is not lede])


def describe_weather(stats):
    """The weather findings as English, effects already computed as
    percentages so the model never multiplies anything."""
    w = stats["weather"]
    if not w["enough"]:
        return (f"Only {w['visits_matched']:,} visits line up with the weather "
                "archive so far -- too few to claim a pattern.")
    lines = [f"These weather figures rest on {w['visits_matched']:,} visits "
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
    for label, f in weather_findings(stats):
        # Direction is a LABEL, not a phrase to parse. Desk-tested: given
        # "heard 82% more often", gemma3 still wrote "notably quiet" -- it
        # reasoned from what it assumes about birds instead of from the
        # figure. An uppercase keyword in front of the number is much
        # harder to invert.
        #
        # And THE BIRD IS THE SUBJECT (#612). The old "SKY rain: MORE OFTEN
        # (44% above ...)" let the model hang the figure on the weather --
        # "your yard has been experiencing rain more frequently than usual,
        # almost 44% above the cardinal's own typical rate", "83% of visits
        # occurring when the temperature is below its average". Each
        # finding is now a finished sentence about the bird's own rate, so
        # there is nothing left for the figure to attach to but the bird.
        pct = round(f["effect"] * 100)
        when = (WHEN_SKY.get(f["bucket"], f"In {f['bucket']} weather")
                if label == "sky" else f"In {f['bucket']} weather")
        if abs(pct) < 10:
            rate = "at its usual rate (ABOUT AVERAGE)"
        else:
            rate = (f"{abs(pct)}% {'MORE OFTEN' if pct > 0 else 'LESS OFTEN'}"
                    " than its own average rate")
        lines.append(
            f"- {when}, this bird turns up {rate}. (This rests on "
            f"{plural(f['visits'], 'visit')} over {f['hours']} hours of that "
            f"weather. Evidence: {evidence(f['thin'])}.)")
    return "\n".join(lines)


WHEN_SKY = {"clear": "Under clear skies", "cloudy": "Under cloudy skies",
            "rain": "When it rains", "snow": "When it snows"}


def weather_findings(stats):
    """The weather buckets worth a line, strongest first: solid findings
    before thin ones, then by size of effect (#612 -- the model opens on
    the first line, so the first line should be the real story). Two kinds
    never appear: an `unknown` bucket (a sensor dropout narrated as "a
    curious result" in three notes on 2026-10-24) and a bucket with no
    computable effect."""
    out = []
    for group, label in (("conditions", "sky"), ("temperature", "temperature")):
        for f in stats["weather"][group]:
            if f["effect"] is None or f["bucket"] == "unknown":
                continue
            out.append((label, f))
    return sorted(out, key=lambda lf: (lf[1]["thin"], -abs(lf[1]["effect"]),
                                       lf[0], lf[1]["bucket"]))


# --- The plain record (#612): the facts as sentences, no model ---------------
#
# What gets stored when the model breaks the rules twice. Built from the same
# stats the prompt carried, so every figure in it is one the page's margin
# figures already show; deliberately unadorned -- a dull true sentence beats
# a lively wrong one, and the dateline says "plain record" so nobody mistakes
# it for the naturalist's voice.

SKY_WORDS = {"clear": "clear skies", "cloudy": "cloudy skies",
             "rain": "rain", "snow": "snow"}


def plain_rhythm(stats, places=None):
    parts = []
    since = (f" since {first_heard_text(stats['first_ts'])}"
             if stats["first_ts"] else "")
    parts.append(f"{plural(stats['total_visits'], 'visit')} on record{since}.")
    peak = stats["peak_window"]
    if peak:
        parts.append(
            f"The busiest stretch of the day is {hour_label(peak['start_hour'])}"
            f" to {hour_label(peak['end_hour'])}, with "
            f"{round(peak['share'] * 100)}% of all visits.")
    day = stats["busiest_day"]
    if day:
        parts.append(
            f"Heard on {plural(day['days_observed'], 'separate day')}; the "
            f"busiest single day had {plural(day['visits'], 'visit')}.")
    t = stats["trend"]
    if gone_quiet(t):
        parts.append(f"Not heard at all in the last {2 * t['window_days']} "
                     "days.")
    elif t:
        parts.append(
            f"In the last {t['window_days']} days: "
            f"{plural(t['recent'], 'visit')}, against {t['prior']:,} in the "
            f"{t['window_days']} days before.")
    live = {k: v for k, v in (stats["sources"] or {}).items()
            if is_live(k, places)}
    if live and places:
        top = max(sorted(live.items()), key=lambda kv: kv[1])[0]
        parts.append(f"Heard most often by {mic_name(top, places)}.")
    return " ".join(parts)


def plain_weather(stats):
    w = stats["weather"]
    if not w["enough"]:
        return (f"Only {plural(w['visits_matched'], 'visit')} line up with the "
                "weather record so far, too few to call a pattern.")
    said = []
    for label, f in weather_findings(stats):
        pct = round(f["effect"] * 100)
        if f["thin"] or abs(pct) < 10:
            continue
        where = (SKY_WORDS.get(f["bucket"], f["bucket"]) if label == "sky"
                 else f"{f['bucket']} weather")
        said.append(f"Heard {abs(pct)}% {'more' if pct > 0 else 'less'} "
                    f"often than usual in {where}.")
        if len(said) == 3:
            break
    if not said:
        return (f"No weather pattern stands out yet across "
                f"{plural(w['visits_matched'], 'matched visit')}.")
    return " ".join(said)


# --- The prompts --------------------------------------------------------------

VOICE = (
    "You are a field naturalist writing a short note for a backyard "
    "birdwatching page. Your reader listens to their own yard through a few "
    "microphones and loves birds; they are not a scientist. Be warm, "
    "specific and a little wry. Plain prose, no lists, no headings, no emoji."
)

RULES = (
    "Hard rules:\n"
    "- Use ONLY the figures given. Never invent, estimate, round differently, "
    "or calculate a new number. If a figure is not listed, do not state it. "
    "Write every figure in digits, exactly as it is given; never spell a "
    "number out as a word.\n"
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
    "- Address the reader as \"you\". The yard, the microphones and the "
    "weather station are the reader's -- write \"your yard\", never \"our\" "
    "or \"my\". Call a microphone by the place it listens from, exactly as "
    "given, and never invent a place.\n"
    "- Open with the first fact given, in your own words. Do not open by "
    "saying a microphone heard or picked up the bird, and never write \"a "
    "good number of\".\n"
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


def where_line(places):
    posts = listening_posts(places)
    return f"\nWhere your reader listens: {posts}\n" if posts else ""


def rhythm_lede(stats, places=None):
    """The fact the rhythm note must open with: describe_stats' first line."""
    return describe_stats(stats, places).split("\n", 1)[0]


def weather_lede(stats):
    """The finding the weather note must open with, or None when there is no
    story to lead with: too few matched visits, or a strongest finding that
    is thin or within 10% of average (opening on "about average" would be
    a lede in name only)."""
    if not stats["weather"]["enough"]:
        return None
    found = weather_findings(stats)
    if not found:
        return None
    _, f = found[0]
    if f["thin"] or abs(round(f["effect"] * 100)) < 10:
        return None
    return next(line for line in describe_weather(stats).split("\n")
                if line.startswith("- "))


def rhythm_prompt(common, description, stats, places=None):
    lede, _, rest = describe_stats(stats, places).partition("\n")
    return (
        f"The bird: {common}.\n"
        + (f"Background: {description.strip()[:900]}\n" if description else "")
        + where_line(places)
        + "\nOpen the note with this fact:\n" + lede
        + "\n\nOther facts you may use, from what your reader's microphones "
          "have recorded:\n" + rest
        + "\n\nWrite the note about WHEN this bird shows up -- its rhythm "
          "through the day, and whether it is coming around more or less "
          "lately.\n\n" + RULES
    )


def weather_prompt(common, stats, places=None):
    # No listening posts here (#612's first desk run): handed them, the
    # model invented per-microphone weather claims ("the microphones at the
    # back of the garage recorded no instances during rainfall"). Weather is
    # matched per VISIT across the whole yard; no figure here is per-mic.
    # `places` is accepted so both prompt builders share one signature.
    lede = weather_lede(stats)
    return (
        f"The bird: {common}.\n"
        + (f"\nOpen the note with this finding:\n{lede}\n" if lede else "")
        + "\nWhat your reader's weather station and microphones say together, "
          "for the whole yard (strongest finding first):\n"
        + describe_weather(stats)
        + "\n\nWrite the note about how WEATHER and conditions move this "
          "bird's odds of turning up. Each percentage compares this bird's "
          "own rate in that weather with its own overall rate; it is never "
          "a share of visits or of hours. These figures describe the whole "
          "yard: do not mention any microphone or place. If the evidence is "
          "too thin to say much, say that in one honest, good-humoured "
          "sentence and stop -- do not pad it.\n\n" + RULES
    )


# --- Store --------------------------------------------------------------------

def profile_fetch_times(conn):
    """species_sci -> the profile pass's fetched_ts, or {} on a store where
    that pass has never run (no table is a normal state, not an error)."""
    try:
        return {r["species_sci"]: r["fetched_ts"] for r in conn.execute(
            "SELECT species_sci, fetched_ts FROM species_profile")}
    except sqlite3.Error:
        return {}


def worklist(conn, step=WATERMARK_STEP, model=None,
             min_visits=MIN_VISITS_FOR_ANALYSIS):
    """Species needing an analysis. The watermark is a visit COUNT rather
    than an age -- a bird nobody has heard since June has nothing new to say
    about itself, and regenerating it would just burn a paragraph.

    This is the CHEAP pre-filter of #217's two-stage gate, so its arms are
    deliberately over-inclusive -- anything it lets through still has to get
    past the stats fingerprint in analyze_species() before an LLM call is
    spent. The arms (only considered once `visits >= min_visits`, #407):
      (0) visit floor -- fewer than MIN_VISITS_FOR_ANALYSIS grouped visits
          means not a candidate at all (one-off false positives must not
          spin Ollama; --refresh / force still analyzes by hand)
      (1) never written
      (2) grown by due_step(visits, step) since the last write (the
          original arm; the step grows with the record since #612)
      (3) stored prompt_version differs from PROMPT_VERSION (includes the
          honest-NULL rows a pre-#217 store carries -- they regenerate once)
      (4) the model this run would use OUTRANKS the stored one (never the
          reverse -- see outranks())
      (5) the profile pass touched this species after the note was last
          written OR gate-checked -- a description may have landed; the
          fingerprint decides. checked_ts is what keeps a dimensions-only
          profile touch from parking the species here forever.
    """
    rows = conn.execute(
        "SELECT l.species_sci, l.species_common, a.visits_watermark,"
        "       a.prompt_version, a.model, a.generated_ts, a.checked_ts"
        " FROM life_list l"
        " LEFT JOIN species_analysis a ON a.species_sci = l.species_sci"
        " ORDER BY l.species_common").fetchall()
    profile_ts = profile_fetch_times(conn)
    out = []
    for r in rows:
        visits = len(group_visits(
            [s["ts"] for s in conn.execute(
                "SELECT ts FROM sightings WHERE species_sci = ? ORDER BY ts",
                (r["species_sci"],))]))
        # Floor first (#407): sub-floor species never enter the arms, so a
        # prompt_version / model / profile-touch row with only one visit
        # still does not burn a generation.
        if visits < min_visits:
            continue
        mark = r["visits_watermark"]
        settled_ts = max(r["generated_ts"] or 0, r["checked_ts"] or 0)
        due = (mark is None
               or visits - mark >= due_step(visits, step)
               or r["prompt_version"] != PROMPT_VERSION
               or (model is not None and outranks(model, r["model"]))
               or profile_ts.get(r["species_sci"], 0) > settled_ts)
        if due:
            out.append((r["species_sci"], r["species_common"], visits))
    return out


def due_step(visits, step=WATERMARK_STEP, share=WATERMARK_SHARE):
    """New visits a written note must see before the watermark arm fires
    again (#612): never fewer than `step`, and `share` of the record once
    the record is big. 20 visits -> 5; 3,400 -> 340."""
    return max(step, math.ceil(share * visits))


def purge_stale(conn, min_visits=MIN_VISITS_FOR_ANALYSIS):
    """Delete the notes no gate can ever reach again (#612), returning the
    species they belonged to. A row at an old PROMPT_VERSION for a species
    still under the #407 floor is unreachable: the floor runs before the
    version arm, so it would sit on the page forever in a retired voice --
    twelve such rows on 2026-10-24, written by the dropped 27b model over a
    single visit ("on only zero days that matched conditions"). Deleting
    leaves the page's honest no-notes state; crossing the floor later writes
    a fresh note through the ordinary never-written arm. A row --refresh
    wrote at the CURRENT version is a human's choice and stays."""
    rows = conn.execute(
        "SELECT species_sci, prompt_version FROM species_analysis").fetchall()
    purged = []
    for r in rows:
        if r["prompt_version"] == PROMPT_VERSION:
            continue
        visits = len(group_visits(
            [s["ts"] for s in conn.execute(
                "SELECT ts FROM sightings WHERE species_sci = ?",
                (r["species_sci"],))]))
        if visits < min_visits:
            conn.execute("DELETE FROM species_analysis WHERE species_sci = ?",
                         (r["species_sci"],))
            purged.append(r["species_sci"])
    if purged:
        conn.commit()
    return purged


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


def write_block(ollama, prompt, plain, *, allow_drive, label, lede=None):
    """One block through the validator (#612): the draft, then at most one
    retry that quotes the violations back, then the plain record. Returns
    (text, fell_back), or (None, False) when the model stopped answering --
    which the caller treats exactly like before: llm-down, row untouched.

    The retry is validated against the ORIGINAL prompt, never the retry
    prompt: the retry quotes the invented figures back, and checking
    against it would let the model's own inventions count as given."""
    def ask(p):
        # The narrator's default ceiling is a live-show pacing number; this
        # is a patient batch job, so it passes its own.
        return ollama.complete(VOICE, p, num_predict=NUM_PREDICT,
                               temperature=0.7, timeout=ANALYSIS_TIMEOUT_S)

    draft = ask(prompt)
    if not draft:
        return None, False
    problems = notes_validate.validate(draft, prompt, allow_drive, lede)
    if not problems:
        return draft.strip(), False
    print(f"[analysis] {label}: draft broke {len(problems)} rule(s) -- "
          "retrying once", flush=True)
    second = ask(prompt + "\n\nYour previous draft broke these rules:\n"
                 + "\n".join(f"- {p}" for p in problems)
                 + "\nWrite the note again from the start, fixing every one.")
    if not second:
        return None, False
    again = notes_validate.validate(second, prompt, allow_drive, lede)
    if not again:
        return second.strip(), False
    print(f"[analysis] {label}: the retry broke the rules too ("
          + "; ".join(again) + ") -- storing the plain record", flush=True)
    return plain(), True


def analyze_species(conn, sci, common, *, ollama, weather_path,
                    now=None, dry_run=False, force=False, host=None,
                    places=None):
    """One species, end to end. Returns a status word:
      'no-visits'  -- nothing on record yet; nothing written
      'stats-only' -- --dry-run, or no LLM configured
      'current'    -- the fingerprint gate: stats, description, prompt
                      version and model rank all unchanged, so a generation
                      would only rephrase the same facts; no LLM call made
      'llm-down'   -- generation failed; any existing row LEFT UNTOUCHED
      'written'    -- both blocks generated (or, after two rule-breaking
                      drafts, the plain record -- `fallback` 1) and stored

    `force` (--refresh's flag) pushes past the fingerprint gate the same way
    --refresh already ignores the watermark. `host` is provenance -- which
    endpoint served the row, useful once two hosts are in play (#217).
    `places` is feeds.places() (#612), read from the registry when omitted.
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

    # The fingerprint gate (issue #217), stage two of the worklist's
    # two-stage filter and the last thing standing before an LLM call:
    # identical prompt inputs, same prompt version, and a model that doesn't
    # outrank the stored one means regeneration would only rephrase. The
    # skip is recorded (checked_ts) so a species the worklist over-included
    # settles instead of being re-examined every tick; generated_ts is NOT
    # touched -- the page's "as of" stamp must never claim freshness that
    # wasn't earned.
    places = load_places() if places is None else places
    fingerprint = stats_fingerprint(stats, description, places_key(places))
    if not force:
        row = conn.execute(
            "SELECT stats_fingerprint, prompt_version, model"
            " FROM species_analysis WHERE species_sci = ?", (sci,)).fetchone()
        if (row is not None
                and row["stats_fingerprint"] == fingerprint
                and row["prompt_version"] == PROMPT_VERSION
                and not outranks(ollama.model, row["model"])):
            conn.execute(
                "UPDATE species_analysis SET checked_ts = ?"
                " WHERE species_sci = ?", (int(now), sci))
            conn.commit()
            return "current", stats

    allow = allow_driveway(places)
    rhythm, r_plain = write_block(
        ollama, rhythm_prompt(common, description, stats, places),
        lambda: plain_rhythm(stats, places), allow_drive=allow,
        label=f"{common} rhythm", lede=rhythm_lede(stats, places))
    # A dead or slow model leaves the existing row exactly as it was -- the
    # narrator's degrade-never-crash ethos, and the species stays on the
    # worklist for the next run.
    if rhythm is None:
        return "llm-down", stats
    weather, w_plain = write_block(
        ollama, weather_prompt(common, stats, places),
        lambda: plain_weather(stats), allow_drive=allow,
        label=f"{common} weather", lede=weather_lede(stats))
    if weather is None:
        return "llm-down", stats

    conn.execute(
        "INSERT OR REPLACE INTO species_analysis (species_sci, rhythm_text,"
        " weather_text, stats_json, visits_watermark, model, generated_ts,"
        " stats_fingerprint, prompt_version, host, fallback)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sci, rhythm, weather, json.dumps(stats),
         len(visits), ollama.model, int(now),
         fingerprint, PROMPT_VERSION, host, int(r_plain or w_plain)))
    conn.commit()
    return "written", stats


def audit(conn, places=None):
    """Every stored note checked by the validator against the prompt it
    WOULD be written from today (stored stats + today's description and
    places) -- the acceptance check for #612, and the tool for spotting a
    rule the model keeps breaking. -> [(species_sci, block, [violations])]."""
    places = load_places() if places is None else places
    allow = allow_driveway(places)
    try:
        descriptions = {r["species_sci"]: r["description"] for r in
                        conn.execute("SELECT species_sci, description"
                                     " FROM species_profile")}
    except sqlite3.Error:
        descriptions = {}
    out = []
    for r in conn.execute(
            "SELECT a.species_sci, a.rhythm_text, a.weather_text, a.stats_json,"
            " COALESCE(l.species_common, a.species_sci) common"
            " FROM species_analysis a LEFT JOIN life_list l"
            " ON l.species_sci = a.species_sci ORDER BY common"):
        try:
            stats = json.loads(r["stats_json"] or "")
            prompts = (("rhythm", r["rhythm_text"], rhythm_prompt(
                            r["common"], descriptions.get(r["species_sci"]),
                            stats, places), rhythm_lede(stats, places)),
                       ("weather", r["weather_text"],
                        weather_prompt(r["common"], stats, places),
                        weather_lede(stats)))
        except (ValueError, KeyError, TypeError):
            out.append((r["species_sci"], "stats", ["stats_json unreadable"]))
            continue
        for block, text, prompt, lede in prompts:
            problems = notes_validate.validate(text, prompt, allow, lede)
            if problems:
                out.append((r["species_sci"], block, problems))
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Write the Aviary's field-naturalist blocks (issue #186)")
    ap.add_argument("--refresh", metavar="SPECIES_SCI",
                    help="re-analyze one species regardless of its watermark")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print the findings; no LLM, no writes")
    ap.add_argument("--audit", action="store_true",
                    help="check every stored note against the validator; "
                         "no LLM, no writes (#612)")
    args = ap.parse_args()

    path = db_path()
    conn = connect(path)
    if args.audit:
        try:
            found = audit(conn)
        finally:
            conn.close()
        for sci, block, problems in found:
            print(f"[audit] {sci} {block}: " + "; ".join(problems), flush=True)
        print(f"[audit] {len(found)} block(s) with violations", flush=True)
        raise SystemExit(1 if found else 0)
    ollama = None
    host = None
    if not args.dry_run:
        addr = ollama_address()
        if addr:
            model = os.environ.get("MERLE_OLLAMA_MODEL", "").strip() \
                or OLLAMA_DEFAULT_MODEL
            ollama = Ollama(*addr, model)
            host = f"{addr[0]}:{addr[1]}"
            print(f"[analysis] narrating with {model} via {host}", flush=True)
        else:
            print("[analysis] MERLE_OLLAMA not set -- stats only", flush=True)

    try:
        if not args.dry_run:
            for sci in purge_stale(conn):
                print(f"[analysis] {sci}: purged a note no gate can reach "
                      "(old prompt version, under the visit floor)",
                      flush=True)
        if args.refresh:
            row = conn.execute(
                "SELECT species_common FROM life_list WHERE species_sci = ?",
                (args.refresh,)).fetchone()
            todo = [(args.refresh,
                     row["species_common"] if row else args.refresh, 0)]
        else:
            # The model rides into the worklist so the outranked-model arm
            # can fire; None (dry-run / no LLM) simply disables that arm.
            todo = worklist(conn, model=ollama.model if ollama else None)
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
                    weather_path=weather_db_path(), dry_run=args.dry_run,
                    force=bool(args.refresh), host=host)
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
