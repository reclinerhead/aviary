# =============================================================================
# aviary -- test_listener_masked_report.py
#
# The masked report (issue #583) reads Earl's `masked (week N):` journal lines
# back into a per-species table. Two pure pieces are worth pinning: the parse
# must round-trip whatever earl.format_masked() writes (the two sit in
# different files and would otherwise drift silently into an empty report),
# and the summary's ordering is the report's whole argument -- days heard
# first, because a resident shows up on many days and noise on one.
#
# The journalctl call is I/O at the boundary, desk-checked on orchid.
# =============================================================================

from listener import earl, masked_report

PREFIX = "2026-11-20T07:14:02-05:00 orchid python[4069534]: [house-rear] "


def test_parse_round_trips_format_masked_behind_a_journal_prefix():
    tally = {
        "Strix varia": ["Barred Owl", 3, 0.91],
        "Accipiter cooperii": ["Cooper's Hawk", 1, 0.7],
        "Nycticorax nycticorax": ["Black-crowned Night-Heron", 2, 0.884],
    }
    rec = masked_report.parse_masked_line(
        PREFIX + earl.format_masked(tally, 39))
    assert rec == {
        "day": "2026-11-20", "source": "house-rear", "week": 39,
        "species": [("Barred Owl", 3, 0.91),
                    ("Black-crowned Night-Heron", 2, 0.88),
                    ("Cooper's Hawk", 1, 0.70)],
    }


def test_parse_reads_the_older_short_iso_offset_too():
    rec = masked_report.parse_masked_line(
        "2026-11-20T07:14:02-0500 kestrel python[198089]: [window-mic] "
        "masked (week 43): Barred Owl x1 best 0.86\n")
    assert rec["source"] == "window-mic"
    assert rec["species"] == [("Barred Owl", 1, 0.86)]


def test_parse_skips_other_and_malformed_lines():
    for line in (
        "",
        PREFIX + "gate stats: 100 windows -- 96 listen, 2 speech-killed, "
                 "2 notable, 0 ungated; gate 61 ms avg; 3 masked",
        PREFIX + "region mask: 70 species for week 43",
        PREFIX + "masked (week 39): ",
        PREFIX + "masked (week 39): Barred Owl x3",
        PREFIX + "masked (week 39): Barred Owl x3 best 0.91, garbage",
        PREFIX + "masked (week x): Barred Owl x3 best 0.91",
        "[house-rear] masked (week 39): Barred Owl x3 best 0.91",  # no stamp
    ):
        assert masked_report.parse_masked_line(line) is None


def rec(day, source, week, *species):
    return {"day": day, "source": source, "week": week,
            "species": list(species)}


def test_summarize_ranks_by_days_heard_before_volume():
    rows = masked_report.summarize([
        rec("2026-11-01", "house-rear", 41, ("Barred Owl", 1, 0.86)),
        rec("2026-11-02", "garage-rear", 41, ("Barred Owl", 2, 0.91)),
        rec("2026-11-08", "house-rear", 42, ("Barred Owl", 1, 0.70)),
        # One loud afternoon: more windows than the owl, but a single day.
        rec("2026-11-02", "house-rear", 41, ("Wood Duck", 9, 0.95)),
        rec("2026-11-02", "house-rear", 41, ("Wood Duck", 4, 0.90)),
    ])
    assert rows == [
        {"species": "Barred Owl", "windows": 4, "best": 0.91, "days": 3,
         "sources": ["garage-rear", "house-rear"], "weeks": [41, 42]},
        {"species": "Wood Duck", "windows": 13, "best": 0.95, "days": 1,
         "sources": ["house-rear"], "weeks": [41]},
    ]


def test_summarize_nothing_is_no_rows():
    assert masked_report.summarize([]) == []


def test_since_arg_turns_a_span_into_journalctl_relative_time():
    assert masked_report.since_arg("14d") == "-14d"
    assert masked_report.since_arg("36h") == "-36h"
    assert masked_report.since_arg("2026-11-01") == "2026-11-01"
    assert masked_report.since_arg("yesterday") == "yesterday"
