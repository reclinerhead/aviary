# =============================================================================
# project-squirrel -- listener/clip_enhance.py
#
# The clip enhancement pass (epic #182, issue #190; v2 in #599): make a faint
# bird AUDIBLE to a human. BirdNET finds birds buried under a plane, road
# noise, wind, or HVAC -- that is the whole point of the model -- but the clip
# is the only evidence a person can independently evaluate, and a clip you
# cannot hear is a clip you cannot check. A high confidence on a barely
# audible signal is exactly the profile of a plausible false positive, so
# "can I hear it?" is not a comfort feature; it is how the model gets
# audited. (Measured motivator: sighting 107, Bald Eagle, confidence 0.852,
# rms 0.01892.)
#
#   python -m listener.clip_enhance                     # fill what's missing
#   python -m listener.clip_enhance --limit 20 --dry-run
#   python -m listener.clip_enhance --refresh amcrest/1752900000-Blue_Jay.wav
#   python -m listener.clip_enhance --force              # every original, v2
#
# The enrichment-pass ethos, same as species_profile.py: worklist-driven,
# idempotent, a per-clip function with the bulk CLI as a thin loop over it
# (a future per-clip "re-enhance" button in the Aviary calls exactly what
# --refresh calls today).
#
# THE SIGNAL CHAIN, AND WHY THE ORDER IS LOAD-BEARING:
#
#   1. BAND-LIMIT. Songbird vocalization lives roughly 2-8 kHz; aircraft,
#      road, wind and HVAC energy is overwhelmingly sub-1 kHz. Rolling off
#      below ~1.5 kHz removes a large fraction of the interfering energy
#      while touching almost none of the bird. Since #599 the band has a top
#      edge too (unity to 10 kHz, gone by 12): yard song tops out below
#      10 kHz and microphone hiss does not. One global band -- per-species
#      banding is explicitly out of scope.
#
#   2. SPECTRAL SUBTRACTION, using the clip's OWN pre-roll as the noise
#      profile. This is the key move and it is nearly free here: write_clip()
#      stores prev + event + next windows, so the leading 3-second window is
#      by construction *this exact acoustic scene without the bird in it* --
#      a perfectly matched, per-clip noise estimate that no general-purpose
#      denoiser gets to have. Subtract with a spectral floor rather than to
#      zero; subtracting to zero is what makes musical noise.
#
#      The floor is 0.30 and the gain mask is smoothed (#599). Three months
#      of clips measured why: the 0.05 floor left a field of isolated
#      spectral spikes that the normalize then lifted by 15-35 dB, and in
#      half of all clips the pre-roll is NOT bird-free (the bird sang before
#      crossing the threshold, or the clip is a mid-visit best-upgrade), so
#      the "matched" profile is often only roughly matched. On the clips
#      where the premise holds, the subtraction adds ~7.2 dB of event-bird
#      over pre-roll SNR beyond the band-limit alone; floor 0.30 with a
#      3x3 mask smoothing keeps 7.0 dB of that and cuts the pre-roll speckle
#      index from 1.99 to 0.92 (a natural bed sits near 0.71). The residual
#      bed is what masks both the speckle and the "transient standing in a
#      vacuum" effect that made a knock or a voice the loudest thing in the
#      clip.
#
#   3. NORMALIZE, LAST. Normalizing first normalizes *the plane*, and the
#      bird stays exactly as faint in relative terms. Only once the
#      interfering energy is gone is the bird the thing that reaches full
#      scale. The test suite asserts this ordering directly, because it is
#      the entire design rationale and a reordering would look fine in a
#      build and sound wrong forever.
#
#      With a cap (#599): the pass may lift the band-limited clip at most
#      GAIN_CAP_DB beyond where a plain normalize of it would land. The
#      reference is the band-limited clip, not the raw one, on purpose: the
#      band-limit's whole job is to remove a full-scale plane from under a
#      -40 dB bird, and a cap measured against the plane would forbid the
#      pass its motivating case. Measured against what survives the band,
#      the cap bounds how loud a stray knock, voice or dog can become.
#
# NEVER IN PLACE. Clips are rewritten when a later window of the same visit
# beats the visit's best confidence (earl.py's VisitTracker verdicts), so an
# in-place pass would be clobbered by the next `best` verdict, or would
# clobber a fresher original. We write a sibling -- <stem>-enh.wav -- and the
# original is never modified and never deleted here. That also keeps the A/B
# possible: enhancement is a lossy aesthetic judgment, and being able to
# compare is how we find out whether it is helping.
#
# Idempotence falls out of mtime: a sibling newer than its original is
# skipped, and a clip rewritten by a later `best` verdict is newer than its
# sibling and so gets re-enhanced for free on the next run. File existence is
# the source of truth -- no schema change, no new column (the do-not-change
# list). The corollary: a parameter change re-enhances NOTHING on its own.
# `--force` puts every original on the worklist regardless of its sibling's
# age; it is a hand step per box after a deploy like #599, never the loop's.
#
# A sibling is written whole or not at all (#601). Kestrel lost power 81 s
# after a tick wrote two siblings; the names survived with zero bytes (ext4
# delayed allocation), their mtimes were newer than the originals so the
# worklist called them current, and wave.open's EOFError on them exited the
# showcase encoder every tick for twenty days. So write_wav() writes a
# `.part` beside the target, fsyncs it, and os.replace()s it into the name;
# and the listing treats an empty sibling as no sibling, so whatever slips
# through is rewritten on the next tick.
#
# numpy only, deliberately: numpy is already in the Earl venv, and
# numpy.fft is entirely sufficient for an FFT-domain band-limit and an
# STFT-based subtraction. scipy would fight test_import_boundary.py and the
# lean-venv posture for no real gain.
#
# Config (env):
#   MERLE_EARL_CLIPS  the clip dir (default "clips")
# =============================================================================

import argparse
import os
import wave

import numpy as np

from listener import gate

DEFAULT_CLIPS_DIR = "clips"
ENH_SUFFIX = "-enh.wav"
SPECIES_DIR = "species"   # the portrait shelf shares the dir; not our business

# --- The band ----------------------------------------------------------------
# Full stop below BAND_STOP_HZ, unity at and above BAND_PASS_HZ, raised-cosine
# between. The ramp is not decoration: a brick wall in the FFT domain rings,
# and ringing at 1 kHz is a new noise to replace the one we just removed.
BAND_STOP_HZ = 1000.0
BAND_PASS_HZ = 1500.0
# The top edge (#599): unity to BAND_TOP_PASS_HZ, gone by BAND_TOP_STOP_HZ,
# the same raised cosine. Measured as cheap insurance: 3 of 180 current clips
# were hiss-dominated, 9 of 25 from the rover's USB mic were.
BAND_TOP_PASS_HZ = 10000.0
BAND_TOP_STOP_HZ = 12000.0

# --- The subtraction ---------------------------------------------------------
N_FFT = 1024              # 21ms at 48k -- fine enough for song, coarse enough
HOP = 256                 # 75% overlap; Hann + hop=N/4 reconstructs cleanly
OVERSUBTRACT = 1.5        # subtract a bit more than the estimate; noise varies
SPECTRAL_FLOOR = 0.30     # never below 30% of the original bin (#599; was 0.05)
MASK_SMOOTH = (3, 3)      # box-smooth the gain mask over (frames, bins)
EPS = 1e-12

# --- The output level --------------------------------------------------------
PEAK = 0.89               # ~-1 dBFS: loud, with headroom for the int16 round
GAIN_CAP_DB = 12.0        # at most this far beyond a plain normalize (#599)


def clips_dir():
    return os.environ.get("MERLE_EARL_CLIPS", "").strip() or DEFAULT_CLIPS_DIR


# --- Pure DSP (test_listener_clip_enhance.py) --------------------------------

def _raised_cosine_edge(freqs, lo_hz, hi_hz):
    """0 at lo_hz, 1 at hi_hz, half-cosine between -- the one ramp shape both
    band edges use."""
    t = np.clip((freqs - lo_hz) / (hi_hz - lo_hz), 0.0, 1.0)
    return 0.5 - 0.5 * np.cos(np.pi * t)


def band_limit(x, sr, stop_hz=BAND_STOP_HZ, pass_hz=BAND_PASS_HZ,
               top_pass_hz=BAND_TOP_PASS_HZ, top_stop_hz=BAND_TOP_STOP_HZ):
    """Keep the band the birds live in: roll off below it (the plane, the
    road, the furnace) and, since #599, above it (mic hiss). FFT-domain over
    the whole clip -- at 9 seconds that is one transform, and it buys an
    exactly specified response instead of a filter design."""
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return x
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    gain = _raised_cosine_edge(freqs, stop_hz, pass_hz)
    gain *= 1.0 - _raised_cosine_edge(freqs, top_pass_hz, top_stop_hz)
    return np.fft.irfft(np.fft.rfft(x) * gain, x.size)


def _window(n_fft):
    """Periodic Hann -- the COLA-correct one. np.hanning is symmetric and
    would leave a slow ripple across the overlap-add."""
    return np.hanning(n_fft + 1)[:n_fft]


def stft(x, n_fft=N_FFT, hop=HOP):
    """(frames, bins) complex spectrogram.

    Both ends are zero-padded by a full window's overlap so that EVERY real
    sample sits under the same number of frames as every other one. Without
    that pad the first and last samples are covered by a single Hann taper
    that approaches zero, and the overlap-add's divisor approaches zero with
    it -- which turns the clip's first millisecond into a 300x spike the
    moment the spectrum is modified. (It round-trips fine unmodified, so this
    is a trap that only springs once the pass does its actual job.)"""
    x = np.asarray(x, dtype=np.float64)
    win = _window(n_fft)
    pad = n_fft - hop
    x = np.pad(x, (pad, pad))
    n_frames = 1 + int(np.ceil(max(0, x.size - n_fft) / hop))
    padded = np.pad(x, (0, (n_frames - 1) * hop + n_fft - x.size))
    frames = np.stack([padded[i * hop:i * hop + n_fft] * win
                       for i in range(n_frames)])
    return np.fft.rfft(frames, axis=1)


def istft(spec, n_samples, n_fft=N_FFT, hop=HOP):
    """Weighted overlap-add back to n_samples, undoing stft's edge pad.
    Dividing by the summed squared window is what makes this a real inverse
    rather than an approximation that quietly scallops the output."""
    win = _window(n_fft)
    pad = n_fft - hop
    frames = np.fft.irfft(spec, n_fft, axis=1)
    length = (frames.shape[0] - 1) * hop + n_fft
    out = np.zeros(length)
    wsum = np.zeros(length)
    for i in range(frames.shape[0]):
        out[i * hop:i * hop + n_fft] += frames[i] * win
        wsum[i * hop:i * hop + n_fft] += win ** 2
    return (out / np.maximum(wsum, EPS))[pad:pad + n_samples]


def noise_profile(noise, n_fft=N_FFT, hop=HOP):
    """The mean magnitude spectrum of a stretch of noise -- here, the clip's
    own pre-roll. One vector of bins, which is the whole estimate."""
    return np.abs(stft(noise, n_fft, hop)).mean(axis=0)


def _box_smooth(mask, shape):
    """Mean over a (frames, bins) box, edge-padded so the clip's first and
    last frames and the band's edge bins are averaged with themselves rather
    than with zeros. A mean of values in [floor, 1] stays in [floor, 1]."""
    t, f = shape
    if t <= 1 and f <= 1:
        return mask
    padded = np.pad(mask, ((t // 2, t - 1 - t // 2), (f // 2, f - 1 - f // 2)),
                    mode="edge")
    out = np.zeros_like(mask)
    for i in range(t):
        for j in range(f):
            out += padded[i:i + mask.shape[0], j:j + mask.shape[1]]
    return out / (t * f)


def subtraction_mask(mag, profile, oversub=OVERSUBTRACT, floor=SPECTRAL_FLOOR,
                     smooth=MASK_SMOOTH):
    """The per-bin gain the subtraction applies, in [0, 1]: what is left of
    each bin after the profile comes off, never below `floor` of the bin
    itself. The floor is proportional to the bin's own magnitude, so a bin
    that is nearly all noise is attenuated hard while a bin carrying real
    signal keeps its shape -- the standard defense against the warbling
    'musical noise' that a subtract-to-zero produces. Smoothing the mask over
    a few frames and bins (#599) is the second defense: an isolated bin that
    happened to poke above the profile no longer becomes an isolated spike in
    the output. Pure, so the tests can look at the mask itself."""
    mag = np.asarray(mag, dtype=np.float64)
    kept = np.maximum(mag - oversub * np.asarray(profile)[None, :], floor * mag)
    mask = kept / np.maximum(mag, EPS)
    if smooth is not None:
        mask = _box_smooth(mask, smooth)
    return np.clip(mask, 0.0, 1.0)


def spectral_subtract(x, profile, oversub=OVERSUBTRACT, floor=SPECTRAL_FLOOR,
                      n_fft=N_FFT, hop=HOP, smooth=MASK_SMOOTH):
    """Subtract the noise profile from every frame's magnitude, keeping the
    original phase -- see subtraction_mask() for the floor and the smoothing."""
    x = np.asarray(x, dtype=np.float64)
    spec = stft(x, n_fft, hop)
    mask = subtraction_mask(np.abs(spec), profile, oversub, floor, smooth)
    return istft(spec * mask, x.size, n_fft, hop)


def normalize(x, peak=PEAK, ref=None, cap_db=GAIN_CAP_DB):
    """Peak-normalize. Runs LAST, always -- see the header. Digital silence
    stays silence rather than becoming amplified nothing.

    With `ref` (#599), the gain is capped at `cap_db` beyond what a plain
    normalize of `ref` would apply: the pass may lift the clip at most that
    far past where the reference would have landed. The gain is never larger
    than the plain peak gain, so the output never exceeds `peak` either way.
    Without `ref` this is exactly the v1 function."""
    x = np.asarray(x, dtype=np.float64)
    loudest = float(np.max(np.abs(x))) if x.size else 0.0
    if loudest <= 0:
        return x
    g = peak / loudest
    if ref is not None:
        ref = np.asarray(ref, dtype=np.float64)
        ref_loudest = float(np.max(np.abs(ref))) if ref.size else 0.0
        if ref_loudest > 0:
            g = min(g, (peak / ref_loudest) * 10 ** (cap_db / 20.0))
    return x * g


def enhance(samples, sr=gate.SAMPLE_RATE, preroll_s=gate.WINDOW_S):
    """The whole chain over one clip's samples (float, -1..1), in order.

    The pre-roll is the leading `preroll_s` seconds -- write_clip()'s `prev`
    window, the scene without the bird. A clip too short to carry one (a
    window at the very start of a stream, where there was no prev to save)
    gets the band-limit and the normalize and honestly skips the subtraction
    rather than estimating the noise from the bird itself. The normalize is
    capped against the band-limited clip (the header says why not the raw)."""
    banded = band_limit(samples, sr)
    x = banded
    pre = x[:int(preroll_s * sr)]
    if pre.size >= N_FFT and x.size > pre.size:
        x = spectral_subtract(x, noise_profile(pre))
    return normalize(x, ref=banded)


# --- WAV I/O (skipped by the test contract -- the boundary, not the logic) ---

def read_wav(path):
    """One of Earl's clips -> (float64 samples in -1..1, sample rate). Mono
    s16le is the only thing write_clip() produces; anything else is a file we
    did not write and have no business rewriting."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != gate.BYTES_PER_SAMPLE:
            raise ValueError(f"{path}: not mono s16le")
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0, sr


PART_SUFFIX = ".part"    # the in-flight name; never a .wav, so never a sibling


def write_wav(path, samples, sr):
    """Same format out as in: WAV, PCM s16le, mono. Clamped before the cast --
    a float that rounds past full scale must not wrap to the opposite rail.

    Atomic (#601): the bytes go to `<path>.part`, are fsynced, and the name
    is swapped in with os.replace(), so a reader -- or a power cut -- never
    sees a half-written or empty file under the real name. A stray `.part`
    from a crash mid-write is not a `.wav`: the worklist and the route never
    see it, and retention ages it out like any file."""
    pcm = np.clip(np.asarray(samples) * 32768.0, -32768, 32767)
    path = str(path)
    part = path + PART_SUFFIX
    with open(part, "wb") as f:
        with wave.open(f, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(gate.BYTES_PER_SAMPLE)
            w.setframerate(sr)
            w.writeframes(pcm.astype("<i2").tobytes())
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, path)


# --- The worklist ------------------------------------------------------------

def enhanced_relpath(relpath):
    """'amcrest/1752900000-Blue_Jay.wav' -> 'amcrest/1752900000-Blue_Jay-enh.wav'.
    A sibling, so the /clips route serves it by path like any other clip and
    the retention prune finds it beside its original."""
    return relpath[:-len(".wav")] + ENH_SUFFIX


def is_enhanced(relpath):
    """Ours, not an original. The one place the naming rule is spelled out;
    the prune and the worklist both defer to it."""
    return relpath.endswith(ENH_SUFFIX)


def needs_enhance(files, force=False):
    """Which clips this run should enhance. `files` is {relpath: mtime} --
    pure with the filesystem injected, the prune_selection precedent.

    On the list: any original .wav with no sibling, or whose sibling is older
    than it (rewritten by a later `best` verdict). Off the list: our own
    siblings, the species/ portrait shelf, and anything already current.
    Re-running is therefore a no-op, which is the whole idempotence claim.

    `force` (#599) lists every original regardless of its sibling: the hand
    step after a parameter change, since mtime alone would keep every
    existing sibling at the old parameters forever. The skips still hold --
    a forced run never enhances a sibling or a portrait."""
    out = []
    for relpath, mtime in sorted(files.items()):
        if not relpath.endswith(".wav") or is_enhanced(relpath):
            continue
        if relpath.startswith(SPECIES_DIR + "/"):
            continue
        sibling = files.get(enhanced_relpath(relpath))
        if force or sibling is None or sibling < mtime:
            out.append(relpath)
    return out


def list_clip_files(media_dir):
    """{posix relpath: mtime} under the clips dir. A missing dir is empty --
    a box that never wrote a clip has nothing to enhance, not an error.

    An EMPTY sibling is left out of the listing (#601): a power cut can
    leave the name with no bytes and a fresh mtime, and listing it would
    tell needs_enhance() the original is current. Absent from the listing,
    the original is back on the worklist and the next tick rewrites it --
    needs_enhance() stays pure and unchanged."""
    out = {}
    for root, _, names in os.walk(media_dir):
        for name in names:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, media_dir).replace(os.sep, "/")
            st = os.stat(full)
            if is_enhanced(rel) and st.st_size == 0:
                continue
            out[rel] = st.st_mtime
    return out


def worklist(media_dir, force=False):
    return needs_enhance(list_clip_files(media_dir), force=force)


# --- The per-clip function (what a future refresh button calls) --------------

def enhance_clip(media_dir, relpath):
    """Enhance one clip, writing its sibling. Returns a status word:
      'enhanced' -- a sibling was written
      'short'    -- too little audio to do anything meaningful with
    Exceptions propagate: the bulk loop logs and moves on, a single-clip
    --refresh should fail loudly. The original is opened read-only and is
    never written, moved, or deleted by this function."""
    source = os.path.join(media_dir, relpath)
    samples, sr = read_wav(source)
    if samples.size < N_FFT:
        return "short"
    write_wav(os.path.join(media_dir, enhanced_relpath(relpath)),
              enhance(samples, sr), sr)
    return "enhanced"


# --- The bulk CLI (a thin loop, per the reusable-pass rule) ------------------

def main():
    ap = argparse.ArgumentParser(
        description="Band-limit, denoise and normalize Earl's clips (#190)")
    ap.add_argument("--clips", default=None,
                    help="clip dir (default: $MERLE_EARL_CLIPS or 'clips')")
    ap.add_argument("--refresh", metavar="RELPATH",
                    help="re-enhance one clip, whatever its sibling's age")
    ap.add_argument("--force", action="store_true",
                    help="re-enhance every original, whatever its sibling's "
                         "age (the hand step after a parameter change)")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after N clips")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be enhanced; write nothing")
    args = ap.parse_args()

    media = args.clips or clips_dir()
    todo = [args.refresh] if args.refresh else worklist(media, force=args.force)
    if args.limit is not None:
        todo = todo[:args.limit]
    if not todo:
        print("[enhance] nothing to do -- every clip has a current sibling",
              flush=True)
        return

    if args.dry_run:
        print(f"[enhance] {len(todo)} clips would be enhanced under {media}:",
              flush=True)
        for relpath in todo:
            print(f"[enhance]   {relpath} -> {enhanced_relpath(relpath)}",
                  flush=True)
        return

    print(f"[enhance] {len(todo)} clips to enhance under {media}", flush=True)
    counts = {}
    for relpath in todo:
        try:
            status = enhance_clip(media, relpath)
        except Exception as e:
            if args.refresh:
                raise
            print(f"[enhance] {relpath}: FAILED ({e}) -- staying on the "
                  "worklist", flush=True)
            counts["failed"] = counts.get("failed", 0) + 1
            continue
        counts[status] = counts.get(status, 0) + 1
        if status == "short":
            print(f"[enhance] {relpath}: too short to enhance", flush=True)
    print("[enhance] done: " + ", ".join(
        f"{v} {k}" for k, v in sorted(counts.items())), flush=True)


if __name__ == "__main__":
    main()
