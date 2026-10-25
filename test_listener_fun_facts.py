# =============================================================================
# project-squirrel -- test_listener_fun_facts.py
#
# The fun-fact pool's guarantees (issue #422): only facts whose quote is
# really in the article survive, with no figure or absolute the quote
# doesn't hold (the Chimney Swift's "can only perch" is the case the
# absolute rule exists for); a plain run writes only species with no entry,
# so a second run spends nothing and facts deleted by hand stay deleted; a
# transient failure is never recorded as a definitive empty; and the file
# is written in a stable, diffable shape. No test reaches Wikipedia or the
# gateway.
# =============================================================================

import datetime
import json

import pytest

from listener import fun_facts, species_profile

ARTICLE = (
    "The northern cardinal is a bird in the genus Cardinalis.\n\n"
    "== Behavior ==\n"
    "During courtship, the male feeds seed to the female beak-to-beak. "
    "The clutch typically contains three to four eggs. "
    "It was once prized as a pet, but its sale was banned in the United "
    "States by the Migratory Bird Treaty Act of 1918.\n\n"
    "== References ==\n"
    "1. Somebody (2001). A book.\n"
)

SWIFT = ("Chimney swifts cannot perch; they cling to vertical surfaces "
         "with their small feet.")


@pytest.fixture(autouse=True)
def no_wikipedia(monkeypatch):
    """No test may reach Wikipedia, including by forgetting to inject."""
    def refuse(url):
        raise AssertionError(f"test tried to fetch {url}")
    monkeypatch.setattr(species_profile, "get_json", refuse)


def summary(extract=ARTICLE, title="Northern cardinal", **page):
    return {"query": {"pages": [{"title": title, "extract": extract, **page}]}}


class Writer:
    """A stand-in gateway: hands back a canned reply and records prompts."""
    model = "anthropic/claude-sonnet-5.5"

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def complete(self, system, prompt, *a, **kw):
        self.calls.append((system, prompt))
        return self.reply


def reply(*pairs):
    return json.dumps({"facts": [{"fact": f, "quote": q} for f, q in pairs]})


# --- The article -------------------------------------------------------------

def test_article_from_reads_the_full_extract():
    assert fun_facts.article_from(summary()) == ("Northern cardinal", ARTICLE.strip())


def test_article_from_missing_disambiguation_and_empty_are_nothing():
    assert fun_facts.article_from({"query": {"pages": [{"missing": True}]}}) == (None, None)
    assert fun_facts.article_from(
        summary(pageprops={"disambiguation": ""})) == (None, None)
    assert fun_facts.article_from(summary(extract="   ")) == (None, None)
    assert fun_facts.article_from(None) == (None, None)


def test_trim_article_drops_the_back_matter():
    out = fun_facts.trim_article(ARTICLE)
    assert "beak-to-beak" in out
    assert "References" not in out and "A book" not in out


def test_trim_article_caps_at_a_paragraph_boundary():
    text = "\n".join(f"Paragraph {i} " + "x" * 50 for i in range(10))
    out = fun_facts.trim_article(text, limit=200)
    assert len(out) <= 200
    assert out.endswith("x")          # whole paragraphs, never a cut word


def test_build_prompt_names_the_bird_and_carries_the_article():
    p = fun_facts.build_prompt("Northern Cardinal", "Cardinalis cardinalis", "TEXT")
    assert "Northern Cardinal (Cardinalis cardinalis)" in p
    assert "<<<\nTEXT\n>>>" in p


# --- The reply ---------------------------------------------------------------

def test_parse_reply_plain_and_fenced():
    body = reply(("A fact.", "A quote."))
    assert fun_facts.parse_reply(body) == [("A fact.", "A quote.")]
    assert fun_facts.parse_reply(f"```json\n{body}\n```") == [("A fact.", "A quote.")]


def test_parse_reply_skips_half_items_and_rejects_other_shapes():
    body = json.dumps({"facts": [{"fact": "A fact."}, {"quote": "Q."},
                                 "loose", {"fact": "B.", "quote": "C."}]})
    assert fun_facts.parse_reply(body) == [("B.", "C.")]
    assert fun_facts.parse_reply("I could not find any facts.") is None
    assert fun_facts.parse_reply('{"facts": "none"}') is None
    assert fun_facts.parse_reply('{"facts": [') is None
    assert fun_facts.parse_reply(json.dumps({"facts": []})) == []


# --- The check ---------------------------------------------------------------

def test_a_faithful_fact_passes():
    fact = "Male cardinals court by feeding the female seed, beak-to-beak."
    quote = "During courtship, the male feeds seed to the female beak-to-beak."
    assert fun_facts.check_fact(fact, quote, ARTICLE) == []


def test_a_quote_the_article_never_says_fails():
    out = fun_facts.check_fact("Cardinals mate for life.",
                               "Northern cardinals mate for life.", ARTICLE)
    assert out == ["its quote is not in the article"]


def test_quote_matching_forgives_typography_case_and_ellipsis():
    retyped = "during courtship,  the male feeds seed\nto the female beak-to-beak."
    assert fun_facts.quote_found(retyped, ARTICLE)
    elided = "It was once prized as a pet ... by the Migratory Bird Treaty Act of 1918."
    assert fun_facts.quote_found(elided, ARTICLE)
    spliced = "It was once prized as a pet ... by the Endangered Species Act."
    assert not fun_facts.quote_found(spliced, ARTICLE)
    apostrophe = fun_facts.normalize("the cardinal’s clutch")
    assert apostrophe == "the cardinal's clutch"


def test_a_figure_the_quote_lacks_fails_and_a_spelled_one_counts():
    quote = "It was once prized as a pet, but its sale was banned in the United States by the Migratory Bird Treaty Act of 1918."
    ok = "Cardinals were popular pets until a 1918 law banned their sale in the U.S."
    assert fun_facts.check_fact(ok, quote, ARTICLE) == []
    bad = "Cardinals were popular pets until a 1916 law banned their sale."
    assert fun_facts.check_fact(bad, quote, ARTICLE) == [
        "figures its quote doesn't hold: 1916"]
    eggs = "The clutch typically contains three to four eggs."
    spelled = "A cardinal nest holds about thirty-four eggs."
    assert fun_facts.check_fact(spelled, eggs, ARTICLE) == [
        "figures its quote doesn't hold: 34"]


def test_the_swift_absolute_is_caught():
    """The near-miss #422 was rewritten around: 'can only perch' from a
    quote that says they cannot perch at all."""
    out = fun_facts.check_fact(
        "Chimney Swifts can only perch on horizontal surfaces.", SWIFT, SWIFT)
    assert out == ["absolutes its quote doesn't say: only"]


def test_the_birds_own_name_is_never_an_absolute():
    """The first pool run dropped six Least Sandpiper and Least Flycatcher
    facts for saying "least"; the name's words are the bird, not a claim."""
    quote = "The female may leave before the chicks can fly."
    fact = "The female least sandpiper may leave before the chicks can fly."
    assert fun_facts.check_fact(fact, quote, quote) == [
        "absolutes its quote doesn't say: least"]
    assert fun_facts.check_fact(fact, quote, quote, name="Least Sandpiper") == []
    # The name excuses only its own words, never a real absolute.
    only = "The female least sandpiper is the only parent left at the nest."
    assert fun_facts.check_fact(only, quote, quote, name="Least Sandpiper") == [
        "absolutes its quote doesn't say: only"]


def test_an_absolute_the_quote_says_is_fine():
    article = "It is the only bird in the family that migrates."
    assert fun_facts.check_fact(
        "It's the only one in its family that migrates.", article, article) == []


def test_length_and_did_you_know_fail():
    quote = "During courtship, the male feeds seed to the female beak-to-beak."
    long = "Did you know " + "the male feeds the female beak-to-beak " * 4
    out = fun_facts.check_fact(long.strip(), quote, ARTICLE)
    assert any(o.startswith("longer than 140") for o in out)
    assert 'begins with "Did you know"' in out


def test_fact_id_is_stable_and_ignores_typography():
    a = fun_facts.fact_id("The cardinal’s song.")
    assert a == fun_facts.fact_id("the cardinal's  song.")
    assert len(a) == 10 and a != fun_facts.fact_id("A different fact.")


def test_select_facts_keeps_passes_drops_repeats_and_caps():
    q = "During courtship, the male feeds seed to the female beak-to-beak."
    eggs = "The clutch typically contains three to four eggs."
    pets = ("It was once prized as a pet, but its sale was banned in the "
            "United States by the Migratory Bird Treaty Act of 1918.")
    genus = "The northern cardinal is a bird in the genus Cardinalis."
    pairs = [
        ("The male feeds the female seed beak-to-beak.", q),
        ("The male feeds seed to the female beak-to-beak.", q),   # repeat
        ("Cardinals mate for life.", "Cardinals mate for life."),  # unquoted
        ("A cardinal clutch usually holds three or four eggs.", eggs),
        ("Cardinals were once kept as pets until a 1918 law ended it.", pets),
        ("The northern cardinal belongs to the genus Cardinalis.", genus),
        ("Courtship includes a seed gift passed between partners.", q),
        ("Selling cardinals became illegal in the United States.", pets),
    ]
    kept, rejected = fun_facts.select_facts(pairs, ARTICLE)
    assert len(kept) == fun_facts.MAX_FACTS
    assert kept[0]["text"] == "The male feeds the female seed beak-to-beak."
    assert all(len(k["id"]) == 10 and k["quote"] for k in kept)
    reasons = {text: r for text, r in rejected}
    assert reasons["The male feeds seed to the female beak-to-beak."] == [
        "repeats an earlier fact"]
    assert reasons["Cardinals mate for life."] == [
        "its quote is not in the article"]
    assert any(r == ["past the 5-fact cap"] for r in reasons.values())


# --- Which species, and which run writes them ----------------------------------

def test_species_union_dedupes_and_keeps_the_first_common_name():
    house = [("Cardinalis cardinalis", "Northern Cardinal")]
    birdhouse = [("Cardinalis cardinalis", "Cardinal"),
                 ("Sitta carolinensis", "White-breasted Nuthatch")]
    kestrel = [("Sitta carolinensis", None), ("Poecile atricapillus", "Black-capped Chickadee")]
    out = fun_facts.species_union(house, [birdhouse, kestrel],
                                  ["Chaetura pelagica", "Cardinalis cardinalis"])
    assert out == [("Cardinalis cardinalis", "Northern Cardinal"),
                   ("Sitta carolinensis", "White-breasted Nuthatch"),
                   ("Poecile atricapillus", "Black-capped Chickadee"),
                   ("Chaetura pelagica", None)]


def test_a_plain_run_writes_only_species_without_an_entry():
    pool = fun_facts.empty_pool()
    fun_facts.put_entry(pool, {"species_sci": "A a", "facts": []})
    fun_facts.put_entry(pool, {"species_sci": "B b", "facts": [{"id": "x"}]})
    union = [("A a", "Aa"), ("B b", "Bb"), ("C c", "Cc")]
    assert fun_facts.todo(union, pool) == [("C c", "Cc")]


def test_refresh_names_one_species_whatever_the_pool_holds():
    pool = fun_facts.empty_pool()
    fun_facts.put_entry(pool, {"species_sci": "B b", "species_common": "Bee",
                               "facts": []})
    assert fun_facts.todo([("A a", "Aa")], pool, refresh="B b") == [("B b", "Bee")]
    assert fun_facts.todo([("A a", "Aa")], pool, refresh="A a") == [("A a", "Aa")]


def test_put_entry_replaces_one_and_keeps_hand_deletions_elsewhere():
    pool = fun_facts.empty_pool()
    fun_facts.put_entry(pool, {"species_sci": "B b", "facts": [{"id": "1"}]})
    fun_facts.put_entry(pool, {"species_sci": "A a", "facts": []})  # pruned by hand
    fun_facts.put_entry(pool, {"species_sci": "B b", "facts": [{"id": "2"}]})
    assert [e["species_sci"] for e in pool["species"]] == ["A a", "B b"]
    assert pool["species"][1]["facts"] == [{"id": "2"}]
    assert pool["species"][0]["facts"] == []


def test_dump_pool_is_stable_and_round_trips(tmp_path):
    pool = {"version": 1, "species": [
        {"species_sci": "Z z", "facts": [{"id": "1", "text": "Café bird.", "quote": "q"}]},
        {"species_sci": "A a", "facts": []}]}
    text = fun_facts.dump_pool(pool)
    assert text.endswith("\n") and "Café" in text
    assert text.index('"A a"') < text.index('"Z z"')
    path = str(tmp_path / "pool.json")
    fun_facts.write_pool(path, pool)
    assert fun_facts.load_pool(path)["species"][0]["species_sci"] == "A a"
    assert fun_facts.dump_pool(fun_facts.load_pool(path)) == text


def test_load_pool_of_nothing_is_empty(tmp_path):
    assert fun_facts.load_pool(str(tmp_path / "absent.json")) == fun_facts.empty_pool()


def test_house_life_list_reads_read_only_and_tolerates_absence(tmp_path):
    import sqlite3
    path = str(tmp_path / "earl.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE life_list (species_sci TEXT, species_common TEXT)")
    c.execute("INSERT INTO life_list VALUES ('Cardinalis cardinalis', 'Northern Cardinal')")
    c.commit()
    c.close()
    assert fun_facts.house_life_list(path) == [("Cardinalis cardinalis", "Northern Cardinal")]
    assert fun_facts.house_life_list(str(tmp_path / "none.db")) == []


# --- One species, end to end ---------------------------------------------------

DAY = datetime.date(2026, 10, 25)


def test_facts_for_writes_checked_facts_with_provenance():
    q = "During courtship, the male feeds seed to the female beak-to-beak."
    w = Writer(reply(("The male feeds the female seed beak-to-beak.", q),
                     ("Cardinals mate for life.", "Cardinals mate for life.")))
    entry, rejected = fun_facts.facts_for(
        "Cardinalis cardinalis", "Northern Cardinal", w,
        fetch_json=lambda url: summary(), today=DAY)
    assert entry["species_common"] == "Northern Cardinal"
    assert entry["article"] == "Northern cardinal"
    assert entry["fetched"] == "2026-10-25"
    assert entry["model"] == "anthropic/claude-sonnet-5.5"
    assert [f["text"] for f in entry["facts"]] == [
        "The male feeds the female seed beak-to-beak."]
    assert rejected == [("Cardinals mate for life.", ["its quote is not in the article"])]
    system, prompt = w.calls[0]
    assert system == fun_facts.SYSTEM
    assert "beak-to-beak" in prompt and "A book" not in prompt   # trimmed


def test_no_page_is_a_definitive_empty_without_a_model_call():
    w = Writer(reply())
    entry, rejected = fun_facts.facts_for(
        "Nobody nobody", None, w,
        fetch_json=lambda url: {"query": {"pages": [{"missing": True}]}},
        today=DAY)
    assert entry["facts"] == [] and entry["article"] is None
    assert entry["species_common"] == "Nobody nobody"
    assert w.calls == [] and rejected == []


def test_a_silent_writer_is_transient_not_recorded():
    assert fun_facts.facts_for(
        "Cardinalis cardinalis", "Northern Cardinal", Writer(None),
        fetch_json=lambda url: summary(), today=DAY) is None


def test_an_unshaped_answer_is_recorded_with_no_facts():
    entry, _ = fun_facts.facts_for(
        "Cardinalis cardinalis", "Northern Cardinal", Writer("Sorry, no."),
        fetch_json=lambda url: summary(), today=DAY)
    assert entry["facts"] == [] and entry["article"] == "Northern cardinal"


def test_a_hand_added_species_takes_the_article_title_as_its_name():
    q = "During courtship, the male feeds seed to the female beak-to-beak."
    entry, _ = fun_facts.facts_for(
        "Cardinalis cardinalis", None,
        Writer(reply(("The male feeds the female seed beak-to-beak.", q))),
        fetch_json=lambda url: summary(), today=DAY)
    assert entry["species_common"] == "Northern cardinal"
