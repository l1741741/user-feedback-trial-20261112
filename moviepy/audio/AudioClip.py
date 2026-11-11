"""Implements AudioClip (base class for audio clips) and its main subclasses:

- Audio clips: AudioClip, AudioFileClip, AudioArrayClip
- Composition: CompositeAudioClip
"""

import numbers
import os

import numpy as np
import proglog

from moviepy.audio.io.ffmpeg_audiowriter import ffmpeg_audiowrite
from moviepy.audio.io.ffplay_audiopreviewer import ffplay_audiopreview
from moviepy.Clip import Clip
from moviepy.decorators import convert_path_to_string, requires_duration
from moviepy.tools import extensions_dict


def _sample_count(duration, fps):
    """Number of samples on the half-open timeline ``[0, duration)``.

    The output sample points are ``i / fps`` for the non-negative integers
    ``i`` such that ``i / fps < duration``. The count is computed exactly
    with respect to that rule, robust to floating-point error in
    ``duration * fps``.
    """
    if duration is None or duration <= 0:
        return 0
    n = int(duration * fps)
    # Correct the floating-point rounding of ``duration * fps`` so that
    # ``n`` is exactly the smallest integer with ``n / fps >= duration``.
    while n > 0 and (n - 1) / fps >= duration:
        n -= 1
    while n / fps < duration:
        n += 1
    return n


def _validate_fps(fps):
    """Checks that ``fps`` is a usable (positive, finite) frame rate."""
    if fps is None or not np.isfinite(fps) or fps <= 0:
        raise ValueError(f"fps must be a positive finite number, got {fps!r}")
    return fps


class AudioClip(Clip):
    """Base class for audio clips.

    See ``AudioFileClip`` and ``CompositeAudioClip`` for usable classes.

    An AudioClip is a Clip with a ``frame_function``  attribute of
    the form `` t -> [ f_t ]`` for mono sound and
    ``t-> [ f1_t, f2_t ]`` for stereo sound (the arrays are Numpy arrays).
    The `f_t` are floats between -1 and 1. These bounds can be
    trespassed without problems (the program will put the
    sound back into the bounds at conversion time, without much impact).

    Parameters
    ----------

    frame_function
      A function `t-> frame at time t`. The frame does not mean much
      for a sound, it is just a float. What 'makes' the sound are
      the variations of that float in the time.

    duration
      Duration of the clip (in seconds). Some clips are infinite, in
      this case their duration will be ``None``.

    nchannels
      Number of channels (one or two for mono or stereo).

    Examples
    --------

    .. code:: python

        # Plays the note A in mono (a sine wave of frequency 440 Hz)
        import numpy as np
        frame_function = lambda t: np.sin(440 * 2 * np.pi * t)
        clip = AudioClip(frame_function, duration=5, fps=44100)
        clip.preview()

        # Plays the note A in stereo (two sine waves of frequencies 440 and 880 Hz)
        frame_function = lambda t: np.array([
            np.sin(440 * 2 * np.pi * t),
            np.sin(880 * 2 * np.pi * t)
        ]).T.copy(order="C")
        clip = AudioClip(frame_function, duration=3, fps=44100)
        clip.preview()

    """

    def __init__(self, frame_function=None, duration=None, fps=None):
        super().__init__()

        if fps is not None:
            self.fps = fps

        if frame_function is not None:
            self.frame_function = frame_function
            frame0 = self.get_frame(0)
            if hasattr(frame0, "__iter__"):
                self.nchannels = len(list(frame0))
            else:
                self.nchannels = 1
        if duration is not None:
            self.duration = duration
            self.end = duration

    @requires_duration
    def iter_chunks(
        self,
        chunksize=None,
        chunk_duration=None,
        fps=None,
        quantize=False,
        nbytes=2,
        logger=None,
    ):
        """Iterator that returns the whole sound array of the clip by chunks.

        The clip is sampled on the half-open timeline: sample points are
        ``i / fps`` for the non-negative integers ``i`` such that
        ``i / fps < duration``. This exact sequence of samples is cut into
        consecutive chunks of ``chunksize`` samples (the last chunk may be
        shorter, chunks are never re-distributed and no empty chunk is
        generated). A clip with ``duration == 0`` yields no chunk at all.
        """
        if fps is None:
            fps = self.fps
        fps = _validate_fps(fps)
        logger = proglog.default_bar_logger(logger)

        if chunk_duration is not None:
            if chunk_duration <= 0:
                raise ValueError(
                    f"chunk_duration must be positive, got {chunk_duration!r}"
                )
            chunksize = int(chunk_duration * fps)
        if chunksize is None:
            raise ValueError("Provide either chunksize or chunk_duration")
        chunksize = int(chunksize)
        if chunksize <= 0:
            raise ValueError(f"chunksize must be a positive integer, got {chunksize}")

        total_size = _sample_count(self.duration, fps)
        if total_size == 0:
            return

        nchunks = (total_size + chunksize - 1) // chunksize

        for i in logger.iter_bar(chunk=list(range(nchunks))):
            start = i * chunksize
            stop = min(start + chunksize, total_size)
            # Integer sample indices divided by fps: no floating-point
            # stepping, so the frames yielded never depend on chunksize.
            timings = np.arange(start, stop) / fps
            yield self.to_soundarray(
                timings, nbytes=nbytes, quantize=quantize, fps=fps, buffersize=chunksize
            )

    @requires_duration
    def to_soundarray(
        self, tt=None, fps=None, quantize=False, nbytes=2, buffersize=50000
    ):
        """
        Transforms the sound into an array that can be played by pygame
        or written in a wav file. See ``AudioClip.preview``.

        Parameters
        ----------

        fps
          Frame rate of the sound for the conversion.
          44100 for top quality.

        nbytes
          Number of bytes to encode the sound: 1 for 8bit sound,
          2 for 16bit, 4 for 32bit sound.

        """
        if tt is None:
            if fps is None:
                fps = self.fps
            fps = _validate_fps(fps)

            max_duration = 1.0 * buffersize / fps
            if self.duration > max_duration:
                # Same sample sequence as below, rendered chunk by chunk and
                # stacked along the time axis (axis 0) whatever the number of
                # channels is.
                return np.concatenate(
                    list(
                        self.iter_chunks(
                            fps=fps,
                            quantize=quantize,
                            nbytes=nbytes,
                            chunksize=buffersize,
                        )
                    ),
                    axis=0,
                )
            else:
                # Half-open timeline: samples i / fps with i / fps < duration.
                tt = np.arange(_sample_count(self.duration, fps)) / fps
        """
        elif len(tt)> 1.5*buffersize:
            nchunks = int(len(tt)/buffersize+1)
            tt_chunks = np.array_split(tt, nchunks)
            return stacker([self.to_soundarray(tt=ttc, buffersize=buffersize, fps=fps,
                                        quantize=quantize, nbytes=nbytes)
                              for ttc in tt_chunks])
        """
        snd_array = self.get_frame(tt)

        if quantize:
            snd_array = np.maximum(-0.99, np.minimum(0.99, snd_array))
            inttype = {1: "int8", 2: "int16", 4: "int32"}[nbytes]
            snd_array = (2 ** (8 * nbytes - 1) * snd_array).astype(inttype)

        return snd_array

    def max_volume(self, stereo=False, chunksize=50000, logger=None):
        """Returns the maximum volume level of the clip."""
        # max volume separated by channels if ``stereo`` and not mono
        stereo = stereo and self.nchannels > 1

        # zero for each channel
        maxi = np.zeros(self.nchannels)
        for chunk in self.iter_chunks(chunksize=chunksize, logger=logger):
            maxi = np.maximum(maxi, abs(chunk).max(axis=0))

        # if mono returns float, otherwise array of volumes by channel
        return maxi if stereo else maxi[0]

    @requires_duration
    @convert_path_to_string("filename")
    def write_audiofile(
        self,
        filename,
        fps=None,
        nbytes=2,
        buffersize=2000,
        codec=None,
        bitrate=None,
        ffmpeg_params=None,
        write_logfile=False,
        logger="bar",
    ):
        """Writes an audio file from the AudioClip.


        Parameters
        ----------

        filename
          Name of the output file, as a string or a path-like object.

        fps
          Frames per second. If not set, it will try default to self.fps if
          already set, otherwise it will default to 44100.

        nbytes
          Sample width (set to 2 for 16-bit sound, 4 for 32-bit sound)

        buffersize
          The sound is not generated all at once, but rather made by bunches
          of frames (chunks). ``buffersize`` is the size of such a chunk.
          Try varying it if you meet audio problems (but you shouldn't
          have to). Default to 2000

        codec
          Which audio codec should be used. If None provided, the codec is
          determined based on the extension of the filename. Choose
          'pcm_s16le' for 16-bit wav and 'pcm_s32le' for 32-bit wav.

        bitrate
          Audio bitrate, given as a string like '50k', '500k', '3000k'.
          Will determine the size and quality of the output file.
          Note that it mainly an indicative goal, the bitrate won't
          necessarily be the this in the output file.

        ffmpeg_params
          Any additional parameters you would like to pass, as a list
          of terms, like ['-option1', 'value1', '-option2', 'value2']

        write_logfile
          If true, produces a detailed logfile named filename + '.log'
          when writing the file

        logger
          Either ``"bar"`` for progress bar or ``None`` or any Proglog logger.

        """
        if not fps:
            if hasattr(self, "fps"):
                fps = self.fps
            else:
                fps = 44100

        if codec is None:
            name, ext = os.path.splitext(os.path.basename(filename))
            try:
                codec = extensions_dict[ext[1:]]["codec"][0]
            except KeyError:
                raise ValueError(
                    "MoviePy couldn't find the codec associated "
                    "with the filename. Provide the 'codec' "
                    "parameter in write_audiofile."
                )

        return ffmpeg_audiowrite(
            self,
            filename,
            fps,
            nbytes,
            buffersize,
            codec=codec,
            bitrate=bitrate,
            write_logfile=write_logfile,
            ffmpeg_params=ffmpeg_params,
            logger=logger,
        )

    @requires_duration
    def audiopreview(
        self, fps=None, buffersize=2000, nbytes=2, audio_flag=None, video_flag=None
    ):
        """
        Preview an AudioClip using ffplay

        Parameters
        ----------

        fps
            Frame rate of the sound. 44100 gives top quality, but may cause
            problems if your computer is not fast enough and your clip is
            complicated. If the sound jumps during the preview, lower it
            (11025 is still fine, 5000 is tolerable).

        buffersize
            The sound is not generated all at once, but rather made by bunches
            of frames (chunks). ``buffersize`` is the size of such a chunk.
            Try varying it if you meet audio problems (but you shouldn't
            have to).

        nbytes:
            Number of bytes to encode the sound: 1 for 8bit sound, 2 for
            16bit, 4 for 32bit sound. 2 bytes is fine.

        audio_flag, video_flag:
            Instances of class threading events that are used to synchronize
            video and audio during ``VideoClip.preview()``.
        """
        ffplay_audiopreview(
            clip=self,
            fps=fps,
            buffersize=buffersize,
            nbytes=nbytes,
            audio_flag=audio_flag,
            video_flag=video_flag,
        )

    def __add__(self, other):
        if isinstance(other, AudioClip):
            return concatenate_audioclips([self, other])
        return super(AudioClip, self).__add__(other)


class AudioArrayClip(AudioClip):
    """

    An audio clip made from a sound array.

    Parameters
    ----------

    array
      A Numpy array representing the sound, of size Nx1 for mono,
      Nx2 for stereo, or more generally NxC for C channels. A 1-D array
      of size N is treated as a mono (Nx1) sound.

    fps
      Frames per second : speed at which the sound is supposed to be
      played.

    """

    def __init__(self, array, fps):
        Clip.__init__(self)
        array = np.asarray(array)
        if array.ndim == 1:
            array = array.reshape((len(array), 1))
        if array.ndim != 2:
            raise ValueError(
                "AudioArrayClip expects a 2-D (N, C) array, got shape "
                f"{array.shape}"
            )
        self.array = array
        self.fps = fps
        self.nchannels = array.shape[1]
        self.duration = 1.0 * len(array) / fps

        def frame_function(t):
            """Complicated, but must be able to handle the case where t
            is a list of the form sin(t).

            Both scalar and vector times are sampled with
            ``floor(t * fps)`` and the ``[0, duration)`` boundary is
            decided on the time itself. Out-of-range times return zeros
            with the clip's number of channels.
            """
            if isinstance(t, np.ndarray):
                tt = np.atleast_1d(np.asarray(t, dtype=float))
                in_array = (tt >= 0) & (tt < self.duration)
                result = np.zeros((len(tt), self.nchannels))
                if in_array.any():
                    inds = np.floor(self.fps * tt[in_array]).astype(int)
                    # Guard against floating-point error at the very end.
                    inds = np.minimum(inds, len(self.array) - 1)
                    result[in_array] = self.array[inds]
                return result
            else:
                if t < 0 or t >= self.duration:
                    return np.zeros(self.nchannels)
                i = min(int(np.floor(self.fps * t)), len(self.array) - 1)
                return self.array[i]

        self.frame_function = frame_function


class CompositeAudioClip(AudioClip):
    """Clip made by composing several AudioClips.

    An audio clip made by putting together several audio clips.

    Parameters
    ----------

    clips
      List of audio clips, which may start playing at different times or
      together, depends on their ``start`` attributes. If all have their
      ``duration`` attribute set, the duration of the composite clip is
      computed automatically.
    """

    def __init__(self, clips):
        self.clips = clips
        self.nchannels = max(clip.nchannels for clip in self.clips)

        # self.duration is set at AudioClip
        duration = None
        for end in self.ends:
            if end is None:
                break
            duration = max(end, duration or 0)

        # self.fps is set at AudioClip
        fps = None
        for clip in self.clips:
            if hasattr(clip, "fps") and isinstance(clip.fps, numbers.Number):
                fps = max(clip.fps, fps or 0)

        super().__init__(duration=duration, fps=fps)

    @property
    def starts(self):
        """Returns starting times for all clips in the composition."""
        return (clip.start for clip in self.clips)

    @property
    def ends(self):
        """Returns ending times for all clips in the composition."""
        return (clip.end for clip in self.clips)

    def frame_function(self, t):
        """Renders a frame for the composition for the time ``t``.

        Channel handling follows an explicit rule: every clip's frame is
        normalized to a ``(..., nchannels)`` shape; a mono clip is played
        identically on all channels of the composition, and any other
        channel count mismatch raises an error instead of silently
        broadcasting along the wrong axis.
        """
        played_parts = [clip.is_playing(t) for clip in self.clips]

        vector_time = isinstance(t, np.ndarray)
        sounds = []
        for clip, part in zip(self.clips, played_parts):
            if part is False:
                continue
            sound = np.asarray(clip.get_frame(t - clip.start), dtype=float)
            if vector_time:
                if sound.ndim == 1:
                    # Mono clip sampled at vector times: (N,) -> (N, 1)
                    sound = sound.reshape((len(sound), 1))
                sound = sound * np.asarray(part).reshape((-1, 1))
            else:
                if sound.ndim == 0:
                    # Mono clip sampled at a scalar time: () -> (1,)
                    sound = sound.reshape(1)
                sound = sound * part

            if sound.shape[-1] == self.nchannels:
                pass
            elif sound.shape[-1] == 1:
                # Explicit broadcast rule: mono is centered, i.e. played
                # identically on every channel of the composition.
                sound = np.broadcast_to(
                    sound, sound.shape[:-1] + (self.nchannels,)
                )
            else:
                raise ValueError(
                    f"Cannot compose a {sound.shape[-1]}-channel clip into a "
                    f"{self.nchannels}-channel composition"
                )
            sounds.append(sound)

        if vector_time:
            zero = np.zeros((len(t), self.nchannels))
        else:
            zero = np.zeros(self.nchannels)

        return zero + sum(sounds)


def concatenate_audioclips(clips):
    """Concatenates one AudioClip after another, in the order that are passed
    to ``clips`` parameter.

    Parameters
    ----------

    clips
      List of audio clips, which will be played one after other.
    """
    # start, end/start2, end2/start3... end
    starts_end = np.cumsum([0, *[clip.duration for clip in clips]])
    newclips = [clip.with_start(t) for clip, t in zip(clips, starts_end[:-1])]

    return CompositeAudioClip(newclips).with_duration(starts_end[-1])
