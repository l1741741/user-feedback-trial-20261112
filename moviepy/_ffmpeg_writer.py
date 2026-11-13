"""Shared subprocess lifecycle management for MoviePy's FFmpeg writers.

The writers in :mod:`moviepy.video.io.ffmpeg_writer` and
:mod:`moviepy.audio.io.ffmpeg_audiowriter` stream raw data to FFmpeg through
its standard input.  This module centralises the process plumbing so that both
writers drain stderr continuously, write all requested bytes, and shut the
child process down with bounded waits.
"""

import math
import os
import select
import subprocess as sp
import threading
from numbers import Integral, Real

from moviepy.tools import cross_platform_popen_params


# Read stderr in chunks so that a full log never has to be kept in memory.
_STDERR_READ_CHUNK = 64 * 1024

# Number of seconds to block in select() before checking whether the drain
# thread has been asked to stop.
_STDERR_SELECT_TIMEOUT = 0.1


def _validate_close_timeout(value):
    """Validate the ``close_timeout`` constructor argument."""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError("close_timeout must be a positive finite number")
    if not math.isfinite(float(value)) or value <= 0:
        raise ValueError("close_timeout must be a positive finite number")


def _validate_stderr_tail_bytes(value):
    """Validate the ``stderr_tail_bytes`` constructor argument."""
    if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
        raise ValueError("stderr_tail_bytes must be a positive integer")


def _append_cleanup_note(exc, note):
    """Attach ``note`` to an already-propagating exception when possible."""
    if exc is None:
        return
    add_note = getattr(exc, "add_note", None)
    if add_note is not None:
        try:
            add_note(note)
        except Exception:
            pass


class _PrematureExit(Exception):
    """Internal marker: FFmpeg exited before the current write completed."""

    def __init__(self, exit_code):
        super().__init__(exit_code)
        self.exit_code = exit_code


class _StopResult:
    """Outcome of a bounded FFmpeg process shutdown."""

    def __init__(self):
        self.exit_code = None
        self.timed_out = False
        self.terminated = False
        self.killed = False
        self.stdin_close_error = None
        self.cleanup_notes = []
        self.stderr_tail = ""

    @property
    def failed(self):
        """Whether the process or its cleanup did not complete cleanly."""
        return self.exit_code != 0 or self.timed_out or bool(self.cleanup_notes)


class _FFmpegWriterBase:
    """Base class handling FFmpeg stdin/stderr and child-process cleanup.

    Subclasses are expected to build their command and call
    :meth:`_start_process` once.
    """

    def __init__(
        self,
        filename,
        logfile=None,
        close_timeout=5.0,
        stderr_tail_bytes=65536,
    ):
        self.filename = os.fspath(filename)
        self.logfile = sp.PIPE if logfile is None else logfile

        try:
            self._capture_stderr = self.logfile == sp.PIPE
        except Exception:
            self._capture_stderr = False

        self._stderr_tail = bytearray()
        self._stderr_lock = threading.Lock()
        self._stderr_thread = None
        self._stderr_stop = threading.Event()

        self.proc = None
        # Remains True until a subprocess is successfully started, so that
        # a constructor failing validation (or half-way through) leaves an
        # object whose close()/__del__ are harmless no-ops.
        self._closed = True

        # Validation must happen before any subprocess is created.
        _validate_close_timeout(close_timeout)
        _validate_stderr_tail_bytes(stderr_tail_bytes)

        self.close_timeout = close_timeout
        self.stderr_tail_bytes = stderr_tail_bytes

    # ------------------------------------------------------------------
    # Spawning and stderr draining
    # ------------------------------------------------------------------
    def _start_process(self, cmd):
        """Start FFmpeg and, when stderr is captured, its drain thread."""
        popen_params = cross_platform_popen_params(
            {
                "stdout": sp.DEVNULL,
                "stderr": self.logfile,
                "stdin": sp.PIPE,
                # Unbuffered pipes make short writes observable and make
                # closing stdin on an abort path immediate rather than a
                # potentially blocking flush.
                "bufsize": 0,
            }
        )

        try:
            self.proc = sp.Popen(cmd, **popen_params)
        except BaseException:
            # No child exists, but __del__ must still find a safe object.
            self._closed = True
            raise

        self._closed = False

        if self._capture_stderr:
            try:
                self._stderr_thread = threading.Thread(
                    target=self._drain_stderr,
                    args=(self.proc.stderr,),
                    name="moviepy-ffmpeg-stderr",
                    daemon=True,
                )
                self._stderr_thread.start()
            except BaseException:
                # A half-constructed object must not leave a running child.
                try:
                    self._stop_process()
                except BaseException:
                    pass
                raise

    def _drain_stderr(self, stream):
        """Continuously read stderr, retaining only the configured tail."""
        if stream is None:
            return

        fd = None
        if os.name == "posix":
            try:
                fd = stream.fileno()
            except (AttributeError, OSError, ValueError):
                fd = None

        try:
            while True:
                if self._stderr_stop.is_set():
                    break

                if fd is not None:
                    try:
                        ready, _, _ = select.select(
                            [fd], [], [], _STDERR_SELECT_TIMEOUT
                        )
                    except (OSError, ValueError):
                        break
                    if not ready:
                        continue

                try:
                    chunk = self._read_stderr_chunk(stream, fd)
                except BlockingIOError:
                    # Data was consumed by another reader in a race.
                    continue
                except (AttributeError, OSError, ValueError):
                    break

                if not chunk:
                    break

                self._append_stderr_tail(chunk)
        except Exception:
            # A broken stderr pipe must never crash the drain thread; the
            # writer will still report the child's exit code.
            pass

    def _read_stderr_chunk(self, stream, fd):
        """Read one chunk of stderr without waiting for a full buffer.

        ``FileIO.read(n)`` only returns once *n* bytes or EOF are available,
        so a partially written diagnostic line could stay undrained until the
        process exits.  ``os.read`` returns as soon as any bytes arrive.
        """
        if fd is not None:
            return os.read(fd, _STDERR_READ_CHUNK)
        return stream.read(_STDERR_READ_CHUNK)

    def _append_stderr_tail(self, chunk):
        """Append bytes to the bounded stderr tail buffer."""
        with self._stderr_lock:
            self._stderr_tail.extend(chunk)
            excess = len(self._stderr_tail) - self.stderr_tail_bytes
            if excess > 0:
                del self._stderr_tail[:excess]

    def _stderr_text(self):
        """Return the retained stderr tail, decoded leniently."""
        with self._stderr_lock:
            data = bytes(self._stderr_tail)
        # Invalid UTF-8 must never hide the actual encoding error.
        return data.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------
    def _write_bytes(self, data, stage):
        """Write every byte of ``data`` or raise an encoding error.

        ``data`` must be bytes-like.  Short writes are retried, a zero-byte
        write is treated as a broken pipe, and an already-dead FFmpeg process
        is reported before more input is mistaken for successfully written.
        """
        if self._closed or self.proc is None:
            raise OSError(
                f"Cannot write {stage} for {self.filename}: "
                "the FFmpeg writer is closed."
            )

        try:
            stdin = self.proc.stdin
            if stdin is None:
                raise OSError(
                    f"Cannot write {stage} for {self.filename}: "
                    "the FFmpeg stdin stream is closed."
                )

            self._check_process_running()

            view = data if isinstance(data, memoryview) else memoryview(data)
            if view.format != "B" or view.ndim != 1:
                # Multi-dimensional buffers (e.g. an RGB frame) must be
                # flattened so that ``len`` and slicing address bytes.
                view = view.cast("B")
            total = len(view)
            written = 0
            while written < total:
                count = stdin.write(view[written:])
                if count is None or count <= 0:
                    raise BrokenPipeError(
                        f"FFmpeg stopped accepting input after {written} "
                        f"of {total} bytes"
                    )
                written += count

            self._check_process_running()
        except Exception as err:
            self._raise_write_error(err, stage)
        except BaseException:
            # Interrupts still get bounded cleanup, but keep their type.
            try:
                self._stop_process()
            except BaseException:
                pass
            raise

    def _check_process_running(self):
        """Raise the internal marker when FFmpeg has already exited."""
        exit_code = self.proc.poll()
        if exit_code is not None:
            raise _PrematureExit(exit_code)

    def _raise_write_error(self, cause, stage):
        """Shut the child down and raise a diagnostic write error."""
        try:
            result = self._stop_process()
        except Exception as stop_error:
            # Never let cleanup replace the original encoding failure.
            result = _StopResult()
            result.cleanup_notes.append(
                f"Error while cleaning up FFmpeg: {stop_error}"
            )
        message = self._build_error(stage=stage, result=result, cause=cause)
        error = IOError(message)
        if isinstance(cause, _PrematureExit):
            raise error from None
        raise error from cause

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def _stop_process(self):
        """Terminate/reap FFmpeg with bounded waits and finish stderr."""
        self._closed = True
        proc = self.proc
        result = _StopResult()

        if proc is None:
            result.stderr_tail = self._stderr_text()
            return result

        stdin = proc.stdin
        if stdin is not None:
            try:
                stdin.close()
            except Exception as err:
                result.stdin_close_error = err

        try:
            proc.wait(timeout=self.close_timeout)
        except sp.TimeoutExpired:
            result.timed_out = True
            result.terminated = True
            try:
                proc.terminate()
            except OSError:
                pass

            try:
                proc.wait(timeout=self.close_timeout)
            except sp.TimeoutExpired:
                result.killed = True
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=self.close_timeout)
                except sp.TimeoutExpired:
                    result.cleanup_notes.append(
                        "FFmpeg did not exit after being killed and could not "
                        "be reaped"
                    )
        except Exception as err:
            result.cleanup_notes.append(f"Error while waiting for FFmpeg: {err}")

        result.exit_code = proc.poll()

        if self._capture_stderr:
            self._finish_stderr_thread(proc, result)
            stream = proc.stderr
            if stream is not None:
                try:
                    stream.close()
                except Exception as err:
                    result.cleanup_notes.append(
                        f"Error while closing FFmpeg stderr: {err}"
                    )

        self.proc = None
        result.stderr_tail = self._stderr_text()
        return result

    def _finish_stderr_thread(self, proc, result):
        """Join the stderr drain thread within bounded time."""
        thread = self._stderr_thread
        if thread is None:
            return

        if thread.is_alive():
            thread.join(timeout=self.close_timeout)

        if thread.is_alive():
            # Tell the select() loop to stop even if a descendant process
            # still holds the pipe open.
            self._stderr_stop.set()
            thread.join(timeout=self.close_timeout)

        if thread.is_alive():
            # Last resort: closing the read end unblocks a pending read on
            # platforms without the select() loop.
            stream = proc.stderr
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            thread.join(timeout=self.close_timeout)

        if thread.is_alive():
            result.cleanup_notes.append(
                "FFmpeg stderr reader thread did not stop within the close "
                "timeout"
            )
        else:
            self._stderr_thread = None

    def close(self):
        """Close stdin, wait for FFmpeg, and report encoding failures.

        Calling this method again after a failure is a no-op; the failure is
        raised only by the first successful invocation of ``close``.
        """
        if self._closed:
            return

        result = self._stop_process()

        if not result.failed:
            return

        self._raise_close_error(result)

    def _raise_close_error(self, result):
        """Raise the diagnostic error for a failed close."""
        message = self._build_error(stage="closing", result=result)
        raise IOError(message)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def _build_error(self, stage, result, cause=None):
        """Build the user-facing error message."""
        lines = []

        if isinstance(cause, _PrematureExit):
            lines.append(
                f"FFmpeg exited with code {cause.exit_code} before the "
                f"{stage} was completely written."
            )
        elif cause is not None:
            lines.append(f"{type(cause).__name__}: {cause}")

        lines.append(
            "MoviePy error: FFMPEG encountered the following error while "
            f"writing file {self.filename}:"
        )

        if result.timed_out:
            timeout_text = (
                f"FFmpeg did not exit within the close timeout of "
                f"{self.close_timeout} seconds after its input was closed"
            )
            if result.terminated:
                timeout_text += "; the process was terminated"
            if result.killed:
                timeout_text += (
                    " and then killed because it did not exit after being "
                    "terminated"
                )
            lines.append(timeout_text + ".")

        if result.exit_code is not None:
            lines.append(f"FFmpeg exited with code {result.exit_code}.")
        elif result.killed:
            lines.append("FFmpeg could not be reaped after being killed.")
        elif result.timed_out:
            lines.append("FFmpeg was still running after the close timeout.")

        if result.stdin_close_error is not None and result.failed:
            lines.append(
                f"Error while closing FFmpeg stdin: {result.stdin_close_error}"
            )

        for note in result.cleanup_notes:
            lines.append(note)

        if self._capture_stderr:
            tail = result.stderr_tail
            if tail:
                lines.append(
                    f"FFmpeg stderr tail (last {self.stderr_tail_bytes} bytes):"
                    f"\n{tail}"
                )
            else:
                lines.append("No FFmpeg stderr output was captured.")
        else:
            lines.append(
                "FFmpeg diagnostics were redirected to the caller-provided "
                "logfile and were not read by MoviePy."
            )

        lines.extend(self._diagnostic_hints(result.stderr_tail))

        return "\n".join(lines)

    def _diagnostic_hints(self, stderr_tail):
        """Return writer-specific hints based on captured diagnostics."""
        return []

    # ------------------------------------------------------------------
    # Context manager / destructor
    # ------------------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.close()
        except BaseException as close_error:
            if exc_type is None:
                raise
            _append_cleanup_note(
                exc_value,
                "Additionally, cleaning up the FFmpeg writer failed: "
                f"{close_error}",
            )
        return False

    def __del__(self):
        try:
            self.close()
        except BaseException:
            pass
