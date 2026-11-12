"""Pixel-exact tests for transparent ``CompositeVideoClip`` compositing.

All media is generated programmatically (solid color frames, constant and
time-varying masks); nothing is downloaded.

The tests cover:
- consistency between ``get_frame`` and ``mask.get_frame`` regardless of
  which one is called first, and regardless of the order times are queried;
- bounded memoization (``memoize_mask=True``) and identical pixels with
  ``memoize_mask=False``;
- no alpha accumulation when the same time is queried repeatedly;
- analytic source-over compositing of semi-transparent layers over
  (semi-)transparent backgrounds, with positions, negative positions,
  oversized/undersized masks, layer ordering and differing start/end;
- nested composites matching the equivalent flattened composition;
- ``close()`` releasing only the resources the composite itself created.
"""

import random
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import moviepy.video.compositing.CompositeVideoClip as composite_module
from moviepy.audio.AudioClip import AudioClip
from moviepy.video.compositing.CompositeVideoClip import CompositeVideoClip
from moviepy.video.VideoClip import ColorClip, ImageClip, VideoClip

# Upper bound of the memoization cache (falls back to the documented value
# so these tests can also run against the pre-fix implementation)
COMPOSE_CACHE_MAXSIZE = getattr(composite_module, "COMPOSE_CACHE_MAXSIZE", 4)


def cache_size(composite):
    """Number of memoized mask/frame entries currently held by a composite."""
    if hasattr(composite, "_compose_cache"):
        return len(composite._compose_cache)
    # pre-fix implementation: an unbounded dict on the mask composite
    mask = getattr(composite, "mask", None)
    if mask is not None and hasattr(mask, "precomputed"):
        return len(mask.precomputed)
    return 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def solid_layer(
    color,
    size,
    alpha=None,
    pos=(0, 0),
    start=0.0,
    duration=10.0,
    layer_index=0,
    mask_clip=None,
):
    """A solid-color RGB clip, optionally with a constant alpha or a
    custom mask clip (possibly of a different size than the RGB frames)."""
    w, h = size
    arr = np.tile(np.array(color, dtype=np.uint8), (h, w, 1))
    clip = ImageClip(arr, duration=duration)
    if mask_clip is not None:
        clip = clip.with_mask(mask_clip)
    elif alpha is not None:
        clip = clip.with_mask(
            ColorClip(size, color=float(alpha), is_mask=True, duration=duration)
        )
    clip = clip.with_position(pos).with_start(start)
    if layer_index:
        clip = clip.with_layer_index(layer_index)
    return clip


def constant_mask(size, value, duration=10.0):
    w, h = size
    return ImageClip(np.full((h, w), float(value)), is_mask=True, duration=duration)


def ramp_mask(size, duration=10.0):
    """A time-varying mask: alpha grows linearly with time (clipped to 1)."""
    w, h = size

    def frame_function(t):
        return np.full((h, w), min(1.0, 0.1 + 0.4 * t))

    return VideoClip(is_mask=True, frame_function=frame_function, duration=duration)


def reference_compose(canvas_size, bg_rgba, layers):
    """Independent source-over compositing reference.

    canvas_size
      (width, height) of the canvas.
    bg_rgba
      None or (r, g, b, a) with 0-255 components for the background.
    layers
      List of (rgb, alpha, pos) tuples, bottom layer first. ``rgb`` is a
      (h, w, 3) array, ``alpha`` a (h, w) array of 0-1 values and ``pos``
      the (x, y) position of the layer (may be negative).

    Returns (rgb, alpha): uint8 (H, W, 3) and float (H, W) arrays.
    """
    width, height = canvas_size
    out_rgb = np.zeros((height, width, 3), dtype=np.float64)
    out_a = np.zeros((height, width), dtype=np.float64)
    if bg_rgba is not None:
        out_rgb[:] = bg_rgba[:3]
        out_a[:] = bg_rgba[3] / 255.0

    for rgb, alpha, (px, py) in layers:
        h, w = alpha.shape
        x1, y1 = max(px, 0), max(py, 0)
        x2, y2 = min(px + w, width), min(py + h, height)
        if x2 <= x1 or y2 <= y1:
            continue
        sx1, sy1 = x1 - px, y1 - py
        a = alpha[sy1 : sy1 + (y2 - y1), sx1 : sx1 + (x2 - x1)].astype(np.float64)[
            ..., None
        ]
        c = rgb[sy1 : sy1 + (y2 - y1), sx1 : sx1 + (x2 - x1)].astype(np.float64)
        bg_a = out_a[y1:y2, x1:x2][..., None]
        bg_c = out_rgb[y1:y2, x1:x2]
        new_a = a + bg_a * (1 - a)
        safe_a = np.where(new_a == 0, 1.0, new_a)
        out_rgb[y1:y2, x1:x2] = (c * a + bg_c * bg_a * (1 - a)) / safe_a
        out_a[y1:y2, x1:x2] = new_a[..., 0]

    return np.round(out_rgb).astype(np.uint8), out_a


def build_demo_composite(memoize_mask):
    """A transparent composite exercising positions (incl. negative),
    constant and time-varying masks and an opaque layer."""
    red = solid_layer(
        (255, 0, 0), (4, 3), alpha=0.6, pos=(1, 2), start=0.5, duration=2.0
    )
    green = solid_layer((0, 255, 0), (3, 3), pos=(-1, -1), duration=10.0)
    green = green.with_mask(ramp_mask((3, 3)))
    blue = solid_layer(
        (0, 0, 255), (2, 2), alpha=0.75, pos=(3, 0), start=1.0, duration=2.0
    )
    yellow = solid_layer((255, 255, 0), (2, 1), pos=(5, 4), duration=10.0)
    return CompositeVideoClip(
        [red, green, blue, yellow], size=(7, 5), bg_color=None,
        memoize_mask=memoize_mask,
    )


TIMES = [0.0, 0.3, 0.75, 1.2, 2.4, 3.5]


# ---------------------------------------------------------------------------
# access order and memoization
# ---------------------------------------------------------------------------


def test_mask_and_rgb_are_access_order_independent():
    """RGB-first, mask-first and memoize_mask=False must give identical
    pixels, for in-order and out-of-order time access."""
    comp_rgb_first = build_demo_composite(memoize_mask=True)
    comp_mask_first = build_demo_composite(memoize_mask=True)
    comp_no_memo = build_demo_composite(memoize_mask=False)

    shuffled = list(TIMES)
    random.Random(2026).shuffle(shuffled)

    results_a, results_b = {}, {}
    # RGB frame requested before the mask, times visited in order
    for t in TIMES:
        results_a[t] = (comp_rgb_first.get_frame(t), comp_rgb_first.mask.get_frame(t))
    # mask requested before the RGB frame, times visited out of order
    for t in shuffled:
        results_b[t] = (comp_mask_first.mask.get_frame(t), comp_mask_first.get_frame(t))

    for t in TIMES:
        frame_a, mask_a = results_a[t]
        mask_b, frame_b = results_b[t]
        # no memoization at all
        frame_c = comp_no_memo.get_frame(t)
        mask_c = comp_no_memo.mask.get_frame(t)

        assert frame_a.shape == (5, 7, 3) and frame_a.dtype == np.uint8
        assert mask_a.shape == (5, 7) and mask_a.dtype.kind == "f"

        np.testing.assert_array_equal(frame_a, frame_b)
        np.testing.assert_array_equal(frame_a, frame_c)
        np.testing.assert_array_equal(mask_a, mask_b)
        np.testing.assert_array_equal(mask_a, mask_c)


def test_repeated_access_same_time_does_not_accumulate_alpha():
    comp = build_demo_composite(memoize_mask=True)
    t = 1.2
    first_mask = comp.mask.get_frame(t)
    first_frame = comp.get_frame(t)
    for _ in range(5):
        np.testing.assert_array_equal(comp.mask.get_frame(t), first_mask)
        np.testing.assert_array_equal(comp.get_frame(t), first_frame)
    # alpha is a coverage value: it can never exceed 1, however many
    # times the same instant is queried
    assert first_mask.max() <= 1.0
    assert first_mask.min() >= 0.0


def test_mask_cache_is_bounded_and_never_stale():
    """Rendering many frames must not accumulate one cached mask per frame,
    and a cached entry must only ever be served for its own time."""
    comp = build_demo_composite(memoize_mask=True)
    reference = build_demo_composite(memoize_mask=False)

    times = [0.02 * i for i in range(300)]
    for t in times:
        comp.get_frame(t)
        comp.mask.get_frame(t)
        assert cache_size(comp) <= COMPOSE_CACHE_MAXSIZE

    # mask-first access pattern also stays bounded
    for t in times:
        comp.mask.get_frame(t + 100.0)
        comp.get_frame(t + 100.0)
        assert cache_size(comp) <= COMPOSE_CACHE_MAXSIZE

    # after all that traffic, results are still exact (no stale reuse)
    for t in [0.13, 1.11, 2.71, 107.03]:
        np.testing.assert_array_equal(
            comp.get_frame(t), reference.get_frame(t)
        )
        np.testing.assert_array_equal(
            comp.mask.get_frame(t), reference.mask.get_frame(t)
        )


def test_mask_cache_of_nested_composites_is_bounded():
    inner = build_demo_composite(memoize_mask=True)
    outer = CompositeVideoClip(
        [inner.with_position((1, 1))], size=(10, 8), bg_color=None
    )
    for i in range(120):
        t = 0.05 * i
        outer.get_frame(t)
        outer.mask.get_frame(t)
        assert cache_size(outer) <= COMPOSE_CACHE_MAXSIZE
        assert cache_size(inner) <= COMPOSE_CACHE_MAXSIZE


def test_is_mask_composite_repeated_and_bounded():
    """A composite of masks (is_mask=True) must also be consistent on
    repeated access and keep its cache bounded."""
    half = constant_mask((3, 2), 0.5).with_position((1, 0))
    quarter = constant_mask((2, 2), 0.25).with_position((2, 1))
    comp = CompositeVideoClip(
        [half, quarter], size=(5, 4), is_mask=True, bg_color=0.0
    )
    first = comp.get_frame(0.0)
    assert first.shape == (4, 5)
    for _ in range(3):
        np.testing.assert_array_equal(comp.get_frame(0.0), first)
    assert first.max() <= 1.0
    # 0.5 over 0.25 at the overlap: 0.5 + 0.25 * (1 - 0.5) = 0.625
    assert first[1, 2] == pytest.approx(0.625)
    for i in range(50):
        comp.get_frame(0.1 * i)
        assert cache_size(comp) <= COMPOSE_CACHE_MAXSIZE


# ---------------------------------------------------------------------------
# analytic pixel tests
# ---------------------------------------------------------------------------


def test_source_over_pixels_with_semitransparent_background():
    """Semi-transparent RGBA background + positioned layers with alpha,
    including a negative position, checked against an independent
    source-over reference."""
    bg_color = (10, 20, 30, 128)  # alpha 128/255
    red = solid_layer((200, 0, 0), (3, 2), alpha=0.5, pos=(2, 1))
    green = solid_layer((0, 200, 0), (2, 2), alpha=0.75, pos=(-1, 3))
    opaque = solid_layer((0, 0, 200), (2, 1), pos=(4, 0))
    comp = CompositeVideoClip(
        [red, green, opaque], size=(7, 5), bg_color=bg_color
    )

    frame = comp.get_frame(0.0)
    mask = comp.mask.get_frame(0.0)

    layers = [
        (
            np.tile(np.array(c, dtype=np.uint8), (h, w, 1)),
            np.full((h, w), a),
            p,
        )
        for c, (w, h), a, p in [
            ((200, 0, 0), (3, 2), 0.5, (2, 1)),
            ((0, 200, 0), (2, 2), 0.75, (-1, 3)),
            ((0, 0, 200), (2, 1), 1.0, (4, 0)),
        ]
    ]
    ref_rgb, ref_a = reference_compose((7, 5), bg_color, layers)

    np.testing.assert_allclose(frame, ref_rgb, atol=2)
    np.testing.assert_allclose(mask, ref_a, atol=1e-6)

    # the background alpha shows through where nothing covers it
    assert mask[4, 6] == pytest.approx(128 / 255)
    # and its color is not darkened by any premultiplication
    np.testing.assert_array_equal(frame[4, 6], [10, 20, 30])


def test_transparent_pixel_colors_are_not_mixed_with_black():
    """A lone semi-transparent red layer over a transparent background must
    keep its pure color (un-premultiplied), not red-blended-with-black."""
    red = solid_layer((255, 0, 0), (2, 2), alpha=0.5, pos=(1, 1))
    comp = CompositeVideoClip([red], size=(4, 4), bg_color=None)

    frame = comp.get_frame(0.0)
    mask = comp.mask.get_frame(0.0)

    np.testing.assert_array_equal(frame[1, 1], [255, 0, 0])
    assert mask[1, 1] == pytest.approx(0.5)
    assert mask[0, 0] == 0.0


def test_oversized_mask_is_cropped_and_undersized_mask_is_zero_padded():
    """Masks of a different size than their clip's frames: larger masks are
    cropped (top-left), smaller ones padded with zeros (transparent)."""
    # 2x2 red clip with a 4x4 half-alpha mask -> cropped to the 2x2 frame
    red = solid_layer(
        (255, 0, 0), (2, 2), pos=(1, 1), mask_clip=constant_mask((4, 4), 0.5)
    )
    # 3x3 green clip with a 1x1 mask -> only its top-left pixel is visible
    green = solid_layer(
        (0, 255, 0), (3, 3), pos=(3, 0), mask_clip=constant_mask((1, 1), 0.75)
    )
    comp = CompositeVideoClip([red, green], size=(6, 5), bg_color=None)

    frame = comp.get_frame(0.0)
    mask = comp.mask.get_frame(0.0)

    # red occupies rows 1-2, cols 1-2 with alpha 0.5
    assert mask[1, 1] == pytest.approx(0.5)
    assert mask[2, 2] == pytest.approx(0.5)
    assert mask[1, 3] == 0.0  # the oversized mask must not spill over
    np.testing.assert_array_equal(frame[1, 1], [255, 0, 0])

    # green: only pixel (0, 3) is visible, with alpha 0.75
    assert mask[0, 3] == pytest.approx(0.75)
    np.testing.assert_array_equal(frame[0, 3], [0, 255, 0])
    assert mask[0, 4] == 0.0
    assert mask[1, 4] == 0.0


def test_negative_position_is_clipped():
    layer = solid_layer((0, 255, 0), (3, 3), alpha=1.0, pos=(-1, -2))
    comp = CompositeVideoClip([layer], size=(4, 4), bg_color=None)
    frame = comp.get_frame(0.0)
    mask = comp.mask.get_frame(0.0)

    # only the bottom-right 2x1 corner of the layer is visible
    expected_mask = np.zeros((4, 4))
    expected_mask[0, 0:2] = 1.0
    np.testing.assert_array_equal(mask, expected_mask)
    np.testing.assert_array_equal(frame[0, 0], [0, 255, 0])
    np.testing.assert_array_equal(frame[0, 1], [0, 255, 0])


def test_layer_ordering_and_same_layer_tie_break():
    bottom = solid_layer((255, 0, 0), (2, 2), layer_index=1)
    top = solid_layer((0, 0, 255), (2, 2), layer_index=2)
    comp = CompositeVideoClip([top, bottom], size=(2, 2), bg_color=None)
    np.testing.assert_array_equal(comp.get_frame(0.0)[0, 0], [0, 0, 255])

    # same layer_index: the clip listed last wins
    first = solid_layer((255, 0, 0), (2, 2))
    second = solid_layer((0, 0, 255), (2, 2))
    comp = CompositeVideoClip([first, second], size=(2, 2), bg_color=None)
    np.testing.assert_array_equal(comp.get_frame(0.0)[0, 0], [0, 0, 255])
    comp = CompositeVideoClip([second, first], size=(2, 2), bg_color=None)
    np.testing.assert_array_equal(comp.get_frame(0.0)[0, 0], [255, 0, 0])


def test_clips_with_different_start_and_end():
    a = solid_layer((255, 0, 0), (2, 2), alpha=0.5, pos=(0, 0))
    a = a.with_start(0.5).with_end(1.5)
    b = solid_layer((0, 0, 255), (2, 2), alpha=0.5, pos=(1, 1))
    b = b.with_start(1.0).with_end(2.0)
    comp = CompositeVideoClip([a, b], size=(3, 3), bg_color=None)

    def expected(t):
        layers = []
        if 0.5 <= t < 1.5:
            layers.append(
                (np.full((2, 2, 3), [255, 0, 0], dtype=np.uint8),
                 np.full((2, 2), 0.5), (0, 0))
            )
        if 1.0 <= t < 2.0:
            layers.append(
                (np.full((2, 2, 3), [0, 0, 255], dtype=np.uint8),
                 np.full((2, 2), 0.5), (1, 1))
            )
        return reference_compose((3, 3), None, layers)

    for t in [0.2, 0.7, 1.2, 1.7, 2.5]:
        ref_rgb, ref_a = expected(t)
        np.testing.assert_allclose(comp.get_frame(t), ref_rgb, atol=2)
        np.testing.assert_allclose(comp.mask.get_frame(t), ref_a, atol=1e-6)


def test_time_varying_mask_matches_reference():
    layer = solid_layer((100, 150, 200), (3, 2), pos=(1, 1))
    layer = layer.with_mask(ramp_mask((3, 2)))
    comp = CompositeVideoClip([layer], size=(5, 4), bg_color=None)

    for t in [0.0, 0.5, 1.0, 2.0]:
        alpha = np.full((2, 3), min(1.0, 0.1 + 0.4 * t))
        rgb = np.tile(np.array([100, 150, 200], dtype=np.uint8), (2, 3, 1))
        ref_rgb, ref_a = reference_compose((5, 4), None, [(rgb, alpha, (1, 1))])
        np.testing.assert_allclose(comp.get_frame(t), ref_rgb, atol=2)
        np.testing.assert_allclose(comp.mask.get_frame(t), ref_a, atol=1e-6)


# ---------------------------------------------------------------------------
# nesting
# ---------------------------------------------------------------------------


def _nested_pieces():
    """Returns (inner_layers, inner_pos, inner_start) used by both the
    nested and the flattened composition."""
    red = solid_layer((255, 0, 0), (4, 3), alpha=0.6, pos=(1, 0), start=0.0)
    green = solid_layer((0, 255, 0), (3, 3), alpha=0.8, pos=(2, 1), start=0.0)
    return [red, green], (4, 3), 0.5


def test_nested_composite_matches_flattened():
    inner_layers, inner_pos, inner_start = _nested_pieces()

    inner = CompositeVideoClip(
        [layer.copy() for layer in inner_layers], size=(6, 4), bg_color=None
    )
    base = solid_layer((0, 0, 255), (12, 10), alpha=0.3, pos=(0, 0))
    top = solid_layer((255, 255, 0), (2, 2), pos=(9, 7))
    outer = CompositeVideoClip(
        [base, inner.with_position(inner_pos).with_start(inner_start), top],
        size=(12, 10),
        bg_color=None,
    )

    # flattened: the inner layers placed directly in the outer composite
    flat_layers = [solid_layer((0, 0, 255), (12, 10), alpha=0.3, pos=(0, 0))]
    for layer in inner_layers:
        px, py = layer.pos(0)
        flat_layers.append(
            layer.copy()
            .with_position((px + inner_pos[0], py + inner_pos[1]))
            .with_start(inner_start)
        )
    flat_layers.append(solid_layer((255, 255, 0), (2, 2), pos=(9, 7)))
    flat = CompositeVideoClip(flat_layers, size=(12, 10), bg_color=None)

    for t in [0.0, 0.4, 0.8, 1.5]:
        np.testing.assert_allclose(
            outer.get_frame(t), flat.get_frame(t), atol=2
        )
        np.testing.assert_allclose(
            outer.mask.get_frame(t), flat.mask.get_frame(t), atol=1e-5
        )


def test_nested_composite_mask_first_and_rgb_first():
    """Nested composites must also be independent of mask/RGB access order."""
    inner_layers, inner_pos, _ = _nested_pieces()

    def build():
        inner = CompositeVideoClip(
            [layer.copy() for layer in inner_layers], size=(6, 4), bg_color=None
        )
        return CompositeVideoClip(
            [inner.with_position(inner_pos)], size=(12, 10), bg_color=None
        )

    comp_a, comp_b = build(), build()
    for t in [0.0, 0.7, 1.3]:
        frame_a = comp_a.get_frame(t)
        mask_a = comp_a.mask.get_frame(t)
        mask_b = comp_b.mask.get_frame(t)
        frame_b = comp_b.get_frame(t)
        np.testing.assert_array_equal(frame_a, frame_b)
        np.testing.assert_array_equal(mask_a, mask_b)


# ---------------------------------------------------------------------------
# resource handling
# ---------------------------------------------------------------------------


class FlagVideoClip(ColorClip):
    """A ColorClip recording whether close() was called on it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed = False

    def close(self):
        self.closed = True
        super().close()


class FlagAudioClip(AudioClip):
    """An AudioClip recording whether close() was called on it."""

    def __init__(self, duration=10.0):
        super().__init__(
            frame_function=lambda t: (
                np.zeros(1) if np.isscalar(t) else np.zeros((len(t), 1))
            ),
            duration=duration,
        )
        self.closed = False

    def close(self):
        self.closed = True
        super().close()


def test_close_does_not_invalidate_shared_child_clips():
    shared_child = FlagVideoClip((4, 4), color=(255, 0, 0), duration=10.0)
    shared_child = shared_child.with_audio(FlagAudioClip())
    # with_audio returns a shallow copy; keep the flagged audio around
    shared_audio = shared_child.audio

    other_child = FlagVideoClip((2, 2), color=(0, 0, 255), duration=10.0)

    comp1 = CompositeVideoClip(
        [shared_child, other_child.with_position((1, 1))],
        size=(6, 6),
        bg_color=None,
    )
    comp2 = CompositeVideoClip([shared_child], size=(6, 6), bg_color=None)

    # sanity: both composites work before closing
    assert comp1.get_frame(0.0).shape == (6, 6, 3)
    assert comp2.get_frame(0.0).shape == (6, 6, 3)

    comp1.close()

    # the composite released what it created itself...
    assert comp1.bg is None
    assert comp1.audio is None

    # ...but the shared children survive: the other composite and the
    # child clips themselves are still fully usable
    assert not shared_child.closed
    assert not other_child.closed
    assert not shared_audio.closed
    np.testing.assert_array_equal(
        comp2.get_frame(0.0)[0, 0], [255, 0, 0]
    )
    np.testing.assert_array_equal(
        shared_child.get_frame(0.0)[0, 0], [255, 0, 0]
    )
    assert shared_audio.get_frame(0.0) is not None

    comp2.close()
    assert not shared_child.closed


def test_close_releases_owned_background_and_audio():
    comp = CompositeVideoClip(
        [solid_layer((1, 2, 3), (2, 2))], size=(4, 4), bg_color=None
    )
    assert comp.created_bg
    bg = comp.bg
    comp.close()
    assert comp.bg is None
    # closing twice must not fail
    comp.close()
    assert bg is not None  # the object itself is just released


def test_user_supplied_background_clip_is_not_closed():
    bg = FlagVideoClip((6, 6), color=(0, 255, 0), duration=10.0)
    child = solid_layer((255, 0, 0), (2, 2), pos=(1, 1))
    comp = CompositeVideoClip([bg, child], size=(6, 6), use_bgclip=True)
    comp.close()
    # the background was supplied by the caller: not ours to close
    assert not bg.closed
    assert comp.bg is bg


# ---------------------------------------------------------------------------
# RGBA PNG artifact
# ---------------------------------------------------------------------------


def test_save_rgba_png_of_composite():
    """Compose a scene and save it as an RGBA PNG demonstrating the actual
    compositing result (colors + alpha)."""
    canvas = (160, 120)
    # semi-transparent dark blue background layer (smaller than the canvas,
    # so the corners stay fully transparent)
    base = solid_layer((16, 32, 64), (100, 80), alpha=0.5, pos=(10, 10))
    # nested composite: opaque red square over semi-transparent green
    red = solid_layer((220, 40, 40), (50, 40), pos=(5, 5))
    green = solid_layer((40, 200, 40), (30, 30), alpha=0.6, pos=(25, 15))
    inner = CompositeVideoClip([red, green], size=(60, 50), bg_color=None)
    # yellow layer hanging off the canvas (negative position)
    yellow = solid_layer((240, 220, 40), (40, 30), alpha=0.9, pos=(-10, 90))
    scene = CompositeVideoClip(
        [base, inner.with_position((40, 30)), yellow],
        size=canvas,
        bg_color=None,
    )

    frame = scene.get_frame(0.0)
    mask = scene.mask.get_frame(0.0)
    assert frame.shape == (120, 160, 3)
    assert mask.shape == (120, 160)

    rgba = np.dstack([frame, np.round(mask * 255).astype(np.uint8)])
    out_path = Path(__file__).with_name("composite_transparency_demo.png")
    Image.fromarray(rgba, "RGBA").save(out_path)

    # PNG is lossless: reading it back must give the exact same RGBA pixels
    reread = np.asarray(Image.open(out_path).convert("RGBA"))
    np.testing.assert_array_equal(reread, rgba)

    # the scene really exercises transparency: fully transparent,
    # semi-transparent and fully opaque pixels are all present
    assert (rgba[..., 3] == 0).any()
    assert ((rgba[..., 3] > 0) & (rgba[..., 3] < 255)).any()
    assert (rgba[..., 3] == 255).any()
