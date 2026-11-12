from dataclasses import dataclass

import numpy as np

from moviepy.Clip import Clip
from moviepy.decorators import audio_video_effect
from moviepy.Effect import Effect
from moviepy.tools import convert_to_seconds


@dataclass
class AudioFadeIn(Effect):
    """Return an audio (or video) clip that is first mute, then the
    sound arrives progressively over ``duration`` seconds.

    Parameters
    ----------

    duration : float
        How long does it take for the sound to return to its normal level.
        Must be a finite, strictly positive number.

    Examples
    --------

    .. code:: python

        clip = VideoFileClip("media/chaplin.mp4")
        clip.with_effects([afx.AudioFadeIn("00:00:06")])
    """

    duration: float

    def __post_init__(self):
        self.duration = convert_to_seconds(self.duration)
        if not isinstance(
            self.duration, (int, float, np.number)
        ) or not np.isfinite(self.duration):
            raise ValueError(
                "AudioFadeIn 'duration' must be a finite number, got %r"
                % (self.duration,)
            )
        if self.duration <= 0:
            raise ValueError(
                "AudioFadeIn 'duration' must be strictly positive, got %r"
                % (self.duration,)
            )

    def _get_factor(self, t):
        """Fade-in gain at local time ``t``.

        Works for scalar times as well as numpy array times. The result has
        the same shape as ``t`` and is clipped to ``[0, 1]`` so times outside
        the fade-in window are never amplified nor phase-inverted.
        """
        return np.clip(np.asarray(t, dtype=float) / self.duration, 0, 1)

    @audio_video_effect
    def apply(self, clip: Clip) -> Clip:
        """Apply the effect to the clip."""
        if clip.duration is None:
            raise ValueError("Attribute 'duration' not set")

        def filter_frame(get_frame, t):
            frame = get_frame(t)
            factor = self._get_factor(t)
            if np.ndim(frame) > np.ndim(factor):
                # Multi-channel frame (..., C): apply the gain along the
                # time axis, never along the channel axis.
                factor = np.expand_dims(factor, axis=-1)
            return factor * frame

        return clip.transform(filter_frame)
