# =============================================================================
# aviary -- listener/masked_report.py
#
# What the region mask has been costing us, by species (issue #583). The mask
# (D4) is mandatory and it works, but a year-round resident can fall out of
# BirdNET's geo model for weeks at a time (the Barred Owl is in orchid's mask
# 35 weeks of 48), and in those weeks a real call is discarded. Earl's workers
# now write what the mask alone cut to the journal, one line per source per
# gate-stats period:
#
#   [house-rear] masked (week 39): Barred Owl x3 best 0.91, Wood Duck x1 best 0.72
#
# This reads those lines back and prints one row per species, so there is
# something to decide an `always_include` rule (#506) from:
#
#   python -m listener.masked_report [--since 14d] [--unit earl-listener]
#
# Rows sort by distinct days heard: a species the mask cut on nine separate
# days is a resident candidate, and a single 0.71 on one afternoon is noise.
# The report only measures. It sets no rule, and a confident BirdNET can still
# be wrong (the rover motor was a 0.9 Wood Duck), so listen before opening a
# hole.
#
# Stdlib only, so it runs from the repo venv or Earl's. The parsing and the
# summary are pure and under test_listener_masked_report.py; the journalctl
# call is I/O at the boundary, desk-checked on orchid.
# =============================================================================

import argparse
import re
import subprocess
import sys

# `journalctl -o short-iso`: "<iso timestamp> <host> <proc>[pid]: <message>".
# The source is the bracket directly before " masked (" -- the pid's bracket
# is followed by a colon, so it can't be mistaken for it.
_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})T\S+ .*\[([^\]]+)\] masked \(week (\d+)\): (.+)$")
# Greedy name, so "x3" inside a name could never split it early; apostrophes
# and hyphens (Cooper's Hawk, Black-crowned Night-Heron) are just characters.
_ENTRY_RE = re.compile(r"^(.+) x(\d+) best (\d+(?:\.\d+)?)$")
_RELATIVE_RE = re.compile(r"^\d+\s*[a-z]+$")


def parse_masked_line(line):
    """One journal line -> {"day", "source", "week", "species": [(common,
    windows, best_conf), ...]}, or None for anything that is not a whole,
    well-formed `masked (week N):` line. One bad entry rejects the line: a
    half-read line would be a count nobody can trust."""
    m = _LINE_RE.match(line.strip())
    if not m:
        return None
    day, source, week, body = m.groups()
    species = []
    for chunk in body.split(", "):
        e = _ENTRY_RE.match(chunk)
        if not e:
            return None
        species.append((e.group(1), int(e.group(2)), float(e.group(3))))
    return {"day": day, "source": source, "week": int(week),
            "species": species}


def summarize(records):
    """Parsed lines -> one row per species: {"species", "windows", "best",
    "days", "sources", "weeks"}, sorted by distinct days heard (most first),
    then windows, then name."""
    rows = {}
    for rec in records:
        for common, windows, best in rec["species"]:
            row = rows.setdefault(common, {
                "species": common, "windows": 0, "best": 0.0,
                "days": set(), "sources": set(), "weeks": set()})
            row["windows"] += windows
            row["best"] = max(row["best"], best)
            row["days"].add(rec["day"])
            row["sources"].add(rec["source"])
            row["weeks"].add(rec["week"])
    out = [{"species": r["species"], "windows": r["windows"],
            "best": r["best"], "days": len(r["days"]),
            "sources": sorted(r["sources"]), "weeks": sorted(r["weeks"])}
           for r in rows.values()]
    out.sort(key=lambda r: (-r["days"], -r["windows"], r["species"]))
    return out


def since_arg(raw):
    """`14d` -> `-14d` (journalctl's "this long ago"); anything else, a
    date or "yesterday", passes through as journalctl's own syntax."""
    raw = raw.strip()
    return f"-{raw}" if _RELATIVE_RE.match(raw) else raw


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m listener.masked_report",
        description="What the region mask discarded, by species (#583).")
    ap.add_argument("--since", default="14d",
                    help="how far back to read: 14d, 36h, or a journalctl "
                         "date (default 14d)")
    ap.add_argument("--unit", default="earl-listener",
                    help="the systemd unit to read (default earl-listener)")
    args = ap.parse_args(argv)

    try:
        proc = subprocess.run(
            ["journalctl", "-u", args.unit, "--since", since_arg(args.since),
             "-o", "short-iso", "--no-pager"],
            capture_output=True, text=True)
    except FileNotFoundError:
        print("journalctl not found -- run this on the box", file=sys.stderr)
        return 2
    if proc.returncode != 0:
        print(proc.stderr.strip() or f"journalctl exited {proc.returncode}",
              file=sys.stderr)
        return 2

    records = [r for r in map(parse_masked_line, proc.stdout.splitlines())
               if r]
    rows = summarize(records)
    if not rows:
        print(f"{args.unit}: the region mask cut nothing since {args.since} "
              "(or this Earl predates #583)")
        return 0
    width = max(len("species"), *(len(r["species"]) for r in rows))
    print(f"{'species':<{width}}  days  windows  best  weeks        sources")
    for r in rows:
        weeks = ",".join(str(w) for w in r["weeks"])
        print(f"{r['species']:<{width}}  {r['days']:>4}  {r['windows']:>7}  "
              f"{r['best']:.2f}  {weeks:<11}  {', '.join(r['sources'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
