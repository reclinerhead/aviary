# =============================================================================
# project-squirrel -- listener/clip_vocal_fill.py
#
# Vocal-fill score for Earl visit clips (issue #442): how much of a clip
# carries bird-band energy, not just a high-confidence chirp with silence
# around it. The species-page "best recording" ranks by confidence *and*
# this score so a full song can beat a single warble at 0.98.
#
#   python -m listener.clip_vocal_fill                 # score missing rows
#   python -m listener.clip_vocal_fill --limit 50 --dry-run
#   python -m listener.clip_vocal_fill --refresh amcrest/123-Northern_Cardinal.wav
#   python -m listener.clip_vocal_fill --force          # recompute even if set
#
# Enrichment-pass ethos (clip_enhance, species_profile): worklist-driven,
# idempotent, re-runnable. Operators run this once on pearl / birdhouse after
# deploy so existing champions re-rank without waiting for a new visit.
#
# THE METRIC (pure, numpy only):
#   1. Keep only the songbird band (~1.5–8 kHz) — wind/HVAC/road live below.
#   2. RMS energy in short frames across the clip.
#   3. Noise floor from the clip's own pre-roll (prev window), same matched
#      scene idea as clip_enhance — or a quiet percentile when too short.
#   4. vocal_fill = fraction of frames above floor * margin, clamped [0, 1].
#
# Broadband sightings.rms is NOT enough: busy low-frequency noise looks
# "full" while a clean warble looks quiet. Band-limit is the whole point.
#
# Persist: sightings.vocal_fill REAL (nullable). MCC never decodes WAVs —
# bestClips reads the column. Unscored rows keep confidence-only ranking.
#
# Config:
#   MERLE_EARL_CLIPS  clip dir (default "clips")
#   MERLE_EARL_DB     sightings store (default "earl.db")
# =============================================================================

from __future__ import annotations

import argparse
import os
import sys
import wave

import numpy as np

from listener import clip_enhance as ce
from listener import gate
from listener import sightings

# Songbird vocalization band — align with clip_enhance's pass edge for the
# low end; high end cuts hiss/ultrasonic garbage without eating most song.
BIRD_LO_HZ = 1500.0
BIRD_HI_HZ = 8000.0

# Frame analysis: ~50 ms, 50% hop — fine enough for a chirp, cheap at 48 kHz.
FRAME_S = 0.05
HOP_S = 0.025

# A frame is "active" when its bird-band RMS exceeds the noise floor by this
# factor. Tuned for synthetic tones + real yard clips: 1.0 is hair-trigger
# on pre-roll residual; 10.0 misses quiet song. 3× is the starting contract.
FLOOR_MULT = 3.0
# Absolute floor so digital silence does not divide by zero / fire on denorms.
ABS_FLOOR = 1e-5


def clips_dir():
    return os.environ.get("MERLE_EARL_CLIPS", "").strip() or ce.DEFAULT_CLIPS_DIR


# --- Pure DSP ----------------------------------------------------------------

def bird_band(samples, sr, lo_hz=BIRD_LO_HZ, hi_hz=BIRD_HI_HZ):
    """FFT band-pass keeping only the songbird band. Zero outside; soft
    nothing — a brick wall is fine here because we measure energy, we do not
    re-listen to the reconstruction."""
    x = np.asarray(samples, dtype=np.float64)
    if x.size == 0:
        return x
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    X = np.fft.rfft(x)
    gain = ((freqs >= lo_hz) & (freqs <= hi_hz)).astype(np.float64)
    return np.fft.irfft(X * gain, n=x.size)


def frame_rms(samples, sr, frame_s=FRAME_S, hop_s=HOP_S):
    """RMS of successive frames. Empty when the clip is shorter than one
    frame — caller treats that as zero fill."""
    x = np.asarray(samples, dtype=np.float64)
    frame = max(1, int(round(frame_s * sr)))
    hop = max(1, int(round(hop_s * sr)))
    if x.size < frame:
        return np.zeros(0, dtype=np.float64)
    n = 1 + (x.size - frame) // hop
    out = np.empty(n, dtype=np.float64)
    for i in range(n):
        chunk = x[i * hop:i * hop + frame]
        out[i] = float(np.sqrt(np.mean(chunk * chunk)))
    return out


def vocal_fill(samples, sr, preroll_s=gate.WINDOW_S,
               lo_hz=BIRD_LO_HZ, hi_hz=BIRD_HI_HZ,
               floor_mult=FLOOR_MULT):
    """Fraction of frames with bird-band energy above the noise floor.

    Returns a float in [0, 1]. Empty / near-empty audio → 0.0 (honest empty
    song, not null — null is reserved for "never scored" in the store)."""
    x = np.asarray(samples, dtype=np.float64)
    if x.size == 0 or sr <= 0:
        return 0.0
    banded = bird_band(x, sr, lo_hz=lo_hz, hi_hz=hi_hz)
    rms = frame_rms(banded, sr)
    if rms.size == 0:
        return 0.0

    hop = max(1, int(round(HOP_S * sr)))
    n_pre = max(1, int(round(preroll_s * sr / hop)))
    if n_pre < rms.size:
        floor_src = rms[:n_pre]
    else:
        # No clean pre-roll (clip shorter than a window, or stream start):
        # quiet percentile of the whole clip is the honest stand-in.
        floor_src = rms

    floor = max(float(np.median(floor_src)) * floor_mult, ABS_FLOOR)
    active = rms > floor
    fill = float(np.mean(active))
    if fill < 0.0:
        return 0.0
    if fill > 1.0:
        return 1.0
    return fill


# --- Score one path / one row ------------------------------------------------

def score_wav(path):
    """Read a clip and return vocal_fill, or None if unreadable / wrong
    format. Fail closed — a bad file must not invent a showcase take."""
    try:
        samples, sr = ce.read_wav(path)
    except (OSError, ValueError, EOFError, wave.Error):
        # EOFError: an empty or truncated file (#601) -- not a wave.Error.
        return None
    if samples.size == 0:
        return 0.0
    return vocal_fill(samples, sr)


def score_relpath(media_dir, relpath):
    """Relative clip path under MERLE_EARL_CLIPS → fill or None."""
    if not relpath or not str(relpath).endswith(".wav"):
        return None
    if ce.is_enhanced(relpath):
        # Rank the original visit clip; enhance is a playback treatment.
        return None
    full = os.path.join(media_dir, relpath)
    if not os.path.isfile(full):
        return None
    return score_wav(full)


# --- Worklist over sightings -------------------------------------------------

def worklist(conn, force=False, refresh_clip=None):
    """Rows that still need a score: (id, clip). Pure against a connection.

    `refresh_clip` forces that relative path (every matching row).
    `force` recomputes every row that has a clip."""
    if refresh_clip is not None:
        rows = conn.execute(
            "SELECT id, clip FROM sightings WHERE clip = ? ORDER BY id",
            (refresh_clip,),
        ).fetchall()
        return [(int(r["id"]), r["clip"]) for r in rows if r["clip"]]

    if force:
        rows = conn.execute(
            "SELECT id, clip FROM sightings"
            " WHERE clip IS NOT NULL AND clip != ''"
            " ORDER BY id"
        ).fetchall()
        return [(int(r["id"]), r["clip"]) for r in rows]

    rows = conn.execute(
        "SELECT id, clip FROM sightings"
        " WHERE clip IS NOT NULL AND clip != ''"
        " AND vocal_fill IS NULL"
        " ORDER BY id"
    ).fetchall()
    return [(int(r["id"]), r["clip"]) for r in rows]


def apply_fill(conn, sighting_id, fill):
    """Write one score. Does not commit — caller batches."""
    conn.execute(
        "UPDATE sightings SET vocal_fill = ? WHERE id = ?",
        (float(fill), int(sighting_id)),
    )


def score_row(conn, media_dir, sighting_id, relpath):
    """Score one sighting. Returns status word for the bulk counter:
      'scored'   — wrote a measured fill
      'missing'  — clip file not on disk (writes 0.0 so the row leaves the
                   worklist; pruned clips are normal under retention)
      'bad'      — unreadable / wrong format (writes 0.0 for the same reason)
    """
    fill = score_relpath(media_dir, relpath)
    if fill is None:
        full = os.path.join(media_dir, relpath) if relpath else ""
        # Persist 0.0 rather than leaving NULL: otherwise every re-run
        # re-walks thousands of retention-pruned paths forever. Zero fill
        # is honest for a vanished file (no song on disk to showcase).
        apply_fill(conn, sighting_id, 0.0)
        if not relpath or not os.path.isfile(full):
            return "missing"
        return "bad"
    apply_fill(conn, sighting_id, fill)
    return "scored"


# --- CLI ---------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Score Earl visit clips for vocal fill (#442)")
    ap.add_argument("--db", default=None,
                    help="earl.db path (default: $MERLE_EARL_DB or earl.db)")
    ap.add_argument("--clips", default=None,
                    help="clip dir (default: $MERLE_EARL_CLIPS or clips)")
    ap.add_argument("--refresh", metavar="RELPATH",
                    help="re-score every sighting row pointing at this clip")
    ap.add_argument("--force", action="store_true",
                    help="recompute even when vocal_fill is already set")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N rows")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be scored; write nothing")
    args = ap.parse_args(argv)

    db = args.db or sightings.db_path()
    media = args.clips or clips_dir()
    conn = sightings.connect(db)
    try:
        todo = worklist(conn, force=args.force, refresh_clip=args.refresh)
        if args.limit is not None:
            todo = todo[: args.limit]
        if not todo:
            print("[vocal-fill] nothing to do — every clip row is scored "
                  "(or no clips on the worklist)", flush=True)
            return 0

        if args.dry_run:
            print(f"[vocal-fill] {len(todo)} rows would be scored "
                  f"(db={db}, clips={media}):", flush=True)
            for sid, clip in todo:
                print(f"[vocal-fill]   id={sid} {clip}", flush=True)
            return 0

        print(f"[vocal-fill] {len(todo)} rows to score under {media}",
              flush=True)
        counts: dict[str, int] = {}
        # Pruned clips are normal (retention) — count them, do not spam the
        # journal with one line per faded path. Failures and bad formats still
        # print; --refresh always prints the one path it was asked about.
        for sid, clip in todo:
            try:
                status = score_row(conn, media, sid, clip)
            except Exception as e:
                if args.refresh:
                    raise
                print(f"[vocal-fill] id={sid} {clip}: FAILED ({e})",
                      flush=True)
                counts["failed"] = counts.get("failed", 0) + 1
                continue
            counts[status] = counts.get(status, 0) + 1
            if args.refresh or status in ("bad", "failed"):
                print(f"[vocal-fill] id={sid} {clip}: {status}", flush=True)
        conn.commit()
        print("[vocal-fill] done: " + ", ".join(
            f"{k}={v}" for k, v in sorted(counts.items())), flush=True)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
