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


def test_numbers_written_as_words_are_caught():
    # Verbatim from the Barred Owl's first #612 desk run.
    text = ("You've recorded seventy-three visits, sixty-three percent of "
            "them late at night.")
    problems = nv.validate(text, FACTS)
    assert any("seventy" in p and "sixty" in p for p in problems)
    # One to ten are ordinary English, not hidden figures.
    assert nv.validate("Heard from two microphones over seven days.",
                       FACTS) == []


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


def test_an_empty_draft_has_nothing_to_object_to():
    # Empty is the caller's llm-down case, not a validation failure.
    assert nv.validate("", FACTS) == []
