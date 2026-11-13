"""Analytic pixel tests for ``vfx.Resize`` / ``VideoClip.resized``.

All tests use small programmatically generated RGB uint8 matrices and
float masks -- no downloaded videos, external fonts, or other
compositors. Expected pixel values are derived analytically from the
documented resampling rules (INTER_LINEAR when upsizing, INTER_AREA when
downsizing) applied in premultiplied-color space.
"""

import numpy as np
from PIL import Image

import pytest

from moviepy import AudioClip, ImageClip, VideoClip, vfx
from moviepy.video.fx.Resize import Resize


def make_clip(rgb, mask=None, duration=2.0):
    """Build a VideoClip from a uint8 (H, W, 3) array and an optional
    float (H, W) mask in [0, 1]."""
    clip = ImageClip(rgb)
    if mask is not None:
        clip = clip.with_mask(ImageClip(mask, is_mask=True))
    return clip.with_duration(duration)


# ---------------------------------------------------------------------
# Hidden colors / premultiplied resampling
# ---------------------------------------------------------------------


def test_upscale_hidden_color_does_not_bleed():
    """1x2 clip: visible red next to fully transparent (hidden) blue,
    upscaled 2 -> 4 horizontally with INTER_LINEAR.

    Source coordinate of output x is (x + 0.5) * 2/4 - 0.5, so the alpha
    ramp is exactly [1, 0.75, 0.25, 0]. Premultiplied blue is 0
    everywhere, so no blue may appear in the output.
    """
    rgb = np.array([[[255, 0, 0], [0, 0, 255]]], dtype="uint8")
    mask = np.array([[1.0, 0.0]])
    resized = make_clip(rgb, mask).resized((4, 1))

    out = resized.get_frame(0)
    out_mask = resized.mask.get_frame(0)

    assert out.shape == (1, 4, 3)
    assert out.dtype == np.uint8
    np.testing.assert_allclose(out_mask[0], [1.0, 0.75, 0.25, 0.0], atol=1e-9)

    # The hidden blue must not pollute any output pixel.
    assert (out[0, :, 2] == 0).all()
    assert (out[0, :, 1] == 0).all()
    # True red is recovered wherever alpha > 0.
    assert (out[0, :3, 0] == 255).all()
    # Fully transparent output pixels have RGB = 0.
    assert list(out[0, 3]) == [0, 0, 0]


def test_downscale_hidden_color_does_not_bleed():
    """1x4 clip downscaled 4 -> 2 with INTER_AREA (plain averages)."""
    rgb = np.array(
        [[[255, 0, 0], [255, 0, 0], [0, 255, 0], [0, 255, 0]]], dtype="uint8"
    )
    mask = np.array([[1.0, 1.0, 0.0, 0.0]])
    resized = make_clip(rgb, mask).resized((2, 1))

    out = resized.get_frame(0)
    out_mask = resized.mask.get_frame(0)

    np.testing.assert_allclose(out_mask[0], [1.0, 0.0], atol=1e-9)
    # Visible half averages to pure red.
    assert list(out[0, 0]) == [255, 0, 0]
    # Hidden green must not leak into the transparent half.
    assert list(out[0, 1]) == [0, 0, 0]


def test_downscale_partial_alpha_recovers_premultiplied_color():
    """1x2 -> 1x1 with INTER_AREA: red at alpha 1 next to green at
    alpha 0.5. Premultiplied average is (127.5, 63.75, 0) with alpha
    0.75, so the recovered color is (170, 85, 0)."""
    rgb = np.array([[[255, 0, 0], [0, 255, 0]]], dtype="uint8")
    mask = np.array([[1.0, 0.5]])
    resized = make_clip(rgb, mask).resized((1, 1))

    out = resized.get_frame(0)
    out_mask = resized.mask.get_frame(0)

    np.testing.assert_allclose(out_mask[0, 0], 0.75, atol=1e-9)
    np.testing.assert_allclose(out[0, 0], [170, 85, 0], atol=1)


def test_fully_transparent_region_outputs_zero_without_nan():
    """A fully transparent clip resizes to RGB = 0, mask = 0, with no
    NaN or Inf, both upscaling and downscaling."""
    rng = np.random.default_rng(7)
    rgb = rng.integers(0, 256, size=(3, 5, 3)).astype("uint8")
    mask = np.zeros((3, 5))
    clip = make_clip(rgb, mask)

    for new_size in [(10, 6), (2, 1)]:
        resized = clip.resized(new_size)
        out = resized.get_frame(0)
        out_mask = resized.mask.get_frame(0)
        assert (out == 0).all()
        assert (out_mask == 0).all()
        assert np.isfinite(out).all() and np.isfinite(out_mask).all()


# ---------------------------------------------------------------------
# Float mask precision
# ---------------------------------------------------------------------


@pytest.mark.parametrize("new_size", [(12, 8), (3, 2)], ids=("up", "down"))
def test_constant_mask_keeps_float_precision(new_size):
    """A constant 0.123456 mask must stay 0.123456 after resizing --
    it must not be quantized to 31/255 through a uint8 round-trip."""
    rgb = np.full((4, 6, 3), [200, 100, 50], dtype="uint8")
    mask = np.full((4, 6), 0.123456)
    resized = make_clip(rgb, mask).resized(new_size)

    out_mask = resized.mask.get_frame(0)
    assert out_mask.dtype == np.float64
    np.testing.assert_allclose(out_mask, 0.123456, atol=1e-6)
    # 31/255 = 0.12156... would be the uint8-quantized value.
    assert abs(out_mask.mean() - 0.123456) < abs(out_mask.mean() - 31 / 255)

    # The constant color survives the premultiply/unpremultiply round-trip.
    out = resized.get_frame(0)
    np.testing.assert_allclose(out, np.full(out.shape, [200, 100, 50]), atol=1)


def test_independent_mask_clip_resized_in_float():
    """Resizing a standalone is_mask clip keeps float precision and
    clips the output to [0, 1]."""
    mask = np.full((4, 4), 0.123456)
    mask[0, 0] = 0.5
    mask_clip = ImageClip(mask, is_mask=True)

    up = mask_clip.resized((8, 8)).get_frame(0)
    assert up.dtype == np.float64
    # Interior (away from the 0.5 corner's interpolation footprint) is
    # exactly the constant.
    np.testing.assert_allclose(up[3:, 3:], 0.123456, atol=1e-6)
    assert up.min() >= 0.0 and up.max() <= 1.0
    # Not quantized on the 1/255 grid.
    assert abs(up[-1, -1] - 0.123456) < abs(up[-1, -1] - 31 / 255)

    down = mask_clip.resized((2, 2)).get_frame(0)
    assert down.dtype == np.float64
    assert down.min() >= 0.0 and down.max() <= 1.0
    # Bottom-right area average is exactly the constant.
    np.testing.assert_allclose(down[1, 1], 0.123456, atol=1e-6)

    # Values above 1 are clipped on output.
    hot_mask = ImageClip(np.full((2, 2), 1.2), is_mask=True)
    assert hot_mask.resized((4, 4)).get_frame(0).max() == 1.0


# ---------------------------------------------------------------------
# Preservation of classic behavior
# ---------------------------------------------------------------------


@pytest.mark.parametrize("new_size", [(14, 10), (3, 2)], ids=("up", "down"))
def test_fully_opaque_mask_matches_no_mask_resize(new_size):
    """With a fully opaque mask the RGB result must be exactly the
    classic uint8 resize, and the resized mask must stay 1."""
    rng = np.random.default_rng(42)
    rgb = rng.integers(0, 256, size=(5, 7, 3)).astype("uint8")

    plain = ImageClip(rgb).resized(new_size).get_frame(0)
    masked = make_clip(rgb, np.ones((5, 7))).resized(new_size)

    assert np.array_equal(masked.get_frame(0), plain)
    np.testing.assert_allclose(masked.mask.get_frame(0), 1.0, atol=1e-12)


@pytest.mark.parametrize(
    ("new_size", "interpolation"),
    [((6, 4), "linear"), ((2, 1), "area")],
    ids=("up-linear", "down-area"),
)
def test_no_mask_resize_matches_opencv(new_size, interpolation):
    """The classic no-mask path is unchanged: same pixels as a direct
    cv2.resize call with the documented interpolation rule."""
    import cv2

    rng = np.random.default_rng(3)
    rgb = rng.integers(0, 256, size=(3, 4, 3)).astype("uint8")
    inter = cv2.INTER_LINEAR if interpolation == "linear" else cv2.INTER_AREA
    expected = cv2.resize(rgb, new_size, interpolation=inter)

    out = ImageClip(rgb).resized(new_size).get_frame(0)
    assert np.array_equal(out, expected)


# ---------------------------------------------------------------------
# Dynamic sizes, time-varying masks, access order
# ---------------------------------------------------------------------


def make_dynamic_clip():
    """RGB: left half red, right half (hidden) blue, 4x4.
    Mask(t): left half 1.0, right half t/2 (time-varying)."""
    rgb = np.zeros((4, 4, 3), dtype="uint8")
    rgb[:, :2] = [255, 0, 0]
    rgb[:, 2:] = [0, 0, 255]

    def mask_frame(t):
        m = np.zeros((4, 4))
        m[:, :2] = 1.0
        m[:, 2:] = t / 2.0
        return m

    clip = ImageClip(rgb, duration=2.0)
    mask_clip = VideoClip(frame_function=mask_frame, is_mask=True, duration=2.0)
    return clip.with_mask(mask_clip)


def test_dynamic_size_time_varying_mask_and_access_order():
    """With a time-function new_size and a time-varying mask:
    - sizes and pixels are identical whatever order/ repetition frames
      and masks are fetched in,
    - the premultiplied color is recomputed at every t (not frozen at
      t=0, where the right half is fully transparent)."""
    resized = make_dynamic_clip().with_effects([vfx.Resize(lambda t: 1 + t)])

    # Deliberately irregular access: mask first, then RGB, out of order,
    # with repetitions.
    m1 = resized.mask.get_frame(1.0)
    f0 = resized.get_frame(0.0)
    f1 = resized.get_frame(1.0)
    m0 = resized.mask.get_frame(0.0)
    f1_again = resized.get_frame(1.0)
    m1_again = resized.mask.get_frame(1.0)

    # Repetitions and order-independent sizes/pixels.
    assert np.array_equal(f1, f1_again)
    assert np.array_equal(m1, m1_again)
    assert f0.shape == (4, 4, 3)  # factor 1 + 0
    assert f1.shape == (8, 8, 3)  # factor 1 + 1
    assert m0.shape == (4, 4)
    assert m1.shape == (8, 8)

    # Mask follows time: right half is 0 at t=0, 0.5 at t=1. At t=1 the
    # 4 -> 8 upscale blends the two boundary columns (3 and 4), so only
    # the pure interior columns are checked here.
    np.testing.assert_allclose(m0[:, 2:], 0.0, atol=1e-9)
    np.testing.assert_allclose(m1[:, 5:], 0.5, atol=1e-9)
    np.testing.assert_allclose(m1[:, :3], 1.0, atol=1e-9)

    # t=0: the hidden blue of the transparent right half must not bleed
    # into the visible left half, and transparent pixels are RGB = 0.
    assert (f0[:, :, 2] == 0).all()
    assert (f0[:, 2:] == 0).all()
    assert (f0[:, :2, 0] == 255).all()

    # t=1: the right half is now semi-transparent; its true blue color
    # is recovered (premultiplied color was NOT frozen at t=0, where
    # this half was invisible). Only interior columns are checked; the
    # two boundary columns are interpolation blends.
    np.testing.assert_allclose(f1[:, 5:, 2], 255, atol=1)
    np.testing.assert_allclose(f1[:, 5:, 0], 0, atol=1)
    np.testing.assert_allclose(f1[:, :3, 0], 255, atol=1)
    np.testing.assert_allclose(f1[:, :3, 2], 0, atol=1)


def test_imageclip_rgb_with_time_varying_mask_not_frozen():
    """An ImageClip (fixed RGB) with a time-varying uniform mask must
    recompute the premultiplied color at every t."""
    rgb = np.full((2, 2, 3), [255, 0, 0], dtype="uint8")
    clip = ImageClip(rgb, duration=1.0)
    mask_clip = VideoClip(
        frame_function=lambda t: np.full((2, 2), float(t)),
        is_mask=True,
        duration=1.0,
    )
    resized = clip.with_mask(mask_clip).resized((4, 4))

    # At t=0 the frame is fully transparent -> RGB 0.
    assert (resized.get_frame(0.0) == 0).all()
    # At t=1 the mask is fully opaque -> the red must come back.
    frame1 = resized.get_frame(1.0)
    np.testing.assert_allclose(frame1, np.full(frame1.shape, [255, 0, 0]), atol=1)
    np.testing.assert_allclose(resized.mask.get_frame(0.5), 0.5, atol=1e-9)


# ---------------------------------------------------------------------
# apply_to_mask=False
# ---------------------------------------------------------------------


def test_apply_to_mask_false_leaves_mask_untouched():
    rgb = np.arange(4 * 4 * 3, dtype="uint8").reshape(4, 4, 3) * 2
    mask = np.linspace(0.1, 0.9, 16).reshape(4, 4)
    clip = make_clip(rgb, mask)

    resized = clip.with_effects([vfx.Resize(0.5, apply_to_mask=False)])
    assert resized.size == [2, 2] or resized.size == (2, 2)
    # Mask keeps its size, values and time semantics.
    assert resized.mask.size == [4, 4] or resized.mask.size == (4, 4)
    np.testing.assert_array_equal(resized.mask.get_frame(0), mask)
    np.testing.assert_array_equal(resized.mask.get_frame(1.5), mask)
    # RGB went through the classic resize path.
    import cv2

    expected = cv2.resize(rgb, (2, 2), interpolation=cv2.INTER_AREA)
    assert np.array_equal(resized.get_frame(0), expected)


def test_apply_to_mask_false_with_time_function_width():
    """apply_to_mask=False must survive the recursive Resize used for
    width/height given as functions of time."""
    rgb = np.arange(2 * 4 * 3, dtype="uint8").reshape(2, 4, 3) * 3
    mask = np.linspace(0.2, 0.8, 8).reshape(2, 4)
    clip = make_clip(rgb, mask)

    resized = clip.with_effects([vfx.Resize(width=lambda t: 2, apply_to_mask=False)])
    assert tuple(resized.size) == (2, 1)
    # Mask untouched: original size and values.
    assert tuple(resized.mask.size) == (4, 2)
    np.testing.assert_array_equal(resized.mask.get_frame(0.7), mask)


def test_apply_to_mask_false_with_time_function_new_size():
    rgb = np.arange(2 * 2 * 3, dtype="uint8").reshape(2, 2, 3)
    mask = np.full((2, 2), 0.4)
    clip = make_clip(rgb, mask)

    resized = clip.with_effects(
        [vfx.Resize(lambda t: (2, 2) if t < 1 else (4, 4), apply_to_mask=False)]
    )
    assert resized.get_frame(0.5).shape == (2, 2, 3)
    assert resized.get_frame(1.5).shape == (4, 4, 3)
    assert resized.mask.get_frame(1.5).shape == (2, 2)
    np.testing.assert_array_equal(resized.mask.get_frame(1.5), mask)


# ---------------------------------------------------------------------
# Sizes forms, effect reuse, metadata, non-mutation
# ---------------------------------------------------------------------


def test_new_size_factor_and_tuple_and_width_height_forms():
    rgb = np.zeros((4, 4, 3), dtype="uint8")
    rgb[:, 2:] = [0, 0, 255]
    mask = np.ones((4, 4))
    mask[:, 2:] = 0.0
    clip = make_clip(rgb, mask)

    assert tuple(clip.resized(0.5).size) == (2, 2)
    assert tuple(clip.resized((6, 2)).size) == (6, 2)
    assert tuple(clip.resized(width=8).size) == (8, 8)
    assert tuple(clip.resized(height=2).size) == (2, 2)

    # The width=/height= forms also use the premultiplied path.
    out = clip.resized(width=8).get_frame(0)
    assert (out[:, :, 2] == 0).all()  # hidden blue stays hidden


def test_same_effect_object_reused_on_different_sizes():
    """Reusing one Resize instance through with_effects must not carry
    size state from one clip to the next."""
    effect = vfx.Resize(0.5)
    clip8 = ImageClip(np.zeros((8, 8, 3), dtype="uint8"))
    clip4 = ImageClip(np.zeros((4, 4, 3), dtype="uint8"))

    assert tuple(clip8.with_effects([effect]).size) == (4, 4)
    assert tuple(clip4.with_effects([effect]).size) == (2, 2)
    assert tuple(clip8.with_effects([effect]).size) == (4, 4)


def test_metadata_preserved():
    audio = AudioClip(lambda t: [np.sin(440 * 2 * np.pi * t)], duration=2, fps=44100)
    clip = (
        make_clip(np.zeros((4, 4, 3), dtype="uint8"), np.full((4, 4), 0.5))
        .with_fps(12)
        .with_audio(audio)
    )
    resized = clip.resized(0.5)
    assert resized.duration == 2
    assert resized.fps == 12
    assert resized.audio is not None
    assert resized.mask is not None


def test_inputs_and_source_arrays_not_mutated():
    rgb = np.arange(3 * 4 * 3, dtype="uint8").reshape(3, 4, 3)
    mask = np.linspace(0.0, 1.0, 12).reshape(3, 4)
    rgb_before = rgb.copy()
    mask_before = mask.copy()

    clip = make_clip(rgb, mask)
    for new_size in [(8, 6), (2, 1)]:
        resized = clip.resized(new_size)
        resized.get_frame(0)
        resized.mask.get_frame(0)
        resized.get_frame(1.0)

    np.testing.assert_array_equal(rgb, rgb_before)
    np.testing.assert_array_equal(mask, mask_before)
    # The input clip still returns its original frames.
    np.testing.assert_array_equal(clip.get_frame(0), rgb_before)
    np.testing.assert_array_equal(clip.mask.get_frame(0), mask_before)
    assert tuple(clip.size) == (4, 3)


# ---------------------------------------------------------------------
# RGBA PNG round-trip
# ---------------------------------------------------------------------


def test_rgba_result_png_roundtrip(tmp_path):
    """Save an actual resized RGBA result as PNG and verify it reads
    back with the same pixels."""
    rgb = np.array(
        [
            [[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 0]],
            [[0, 255, 255], [255, 0, 255], [128, 64, 32], [10, 20, 30]],
        ],
        dtype="uint8",
    )
    mask = np.array(
        [[1.0, 0.75, 0.5, 0.25], [0.123456, 0.0, 0.9, 1.0]]
    )
    resized = make_clip(rgb, mask).resized((8, 4))

    out = resized.get_frame(0)
    out_mask = resized.mask.get_frame(0)
    assert out.shape == (4, 8, 3) and out_mask.shape == (4, 8)

    rgba = np.dstack([out, np.rint(out_mask * 255).astype("uint8")])
    filename = str(tmp_path / "resized_rgba.png")
    Image.fromarray(rgba, "RGBA").save(filename)

    back = np.array(Image.open(filename))
    assert back.shape == (4, 8, 4)
    # PNG is lossless: exact RGB and alpha round-trip.
    np.testing.assert_array_equal(back[:, :, :3], out)
    np.testing.assert_array_equal(back[:, :, 3], rgba[:, :, 3])
    # Alpha channel matches the float mask within 8-bit rounding.
    np.testing.assert_allclose(back[:, :, 3] / 255.0, out_mask, atol=1 / 255 + 1e-6)
