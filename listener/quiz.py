# =============================================================================
# aviary -- listener/quiz.py
#
# The quiz's question pool (issue #715, phase 1 of epic #712): every fact in
# the fun-fact pool (web/data/fun-facts.json, #422) judged once, and the
# distinctive ones turned into a "which bird...?" question with one right
# answer (the fact's own species) and three to eight wrong answers that have
# each been checked. The pool is a JSON file in the repo (web/data/quiz.json)
# and reaches the stations with the nightly promote like any code.
#
# A RIGHT ANSWER MARKED WRONG IS THE ONE OUTCOME THE QUIZ CANNOT HAVE. The
# fact writer proved each fact true of one bird; nothing proved it false of
# another. The case that shaped this file: "A group of tufted titmice is
# called a troupe or a banditry" reads like a perfect question, and
# chickadees are commonly called a banditry too. So:
#   - pass A (one call per source species, no article) asks whether each
#     fact is distinctive, words the question, and proposes six to eight
#     wrong answers from the other birds in the fact pool;
#   - pass B (per candidate species, beside ITS Wikipedia article) asks of
#     every claim that proposed this bird: is it true of this one too?
#   - THE VERIFIER'S RULE IS THE REVERSE OF THE FACT WRITER'S. The fact
#     writer may use only the article. The verifier must use the article AND
#     its own knowledge, because a chickadee's article may never say
#     "banditry". A candidate survives only on a clear "no"; "yes",
#     "unsure", no verdict and an unreadable one all drop it. Dropping costs
#     nothing; keeping a bad one is the failure;
#   - the pure checks below refuse a question that names its answer or
#     gives away a word of its name, and one with fewer than three wrong
#     answers left;
#   - and a person reads every question in the PR before it ships. The
#     checks narrow what the review has to catch; they do not replace it.
#
# THE MODEL WRITES NO NEW CLAIM. A question restates a fact the pool already
# holds and its answer is that fact's species. The reveal is the fact
# itself, read from the fact pool, so there is one copy.
#
# HAND-RUN, NEVER A TICK. Nothing in the enrichment loop imports this file.
# python -m listener.quiz --out ~/quiz.json on orchid, then scp the file
# home and open a PR. --out is REQUIRED (bar --dry-run) and never the deploy
# checkout's copy: an uncommitted edit in orchid's checkout aborts
# autodeploy's pull silently. The run starts from --out when it exists (a
# resumed run) and from the shipped pool otherwise. Its only inputs are the
# shipped fact pool, Wikipedia and the gateway: no earl.db, no station call.
#
# IDEMPOTENT BY ENTRY. One entry per fact id that has been judged. A plain
# run only judges fact ids with no entry, so a second run makes no model
# calls. A skipped fact keeps its entry (question null, skip the reason).
# TO REJECT A QUESTION IN REVIEW, NULL ITS question; NEVER DELETE THE ENTRY:
# a deleted entry is judged again on the next run. A definitive outcome is
# recorded; a transient one (Wikipedia or the gateway unreachable) is not.
# Pass A's proposals are kept in --out as entries carrying "candidates"
# (still waiting on pass B), so an interrupted run resumes without paying
# for pass A again; the shipped-file check refuses a file that holds one.
# An entry whose fact has left the fact pool is pruned and reported.
#
# WIKIPEDIA ONLY (#318): the fun-facts fetch, and nothing from ADW.
#
# Config (env, the units' names):
#   AI_GATEWAY_API_KEY, MERLE_GATEWAY_MODEL   the model (gateway_client.py)
# =============================================================================

import argparse
import contextlib
import datetime
import io
import json
import os
import re
import time

from listener import fun_facts, species_profile
from gateway_client import Gateway, gateway_config

POOL_VERSION = 1
# Both relative to the repo root (the units' WorkingDirectory).
FACT_POOL = fun_facts.SHIPPED_POOL
SHIPPED_POOL = os.path.join("web", "data", "quiz.json")

# The strip's headline on a phone, the fact's own cap.
MAX_QUESTION_CHARS = 140
MIN_WRONG = 3
# Kept so each box can later draw three from its own life list.
MAX_WRONG = 8
# One verifier call carries the candidate's whole article and this many
# claims. Bounded because the gateway client's token ceiling is fixed and a
# cut-off answer is no answer: a bird proposed under a hundred claims would
# never get through in one call.
CLAIMS_PER_CALL = 20

SKIP_GENERAL = "general trait"
SKIP_TOO_FEW = "fewer than three wrong answers"
SKIP_NAMES_ANSWER = "names its answer"
SKIP_UNREADABLE = "the answer wasn't the JSON asked for"
SKIP_NOT_JUDGED = "the answer left this fact out"

# Question words that are a name word in another form.
_IRREGULAR = {"titmice": "titmouse", "geese": "goose"}
_USAGE = re.compile(r"(\d+) tokens in, (\d+) out")

PROPOSE_SYSTEM = """\
You help write a daily multiple-choice quiz for a family's backyard \
bird-listening page. Every question asks which bird a fact is about. Its \
right answer is the bird the fact belongs to, and its wrong answers are other \
birds from a fixed list.

You are given one bird's facts, each already verified against its Wikipedia \
article and shown with the sentence it rests on, and the list of other birds.

For each fact, decide three things.

distinctive: true only if the fact is particular to this bird: a nickname, a \
record, a named event, a piece of history, an odd behavior. false if it is a \
general trait many birds share ("eats insects and spiders", "usually mates \
for life"), or if you can think of several birds on the list it is also true \
of. When in doubt, false.

question: one sentence of at most 140 characters, in plain American English, \
that the fact answers.
- Never name the bird, and don't give the answer away another way.
- A word of the bird's name may appear only when every candidate you give \
shares that word ("Which woodpecker..." with only woodpeckers as candidates). \
Otherwise use none: not "finches" for a House Finch, not "red" for a \
Red-tailed Hawk.
- Restate the fact and nothing more. Add no claim the fact does not make.
- Don't begin with "Did you know". No stock phrases.

candidates: six to eight birds from the list, names copied exactly, that a \
reader might plausibly pick and that you believe the fact is false of. Leave \
out any bird the fact is true of, or is commonly said of, even loosely. Each \
one will be checked against its own article afterwards.

Answer with JSON only, no prose around it, one item per fact:
{"facts": [{"id": "...", "distinctive": true, "question": "...", \
"candidates": ["...", "..."]}]}
For a fact that is not distinctive, give "question": null and \
"candidates": []."""

VERIFY_SYSTEM = """\
You check the wrong answers of a multiple-choice bird quiz. Each question \
asks which bird a fact is about. The one failure the quiz cannot have is a \
wrong answer that is actually right: someone picks it, is told it was not the \
answer, and they were correct.

You are given one bird (the candidate), its Wikipedia article, and a list of \
claims. Each claim is a fact known to be true of a DIFFERENT bird, with the \
quiz question written from it. The candidate has been proposed as a wrong \
answer to each. For each claim, decide whether the candidate would also be a \
fair right answer.

Use the article AND your own knowledge. The article not mentioning something \
does not make it false of this bird.

- "yes": the article supports the claim for this bird, or you know it to be \
true of this bird, or it is commonly said of this bird.
- "unsure": it may be true, is partly true, is true of close relatives, or \
you cannot rule it out.
- "no": only when the article does not support it and you are confident it \
is neither true of this bird nor commonly said of it.

Dropping a candidate costs nothing; keeping a bad one is the failure. When in \
doubt, "unsure".

Answer with JSON only, no prose around it, one item per claim:
{"verdicts": [{"id": "...", "verdict": "no", "reason": "..."}]}
The reason is one plain sentence of at most 120 characters saying why."""


# --- Pure pieces (test_listener_quiz.py) --------------------------------------

def _words(text):
    """Whole words, case and typography folded, hyphens split, a possessive
    's dropped: "Red-tailed" is "red" and "tailed", "Cooper's" is "cooper"."""
    out = []
    for w in re.findall(r"[a-z0-9']+", fun_facts.normalize(text)):
        w = w.strip("'")
        if w.endswith("'s"):
            w = w[:-2]
        if w:
            out.append(w)
    return out


def name_words(common):
    return set(_words(common))


def leaked_words(question, answer_common):
    """The words of the answer's name that the question carries, plurals
    included ("finches" leaks "finch", "titmice" leaks "titmouse")."""
    said = set()
    for w in _words(question):
        said.add(w)
        said.add(_IRREGULAR.get(w, w))
        if w.endswith("es"):
            said.add(w[:-2])
        if w.endswith("s"):
            said.add(w[:-1])
    return sorted(name_words(answer_common) & said)


def _gives_away(words):
    return f"gives away a word of its answer's name ({', '.join(words)})"


def check_question(question, answer_common, wrong_commons):
    """[violation, ...] -- empty means the question may ship beside these
    wrong answers. English, because the run prints them for the reviewer."""
    out = []
    q = fun_facts.normalize(question)
    if fun_facts.normalize(answer_common) in q:
        out.append(SKIP_NAMES_ANSWER)
    else:
        unshared = [w for w in leaked_words(question, answer_common)
                    if not all(w in name_words(c) for c in wrong_commons)]
        if unshared:
            out.append(_gives_away(unshared))
    if len(question) > MAX_QUESTION_CHARS:
        out.append(f"longer than {MAX_QUESTION_CHARS} characters "
                   f"({len(question)})")
    if q.startswith("did you know"):
        out.append('begins with "Did you know"')
    return out


def check_wrong(wrong, answer_sci, known):
    """[violation, ...] for a question's wrong answers: each a species in
    the fact pool (`known`, a set of scientific names), none the answer's
    own, none twice, each with its reason, and enough of them."""
    out = []
    seen = set()
    for w in wrong:
        sci = w.get("species_sci")
        name = w.get("species_common") or sci
        if sci == answer_sci:
            out.append(f"{name} is the answer")
        elif sci not in known:
            out.append(f"{name} is not in the fact pool")
        if sci in seen:
            out.append(f"{name} appears twice")
        seen.add(sci)
        if not str(w.get("reason") or "").strip():
            out.append(f"{name} has no reason")
    if len(wrong) < MIN_WRONG:
        out.append(SKIP_TOO_FEW)
    if len(wrong) > MAX_WRONG:
        out.append(f"more than {MAX_WRONG} wrong answers")
    return out


def fact_index(fact_pool):
    """{fact id: {species_sci, species_common, text, quote}} over the pool."""
    return {f["id"]: {"species_sci": e["species_sci"],
                      "species_common": e["species_common"],
                      "text": f["text"], "quote": f.get("quote") or ""}
            for e in fact_pool.get("species") or []
            for f in e.get("facts") or []}


def roster(fact_pool):
    """[(sci, common)] a wrong answer may be drawn from: every species in
    the fact pool with a known article, since pass B has to read it."""
    return sorted(((e["species_sci"], e["species_common"])
                   for e in fact_pool.get("species") or [] if e.get("article")),
                  key=lambda r: r[1])


def build_propose_prompt(common, sci, facts, others):
    """Pass A: the bird's facts with their quotes, and every OTHER bird."""
    lines = [f"Bird: {common} ({sci})", "", "Facts:"]
    for f in facts:
        lines += [f"- id {f['id']}: {f['text']}",
                  f"  rests on: \"{f.get('quote') or ''}\""]
    lines += ["", "Other birds (candidates come from this list only):"]
    lines += [f"- {name}" for s, name in others if s != sci]
    return "\n".join(lines)


def build_verify_prompt(common, sci, article, claims):
    """Pass B: every claim that proposed this bird, beside its article.
    `claims` is [(fact id, source common name, fact text, question)]."""
    lines = [f"Candidate: {common} ({sci})", "", "Article:", "<<<", article,
             ">>>", "", f"Claims (is each also true of the {common}?):"]
    for fid, source, text, question in claims:
        lines += [f"- id {fid}", f"  true of the {source}: {text}",
                  f"  question: {question}"]
    return "\n".join(lines)


def _json_list(text, key):
    """The reply's list under `key`, or None when the answer isn't the
    shape asked for (prose, a truncated body, another shape)."""
    raw = fun_facts._FENCE.sub("", (text or "").strip())
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        body = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    items = body.get(key) if isinstance(body, dict) else None
    return items if isinstance(items, list) else None


def parse_proposals(text):
    """Pass A's JSON -> {fact id: {distinctive, question, candidates}}, or
    None. `distinctive` is True only for a literal true."""
    items = _json_list(text, "facts")
    if items is None:
        return None
    out = {}
    for it in items:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        names = it.get("candidates")
        out[str(it["id"]).strip()] = {
            "distinctive": it.get("distinctive") is True,
            "question": str(it.get("question") or "").strip(),
            "candidates": [str(n).strip() for n in names
                           if isinstance(n, str) and n.strip()]
            if isinstance(names, list) else []}
    return out


def parse_verdicts(text):
    """Pass B's JSON -> {fact id: (verdict, reason)}, or None. The verdict
    is passed through lower-cased; only keeps() decides what survives."""
    items = _json_list(text, "verdicts")
    if items is None:
        return None
    out = {}
    for it in items:
        if not isinstance(it, dict) or not it.get("id"):
            continue
        out[str(it["id"]).strip()] = (
            str(it.get("verdict") or "").strip().casefold(),
            str(it.get("reason") or "").strip())
    return out


def keeps(verdict, reason):
    """Only a clear "no" that says why keeps a candidate."""
    return verdict == "no" and bool(reason)


def _skipped(fact_id, fact, reason, model, stamp):
    return {"fact_id": fact_id, "species_sci": fact["species_sci"],
            "species_common": fact["species_common"], "question": None,
            "wrong": [], "skip": reason, "model": model, "written": stamp}


def propose_entry(fact_id, fact, proposal, others, model, stamp):
    """One fact's entry after pass A: skipped for good, or a question
    waiting on pass B with its candidates. `proposal` is the fact's item
    from parse_proposals (None when the answer left it out); `others` is
    the roster. A candidate that isn't on the roster, is the answer's own
    species, repeats, or lacks a name word the question gives away is
    dropped here, before anything is spent checking it."""
    if proposal is None:
        return _skipped(fact_id, fact, SKIP_NOT_JUDGED, model, stamp)
    if not proposal["distinctive"]:
        return _skipped(fact_id, fact, SKIP_GENERAL, model, stamp)
    question, answer = proposal["question"], fact["species_common"]
    if not question:
        return _skipped(fact_id, fact, "no question written", model, stamp)
    # With no wrong answers yet, this is the name, length and opener checks;
    # the name-word rule is applied to the candidates below.
    reasons = check_question(question, answer, [])
    if reasons:
        return _skipped(fact_id, fact, "; ".join(reasons), model, stamp)
    leaked = leaked_words(question, answer)
    by_name = {fun_facts.normalize(c): (s, c) for s, c in others}
    candidates = []
    for name in proposal["candidates"]:
        hit = by_name.get(fun_facts.normalize(name))
        if (hit is None or hit[0] == fact["species_sci"]
                or any(c["species_sci"] == hit[0] for c in candidates)
                or not all(w in name_words(hit[1]) for w in leaked)):
            continue
        candidates.append({"species_sci": hit[0], "species_common": hit[1]})
    candidates = candidates[:MAX_WRONG]
    if len(candidates) < MIN_WRONG:
        return _skipped(fact_id, fact,
                        _gives_away(leaked) if leaked else SKIP_TOO_FEW,
                        model, stamp)
    return {"fact_id": fact_id, "species_sci": fact["species_sci"],
            "species_common": answer, "question": question, "wrong": [],
            "candidates": candidates, "skip": None, "model": model,
            "written": stamp}


def waiting(entry):
    """Still waiting on pass B."""
    return "candidates" in entry


def claims_by_candidate(quiz):
    """{candidate sci: (common, [entry, ...])} over every waiting entry:
    the grouping that makes pass B one article per candidate."""
    out = {}
    for e in quiz["questions"]:
        for c in e.get("candidates") or []:
            out.setdefault(c["species_sci"], (c["species_common"], []))[1] \
               .append(e)
    return out


def resolve(entry, sci, verdict, reason, known, stamp):
    """Record one candidate's verdict on a waiting entry: it leaves the
    candidates, and joins the wrong answers only on a clear "no". When the
    last candidate is resolved the entry is settled: a question with its
    checked wrong answers, or skipped with the reason."""
    mine = [c for c in entry["candidates"] if c["species_sci"] == sci]
    entry["candidates"] = [c for c in entry["candidates"]
                           if c["species_sci"] != sci]
    if mine and keeps(verdict, reason):
        entry["wrong"].append({**mine[0], "reason": reason})
    if entry["candidates"]:
        return entry
    del entry["candidates"]
    wrong = entry["wrong"][:MAX_WRONG]
    reasons = check_wrong(wrong, entry["species_sci"], known)
    if SKIP_TOO_FEW in reasons:
        reasons = [SKIP_TOO_FEW]
    else:
        reasons += check_question(entry["question"], entry["species_common"],
                                  [w["species_common"] for w in wrong])
    entry["wrong"] = [] if reasons else wrong
    if reasons:
        entry["question"], entry["skip"] = None, "; ".join(reasons)
    entry["written"] = stamp
    return entry


def todo(fact_pool, quiz, refresh=None):
    """[(species entry, [fact, ...])] a run sends to pass A: every fact id
    with no entry, grouped by its species in the fact pool's order.
    --refresh names one species, whose entries the caller has dropped."""
    have = {e["fact_id"] for e in quiz["questions"]}
    work = []
    for sp in fact_pool.get("species") or []:
        if refresh and sp["species_sci"] != refresh:
            continue
        facts = [f for f in sp.get("facts") or [] if f["id"] not in have]
        if facts:
            work.append((sp, facts))
    return work


def drop_species(quiz, sci):
    """--refresh: forget one species' entries so they are judged again."""
    quiz["questions"] = [e for e in quiz["questions"]
                         if e.get("species_sci") != sci]
    return quiz


def prune(quiz, index):
    """Drop every entry whose fact has left the fact pool; return them."""
    gone = [e for e in quiz["questions"] if e.get("fact_id") not in index]
    quiz["questions"] = [e for e in quiz["questions"]
                         if e.get("fact_id") in index]
    return gone


def empty_quiz():
    return {"version": POOL_VERSION, "questions": []}


def put_entry(quiz, entry):
    """Insert or replace one fact's entry."""
    quiz["questions"] = [e for e in quiz["questions"]
                         if e.get("fact_id") != entry["fact_id"]] + [entry]
    return quiz


def dump_quiz(quiz):
    """Stable text so a PR diff reads question by question: sorted by
    species then fact id, two-space indent, real Unicode, trailing newline."""
    body = {"version": quiz.get("version", POOL_VERSION),
            "questions": sorted(quiz["questions"], key=lambda e: (
                e.get("species_sci") or "", e.get("fact_id") or ""))}
    return json.dumps(body, indent=2, ensure_ascii=False) + "\n"


def shipped_problems(quiz, fact_pool):
    """[problem, ...] in a shipped quiz.json, read beside the fact pool:
    what stops a hand edit in review from shipping a question the generator
    would have refused. A nulled or skipped entry has nothing to check."""
    index = fact_index(fact_pool)
    known = {e["species_sci"] for e in fact_pool.get("species") or []}
    out, seen = [], set()
    for e in quiz.get("questions") or []:
        fid = e.get("fact_id")
        if fid in seen:
            out.append(f"{fid}: more than one entry")
        seen.add(fid)
        if waiting(e):
            out.append(f"{fid}: still waiting on pass B")
            continue
        if not e.get("question"):
            continue
        fact = index.get(fid)
        if fact is None:
            out.append(f"{fid}: its fact is not in the fact pool")
            continue
        if fact["species_sci"] != e.get("species_sci"):
            out.append(f"{fid}: its answer is not its fact's species")
        wrong = e.get("wrong") or []
        for r in (check_wrong(wrong, fact["species_sci"], known)
                  + check_question(e["question"], fact["species_common"],
                                   [w.get("species_common") or ""
                                    for w in wrong])):
            out.append(f"{fid}: {r}")
    return out


def shipped_file_problems(quiz_path=SHIPPED_POOL, facts_path=FACT_POOL):
    """shipped_problems over the files, or None while there is no quiz.json
    (the code lands a PR before the first pool does)."""
    if not os.path.exists(quiz_path):
        return None
    return shipped_problems(load_quiz(quiz_path),
                            fun_facts.load_pool(facts_path))


# --- The two passes -----------------------------------------------------------

def propose_for(species, facts, others, writer, *, today=None):
    """Pass A for one source species: its unjudged facts' entries, or None
    when the outcome was transient (the gateway gave no answer)."""
    sci, common = species["species_sci"], species["species_common"]
    reply = writer.complete(
        PROPOSE_SYSTEM, build_propose_prompt(common, sci, facts, others))
    if reply is None:
        return None
    stamp = (today or datetime.date.today()).isoformat()
    model = getattr(writer, "model", None)
    proposals = parse_proposals(reply)
    out = []
    for f in facts:
        fact = {"species_sci": sci, "species_common": common, **f}
        if proposals is None:
            out.append(_skipped(f["id"], fact, SKIP_UNREADABLE, model, stamp))
        else:
            out.append(propose_entry(f["id"], fact, proposals.get(f["id"]),
                                     others, model, stamp))
    return out


def verify_candidate(sci, common, entries, index, writer, *,
                     fetch_json=species_profile.get_json):
    """Pass B for one candidate species: {fact id: (verdict, reason)} for
    every claim that got a definitive outcome. A claim missing from the
    result was transient (the gateway gave no answer) and stays waiting. A
    candidate with no usable article, an unreadable answer and a claim the
    answer left out are all definitive, and none of them is a "no".
    Wikipedia being unreachable raises, like the fun-facts fetch."""
    _, text = fun_facts.article_from(
        fetch_json(species_profile.summary_url(sci)))
    if text is None:
        return {e["fact_id"]: ("no article", "") for e in entries}
    article = fun_facts.trim_article(text)
    out = {}
    for i in range(0, len(entries), CLAIMS_PER_CALL):
        batch = entries[i:i + CLAIMS_PER_CALL]
        claims = [(e["fact_id"], e["species_common"],
                   index[e["fact_id"]]["text"], e["question"]) for e in batch]
        reply = writer.complete(
            VERIFY_SYSTEM, build_verify_prompt(common, sci, article, claims))
        if reply is None:
            continue
        verdicts = parse_verdicts(reply)
        for e in batch:
            if verdicts is None:
                out[e["fact_id"]] = ("unreadable", "")
            else:
                out[e["fact_id"]] = verdicts.get(e["fact_id"], ("missing", ""))
    return out


# --- The wire -----------------------------------------------------------------

class Metered:
    """The gateway with a running bill. gateway_client prints each call's
    token usage and returns only the text, so the meter reads that line as
    it passes it through (test_listener_quiz pins the line it depends on)."""

    def __init__(self, writer):
        self.writer = writer
        self.model = getattr(writer, "model", None)
        self.calls = self.tokens_in = self.tokens_out = 0

    def complete(self, system, prompt, *a, **kw):
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            text = self.writer.complete(system, prompt, *a, **kw)
        print(said.getvalue(), end="", flush=True)
        self.calls += 1
        for tin, tout in _USAGE.findall(said.getvalue()):
            self.tokens_in += int(tin)
            self.tokens_out += int(tout)
        return text

    def spend(self):
        return (f"{self.calls} model calls, {self.tokens_in} tokens in, "
                f"{self.tokens_out} out ({self.model})")


def load_quiz(path):
    if not path or not os.path.exists(path):
        return empty_quiz()
    with open(path, encoding="utf-8") as f:
        quiz = json.load(f)
    quiz.setdefault("version", POOL_VERSION)
    quiz.setdefault("questions", [])
    return quiz


def write_quiz(path, quiz):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(dump_quiz(quiz))
    os.replace(tmp, path)


# --- The CLI ------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Write the Aviary's quiz question pool from the "
                    "fun-fact pool (#715)")
    ap.add_argument("--out", help="where the pool is written; resumed from "
                    "when it exists (never orchid's deploy checkout)")
    ap.add_argument("--refresh", metavar="SPECIES_SCI",
                    help="rejudge one species' facts")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop pass A after this many source species (a "
                         "trial run)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the questions, write nothing")
    args = ap.parse_args()
    if not args.out and not args.dry_run:
        ap.error("--out is required (e.g. --out ~/quiz.json)")

    config = gateway_config()
    if config is None:
        raise SystemExit("[quiz] no AI_GATEWAY_API_KEY -- nothing written")
    writer = Metered(Gateway(*config))

    fact_pool = fun_facts.load_pool(FACT_POOL)
    index = fact_index(fact_pool)
    others = roster(fact_pool)
    known = {e["species_sci"] for e in fact_pool["species"]}
    if not index:
        raise SystemExit(f"[quiz] no facts in {FACT_POOL} -- run from the "
                         "repo root")

    out = os.path.expanduser(args.out) if args.out else None
    start = out if out and os.path.exists(out) else SHIPPED_POOL
    quiz = load_quiz(start)
    print(f"[quiz] starting from {start} ({len(quiz['questions'])} entries); "
          f"{len(index)} facts across {len(others)} species", flush=True)

    def save():
        if not args.dry_run:
            write_quiz(out, quiz)

    for e in prune(quiz, index):
        print(f"[quiz] pruned {e.get('fact_id')} ({e.get('species_common')}): "
              "its fact has left the fact pool", flush=True)
    if args.refresh:
        drop_species(quiz, args.refresh)
    work = todo(fact_pool, quiz, args.refresh)
    if args.limit:
        work = work[:args.limit]
    print(f"[quiz] pass A: {len(work)} species to judge with {writer.model}",
          flush=True)

    retry = 0
    stamp = datetime.date.today().isoformat()
    for species, facts in work:
        name = species["species_common"]
        try:
            entries = propose_for(species, facts, others, writer)
        except Exception as e:
            entries = None
            print(f"[quiz] {name}: FAILED ({e})", flush=True)
        if entries is None:
            print(f"[quiz] {name}: no answer -- retried next run", flush=True)
            retry += 1
            continue
        for e in entries:
            if e["skip"]:
                print(f"[quiz] {name}: - {index[e['fact_id']]['text']}  "
                      f"[{e['skip']}]", flush=True)
            else:
                print(f"[quiz] {name}: ? {e['question']}  [asks "
                      + ", ".join(c["species_common"]
                                  for c in e["candidates"]) + "]", flush=True)
            put_entry(quiz, e)
        save()          # after every bird: a crash loses one

    groups = claims_by_candidate(quiz)
    print(f"[quiz] pass B: {len(groups)} candidate species to read",
          flush=True)
    for i, (sci, (common, entries)) in enumerate(sorted(groups.items())):
        if i:
            time.sleep(species_profile.THROTTLE_S)
        try:
            verdicts = verify_candidate(sci, common, entries, index, writer)
        except Exception as e:
            print(f"[quiz] {common}: FAILED ({e}) -- retried next run",
                  flush=True)
            retry += 1
            continue
        if len(verdicts) < len(entries):
            print(f"[quiz] {common}: {len(entries) - len(verdicts)} claims "
                  "unanswered -- retried next run", flush=True)
            retry += 1
        for e in entries:
            if e["fact_id"] not in verdicts:
                continue
            verdict, reason = verdicts[e["fact_id"]]
            question = e["question"]
            mark = "+" if keeps(verdict, reason) else "-"
            print(f"[quiz] {common} {mark} {question}  [{verdict}"
                  + (f": {reason}" if reason else "") + "]", flush=True)
            resolve(e, sci, verdict, reason, known, stamp)
            if not waiting(e):
                if e["question"]:
                    print(f"[quiz] {e['species_common']}: = {e['question']}  "
                          + " · ".join(w["species_common"]
                                       for w in e["wrong"]), flush=True)
                else:
                    print(f"[quiz] {e['species_common']}: x {question}  "
                          f"[{e['skip']}]", flush=True)
        save()          # after every candidate: a crash loses one article

    asked = sum(1 for e in quiz["questions"] if e.get("question")
                and not waiting(e))
    held = sum(1 for e in quiz["questions"] if waiting(e))
    print(f"[quiz] done: {asked} questions, "
          f"{len(quiz['questions']) - asked - held} skipped, {held} still "
          f"waiting on pass B, {retry} to retry"
          + ("" if args.dry_run else f" -> {out}"), flush=True)
    print(f"[quiz] spend: {writer.spend()}", flush=True)


if __name__ == "__main__":
    main()
