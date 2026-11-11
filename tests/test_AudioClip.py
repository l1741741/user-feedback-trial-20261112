"""AudioClip tests: unified half-open sampling timeline across
``to_soundarray``, ``iter_chunks`` and ``write_audiofile``."""

import os
import subprocess

import numpy as np

import pytest

from moviepy import (
    AudioArrayClip,
    AudioClip,
    CompositeAudioClip,
    concatenate_audioclips,
)


def mono_clip(duration=1.0, fps=44100, freq=440):
    return AudioClip(
        lambda t: 0.5 * np.sin(freq * 2 * np.pi * t), duration=duration, fps=fps
    )


def stereo_array_clip(n=1000, fps=1000, channels=2):
    rng = np.random.RandomState(42)
    array = rng.uniform(-0.9, 0.9, size=(n, channels))
    return AudioArrayClip(array, fps=fps), array


@pytest.mark.parametrize(
    ("duration", "fps", "expected_n"),
    (
        (1.0, 44100, 44100),
        (0.3, 44100, 13230),
        (2.4, 60, 144),
        (0.1, 100, 10),
        (1 / 3, 1000, 334),  # i/1000 < 1/3 <=> i <= 333
        (1 / 44100, 44100, 1),  # exactly one sample
        (0.0, 44100, 0),  # empty timeline
    ),
)
def test_to_soundarray_sample_count(duration, fps, expected_n):
    """Half-open timeline: samples are i/fps with i/fps < duration."""
    clip = mono_clip(duration=duration, fps=fps)
    assert len(clip.to_soundarray(fps=fps)) == expected_n


def test_to_soundarray_short_and_chunked_branches_match():
    """The short and the chunked branches of to_soundarray must return the
    exact same samples, channel axis and dtype, for mono and stereo."""
    for clip in (mono_clip(duration=0.5, fps=44100), stereo_array_clip(5000)[0]):
        for quantize, nbytes in ((False, 2), (True, 2)):
            short = clip.to_soundarray(
                fps=clip.fps, quantize=quantize, nbytes=nbytes, buffersize=10**9
            )
            chunked = clip.to_soundarray(
                fps=clip.fps, quantize=quantize, nbytes=nbytes, buffersize=1000
            )
            assert short.shape == chunked.shape
            assert short.dtype == chunked.dtype
            assert np.array_equal(short, chunked)


def test_to_soundarray_frame_count_independent_of_buffersize():
    """Floating-point stepping must not make the frame count depend on
    the buffersize."""
    clip = stereo_array_clip(n=12345, fps=44100)[0]
    reference = clip.to_soundarray(fps=44100, buffersize=10**9)
    for buffersize in (1, 7, 1000, 4096):
        chunked = clip.to_soundarray(fps=44100, buffersize=buffersize)
        assert chunked.shape == reference.shape
        assert np.array_equal(chunked, reference)


def test_iter_chunks_sizes_and_content():
    """Chunks are consecutive blocks of chunksize samples, only the last
    one may be shorter, and no empty chunk is generated."""
    clip, array = stereo_array_clip(n=1000, fps=1000)
    chunks = list(clip.iter_chunks(chunksize=300))
    assert [len(c) for c in chunks] == [300, 300, 300, 100]
    assert np.allclose(np.concatenate(chunks, axis=0), array)

    # exact multiple of chunksize: no trailing empty chunk
    chunks = list(clip.iter_chunks(chunksize=500))
    assert [len(c) for c in chunks] == [500, 500]


def test_iter_chunks_zero_duration_yields_nothing():
    clip = mono_clip(duration=0.0, fps=44100)
    assert list(clip.iter_chunks(chunksize=100)) == []


def test_iter_chunks_chunk_duration():
    """chunk_duration is converted to a chunk length of
    int(chunk_duration * fps) samples."""
    clip, _ = stereo_array_clip(n=1000, fps=1000)
    chunks = list(clip.iter_chunks(chunk_duration=0.3))
    assert [len(c) for c in chunks] == [300, 300, 300, 100]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    (
        ({"chunksize": 0}, "chunksize"),
        ({"chunksize": -3}, "chunksize"),
        ({"chunksize": None, "chunk_duration": None}, "chunksize"),
        ({"chunk_duration": 0}, "chunk_duration"),
        ({"chunk_duration": 1e-9, "fps": 10}, "chunksize"),  # rounds down to 0
        ({"chunksize": 10, "fps": 0}, "fps"),
        ({"chunksize": 10, "fps": -5}, "fps"),
    ),
)
def test_iter_chunks_invalid_parameters(kwargs, match):
    clip = mono_clip(duration=1.0, fps=44100)
    with pytest.raises(ValueError, match=match):
        list(clip.iter_chunks(**kwargs))


def test_iter_chunks_same_sequence_as_to_soundarray():
    """iter_chunks and to_soundarray sample the exact same sequence."""
    clip, _ = stereo_array_clip(n=4321, fps=22050)
    chunked = np.concatenate(list(clip.iter_chunks(chunksize=1000)), axis=0)
    direct = clip.to_soundarray(fps=22050, buffersize=10**9)
    assert chunked.shape == direct.shape
    assert np.array_equal(chunked, direct)


def test_audioarrayclip_channel_count_preserved():
    """Mono stays mono, and more than 2 channels are supported."""
    mono, _ = stereo_array_clip(n=100, channels=1)
    assert mono.nchannels == 1
    assert mono.get_frame(0.0).shape == (1,)
    assert mono.get_frame(np.array([0.0, 0.001])).shape == (2, 1)

    three, _ = stereo_array_clip(n=100, channels=3)
    assert three.nchannels == 3
    assert three.get_frame(0.0).shape == (3,)
    assert three.get_frame(np.array([0.0, 0.001])).shape == (2, 3)
    assert three.to_soundarray(fps=1000).shape == (100, 3)

    # 1-D input is treated as mono, not broadcast to stereo
    onedim = AudioArrayClip(np.zeros((50,)), fps=10)
    assert onedim.nchannels == 1
    assert onedim.to_soundarray(fps=10).shape == (50, 1)


def test_audioarrayclip_scalar_and_vector_sampling_consistent():
    """Scalar and vector sampling both use floor(t * fps) and decide the
    [0, duration) boundary on the time itself."""
    clip, array = stereo_array_clip(n=1000, fps=1000)

    tt = np.array([0.0, 0.0015, 0.1234, 0.9999])
    vector = clip.get_frame(tt)
    for i, t in enumerate(tt):
        scalar = clip.get_frame(t)
        assert scalar.shape == (2,)
        assert np.array_equal(vector[i], scalar)
        # floor sampling: t=0.0015 -> frame 1, t=0.1234 -> frame 123
        assert np.array_equal(scalar, array[int(np.floor(1000 * t))])


def test_audioarrayclip_out_of_range_times_return_zeros():
    clip, array = stereo_array_clip(n=1000, fps=1000)
    assert not np.array_equal(array[0], 0)  # make the test meaningful

    # a tiny negative time must not be truncated to the first frame
    assert np.array_equal(clip.get_frame(-1e-9), np.zeros(2))
    # past the end
    assert np.array_equal(clip.get_frame(1.0), np.zeros(2))
    assert np.array_equal(clip.get_frame(5.0), np.zeros(2))

    vector = clip.get_frame(np.array([-1e-9, 0.0, 1.0]))
    assert np.array_equal(vector[0], np.zeros(2))
    assert np.array_equal(vector[1], array[0])
    assert np.array_equal(vector[2], np.zeros(2))


def test_audioarrayclip_mono_not_duplicated_to_stereo():
    clip, array = stereo_array_clip(n=100, channels=1)
    frames = clip.get_frame(np.array([0.0, 0.001, 0.002]))
    assert frames.shape == (3, 1)
    assert np.array_equal(frames[:, 0], array[:3, 0])


@pytest.mark.parametrize(("nbytes", "dtype"), ((1, "int8"), (2, "int16"), (4, "int32")))
def test_to_soundarray_quantize_nbytes(nbytes, dtype):
    """quantize=True honors nbytes in both the short and chunked branches."""
    clip = stereo_array_clip(n=5000, fps=1000)[0]
    short = clip.to_soundarray(
        fps=1000, quantize=True, nbytes=nbytes, buffersize=10**9
    )
    chunked = clip.to_soundarray(fps=1000, quantize=True, nbytes=nbytes, buffersize=512)
    assert short.dtype == np.dtype(dtype)
    assert chunked.dtype == np.dtype(dtype)
    assert np.array_equal(short, chunked)


def test_to_soundarray_explicit_tt_samples_given_times():
    """An explicit tt samples exactly the caller-provided time points."""
    clip, array = stereo_array_clip(n=1000, fps=1000)
    tt = np.array([0.5, 0.25, 0.25, 0.0])
    sound = clip.to_soundarray(tt=tt, fps=1000)
    assert sound.shape == (4, 2)
    assert np.array_equal(sound[0], array[500])
    assert np.array_equal(sound[1], array[250])
    assert np.array_equal(sound[2], array[250])
    assert np.array_equal(sound[3], array[0])


def test_composite_audio_clip_mixed_channels():
    """A mono clip composed with a stereo clip is centered (played
    identically on every channel), for scalar and vector times."""
    mono = AudioClip(lambda t: 0.5 * np.sin(2 * np.pi * t), duration=1.0, fps=100)
    stereo, array = stereo_array_clip(n=100, fps=100)
    comp = CompositeAudioClip([mono, stereo])
    assert comp.nchannels == 2

    frame = comp.get_frame(0.5)
    assert frame.shape == (2,)
    frames = comp.get_frame(np.arange(0, 1, 0.01))
    assert frames.shape == (100, 2)


def test_composite_audio_clip_mono_centered_values():
    mono = AudioClip(lambda t: 0.25 * np.ones_like(np.asarray(t, dtype=float)),
                     duration=1.0, fps=100)
    three, _ = stereo_array_clip(n=100, fps=100, channels=3)
    comp = CompositeAudioClip([mono, three])
    assert comp.nchannels == 3
    frame = comp.get_frame(0.5)
    assert frame.shape == (3,)
    # mono signal added identically to every channel
    assert np.allclose(frame, three.get_frame(0.5) + 0.25)


def test_composite_audio_clip_incompatible_channels_raise():
    stereo, _ = stereo_array_clip(n=100, fps=100, channels=2)
    three, _ = stereo_array_clip(n=100, fps=100, channels=3)
    comp = CompositeAudioClip([stereo, three])
    assert comp.nchannels == 3
    with pytest.raises(ValueError, match="channel"):
        comp.get_frame(0.5)


def test_concatenate_audioclips_mixed_channels():
    mono = AudioClip(lambda t: 0.1 * np.ones_like(np.asarray(t, dtype=float)),
                     duration=0.5, fps=100)
    stereo, _ = stereo_array_clip(n=50, fps=100)
    cat = concatenate_audioclips([mono, stereo])
    assert cat.nchannels == 2
    assert cat.duration == 1.0
    sound = cat.to_soundarray(fps=100)
    assert sound.shape == (100, 2)
    # first half: mono signal on both channels
    assert np.allclose(sound[:50], 0.1)


def _decode_wav(filename):
    """Decode a wav file to a (N, C) int16 array using ffmpeg directly."""
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=channels,sample_rate",
            "-of", "default=noprint_wrappers=1", filename,
        ],
        capture_output=True, text=True, check=True,
    )
    info = dict(
        line.split("=") for line in probe.stdout.strip().splitlines()
    )
    channels, sample_rate = int(info["channels"]), int(info["sample_rate"])
    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", filename,
            "-f", "s16le", "-acodec", "pcm_s16le", "-",
        ],
        capture_output=True, check=True,
    ).stdout
    array = np.frombuffer(raw, dtype="int16").reshape((-1, channels))
    return array, channels, sample_rate


@pytest.mark.parametrize("channels", (1, 3))
def test_write_audiofile_lossless_wav(tmp_path, channels):
    """Writing a mono and a multi-channel clip to WAV with the real ffmpeg
    produces the same sample sequence as to_soundarray/iter_chunks."""
    fps = 44100
    n = 10000
    tt = np.arange(n) / fps
    rng = np.random.RandomState(7)
    array = 0.4 * np.sin(2 * np.pi * 440 * tt)[:, None] * np.ones((1, channels))
    array += 0.1 * rng.uniform(-1, 1, size=(n, channels))
    clip = AudioArrayClip(array, fps=fps)

    filename = str(tmp_path / f"test_{channels}ch.wav")
    clip.write_audiofile(filename, fps=fps, nbytes=2, buffersize=2000, logger=None)

    decoded, wav_channels, wav_rate = _decode_wav(filename)
    assert wav_channels == channels
    assert wav_rate == fps
    # frame count matches the half-open timeline
    assert decoded.shape == (n, channels)
    # lossless: every sample matches the quantized source, check the
    # whole array plus explicitly the first and last frames
    expected = clip.to_soundarray(fps=fps, quantize=True, nbytes=2)
    assert np.array_equal(decoded[0], expected[0])
    assert np.array_equal(decoded[-1], expected[-1])
    assert np.array_equal(decoded, expected)
