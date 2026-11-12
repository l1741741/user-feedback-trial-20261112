"""MoviePy audio reading with ffmpeg."""

import subprocess as sp
import wave

import numpy as np

from moviepy.config import FFMPEG_BINARY
from moviepy.tools import cross_platform_popen_params, ffmpeg_escape_filename
from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos

# Small tolerance (in frames) used when discretizing a time ``t`` into a
# frame index with ``floor(fps * t)``.  It absorbs floating-point
# representation error (e.g. ``fps * (k / fps)`` may evaluate to a hair
# below ``k``) without ever moving a time genuinely inside a frame to the
# next one: at any reasonable sample rate, one frame lasts orders of
# magnitude longer than ``_FRAME_EPS / fps`` seconds.
_FRAME_EPS = 1e-6


class FFMPEG_AudioReader:
    """A class to read the audio in either video files or audio files
    using ffmpeg. ffmpeg will read any audio and transform them into
    raw data.

    The valid time range is the half-open interval ``[0, duration)`` where
    ``duration = n_frames / fps`` and ``n_frames`` is the real number of
    frames of the file (read from the file itself whenever the container
    allows it, e.g. PCM WAV).  Times outside that range return silence
    (zeros) with the right number of channels.

    Both scalar times and 1-D arrays of times are discretized the same
    way, ``floor(fps * t)`` (up to a tiny floating-point tolerance), so a
    scalar access and a vector access at the same time always return the
    same sample, regardless of the current state of the internal buffer.

    Parameters
    ----------

    filename
      Name of any video or audio file, like ``video.mp4`` or
      ``sound.wav`` etc.

    buffersize
      The size of the buffer to use. Should be bigger than the buffer
      used by ``write_audiofile``

    print_infos
      Print the ffmpeg infos on the file being read (for debugging)

    fps
      Desired frames per second in the decoded signal that will be
      received from ffmpeg

    nbytes
      Desired number of bytes (1,2,4) in the signal that will be
      received from ffmpeg

    audio_stream_index
      The index of the audio stream to read from the file.
    """

    def __init__(
        self,
        filename,
        buffersize,
        decode_file=False,
        print_infos=False,
        fps=44100,
        nbytes=2,
        nchannels=2,
        audio_stream_index=0,
    ):
        self.filename = filename
        self.nbytes = nbytes
        self.fps = fps
        self.format = "s%dle" % (8 * nbytes)
        self.codec = "pcm_s%dle" % (8 * nbytes)
        self.nchannels = nchannels
        infos = ffmpeg_parse_infos(
            filename, decode_file=decode_file, print_infos=print_infos
        )
        self.bitrate = infos["audio_bitrate"]
        self.infos = infos
        self.proc = None
        self.audio_stream_index = audio_stream_index

        # Number of frames of the decoded stream, and the half-open valid
        # time range [0, duration) derived from it.  Use the real frame
        # count of the file when it is exactly known (PCM WAV header);
        # ffmpeg's own "Duration" tag only has centisecond precision and
        # would misplace the end of the valid range by hundreds of frames.
        self.n_frames = self._probe_n_frames(infos)
        self.duration = self.n_frames / float(self.fps)

        self.buffersize = max(1, min(self.n_frames + 1, buffersize))
        self.buffer = None
        self.buffer_startframe = 0
        self.initialize()
        self.buffer_around(1)

    def _probe_n_frames(self, infos):
        """Return the exact number of frames of the decoded stream.

        For PCM WAV files the frame count and sample rate are read from
        the file header (standard library ``wave``), which is exact.
        For other containers, fall back to the duration reported by
        ffmpeg, rounded to the nearest frame.
        """
        try:
            with wave.open(self.filename, "rb") as wav_file:
                file_fps = wav_file.getframerate()
                file_frames = wav_file.getnframes()
            if file_fps > 0:
                return int(round(file_frames * self.fps / float(file_fps)))
        except Exception:
            # Not a PCM WAV file readable by the ``wave`` module: fall
            # back to the (approximate) duration reported by ffmpeg.
            pass
        return int(round(infos["duration"] * self.fps))

    def initialize(self, start_time=0):
        """Opens the file, creates the pipe.

        Any previously spawned ffmpeg process is terminated first, so
        re-initializing never leaves orphan ffmpeg processes behind.
        """
        self.close()  # if any

        if start_time != 0:
            offset = min(1, start_time)
            i_arg = [
                "-ss",
                "%.06f" % (start_time - offset),
                "-i",
                ffmpeg_escape_filename(self.filename),
                "-map",
                "0:a:%d" % self.audio_stream_index,
                "-vn",
                "-ss",
                "%.06f" % offset,
            ]
        else:
            i_arg = ["-i", ffmpeg_escape_filename(self.filename), "-vn"]

        cmd = (
            [FFMPEG_BINARY]
            + i_arg
            + [
                "-loglevel",
                "error",
                "-f",
                self.format,
                "-acodec",
                self.codec,
                "-ar",
                "%d" % self.fps,
                "-ac",
                "%d" % self.nchannels,
                "-",
            ]
        )

        popen_params = cross_platform_popen_params(
            {
                "bufsize": self.buffersize,
                "stdout": sp.PIPE,
                "stderr": sp.PIPE,
                "stdin": sp.DEVNULL,
            }
        )

        self.proc = sp.Popen(cmd, **popen_params)

        # ``self.pos`` is the (0-indexed) number of the next frame that
        # will be read from the stream.
        self.pos = int(round(self.fps * start_time))

    def skip_chunk(self, chunksize):
        """Skip a chunk of audio data by reading and discarding the specified number of
        frames from the audio stream. The audio stream is read from the `proc` stdout.
        After skipping the chunk, the `pos` attribute is updated accordingly.

        Parameters
        ----------
        chunksize (int):
          The number of audio frames to skip.
        """
        _ = self.proc.stdout.read(self.nchannels * chunksize * self.nbytes)
        self.pos = self.pos + chunksize

    def read_chunk(self, chunksize):
        """Read a chunk of audio data from the audio stream.

        This method reads a chunk of audio data from the audio stream. The
        specified number of frames, given by `chunksize`, is read from the
        `proc` stdout. The audio data is returned as a NumPy array, where
        each row corresponds to a frame and each column corresponds to a
        channel. If there is not enough audio left to read (a genuine
        short read at end of file), the remaining portion is padded with
        zeros -- never with stale data from a previous buffer -- ensuring
        that the returned array has the desired length. The `pos`
        attribute is updated accordingly.

        Parameters
        ----------
        chunksize (float):
          The desired number of audio frames to read.

        """
        # chunksize is not being autoconverted from float to int
        chunksize = int(round(chunksize))
        s = self.proc.stdout.read(self.nchannels * chunksize * self.nbytes)
        data_type = {1: "int8", 2: "int16", 4: "int32"}[self.nbytes]
        result = np.frombuffer(s, dtype=data_type)
        result = (1.0 * result / 2 ** (8 * self.nbytes - 1)).reshape(
            (int(len(result) / self.nchannels), self.nchannels)
        )

        # Pad the read chunk with zeros when there isn't enough audio
        # left to read, so the buffer is always at full length.
        pad = np.zeros((chunksize - len(result), self.nchannels), dtype=result.dtype)
        result = np.concatenate([result, pad])
        self.pos = self.pos + chunksize
        return result

    def seek(self, pos):
        """Move the stream to the 0-indexed frame ``pos``.

        Note for coders: getting an arbitrary frame in the file with
        ffmpeg can be painfully slow if some decoding has to be done.
        This function tries to avoid fetching arbitrary frames whenever
        possible, by moving between adjacent frames, and only spawns a
        new ffmpeg process (terminating the previous one) when the target
        is behind us or very far ahead.
        """
        pos = max(0, int(pos))
        if self.proc is None:
            self.initialize(1.0 * pos / self.fps)
        elif (pos < self.pos) or (pos > (self.pos + 100000)):
            t = 1.0 * pos / self.fps
            self.initialize(t)
        elif pos > self.pos:
            self.skip_chunk(pos - self.pos)
        # last case standing: pos = current pos
        self.pos = pos

    def _frame_indices(self, tt):
        """Discretize time(s) into frame indices with ``floor(fps * t)``.

        A tiny tolerance absorbs floating-point representation error so
        that e.g. ``t = k / fps`` maps exactly to frame ``k`` and the last
        legal time is never rounded out of the valid range.  Negative
        times are handled by the caller *before* this discretization, so
        a tiny negative time can never be truncated into frame 0.
        """
        return np.floor(self.fps * np.asarray(tt, dtype=float) + _FRAME_EPS).astype(
            np.int64
        )

    def get_frame(self, tt):
        """Retrieve the audio frame(s) corresponding to the given timestamp(s).

        Parameters
        ----------
        tt (float or numpy.ndarray):
          The timestamp(s) at which to retrieve the audio frame(s).
          If `tt` is a single float value, the frame corresponding to that
          timestamp is returned. If `tt` is a NumPy array of timestamps, an
          array of frames corresponding to each timestamp is returned, in
          the same order as the input. Times outside ``[0, duration)``
          yield rows of zeros; empty arrays yield an empty result.
        """
        if isinstance(tt, (list, tuple, np.ndarray)):
            return self._get_frames_array(np.asarray(tt, dtype=float))

        # Scalar access.
        t = float(tt)
        if t < 0 or t >= self.duration:
            # Out of the half-open valid range [0, duration): silence.
            return np.zeros(self.nchannels)

        ind = min(int(self._frame_indices(t)[()]), self.n_frames - 1)

        if self.buffer is None or not (
            0 <= (ind - self.buffer_startframe) < len(self.buffer)
        ):
            # out of the buffer: recenter the buffer
            self.buffer_around(ind)

        # read the frame in the buffer
        return self.buffer[ind - self.buffer_startframe]

    def _get_frames_array(self, tt):
        """Vector access: one frame per timestamp, in input order.

        The requested frames are fetched by contiguous ranges small
        enough to fit the buffer, so a single spread-out access never
        pulls the whole audio into memory, and the buffer is left in a
        consistent state for subsequent scalar accesses.
        """
        if tt.ndim == 0:
            return self.get_frame(float(tt))
        if tt.ndim != 1:
            raise ValueError(
                "FFMPEG_AudioReader.get_frame only accepts scalars or "
                "1-D arrays of times, got shape %s" % (tt.shape,)
            )

        result = np.zeros((len(tt), self.nchannels))
        if tt.size == 0:
            return result

        # elements of t that are actually in the half-open valid range
        # [0, duration); everything else stays zero.
        in_time = (tt >= 0) & (tt < self.duration)
        if not in_time.any():
            return result

        rows = np.nonzero(in_time)[0]
        frames = self._frame_indices(tt[rows])
        # Legal times can never discretize out of the valid range, even
        # when floating-point rounding pushes them a hair past the end.
        np.minimum(frames, self.n_frames - 1, out=frames)

        # Process the frames in sorted order, grouped into contiguous
        # ranges that provably fit in one buffer (span <= (buffersize-1)//2
        # frames around the first frame of the group).  This keeps memory
        # bounded by the buffersize whatever the span of the request is.
        order = np.argsort(frames, kind="stable")
        sorted_frames = frames[order]
        span_limit = (self.buffersize - 1) // 2

        start = 0
        total = len(sorted_frames)
        while start < total:
            stop = start + 1
            limit = sorted_frames[start] + span_limit
            while stop < total and sorted_frames[stop] <= limit:
                stop += 1

            group_frames = sorted_frames[start:stop]
            self.buffer_around(int(group_frames[0]))
            indices = group_frames - self.buffer_startframe
            result[rows[order[start:stop]]] = self.buffer[indices]
            start = stop

        return result

    def buffer_around(self, frame_number):
        """Fill the buffer with frames, centered on frame_number if possible.

        After this call, frame ``frame_number`` is inside the buffer, the
        buffer covers ``[buffer_startframe, buffer_startframe + buffersize)``
        (zero-padded past end of file), and the stream position is
        consistent with the end of the buffer.
        """
        # start-frame for the buffer
        new_bufferstart = max(0, int(frame_number) - self.buffersize // 2)

        if self.buffer is not None:
            current_f_end = self.buffer_startframe + len(self.buffer)
            reuse_possible = (
                self.buffer_startframe < new_bufferstart < current_f_end
                # The tail of the current buffer can only be reused if the
                # stream is still positioned right after it (an external
                # seek() may have moved it since the buffer was filled).
                and self.pos == current_f_end
            )
            if reuse_possible:
                # We already have part of what must be read
                conserved = current_f_end - new_bufferstart
                chunksize = self.buffersize - conserved
                if chunksize > 0:
                    array = self.read_chunk(chunksize)
                    self.buffer = np.vstack([self.buffer[-conserved:], array])
                else:
                    self.buffer = self.buffer[-self.buffersize :].copy()
            else:
                self.seek(new_bufferstart)
                self.buffer = self.read_chunk(self.buffersize)
        else:
            self.seek(new_bufferstart)
            self.buffer = self.read_chunk(self.buffersize)

        self.buffer_startframe = new_bufferstart

    def close(self):
        """Closes the reader, terminating the subprocess if is still alive.

        Safe to call multiple times.
        """
        proc = getattr(self, "proc", None)
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                proc.wait()
            # Release the pipes even if the process had already exited.
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
            self.proc = None

    def __del__(self):
        # If the garbage collector comes, make sure the subprocess is terminated.
        self.close()
