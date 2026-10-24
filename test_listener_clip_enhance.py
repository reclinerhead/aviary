# =============================================================================
# project-squirrel -- test_listener_clip_enhance.py
#
# The clip enhancement pass (issue #190). Deterministic DSP whose regressions
# are SILENT -- a wrong result survives both a build and a casual listen --
# which is exactly the pure-logic case CLAUDE.md says to cover. Synthesized
# signals throughout, never real clips: a test that needs a Bald Eagle to
# have flown over is not a test.
#
# The load-bearing one is test_normalize_runs_last_so_the_bird_is_loud: it
# guards the ordering that is the entire design rationale. Normalizing before
# the filtering normalizes *the plane*, and the bird stays exactly as faint
# in relative terms. That bug would look completely fine in review.
# =============================================================================

import numpy as np
import pytest

from listener import clip_enhance as ce

SR = 48000


def tone(hz, seconds=1.0, amp=1.0, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    return amp * np.sin(2 * np.pi * hz * t)


def rms(x):
    return float(np.sqrt(np.mean(np.square(x))))


# --- 1. the band-limit -------------------------------------------------------

def test_band_limit_kills_low_frequency_noise():
    """A 200 Hz tone is the plane, the truck, the furnace. It should go."""
    out = ce.band_limit(tone(200), SR)
    assert rms(out) < 0.01 * rms(tone(200))


def test_band_limit_passes_the_birds_band():
    """4 kHz is where the song lives; it must come through substantially
    intact, not merely survive."""
    bird = tone(4000)
    assert rms(ce.band_limit(bird, SR)) == pytest.approx(rms(bird), rel=0.02)


def test_band_limit_handles_an_empty_clip():
    assert ce.band_limit(np.array([]), SR).size == 0


def test_band_limit_has_a_top_edge_for_hiss():
    """#599: yard song tops out below 10 kHz, microphone hiss does not. A
    14 kHz tone is gone (> 40 dB down); an 8 kHz tone is untouched."""
    hiss = tone(14000)
    assert rms(ce.band_limit(hiss, SR)) < 0.01 * rms(hiss)
    high_song = tone(8000)
    assert rms(ce.band_limit(high_song, SR)) == pytest.approx(
        rms(high_song), rel=0.02)


# --- 2. the spectral subtraction ---------------------------------------------

def test_spectral_subtraction_keeps_the_tone_and_drops_the_floor():
    """signal = known noise + known tone. The tone survives; the noise floor
    measurably drops. Fixed seed -- a flaky DSP test teaches nothing."""
    rng = np.random.default_rng(190)
    noise = rng.normal(0, 0.1, SR)
    bird = tone(4000, amp=0.2)
    profile = ce.noise_profile(rng.normal(0, 0.1, SR))

    out = ce.spectral_subtract(noise + bird, profile)

    # The noise-only stretch got quieter...
    assert rms(ce.spectral_subtract(noise, profile)) < 0.5 * rms(noise)
    # ...while the tone is still the dominant thing in the result.
    spec = np.abs(np.fft.rfft(out))
    peak_hz = np.fft.rfftfreq(out.size, 1 / SR)[int(np.argmax(spec))]
    assert peak_hz == pytest.approx(4000, abs=20)


def test_the_floor_holds_and_the_subtraction_still_subtracts():
    """#599: the floor is 0.30 of each bin. Noise-only input comes out
    quieter -- the subtraction did its job -- but never below the floor,
    which is the residual bed that masks musical noise."""
    rng = np.random.default_rng(599)
    noise = rng.normal(0, 0.1, SR)
    profile = ce.noise_profile(rng.normal(0, 0.1, SR))
    out = ce.spectral_subtract(noise, profile)
    assert 0.25 * rms(noise) <= rms(out) < 0.6 * rms(noise)


def test_the_smoothed_mask_has_fewer_spikes():
    """#599: the gain mask is box-smoothed over frames and bins. Smoothing
    keeps every gain inside [floor, 1] and lowers the mask's frame-to-frame
    scatter -- and, the claim that matters, the OUTPUT's per-bin speckle
    (std/mean across frames) drops too. Fixed seed, as always."""
    rng = np.random.default_rng(190)
    noise = rng.normal(0, 0.1, SR)
    profile = ce.noise_profile(rng.normal(0, 0.1, SR))
    mag = np.abs(ce.stft(noise))

    rough = ce.subtraction_mask(mag, profile, smooth=None)
    smooth = ce.subtraction_mask(mag, profile)
    for mask in (rough, smooth):
        assert mask.min() >= ce.SPECTRAL_FLOOR - 1e-9
        assert mask.max() <= 1.0 + 1e-9
    assert smooth.std(axis=0).mean() < 0.5 * rough.std(axis=0).mean()

    def speckle(x):
        s = np.abs(ce.stft(x))
        return float(np.median(s.std(axis=0) / np.maximum(s.mean(axis=0),
                                                           1e-9)))
    assert speckle(ce.spectral_subtract(noise, profile)) < speckle(
        ce.spectral_subtract(noise, profile, smooth=None))


def test_the_stft_round_trips():
    """istft(stft(x)) is the identity, or the subtraction is measuring one
    signal and rebuilding a different one."""
    x = tone(3000, seconds=0.5) * 0.4
    back = ce.istft(ce.stft(x), x.size)
    assert np.allclose(back, x, atol=1e-9)


# --- 3. normalization, and THE ORDERING ---------------------------------------

def test_normalize_reaches_the_target_peak():
    assert np.max(np.abs(ce.normalize(tone(4000, amp=0.01)))) == pytest.approx(
        ce.PEAK)


def test_normalize_leaves_silence_silent():
    assert np.max(np.abs(ce.normalize(np.zeros(1000)))) == 0.0


def test_normalize_is_capped_against_its_reference():
    """#599: with a reference, the gain may exceed the reference's own plain
    normalize by at most GAIN_CAP_DB. A residue 60 dB under its reference
    is lifted by exactly the cap; a clip the cap does not reach still hits
    PEAK; the output never exceeds PEAK either way."""
    ref = tone(4000, amp=1.0)
    residue = tone(4000, amp=0.001)
    out = ce.normalize(residue, ref=ref)
    expected = 0.001 * ce.PEAK * 10 ** (ce.GAIN_CAP_DB / 20)
    assert np.max(np.abs(out)) == pytest.approx(expected, rel=1e-6)

    not_bound = ce.normalize(tone(4000, amp=0.5), ref=tone(4000, amp=0.6))
    assert np.max(np.abs(not_bound)) == pytest.approx(ce.PEAK)
    assert np.max(np.abs(out)) <= ce.PEAK


def test_normalize_without_a_reference_is_the_v1_function():
    x = tone(4000, amp=0.01)
    assert np.array_equal(ce.normalize(x), x * (ce.PEAK / np.max(np.abs(x))))


def test_normalize_runs_last_so_the_bird_is_loud():
    """THE ordering guard, and the reason this pass exists.

    A loud 200 Hz plane with a faint 4 kHz bird under it. After the full
    chain the BIRD must be what reaches near full scale. Normalizing before
    filtering would normalize the plane and leave the bird at ~1% -- audibly
    useless, and invisible to every other check we run."""
    plane = tone(200, seconds=3, amp=0.9)
    bird = np.concatenate([np.zeros(SR), tone(4000, seconds=1, amp=0.01),
                           np.zeros(SR)])

    out = ce.enhance(plane + bird, SR, preroll_s=1.0)

    # The bird's second is now near full scale...
    assert np.max(np.abs(out[SR:2 * SR])) > 0.5
    # ...and the surviving energy is the bird's, not the plane's.
    spec = np.abs(np.fft.rfft(out[SR:2 * SR]))
    peak_hz = np.fft.rfftfreq(SR, 1 / SR)[int(np.argmax(spec))]
    assert peak_hz == pytest.approx(4000, abs=20)


def test_enhance_without_a_preroll_still_band_limits_and_normalizes():
    """A clip too short to carry a prev window skips the subtraction rather
    than estimating the noise from the bird itself."""
    out = ce.enhance(tone(4000, seconds=0.01, amp=0.001), SR)
    assert np.max(np.abs(out)) == pytest.approx(ce.PEAK)


# --- 4. idempotence, at the file level ---------------------------------------

def clip(tmp_path, relpath, samples, mtime=None):
    path = tmp_path / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    ce.write_wav(path, samples, SR)
    if mtime is not None:
        import os
        os.utime(path, (mtime, mtime))
    return path


def test_enhancing_twice_yields_identical_bytes(tmp_path):
    clip(tmp_path, "amcrest/100-Blue_Jay.wav",
         tone(200, seconds=3, amp=0.8) + tone(4000, seconds=3, amp=0.02))
    ce.enhance_clip(str(tmp_path), "amcrest/100-Blue_Jay.wav")
    first = (tmp_path / "amcrest/100-Blue_Jay-enh.wav").read_bytes()
    ce.enhance_clip(str(tmp_path), "amcrest/100-Blue_Jay.wav")
    assert (tmp_path / "amcrest/100-Blue_Jay-enh.wav").read_bytes() == first


def test_the_original_is_never_touched(tmp_path):
    path = clip(tmp_path, "amcrest/100-Blue_Jay.wav", tone(4000, 3, 0.3))
    before = path.read_bytes()
    ce.enhance_clip(str(tmp_path), "amcrest/100-Blue_Jay.wav")
    assert path.exists() and path.read_bytes() == before


def test_a_clip_too_short_to_enhance_says_so(tmp_path):
    clip(tmp_path, "amcrest/100-Blue_Jay.wav", np.zeros(64))
    assert ce.enhance_clip(str(tmp_path), "amcrest/100-Blue_Jay.wav") == "short"


# --- 5. the worklist ----------------------------------------------------------

def test_a_clip_with_no_sibling_is_on_the_worklist():
    assert ce.needs_enhance({"amcrest/100-Blue_Jay.wav": 500}) == [
        "amcrest/100-Blue_Jay.wav"]


def test_a_clip_with_a_current_sibling_is_not():
    assert ce.needs_enhance({"amcrest/100-Blue_Jay.wav": 500,
                             "amcrest/100-Blue_Jay-enh.wav": 600}) == []


def test_a_clip_rewritten_by_a_later_best_verdict_reappears():
    """VisitTracker rewrites a clip when a later window beats the visit's
    best confidence. The original is then newer than its sibling, and the
    pass picks it up on the next ordinary run -- no bookkeeping, no column."""
    assert ce.needs_enhance({"amcrest/100-Blue_Jay.wav": 900,
                             "amcrest/100-Blue_Jay-enh.wav": 600}) == [
        "amcrest/100-Blue_Jay.wav"]


def test_the_worklist_ignores_siblings_portraits_and_non_wavs():
    files = {"amcrest/100-Blue_Jay-enh.wav": 500,   # ours already
             "species/Cyanocitta_cristata.jpg": 500,  # the portrait shelf
             "amcrest/notes.txt": 500}
    assert ce.needs_enhance(files) == []


def test_force_lists_every_original_and_nothing_else():
    """#599: a parameter change re-enhances nothing by mtime, so --force
    puts every original back on the list -- current sibling or not -- while
    the skips (siblings, portraits, non-WAVs) still hold."""
    files = {"amcrest/100-Blue_Jay.wav": 500,
             "amcrest/100-Blue_Jay-enh.wav": 600,      # current sibling
             "amcrest/200-Northern_Cardinal.wav": 500,  # no sibling
             "species/Cyanocitta_cristata.wav": 500,
             "amcrest/notes.txt": 500}
    assert ce.needs_enhance(files, force=True) == [
        "amcrest/100-Blue_Jay.wav", "amcrest/200-Northern_Cardinal.wav"]
    assert ce.needs_enhance(files) == ["amcrest/200-Northern_Cardinal.wav"]


def test_the_worklist_walks_a_real_dir(tmp_path):
    clip(tmp_path, "amcrest/100-Blue_Jay.wav", tone(4000, 3, 0.3))
    assert ce.worklist(str(tmp_path)) == ["amcrest/100-Blue_Jay.wav"]


def test_enhanced_relpath_is_a_sibling():
    assert ce.enhanced_relpath("amcrest/100-Blue_Jay.wav") == (
        "amcrest/100-Blue_Jay-enh.wav")
    assert ce.is_enhanced("amcrest/100-Blue_Jay-enh.wav")
    assert not ce.is_enhanced("amcrest/100-Blue_Jay.wav")
