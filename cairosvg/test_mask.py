"""
Pixel-level tests for SVG ``<mask>`` luminance and alpha semantics.

These tests render small offline SVGs built from solid colors, grays and
semi-transparent blocks, then assert exact pixels (with a small tolerance
for antialiasing and 8-bit rounding). They guard the distinction between
luminance masks and alpha masks, the coordinate space of reused masks, and
the single application of group/mask opacity.

"""

import io

import pytest

from . import svg2png

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

requires_pillow = pytest.mark.skipif(
    Image is None, reason='Pillow is required for pixel tests')


def render(svg):
    """Render an SVG byte string to an RGBA PIL image."""
    png = svg2png(bytestring=svg if isinstance(svg, bytes) else svg.encode())
    return Image.open(io.BytesIO(png)).convert('RGBA')


def assert_pixel(image, x, y, expected, tolerance=2):
    """Assert one RGBA pixel, channel by channel, within ``tolerance``."""
    actual = image.getpixel((x, y))
    for channel, (got, want) in enumerate(zip(actual, expected)):
        assert abs(got - want) <= tolerance, (
            f'pixel ({x},{y}) channel {channel}: got {got}, want {want} '
            f'(full RGBA {actual}, expected {expected})')


def svg(body, width=300, height=100):
    return f'''<svg xmlns="http://www.w3.org/2000/svg"
        width="{width}" height="{height}" viewBox="0 0 {width} {height}">
        {body}</svg>'''


@requires_pillow
def test_luminance_black_gray_white():
    """Black hides, gray partially shows, white fully shows."""
    image = render(svg('''
      <defs><mask id="m" maskUnits="userSpaceOnUse"
          x="0" y="0" width="300" height="100">
        <rect x="0" y="0" width="100" height="100" fill="black"/>
        <rect x="100" y="0" width="100" height="100" fill="rgb(128,128,128)"/>
        <rect x="200" y="0" width="100" height="100" fill="white"/>
      </mask></defs>
      <rect width="300" height="100" fill="red" mask="url(#m)"/>'''))
    assert_pixel(image, 25, 50, (0, 0, 0, 0))
    # sRGB luma of gray 128/255 ~= 0.502 -> alpha round(0.502*255) = 128
    assert_pixel(image, 150, 50, (255, 0, 0, 128))
    assert_pixel(image, 250, 50, (255, 0, 0, 255))


@requires_pillow
def test_color_luminance_coefficients():
    """Primary colors contribute through their Rec.709 luminance."""
    image = render(svg('''
      <defs><mask id="m" maskUnits="userSpaceOnUse"
          x="0" y="0" width="300" height="100">
        <rect x="0" y="0" width="100" height="100" fill="red"/>
        <rect x="100" y="0" width="100" height="100" fill="lime"/>
        <rect x="200" y="0" width="100" height="100" fill="blue"/>
      </mask></defs>
      <rect width="300" height="100" fill="white" mask="url(#m)"/>'''))
    # red 0.2126*255 ~= 54, green 0.7152*255 ~= 182, blue 0.0722*255 ~= 18
    assert_pixel(image, 50, 50, (255, 255, 255, 54))
    assert_pixel(image, 150, 50, (255, 255, 255, 182))
    assert_pixel(image, 250, 50, (255, 255, 255, 18))


@requires_pillow
def test_mask_alpha_multiplies_luminance():
    """Mask content opacity scales the luminance coverage."""
    image = render(svg('''
      <defs><mask id="m" maskUnits="userSpaceOnUse"
          x="0" y="0" width="300" height="100">
        <rect x="0" y="0" width="100" height="100"
              fill="white" opacity="0.25"/>
        <rect x="100" y="0" width="100" height="100"
              fill="white" opacity="0.5"/>
        <rect x="200" y="0" width="100" height="100"
              fill="white" opacity="0.75"/>
      </mask></defs>
      <rect width="300" height="100" fill="red" mask="url(#m)"/>'''))
    assert_pixel(image, 50, 50, (255, 0, 0, 64))
    assert_pixel(image, 150, 50, (255, 0, 0, 128))
    assert_pixel(image, 250, 50, (255, 0, 0, 191))


@requires_pillow
def test_alpha_mask_ignores_colors():
    """An alpha mask uses only alpha; black opaque still fully shows."""
    image = render(svg('''
      <defs><mask id="a" mask-type="alpha" maskUnits="userSpaceOnUse"
          x="0" y="0" width="300" height="100">
        <rect x="0" y="0" width="150" height="100" fill="black"/>
        <rect x="150" y="0" width="150" height="100"
              fill="white" opacity="0.5"/>
      </mask></defs>
      <rect width="300" height="100" fill="red" mask="url(#a)"/>'''))
    # Opaque black in an alpha mask is fully opaque.
    assert_pixel(image, 75, 50, (255, 0, 0, 255))
    assert_pixel(image, 225, 50, (255, 0, 0, 128))


@requires_pillow
def test_luminance_and_alpha_masks_differ():
    """The two mask types must not be treated as identical."""
    image = render(svg('''
      <defs>
        <mask id="a" mask-type="alpha" maskUnits="userSpaceOnUse"
            x="0" y="0" width="300" height="50">
          <rect width="300" height="50" fill="black"/>
        </mask>
        <mask id="l" mask-type="luminance" maskUnits="userSpaceOnUse"
            x="0" y="0" width="300" height="50">
          <rect width="300" height="50" fill="black"/>
        </mask>
      </defs>
      <rect y="0" width="300" height="50" fill="red" mask="url(#a)"/>
      <rect y="50" width="300" height="50" fill="red" mask="url(#l)"/>''',
        height=100))
    assert_pixel(image, 10, 25, (255, 0, 0, 255))  # alpha: visible
    assert_pixel(image, 10, 75, (0, 0, 0, 0))      # luminance: hidden


@requires_pillow
def test_outside_mask_region_is_transparent():
    """No color bleeds through transparent pixels outside the region."""
    image = render(svg('''
      <defs><mask id="m">
        <rect x="50" y="50" width="100" height="100" fill="white"/>
      </mask></defs>
      <rect width="200" height="200" fill="red" mask="url(#m)"/>''',
        width=200, height=200))
    assert_pixel(image, 10, 10, (0, 0, 0, 0))
    assert_pixel(image, 190, 190, (0, 0, 0, 0))
    assert_pixel(image, 100, 100, (255, 0, 0, 255))


@requires_pillow
def test_group_opacity_applied_once():
    """Group opacity 0.5 over mask alpha 0.5 yields 0.25, not 0.125."""
    image = render(svg('''
      <defs><mask id="m" maskUnits="userSpaceOnUse"
          x="0" y="0" width="200" height="100">
        <rect width="200" height="100" fill="white" opacity="0.5"/>
      </mask></defs>
      <g opacity="0.5" mask="url(#m)">
        <rect width="200" height="100" fill="red"/>
      </g>''', width=200))
    assert_pixel(image, 100, 50, (255, 0, 0, 64))


@requires_pillow
def test_leaf_opacity_and_mask_applied_once():
    """A masked leaf's own opacity is not folded into the mask twice."""
    image = render(svg('''
      <defs><mask id="m" maskUnits="userSpaceOnUse"
          x="0" y="0" width="200" height="100">
        <rect width="200" height="100" fill="white" opacity="0.5"/>
      </mask></defs>
      <rect x="0" width="100" height="100" fill="red"
            opacity="0.5" mask="url(#m)"/>
      <rect x="100" width="100" height="100" fill="red" mask="url(#m)"/>''',
        width=200))
    assert_pixel(image, 50, 50, (255, 0, 0, 64))    # 0.5 * 0.5
    assert_pixel(image, 150, 50, (255, 0, 0, 128))  # mask only


@requires_pillow
def test_userspace_mask_reuse_is_stable():
    """Reusing a mask must not mutate or break the shared definition."""
    body = '''
      <defs><mask id="m" maskUnits="userSpaceOnUse"
          x="0" y="0" width="300" height="120">
        <rect x="10" y="10" width="80" height="100" fill="white"/>
      </mask></defs>
      <rect x="0" y="0" width="150" height="120" fill="red" mask="url(#m)"/>
      <rect x="150" y="0" width="150" height="120" fill="blue"
            mask="url(#m)"/>'''
    first = render(svg(body, width=300, height=120))
    second = render(svg(body, width=300, height=120))
    # First object is masked inside the fixed user-space region.
    assert_pixel(first, 50, 60, (255, 0, 0, 255))
    # Repeated exports are byte-identical (no leaked node mutation).
    assert first.tobytes() == second.tobytes()


@requires_pillow
def test_object_bounding_box_reuse_per_object():
    """An objectBoundingBox mask adapts to each object's box independently."""
    image = render(svg('''
      <defs><mask id="m" maskContentUnits="objectBoundingBox">
        <rect x="0.25" y="0.25" width="0.5" height="0.5" fill="white"/>
      </mask></defs>
      <rect x="0" y="0" width="200" height="200" fill="red" mask="url(#m)"/>
      <rect x="220" y="50" width="160" height="100" fill="blue"
            mask="url(#m)"/>''', width=400, height=200))
    # First box: visible only within 50..150.
    assert_pixel(image, 25, 100, (0, 0, 0, 0))
    assert_pixel(image, 100, 100, (255, 0, 0, 255))
    assert_pixel(image, 175, 100, (0, 0, 0, 0))
    # Second box (220,50 160x100): visible within 260..340 / 75..125.
    assert_pixel(image, 240, 100, (0, 0, 0, 0))
    assert_pixel(image, 300, 100, (0, 0, 255, 255))
    assert_pixel(image, 350, 100, (0, 0, 0, 0))
    assert_pixel(image, 300, 60, (0, 0, 0, 0))
    assert_pixel(image, 300, 90, (0, 0, 255, 255))


@requires_pillow
def test_gradient_mask_edge_is_smooth():
    """A luminance gradient produces a matching linear alpha ramp."""
    image = render(svg('''
      <defs>
        <linearGradient id="g" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="black"/>
          <stop offset="1" stop-color="white"/>
        </linearGradient>
        <mask id="m" maskUnits="userSpaceOnUse"
            x="0" y="0" width="200" height="100">
          <rect width="200" height="100" fill="url(#g)"/>
        </mask>
      </defs>
      <rect width="200" height="100" fill="red" mask="url(#m)"/>''',
        width=200))
    alphas = [image.getpixel((x, 50))[3] for x in (10, 50, 100, 150, 190)]
    # Strictly increasing and roughly linear with the gradient position.
    assert alphas == sorted(alphas)
    assert alphas[0] < 20
    assert 110 <= alphas[2] <= 145
    assert alphas[-1] > 235


@requires_pillow
def test_repeated_export_is_deterministic():
    """Rendering the same masked SVG twice gives identical bytes."""
    source = svg('''
      <defs><mask id="m" maskUnits="userSpaceOnUse"
          x="0" y="0" width="300" height="100">
        <rect width="300" height="100" fill="white" opacity="0.4"/>
      </mask></defs>
      <rect width="300" height="100" fill="red" mask="url(#m)"/>''')
    assert svg2png(bytestring=source.encode()) == svg2png(
        bytestring=source.encode())
