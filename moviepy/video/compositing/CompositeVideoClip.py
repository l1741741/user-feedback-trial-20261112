"""Main video composition interface of MoviePy."""

from collections import OrderedDict
from functools import reduce

import numpy as np

from moviepy.audio.AudioClip import CompositeAudioClip
from moviepy.video.VideoClip import ColorClip, VideoClip


#: Maximum number of entries kept in the memoization cache of a
#: ``CompositeVideoClip``. The cache only exists so that ``get_frame(t)``
#: and ``mask.get_frame(t)`` called for the same time ``t`` (in any order)
#: can share a single compositing pass. It is strictly bounded -- only the
#: most recent times are kept -- so rendering a long video never accumulates
#: one cached mask per frame.
COMPOSE_CACHE_MAXSIZE = 4


def _is_transparent_color(color):
    """True if ``color`` is an RGBA color whose alpha is not fully opaque.

    Color components follow the MoviePy 0-255 convention, so only an alpha
    of 255 is considered opaque.
    """
    if color is None or isinstance(color, str) or not hasattr(color, "__getitem__"):
        return False
    try:
        return len(color) == 4 and color[3] < 255
    except (IndexError, TypeError):
        return False


class CompositeVideoClip(VideoClip):
    """
    A VideoClip made of other videoclips displayed together. This is the
    base class for most compositions.

    Parameters
    ----------

    size
      The size (width, height) of the final clip.

    clips
      A list of videoclips.

      Clips with a higher ``layer`` attribute will be displayed
      on top of other clips in a lower layer.
      If two or more clips share the same ``layer``,
      then the one appearing latest in ``clips`` will be displayed
      on top (i.e. it has the higher layer).

      For each clip:

      - The attribute ``pos`` determines where the clip is placed.
          See ``VideoClip.set_pos``
      - The mask of the clip determines which parts are visible.

      Finally, if all the clips in the list have their ``duration``
      attribute set, then the duration of the composite video clip
      is computed automatically

    bg_color
      Color for the unmasked and unfilled regions. Set to None for these
      regions to be transparent (will be slower).
      Default is black (0, 0, 0).

    use_bgclip
      Set to True if the first clip in the list should be used as the
      'background' on which all other clips are blitted. That first clip must
      have the same size as the final clip. If it has no transparency, the final
      clip will have no mask.

    is_mask
      Set to True if the composite clip is a mask.

    memoize_mask
        If True (default), the result of compositing a frame (image and
        mask) at a given time is memoized, so that ``get_frame(t)`` and
        ``mask.get_frame(t)`` share a single computation no matter which
        one is called first. The cache is bounded: only the
        ``COMPOSE_CACHE_MAXSIZE`` most recent times are kept, so memory
        usage does not grow with the number of frames rendered, and a
        cached result is only ever returned for the exact time it was
        computed for. If False, image and mask are recomputed on every
        access and produce the exact same pixels.



    The clip with the highest FPS will be the FPS of the composite clip.

    """

    def __init__(
        self,
        clips,
        size=None,
        bg_color=(0, 0, 0),
        use_bgclip=False,
        is_mask=False,
        memoize_mask=True,
    ):
        if size is None:
            size = clips[0].size

        if use_bgclip and (clips[0].mask is None):
            transparent = False
        elif bg_color is None:
            transparent = True
        elif use_bgclip:
            # The background clip has a mask, so the composition is transparent
            transparent = True
        else:
            # An RGBA background color whose alpha is not fully opaque also
            # makes the composition transparent
            transparent = _is_transparent_color(bg_color)

        # If we must not use first clip as background and no background
        # color was provided, generate a transparent background (a fully
        # transparent RGBA color, or 0.0 for masks)
        if (not use_bgclip) and bg_color is None:
            bg_color = 0.0 if is_mask else (0, 0, 0, 0)

        fpss = [clip.fps for clip in clips if getattr(clip, "fps", None)]
        self.fps = max(fpss) if fpss else None

        VideoClip.__init__(self)

        self.size = size
        self.is_mask = is_mask
        self.clips = clips
        self.bg_color = bg_color
        self.memoize_mask = memoize_mask

        # Use first clip as background if necessary, else use color
        # either set by user or previously generated
        if use_bgclip:
            self.bg = clips[0]
            self.clips = clips[1:]
            self.created_bg = False
        else:
            self.clips = clips
            self.bg = ColorClip(size, color=self.bg_color, is_mask=is_mask)
            self.created_bg = True

        # order self.clips by layer
        self.clips = sorted(self.clips, key=lambda clip: clip.layer_index)

        # compute duration
        ends = [clip.end for clip in self.clips]
        if None not in ends:
            duration = max(ends)
            self.duration = duration
            self.end = duration

        # compute audio
        audioclips = [v.audio for v in self.clips if v.audio is not None]
        if audioclips:
            self.audio = CompositeAudioClip(audioclips)

        # Bounded memoization cache shared by the RGB and mask frame
        # generation, so both stay consistent whichever is requested first.
        self._compose_cache = OrderedDict()

        # compute mask if necessary. The mask is generated by the very same
        # compositing routine as the RGB frames (see _compose_frame_and_mask)
        # so that both always agree, in whichever order they are requested.
        # A composite of masks (is_mask=True) needs no mask of its own: its
        # frames already are alpha values.
        if transparent and not is_mask:
            self.mask = self._make_mask()

    def _make_mask(self):
        """Build the mask clip of a transparent composite clip.

        The returned clip is a grayscale (values in 0-1) VideoClip whose
        frames are the alpha channel produced by the same compositing pass
        as the RGB frames of this clip.
        """
        composite = self

        def mask_frame_function(t):
            _, mask = composite._compose_frame_and_mask(t)
            if mask is None:
                # The composition is fully opaque at this time
                mask = np.ones((composite.size[1], composite.size[0]), dtype=float)
            return np.asarray(mask, dtype=float)

        mask = VideoClip(is_mask=True)
        mask.frame_function = mask_frame_function
        mask.size = self.size
        if self.duration is not None:
            mask.duration = self.duration
            mask.end = self.duration
        return mask

    def _cache_get(self, t):
        """Returns the cached composition for the exact time ``t``, or None."""
        try:
            entry = self._compose_cache[t]
        except KeyError:
            return None
        self._compose_cache.move_to_end(t)
        return entry

    def _cache_put(self, t, entry):
        """Stores a composition in the bounded cache, evicting the oldest."""
        self._compose_cache[t] = entry
        self._compose_cache.move_to_end(t)
        while len(self._compose_cache) > COMPOSE_CACHE_MAXSIZE:
            self._compose_cache.popitem(last=False)

    def _compose_frame_and_mask(self, t):
        """Composites the RGB frame and the alpha mask at time ``t``.

        Both are computed by this single routine so they cannot disagree,
        and (when ``memoize_mask`` is True) the bounded cache is consulted
        and updated, so a frame and its mask are composited only once per
        time, in whichever order they are requested.

        Returns a tuple ``(frame, mask)`` where ``frame`` is a ``(H, W, 3)``
        uint8 array and ``mask`` is a ``(H, W)`` float array, or None if the
        composition is fully opaque at time ``t``. The returned arrays are
        private copies: mutating them does not corrupt the cache.
        """
        if self.memoize_mask:
            cached = self._cache_get(t)
            if cached is not None:
                frame, mask = cached
                if mask is not None:
                    mask = mask.copy()
                return frame.copy(), mask

        bg_t = t - self.bg.start
        bg_frame = self.bg.get_frame(bg_t).astype("uint8")
        clip_height, clip_width = bg_frame.shape[:2]

        current_mask = None
        if self.bg.mask is not None:
            bgm_t = t - self.bg.mask.start
            # Copy: the mask of the background may return the same array on
            # every call, which must not be mutated by the compositing
            current_mask = np.array(self.bg.mask.get_frame(bgm_t), dtype=float)

            # Resize bg_mask to match bg_frame, always use top left corner
            if bg_frame.shape[:2] != current_mask.shape[:2]:
                mask_height, mask_width = current_mask.shape[:2]

                # If mask is larger, crop it
                if mask_width > clip_width or mask_height > clip_height:
                    current_mask = current_mask[:clip_height, :clip_width]

                # If mask is smaller, fill it with zeros
                if mask_width < clip_width or mask_height < clip_height:
                    new_mask = np.zeros(
                        (clip_height, clip_width), dtype=current_mask.dtype
                    )
                    new_mask[:mask_height, :mask_width] = current_mask
                    current_mask = new_mask

        # For each clip apply on top of current img
        current_frame = bg_frame
        for clip in self.playing_clips(t):
            current_frame, current_mask = clip.compose_on(
                current_frame, t, current_mask
            )

        if self.memoize_mask:
            cached_mask = None if current_mask is None else current_mask.copy()
            self._cache_put(t, (current_frame.copy(), cached_mask))

        return current_frame, current_mask

    def frame_function(self, t):
        """The clips playing at time `t` are blitted over one another."""
        if self.is_mask:
            # A composite of masks: blit the masks of the clips playing at
            # time `t` over one another, starting from a blank mask, so
            # repeated calls never accumulate alpha.
            cached = self._cache_get(t) if self.memoize_mask else None
            if cached is not None:
                return cached.copy()

            mask = np.zeros((self.size[1], self.size[0]), dtype=float)
            for clip in self.playing_clips(t):
                mask = clip.compose_mask(mask, t)

            if self.memoize_mask:
                self._cache_put(t, mask)
                return mask.copy()
            return mask

        frame, _ = self._compose_frame_and_mask(t)
        return frame

    def playing_clips(self, t=0):
        """Returns a list of the clips in the composite clips that are
        actually playing at the given time `t`.
        """
        return [clip for clip in self.clips if clip.is_playing(t)]

    def close(self):
        """Closes the instance, releasing all the resources.

        Only the resources owned by the composite itself are released: the
        background clip it created and its composite audio track. The clips
        given in ``clips`` (and their own audio) may be shared with other
        compositions and are deliberately left untouched.
        """
        if self.created_bg and self.bg:
            # Only close the background clip if it was locally created.
            # Otherwise, it remains the job of whoever created it.
            self.bg.close()
            self.bg = None
        if hasattr(self, "audio") and self.audio:
            self.audio.close()
            self.audio = None
        if hasattr(self, "_compose_cache"):
            self._compose_cache.clear()
        super().close()


def clips_array(array, rows_widths=None, cols_heights=None, bg_color=None):
    """Given a matrix whose rows are clips, creates a CompositeVideoClip where
    all clips are placed side by side horizontally for each clip in each row
    and one row on top of the other for each row. So given next matrix of clips
    with same size:

    ```python
    clips_array([[clip1, clip2, clip3], [clip4, clip5, clip6]])
    ```

    the result will be a CompositeVideoClip with a layout displayed like:

    ```
    ┏━━━━━━━┳━━━━━━━┳━━━━━━━┓
    ┃       ┃       ┃       ┃
    ┃ clip1 ┃ clip2 ┃ clip3 ┃
    ┃       ┃       ┃       ┃
    ┣━━━━━━━╋━━━━━━━╋━━━━━━━┫
    ┃       ┃       ┃       ┃
    ┃ clip4 ┃ clip5 ┃ clip6 ┃
    ┃       ┃       ┃       ┃
    ┗━━━━━━━┻━━━━━━━┻━━━━━━━┛
    ```

    If some clips doesn't fulfill the space required by the rows or columns
    in which are placed, that space will be filled by the color defined in
    ``bg_color``.

    array
      Matrix of clips included in the returned composited video clip.

    rows_widths
      Widths of the different rows in pixels. If ``None``, is set automatically.

    cols_heights
      Heights of the different columns in pixels. If ``None``, is set automatically.

    bg_color
       Fill color for the masked and unfilled regions. Set to ``None`` for these
       regions to be transparent (processing will be slower).
    """
    array = np.array(array)
    sizes_array = np.array([[clip.size for clip in line] for line in array])

    # find row width and col_widths automatically if not provided
    if rows_widths is None:
        rows_widths = sizes_array[:, :, 1].max(axis=1)
    if cols_heights is None:
        cols_heights = sizes_array[:, :, 0].max(axis=0)

    # compute start positions of X for rows and Y for columns
    xs = np.cumsum([0] + list(cols_heights))
    ys = np.cumsum([0] + list(rows_widths))

    for j, (x, ch) in enumerate(zip(xs[:-1], cols_heights)):
        for i, (y, rw) in enumerate(zip(ys[:-1], rows_widths)):
            clip = array[i, j]
            w, h = clip.size
            # if clip not fulfill row width or column height
            if (w < ch) or (h < rw):
                clip = CompositeVideoClip(
                    [clip.with_position("center")], size=(ch, rw), bg_color=bg_color
                ).with_duration(clip.duration)

            array[i, j] = clip.with_position((x, y))

    return CompositeVideoClip(array.flatten(), size=(xs[-1], ys[-1]), bg_color=bg_color)


def concatenate_videoclips(
    clips, method="chain", transition=None, bg_color=None, is_mask=False, padding=0
):
    """Concatenates several video clips.

    Returns a video clip made by clip by concatenating several video clips.
    (Concatenated means that they will be played one after another).

    There are two methods:

    - method="chain": will produce a clip that simply outputs
      the frames of the successive clips, without any correction if they are
      not of the same size of anything. If none of the clips have masks the
      resulting clip has no mask, else the mask is a concatenation of masks
      (using completely opaque for clips that don't have masks, obviously).
      If you have clips of different size and you want to write directly the
      result of the concatenation to a file, use the method "compose" instead.

    - method="compose", if the clips do not have the same resolution, the final
      resolution will be such that no clip has to be resized.
      As a consequence the final clip has the height of the highest clip and the
      width of the widest clip of the list. All the clips with smaller dimensions
      will appear centered. The border will be transparent if mask=True, else it
      will be of the color specified by ``bg_color``.

    The clip with the highest FPS will be the FPS of the result clip.

    Parameters
    ----------
    clips
      A list of video clips which must all have their ``duration``
      attributes set.
    method
      "chain" or "compose": see above.
    transition
      A clip that will be played between each two clips of the list.

    bg_color
      Only for method='compose'. Color of the background.
      Set to None for a transparent clip

    padding
      Only for method='compose'. Duration during two consecutive clips.
      Note that for negative padding, a clip will partly play at the same
      time as the clip it follows (negative padding is cool for clips who fade
      in on one another). A non-null padding automatically sets the method to
      `compose`.

    """
    if transition is not None:
        clip_transition_pairs = [[v, transition] for v in clips[:-1]]
        clips = reduce(lambda x, y: x + y, clip_transition_pairs) + [clips[-1]]
        transition = None

    timings = np.cumsum([0] + [clip.duration for clip in clips])

    sizes = [clip.size for clip in clips]

    w = max(size[0] for size in sizes)
    h = max(size[1] for size in sizes)

    timings = np.maximum(0, timings + padding * np.arange(len(timings)))
    timings[-1] -= padding  # Last element is the duration of the whole

    if method == "chain":

        def frame_function(t):
            i = max([i for i, e in enumerate(timings) if e <= t])
            return clips[i].get_frame(t - timings[i])

        def get_mask(clip):
            mask = clip.mask or ColorClip(clip.size, color=1, is_mask=True)
            if mask.duration is None:
                mask.duration = clip.duration
            return mask

        result = VideoClip(is_mask=is_mask, frame_function=frame_function)
        if any([clip.mask is not None for clip in clips]):
            masks = [get_mask(clip) for clip in clips]
            result.mask = concatenate_videoclips(masks, method="chain", is_mask=True)
            result.clips = clips
    elif method == "compose":
        result = CompositeVideoClip(
            [
                clip.with_start(t).with_position("center")
                for (clip, t) in zip(clips, timings)
            ],
            size=(w, h),
            bg_color=bg_color,
            is_mask=is_mask,
        )
    else:
        raise Exception(
            "MoviePy Error: The 'method' argument of "
            "concatenate_videoclips must be 'chain' or 'compose'"
        )

    result.timings = timings

    result.start_times = timings[:-1]
    result.start, result.duration, result.end = 0, timings[-1], timings[-1]

    audio_t = [
        (clip.audio, t) for clip, t in zip(clips, timings) if clip.audio is not None
    ]
    if audio_t:
        result.audio = CompositeAudioClip(
            [a.with_start(t) for a, t in audio_t]
        ).with_duration(result.duration)

    fpss = [clip.fps for clip in clips if getattr(clip, "fps", None) is not None]
    result.fps = max(fpss) if fpss else None
    return result
