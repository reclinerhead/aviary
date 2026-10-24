# =============================================================================
# project-squirrel -- listener/editor.py
#
# The editor (issue #614): given every finding for one bird (findings.py)
# and what the journal has already said about it, decide WHETHER an entry
# is due, and if so which finding leads and which few support it. A
# newsroom's job -- rank by how surprising and how well supported a fact
# is, never run the same lede twice -- done in Python so the model is only
# ever handed one story to tell.
#
# WHEN (due): an entry is written because something HAPPENED, not because
# a visit counter ticked --
#   first      the bird has never had an entry
#   anomaly    a suspect pattern never yet flagged (a "swallow" at night)
#   arrival    a season's first visit, not yet written up
#   departure  a season's silence, not yet written up
#   record     a new busiest day, or a streak crossing a 30-day mark
#   weekly     a week since the last entry and the bird was heard in it,
#              AND the pick says something the last entry didn't
#   refresh    a human asked (the species page's link, #613)
# A regular therefore gets about one entry a week; a migrant gets exactly
# the entries its season deserves. (No fixed weekday for "weekly": the
# loop's per-tick cap spreads the load across the week instead of piling
# sixty birds onto one Sunday evening on a workstation's GPU.)
#
# WHAT (pick): interest = kind weight x min(effect, 1.5) x support factor,
# thin findings and recently-said ones heavily discounted; the season's
# news and an anomaly carry a bonus that only applies while unsaid, so they
# lead once and then step aside.
# =============================================================================

# Per-kind weight on the base score. Sun-relative timing is the field
# naturalist's own frame (#614's review), so it edges out a raw share.
KIND_WEIGHT = {"dawn": 1.2, "dusk": 1.0, "night": 1.2, "place": 1.0,
               "record": 0.9, "trend": 1.0, "weather_sky": 1.0,
               "weather_temp": 1.0, "lingering": 1.0, "arrival": 1.0,
               "departure": 1.0, "anomaly": 1.0}
# Unsaid news leads: added on top of the base score.
NEWS_BONUS = {"anomaly": 30.0, "arrival": 20.0, "departure": 20.0,
              "lingering": 5.0}
# Support at which a finding counts as fully supported (its own unit).
FULL_SUPPORT = 30
THIN_FACTOR = 0.3
SAID_FACTOR = 0.2
WEEK_S = 7 * 86400
# How many recent entries count as "already said".
RECENT_ENTRIES = 3


def interest(f, said=frozenset()):
    """How strongly `f` deserves the lede, given the novelty keys recently
    said."""
    base = (KIND_WEIGHT.get(f["kind"], 1.0) * min(f["effect"], 1.5)
            * min(1.0, f["support"] / FULL_SUPPORT))
    if f["thin"]:
        base *= THIN_FACTOR
    if f["novelty_key"] in said:
        return base * SAID_FACTOR
    return base + NEWS_BONUS.get(f["kind"], 0.0)


# Under an anomaly lede, only facts that don't rest on the suspect
# detections may support it: when they are the record, the night-heavy
# "swallow's" weather and timing habits are the insect's, not a bird's.
ANOMALY_SUPPORT = frozenset(("departure", "arrival", "lingering", "record",
                             "place"))


def pick(findings, said=frozenset(), n=4):
    """-> (lede, [supporting...]) or (None, []). Never two of one kind."""
    ranked = sorted(findings, key=lambda f: (-interest(f, said), f["kind"]))
    if not ranked:
        return None, []
    lede, kinds, rest = ranked[0], {ranked[0]["kind"]}, []
    for f in ranked[1:]:
        if len(rest) >= n - 1:
            break
        if f["kind"] in kinds:
            continue
        if lede["kind"] == "anomaly" and f["kind"] not in ANOMALY_SUPPORT:
            continue
        kinds.add(f["kind"])
        rest.append(f)
    return lede, rest


def record_broken(current, previous):
    """A record worth an entry: a new busiest day, or the longest streak
    crossing a 30-day mark. (A growing streak breaks "longest" every single
    day; only the milestone is news.)"""
    if current is None or previous is None:
        return False
    c, p = current["fields"], previous["fields"]
    return (c["busiest"] > p["busiest"]
            or c["longest"] // 30 > p["longest"] // 30)


def by_kind(findings, kind):
    return next((f for f in findings if f["kind"] == kind), None)


def due(findings, entries, now, visits_last_week, said=frozenset()):
    """The trigger that fires, or None. `entries` are this bird's stored
    entries newest first, each {"entry_ts", "findings": [Finding...]}."""
    if not findings:
        return None
    if not entries:
        return "first"
    ever = {f["novelty_key"] for e in entries for f in e["findings"]}
    for kind in ("anomaly", "arrival", "departure"):
        f = by_kind(findings, kind)
        if f is not None and f["novelty_key"] not in ever:
            return kind
    previous = next((by_kind(e["findings"], "record") for e in entries
                     if by_kind(e["findings"], "record")), None)
    if record_broken(by_kind(findings, "record"), previous):
        return "record"
    if now - entries[0]["entry_ts"] >= WEEK_S and visits_last_week > 0:
        lede, _ = pick(findings, said)
        if lede is not None and lede["novelty_key"] not in said:
            return "weekly"
    return None


def said_recently(entries):
    """Novelty keys of the last few entries' findings."""
    return frozenset(f["novelty_key"] for e in entries[:RECENT_ENTRIES]
                     for f in e["findings"])


TITLES = {"departure": "gone quiet", "lingering": "staying late",
          "anomaly": "heard at an odd hour", "night": "a voice after dark",
          "dusk": "until dark"}


def title(lede, places=None):
    """The entry's heading, made in Python from its lede (never by the
    model): what the entry is about, in a few words."""
    k, fields = lede["kind"], lede["fields"]
    if k == "arrival":
        return "first heard" if fields.get("first_ever") else "back again"
    if k == "dawn":
        label = fields.get("label") or ""
        return ("up before the sun" if label.startswith("a dawn")
                else "up with the sun" if label == "up with the sun"
                else "a late riser" if label == "a late riser"
                else "the first song")
    if k == "place":
        p = (places or {}).get(fields.get("source"))
        return f"at {p.phrase}" if p is not None and p.phrase else \
            "one corner of the yard"
    if k == "trend":
        return "a busy week" if fields["recent"] > fields["prior"] \
            else "a quiet week"
    if k == "record":
        if fields.get("current", 0) >= 14:
            return f"every day for {fields['current']} days"
        days = fields.get("days", 0)
        return f"heard on {days} day{'' if days == 1 else 's'}"
    if k == "weather_sky":
        return {"rain": "a rain bird", "clear": "under clear skies",
                "cloudy": "under cloudy skies", "snow": "in the snow"}.get(
                    fields["bucket"], "the weather")
    if k == "weather_temp":
        return f"in {fields['bucket']} weather"
    return TITLES.get(k, "the latest")
