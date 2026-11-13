"""MoviePy audio writing with ffmpeg."""

import os

import numpy as np
import proglog

from moviepy._ffmpeg_writer import _FFmpegWriterBase
from moviepy.config import FFMPEG_BINARY
from moviepy.decorators import requires_duration
from moviepy.tools import ffmpeg_escape_filename


class FFMPEG_AudioWriter(_FFmpegWriterBase):
    """
    A class to write an AudioClip into an audio file.

    Parameters
    ----------

    filename
      Name of any video or audio file, like ``video.mp4`` or ``sound.wav`` etc.

    size
      Size (width,height) in pixels of the output video.

    fps_input
      Frames per second of the input audio (given by the AudioClip being
      written down).

    nbytes : int, optional
      Number of bytes per sample. Default is 2 (16-bit audio).

    nchannels : int, optional
      Number of audio channels. Default is 2 (stereo).

    codec : str, optional
        The codec to use for the output. Default is ``libfdk_aac``.

    bitrate:
      A string indicating the bitrate of the final video. Only
      relevant for codecs which accept a bitrate.

    input_video : str, optional
      Path to an input video file. If provided, the audio will be muxed with this video.
      If not provided, the output will be audio-only.

    logfile : file-like object or None, optional
      A file object where FFMPEG logs will be written. If None, logs are suppressed.

    ffmpeg_params : list of str, optional
      Additional FFMPEG command-line parameters to customize the output.

    close_timeout : float, optional
      Maximum number of seconds to wait for ffmpeg to exit after its input is
      closed, and again after it is terminated or killed. Must be a positive
      finite number.

    stderr_tail_bytes : int, optional
      Maximum number of stderr bytes kept in memory for diagnostics. Must be a
      positive integer. Only the last bytes are retained, and invalid UTF-8 is
      decoded using replacement characters.
    """

    def __init__(
        self,
        filename,
        fps_input,
        nbytes=2,
        nchannels=2,
        codec="libfdk_aac",
        bitrate=None,
        input_video=None,
        logfile=None,
        ffmpeg_params=None,
        close_timeout=5.0,
        stderr_tail_bytes=65536,
    ):
        super().__init__(
            filename,
            logfile=logfile,
            close_timeout=close_timeout,
            stderr_tail_bytes=stderr_tail_bytes,
        )
        self.codec = codec
        self.ext = self.filename.split(".")[-1]

        # ``FFMPEG_BINARY`` is normally a string, but accepting a token
        # sequence keeps construction replaceable in tests and wrappers.
        binary = (
            list(FFMPEG_BINARY)
            if isinstance(FFMPEG_BINARY, (list, tuple))
            else [FFMPEG_BINARY]
        )

        # order is important
        cmd = [
            *binary,
            "-y",
            "-loglevel",
            "error" if self._capture_stderr else "info",
            "-f",
            "s%dle" % (8 * nbytes),
            "-acodec",
            "pcm_s%dle" % (8 * nbytes),
            "-ar",
            "%d" % fps_input,
            "-ac",
            "%d" % nchannels,
            "-i",
            "-",
        ]
        if input_video is None:
            cmd.extend(["-vn"])
        else:
            cmd.extend(["-i", ffmpeg_escape_filename(input_video), "-vcodec", "copy"])

        cmd.extend(["-acodec", codec] + ["-ar", "%d" % fps_input])
        cmd.extend(["-strict", "-2"])  # needed to support codec 'aac'
        if bitrate is not None:
            cmd.extend(["-ab", bitrate])
        if ffmpeg_params is not None:
            cmd.extend(ffmpeg_params)
        cmd.extend([ffmpeg_escape_filename(self.filename)])

        self._start_process(cmd)

    def write_frames(self, frames_array):
        """Send the audio frame (a chunck of ``AudioClip``) to ffmpeg for writting"""
        if not frames_array.flags["C_CONTIGUOUS"]:
            frames_array = np.ascontiguousarray(frames_array)

        self._write_bytes(frames_array.tobytes(), "audio chunk")

    def _diagnostic_hints(self, stderr_tail):
        return _audio_error_hints(stderr_tail, self.codec, self.ext)


def _audio_error_hints(stderr_tail, codec, ext):
    """Return writer-specific hints based on captured diagnostics."""
    hints = []
    if "Unknown encoder" in stderr_tail:
        hints.append(
            "The audio export failed because FFMPEG didn't find the "
            f"specified codec for audio encoding {codec}. "
            "Please install this codec or change the codec when calling "
            "write_videofile or write_audiofile.\nFor instance for mp3:\n"
            "   >>> write_videofile('myvid.mp4', audio_codec='libmp3lame')"
        )

    elif "incorrect codec parameters ?" in stderr_tail:
        hints.append(
            "The audio export failed, possibly because the "
            f"codec specified for the video {codec} is not compatible"
            f" with the given extension {ext}. Please specify a "
            "valid 'codec' argument in write_audiofile or 'audio_codoc'"
            "argument in write_videofile. This would be "
            "'libmp3lame' for mp3, 'libvorbis' for ogg..."
        )

    elif "bitrate not specified" in stderr_tail:
        hints.append(
            "The audio export failed, possibly because the "
            "bitrate you specified was too high or too low for "
            "the audio codec."
        )

    elif "Invalid encoder type" in stderr_tail:
        hints.append(
            "The audio export failed because the codec "
            "or file extension you provided is not suitable for audio"
        )

    return hints


@requires_duration
def ffmpeg_audiowrite(
    clip,
    filename,
    fps,
    nbytes,
    buffersize,
    codec="libvorbis",
    bitrate=None,
    write_logfile=False,
    ffmpeg_params=None,
    logger="bar",
):
    """
    A function that wraps the FFMPEG_AudioWriter to write an AudioClip
    to a file.
    """
    filename = os.fspath(filename)
    if write_logfile:
        logfile = open(filename + ".log", "w+")
    else:
        logfile = None
    logger = proglog.default_bar_logger(logger)
    logger(message="MoviePy - Writing audio in %s" % filename)

    try:
        writer = FFMPEG_AudioWriter(
            filename,
            fps,
            nbytes,
            clip.nchannels,
            codec=codec,
            bitrate=bitrate,
            logfile=logfile,
            ffmpeg_params=ffmpeg_params,
        )

        try:
            for chunk in clip.iter_chunks(
                chunksize=buffersize,
                quantize=True,
                nbytes=nbytes,
                fps=fps,
                logger=logger,
            ):
                writer.write_frames(chunk)

            writer.close()
        except BaseException as error:
            try:
                writer.close()
            except BaseException as close_error:
                if hasattr(error, "add_note"):
                    error.add_note(
                        "Additionally, cleaning up the FFmpeg writer failed: "
                        f"{close_error}"
                    )
            raise
    except BaseException as error:
        # ``logfile`` belongs to this function, so reading it for hints is
        # safe.  The writer itself never reads caller-owned logfiles.
        if write_logfile:
            _append_logfile_hints(error, logfile, codec, filename.split(".")[-1])
        raise
    finally:
        if write_logfile:
            logfile.close()

    logger(message="MoviePy - Done.")


def _append_logfile_hints(error, logfile, codec, ext):
    """Add codec hints found in moviepy's own logfile to ``error``."""
    try:
        logfile.flush()
        logfile.seek(0)
        content = logfile.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        hints = _audio_error_hints(content, codec, ext)
        if hints:
            _extend_error_message(error, "\n".join(hints))
    except Exception:
        # Diagnostics must never replace the real export error.
        pass


def _extend_error_message(error, extra):
    """Append text to an exception message when it supports that."""
    if not error.args or not isinstance(error.args[0], str):
        return
    error.args = (error.args[0] + "\n\n" + extra,) + error.args[1:]
