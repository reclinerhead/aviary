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

# The annotation vocabulary the prompts hand the model
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

# Month names, compared in lower case. May is left out on purpose: it is
# also a verb ("it may return"), and a wrong May is the rarer miss.
_MONTH = re.compile(r"\b(january|february|march|april|june|july|august|"
                    r"september|october|november|december)\b")

# The house formula the 2026-10-24 review found opening nearly every note
# ("Your driveway microphone has been picking up a good number of ...").
_FILLER = re.compile(r"\ba good number of\b", re.I)
_PICKED_UP = re.compile(r"\b(picking|picked|picks|pick)\s+up\b", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

# Numbers written as WORDS are read as numbers and held to the same figure
# rule (#614). #612's first desk run caught "seventy-three visits" escaping
# a digits-only check; #614's caught the opposite failure -- gemma copies a
# given "18 minutes before sunrise" faithfully as "eighteen minutes before
# sunrise", and a rule against spelled numbers threw away 8 of 12 sketches
# for being accurate. So a spelled number is fine when its VALUE was given
# and an invention when it wasn't, exactly like a digit. A lone word of ten
# or under ("one microphone", "two paragraphs", "seven days") is ordinary
# English, not a figure, and is never checked.
_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
_SCALES = {"hundred": 100, "thousand": 1000}
_WORD = "|".join(sorted([*_UNITS, *_TENS, *_SCALES], key=len, reverse=True))
_NUMBER_PHRASE = re.compile(
    rf"\b(?:{_WORD})(?:(?:[\s-]+(?:and[\s-]+)?)(?:{_WORD}))*\b", re.I)


def words_value(phrase):
    """"seventy-three" -> 73, "one hundred and twelve" -> 112, or None for
    a lone small word ("two"), which is English rather than a figure."""
    tokens = re.findall(_WORD, phrase.lower())
    if len(tokens) == 1 and tokens[0] in _UNITS and _UNITS[tokens[0]] <= 10:
        return None
    total = current = 0
    for t in tokens:
        if t in _UNITS:
            current += _UNITS[t]
        elif t in _TENS:
            current += _TENS[t]
        elif t == "hundred":
            current = (current or 1) * 100
        else:                                   # thousand
            total += (current or 1) * 1000
            current = 0
    return total + current


def figures(text):
    """Every figure in `text`, commas dropped -- the set the figure rule
    compares -- including numbers written as words ("eighteen" -> 18). A
    trailing comma or period is punctuation, not a decimal."""
    out = set()
    # "10:00pm" is the given "10pm" on a clock face, not a new figure "00".
    text = re.sub(r"\b(\d{1,2}):00(?!\d)", r"\1", text or "")
    for m in _FIGURE.finditer(text):
        tok = m.group(0).rstrip(",").replace(",", "")
        if tok:
            out.add(tok)
    for m in _NUMBER_PHRASE.finditer(text):
        value = words_value(m.group(0))
        if value is not None:
            out.add(str(value))
    return out


# A fact line's evidence parenthesis (#630, findings.py): the sample size
# and the spread, for the model to weigh and never to write. Its figures
# are not "given".
_EVIDENCE = re.compile(r"\s*\(evidence:[^)]*\)", re.I)


def strip_evidence(text):
    return _EVIDENCE.sub("", text or "")


def given_figures(inputs):
    """The figures the prose may state: every figure in `inputs` outside
    an (evidence: ...) parenthesis."""
    return figures(strip_evidence(inputs))


def first_sentence(text):
    text = (text or "").strip()
    return _SENTENCE_END.split(text, maxsplit=1)[0] if text else ""


def unfounded_places(text, terms, facts):
    """The place terms the prose names that no given FACT names. The #614
    desk run's worst failure: handed the yard's listening posts, gemma put
    birds at microphones that never heard them ("the back of the house,
    over the driveway, seems to pick up the most calls" -- for an owl 99%
    on the garage board). A place is a claim; only a fact can make it."""
    told = (facts or "").lower()
    out = []
    for term in terms or ():
        pattern = rf"\b{re.escape(term.lower())}\b"
        if re.search(pattern, text.lower()) and not re.search(pattern, told):
            out.append(term)
    return sorted(set(out))


def validate(text, inputs, allow_driveway=False, lede=None, place_terms=None,
             facts=None):
    """The violations in one generated block, [] when it may be stored.

    `inputs` is everything the model was shown (a string, or a list of
    them); `allow_driveway` is True only when one of this station's own
    place phrases says "driveway" -- the word is a fact about one house's
    rear camera, never a generic name for a yard. `lede` is the fact the
    note was told to open with: when it carries figures, the first sentence
    must state at least one of them (the first desk run opened all four
    notes on the visit total, whatever line came first). `place_terms` are
    the yard's place words and `facts` the fact lines the model was handed:
    a place named in the prose must be named by a fact (#614)."""
    if isinstance(inputs, (list, tuple)):
        inputs = "\n".join(i for i in inputs if i)
    text = text or ""
    problems = []

    given = given_figures(inputs)
    stated = figures(text) - given
    evidence = sorted(stated & figures(inputs), key=lambda s: (len(s), s))
    invented = sorted(stated - set(evidence), key=lambda s: (len(s), s))
    if invented:
        problems.append(
            "it states figures that were not given: " + ", ".join(invented)
            + " -- use only the figures given, written exactly as given")
    if evidence:
        problems.append(
            "it states figures from an (evidence: ...) parenthesis: "
            + ", ".join(evidence) + " -- the evidence is for you to weigh, "
            "never to write; say how solid the finding is in words")

    # Months are figures too (#614): "visiting your yard since January" for
    # a swift first heard on July 18 escaped every numeric check.
    months = sorted(set(_MONTH.findall(text.lower()))
                    - set(_MONTH.findall(inputs.lower())))
    if months:
        problems.append("it names months that were not given: "
                        + ", ".join(m.capitalize() for m in months)
                        + " -- use only the dates given")

    lede_figures = given_figures(lede)
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

    places = unfounded_places(text, place_terms, facts)
    if places:
        problems.append("it says where the bird was heard ("
                        + ", ".join(places) + ") but no fact given says so "
                        "-- mention a place only when a fact names it")

    if _FILLER.search(text):
        problems.append("it uses the stock phrase 'a good number of' -- say "
                        "the figure, or say nothing")

    if _PICKED_UP.search(first_sentence(text)):
        problems.append("it opens by saying a microphone picked the bird up "
                        "-- open with the first fact given instead")

    return problems
