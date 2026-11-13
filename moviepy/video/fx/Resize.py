import numbers
from dataclasses import dataclass
from typing import Union

import cv2
import numpy as np

from moviepy.Effect import Effect


@dataclass
class Resize(Effect):
    """Effect returning a video clip that is a resized version of the clip.

    Parameters
    ----------

    new_size : tuple or float or function, optional
        Can be either
        - ``(width, height)`` in pixels or a float representing
        - A scaling factor, like ``0.5``.
        - A function of time returning one of these.

    height : int, optional
        Height of the new clip in pixels. The width is then computed so
        that the width/height ratio is conserved.

    width : int, optional
        Width of the new clip in pixels. The height is then computed so
        that the width/height ratio is conserved.

    apply_to_mask : bool, optional
        When ``True`` (default) and the clip has a mask, the mask is
        resized with the same geometric transform as the RGB frames, and
        the RGB frames are resampled in premultiplied-color space (colors
        multiplied by alpha, resampled, then un-multiplied) so that
        hidden colors of fully transparent pixels cannot bleed into
        visible ones. When ``False``, only the RGB frames are resized and
        the mask is left completely untouched.

    Examples
    --------

    .. code:: python

        clip.with_effects([vfx.Resize((460,720))]) # New resolution: (460,720)
        clip.with_effects([vfx.Resize(0.6)]) # width and height multiplied by 0.6
        clip.with_effects([vfx.Resize(width=800)]) # height computed automatically.
        clip.with_effects([vfx.Resize(lambda t : 1+0.02*t)]) # slow clip swelling
    """

    new_size: Union[tuple, float, callable] = None
    height: int = None
    width: int = None
    apply_to_mask: bool = True

    @staticmethod
    def _interpolation(pic, new_size):
        """Interpolation rule shared by all resampling paths: bilinear when
        upsizing, area when downsizing."""
        lx, ly = int(new_size[0]), int(new_size[1])
        if lx > pic.shape[1] or ly > pic.shape[0]:
            # For upsizing use linear for good quality & decent speed
            interpolation = cv2.INTER_LINEAR
        else:
            # For dowsizing use area to prevent aliasing
            interpolation = cv2.INTER_AREA
        return (lx, ly), interpolation

    def resizer(self, pic, new_size):
        """Resize a uint8 image using openCV (classic, mask-less path)."""
        (lx, ly), interpolation = self._interpolation(pic, new_size)
        return cv2.resize(+pic.astype("uint8"), (lx, ly), interpolation=interpolation)

    def float_resizer(self, pic, new_size):
        """Resize a float image using openCV, keeping full float precision
        (no uint8 round-trip)."""
        (lx, ly), interpolation = self._interpolation(pic, new_size)
        return cv2.resize(
            pic.astype("float64"), (lx, ly), interpolation=interpolation
        )

    def _premultiplied_frame(self, rgb_frame, mask_frame, new_size):
        """Resize one RGB frame together with its alpha mask, as one
        geometric transform applied in premultiplied-color space.

        The RGB colors are multiplied by the alpha, resampled with the
        exact same geometry/interpolation as the alpha itself, and the
        true colors are then recovered by dividing by the resized alpha.
        This prevents hidden colors of fully transparent pixels from
        bleeding into their visible neighbors.
        """
        alpha = mask_frame.astype("float64")
        if alpha.min() >= 1.0:
            # Fully opaque frame: identical to the classic uint8 resize.
            return self.resizer(rgb_frame, new_size)

        premultiplied = rgb_frame.astype("float64") * alpha[..., np.newaxis]
        resized_premultiplied = self.float_resizer(premultiplied, new_size)
        resized_alpha = np.clip(self.float_resizer(alpha, new_size), 0.0, 1.0)

        out = np.zeros(resized_premultiplied.shape, dtype="float64")
        np.divide(
            resized_premultiplied,
            resized_alpha[..., np.newaxis],
            out=out,
            where=resized_alpha[..., np.newaxis] > 0,
        )
        return np.clip(np.rint(out), 0, 255).astype("uint8")

    def _apply_with_size_getter(self, clip, get_new_size):
        """Build the resized clip for a (possibly time-dependent)
        ``t -> [w, h]`` size getter."""
        if clip.is_mask:
            # Masks are resampled in float precision and clipped to [0, 1];
            # they are never rounded through uint8.
            def mask_filter(get_frame, t):
                return np.clip(
                    self.float_resizer(get_frame(t), get_new_size(t)), 0.0, 1.0
                )

            return clip.transform(mask_filter, keep_duration=True)

        if self.apply_to_mask and clip.mask is not None:
            # RGB and alpha share the same geometric transform, resampled
            # in premultiplied-color space, frame by frame (so a
            # time-varying mask is honored at every t).
            def premultiplied_filter(get_frame, t):
                return self._premultiplied_frame(
                    get_frame(t), clip.mask.get_frame(t), get_new_size(t)
                )

            new_clip = clip.transform(premultiplied_filter, keep_duration=True)
            new_clip.mask = clip.mask.with_effects(
                [Resize(get_new_size, apply_to_mask=False)]
            )
            return new_clip

        # Classic path: no mask, or mask transformation explicitly disabled
        # with apply_to_mask=False (the mask is then left untouched).
        def filter(get_frame, t):
            return self.resizer(get_frame(t).astype("uint8"), get_new_size(t))

        return clip.transform(filter, keep_duration=True)

    def apply(self, clip):
        """Apply the effect to the clip."""
        w, h = clip.size

        if self.new_size is not None:

            def translate_new_size(new_size_):
                """Returns a [w, h] pair from `new_size_`. If `new_size_` is a
                scalar, then work out the correct pair using the clip's size.
                Otherwise just return `new_size_`
                """
                if isinstance(new_size_, numbers.Number):
                    return [new_size_ * w, new_size_ * h]
                else:
                    return new_size_

            if hasattr(self.new_size, "__call__"):
                # The resizing is a function of time

                def get_new_size(t):
                    return translate_new_size(self.new_size(t))

                return self._apply_with_size_getter(clip, get_new_size)

            new_size = translate_new_size(self.new_size)

        elif self.height is not None:
            if hasattr(self.height, "__call__"):

                def func(t):
                    return 1.0 * int(self.height(t)) / h

                return clip.with_effects(
                    [Resize(func, apply_to_mask=self.apply_to_mask)]
                )

            else:
                new_size = [w * self.height / h, self.height]

        elif self.width is not None:
            if hasattr(self.width, "__call__"):

                def func(t):
                    return 1.0 * self.width(t) / w

                return clip.with_effects(
                    [Resize(func, apply_to_mask=self.apply_to_mask)]
                )

            else:
                new_size = [self.width, h * self.width / w]
        else:
            raise ValueError(
                "You must provide either 'new_size' or 'height' or 'width'"
            )

        # From here, the resizing is constant (not a function of time)
        return self._apply_with_size_getter(clip, lambda t: new_size)
