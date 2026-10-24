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


# --- 0. the de-hum (#609) ----------------------------------------------------
# Kestrel's window-mic hears the power line: tones on the odd multiples of a
# slightly-off-60 Hz fundamental, 2.5-11.5 kHz. Synthesized the same way here
# -- a 9 s clip, the length write_clip() stores, so the transform resolves
# lines as finely as it does on a real clip.

CLIP_S = 9.0
LINE_HALF_HZ = 0.5        # what counts as "on a harmonic" when we listen


def mains_comb(f0=60.013, harmonics=range(41, 191, 2), amp=0.002,
               seconds=CLIP_S, seed=609, wander_hz=0.0):
    """`wander_hz` lets the grid drift sinusoidally within the clip, as
    kestrel's measured: tens of degrees a second at 5 kHz."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    cycles = f0 * t + wander_hz * 7 / (2 * np.pi) * (1 - np.cos(2 * np.pi * t / 7))
    return amp * sum(np.sin(2 * np.pi * k * cycles + rng.uniform(0, 2 * np.pi))
                     for k in harmonics)


def chirp(lo_hz=3000, hi_hz=5000, seconds=1.0, amp=0.1):
    """A bird-like upsweep: its energy never sits still on one bin."""
    t = np.arange(int(seconds * SR)) / SR
    return amp * np.sin(2 * np.pi * (lo_hz * t + (hi_hz - lo_hz) * t ** 2
                                      / (2 * seconds)))


def hiss(sigma=0.001, seconds=CLIP_S, seed=7):
    return np.random.default_rng(seed).normal(0, sigma, int(seconds * SR))


def comb_share(y, f0):
    """The share of y's kept-band energy that sits on the comb's harmonics --
    what the ear hears as the buzz."""
    power = np.abs(np.fft.rfft(y)) ** 2
    freqs = np.fft.rfftfreq(y.size, 1 / SR)
    band = (freqs >= ce.BAND_PASS_HZ) & (freqs <= ce.BAND_TOP_PASS_HZ)
    k = np.rint(freqs / f0)
    on = band & (k >= 1) & (np.abs(freqs - k * f0) <= LINE_HALF_HZ)
    return power[on].sum() / power[band].sum()


def test_detect_mains_finds_an_off_nominal_60hz_comb():
    """The grid wanders; kestrel's clips fit 59.95-60.04 Hz. The fundamental
    has to be found to the mHz, or the 166th harmonic misses its notch."""
    f0 = ce.detect_mains(mains_comb(f0=60.013) + hiss(), SR)
    assert f0 == pytest.approx(60.013, abs=0.002)


def test_detect_mains_finds_a_50hz_comb():
    """Portability: a station on a 50 Hz grid, every harmonic present."""
    f0 = ce.detect_mains(mains_comb(f0=49.987, harmonics=range(30, 200))
                         + hiss(), SR)
    assert f0 == pytest.approx(49.987, abs=0.002)


def test_detect_mains_says_none_without_a_comb():
    """Noise alone, a sweeping bird, and -- the case the median exists for --
    one loud steady whistle parked exactly on a multiple of 60 Hz. None of
    them is a comb across the band."""
    assert ce.detect_mains(hiss(), SR) is None
    bird = np.concatenate([np.zeros(4 * SR), chirp(amp=0.2),
                           np.zeros(4 * SR)])
    assert ce.detect_mains(bird + hiss(), SR) is None
    whistle = tone(67 * 60.0, seconds=CLIP_S, amp=0.3)
    assert ce.detect_mains(whistle + hiss(), SR) is None
    assert ce.detect_mains(np.zeros(int(CLIP_S * SR)), SR) is None


def test_detect_mains_needs_a_second_of_audio():
    assert ce.detect_mains(mains_comb(seconds=0.5), SR) is None


def test_dehum_removes_a_steady_comb_everywhere_including_the_edges():
    """At least 30 dB off the whole clip, and the first and last quarter
    second as clean as the middle. A whole-clip notch measured -16 dB here,
    with the leftover piled up at both ends (+23 dB over the middle)."""
    comb = mains_comb()
    out = ce.dehum(comb, SR, 60.013)
    assert out.size == comb.size
    assert rms(out) < 10 ** (-30 / 20) * rms(comb)
    q = SR // 4
    ends = rms(np.concatenate([out[:q], out[-q:]]))
    assert ends < 10 ** (-30 / 20) * rms(comb)


def test_dehum_follows_a_wandering_grid():
    """The grid drifts within a clip; the tracked phase follows it. A fixed
    f0 template smears the high harmonics and keeps most of the comb."""
    comb = mains_comb(f0=60.01, wander_hz=0.002) + hiss(sigma=0.002)
    f0 = ce.detect_mains(comb, SR)
    assert f0 is not None
    clean = mains_comb(f0=60.01, wander_hz=0.002)
    residual = ce.dehum(comb, SR, f0) - (comb - clean)
    assert rms(residual) < 10 ** (-25 / 20) * rms(clean)


def test_dehum_spares_the_bird():
    """Only what repeats in step with the grid for the whole clip goes. A
    sweeping chirp loses under 0.2 dB, and even a steady whistle parked
    exactly on a harmonic for a second loses under 1 dB (a notch: 4)."""
    bird = np.concatenate([np.zeros(4 * SR), chirp(), np.zeros(4 * SR)])
    assert 20 * np.log10(rms(ce.dehum(bird, SR, 60.013)) / rms(bird)) > -0.2
    whistle = np.concatenate([np.zeros(4 * SR),
                              tone(67 * 60.013, amp=0.1) * np.hanning(SR),
                              np.zeros(4 * SR)])
    kept = rms(ce.dehum(whistle, SR, 60.013)) / rms(whistle)
    assert 20 * np.log10(kept) > -1.0


def test_enhance_without_a_comb_is_the_v2_chain_byte_for_byte():
    """The containment guarantee: a clip with no hum takes exactly the path
    it took before #609, so every comb-free mic's siblings are unchanged."""
    rng = np.random.default_rng(190)
    x = (tone(200, seconds=CLIP_S, amp=0.5) + rng.normal(0, 0.01, int(CLIP_S * SR))
         + np.concatenate([np.zeros(4 * SR), chirp(amp=0.02), np.zeros(4 * SR)]))
    banded = ce.band_limit(x, SR)
    pre = banded[:int(gate_window_s() * SR)]
    v2 = ce.normalize(ce.spectral_subtract(banded, ce.noise_profile(pre)),
                      ref=banded)
    assert ce.detect_mains(x, SR) is None
    assert np.array_equal(ce.enhance(x, SR), v2)


def gate_window_s():
    from listener import gate
    return gate.WINDOW_S


def test_enhance_takes_the_buzz_out_before_the_gain():
    """The bug, as a regression: a faint bird over hum. Through v2 alone the
    floor keeps the comb and the normalize lifts it with the bird, so it is a
    large share of the result; with the de-hum the result's share is what
    the same clip without any hum scores (any sound puts ~1.7% of its band
    within half a hertz of some multiple of 60)."""
    bird = np.concatenate([np.zeros(3 * SR), chirp(amp=0.004, seconds=3.0),
                           np.zeros(3 * SR)])
    clean = bird + hiss(sigma=0.0005)
    x = mains_comb() + clean
    f0 = ce.detect_mains(x, SR)
    assert f0 == pytest.approx(60.013, abs=0.002)

    banded = ce.band_limit(x, SR)
    v2_only = ce.normalize(ce.spectral_subtract(
        banded, ce.noise_profile(banded[:3 * SR])), ref=banded)
    assert comb_share(v2_only, f0) > 0.20
    baseline = comb_share(ce.enhance(clean, SR), f0)
    assert comb_share(ce.enhance(x, SR), f0) < baseline + 0.005


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


def test_an_empty_newer_sibling_is_no_sibling(tmp_path):
    """#601: a power cut leaves a sibling's name with zero bytes and a fresh
    mtime. By mtime alone it is 'current' and the original never heals; the
    listing leaves an empty sibling out, so the original is back on the
    worklist and the next tick rewrites it."""
    clip(tmp_path, "amcrest/100-Blue_Jay.wav", tone(4000, 3, 0.3), mtime=500)
    empty = tmp_path / "amcrest/100-Blue_Jay-enh.wav"
    empty.write_bytes(b"")
    import os
    os.utime(empty, (900, 900))
    assert ce.worklist(str(tmp_path)) == ["amcrest/100-Blue_Jay.wav"]
    ce.enhance_clip(str(tmp_path), "amcrest/100-Blue_Jay.wav")
    assert empty.stat().st_size > 44
    assert ce.worklist(str(tmp_path)) == []


def test_write_wav_lands_whole_with_no_part_left_behind(tmp_path):
    """#601: the bytes go to a .part and are renamed in; after the call the
    target is a readable WAV and the in-flight name is gone. (The crash
    atomicity itself is the OS's promise, exempt beyond this.)"""
    path = tmp_path / "amcrest/100-Blue_Jay-enh.wav"
    path.parent.mkdir(parents=True)
    ce.write_wav(path, tone(4000, 1, 0.3), SR)
    samples, sr = ce.read_wav(path)
    assert sr == SR and samples.size == SR
    assert not (tmp_path / "amcrest/100-Blue_Jay-enh.wav.part").exists()
    assert sorted(p.name for p in path.parent.iterdir()) == ["100-Blue_Jay-enh.wav"]


def test_enhanced_relpath_is_a_sibling():
    assert ce.enhanced_relpath("amcrest/100-Blue_Jay.wav") == (
        "amcrest/100-Blue_Jay-enh.wav")
    assert ce.is_enhanced("amcrest/100-Blue_Jay-enh.wav")
    assert not ce.is_enhanced("amcrest/100-Blue_Jay.wav")
