"""Lifecycle, diagnostics and short-write tests for MoviePy's FFmpeg writers.

These tests use a local fake encoder so that failure modes (huge stderr,
non-zero exits, hangs, ignored SIGTERM) can be exercised deterministically
without any network access.
"""

import io
import os
import subprocess as sp
import threading
import time

import numpy as np

import pytest

import moviepy.audio.io.ffmpeg_audiowriter as audiowriter_module
import moviepy.video.io.ffmpeg_writer as video_writer_module
from moviepy.audio.io.ffmpeg_audiowriter import FFMPEG_AudioWriter, ffmpeg_audiowrite
from moviepy.video.io.ffmpeg_writer import FFMPEG_VideoWriter, ffmpeg_write_video


FAKE_ENCODER = os.environ.get("MOVIEPY_FAKE_ENCODER", "")

pytestmark = pytest.mark.skipif(
    not FAKE_ENCODER or not os.path.exists(FAKE_ENCODER),
    reason="MOVIEPY_FAKE_ENCODER must point to the local fake encoder",
)

SIZE = (64, 48)


def _frame(seed=0):
    rng = np.random.default_rng(seed)
    return rng.integers(0, 255, size=(SIZE[1], SIZE[0], 3), dtype=np.uint8)


def _fake_children():
    """Return PIDs of live fake-encoder processes."""
    output = sp.run(
        ["ps", "-o", "pid=,command="], capture_output=True, text=True
    ).stdout
    pids = set()
    for line in output.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        if "fake_ffmpeg" in command and "ps -o" not in command:
            try:
                pids.add(int(pid_text))
            except ValueError:
                pass
    return pids


@pytest.fixture(autouse=True)
def _assert_no_leaked_children():
    before = _fake_children()
    yield
    deadline = time.monotonic() + 2
    leaked = set()
    while True:
        leaked = _fake_children() - before
        if not leaked or time.monotonic() > deadline:
            break
        time.sleep(0.05)
    for pid in leaked:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    assert not leaked, f"leaked fake encoder processes: {leaked}"


def _video_writer(tmp_path, monkeypatch, mode, extra_args=(), **kwargs):
    monkeypatch.setattr(
        video_writer_module,
        "FFMPEG_BINARY",
        [FAKE_ENCODER, "--mode", mode, *extra_args],
    )
    return FFMPEG_VideoWriter(
        str(tmp_path / "out.mp4"), SIZE, 24, codec="libx264", **kwargs
    )


def _audio_writer(tmp_path, monkeypatch, mode, extra_args=(), **kwargs):
    monkeypatch.setattr(
        audiowriter_module,
        "FFMPEG_BINARY",
        [FAKE_ENCODER, "--mode", mode, *extra_args],
    )
    return FFMPEG_AudioWriter(
        str(tmp_path / "out.mp4"),
        44100,
        nbytes=2,
        nchannels=2,
        codec="pcm_s16le",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs",
    [
        {"close_timeout": 0},
        {"close_timeout": -1},
        {"close_timeout": float("inf")},
        {"close_timeout": float("nan")},
        {"close_timeout": "5"},
        {"close_timeout": True},
        {"stderr_tail_bytes": 0},
        {"stderr_tail_bytes": -5},
        {"stderr_tail_bytes": 1.5},
        {"stderr_tail_bytes": True},
        {"stderr_tail_bytes": "65536"},
    ],
)
def test_invalid_constructor_parameters_raise_before_process(tmp_path, kwargs):
    with pytest.raises(ValueError):
        FFMPEG_VideoWriter(str(tmp_path / "v.mp4"), SIZE, 24, **kwargs)
    with pytest.raises(ValueError):
        FFMPEG_AudioWriter(str(tmp_path / "a.wav"), 44100, **kwargs)
    assert os.listdir(tmp_path) == []


def test_valid_constructor_parameters_are_accepted(tmp_path):
    writer = FFMPEG_VideoWriter(
        str(tmp_path / "v.mp4"), SIZE, 1, close_timeout=0.5, stderr_tail_bytes=16
    )
    writer.close()


# ---------------------------------------------------------------------------
# Stderr draining
# ---------------------------------------------------------------------------
def test_large_stderr_is_drained_and_only_tail_is_kept(tmp_path, monkeypatch):
    writer = _video_writer(
        tmp_path,
        monkeypatch,
        "big-stderr",
        ["--stderr-bytes", "2000000", "--code", "0"],
    )
    try:
        for i in range(5):
            writer.write_frame(_frame(i))
        writer.close()
    finally:
        writer.close()

    assert writer._capture_stderr
    assert len(writer._stderr_tail) == writer.stderr_tail_bytes
    assert "TAIL-MARKER" in writer._stderr_text()


def test_stderr_flood_does_not_block_frame_writes(tmp_path, monkeypatch):
    writer = _video_writer(
        tmp_path,
        monkeypatch,
        "big-stderr",
        ["--stderr-bytes", "1000000", "--code", "0"],
    )
    started = time.monotonic()
    for i in range(50):
        writer.write_frame(_frame(i))
    writer.close()
    assert time.monotonic() - started < 15


def test_stderr_is_drained_incrementally_not_only_at_close(tmp_path, monkeypatch):
    """Partial stderr output must appear while the child is still running."""
    monkeypatch.setattr(
        video_writer_module,
        "FFMPEG_BINARY",
        [
            "sh",
            "-c",
            'printf "partial" >&2; sleep 1; printf " line done\\n" >&2; exit 1',
        ],
    )
    writer = FFMPEG_VideoWriter(
        str(tmp_path / "out.mp4"), SIZE, 24, codec="libx264", close_timeout=2
    )
    try:
        time.sleep(0.3)
        assert "partial" in writer._stderr_text()
        assert "line done" not in writer._stderr_text()
    finally:
        try:
            writer.close()
        except IOError:
            pass


def test_invalid_utf8_stderr_does_not_hide_the_encoding_error(
    tmp_path, monkeypatch
):
    writer = _video_writer(
        tmp_path,
        monkeypatch,
        "exit-immediate",
        ["--code", "2", "--stderr-file", "invalid-utf8"],
        stderr_tail_bytes=200,
    )
    try:
        with pytest.raises(IOError) as excinfo:
            for i in range(200):
                writer.write_frame(_frame(i))
    finally:
        writer.close()

    message = str(excinfo.value)
    assert "DECODE-MARKER" in message
    assert "2" in message
    assert "�" in message


# ---------------------------------------------------------------------------
# Non-zero exits and premature failure
# ---------------------------------------------------------------------------
def test_immediate_failure_reported_on_write(tmp_path, monkeypatch):
    writer = _video_writer(tmp_path, monkeypatch, "exit-immediate", ["--code", "7"])
    try:
        with pytest.raises(IOError) as excinfo:
            for i in range(200):
                writer.write_frame(_frame(i))
        assert "7" in str(excinfo.value)
    finally:
        writer.close()

    writer.close()  # idempotent: the recorded failure is not re-raised


def test_failure_stops_further_frames_from_being_treated_as_written(
    tmp_path, monkeypatch
):
    writer = _video_writer(tmp_path, monkeypatch, "exit-immediate", ["--code", "7"])

    class _CountingPipe:
        def __init__(self):
            self.writes = 0

        def write(self, data):
            self.writes += 1
            raise BrokenPipeError("fake broken pipe")

        def close(self):
            pass

    pipe = _CountingPipe()
    writer.proc.stdin = pipe
    with pytest.raises(IOError):
        writer.write_frame(_frame())
    assert pipe.writes == 1
    with pytest.raises(OSError, match="closed"):
        writer.write_frame(_frame())


def test_failure_at_end_of_input_reported_by_close(tmp_path, monkeypatch):
    writer = _video_writer(tmp_path, monkeypatch, "fail-close", ["--code", "3"])
    for i in range(3):
        writer.write_frame(_frame(i))
    with pytest.raises(IOError) as excinfo:
        writer.close()

    message = str(excinfo.value)
    assert "fake: failure at end of input" in message
    assert "3" in message
    writer.close()


def test_close_error_names_the_output_file(tmp_path, monkeypatch):
    writer = _video_writer(tmp_path, monkeypatch, "fail-close", ["--code", "9"])
    writer.write_frame(_frame())
    with pytest.raises(IOError) as excinfo:
        writer.close()
    assert str(tmp_path / "out.mp4") in str(excinfo.value)


def test_audio_failure_is_reported(tmp_path, monkeypatch):
    writer = _audio_writer(tmp_path, monkeypatch, "fail-close", ["--code", "4"])
    writer.write_frames(np.zeros(100, dtype=np.int16))
    with pytest.raises(IOError) as excinfo:
        writer.close()
    assert "4" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Timeouts and termination
# ---------------------------------------------------------------------------
def test_close_times_out_and_terminates(tmp_path, monkeypatch):
    writer = _video_writer(tmp_path, monkeypatch, "hang", close_timeout=0.3)
    writer.write_frame(_frame())
    started = time.monotonic()
    with pytest.raises(IOError) as excinfo:
        writer.close()
    elapsed = time.monotonic() - started

    assert elapsed < 5, "close did not respect close_timeout"
    message = str(excinfo.value)
    assert "did not exit" in message
    assert "terminate" in message.lower()
    assert writer.proc is None


def test_close_kills_process_that_ignores_terminate(tmp_path, monkeypatch):
    writer = _video_writer(
        tmp_path, monkeypatch, "hang-ignore-term", close_timeout=0.25
    )
    writer.write_frame(_frame())
    started = time.monotonic()
    with pytest.raises(IOError) as excinfo:
        writer.close()
    elapsed = time.monotonic() - started

    assert elapsed < 3, "kill path was not bounded"
    message = str(excinfo.value)
    assert "terminated" in message.lower()
    assert "killed" in message.lower()
    assert writer.proc is None


def test_no_live_stderr_thread_after_close(tmp_path, monkeypatch):
    writer = _video_writer(tmp_path, monkeypatch, "ok")
    writer.write_frame(_frame())
    writer.close()
    alive = [t for t in threading.enumerate() if t.name == "moviepy-ffmpeg-stderr"]
    assert alive == []


def test_close_is_bounded_when_grandchild_holds_stderr_open(tmp_path, monkeypatch):
    """A surviving descendant must not keep the drain thread alive forever."""
    marker = "fake_ffmpeg_holder"
    holder = tmp_path / marker
    holder.write_text(
        "#!/bin/sh\n"
        "# Keep a background child alive so the stderr pipe stays open.\n"
        "(exec -a " + marker + " sleep 30) &\n"
        "echo holder\nexit 0\n"
    )
    holder.chmod(0o755)
    monkeypatch.setattr(video_writer_module, "FFMPEG_BINARY", [str(holder)])
    writer = FFMPEG_VideoWriter(
        str(tmp_path / "out.mp4"), SIZE, 24, codec="libx264", close_timeout=0.4
    )
    started = time.monotonic()
    try:
        writer.write_frame(_frame())
    except IOError:
        # The helper exits immediately, so a write may legitimately race it.
        pass
    try:
        writer.close()
    except IOError:
        # A close timeout is an acceptable outcome as long as it is bounded.
        pass
    finally:
        sp.run(["pkill", "-f", marker], capture_output=True)
    try:
        writer.close()
    except IOError:
        raise AssertionError("repeated close re-raised a recorded failure")

    assert time.monotonic() - started < 5
    assert writer._stderr_thread is None
    assert writer.proc is None


# ---------------------------------------------------------------------------
# Write semantics
# ---------------------------------------------------------------------------
class _ShortWritePipe:
    """A stdin replacement returning several partial writes per call."""

    def __init__(self, max_chunk):
        self.max_chunk = max_chunk
        self.received = bytearray()
        self.closed = False

    def write(self, data):
        if self.closed:
            return 0
        n = min(self.max_chunk, len(data))
        self.received.extend(data[:n])
        return n

    def close(self):
        self.closed = True


def test_video_write_retries_short_writes(tmp_path, monkeypatch):
    writer = _video_writer(tmp_path, monkeypatch, "ok")
    frame = _frame(5)
    stdin = _ShortWritePipe(7)
    writer.proc.stdin = stdin
    writer.write_frame(frame)
    assert bytes(stdin.received) == frame.tobytes()
    writer.close()


def test_audio_write_retries_short_writes(tmp_path, monkeypatch):
    writer = _audio_writer(tmp_path, monkeypatch, "ok")
    chunk = np.arange(200, dtype=np.int16)
    stdin = _ShortWritePipe(13)
    writer.proc.stdin = stdin
    writer.write_frames(chunk)
    assert bytes(stdin.received) == chunk.tobytes()
    writer.close()


@pytest.mark.parametrize("factory", ["video", "audio"])
def test_zero_byte_write_is_an_error(tmp_path, monkeypatch, factory):
    writer = (
        _video_writer(tmp_path, monkeypatch, "ok")
        if factory == "video"
        else _audio_writer(tmp_path, monkeypatch, "ok")
    )

    class _ZeroPipe:
        def write(self, data):
            return 0

        def close(self):
            pass

    writer.proc.stdin = _ZeroPipe()
    with pytest.raises(IOError):
        if factory == "video":
            writer.write_frame(_frame(1))
        else:
            writer.write_frames(np.zeros(10, dtype=np.int16))
    assert writer.proc is None


def test_non_contiguous_video_array_is_written_in_c_order(tmp_path, monkeypatch):
    writer = _video_writer(tmp_path, monkeypatch, "ok")
    view = _frame(2)[:, ::2, :]
    assert not view.flags["C_CONTIGUOUS"]
    expected = np.ascontiguousarray(view).tobytes()
    stdin = _ShortWritePipe(11)
    writer.proc.stdin = stdin
    writer.write_frame(view)
    assert bytes(stdin.received) == expected
    writer.close()


@pytest.mark.parametrize("factory", ["video", "audio"])
def test_write_after_close_raises_oserror_not_attribute_error(
    tmp_path, monkeypatch, factory
):
    writer = (
        _video_writer(tmp_path, monkeypatch, "ok")
        if factory == "video"
        else _audio_writer(tmp_path, monkeypatch, "ok")
    )
    writer.close()
    with pytest.raises(OSError) as excinfo:
        if factory == "video":
            writer.write_frame(_frame())
        else:
            writer.write_frames(np.zeros(10, dtype=np.int16))
    assert not isinstance(excinfo.value, AttributeError)
    assert "closed" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Caller-owned logfile
# ---------------------------------------------------------------------------
def test_caller_logfile_is_not_closed_seeked_or_read(tmp_path, monkeypatch):
    calls = []
    path = tmp_path / "caller.log"
    real_file = open(path, "w+")

    class _SpyLogfile(io.TextIOBase):
        def __init__(self, real):
            self._real = real

        def fileno(self):
            return self._real.fileno()

        def writable(self):
            return True

        def write(self, text):
            calls.append(("write", text))
            return self._real.write(text)

        def flush(self):
            return self._real.flush()

        def seek(self, *args):
            calls.append(("seek", args))
            return self._real.seek(*args)

        def read(self, *args):
            calls.append(("read", args))
            return self._real.read(*args)

        def close(self):
            calls.append(("close",))
            return self._real.close()

    spy = _SpyLogfile(real_file)
    writer = _video_writer(
        tmp_path, monkeypatch, "fail-close", ["--code", "5"], logfile=spy
    )
    writer.write_frame(_frame())
    with pytest.raises(IOError) as excinfo:
        writer.close()

    message = str(excinfo.value)
    assert "5" in message
    assert str(tmp_path / "out.mp4") in message
    assert "redirected" in message
    assert [call[0] for call in calls if call[0] in ("seek", "read", "close")] == []

    # The caller's handle is still open and usable.
    assert not real_file.closed
    real_file.close()


# ---------------------------------------------------------------------------
# Upper-layer wrappers
# ---------------------------------------------------------------------------
def _stub_clip(frames, fail_at=None):
    class Clip:
        size = SIZE
        mask = None

        def iter_frames(self, **kwargs):
            for index, frame in enumerate(frames):
                if fail_at is not None and index == fail_at:
                    raise RuntimeError("business failure while generating frames")
                yield index / 24.0, frame

    return Clip()


@pytest.mark.parametrize("write_logfile", [False, True])
def test_ffmpeg_write_video_cleans_up_on_write_failure(
    tmp_path, monkeypatch, write_logfile
):
    monkeypatch.setattr(
        video_writer_module,
        "FFMPEG_BINARY",
        [FAKE_ENCODER, "--mode", "exit-immediate"],
    )
    with pytest.raises(IOError):
        ffmpeg_write_video(
            _stub_clip([_frame(i) for i in range(4)]),
            str(tmp_path / "v.mp4"),
            24,
            write_logfile=write_logfile,
            logger=None,
        )
    if write_logfile:
        assert os.path.exists(str(tmp_path / "v.mp4.log"))
    assert not os.path.exists(str(tmp_path / "v.mp4"))


def test_ffmpeg_write_video_does_not_log_success_on_business_error(
    tmp_path, monkeypatch
):
    messages = []

    class Logger:
        def __call__(self, **kwargs):
            messages.append(kwargs.get("message"))

    monkeypatch.setattr(
        video_writer_module, "FFMPEG_BINARY", [FAKE_ENCODER, "--mode", "exit-immediate"]
    )
    with pytest.raises(RuntimeError, match="business failure"):
        ffmpeg_write_video(
            _stub_clip([_frame(i) for i in range(6)], fail_at=3),
            str(tmp_path / "v.mp4"),
            24,
            logger=Logger(),
        )
    assert not any(message and "Done" in message for message in messages)


def test_ffmpeg_write_video_accepts_pathlib_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        video_writer_module, "FFMPEG_BINARY", [FAKE_ENCODER, "--mode", "ok"]
    )
    output = tmp_path / "v.mp4"
    ffmpeg_write_video(
        _stub_clip([_frame(i) for i in range(3)]),
        output,
        24,
        logger=None,
    )
    assert output.exists()


@pytest.mark.parametrize("write_logfile", [False, True])
def test_ffmpeg_audiowrite_cleans_up_on_write_failure(
    tmp_path, monkeypatch, write_logfile
):
    from moviepy.audio.AudioClip import AudioArrayClip

    monkeypatch.setattr(
        audiowriter_module, "FFMPEG_BINARY", [FAKE_ENCODER, "--mode", "exit-immediate"]
    )
    clip = AudioArrayClip(np.zeros((4410, 2), dtype=np.int16), fps=44100)
    with pytest.raises(IOError):
        ffmpeg_audiowrite(
            clip,
            str(tmp_path / "a.wav"),
            44100,
            2,
            2000,
            codec="pcm_s16le",
            write_logfile=write_logfile,
            logger=None,
        )
    if write_logfile:
        assert os.path.exists(str(tmp_path / "a.wav.log"))
    assert not os.path.exists(str(tmp_path / "a.wav"))


def test_context_manager_keeps_business_exception(tmp_path, monkeypatch):
    writer = _video_writer(tmp_path, monkeypatch, "fail-close", ["--code", "6"])
    with pytest.raises(RuntimeError, match="business failure"):
        with writer:
            raise RuntimeError("business failure")
    assert writer.proc is None
    # The cleanup failure is attached as a note rather than replacing it.
    assert writer._closed


def test_process_creation_failure_leaves_no_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(
        video_writer_module, "FFMPEG_BINARY", ["/nonexistent/missing-ffmpeg"]
    )
    with pytest.raises(OSError):
        FFMPEG_VideoWriter(str(tmp_path / "v.mp4"), SIZE, 1, codec="libx264")
    assert not [
        t for t in threading.enumerate() if t.name == "moviepy-ffmpeg-stderr"
    ]


def test_half_constructed_object_cleans_up_safely(tmp_path):
    """A writer whose constructor failed must not leak or raise from cleanup."""
    from moviepy._ffmpeg_writer import _FFmpegWriterBase

    half_built = FFMPEG_VideoWriter.__new__(FFMPEG_VideoWriter)
    _FFmpegWriterBase.__init__(half_built, str(tmp_path / "half.mp4"))
    assert half_built.close() is None
    with pytest.raises(OSError, match="closed"):
        half_built.write_frame(_frame())
    half_built.close()
    half_built.__del__()
