# =============================================================================
# project-squirrel -- test_listener_notes_validate.py
#
# The check between the model and the store (issue #612). Every case here
# is a sentence the model actually wrote into earl.db on orchid before the
# validator existed, or the clean counterpart that must still pass -- a
# validator that rejects good prose would turn every note into the plain
# record, which is a failure of its own.
# =============================================================================

import pytest

from listener import notes_validate as nv

FACTS = (
    "Busiest stretch of the day: 7am to 10am, holding 1,234 visits "
    "(35% of all of them).\n"
    "Total visits on record: 3,420.\n"
    "First heard: July 18, 2026.\n"
    "Last 7 days: 93 visits, against 126 in the 7 days before that.")


def test_a_clean_note_passes():
    text = ("Your cardinals are morning birds: 35% of their 3,420 visits land "
            "between 7am and 10am. Lately they have eased off a little, 93 "
            "visits in the last 7 days against 126 the week before.")
    assert nv.validate(text, FACTS) == []


def test_figures_compare_without_commas():
    assert nv.figures("3,420 visits and 1234 more, at 0.82.") == \
        {"3420", "1234", "0.82"}
    # A trailing comma after a number is punctuation, not a figure.
    assert nv.figures("July 18, 2026") == {"18", "2026"}
    # A whole hour on a clock face is the hour, not a figure "00".
    assert nv.figures("at 10:00pm") == {"10"}
    assert nv.figures("at 10:30pm") == {"10", "30"}
    assert nv.validate("Heard 3420 times since 2026.", FACTS) == []


def test_an_invented_figure_is_named():
    problems = nv.validate("Heard on 14 days, with 35% in the morning.", FACTS)
    assert len(problems) == 1
    assert "14" in problems[0] and "35" not in problems[0]


def test_inputs_may_be_a_list_and_the_background_counts():
    # The Wikipedia background rides the prompt, so its figures are given.
    background = "Its sale was banned by the Migratory Bird Treaty Act of 1918."
    text = "Protected since 1918, and heard 3,420 times here."
    assert nv.validate(text, [FACTS, background]) == []
    assert nv.validate(text, FACTS) != []


@pytest.mark.parametrize("leak", [
    "'Evidence: solid' suggests this pattern of activity is reliable.",
    "evidence: THIN, so take it lightly.",
    "Their visits were MORE OFTEN on clear days.",
    "They came LESS OFTEN in the rain.",
    "Rain is ABOUT AVERAGE for them.",
    "The SKY clear figure stands out.",
])
def test_annotation_labels_never_reach_the_page(leak):
    assert nv.validate(leak, FACTS)


def test_ordinary_words_that_share_a_label_are_fine():
    # The labels are matched in their uppercase, label form only.
    text = "The evidence here is thin, and they came more often in sun."
    assert nv.validate(text, FACTS) == []


def test_an_unknown_bucket_is_not_a_fact_about_a_bird():
    # Verbatim from the American Robin's note, 2026-10-24.
    text = ("Interestingly, the microphone hasn't registered a single visit on "
            "days where the temperature was \"unknown,\" which is a curious "
            "result.")
    assert any("unknown" in p for p in nv.validate(text, FACTS))


@pytest.mark.parametrize("text", [
    "Our microphone heard it at dawn.",
    "It visits my yard at dawn.",
    "Our own weather station agrees.",
])
def test_the_yard_is_the_readers(text):
    assert nv.validate(text, FACTS)


def test_your_is_not_our():
    assert nv.validate("Your microphones hear it at dawn.", FACTS) == []


def test_driveway_only_where_a_place_says_so():
    text = "It favours the back of the house, over the driveway."
    assert nv.validate(text, FACTS)                       # a station: never
    assert nv.validate(text, FACTS, allow_driveway=True) == []


def test_the_stock_opener_is_caught_but_only_as_an_opener():
    # The formula opening nearly every note before #612.
    opener = ("Your microphones have been picking up cardinals since July. "
              "They like mornings.")
    assert any("opens" in p for p in nv.validate(opener, FACTS))
    # The same verb deep in the note is just a verb.
    later = ("Cardinals are morning birds. The front microphone picked up "
             "the most of them.")
    assert nv.validate(later, FACTS) == []
    assert nv.validate("They make a good number of calls.", FACTS)


def test_numbers_written_as_words_are_held_to_the_figure_rule():
    # Verbatim from the Barred Owl's first #612 desk run: invented, caught.
    text = ("You've recorded seventy-three visits, sixty-three percent of "
            "them late at night.")
    problems = nv.validate(text, FACTS)
    assert len(problems) == 1 and "63, 73" in problems[0]
    # #614's desk run: a GIVEN figure copied as words is faithful, not
    # invented -- rejecting it threw away 8 of 12 accurate sketches.
    given = "Busiest from 7am to 10am, holding 1,234 visits (35% of them)."
    assert nv.validate("Thirty-five percent of its visits come early.",
                       given) == []
    assert nv.validate("It sang one thousand two hundred and thirty-four "
                       "times.", given) == []
    # One to ten alone are ordinary English, not hidden figures.
    assert nv.validate("Heard from two microphones over seven days.",
                       FACTS) == []


def test_words_value():
    assert nv.words_value("seventy-three") == 73
    assert nv.words_value("one hundred and twelve") == 112
    assert nv.words_value("eighteen") == 18
    assert nv.words_value("twenty one") == 21
    assert nv.words_value("two") is None             # English, not a figure
    assert nv.words_value("ten") is None
    assert nv.figures("a thousand visits") == {"1000"}
    assert nv.figures("eighteen minutes before sunrise") == {"18"}
    assert nv.figures("two paragraphs") == set()


def test_the_first_sentence_must_carry_the_lede():
    lede = ("Busiest stretch of the day: 7am to 10am, holding 1,234 visits "
            "(35% of all of them).")
    # All four notes of the first desk run opened on the total instead.
    off = ("Your yard has seen 3,420 visits since July 18, 2026. Most land "
           "between 7am and 10am.")
    problems = nv.validate(off, FACTS, lede=lede)
    assert any("first sentence" in p for p in problems)
    on = "Mornings are theirs: 35% of visits land between 7am and 10am."
    assert nv.validate(on, FACTS, lede=lede) == []
    # A lede with no figures can't be checked this way, so it isn't.
    assert nv.validate(off, FACTS, lede="A quiet bird.") == []


def test_a_month_needs_to_have_been_given():
    # Verbatim from the #614 desk run, for a swift first heard July 18.
    swift = "It's been visiting your yard since January."
    given = "First heard July 18, 2026; last heard in early September."
    problems = nv.validate(swift, given)
    assert problems == ["it names months that were not given: January -- "
                        "use only the dates given"]
    assert nv.validate("Here since July, gone by September.", given) == []
    # "may" the verb is not the month (May is left out of the month list).
    assert nv.validate("It may return.", given) == []


def test_a_place_needs_a_fact_that_names_it():
    terms = ["back of the house", "driveway", "back of the garage", "garage",
             "front door"]
    # Verbatim from the #614 desk run, for an owl heard only behind the
    # garage: the place is an invention.
    owl = ("The back of the house, over the driveway, seems to pick up the "
           "most calls.")
    facts = "Heard almost only at the old microphone at the back of the garage."
    problems = nv.validate(owl, owl, True, place_terms=terms, facts=facts)
    assert any("back of the house, driveway" in p for p in problems)
    # Named by a fact: fine.
    ok = "It lives behind the garage, at the back of the garage."
    assert nv.validate(ok, facts, True, place_terms=terms, facts=facts) == []
    # Word-bounded: "doorstep" is not the front door.
    assert nv.unfounded_places("a doorstep song", terms, "") == []


def test_an_empty_draft_has_nothing_to_object_to():
    # Empty is the caller's llm-down case, not a validation failure.
    assert nv.validate("", FACTS) == []
