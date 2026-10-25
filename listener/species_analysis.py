# =============================================================================
# project-squirrel -- listener/species_analysis.py
#
# The field journal (epic #182 Phase 4 / #186, rebuilt as epic #611 Phase A,
# issue #614, one note per bird since #626): what the yard's own record says
# about a species, computed in Python and written up by the LLM --
#
#   THE STATS (species_analysis.stats_json): the margin figures' numbers --
#     the 24-hour histogram, the peak window, the exposure-normalised
#     weather effects -- stored through the #217 fingerprint gate. No prose:
#     the standing sketch this row once carried retired in #626 (it said
#     what the entry beside it said), and an old rhythm_text is carried
#     forward untouched, never erased, as the page's fallback until the
#     bird's first entry lands.
#   DATED ENTRIES (species_journal): written when something HAPPENED -- an
#     arrival, a departure, a record, a suspect pattern, a week's news --
#     each led by the finding listener/editor.py ranks first and carrying
#     the bird's character beside it (when in the day, the season, the
#     weather, the place, how steadily), from the facts listener/findings.py
#     computes. The newest entry IS the bird's field note; a journal is
#     never re-narrated.
#
#   python -m listener.species_analysis              (stats + due entries)
#   python -m listener.species_analysis --refresh "Cardinalis cardinalis"
#   python -m listener.species_analysis --refresh "..." --writer gateway --compare
#                                                    (a second opinion from a
#                                                     Claude model behind the
#                                                     Vercel AI Gateway, #632:
#                                                     ON DEMAND ONLY -- the
#                                                     loop never builds it)
#   python -m listener.species_analysis --dry-run    (findings + picks, no LLM)
#   python -m listener.species_analysis --audit      (validator over the store)
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
# HYGIENE (issue #612), the 2026-10-24 review's fixes, which every entry
# still passes through:
#   - NOTHING IS STORED UNCHECKED. listener/notes_validate.py checks the
#     prose (figures must appear in the prompt; no annotation labels, no
#     `unknown`, no "our microphone", no "driveway" unless a place phrase
#     says so, no stock opener, an entry's first sentence carries its lede).
#     One retry with the violations quoted; a second failure stores the
#     PLAIN RECORD (the fact lines as sentences, no model) with
#     `fallback = 1`, so the page can say so.
#   - THE YARD'S OWN VOCABULARY. Microphones are named by their `place:`
#     phrase from the feeds registry (feeds.places()); on a station the
#     phrases come from that station's own registry.
#   - FROZEN ROWS PURGED (purge_stale) and PACING (due_step), as before.
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
#   AI_GATEWAY_API_KEY  the cloud writer's key (narration/gateway.py), read
#   MERLE_GATEWAY_MODEL   ONLY under --refresh --writer gateway; the loop's
#                       ticks construct Ollama and nothing else, whatever the
#                       unit's environment holds (#632: no surprise bills)
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
from listener import editor, findings, gate, notes_validate, region_prior
# The narrator owns the Ollama plumbing (endpoint config, the blocking
# non-streaming client, the model default); this borrows it rather than
# growing a second copy that could drift -- the weather post's precedent.
# The cloud writer (#632) has the same shape and lives beside it.
from narration import gateway
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
# Up to 280 words in four short paragraphs (#636); the cap is a backstop
# for a model ignoring the rules (Ollama only -- the cloud writer's
# ceiling is its own, narration/gateway.py).
NUM_PREDICT = 560
# Analysis is a long, patient job (one call per species, ~30-90s on CPU), not
# a live show like the narrator's 30s pacing gate.
ANALYSIS_TIMEOUT_S = 300

# Bumped BY HAND whenever the stats shape changes (issue #217). A stored row
# whose prompt_version differs is re-stored on the next tick -- which is how
# a shape change refreshes the whole archive with no migration and no manual
# pass. 5 is the stats-only row (#626): the bump re-stores every row once,
# cheaply, and spends no generation.
PROMPT_VERSION = 5
# The dated entries' prompt. Entries are a journal -- history is never
# re-narrated -- so this version only tells --audit which prompt an entry
# was written from; a bump re-writes nothing. 2 is the one-note prompt
# (#626): a note that stands alone, up to 220 words; 3 (#636) adds the
# yard line, the no-stock-phrases rule, American spelling and a 150-280
# word budget.
JOURNAL_VERSION = 3

# Model preference, best first (issue #217): what the enrichment loop picks
# from the models a host holds (enrichment_loop.pick_model). Until #626 it
# also gated the sketch's regeneration -- a model re-narrated a row only when
# it OUTRANKED the stored one, so two hosts with different models settled
# instead of thrashing; the stats row has no prose to improve, and entries
# are never re-narrated, so outranks() now serves the host choice alone.
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
-- The field journal (#614): append-only dated entries, one row each,
-- written when the editor says something happened. findings_json is the
-- lede first, then the supporting facts -- exactly what the prompt held,
-- so every sentence stays auditable after the fact.
CREATE TABLE IF NOT EXISTS species_journal (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    species_sci    TEXT NOT NULL,
    entry_ts       INTEGER NOT NULL,
    trigger        TEXT NOT NULL,
    lede_kind      TEXT NOT NULL,
    title          TEXT NOT NULL,
    text           TEXT NOT NULL,
    findings_json  TEXT NOT NULL,
    model          TEXT,
    host           TEXT,
    prompt_version INTEGER,
    fallback       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS species_journal_by_species
    ON species_journal (species_sci, entry_ts);
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
    """The identity of one species' INPUTS (issue #217): the full stats
    package plus the exact description snippet the entry prompt feeds the
    model. Identical fingerprint + matching prompt version means nothing
    about the bird's figures or background has changed -- so the row is
    left alone. The description rides the hash because it is a real input
    the stats can't see: the profile pass landing a Wikipedia background
    later must read as changed inputs.
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


def prompt_context(places, yard=None):
    """What rides the stats fingerprint beside the figures and the
    background: the place phrases (#612) and the yard blurb (#636) -- both
    are prompt inputs the figures can't see, so editing either re-stores
    the row."""
    return places_key(places) + "\x00" + (yard or "")


def places_key(places):
    """A stable string of the place phrases, for the fingerprint."""
    return "|".join(f"{k}={p.phrase}:{int(p.current)}"
                    for k, p in sorted((places or {}).items()))


def allow_driveway(places):
    """True only when one of THIS registry's phrases says "driveway" -- the
    word is a fact about the house's rear camera, never a name for a yard."""
    return any(p.phrase and "driveway" in p.phrase.lower()
               for p in (places or {}).values())


# Leading words a place phrase's parts carry that are not the place itself.
_PLACE_LEADS = ("over the ", "by the ", "under the ", "at the ", "facing the ",
                "in the ", "parked in the ", "the ")
# Last words too generic to stand for one microphone's place on their own.
_GENERIC_NOUNS = frozenset(("house", "yard", "home"))


def place_terms(places):
    """The words a note could use to put a bird somewhere in THIS yard,
    from the registry's phrases (#614): each comma-part with its leading
    preposition dropped ("back of the house", "driveway", "front door"),
    plus its last noun where that names a spot ("garage", "eave",
    "window"). The validator lets the prose name one only when a fact
    does -- the desk run's place inventions had nowhere else to come from
    than these phrases."""
    out = set()
    for p in (places or {}).values():
        for part in (p.phrase or "").split(","):
            part = part.strip().lower()
            for lead in _PLACE_LEADS:
                if part.startswith(lead):
                    part = part[len(lead):]
                    break
            if len(part) < 4:
                continue
            out.add(part)
            last = part.split()[-1]
            if last not in _GENERIC_NOUNS and len(last) >= 4:
                out.add(last)
    return sorted(out)


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


# --- The prompts --------------------------------------------------------------

VOICE = (
    "You are a field naturalist writing a short note for a backyard "
    "birdwatching page. Your reader listens to their own yard through a few "
    "microphones and loves birds; they are not a scientist. Be warm, "
    "specific and a little wry. Plain prose in American English, no lists, "
    "no headings, no emoji."
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
    "- A parenthesis marked 'evidence:' tells you how solid a finding is "
    "-- how many mornings, how many hours of weather. Read it, weigh it, "
    "and never write its figures.\n"
    "- Figures are not a quota. Most sentences carry none; keep a figure "
    "only where it is the point -- a date, a count that is itself the "
    "story, the size of an effect -- and never more than two in a "
    "sentence. A figure you leave out is said in plain words (far less "
    "often, nearly every day), never replaced by another number.\n"
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
    "or \"my\". Say where in the yard the bird was heard ONLY when a fact "
    "given says where, in that fact's own words; never invent a place.\n"
    "- Open with the first fact given, in your own words. Do not open by "
    "saying a microphone heard or picked up the bird, and never write \"a "
    "good number of\".\n"
    "- A line marked \"The yard:\" says what your reader's yard is like. You "
    "may tie a habit to what the yard offers, as your one guess; you may "
    "NEVER say where in the yard a bird was heard unless a fact says so -- "
    "the facts say where the microphones heard it, the yard line does not.\n"
    "- Write each note as if it were the only one you will ever write: no "
    "phrase you would reach for on every bird (\"hold that one loosely\", "
    "\"a neighbour rather than a guest\"). Say the hedge, and the bird's "
    "standing, in words this bird earns.\n"
    "- Four short paragraphs at most. No preamble, no sign-off, no "
    "restating the bird's name as a heading."
)


def prompt_line(f):
    """A finding as the model reads it: its finished sentence, plus the
    hedge marker on a thin one (RULES says what the marker means)."""
    return f["fact_line"] + (" Evidence: THIN -- hedge this one."
                             if f["thin"] else "")


def plain_line(f):
    """A finding as the plain record states it: the sentence with the
    uppercase direction keywords put back into ordinary English and the
    evidence parenthesis dropped (#630) -- the reader gets the claim, as
    the model would have written it."""
    return (notes_validate.strip_evidence(f["fact_line"])
            .replace("MORE OFTEN", "more often")
            .replace("LESS OFTEN", "less often"))


def fact_lines(found):
    """The facts exactly as a prompt lists them -- also what the validator
    checks a named place against."""
    return "\n".join(f"- {prompt_line(f)}" for f in found)


# The prompt carries NO list of the yard's listening posts (#614): handed
# one, the model put birds at microphones that never heard them. Where a
# bird is heard is a fact (findings.place) like any other, and the
# validator rejects a place no fact names.

def entry_prompt(common, description, lede, rest, now, first=False,
                 yard=None):
    """The one note (#626): the newest entry is all the page says about the
    bird, so it must stand on its own -- the lede first, then the bird's
    character (the editor hands over the best fact of each theme) in two
    to four short paragraphs. Until #626 an entry assumed its reader had
    the standing sketch beside it and the earlier entries behind it. `yard`
    (#636) is the registry's blurb of what the yard is, the one thing that
    lets a note belong to this yard and not a generic one."""
    caution = (
        "\nThis entry is a caution, not a portrait: say plainly that these "
        "detections may not be this bird at all, and invite your reader to "
        "listen to a recording before believing them.\n"
        if lede["kind"] == "anomaly" else "")
    return (
        f"The bird: {common}.\n"
        + (f"Background: {description.strip()[:900]}\n" if description else "")
        + (f"The yard: {yard.strip()}\n" if yard else "")
        + f"\nToday is {findings.date_text(now)}. You are writing the "
          "field notes for this bird in your reader's yard: one dated entry "
          "in a running journal, read on its own -- it is all the page "
          "says about the bird"
        + (", and it is the first entry." if first else
           ". Earlier entries are kept behind it, but assume they were not "
           "read.")
        + "\n\nOpen the note with this fact:\n" + prompt_line(lede)
        + ("\n\nThe other facts, which the note should cover where they "
           "help -- when in the day the bird is heard, where it stands in "
           "the season, what weather suits it, where in the yard, how "
           "steadily:\n" + fact_lines(rest) if rest else "")
        + "\n" + caution
        + "\nWrite one note of 150 to 280 words in two to four short "
          "paragraphs.\n\n" + RULES
    )


def plain_entry(lede, rest):
    return " ".join(plain_line(f) for f in [lede, *rest])


# --- Store --------------------------------------------------------------------

def profile_fetch_times(conn):
    """species_sci -> the profile pass's fetched_ts, or {} on a store where
    that pass has never run (no table is a normal state, not an error)."""
    try:
        return {r["species_sci"]: r["fetched_ts"] for r in conn.execute(
            "SELECT species_sci, fetched_ts FROM species_profile")}
    except sqlite3.Error:
        return {}


def worklist(conn, step=WATERMARK_STEP, min_visits=MIN_VISITS_FOR_ANALYSIS):
    """Species whose stats row needs a look. The watermark is a visit COUNT
    rather than an age -- a bird nobody has heard since June has nothing new
    to say about itself.

    This is the CHEAP pre-filter of #217's two-stage gate, so its arms are
    deliberately over-inclusive -- anything it lets through still has to get
    past the stats fingerprint in analyze_species() before a row is
    re-stored. The arms (only considered once `visits >= min_visits`, #407):
      (0) visit floor -- fewer than MIN_VISITS_FOR_ANALYSIS grouped visits
          means not a candidate at all (one-off false positives must not
          spin the pass; --refresh / force still analyzes by hand)
      (1) never written
      (2) grown by due_step(visits, step) since the last write (the
          original arm; the step grows with the record since #612)
      (3) stored prompt_version differs from PROMPT_VERSION (includes the
          honest-NULL rows a pre-#217 store carries -- they re-store once)
      (4) the profile pass touched this species after the row was last
          written OR gate-checked -- a description may have landed; the
          fingerprint decides. checked_ts is what keeps a dimensions-only
          profile touch from parking the species here forever.
    The model-rank arm (#217) left with the sketch (#626): a stats row has
    no prose a better model could improve, and outranks() against the NULL
    model the row now carries would re-queue every bird every tick.
    """
    rows = conn.execute(
        "SELECT l.species_sci, l.species_common, a.visits_watermark,"
        "       a.prompt_version, a.generated_ts, a.checked_ts"
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


def write_block(ollama, prompt, plain, *, allow_drive, label, lede=None,
                terms=None, facts=None):
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
    problems = notes_validate.validate(draft, prompt, allow_drive, lede,
                                       terms, facts)
    if not problems:
        return draft.strip(), False
    print(f"[analysis] {label}: draft broke {len(problems)} rule(s) -- "
          "retrying once", flush=True)
    second = ask(prompt + "\n\nYour previous draft broke these rules:\n"
                 + "\n".join(f"- {p}" for p in problems)
                 + "\nWrite the note again from the start, fixing every one.")
    if not second:
        return None, False
    again = notes_validate.validate(second, prompt, allow_drive, lede,
                                    terms, facts)
    if not again:
        return second.strip(), False
    print(f"[analysis] {label}: the retry broke the rules too ("
          + "; ".join(again) + ") -- storing the plain record", flush=True)
    return plain(), True


# --- The journal's inputs (#614) ---------------------------------------------

def journal_context(conn, weather_path, now, places=None, yard=None):
    """What every species' findings share, gathered ONCE per run: the
    registry's places and its yard blurb (#636), Home (for the sun), the
    microphones fair to compare week on week, the yard's detections per
    microphone (for place), and the weather archive. A tick that looks at
    a hundred birds reads the archive once, not a hundred times. `yard`
    None reads the registry; a registry without the key is a None blurb,
    and the prompt simply carries no yard line."""
    spans, counts = {}, {}
    for r in conn.execute("SELECT source, MIN(ts) a, MAX(ts) b, COUNT(*) n"
                          " FROM sightings GROUP BY source"):
        spans[r["source"]] = (r["a"], r["b"])
        counts[r["source"]] = r["n"]
    observations = load_observations(weather_path, 0, now)
    return {"now": now,
            "places": load_places() if places is None else places,
            "yard": feeds.yard() if yard is None else yard,
            "latlon": region_prior.home(conn),
            "fair": findings.fair_sources(spans, now),
            "counts": counts,
            "observations": observations,
            "obs_ts": [o["ts"] for o in observations]}


def species_inputs(conn, sci):
    """(visit openings, [(opening, source)], detections per source,
    first_ts) for one species -- the visit grouping the whole Aviary uses
    (gate.VISIT_GAP_S), each visit credited to the microphone that opened it."""
    visits, rows, sources, last = [], [], {}, None
    for r in conn.execute("SELECT ts, source FROM sightings"
                          " WHERE species_sci = ? ORDER BY ts", (sci,)):
        if last is None or r["ts"] - last > gate.VISIT_GAP_S:
            visits.append(r["ts"])
            rows.append((r["ts"], r["source"]))
        last = r["ts"]
        sources[r["source"]] = sources.get(r["source"], 0) + 1
    first = conn.execute("SELECT MIN(first_ts) f FROM life_list"
                         " WHERE species_sci = ?", (sci,)).fetchone()
    return visits, rows, sources, (first["f"] if first else None)


def species_findings(conn, sci, ctx):
    """(stats, findings, visits) for one species; stats is the margin
    figures' package (build_stats, unchanged), findings the journal's."""
    visits, rows, sources, first_ts = species_inputs(conn, sci)
    if not visits:
        return None, [], []
    lo = bisect.bisect_left(ctx["obs_ts"], visits[0] - WEATHER_MATCH_S)
    hi = bisect.bisect_right(ctx["obs_ts"], visits[-1] + WEATHER_MATCH_S)
    obs = ctx["observations"][lo:hi]
    stats = build_stats(visits, obs, ctx["now"], first_ts=first_ts,
                        sources=sources)
    found = findings.compute(
        sci=sci, visits=visits, visit_rows=rows, sources=sources,
        stats=stats, observations=obs, now=ctx["now"],
        latlon=ctx["latlon"], curve=region_prior.prior_curve(conn, sci),
        places=ctx["places"], fair=ctx["fair"], yard=ctx["counts"])
    return stats, found, visits


def description_of(conn, sci):
    """The profile's Wikipedia lead, or None on a store that has no
    profiles (the background paragraph is optional context)."""
    try:
        row = conn.execute("SELECT description FROM species_profile"
                           " WHERE species_sci = ?", (sci,)).fetchone()
        return row["description"] if row else None
    except sqlite3.Error:
        return None


# --- The stats (the margin figures) ------------------------------------------

def analyze_species(conn, sci, common, *, ollama, weather_path,
                    now=None, dry_run=False, force=False, host=None,
                    places=None, ctx=None):
    """One species' STATS ROW, end to end (#626): the margin figures'
    numbers (build_stats), stored through the #217 fingerprint gate, and
    NO prose -- the model is never called here. The standing sketch this
    row carried until #626 is gone; an old `rhythm_text` / `weather_text`
    is carried forward untouched, so a bird whose first entry has not
    landed still shows something, and a page never loses a note to a
    deploy. Returns a status word:
      'no-visits'  -- nothing on record yet; nothing written
      'stats-only' -- --dry-run, or no LLM configured: stats land only
                      where notes do (a station with no model keeps its
                      honest empty state until #620)
      'current'    -- the fingerprint gate: inputs and version unchanged,
                      so the row is left alone (checked_ts bookkeeping)
      'written'    -- the stats stored (and the row's prose, if any, kept)

    `force` (--refresh) pushes past the fingerprint, `host` is provenance,
    `ctx` (journal_context) is shared across a run and built here when
    omitted. `common` is unused since #626 but kept so every caller reads
    the same as write_entry's."""
    now = now or time.time()
    ctx = ctx or journal_context(conn, weather_path, now, places)
    places = ctx["places"]
    stats, _found, visits = species_findings(conn, sci, ctx)
    if not visits:
        return "no-visits", None
    if dry_run or ollama is None:
        return "stats-only", stats

    # The description and the places still ride the fingerprint: they are
    # inputs to the entries written from this row's findings, and a change
    # in either is a reason to re-store (and re-date) the figures.
    fingerprint = stats_fingerprint(stats, description_of(conn, sci),
                                    prompt_context(places, ctx.get("yard")))
    row = conn.execute(
        "SELECT stats_fingerprint, prompt_version, rhythm_text, weather_text,"
        " model, fallback FROM species_analysis WHERE species_sci = ?",
        (sci,)).fetchone()
    if (not force and row is not None
            and row["stats_fingerprint"] == fingerprint
            and row["prompt_version"] == PROMPT_VERSION):
        conn.execute("UPDATE species_analysis SET checked_ts = ?"
                     " WHERE species_sci = ?", (int(now), sci))
        conn.commit()
        return "current", stats

    kept = (row["rhythm_text"], row["weather_text"], row["model"],
            row["fallback"]) if row is not None else (None, None, None, 0)
    conn.execute(
        "INSERT OR REPLACE INTO species_analysis (species_sci, rhythm_text,"
        " weather_text, stats_json, visits_watermark, model, generated_ts,"
        " stats_fingerprint, prompt_version, host, fallback)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sci, kept[0], kept[1], json.dumps(stats), len(visits), kept[2],
         int(now), fingerprint, PROMPT_VERSION, host, kept[3] or 0))
    conn.commit()
    return "written", stats


# --- The dated entries (the note) --------------------------------------------

def load_entries(conn, sci):
    """This bird's entries, newest first, findings parsed -- the editor's
    memory of what has been said."""
    out = []
    for r in conn.execute(
            "SELECT entry_ts, trigger, findings_json FROM species_journal"
            " WHERE species_sci = ? ORDER BY entry_ts DESC, id DESC", (sci,)):
        try:
            found = json.loads(r["findings_json"])
        except (TypeError, ValueError):
            found = []
        out.append({"entry_ts": r["entry_ts"], "trigger": r["trigger"],
                    "findings": found})
    return out


def entry_due(conn, sci, ctx, force=False):
    """(trigger, findings, entries, said) for one species -- the editor's
    decision with everything write_entry needs to act on it. trigger None
    means no entry is due."""
    now = ctx["now"]
    stats, found, visits = species_findings(conn, sci, ctx)
    if not visits:
        return None, [], [], frozenset()
    entries = load_entries(conn, sci)
    said = editor.said_recently(entries)
    if force:
        return ("refresh" if found else None), found, entries, said
    week = sum(1 for ts in visits if ts > now - editor.WEEK_S)
    return editor.due(found, entries, now, week, said), found, entries, said


def write_entry(conn, sci, common, *, ollama, ctx, force=False, host=None,
                due=None, compare=False):
    """Write this bird's next dated entry if one is due (or `force`, the
    species page's refresh). Returns (status, trigger): status 'not-due',
    'stats-only' (no LLM), 'llm-down' (nothing written) or 'written'.
    `due` is entry_due()'s answer when the caller already has it.

    `compare` (#632, the cloud writer's default): the entry is written from
    the NEWEST entry's stored findings -- lede and supporting facts verbatim,
    trigger `compare` -- so two writers' texts differ only by writer. Two
    ordinary refreshes are not the same prompt: the editor discounts what
    the last entries said, and the lede moves. With no entry yet to compare
    against it picks fresh, as a refresh would."""
    trigger, found, entries, said = \
        due or entry_due(conn, sci, ctx, force or compare)
    if trigger is None:
        return "not-due", None
    if ollama is None:
        return "stats-only", trigger
    if compare and entries and entries[0]["findings"]:
        lede, *rest = entries[0]["findings"]
        trigger = "compare"
    else:
        lede, rest = editor.pick(found, said)
    places, now = ctx["places"], ctx["now"]
    text, fell_back = write_block(
        ollama,
        entry_prompt(common, description_of(conn, sci), lede, rest, now,
                     first=not entries, yard=ctx.get("yard")),
        lambda: plain_entry(lede, rest), allow_drive=allow_driveway(places),
        label=f"{common} entry ({trigger})", lede=lede["fact_line"],
        terms=place_terms(places), facts=fact_lines([lede, *rest]))
    if text is None:
        return "llm-down", trigger
    conn.execute(
        "INSERT INTO species_journal (species_sci, entry_ts, trigger,"
        " lede_kind, title, text, findings_json, model, host,"
        " prompt_version, fallback) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (sci, int(now), trigger, lede["kind"], editor.title(lede, places),
         text, json.dumps([lede, *rest]), ollama.model, host,
         JOURNAL_VERSION, int(fell_back)))
    conn.commit()
    return "written", trigger


# The queue (enrichment_loop.drain_analysis): the stats rows first (free --
# no model, so they never count against the tick's cap, and the page's
# figures are there when its first entry lands), then news entries, then
# every bird's FIRST entry, then weekly and refresh entries.
NEWS_TRIGGERS = ("anomaly", "arrival", "departure", "record")
AHEAD_OF_WEEKLY = NEWS_TRIGGERS + ("first",)


def journal_worklist(conn, ctx, min_visits=MIN_VISITS_FOR_ANALYSIS):
    """[(sci, common, due)] for every species above the #407 floor whose
    entry is due: news triggers first, then first entries, then the rest.
    The floor holds here too: a one-off false positive gets no journal."""
    out = []
    for r in conn.execute("SELECT DISTINCT species_sci, species_common"
                          " FROM life_list ORDER BY species_common"):
        visits, _, _, _ = species_inputs(conn, r["species_sci"])
        if len(visits) < min_visits:
            continue
        due = entry_due(conn, r["species_sci"], ctx)
        if due[0] is not None:
            out.append((r["species_sci"], r["species_common"], due))
    return sorted(out, key=lambda x: (x[2][0] not in NEWS_TRIGGERS,
                                      x[2][0] != "first"))


# --- The cloud writer (#632) --------------------------------------------------

def cloud_writer(environ=None):
    """(client, host) for the on-demand Claude writer, or (None, None) with
    the reason printed. Called from ONE place -- main(), under --refresh
    --writer gateway -- and never from the loop: a tick must not be able to
    spend money, whatever environment the unit carries."""
    cfg = gateway.gateway_config(environ)
    if cfg is None:
        print("[analysis] AI_GATEWAY_API_KEY not set -- no cloud writer, "
              "nothing written", flush=True)
        return None, None
    key, model = cfg
    print(f"[analysis] writing on demand with {model} via "
          f"{gateway.GATEWAY_HOST}", flush=True)
    return gateway.Gateway(key, model), gateway.GATEWAY_HOST


# --- Audit --------------------------------------------------------------------

def audit(conn, places=None, yard=None):
    """Every stored entry at the current JOURNAL_VERSION checked by the
    validator against the prompt it was written from -- rebuilt from what
    was STORED (findings_json) plus today's background, places and yard.
    Older entries are skipped: they were written to an older prompt, and a
    journal is never re-narrated. A stats row carries no prose of its own
    since #626 (an old sketch carried forward is a fallback, not a claim
    this version made), so nothing there is audited.
    -> [(species_sci, block, [...])]."""
    places = load_places() if places is None else places
    yard = feeds.yard() if yard is None else yard
    allow = allow_driveway(places)
    terms = place_terms(places)
    out = []
    try:
        rows = conn.execute(
            "SELECT j.id, j.species_sci, j.entry_ts, j.text, j.findings_json,"
            " COALESCE(l.species_common, j.species_sci) common"
            " FROM species_journal j LEFT JOIN life_list l"
            " ON l.species_sci = j.species_sci"
            " WHERE j.prompt_version = ? GROUP BY j.id ORDER BY j.id",
            (JOURNAL_VERSION,)).fetchall()
    except sqlite3.Error:
        rows = []
    for r in rows:
        try:
            lede, *rest = json.loads(r["findings_json"])
        except (ValueError, TypeError):
            out.append((r["species_sci"], f"entry {r['id']}",
                        ["findings_json unreadable"]))
            continue
        prompt = entry_prompt(r["common"],
                              description_of(conn, r["species_sci"]),
                              lede, rest, r["entry_ts"], yard=yard)
        problems = notes_validate.validate(r["text"], prompt, allow,
                                           lede["fact_line"], terms,
                                           fact_lines([lede, *rest]))
        if problems:
            out.append((r["species_sci"], f"entry {r['id']}", problems))
    return out


# --- The CLI ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Write the Aviary's field journal: the margin figures' "
                    "stats and dated entries (issues #186, #614, #626)")
    ap.add_argument("--refresh", metavar="SPECIES_SCI",
                    help="store one species' stats and write it a new entry "
                         "now, past every gate (the species page's link, "
                         "#613)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print each species' findings and the editor's "
                         "pick; no LLM, no writes")
    ap.add_argument("--audit", action="store_true",
                    help="check every stored entry against the validator; "
                         "no LLM, no writes (#612)")
    ap.add_argument("--writer", choices=("ollama", "gateway"),
                    default="ollama",
                    help="who writes a --refresh entry: ollama (MERLE_OLLAMA, "
                         "the default) or gateway -- a Claude model behind "
                         "the Vercel AI Gateway (AI_GATEWAY_API_KEY), on "
                         "demand only (#632)")
    ap.add_argument("--compare", action="store_true",
                    help="with --refresh: write the entry from the newest "
                         "entry's own facts, so the texts differ only by "
                         "writer (#632)")
    args = ap.parse_args()
    if args.writer == "gateway" and not args.refresh:
        ap.error("--writer gateway is on demand only: use it with --refresh")
    if args.compare and not args.refresh:
        ap.error("--compare needs --refresh")

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

    ollama, host = None, None
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

    now = time.time()
    try:
        ctx = journal_context(conn, weather_db_path(), now)
        if args.dry_run:
            for sci, common, (trigger, found, entries, said) in [
                    (r["species_sci"], r["species_common"],
                     entry_due(conn, r["species_sci"], ctx))
                    for r in conn.execute(
                        "SELECT DISTINCT species_sci, species_common"
                        " FROM life_list ORDER BY species_common")]:
                lede, rest = editor.pick(found, said)
                if lede is None:
                    continue
                print(f"[analysis] {common}: due={trigger} "
                      f"title={editor.title(lede, ctx['places'])!r}",
                      flush=True)
                for f in [lede, *rest]:
                    print(f"    {f['kind']}: {f['fact_line']}", flush=True)
            return
        for sci in purge_stale(conn):
            print(f"[analysis] {sci}: purged a note no gate can reach "
                  "(old prompt version, under the visit floor)", flush=True)
        if args.refresh:
            row = conn.execute(
                "SELECT species_common FROM life_list WHERE species_sci = ?",
                (args.refresh,)).fetchone()
            common = row["species_common"] if row else args.refresh
            # The writer: Ollama as configured, or -- the one place in the
            # codebase that builds it -- the cloud writer (#632). The stats
            # row is written by whichever is present (no model is called
            # for it), with that writer's host as provenance.
            writer, writer_host = ollama, host
            if args.writer == "gateway":
                writer, writer_host = cloud_writer()
                if writer is None:
                    return
            status, _ = analyze_species(conn, args.refresh, common,
                                        ollama=ollama or writer,
                                        weather_path=None, now=now,
                                        force=True, host=host or writer_host,
                                        ctx=ctx)
            print(f"[analysis] {common} stats: {status}", flush=True)
            status, trigger = write_entry(conn, args.refresh, common,
                                          ollama=writer, ctx=ctx, force=True,
                                          host=writer_host,
                                          compare=args.compare)
            print(f"[analysis] {common} entry ({trigger}): {status}",
                  flush=True)
            return
        counts = {}
        for sci, common, _ in worklist(conn):
            status, _ = analyze_species(conn, sci, common, ollama=ollama,
                                        weather_path=None, now=now,
                                        host=host, ctx=ctx)
            counts[f"stats {status}"] = counts.get(f"stats {status}", 0) + 1
            if status == "written":
                print(f"[analysis] {common} stats: written", flush=True)
        for sci, common, due in journal_worklist(conn, ctx):
            status, trigger = write_entry(conn, sci, common, ollama=ollama,
                                          ctx=ctx, host=host, due=due)
            counts[f"entry {status}"] = counts.get(f"entry {status}", 0) + 1
            print(f"[analysis] {common} entry ({trigger}): {status}",
                  flush=True)
        print("[analysis] done: " + (", ".join(
            f"{v} {k}" for k, v in sorted(counts.items())) or "nothing due"),
            flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
