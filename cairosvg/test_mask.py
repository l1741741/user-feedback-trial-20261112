"""
Tests for SVG masks: luminance/alpha semantics, mask units, region
clipping, opacity handling and definition reuse.

All cases use small canvases and ``shape-rendering="crispEdges"`` so
that pixels can be probed exactly, without anti-aliasing edge effects.

"""

import io
from pathlib import Path

import pytest

import cairocffi as cairo

from . import svg2png
from .parser import Tree
from .surface import PNGSurface

ARTIFACTS_FOLDER = (
    Path(__file__).parent.parent / 'test_non_regression' / 'mask')


def render(svg, **kwargs):
    """Render ``svg`` to an in-memory PNG image surface."""
    png = svg2png(svg.encode(), **kwargs)
    return cairo.ImageSurface.create_from_png(io.BytesIO(png))


def pixel(surface, x, y):
    """Return the unpremultiplied ``(r, g, b, a)`` of a pixel."""
    data = memoryview(surface.get_data())
    i = y * surface.get_stride() + 4 * x
    b, g, r, a = data[i], data[i + 1], data[i + 2], data[i + 3]
    if a:
        r, g, b = r * 255 // a, g * 255 // a, b * 255 // a
    return r, g, b, a


def assert_pixel(surface, x, y, expected, tolerance=2):
    """Assert that a pixel is close to an expected ``(r, g, b, a)``."""
    actual = pixel(surface, x, y)
    for actual_channel, expected_channel in zip(actual, expected):
        assert abs(actual_channel - expected_channel) <= tolerance, (
            f'pixel ({x}, {y}): got {actual}, expected {expected}')


def test_luminance_black_gray_white():
    """Luminance mask: black hides, white shows, gray is halfway."""
    surface = render('''
<svg xmlns="http://www.w3.org/2000/svg" width="30" height="10">
  <mask id="m" maskUnits="userSpaceOnUse" x="0" y="0"
        width="30" height="10">
    <rect x="0" y="0" width="10" height="10" fill="black"/>
    <rect x="10" y="0" width="10" height="10" fill="#808080"/>
    <rect x="20" y="0" width="10" height="10" fill="white"/>
  </mask>
  <rect x="0" y="0" width="30" height="10" fill="blue"/>
  <rect x="0" y="0" width="30" height="10" fill="red" mask="url(#m)"
        shape-rendering="crispEdges"/>
</svg>''')
    # Black mask: only the blue background is visible
    assert_pixel(surface, 5, 5, (0, 0, 255, 255))
    # Gray mask (128/255): red over blue at ~50%
    assert_pixel(surface, 15, 5, (128, 0, 127, 255))
    # White mask: plain red
    assert_pixel(surface, 25, 5, (255, 0, 0, 255))


def test_luminance_multiplied_by_alpha():
    """Luminance mask: content alpha multiplies the RGB luminance."""
    surface = render('''
<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
  <mask id="m" maskUnits="userSpaceOnUse" x="0" y="0"
        width="10" height="10">
    <rect x="0" y="0" width="10" height="10" fill="white"
          fill-opacity="0.5"/>
  </mask>
  <rect x="0" y="0" width="10" height="10" fill="red" mask="url(#m)"
        shape-rendering="crispEdges"/>
</svg>''')
    # Luminance 1 * alpha 0.5: half-transparent red
    assert_pixel(surface, 5, 5, (255, 0, 0, 128))


def test_mask_type_alpha_ignores_rgb():
    """Alpha mask: RGB channels are ignored, only alpha is used.

    Also save the PNG pair showing the difference between the two
    ``mask-type`` values for the same red mask content.

    """
    template = '''
<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
  <mask id="m" mask-type="{mask_type}" maskUnits="userSpaceOnUse"
        x="0" y="0" width="10" height="10">
    <rect x="0" y="0" width="10" height="10" fill="red"/>
  </mask>
  <rect x="0" y="0" width="10" height="10" fill="blue"/>
  <rect x="0" y="0" width="10" height="10" fill="#00ff00"
        mask="url(#m)" shape-rendering="crispEdges"/>
</svg>'''
    ARTIFACTS_FOLDER.mkdir(parents=True, exist_ok=True)

    alpha_png = svg2png(template.format(mask_type='alpha').encode())
    (ARTIFACTS_FOLDER / 'mask-type-alpha.png').write_bytes(alpha_png)
    alpha_surface = cairo.ImageSurface.create_from_png(io.BytesIO(alpha_png))
    # Red is fully opaque: the green rect is fully visible
    assert_pixel(alpha_surface, 5, 5, (0, 255, 0, 255))

    luminance_png = svg2png(template.format(mask_type='luminance').encode())
    (ARTIFACTS_FOLDER / 'mask-type-luminance.png').write_bytes(luminance_png)
    luminance_surface = cairo.ImageSurface.create_from_png(
        io.BytesIO(luminance_png))
    # Red luminance is 0.2126: green at ~21% over blue
    assert_pixel(luminance_surface, 5, 5, (0, 54, 201, 255), tolerance=3)

    # The two rendering modes must produce visibly different results
    assert alpha_png != luminance_png


def test_mask_type_default_is_luminance():
    """Without ``mask-type``, masks use the luminance of the content."""
    surface = render('''
<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
  <mask id="m" maskUnits="userSpaceOnUse" x="0" y="0"
        width="10" height="10">
    <rect x="0" y="0" width="10" height="10" fill="red"/>
  </mask>
  <rect x="0" y="0" width="10" height="10" fill="blue"/>
  <rect x="0" y="0" width="10" height="10" fill="#00ff00"
        mask="url(#m)" shape-rendering="crispEdges"/>
</svg>''')
    assert_pixel(surface, 5, 5, (0, 54, 201, 255), tolerance=3)


def test_mask_units_user_space_on_use():
    """userSpaceOnUse mask region: non-zero position clips the result."""
    surface = render('''
<svg xmlns="http://www.w3.org/2000/svg" width="30" height="30">
  <mask id="m" maskUnits="userSpaceOnUse" x="10" y="10"
        width="10" height="10">
    <rect x="0" y="0" width="30" height="30" fill="white"/>
  </mask>
  <rect x="0" y="0" width="30" height="30" fill="red" mask="url(#m)"
        shape-rendering="crispEdges"/>
</svg>''')
    # The white content covers everything, but only the 10-20 square is
    # inside the mask region
    assert_pixel(surface, 5, 15, (0, 0, 0, 0))
    assert_pixel(surface, 15, 5, (0, 0, 0, 0))
    assert_pixel(surface, 15, 15, (255, 0, 0, 255))
    assert_pixel(surface, 25, 15, (0, 0, 0, 0))
    assert_pixel(surface, 15, 25, (0, 0, 0, 0))


def test_mask_units_object_bounding_box():
    """objectBoundingBox mask region: fractions of the element's bbox."""
    surface = render('''
<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">
  <mask id="m" maskUnits="objectBoundingBox" mask-type="alpha"
        x="0.25" y="0" width="0.5" height="1">
    <rect x="0" y="0" width="40" height="20" fill="white"/>
  </mask>
  <rect x="10" y="5" width="20" height="10" fill="red" mask="url(#m)"
        shape-rendering="crispEdges"/>
</svg>''')
    # Region is the central half of the rect's own bbox: x in 15-25,
    # not the central half of the 40px-wide surface
    assert_pixel(surface, 12, 10, (0, 0, 0, 0))
    assert_pixel(surface, 20, 10, (255, 0, 0, 255))
    assert_pixel(surface, 28, 10, (0, 0, 0, 0))


def test_mask_content_units_object_bounding_box():
    """objectBoundingBox content: coordinates are fractions of the bbox."""
    surface = render('''
<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">
  <mask id="m" maskContentUnits="objectBoundingBox" mask-type="alpha">
    <rect x="0.5" y="0" width="0.5" height="1" fill="white"/>
  </mask>
  <rect x="10" y="5" width="20" height="10" fill="red" mask="url(#m)"
        shape-rendering="crispEdges"/>
</svg>''')
    # The white half starts at the middle of the rect's bbox (x=20),
    # not at the middle of the surface
    assert_pixel(surface, 15, 10, (0, 0, 0, 0))
    assert_pixel(surface, 25, 10, (255, 0, 0, 255))


def test_mask_region_clips_content():
    """Content drawn outside of the mask region must not leak."""
    surface = render('''
<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">
  <mask id="m" maskUnits="userSpaceOnUse" x="5" y="0"
        width="10" height="10">
    <rect x="-20" y="-20" width="60" height="60" fill="white"/>
  </mask>
  <rect x="0" y="0" width="20" height="10" fill="red" mask="url(#m)"
        shape-rendering="crispEdges"/>
</svg>''')
    assert_pixel(surface, 2, 5, (0, 0, 0, 0))
    assert_pixel(surface, 10, 5, (255, 0, 0, 255))
    assert_pixel(surface, 17, 5, (0, 0, 0, 0))


def test_mask_reused_on_different_sizes():
    """A mask referenced twice recomputes its geometry each time."""
    surface = render('''
<svg xmlns="http://www.w3.org/2000/svg" width="40" height="30">
  <mask id="m" maskContentUnits="objectBoundingBox" mask-type="alpha">
    <rect x="0" y="0" width="0.5" height="1" fill="white"/>
  </mask>
  <rect x="0" y="0" width="40" height="10" fill="red" mask="url(#m)"
        shape-rendering="crispEdges"/>
  <rect x="0" y="20" width="20" height="10" fill="red" mask="url(#m)"
        shape-rendering="crispEdges"/>
</svg>''')
    # First rect: left half of its own 40px bbox (20px) is visible
    assert_pixel(surface, 15, 5, (255, 0, 0, 255))
    assert_pixel(surface, 25, 5, (0, 0, 0, 0))
    # Second rect: left half of its own 20px bbox (10px), it must not
    # reuse the 20px half computed for the first rect
    assert_pixel(surface, 5, 25, (255, 0, 0, 255))
    assert_pixel(surface, 15, 25, (0, 0, 0, 0))


def test_mask_opacity_applied_once():
    """The element's opacity multiplies the masked result exactly once."""
    surface = render('''
<svg xmlns="http://www.w3.org/2000/svg" width="10" height="20">
  <mask id="m" maskUnits="userSpaceOnUse" x="0" y="0"
        width="10" height="20">
    <rect x="0" y="0" width="10" height="20" fill="white"/>
  </mask>
  <rect x="0" y="0" width="10" height="10" fill="red" opacity="0.5"
        mask="url(#m)" shape-rendering="crispEdges"/>
  <rect x="0" y="10" width="10" height="10" fill="red" mask="url(#m)"
        shape-rendering="crispEdges"/>
</svg>''')
    # 0.5 opacity applied once, not squared
    assert_pixel(surface, 5, 5, (255, 0, 0, 128))
    # The second reference is not polluted by the first one's opacity
    assert_pixel(surface, 5, 15, (255, 0, 0, 255))


def test_mask_definition_not_mutated():
    """Painting a mask must not modify the stored definition node."""
    svg = '''
<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">
  <mask id="m" maskContentUnits="objectBoundingBox">
    <rect x="0" y="0" width="0.5" height="1" fill="white"/>
  </mask>
  <rect x="0" y="0" width="20" height="10" fill="red" opacity="0.5"
        mask="url(#m)" shape-rendering="crispEdges"/>
</svg>'''
    tree = Tree(bytestring=svg.encode())
    surface = PNGSurface(tree, None, 96)
    mask_node = surface.masks['m']
    # The definition keeps its original tag and gains no computed
    # region, viewBox or opacity from the rendering
    assert mask_node.tag == 'mask'
    assert 'opacity' not in mask_node
    assert 'viewBox' not in mask_node
    assert 'x' not in mask_node
    assert 'y' not in mask_node
    assert 'width' not in mask_node
    assert 'height' not in mask_node


def test_mask_zero_size_bounding_box():
    """A masked element with an empty bbox renders nothing, no crash."""
    surface = render('''
<svg xmlns="http://www.w3.org/2000/svg" width="10" height="30">
  <mask id="m">
    <rect x="0" y="0" width="1" height="1" fill="white"/>
  </mask>
  <line x1="5" y1="5" x2="5" y2="25" stroke="red" mask="url(#m)"/>
  <rect x="0" y="0" width="10" height="10" fill="blue"/>
</svg>''')
    # The vertical line has a zero-width bounding box: its masked
    # rendering is empty, the rest of the document is unaffected
    assert_pixel(surface, 5, 20, (0, 0, 0, 0))
    assert_pixel(surface, 5, 5, (0, 0, 255, 255))


def test_mask_rendering_is_deterministic():
    """Rendering the same masked document twice gives identical bytes."""
    svg = '''
<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">
  <mask id="m" maskContentUnits="objectBoundingBox">
    <rect x="0" y="0" width="0.5" height="1" fill="#808080"/>
  </mask>
  <rect x="0" y="0" width="20" height="10" fill="red" mask="url(#m)"
        shape-rendering="crispEdges"/>
</svg>'''.encode()
    assert svg2png(svg) == svg2png(svg)


def test_mask_saved_artifacts_differ():
    """The saved PNG pair actually shows the luminance/alpha difference."""
    alpha_png = ARTIFACTS_FOLDER / 'mask-type-alpha.png'
    luminance_png = ARTIFACTS_FOLDER / 'mask-type-luminance.png'
    if not (alpha_png.exists() and luminance_png.exists()):
        pytest.skip('artifacts are generated by test_mask_type_alpha')
    alpha_surface = cairo.ImageSurface.create_from_png(str(alpha_png))
    luminance_surface = cairo.ImageSurface.create_from_png(
        str(luminance_png))
    assert pixel(alpha_surface, 5, 5) != pixel(luminance_surface, 5, 5)
