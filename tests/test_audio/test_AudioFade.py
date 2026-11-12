"""Regression tests for the AudioFadeIn / AudioFadeOut effects.

The fades must behave identically whatever the number of channels
(mono, stereo, 4 channels, 6 channels) and whatever the shape of the
times passed to ``get_frame`` (scalar or numpy vector).
"""

import wave

import numpy as np
import pytest

from moviepy import AudioClip
from moviepy.audio.AudioClip import (
    CompositeAudioClip,
    concatenate_audioclips,
)
from moviepy.audio.fx.AudioFadeIn import AudioFadeIn
from moviepy.audio.fx.AudioFadeOut import AudioFadeOut


CLIP_DURATION = 4.0
FADE_DURATION = 1.0
SAMPLE_TIMES = np.array([0.0, 0.1, 0.25, 0.5, 0.75, 1.0, 1.25, 2.0, 3.5])


def make_clip(nchannels, duration=CLIP_DURATION, fps=8000):
    """Build an AudioClip with a deterministic, channel-specific frame.

    The frame_function follows the documented conventions:

    * scalar time -> a float (mono) or a ``(C,)`` array (multi-channel),
    * vector time -> an ``(N,)`` array (mono) or ``(N, C)`` array.
    """
    if nchannels == 1:

        def frame_function(t):
            is_scalar = not isinstance(t, np.ndarray)
            t = np.asarray(t, dtype=float)
            frame = 0.5 * np.sin(2 * np.pi * 440 * t) + 0.25
            return float(frame) if is_scalar else frame

    else:

        def frame_function(t):
            t = np.asarray(t, dtype=float)
            channels = [
                (c + 1) / nchannels
                * (0.5 * np.sin(2 * np.pi * 220 * (c + 1) * t) + 0.5)
                for c in range(nchannels)
            ]
            # shape (C,) for scalar t, (N, C) for vector t
            return np.stack(channels, axis=-1)

    clip = AudioClip(frame_function, duration=duration, fps=fps)
    # AudioClip's auto-detection probes with an int 0 which the time
    # decorator converts to a 0-d numpy scalar; a 0-d array is iterable but
    # has no length, so the detection falls back to nchannels=1. Set the
    # real channel count explicitly.
    clip.nchannels = nchannels
    assert clip.nchannels == nchannels
    return clip


def expected_fadein_factor(t, fade_duration):
    return np.clip(np.asarray(t, dtype=float) / fade_duration, 0, 1)


def expected_fadeout_factor(t, clip_duration, fade_duration):
    return np.clip((clip_duration - np.asarray(t, dtype=float)) / fade_duration, 0, 1)


def expected_fadein_frame(clip, times, fade_duration):
    """What a fade-in of ``clip`` at ``times`` should numerically produce."""
    factor = expected_fadein_factor(times, fade_duration)
    if clip.nchannels > 1:
        factor = factor[:, None]
    return factor * clip.get_frame(times)


def expected_fadeout_frame(clip, times, fade_duration):
    """What a fade-out of ``clip`` at ``times`` should numerically produce."""
    factor = expected_fadeout_factor(times, clip.duration, fade_duration)
    if clip.nchannels > 1:
        factor = factor[:, None]
    return factor * clip.get_frame(times)


def as_vector_frames(clip, times):
    """Call get_frame once on the vector and once per scalar time."""
    vector_frames = clip.get_frame(times)
    scalar_frames = np.stack(
        [np.asarray(clip.get_frame(float(t)), dtype=float) for t in times]
    )
    return vector_frames, scalar_frames


@pytest.mark.parametrize("nchannels", [1, 2, 4, 6])
@pytest.mark.parametrize("effect_cls", [AudioFadeIn, AudioFadeOut])
def test_fade_preserves_frame_shape(nchannels, effect_cls):
    clip = make_clip(nchannels)
    faded = clip.with_effects([effect_cls(FADE_DURATION)])

    # scalar time keeps the scalar / (C,) shape of the source frame
    scalar_frame = faded.get_frame(0.5)
    raw_scalar_frame = clip.get_frame(0.5)
    assert np.shape(scalar_frame) == np.shape(raw_scalar_frame)

    # vector time keeps the (N,) / (N, C) shape of the source frame
    vector_frame, _ = as_vector_frames(faded, SAMPLE_TIMES)
    raw_vector_frame = clip.get_frame(SAMPLE_TIMES)
    assert vector_frame.shape == raw_vector_frame.shape
    assert faded.nchannels == nchannels

    # mono must not be duplicated into a trailing channel axis
    if nchannels == 1:
        assert vector_frame.ndim == 1
        assert np.ndim(scalar_frame) == 0


@pytest.mark.parametrize("nchannels", [1, 2, 4, 6])
def test_fadein_factors_and_scalar_vector_consistency(nchannels):
    clip = make_clip(nchannels)
    faded = clip.with_effects([AudioFadeIn(FADE_DURATION)])

    vector_frames, scalar_frames = as_vector_frames(faded, SAMPLE_TIMES)
    np.testing.assert_allclose(vector_frames, scalar_frames, atol=1e-12)
    np.testing.assert_allclose(
        vector_frames,
        expected_fadein_frame(clip, SAMPLE_TIMES, FADE_DURATION),
        atol=1e-12,
    )

    # outside the fade-in window the gain is clipped: no amplification,
    # no negative / phase-inverted gain before the clip start
    beyond = np.array([-1.0, -0.01, FADE_DURATION, CLIP_DURATION - 0.01])
    frames = faded.get_frame(beyond)
    np.testing.assert_allclose(
        frames, expected_fadein_frame(clip, beyond, FADE_DURATION), atol=1e-12
    )
    assert np.all(frames * clip.get_frame(beyond) >= 0)
    np.testing.assert_allclose(
        faded.get_frame(FADE_DURATION + 0.5),
        clip.get_frame(FADE_DURATION + 0.5),
        atol=1e-12,
    )


@pytest.mark.parametrize("nchannels", [1, 2, 4, 6])
def test_fadeout_factors_and_scalar_vector_consistency(nchannels):
    clip = make_clip(nchannels)
    faded = clip.with_effects([AudioFadeOut(FADE_DURATION)])

    vector_frames, scalar_frames = as_vector_frames(faded, SAMPLE_TIMES)
    np.testing.assert_allclose(vector_frames, scalar_frames, atol=1e-12)
    np.testing.assert_allclose(
        vector_frames,
        expected_fadeout_frame(clip, SAMPLE_TIMES, FADE_DURATION),
        atol=1e-12,
    )

    # outside the fade-out window the gain is clipped: full volume before,
    # silence at / after the end, never negative
    beyond = np.array(
        [0.0, CLIP_DURATION - FADE_DURATION - 0.01, CLIP_DURATION, CLIP_DURATION + 1.0]
    )
    frames = faded.get_frame(beyond)
    np.testing.assert_allclose(
        frames, expected_fadeout_frame(clip, beyond, FADE_DURATION), atol=1e-12
    )
    assert np.all(frames * clip.get_frame(beyond) >= 0)
    np.testing.assert_allclose(
        faded.get_frame(0.5), clip.get_frame(0.5), atol=1e-12
    )
    np.testing.assert_allclose(
        faded.get_frame(CLIP_DURATION),
        np.zeros(np.shape(faded.get_frame(CLIP_DURATION))),
        atol=1e-12,
    )


@pytest.mark.parametrize("effect_cls", [AudioFadeIn, AudioFadeOut])
@pytest.mark.parametrize(
    "bad_duration", [0, -0.5, float("nan"), float("inf"), float("-inf")]
)
def test_invalid_fade_duration_raises(effect_cls, bad_duration):
    with pytest.raises(ValueError, match="duration"):
        effect_cls(bad_duration)


def test_string_fade_duration_still_accepted():
    # convert_to_seconds support must keep working
    clip = make_clip(1)
    faded = clip.with_effects([AudioFadeIn("00:00:01")])
    np.testing.assert_allclose(
        faded.get_frame(0.5), 0.5 * clip.get_frame(0.5), atol=1e-12
    )


@pytest.mark.parametrize("effect_cls", [AudioFadeIn, AudioFadeOut])
def test_empty_time_vector(effect_cls):
    clip = make_clip(2)
    faded = clip.with_effects([effect_cls(FADE_DURATION)])
    empty = faded.get_frame(np.array([]))
    assert empty.shape == (0, 2)

    mono = make_clip(1)
    faded_mono = mono.with_effects([effect_cls(FADE_DURATION)])
    empty_mono = faded_mono.get_frame(np.array([]))
    assert empty_mono.shape == (0,)


@pytest.mark.parametrize("nchannels", [1, 2, 4, 6])
def test_effect_instance_reuse_across_clips(nchannels):
    """One effect instance reused on different clips must not leak state."""
    clip_a = make_clip(nchannels, duration=2.0)
    clip_b = make_clip(nchannels, duration=6.0)

    fadein = AudioFadeIn(FADE_DURATION)
    faded_a = clip_a.with_effects([fadein])
    faded_b = clip_b.with_effects([fadein])

    # the shared effect instance itself stays untouched and reusable
    assert fadein.duration == FADE_DURATION

    times_a = np.linspace(0, 2.0, 9)
    np.testing.assert_allclose(
        faded_a.get_frame(times_a),
        expected_fadein_frame(clip_a, times_a, FADE_DURATION),
        atol=1e-12,
    )
    times_b = np.linspace(0, 6.0, 13)
    np.testing.assert_allclose(
        faded_b.get_frame(times_b),
        expected_fadein_frame(clip_b, times_b, FADE_DURATION),
        atol=1e-12,
    )

    # same check for fade-out, whose factor depends on the clip duration:
    # the second clip's 6s duration must not be cached from the first clip.
    fadeout = AudioFadeOut(FADE_DURATION)
    out_a = clip_a.with_effects([fadeout])
    out_b = clip_b.with_effects([fadeout])
    t = 1.5  # inside the 2s clip, far from the end of the 6s clip
    np.testing.assert_allclose(
        out_b.get_frame(t), clip_b.get_frame(t), atol=1e-12
    )
    np.testing.assert_allclose(out_a.get_frame(2.0), 0.0, atol=1e-12)
    assert fadeout.duration == FADE_DURATION


def test_reuse_across_different_channel_counts():
    mono_clip = make_clip(1)
    stereo_clip = make_clip(2)
    effect = AudioFadeIn(FADE_DURATION)

    faded_mono = mono_clip.with_effects([effect])
    faded_stereo = stereo_clip.with_effects([effect])

    assert faded_mono.get_frame(np.array([0.25, 0.5])).shape == (2,)
    assert faded_stereo.get_frame(np.array([0.25, 0.5])).shape == (2, 2)
    np.testing.assert_allclose(
        faded_mono.get_frame(0.5), 0.5 * mono_clip.get_frame(0.5), atol=1e-12
    )


@pytest.mark.parametrize("nchannels", [1, 2])
def test_fade_with_start_inside_composite(nchannels):
    """The fade is computed in local clip time, not against absolute start."""
    start = 10.0
    clip = make_clip(nchannels).with_start(start)
    faded = clip.with_effects([AudioFadeIn(FADE_DURATION)])
    composite = CompositeAudioClip([faded])

    local_times = np.array([0.0, 0.25, 0.5, 1.0, 1.5])
    absolute_times = local_times + start

    frames = composite.get_frame(absolute_times)
    raw = clip.get_frame(local_times)
    factor = expected_fadein_factor(local_times, FADE_DURATION)
    if nchannels > 1:
        factor = factor[:, None]
    np.testing.assert_allclose(frames, factor * raw, atol=1e-12)
    assert frames.shape == raw.shape


def test_fade_on_subclipped_clip_uses_local_time():
    clip = make_clip(1).subclipped(1.0, 3.0)  # local duration 2s
    assert clip.duration == 2.0
    faded = clip.with_effects([AudioFadeOut(1.0)])

    times = np.array([0.0, 0.5, 1.0, 1.5, 2.0])
    factor = expected_fadeout_factor(times, 2.0, 1.0)
    np.testing.assert_allclose(
        faded.get_frame(times), factor * clip.get_frame(times), atol=1e-12
    )
    # the subclip starts 1s into the source; the fade-out must be aligned
    # to the subclip end, not the original clip's absolute timeline
    np.testing.assert_allclose(faded.get_frame(2.0), 0.0, atol=1e-12)


@pytest.mark.parametrize("nchannels", [1, 2])
def test_fade_on_composite_and_concatenation(nchannels):
    clip_a = make_clip(nchannels, duration=2.0)
    clip_b = make_clip(nchannels, duration=2.0)
    concatenated = concatenate_audioclips([clip_a, clip_b])
    assert concatenated.duration == 4.0

    faded = concatenated.with_effects(
        [AudioFadeIn(1.0), AudioFadeOut(1.0)]
    )

    times = np.array([0.0, 0.5, 1.0, 2.0, 3.0, 3.5, 4.0])
    frames = faded.get_frame(times)
    raw = concatenated.get_frame(times)
    factor_in = expected_fadein_factor(times, 1.0)
    factor_out = expected_fadeout_factor(times, 4.0, 1.0)
    factor = factor_in * factor_out
    if nchannels > 1:
        factor = factor[:, None]
    np.testing.assert_allclose(frames, factor * raw, atol=1e-12)
    assert frames.shape == raw.shape

    # silence at both ends
    assert np.allclose(faded.get_frame(0.0), 0.0, atol=1e-12)
    assert np.allclose(faded.get_frame(4.0), 0.0, atol=1e-12)

    # also works when the faded clip itself is embedded in a composite
    composite = CompositeAudioClip([faded])
    assert composite.nchannels == nchannels
    composite_frames = composite.get_frame(times)
    assert composite_frames.shape == frames.shape
    np.testing.assert_allclose(composite_frames, frames, atol=1e-12)


@pytest.mark.parametrize("effect_cls", [AudioFadeIn, AudioFadeOut])
def test_input_clip_and_source_array_not_modified(effect_cls, tmp_path):
    clip = make_clip(2)
    times = np.linspace(0, CLIP_DURATION, 21, endpoint=False)
    raw_before = clip.get_frame(times).copy()
    times_copy = times.copy()

    faded = clip.with_effects([effect_cls(FADE_DURATION)])
    faded.get_frame(times)
    faded.get_frame(0.33)

    np.testing.assert_array_equal(clip.get_frame(times), raw_before)
    np.testing.assert_array_equal(times, times_copy)
    # applying returns a new clip, leaving the original un-faded
    np.testing.assert_allclose(
        clip.get_frame(0.25), make_clip(2).get_frame(0.25), atol=1e-12
    )


def test_wav_export_envelope_and_endpoints(tmp_path):
    """Render an actual faded tone to WAV and check the envelope numerically."""
    fps = 22050
    duration = 2.0
    clip = AudioClip(
        lambda t: np.sin(2 * np.pi * 440 * np.asarray(t, dtype=float)),
        duration=duration,
        fps=fps,
    )
    faded = clip.with_effects([AudioFadeIn(0.5), AudioFadeOut(0.5)])
    wav_path = str(tmp_path / "faded_tone.wav")
    faded.write_audiofile(wav_path, fps=fps, logger=None)

    with wave.open(wav_path, "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == fps
        nframes = wav.getnframes()
        raw = wav.readframes(nframes)
    samples = np.frombuffer(raw, dtype=np.int16).astype(float) / 2**15

    assert nframes == int(duration * fps)

    # first and last samples are effectively silent
    assert abs(samples[0]) < 1e-3
    assert abs(samples[-1]) < 1e-3
    # the peak happens in the fully-open middle section, not at the edges
    middle = samples[int(0.75 * fps) : int(1.25 * fps)]
    assert np.max(np.abs(middle)) > 0.9
    assert np.max(np.abs(samples[: int(0.1 * fps)])) < 0.5
    assert np.max(np.abs(samples[-int(0.1 * fps) :])) < 0.5

    # numerical envelope: compare sample magnitudes against the expected
    # piecewise-linear gain, ignoring sine sign changes via local maxima
    t = np.arange(nframes) / fps
    expected_gain = expected_fadein_factor(t, 0.5) * expected_fadeout_factor(
        t, duration, 0.5
    )
    # envelope measured on windows small vs. fade slope, large vs. period
    window = fps // 440  # one 440Hz period
    n_windows = nframes // window
    measured = np.array(
        [
            np.max(np.abs(samples[i * window : (i + 1) * window]))
            for i in range(n_windows)
        ]
    )
    expected = np.array(
        [
            np.max(expected_gain[i * window : (i + 1) * window])
            for i in range(n_windows)
        ]
    )
    np.testing.assert_allclose(measured, expected, atol=0.05)
