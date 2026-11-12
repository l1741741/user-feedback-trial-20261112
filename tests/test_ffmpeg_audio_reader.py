"""Tests for FFMPEG_AudioReader mixed scalar/vector random access.

Only locally generated, deterministic mono/stereo PCM WAV files are used.
They are decoded by the real ffmpeg binary (no subprocess mocking); the
independent reference is the `wave` module + NumPy (the exact int16
samples written to the file).  Since the reader returns samples as
``int16 / 2**15`` (exactly representable in float64), all comparisons
against the reference are exact.
"""

import wave

import numpy as np
import pytest

from moviepy.audio.io.AudioFileClip import AudioFileClip
from moviepy.audio.io.readers import FFMPEG_AudioReader

# ----------------------------------------------------------------------
# Deterministic test media (generated locally, PCM s16le WAV)
# ----------------------------------------------------------------------

MONO_FPS = 8000
MONO_N = 2000

STEREO_FPS = 44100
STEREO_N = 5000


def _mono_samples():
    rng = np.random.default_rng(1234)
    samples = rng.integers(-32768, 32767, size=MONO_N, dtype=np.int16)
    samples[0] = 12345  # frame 0 must be non-zero (negative-time tests)
    samples[-1] = -23456  # last frame must be non-zero (end-of-file tests)
    return samples


def _stereo_samples():
    # "index-encoded": frame i holds [i, 30000 - i], so any returned frame
    # uniquely identifies the source index and validates both channels.
    left = np.arange(STEREO_N, dtype=np.int16)
    right = (30000 - np.arange(STEREO_N)).astype(np.int16)
    return np.stack([left, right], axis=1)


def _write_wav(path, samples, fps):
    samples = np.asarray(samples, dtype=np.int16)
    nchannels = 1 if samples.ndim == 1 else samples.shape[1]
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(nchannels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(fps)
        wav_file.writeframes(np.ascontiguousarray(samples).tobytes())


def _read_wav_reference(path):
    """Independent reference: samples straight from the WAV header/data."""
    with wave.open(str(path), "rb") as wav_file:
        nchannels = wav_file.getnchannels()
        nframes = wav_file.getnframes()
        fps = wav_file.getframerate()
        raw = wav_file.readframes(nframes)
    data = np.frombuffer(raw, dtype="<i2").reshape(nframes, nchannels)
    return (data / 32768.0).astype(np.float64), fps


@pytest.fixture(scope="module")
def mono_wav(tmp_path_factory):
    path = tmp_path_factory.mktemp("media") / "mono.wav"
    _write_wav(path, _mono_samples(), MONO_FPS)
    reference, fps = _read_wav_reference(path)
    assert fps == MONO_FPS and len(reference) == MONO_N
    return str(path), reference, MONO_FPS, 1


@pytest.fixture(scope="module")
def stereo_wav(tmp_path_factory):
    path = tmp_path_factory.mktemp("media") / "stereo.wav"
    _write_wav(path, _stereo_samples(), STEREO_FPS)
    reference, fps = _read_wav_reference(path)
    assert fps == STEREO_FPS and len(reference) == STEREO_N
    return str(path), reference, STEREO_FPS, 2


@pytest.fixture()
def mono_reader(mono_wav):
    filename, _, fps, nchannels = mono_wav
    reader = FFMPEG_AudioReader(
        filename, buffersize=200, fps=fps, nchannels=nchannels
    )
    yield reader
    reader.close()


@pytest.fixture()
def stereo_reader(stereo_wav):
    filename, _, fps, nchannels = stereo_wav
    reader = FFMPEG_AudioReader(
        filename, buffersize=300, fps=fps, nchannels=nchannels
    )
    yield reader
    reader.close()


# ----------------------------------------------------------------------
# Scalar access
# ----------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", ["mono_wav", "stereo_wav"])
def test_scalar_matches_wave_reference(fixture_name, request):
    filename, reference, fps, nchannels = request.getfixturevalue(fixture_name)
    reader = FFMPEG_AudioReader(filename, buffersize=173, fps=fps, nchannels=nchannels)
    try:
        n_frames = len(reference)
        assert reader.n_frames == n_frames
        assert reader.duration == pytest.approx(n_frames / fps)
        for k in [0, 1, 2, 7, n_frames // 2, n_frames - 2, n_frames - 1]:
            frame = reader.get_frame(k / fps)
            assert frame.shape == (nchannels,)
            np.testing.assert_array_equal(frame, reference[k])
    finally:
        reader.close()


def test_scalar_out_of_range_returns_zeros(mono_reader, mono_wav):
    _, reference, fps, nchannels = mono_wav
    n_frames = len(reference)
    duration = n_frames / fps

    # frame 0 is non-zero by construction: a tiny negative time must NOT
    # be truncated into frame 0.
    assert np.any(reference[0] != 0)
    for t in [-1.0, -1.0 / fps, -0.5 / fps, -1e-9, -1e-12]:
        np.testing.assert_array_equal(
            mono_reader.get_frame(t), np.zeros(nchannels)
        )

    # half-open range: t == duration and beyond -> zeros
    assert np.any(reference[-1] != 0)
    for t in [duration, duration + 1.0 / fps, duration + 10.0]:
        np.testing.assert_array_equal(
            mono_reader.get_frame(t), np.zeros(nchannels)
        )


def test_last_frame_not_misread_by_float_rounding(stereo_reader, stereo_wav):
    """t = (n_frames - 1) / fps must give the last frame, not the previous
    one, and a legal time a hair below duration must not fall out of range."""
    _, reference, fps, _ = stereo_wav
    n_frames = len(reference)
    duration = n_frames / fps

    np.testing.assert_array_equal(
        stereo_reader.get_frame((n_frames - 1) / fps), reference[-1]
    )
    np.testing.assert_array_equal(
        stereo_reader.get_frame(np.nextafter(duration, 0.0)), reference[-1]
    )
    np.testing.assert_array_equal(
        stereo_reader.get_frame(duration - 1e-9), reference[-1]
    )
    # ... while t == duration itself is out of the half-open range
    np.testing.assert_array_equal(stereo_reader.get_frame(duration), np.zeros(2))


def test_scalar_and_vector_discretization_agree(mono_reader, mono_wav):
    """floor(t * fps) must select the same sample for scalar and vector."""
    _, reference, fps, _ = mono_wav
    rng = np.random.default_rng(7)
    times = rng.uniform(0, len(reference) / fps, size=200)
    vector_result = mono_reader.get_frame(times)
    for i, t in enumerate(times):
        np.testing.assert_array_equal(mono_reader.get_frame(float(t)), vector_result[i])


# ----------------------------------------------------------------------
# Vector access
# ----------------------------------------------------------------------


def test_vector_unordered_repeated_points(stereo_reader, stereo_wav):
    _, reference, fps, _ = stereo_wav
    rng = np.random.default_rng(42)
    # shuffled, with plenty of duplicates
    indices = rng.integers(0, len(reference), size=800)
    times = indices / fps

    result = stereo_reader.get_frame(times)
    assert result.shape == (len(indices), 2)
    # output order must match input order, duplicates included
    np.testing.assert_array_equal(result, reference[indices])

    # a different access order of the same times must give the same samples
    permutation = rng.permutation(len(times))
    result2 = stereo_reader.get_frame(times[permutation])
    np.testing.assert_array_equal(result2, reference[indices][permutation])


def test_vector_empty_and_fully_out_of_range(stereo_reader, stereo_wav):
    _, reference, fps, _ = stereo_wav
    duration = len(reference) / fps

    empty = stereo_reader.get_frame(np.array([]))
    assert empty.shape == (0, 2)

    result = stereo_reader.get_frame(np.array([-3.0, -1e-6, duration, duration + 5]))
    np.testing.assert_array_equal(result, np.zeros((4, 2)))

    # scalar access still works afterwards (buffer left in consistent state)
    np.testing.assert_array_equal(stereo_reader.get_frame(10 / fps), reference[10])


def test_vector_spanning_many_buffers_small_buffersize(stereo_wav):
    """A single spread-out request must be fetched in small contiguous
    ranges (bounded memory), not by loading the whole file."""
    filename, reference, fps, nchannels = stereo_wav
    reader = FFMPEG_AudioReader(
        filename, buffersize=64, fps=fps, nchannels=nchannels
    )
    try:
        rng = np.random.default_rng(3)
        indices = rng.permutation(len(reference))[:1500]  # shuffled, file-wide
        result = reader.get_frame(indices / fps)
        np.testing.assert_array_equal(result, reference[indices])
        # memory stays bounded by the buffersize whatever the request span
        assert len(reader.buffer) <= 64
        # and the reader is still consistent for a follow-up scalar access
        np.testing.assert_array_equal(reader.get_frame(0.0), reference[0])
    finally:
        reader.close()


def test_vector_straddling_end_of_file(mono_reader, mono_wav):
    _, reference, fps, nchannels = mono_wav
    n_frames = len(reference)
    indices = np.arange(n_frames - 3, n_frames + 4)  # last valid + beyond
    result = mono_reader.get_frame(indices / fps)
    np.testing.assert_array_equal(result[:3], reference[-3:])
    np.testing.assert_array_equal(result[3:], np.zeros((4, nchannels)))


def test_vector_accepts_list_input(mono_reader, mono_wav):
    _, reference, fps, _ = mono_wav
    result = mono_reader.get_frame([3 / fps, 1 / fps, 1 / fps, 100 / fps])
    np.testing.assert_array_equal(
        result, np.stack([reference[3], reference[1], reference[1], reference[100]])
    )


# ----------------------------------------------------------------------
# Buffer / seek consistency
# ----------------------------------------------------------------------


@pytest.mark.parametrize("buffersize", [1, 2, 17, 100, 4096, 200000])
def test_buffersize_invariance(stereo_wav, buffersize):
    """Same access script, any buffersize -> identical samples."""
    filename, reference, fps, nchannels = stereo_wav
    reader = FFMPEG_AudioReader(
        filename, buffersize=buffersize, fps=fps, nchannels=nchannels
    )
    try:
        rng = np.random.default_rng(99)
        indices = rng.integers(0, len(reference), size=300)
        # interleave scalar and vector accesses
        got_scalar = np.array([reader.get_frame(i / fps) for i in indices[:50]])
        got_vector = reader.get_frame(indices[50:] / fps)
        np.testing.assert_array_equal(got_scalar, reference[indices[:50]])
        np.testing.assert_array_equal(got_vector, reference[indices[50:]])
    finally:
        reader.close()


def test_result_independent_of_previous_buffer_position(stereo_wav):
    """Same frame fetched after different access histories -> same sample."""
    filename, reference, fps, nchannels = stereo_wav
    reader = FFMPEG_AudioReader(
        filename, buffersize=50, fps=fps, nchannels=nchannels
    )
    try:
        target = 1234
        histories = [
            [0, 4999, 2500, 10],  # jumps all over the file
            [4999, 0, 4999, 0],
            list(range(1200, 1270)),  # sequential neighbourhood
        ]
        for history in histories:
            for k in history:
                reader.get_frame(k / fps)
            np.testing.assert_array_equal(
                reader.get_frame(target / fps), reference[target]
            )
            np.testing.assert_array_equal(
                reader.get_frame(np.array([target, target + 1]) / fps),
                reference[target : target + 2],
            )
    finally:
        reader.close()


def test_multiple_external_seeks_keep_buffer_consistent(mono_wav):
    """Direct seek() calls (which move the stream but not the buffer) must
    not corrupt later reads through the stale-buffer reuse path."""
    filename, reference, fps, nchannels = mono_wav
    reader = FFMPEG_AudioReader(filename, buffersize=50, fps=fps, nchannels=nchannels)
    try:
        reader.seek(1000)
        reader.seek(10)
        reader.seek(1500)
        reader.seek(0)
        # buffer still covers frames [0, 50) but the stream is at 0; a read
        # whose new buffer would overlap the old one must not reuse stale
        # data assuming the stream sits at the end of the old buffer.
        np.testing.assert_array_equal(reader.get_frame(60 / fps), reference[60])
        reader.seek(10)  # stream now *behind* the buffer it would reuse
        np.testing.assert_array_equal(reader.get_frame(45 / fps), reference[45])
        np.testing.assert_array_equal(reader.get_frame(75 / fps), reference[75])
        # and plain repeated seeking back and forth stays exact
        for k in [1999, 0, 999, 5, 1500, 42]:
            np.testing.assert_array_equal(reader.get_frame(k / fps), reference[k])
    finally:
        reader.close()


def test_reinitialize_leaves_no_ffmpeg_process_behind(mono_wav):
    filename, reference, fps, nchannels = mono_wav
    reader = FFMPEG_AudioReader(filename, buffersize=30, fps=fps, nchannels=nchannels)
    try:
        procs = [reader.proc]
        # alternating far-apart accesses force re-initialization each time
        for k in [1999, 1, 1800, 5, 1500, 0, 1999]:
            np.testing.assert_array_equal(reader.get_frame(k / fps), reference[k])
            if reader.proc is not procs[-1]:
                procs.append(reader.proc)
        assert len(procs) > 1  # re-initialization really happened
        # every previous ffmpeg process was terminated: no leak
        for proc in procs[:-1]:
            assert proc.poll() is not None
        # the current one is either alive or finished cleanly at EOF
        assert reader.proc.poll() in (None, 0)
    finally:
        reader.close()
    assert reader.proc is None
    for proc in procs:
        assert proc.poll() is not None


def test_close_is_idempotent_and_releases_process(mono_wav):
    filename, _, fps, nchannels = mono_wav
    reader = FFMPEG_AudioReader(filename, buffersize=100, fps=fps, nchannels=nchannels)
    proc = reader.proc
    reader.close()
    reader.close()  # must not raise
    assert reader.proc is None
    assert proc.poll() is not None  # ffmpeg process is gone
    reader.__del__()  # must not raise either


# ----------------------------------------------------------------------
# AudioFileClip public entry point
# ----------------------------------------------------------------------


def test_audiofileclip_public_entry(stereo_wav):
    filename, reference, fps, _ = stereo_wav
    clip = AudioFileClip(filename, fps=fps, buffersize=128)
    try:
        assert clip.duration == pytest.approx(len(reference) / fps)
        assert clip.nchannels == 2
        np.testing.assert_array_equal(clip.get_frame(7 / fps), reference[7])
        indices = np.array([100, 3, 3, 4999, 2000])
        np.testing.assert_array_equal(
            clip.get_frame(indices / fps), reference[indices]
        )
    finally:
        clip.close()
    clip.close()  # idempotent
