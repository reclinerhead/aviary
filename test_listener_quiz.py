# =============================================================================
# aviary -- test_listener_quiz.py
#
# The quiz question pool's guarantees (issue #715): a question never names
# its answer or gives away a word of its name; a wrong answer survives only
# on the verifier's clear "no" (the banditry case: a titmouse question whose
# chickadee comes back "yes" ships without the chickadee, or not at all); a
# plain run judges only fact ids with no entry, so a second run spends
# nothing and a question nulled by hand stays nulled; a transient failure
# writes nothing final; the file is written in a stable, diffable shape; and
# the shipped file passes the same checks the generator applies. No test
# reaches Wikipedia or the gateway.
# =============================================================================

import datetime
import json

import pytest

import gateway_client
from listener import quiz, species_profile

TODAY = datetime.date(2026, 11, 24)
STAMP = "2026-11-24"
MODEL = "anthropic/claude-sonnet-5.5"

BIRDS = [
    ("Baeolophus bicolor", "Tufted Titmouse"),
    ("Poecile atricapillus", "Black-capped Chickadee"),
    ("Sitta carolinensis", "White-breasted Nuthatch"),
    ("Cyanocitta cristata", "Blue Jay"),
    ("Cardinalis cardinalis", "Northern Cardinal"),
    ("Dryobates pubescens", "Downy Woodpecker"),
    ("Leuconotopicus villosus", "Hairy Woodpecker"),
    ("Dryocopus pileatus", "Pileated Woodpecker"),
    ("Melanerpes carolinus", "Red-bellied Woodpecker"),
    ("Haemorhous mexicanus", "House Finch"),
    ("Haemorhous purpureus", "Purple Finch"),
    ("Buteo jamaicensis", "Red-tailed Hawk"),
]
SCI = {common: sci for sci, common in BIRDS}
KNOWN = set(SCI.values())

FACTS = {
    "Baeolophus bicolor": [
        ("banditry01", "A group of tufted titmice is called a troupe or a "
                       "banditry."),
        ("insects002", "Tufted titmice eat insects and spiders.")],
    "Dryobates pubescens": [
        ("smallest03", "The downy woodpecker is the smallest woodpecker in "
                       "North America.")],
    "Haemorhous mexicanus": [
        ("hollywood4", "House finches were once sold in New York as "
                       "Hollywood finches.")],
}

BANDITRY_Q = 'A group of which bird is called a "banditry"?'
ARTICLE = ("The black-capped chickadee is a small songbird.\n\n"
           "== Behavior ==\nIt caches seeds for the winter.\n\n"
           "== References ==\n1. A book.\n")


@pytest.fixture(autouse=True)
def no_wikipedia(monkeypatch):
    """No test may reach Wikipedia, including by forgetting to inject."""
    def refuse(url):
        raise AssertionError(f"test tried to fetch {url}")
    monkeypatch.setattr(species_profile, "get_json", refuse)


def fact_pool(facts=FACTS, no_article=()):
    return {"version": 1, "species": [
        {"species_sci": sci, "species_common": common,
         "article": None if common in no_article else common.lower(),
         "fetched": "2026-10-25", "model": MODEL,
         "facts": [{"id": i, "text": t, "quote": f"Quote for {i}."}
                   for i, t in facts.get(sci, [])]}
        for sci, common in BIRDS]}


POOL = fact_pool()
INDEX = quiz.fact_index(POOL)
OTHERS = quiz.roster(POOL)


def summary(extract=ARTICLE, title="Black-capped chickadee", **page):
    return {"query": {"pages": [{"title": title, "extract": extract, **page}]}}


class Writer:
    """A stand-in gateway: hands back canned replies in order (the last one
    repeats) and records the prompts."""
    model = MODEL

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, prompt, *a, **kw):
        self.calls.append((system, prompt))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def proposals(*items):
    return json.dumps({"facts": [
        {"id": i, "distinctive": d, "question": q, "candidates": list(c)}
        for i, d, q, c in items]})


def verdicts(*items):
    return json.dumps({"verdicts": [
        {"id": i, "verdict": v, "reason": r} for i, v, r in items]})


def fact(fid):
    return {"id": fid, **INDEX[fid]}


def proposed(fid, question, names):
    """A fact's entry as pass A leaves it."""
    return quiz.propose_entry(
        fid, INDEX[fid], {"distinctive": True, "question": question,
                          "candidates": list(names)}, OTHERS, MODEL, STAMP)


def banditry(names=("Black-capped Chickadee", "White-breasted Nuthatch",
                    "Blue Jay", "Northern Cardinal")):
    return proposed("banditry01", BANDITRY_Q, names)


def settle(entry, **by_common):
    """Resolve every candidate: "no" with a reason unless told otherwise."""
    for c in list(entry["candidates"]):
        verdict, reason = by_common.get(
            c["species_common"].split()[-1].lower(), ("no", "Not said of it."))
        quiz.resolve(entry, c["species_sci"], verdict, reason, KNOWN, STAMP)
    return entry


# --- The prompts --------------------------------------------------------------

def test_propose_prompt_carries_facts_quotes_and_every_other_bird():
    facts = [fact("banditry01"), fact("insects002")]
    p = quiz.build_propose_prompt("Tufted Titmouse", "Baeolophus bicolor",
                                  facts, OTHERS)
    assert p.startswith("Bird: Tufted Titmouse (Baeolophus bicolor)")
    for f in facts:
        assert f"id {f['id']}: {f['text']}" in p
        assert f["quote"] in p
    listed = p.split("Other birds")[1]
    assert "- Black-capped Chickadee" in listed and "- Blue Jay" in listed
    assert "Tufted Titmouse" not in listed       # never its own wrong answer
    assert len([ln for ln in listed.splitlines() if ln.startswith("- ")]) \
        == len(BIRDS) - 1


def test_the_roster_leaves_out_a_bird_with_no_article():
    names = [c for _, c in quiz.roster(fact_pool(no_article={"Blue Jay"}))]
    assert "Blue Jay" not in names and len(names) == len(BIRDS) - 1
    assert names == sorted(names)


def test_verify_prompt_groups_the_claims_under_one_article():
    claims = [("banditry01", "Tufted Titmouse", INDEX["banditry01"]["text"],
               BANDITRY_Q),
              ("hollywood4", "House Finch", INDEX["hollywood4"]["text"],
               "Which bird was once sold as a Hollywood bird?")]
    p = quiz.build_verify_prompt("Black-capped Chickadee",
                                 "Poecile atricapillus", "THE ARTICLE", claims)
    assert p.startswith("Candidate: Black-capped Chickadee (Poecile atricapillus)")
    assert p.count("THE ARTICLE") == 1
    assert p.index("THE ARTICLE") < p.index("id banditry01") \
        < p.index("id hollywood4")
    assert "true of the Tufted Titmouse: " + INDEX["banditry01"]["text"] in p
    assert "question: " + BANDITRY_Q in p


def test_the_verifier_is_told_to_use_its_own_knowledge_too():
    # The reverse of the fact writer's rule, and the reason the banditry
    # case is caught: a chickadee's article may never say the word.
    assert "AND your own knowledge" in quiz.VERIFY_SYSTEM
    assert "ONLY the article" not in quiz.VERIFY_SYSTEM


# --- Reply parsing ------------------------------------------------------------

def test_parse_proposals_plain_and_fenced():
    raw = proposals(("banditry01", True, BANDITRY_Q, ["Blue Jay", "House Finch"]),
                    ("insects002", False, None, []))
    want = {"banditry01": {"distinctive": True, "question": BANDITRY_Q,
                           "candidates": ["Blue Jay", "House Finch"]},
            "insects002": {"distinctive": False, "question": "",
                           "candidates": []}}
    assert quiz.parse_proposals(raw) == want
    assert quiz.parse_proposals("```json\n" + raw + "\n```") == want
    assert quiz.parse_proposals("Here you go:\n" + raw) == want


def test_parse_proposals_malformed_and_truncated_are_none():
    raw = proposals(("banditry01", True, BANDITRY_Q, ["Blue Jay"]))
    assert quiz.parse_proposals(raw[:len(raw) // 2]) is None     # cut off
    assert quiz.parse_proposals("I can't help with that.") is None
    assert quiz.parse_proposals("") is None
    assert quiz.parse_proposals(None) is None
    assert quiz.parse_proposals('{"facts": "none"}') is None
    assert quiz.parse_proposals('["banditry01"]') is None


def test_parse_proposals_tolerates_odd_items():
    raw = json.dumps({"facts": [
        "nonsense", {"distinctive": True},                   # no id: skipped
        {"id": "a", "distinctive": "yes", "question": "Q?",
         "candidates": "Blue Jay"},                          # not a literal true
        {"id": "b", "distinctive": True, "question": " Q? ",
         "candidates": ["Blue Jay", 7, " ", None]}]})
    assert quiz.parse_proposals(raw) == {
        "a": {"distinctive": False, "question": "Q?", "candidates": []},
        "b": {"distinctive": True, "question": "Q?", "candidates": ["Blue Jay"]}}


def test_parse_verdicts_plain_fenced_malformed_and_truncated():
    raw = verdicts(("banditry01", "No", "Not a term used for jays."),
                   ("hollywood4", "unsure", ""))
    want = {"banditry01": ("no", "Not a term used for jays."),
            "hollywood4": ("unsure", "")}
    assert quiz.parse_verdicts(raw) == want
    assert quiz.parse_verdicts("```\n" + raw + "\n```") == want
    assert quiz.parse_verdicts(raw[:30]) is None
    assert quiz.parse_verdicts("no") is None
    assert quiz.parse_verdicts('{"verdicts": {"banditry01": "no"}}') is None


# --- The pure checks ----------------------------------------------------------

def test_a_plain_question_passes():
    assert quiz.check_question(BANDITRY_Q, "Tufted Titmouse",
                               ["Blue Jay", "Northern Cardinal",
                                "House Finch"]) == []


def test_the_answers_name_in_the_question_fails():
    out = quiz.check_question("Is it the tufted titmouse that travels in a "
                              "banditry?", "Tufted Titmouse", ["Blue Jay"])
    assert out == [quiz.SKIP_NAMES_ANSWER]


def test_a_name_word_every_option_shares_is_fine():
    woodpeckers = ["Hairy Woodpecker", "Pileated Woodpecker",
                   "Red-bellied Woodpecker"]
    q = "Which woodpecker is the smallest in North America?"
    assert quiz.check_question(q, "Downy Woodpecker", woodpeckers) == []


def test_a_name_word_only_some_options_share_fails():
    q = "Which woodpecker is the smallest in North America?"
    out = quiz.check_question(q, "Downy Woodpecker", [
        "Hairy Woodpecker", "Pileated Woodpecker", "White-breasted Nuthatch"])
    assert out == ["gives away a word of its answer's name (woodpecker)"]


def test_a_plural_name_word_is_still_the_word():
    # The issue's own case: "Hollywood finches" names the House Finch.
    q = "Which bird was once sold in New York as Hollywood finches?"
    assert quiz.leaked_words(q, "House Finch") == ["finch"]
    assert quiz.check_question(q, "House Finch", ["Blue Jay", "Purple Finch",
                                                  "Northern Cardinal"])         == ["gives away a word of its answer's name (finch)"]
    assert quiz.leaked_words("Which birds travel as titmice?",
                             "Tufted Titmouse") == ["titmouse"]


def test_a_hyphenated_name_is_split_and_words_are_whole():
    assert quiz.name_words("Red-tailed Hawk") == {"red", "tailed", "hawk"}
    assert quiz.name_words("Cooper's Hawk") == {"cooper", "hawk"}
    q = "Which bird's scream is dubbed over red carpets on TV?"
    assert quiz.leaked_words(q, "Red-tailed Hawk") == ["red"]
    # "red" inside another word is not the word.
    assert quiz.leaked_words("Which bird is revered and tailored for TV?",
                             "Red-tailed Hawk") == []
    # Shared by every option, a hyphenated word passes like any other.
    assert quiz.check_question(q, "Red-tailed Hawk",
                               ["Red-bellied Woodpecker"]) == []


def test_length_and_did_you_know_fail():
    long = "Which bird " + "really " * 20 + "does this?"
    assert any("longer than 140" in r for r in
               quiz.check_question(long, "Blue Jay", []))
    assert quiz.check_question("Did you know which bird does this?",
                               "Blue Jay", []) == ['begins with "Did you know"']


def wrong(*commons, reason="Not said of it."):
    return [{"species_sci": SCI[c], "species_common": c, "reason": reason}
            for c in commons]


def test_three_good_wrong_answers_pass():
    w = wrong("Blue Jay", "Northern Cardinal", "House Finch")
    assert quiz.check_wrong(w, SCI["Tufted Titmouse"], KNOWN) == []


def test_a_wrong_answer_that_is_the_answer_repeats_or_is_unknown_fails():
    w = wrong("Blue Jay", "Northern Cardinal", "House Finch")
    assert quiz.check_wrong(w + wrong("Tufted Titmouse"),
                            SCI["Tufted Titmouse"], KNOWN)         == ["Tufted Titmouse is the answer"]
    assert quiz.check_wrong(w + wrong("Blue Jay"), SCI["Tufted Titmouse"],
                            KNOWN) == ["Blue Jay appears twice"]
    stranger = [{"species_sci": "Pica pica", "species_common": "Magpie",
                 "reason": "Not it."}]
    assert quiz.check_wrong(w + stranger, SCI["Tufted Titmouse"], KNOWN)         == ["Magpie is not in the fact pool"]


def test_too_few_too_many_and_a_missing_reason_fail():
    two = wrong("Blue Jay", "Northern Cardinal")
    assert quiz.check_wrong(two, SCI["Tufted Titmouse"], KNOWN)         == [quiz.SKIP_TOO_FEW]
    nine = wrong(*[c for _, c in BIRDS[1:10]])
    assert quiz.check_wrong(nine, SCI["Tufted Titmouse"], KNOWN)         == ["more than 8 wrong answers"]
    bare = wrong("Blue Jay", "Northern Cardinal", "House Finch", reason=" ")
    assert len(quiz.check_wrong(bare, SCI["Tufted Titmouse"], KNOWN)) == 3


# --- Pass A's outcome ---------------------------------------------------------

def test_a_general_trait_is_skipped_for_good():
    e = quiz.propose_entry("insects002", INDEX["insects002"],
                           {"distinctive": False, "question": "",
                            "candidates": []}, OTHERS, MODEL, STAMP)
    assert e == {"fact_id": "insects002", "species_sci": "Baeolophus bicolor",
                 "species_common": "Tufted Titmouse", "question": None,
                 "wrong": [], "skip": "general trait", "model": MODEL,
                 "written": STAMP}
    assert not quiz.waiting(e)


def test_a_fact_the_answer_left_out_is_skipped():
    e = quiz.propose_entry("insects002", INDEX["insects002"], None, OTHERS,
                           MODEL, STAMP)
    assert e["question"] is None and e["skip"] == quiz.SKIP_NOT_JUDGED


def test_a_distinctive_fact_waits_on_pass_b_with_its_candidates():
    e = banditry(["black-capped chickadee", "Blue Jay", "Tufted Titmouse",
                  "Blue Jay", "Emperor Penguin", "Northern Cardinal"])
    assert quiz.waiting(e) and e["skip"] is None and e["wrong"] == []
    assert e["question"] == BANDITRY_Q
    # Matched by name whatever its case; the answer's own species, a repeat
    # and a bird that isn't in the fact pool never reach pass B.
    assert [c["species_common"] for c in e["candidates"]] == [
        "Black-capped Chickadee", "Blue Jay", "Northern Cardinal"]
    assert e["candidates"][0]["species_sci"] == "Poecile atricapillus"


def test_candidates_are_capped_at_eight():
    e = banditry([c for _, c in BIRDS[1:]])
    assert len(e["candidates"]) == quiz.MAX_WRONG


def test_fewer_than_three_usable_candidates_is_no_question():
    e = banditry(["Blue Jay", "Northern Cardinal", "Emperor Penguin"])
    assert e["question"] is None and e["skip"] == quiz.SKIP_TOO_FEW
    assert not quiz.waiting(e)


def test_a_question_that_names_its_answer_never_reaches_pass_b():
    e = proposed("banditry01", "Is a group of Tufted Titmouse a banditry?",
                 ["Blue Jay", "Northern Cardinal", "House Finch"])
    assert e["question"] is None and e["skip"] == quiz.SKIP_NAMES_ANSWER


def test_a_leaked_name_word_keeps_only_candidates_that_share_it():
    q = "Which woodpecker is the smallest in North America?"
    e = proposed("smallest03", q, ["Hairy Woodpecker", "Blue Jay",
                                   "Pileated Woodpecker",
                                   "Red-bellied Woodpecker"])
    assert [c["species_common"] for c in e["candidates"]] == [
        "Hairy Woodpecker", "Pileated Woodpecker", "Red-bellied Woodpecker"]
    e = proposed("hollywood4", "Which bird was sold as Hollywood finches?",
                 ["Purple Finch", "Blue Jay", "Northern Cardinal",
                  "Tufted Titmouse"])
    assert e["question"] is None
    assert e["skip"] == "gives away a word of its answer's name (finch)"


# --- Verdicts -----------------------------------------------------------------

@pytest.mark.parametrize("verdict,reason,kept", [
    ("no", "Not a term used for jays.", True),
    ("yes", "Chickadees are called a banditry too.", False),
    ("unsure", "Possibly.", False),
    ("missing", "", False),
    ("unreadable", "", False),
    ("no article", "", False),
    ("", "No verdict given.", False),
    ("no, probably", "Hedged.", False),
    ("no", "", False),                  # a kept answer always says why
])
def test_only_a_clear_no_keeps_a_candidate(verdict, reason, kept):
    assert quiz.keeps(verdict, reason) is kept


def test_the_banditry_question_ships_without_the_chickadee():
    e = settle(banditry(), chickadee=(
        "yes", "Chickadees are commonly called a banditry too."))
    assert e["question"] == BANDITRY_Q and e["skip"] is None
    assert not quiz.waiting(e) and e["written"] == STAMP
    assert [w["species_common"] for w in e["wrong"]] == [
        "White-breasted Nuthatch", "Blue Jay", "Northern Cardinal"]
    assert all(w["reason"] for w in e["wrong"])
    assert quiz.shipped_problems({"questions": [e]}, POOL) == []


def test_the_banditry_question_with_fewer_than_three_left_does_not_ship():
    e = settle(banditry(), chickadee=("yes", "Called a banditry too."),
               nuthatch=("unsure", "Mixed flocks with titmice."))
    assert e["question"] is None and e["wrong"] == []
    assert e["skip"] == quiz.SKIP_TOO_FEW and not quiz.waiting(e)


def test_an_entry_waits_until_its_last_candidate_is_resolved():
    e = banditry()
    quiz.resolve(e, SCI["Blue Jay"], "no", "Not said of jays.", KNOWN, STAMP)
    assert quiz.waiting(e) and len(e["candidates"]) == 3
    assert [w["species_common"] for w in e["wrong"]] == ["Blue Jay"]
    # A bird that was never a candidate changes nothing.
    quiz.resolve(e, SCI["House Finch"], "no", "Not it.", KNOWN, STAMP)
    assert len(e["candidates"]) == 3 and len(e["wrong"]) == 1


def test_claims_are_grouped_by_candidate():
    a = banditry()
    b = proposed("hollywood4", "Which bird was once sold as a Hollywood bird?",
                 ["Blue Jay", "Northern Cardinal", "Tufted Titmouse"])
    done = settle(proposed("smallest03", "Which bird is the smallest of its "
                           "family in North America?",
                           ["Blue Jay", "House Finch", "Northern Cardinal"]))
    groups = quiz.claims_by_candidate({"questions": [a, b, done]})
    assert set(groups) == {SCI[c] for c in (
        "Black-capped Chickadee", "White-breasted Nuthatch", "Blue Jay",
        "Northern Cardinal", "Tufted Titmouse")}
    common, entries = groups[SCI["Blue Jay"]]
    assert common == "Blue Jay"
    assert [e["fact_id"] for e in entries] == ["banditry01", "hollywood4"]
    assert [e["fact_id"] for e in groups[SCI["Tufted Titmouse"]][1]]         == ["hollywood4"]


# --- The two passes, with a stand-in writer -----------------------------------

def titmouse():
    return POOL["species"][0], POOL["species"][0]["facts"]


def test_pass_a_writes_one_entry_per_fact_in_one_call():
    writer = Writer(proposals(
        ("banditry01", True, BANDITRY_Q,
         ["Black-capped Chickadee", "Blue Jay", "Northern Cardinal"]),
        ("insects002", False, None, [])))
    species, facts = titmouse()
    entries = quiz.propose_for(species, facts, OTHERS, writer, today=TODAY)
    assert len(writer.calls) == 1
    assert writer.calls[0][0] == quiz.PROPOSE_SYSTEM
    assert [e["fact_id"] for e in entries] == ["banditry01", "insects002"]
    assert quiz.waiting(entries[0]) and entries[0]["model"] == MODEL
    assert entries[1]["skip"] == "general trait"
    assert entries[1]["written"] == STAMP


def test_pass_a_with_a_silent_gateway_is_transient():
    species, facts = titmouse()
    assert quiz.propose_for(species, facts, OTHERS, Writer(None)) is None


def test_pass_a_with_an_unshaped_answer_is_recorded_as_skipped():
    species, facts = titmouse()
    entries = quiz.propose_for(species, facts, OTHERS, Writer("Sure!"),
                               today=TODAY)
    assert [e["skip"] for e in entries] == [quiz.SKIP_UNREADABLE] * 2


def test_pass_b_reads_the_candidates_article_once_per_call():
    a = banditry()
    b = proposed("hollywood4", "Which bird was once sold as a Hollywood bird?",
                 ["Black-capped Chickadee", "Blue Jay", "Northern Cardinal"])
    writer = Writer(verdicts(
        ("banditry01", "yes", "Chickadees are called a banditry too."),
        ("hollywood4", "no", "Never sold as a cage bird under that name.")))
    fetched = []

    def fetch(url):
        fetched.append(url)
        return summary()

    out = quiz.verify_candidate(SCI["Black-capped Chickadee"],
                                "Black-capped Chickadee", [a, b], INDEX,
                                writer, fetch_json=fetch)
    assert fetched == [species_profile.summary_url("Poecile atricapillus")]
    assert len(writer.calls) == 1
    system, prompt = writer.calls[0]
    assert system == quiz.VERIFY_SYSTEM
    assert "It caches seeds for the winter." in prompt
    assert "A book." not in prompt                 # the back matter is cut
    assert "id banditry01" in prompt and "id hollywood4" in prompt
    assert out == {
        "banditry01": ("yes", "Chickadees are called a banditry too."),
        "hollywood4": ("no", "Never sold as a cage bird under that name.")}


def test_pass_b_drops_a_claim_the_answer_left_out_or_garbled():
    a = banditry()
    fetch = lambda url: summary()
    out = quiz.verify_candidate(SCI["Blue Jay"], "Blue Jay", [a], INDEX,
                                Writer(verdicts()), fetch_json=fetch)
    assert out == {"banditry01": ("missing", "")}
    out = quiz.verify_candidate(SCI["Blue Jay"], "Blue Jay", [a], INDEX,
                                Writer("no idea"), fetch_json=fetch)
    assert out == {"banditry01": ("unreadable", "")}
    assert not quiz.keeps(*out["banditry01"])


def test_pass_b_with_no_article_drops_the_candidate_without_a_model_call():
    writer = Writer("unused")
    out = quiz.verify_candidate(
        SCI["Blue Jay"], "Blue Jay", [banditry()], INDEX, writer,
        fetch_json=lambda url: {"query": {"pages": [{"missing": True}]}})
    assert out == {"banditry01": ("no article", "")} and writer.calls == []


def test_pass_b_with_a_silent_gateway_writes_nothing_final():
    e = banditry()
    before = json.dumps(e)
    out = quiz.verify_candidate(SCI["Blue Jay"], "Blue Jay", [e], INDEX,
                                Writer(None), fetch_json=lambda url: summary())
    assert out == {}                       # nothing to resolve: still waiting
    assert json.dumps(e) == before and quiz.waiting(e)


def test_pass_b_splits_a_popular_candidate_and_keeps_what_was_answered(
        monkeypatch):
    monkeypatch.setattr(quiz, "CLAIMS_PER_CALL", 1)
    a = banditry()
    b = proposed("hollywood4", "Which bird was once sold as a Hollywood bird?",
                 ["Blue Jay", "Northern Cardinal", "Tufted Titmouse"])
    writer = Writer(verdicts(("banditry01", "no", "Not said of jays.")), None)
    out = quiz.verify_candidate(SCI["Blue Jay"], "Blue Jay", [a, b], INDEX,
                                writer, fetch_json=lambda url: summary())
    assert len(writer.calls) == 2
    assert out == {"banditry01": ("no", "Not said of jays.")}


# --- Merge and idempotency ----------------------------------------------------

def judged_pool():
    """Every fact judged: one question, one hand-nulled, two skipped."""
    q = quiz.empty_quiz()
    quiz.put_entry(q, settle(banditry(), chickadee=(
        "yes", "Chickadees are commonly called a banditry too.")))
    nulled = settle(proposed("smallest03", "Which bird is the smallest of "
                             "its family in North America?",
                             ["Blue Jay", "House Finch", "Northern Cardinal"]))
    nulled["question"] = None              # Todd's review: nulled, not deleted
    quiz.put_entry(q, nulled)
    for fid in ("insects002", "hollywood4"):
        quiz.put_entry(q, quiz.propose_entry(
            fid, INDEX[fid], {"distinctive": False, "question": "",
                              "candidates": []}, OTHERS, MODEL, STAMP))
    return q


def test_a_plain_run_judges_only_facts_with_no_entry():
    assert [(sp["species_common"], [f["id"] for f in facts])
            for sp, facts in quiz.todo(POOL, quiz.empty_quiz())] == [
        ("Tufted Titmouse", ["banditry01", "insects002"]),
        ("Downy Woodpecker", ["smallest03"]),
        ("House Finch", ["hollywood4"])]
    q = quiz.empty_quiz()
    quiz.put_entry(q, banditry())          # still waiting counts as judged
    assert [[f["id"] for f in facts] for _, facts in quiz.todo(POOL, q)] == [
        ["insects002"], ["smallest03"], ["hollywood4"]]


def test_a_second_run_has_nothing_to_do():
    # Skipped facts stay skipped and a question nulled by hand stays nulled:
    # each still has its entry, so nothing is sent to the model again.
    q = judged_pool()
    assert quiz.todo(POOL, q) == []
    assert quiz.claims_by_candidate(q) == {}


def test_a_deleted_entry_is_judged_again():
    q = judged_pool()
    q["questions"] = [e for e in q["questions"]
                      if e["fact_id"] != "smallest03"]
    assert [[f["id"] for f in facts] for _, facts in quiz.todo(POOL, q)]         == [["smallest03"]]


def test_refresh_rejudges_one_species():
    q = quiz.drop_species(judged_pool(), "Baeolophus bicolor")
    assert {e["fact_id"] for e in q["questions"]} == {"smallest03",
                                                      "hollywood4"}
    work = quiz.todo(POOL, q, "Baeolophus bicolor")
    assert [(sp["species_sci"], [f["id"] for f in facts])
            for sp, facts in work] == [
        ("Baeolophus bicolor", ["banditry01", "insects002"])]


def test_an_entry_whose_fact_is_gone_is_pruned():
    q = judged_pool()
    smaller = quiz.fact_index(fact_pool(
        {k: v for k, v in FACTS.items() if k != "Haemorhous mexicanus"}))
    gone = quiz.prune(q, smaller)
    assert [e["fact_id"] for e in gone] == ["hollywood4"]
    assert "hollywood4" not in {e["fact_id"] for e in q["questions"]}
    assert len(q["questions"]) == 3 and quiz.prune(q, smaller) == []


def test_put_entry_replaces_one_fact_and_leaves_the_rest():
    q = judged_pool()
    quiz.put_entry(q, banditry())
    assert len(q["questions"]) == 4
    assert quiz.waiting(next(e for e in q["questions"]
                             if e["fact_id"] == "banditry01"))


# --- The stable dump ----------------------------------------------------------

def test_dump_quiz_is_sorted_stable_and_round_trips(tmp_path):
    q = judged_pool()
    text = quiz.dump_quiz(q)
    assert text.endswith("]\n}\n") and "\r" not in text
    assert '\\"banditry\\"' in text and "\\u" not in text
    keys = [(e["species_sci"], e["fact_id"])
            for e in json.loads(text)["questions"]]
    assert keys == sorted(keys)
    q["questions"].reverse()
    assert quiz.dump_quiz(q) == text       # order in memory never shows
    path = str(tmp_path / "quiz.json")
    quiz.write_quiz(path, q)
    assert quiz.dump_quiz(quiz.load_quiz(path)) == text
    with open(path, encoding="utf-8") as f:
        assert f.read() == text


def test_load_quiz_of_nothing_is_empty(tmp_path):
    assert quiz.load_quiz(str(tmp_path / "absent.json")) == quiz.empty_quiz()


# --- The bill -----------------------------------------------------------------

def test_the_meter_reads_the_gateways_own_usage_line(monkeypatch, capsys):
    # gateway_client prints usage and returns only text; the meter depends
    # on that line's words, so a change to it fails here, not on orchid.
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return json.dumps({
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 1500, "output_tokens": 900}}
            ).encode("utf-8")

    monkeypatch.setattr(gateway_client.urllib.request, "urlopen",
                        lambda req, timeout=None: Resp())
    meter = quiz.Metered(gateway_client.Gateway("key", MODEL))
    assert meter.complete("system", "prompt") == "ok"
    assert meter.complete("system", "prompt") == "ok"
    assert (meter.calls, meter.tokens_in, meter.tokens_out) == (2, 3000, 1800)
    assert meter.spend() == (
        f"2 model calls, 3000 tokens in, 1800 out ({MODEL})")
    assert "1500 tokens in, 900 out" in capsys.readouterr().out   # still shown


# --- The shipped file ---------------------------------------------------------

def test_shipped_problems_passes_a_clean_pool():
    assert quiz.shipped_problems(judged_pool(), POOL) == []


def test_shipped_problems_catches_what_a_hand_edit_can_break():
    def broken(change):
        q = judged_pool()
        change(next(e for e in q["questions"]
                    if e["fact_id"] == "banditry01"))
        return quiz.shipped_problems(q, POOL)

    assert broken(lambda e: e["wrong"].pop())         == ["banditry01: fewer than three wrong answers"]
    assert broken(lambda e: e.update(
        question="A group of which titmouse is a banditry?"))         == ["banditry01: gives away a word of its answer's name (titmouse)"]
    assert broken(lambda e: e.update(
        question="Is a group of tufted titmice or a Tufted Titmouse a "
                 "banditry?")) == ["banditry01: names its answer"]
    assert broken(lambda e: e.update(candidates=[]))         == ["banditry01: still waiting on pass B"]
    assert broken(lambda e: e.update(fact_id="nosuchfact"))         == ["nosuchfact: its fact is not in the fact pool"]
    assert broken(lambda e: e.update(species_sci="Cyanocitta cristata"))         == ["banditry01: its answer is not its fact's species"]
    assert broken(lambda e: e["wrong"].append(dict(
        species_sci="Poecile atricapillus",
        species_common="Black-capped Chickadee", reason="")))         == ["banditry01: Black-capped Chickadee has no reason"]
    assert broken(lambda e: e.update(fact_id="insects002"))         == ["insects002: more than one entry"]


def test_the_shipped_file_check_reads_the_files_and_knows_absence(tmp_path):
    facts_path = str(tmp_path / "fun-facts.json")
    quiz_path = str(tmp_path / "quiz.json")
    with open(facts_path, "w", encoding="utf-8") as f:
        json.dump(POOL, f)
    assert quiz.shipped_file_problems(quiz_path, facts_path) is None
    quiz.write_quiz(quiz_path, judged_pool())
    assert quiz.shipped_file_problems(quiz_path, facts_path) == []
    q = judged_pool()
    quiz.put_entry(q, banditry())
    quiz.write_quiz(quiz_path, q)
    assert quiz.shipped_file_problems(quiz_path, facts_path)         == ["banditry01: still waiting on pass B"]


def test_the_shipped_quiz_passes_the_generators_checks():
    """web/data/quiz.json beside web/data/fun-facts.json, as committed. The
    first pool lands in its own PR (#715), so until then there is nothing
    to check and this skips."""
    problems = quiz.shipped_file_problems()
    if problems is None:
        pytest.skip(f"no {quiz.SHIPPED_POOL} yet")
    assert problems == []
