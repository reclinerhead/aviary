# =============================================================================
# project-squirrel -- listener/notes_validate.py
#
# The check between the model and the store (issue #612). The field notes'
# whole safety property is "Python computes, the model narrates" -- and until
# this file, nothing verified the narration. Three failure modes the guide
# recorded as FIXED BY PROMPT WORDING were back in the store on 2026-10-24
# ("'Evidence: solid' suggests this pattern is reliable", a sensor dropout
# narrated as "a curious result" about the `unknown` bucket, "Our microphone"
# after #558 banned it). A rule in a prompt is a request; this is the check.
#
#   validate(text, inputs, allow_driveway=False) -> [violation, ...]
#
# Pure and deliberately dumb: regexes over the prose, no model, no I/O. It
# answers ONE question -- may this text be stored? -- and every answer it
# gives is a sentence the model can be shown on a retry ("Your previous draft
# broke these rules: ..."), which is why violations are English, not codes.
#
# THE FIGURE RULE is the load-bearing one. Every digit run in the prose must
# appear somewhere in what the model was given (`inputs`: the prompt itself
# -- fact lines, the listening-posts sentence, the Wikipedia background).
# "Figures ⊆ inputs" is exactly "the model did no arithmetic and invented no
# number", made mechanical. Commas are ignored on both sides (1,500 and 1500
# are one figure), and a number written as a WORD is not caught -- the rules
# tell the model to write digits, and a spelled-out "fourteen" that slips
# through is copied from a figure far more often than invented.
#
# Shared on purpose: Dawn Chorus (#507) names the same gate ("no numbers not
# present in the input"), and the field-journal epic (#611) routes every
# entry through it.
# =============================================================================

import re

# A figure: a digit run, optionally comma-grouped, optionally with decimals.
# "7am" yields 7, "35%" yields 35, "1,500" yields 1500, "0.82" yields 0.82.
_FIGURE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# The annotation vocabulary describe_stats/describe_weather hand the model
# as LABELS. Case-sensitive where the label is uppercase (so ordinary prose
# saying "thin" or "more often" is fine), case-insensitive for "Evidence:"
# because the leak arrived as "'Evidence: solid'".
_LABELS = (
    (re.compile(r"evidence\s*:", re.I), "the label 'Evidence:'"),
    (re.compile(r"\bTHIN\b"), "the label 'THIN'"),
    (re.compile(r"\bMORE OFTEN\b"), "the label 'MORE OFTEN'"),
    (re.compile(r"\bLESS OFTEN\b"), "the label 'LESS OFTEN'"),
    (re.compile(r"\bABOUT AVERAGE\b"), "the label 'ABOUT AVERAGE'"),
    (re.compile(r"\bSKY\b"), "the label 'SKY'"),
    (re.compile(r"\bTEMPERATURE\b"), "the label 'TEMPERATURE'"),
)

# `unknown` is a sensor dropout's bucket name, never a fact about a bird.
_UNKNOWN = re.compile(r"\bunknown\b", re.I)

# The reader owns the yard (#558). \bour cannot match inside "your".
_POSSESSIVE = re.compile(
    r"\b(our|my)\s+(own\s+)?(microphones?|mics?|yard|garden|driveway|"
    r"weather station|recordings?|records?|data)\b", re.I)

_DRIVEWAY = re.compile(r"\bdriveway\b", re.I)

# The house formula the 2026-10-24 review found opening nearly every note
# ("Your driveway microphone has been picking up a good number of ...").
_FILLER = re.compile(r"\ba good number of\b", re.I)
_PICKED_UP = re.compile(r"\b(picking|picked|picks|pick)\s+up\b", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# A figure written as a word escapes the figure rule entirely -- the first
# desk run of #612 wrote "seventy-three visits" and "sixty-three percent".
# One to ten stay legal ("one microphone", "two paragraphs", "seven days" are
# ordinary English); anything a count or a percentage would need is caught.
_NUMBER_WORD = re.compile(
    r"\b(eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|"
    r"nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundred|thousand)\b", re.I)


def figures(text):
    """Every figure in `text`, commas dropped -- the set the figure rule
    compares. A trailing comma or period is punctuation, not a decimal."""
    out = set()
    for m in _FIGURE.finditer(text or ""):
        tok = m.group(0).rstrip(",").replace(",", "")
        if tok:
            out.add(tok)
    return out


def first_sentence(text):
    text = (text or "").strip()
    return _SENTENCE_END.split(text, maxsplit=1)[0] if text else ""


def validate(text, inputs, allow_driveway=False, lede=None):
    """The violations in one generated block, [] when it may be stored.

    `inputs` is everything the model was shown (a string, or a list of
    them); `allow_driveway` is True only when one of this station's own
    place phrases says "driveway" -- the word is a fact about one house's
    rear camera, never a generic name for a yard. `lede` is the fact the
    note was told to open with: when it carries figures, the first sentence
    must state at least one of them (the first desk run opened all four
    notes on the visit total, whatever line came first)."""
    if isinstance(inputs, (list, tuple)):
        inputs = "\n".join(i for i in inputs if i)
    text = text or ""
    problems = []

    invented = sorted(figures(text) - figures(inputs),
                      key=lambda s: (len(s), s))
    if invented:
        problems.append(
            "it states figures that were not given: " + ", ".join(invented)
            + " -- use only the figures given, written exactly as given")

    words = sorted({m.group(0).lower() for m in _NUMBER_WORD.finditer(text)})
    if words:
        problems.append("it spells numbers out as words (" + ", ".join(words)
                        + ") -- write every figure in digits, exactly as "
                        "given")

    lede_figures = figures(lede)
    if text and lede_figures and not (figures(first_sentence(text))
                                      & lede_figures):
        problems.append("its first sentence is not about the fact it was "
                        f"told to open with: \"{lede.strip()}\"")

    for pattern, name in _LABELS:
        if pattern.search(text):
            problems.append(f"it copies {name} into the prose -- the labels "
                            "are for you to read, never words to write")

    if _UNKNOWN.search(text):
        problems.append("it mentions an 'unknown' reading -- a missing "
                        "measurement is not a fact about the bird; leave it "
                        "out")

    m = _POSSESSIVE.search(text)
    if m:
        problems.append(f"it says '{m.group(0)}' -- the yard and the "
                        "microphones are the reader's: say 'your'")

    if not allow_driveway and _DRIVEWAY.search(text):
        problems.append("it calls the place a driveway -- name each "
                        "microphone by the place it was given, nothing else")

    if _FILLER.search(text):
        problems.append("it uses the stock phrase 'a good number of' -- say "
                        "the figure, or say nothing")

    if _PICKED_UP.search(first_sentence(text)):
        problems.append("it opens by saying a microphone picked the bird up "
                        "-- open with the first fact given instead")

    return problems
