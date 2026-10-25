# =============================================================================
# project-squirrel -- listener/fun_facts.py
#
# The Aviary's fun-fact pool (issue #422): 3-5 short, true facts per bird,
# drawn from its Wikipedia article by a Claude model behind the AI Gateway,
# each carrying the article sentence it rests on. The pool is a JSON file in
# the repo (mcc/data/fun-facts.json) that the dashboard picks a fact of the
# day from; it reaches the stations with the nightly promote like any code.
#
# A FACT BELONGS TO THE SPECIES, NOT THE YARD. The cardinal's beak-to-beak
# courtship is as true at Karen's as here, so the facts are written ONCE, on
# orchid (the only box with a gateway key), and shipped -- not generated per
# box. Stations have no model (#620) and need none for this.
#
# A WRONG FACT IS WORSE THAN NONE, because the family repeats it. The
# near-miss that shaped this file: "Chimney Swifts can only perch on
# horizontal surfaces" is the reverse of the truth. So:
#   - the model sees ONLY the article and must quote, verbatim, the
#     sentence each fact rests on;
#   - check_fact() drops any fact whose quote isn't in the article, any
#     figure the quote doesn't hold, and any absolute ("only", "never",
#     "largest") the quote doesn't say -- the swift's failure, mechanised;
#   - and a person reads every survivor beside its quote in the PR before
#     it ships. The validator narrows what the review has to catch; it does
#     not replace it.
#
# HAND-RUN, NEVER A TICK. Nothing in the enrichment loop imports this file.
# The output is a PR, so the run is a person's: `python -m listener.fun_facts
# --out ~/fun-facts.json` on orchid, then scp the file home and open a PR.
# `--out` is REQUIRED (bar --dry-run) and never the deploy checkout's copy:
# an uncommitted edit in orchid's checkout aborts autodeploy's pull
# silently. The run starts from --out when it exists (a resumed run) and
# from the shipped pool otherwise.
#
# IDEMPOTENT BY ENTRY. A plain run only writes species with no entry. A
# species whose article was missing, or whose facts all failed the check, is
# written with `facts: []` so the next plain run doesn't pay for it again --
# a definitive outcome is recorded; a transient one (Wikipedia or the
# gateway unreachable) is not, and retries next run. Facts deleted by hand in
# review stay deleted: the entry still exists. `--refresh SCI` rewrites one.
#
# WIKIPEDIA ONLY. ADW prose (#318, CC BY-NC-SA) never enters the pool: its
# licence can't sit beside CC BY-SA text, and the pool lives in the repo.
#
# Config (env, the units' names):
#   AI_GATEWAY_API_KEY, MERLE_GATEWAY_MODEL   the writer (narration/gateway.py)
#   MERLE_EARL_DB                             the house life list
#   MERLE_STATIONS                            name=url pairs (#342); each
#                                             station's roster is <url>/roster
# =============================================================================

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import time
import unicodedata
import urllib.request

from listener import species_profile
from listener.notes_validate import figures
from narration.gateway import Gateway, gateway_config

POOL_VERSION = 1
# The shipped pool, relative to the repo root (the units' WorkingDirectory).
SHIPPED_POOL = os.path.join("mcc", "data", "fun-facts.json")

# The strip's headline on a phone: a fact past this wraps to a paragraph.
MAX_FACT_CHARS = 140
MAX_FACTS = 5
# The article the model sees: the lead plus the sections where the trivia
# lives (Behavior, Breeding, Diet), bounded so one bird stays a few cents.
# Most bird articles fit whole; the long ones lose their tail, which is
# where Wikipedia keeps taxonomy minutiae and conservation status.
MAX_ARTICLE_CHARS = 30_000
# Two facts sharing this much of their vocabulary are one fact twice.
NEAR_DUPLICATE = 0.6
ROSTER_TIMEOUT_S = 20

# Trailing apparatus: nothing a fact can rest on lives past these headings.
_BACK_MATTER = re.compile(
    r"^==+\s*(?:See also|References|Notes|Footnotes|Further reading|"
    r"External links|Bibliography|Sources|Citations|Cited (?:texts|works))"
    r"\s*==+\s*$", re.M | re.I)

# The swift's failure: an absolute the source never stated. A fact may use
# one of these words only when its quote does.
ABSOLUTES = ("only", "always", "never", "first", "last", "all", "every",
             "none", "largest", "smallest", "biggest", "fastest", "slowest",
             "longest", "shortest", "oldest", "most", "least", "unique")
_WORDS = re.compile(r"[a-z0-9']+")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…)\s*")

SYSTEM = """\
You pick fun facts about one bird for a family's backyard bird-listening \
page. The facts appear one at a time under the heading "Did you know?".

Rules:
- Use ONLY the article text you are given. Never add anything from your own \
knowledge, even if you are sure it is true.
- Each fact is one sentence of at most 140 characters, in plain American \
English, that a family member would enjoy repeating at dinner.
- Prefer behavior, nesting, diet, voice, history, records and oddities. Skip \
plain descriptions of plumage, size and range.
- Keep the article's meaning exactly. Never flip a negation or a direction. \
Never add "only", "always", "never", "first", "largest" or any other \
absolute unless your quote says it.
- No number that isn't in your quote.
- Don't begin with "Did you know"; the heading says it. No stock phrases \
("fascinating", "incredibly", "a true marvel").
- For each fact, give the quote: the sentence or sentences from the article \
it rests on, copied character for character.

Answer with JSON only, no prose around it:
{"facts": [{"fact": "...", "quote": "..."}]}
Give 3 to 5 facts; fewer if the article doesn't hold that many good ones, \
and an empty list if it holds none."""


# --- Pure pieces (test_listener_fun_facts.py) --------------------------------

def article_from(d):
    """The summary query (species_profile.summary_url, the same call the
    profile pass makes) -> (title, full plain-text extract), or (None, None)
    for a missing page, a disambiguation page or an empty extract -- the
    profile pass's own definition of "nothing usable"."""
    pages = ((d or {}).get("query") or {}).get("pages") or []
    page = pages[0] if pages else None
    if not page or page.get("missing"):
        return None, None
    if "disambiguation" in (page.get("pageprops") or {}):
        return None, None
    text = (page.get("extract") or "").strip()
    if not text:
        return None, None
    return page.get("title"), text


def trim_article(text, limit=MAX_ARTICLE_CHARS):
    """Drop the back matter, then cap at a paragraph boundary."""
    m = _BACK_MATTER.search(text or "")
    body = (text[:m.start()] if m else (text or "")).strip()
    if len(body) <= limit:
        return body
    cut = body.rfind("\n", 0, limit)
    return body[:cut if cut > 0 else limit].rstrip()


def build_prompt(common, sci, article):
    return (f"Bird: {common} ({sci})\n\n"
            f"Article:\n<<<\n{article}\n>>>")


def parse_reply(text):
    """The model's JSON -> [(fact, quote), ...], or None when the answer
    isn't the shape asked for. Items missing either half are skipped."""
    raw = _FENCE.sub("", (text or "").strip())
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        body = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    items = body.get("facts") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return None
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        fact = str(it.get("fact") or "").strip()
        quote = str(it.get("quote") or "").strip()
        if fact and quote:
            out.append((fact, quote))
    return out


def normalize(text):
    """The quote check's view of text: Unicode-folded, curly quotes and
    dashes made plain, whitespace collapsed, case folded. A model that
    retypes ’ as ' or a newline as a space has still quoted faithfully."""
    t = unicodedata.normalize("NFKC", text or "")
    t = (t.replace("‘", "'").replace("’", "'")
          .replace("“", '"').replace("”", '"')
          .replace("–", "-").replace("—", "-"))
    return re.sub(r"\s+", " ", t).strip().casefold()


def quote_found(quote, article):
    """Every ellipsis-separated piece of the quote appears in the article.
    A quote that elides the middle of a sentence is still a quote; a piece
    the article never says is not."""
    hay = normalize(article)
    pieces = [p for p in _ELLIPSIS.split(quote or "") if p.strip()]
    return bool(pieces) and all(normalize(p) in hay for p in pieces)


def check_fact(fact, quote, article):
    """[violation, ...] -- empty means the fact may enter the pool. English,
    like notes_validate, because the run prints them for the reviewer."""
    out = []
    if not quote_found(quote, article):
        out.append("its quote is not in the article")
    if len(fact) > MAX_FACT_CHARS:
        out.append(f"longer than {MAX_FACT_CHARS} characters ({len(fact)})")
    unfounded = sorted(figures(fact) - figures(quote))
    if unfounded:
        out.append(f"figures its quote doesn't hold: {', '.join(unfounded)}")
    quote_words = set(_WORDS.findall(normalize(quote)))
    added = [w for w in ABSOLUTES
             if w in set(_WORDS.findall(normalize(fact)))
             and w not in quote_words]
    if added:
        out.append(f"absolutes its quote doesn't say: {', '.join(added)}")
    if normalize(fact).startswith("did you know"):
        out.append('begins with "Did you know"')
    return out


def fact_id(text):
    """A short stable id: the dashboard's schedule and the dismiss cookie
    key on it, so the same words are the same fact on every box."""
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:10]


def _vocabulary(text):
    return set(_WORDS.findall(normalize(text)))


def near_duplicate(a, b, threshold=NEAR_DUPLICATE):
    va, vb = _vocabulary(a), _vocabulary(b)
    if not va or not vb:
        return False
    return len(va & vb) / len(va | vb) >= threshold


def select_facts(pairs, article):
    """Check every (fact, quote); keep the first MAX_FACTS that pass and
    aren't a near-duplicate of one already kept. Returns (kept, rejected):
    kept as pool facts, rejected as (fact, [reasons]) for the run's log."""
    kept, rejected = [], []
    for fact, quote in pairs:
        reasons = check_fact(fact, quote, article)
        if not reasons and any(near_duplicate(fact, k["text"]) for k in kept):
            reasons = ["repeats an earlier fact"]
        if not reasons and len(kept) >= MAX_FACTS:
            reasons = [f"past the {MAX_FACTS}-fact cap"]
        if reasons:
            rejected.append((fact, reasons))
            continue
        kept.append({"id": fact_id(fact), "text": fact, "quote": quote})
    return kept, rejected


def species_union(house, stations, extra):
    """[(sci, common)] in first-seen order, deduped by scientific name: the
    house life list, then each station's roster, then --species by hand.
    The first common name seen wins; a hand-added species with none known
    gets its scientific name until the article supplies one."""
    seen = {}
    for rows in (house, *stations, [(s, None) for s in extra]):
        for sci, common in rows:
            sci = (sci or "").strip()
            if sci and (sci not in seen or not seen[sci]):
                seen[sci] = seen.get(sci) or (common or "").strip() or None
    return list(seen.items())


def todo(union, pool, refresh=None):
    """The species a run writes: --refresh names one (whatever the pool
    holds); otherwise every species with no entry in the pool yet."""
    if refresh:
        common = dict(union).get(refresh) or next(
            (e.get("species_common") for e in pool["species"]
             if e.get("species_sci") == refresh), None)
        return [(refresh, common)]
    have = {e.get("species_sci") for e in pool["species"]}
    return [(sci, common) for sci, common in union if sci not in have]


def empty_pool():
    return {"version": POOL_VERSION, "species": []}


def put_entry(pool, entry):
    """Insert or replace one species' entry, keeping the file sorted."""
    rest = [e for e in pool["species"]
            if e.get("species_sci") != entry["species_sci"]]
    pool["species"] = sorted(rest + [entry], key=lambda e: e["species_sci"])
    return pool


def dump_pool(pool):
    """Stable text so a PR diff reads fact by fact: sorted species, two-
    space indent, real Unicode, trailing newline."""
    body = {"version": pool.get("version", POOL_VERSION),
            "species": sorted(pool["species"], key=lambda e: e["species_sci"])}
    return json.dumps(body, indent=2, ensure_ascii=False) + "\n"


# --- The per-species function -------------------------------------------------

def facts_for(sci, common, writer, *, fetch_json=species_profile.get_json,
              today=None):
    """One species' pool entry, or None when the outcome was transient
    (Wikipedia or the writer unreachable) and the next run should retry.
    Returns (entry, rejected) so the CLI can show the reviewer what fell."""
    title, text = article_from(fetch_json(species_profile.summary_url(sci)))
    stamp = (today or datetime.date.today()).isoformat()
    if text is None:
        return ({"species_sci": sci, "species_common": common or sci,
                 "article": None, "fetched": stamp, "model": None,
                 "facts": []}, [])
    article = trim_article(text)
    reply = writer.complete(SYSTEM, build_prompt(common or title, sci, article))
    if reply is None:
        return None
    pairs = parse_reply(reply)
    if pairs is None:
        print(f"[facts] {common or sci}: the answer wasn't the JSON asked "
              "for -- recorded with no facts", flush=True)
        pairs = []
    kept, rejected = select_facts(pairs, article)
    return ({"species_sci": sci, "species_common": common or title or sci,
             "article": title, "fetched": stamp,
             "model": getattr(writer, "model", None), "facts": kept},
            rejected)


# --- The wire -----------------------------------------------------------------

def house_life_list(path):
    """[(sci, common)] from earl.db's life list, opened read-only (this
    pass owns nothing in the store). A missing file is an empty list."""
    if not path or not os.path.exists(path):
        return []
    conn = sqlite3.connect(
        pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return [(r[0], r[1]) for r in conn.execute(
            "SELECT species_sci, species_common FROM life_list")]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def station_roster(url):
    """[(sci, common)] from a station's /aviary/roster over the tailnet --
    the direction the liveness watcher already polls (#342)."""
    req = urllib.request.Request(url.rstrip("/") + "/roster",
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=ROSTER_TIMEOUT_S) as r:
        body = json.loads(r.read().decode("utf-8"))
    return [(e.get("species_sci"), e.get("species_common"))
            for e in body.get("species") or [] if isinstance(e, dict)]


def load_pool(path):
    if not path or not os.path.exists(path):
        return empty_pool()
    with open(path, encoding="utf-8") as f:
        pool = json.load(f)
    pool.setdefault("version", POOL_VERSION)
    pool.setdefault("species", [])
    return pool


def write_pool(path, pool):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump_pool(pool))
    os.replace(tmp, path)


# --- The CLI ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Write the Aviary's fun-fact pool from Wikipedia (#422)")
    ap.add_argument("--out", help="where the pool is written; resumed from "
                    "when it exists (never orchid's deploy checkout)")
    ap.add_argument("--pool", default=SHIPPED_POOL,
                    help="the shipped pool a fresh --out starts from "
                         f"(default {SHIPPED_POOL})")
    ap.add_argument("--refresh", metavar="SPECIES_SCI",
                    help="rewrite one species' facts")
    ap.add_argument("--species", metavar="SPECIES_SCI", action="append",
                    default=[], help="add a species by hand (repeatable)")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many species (a trial run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the facts, write nothing")
    args = ap.parse_args()
    if not args.out and not args.dry_run:
        ap.error("--out is required (e.g. --out ~/fun-facts.json)")

    config = gateway_config()
    if config is None:
        raise SystemExit("[facts] no AI_GATEWAY_API_KEY -- nothing written")
    writer = Gateway(*config)

    out = os.path.expanduser(args.out) if args.out else None
    start = out if out and os.path.exists(out) else args.pool
    pool = load_pool(start)
    print(f"[facts] starting from {start} ({len(pool['species'])} species)",
          flush=True)

    house = house_life_list(species_profile.db_path())
    stations = []
    raw = (os.environ.get("MERLE_STATIONS") or "").strip()
    if raw:
        from flyway.liveness import parse_stations
        for name, url in parse_stations(raw):
            try:
                rows = station_roster(url)
            except Exception as e:
                print(f"[facts] {name}: roster unreachable ({e}) -- its "
                      "birds wait for the next run", flush=True)
                continue
            print(f"[facts] {name}: {len(rows)} species", flush=True)
            stations.append(rows)
    union = species_union(house, stations, args.species)
    work = todo(union, pool, args.refresh)
    if args.limit:
        work = work[:args.limit]
    print(f"[facts] house {len(house)} species, {len(union)} in all; "
          f"{len(work)} to write with {writer.model}", flush=True)

    counts = {"written": 0, "empty": 0, "retry": 0}
    for i, (sci, common) in enumerate(work):
        if i:
            time.sleep(species_profile.THROTTLE_S)
        try:
            result = facts_for(sci, common, writer)
        except Exception as e:
            print(f"[facts] {common or sci}: FAILED ({e}) -- retried next run",
                  flush=True)
            counts["retry"] += 1
            continue
        if result is None:
            print(f"[facts] {common or sci}: no answer -- retried next run",
                  flush=True)
            counts["retry"] += 1
            continue
        entry, rejected = result
        name = entry["species_common"]
        if entry["article"] is None:
            print(f"[facts] {name}: no usable Wikipedia page", flush=True)
        for f in entry["facts"]:
            print(f"[facts] {name}: + {f['text']}", flush=True)
        for text, reasons in rejected:
            print(f"[facts] {name}: - {text}  [{'; '.join(reasons)}]",
                  flush=True)
        counts["written" if entry["facts"] else "empty"] += 1
        if not args.dry_run:
            put_entry(pool, entry)
            write_pool(out, pool)    # after every bird: a crash loses one
    print(f"[facts] done: {counts['written']} with facts, {counts['empty']} "
          f"with none, {counts['retry']} to retry"
          + ("" if args.dry_run else f" -> {out}"), flush=True)


if __name__ == "__main__":
    main()
