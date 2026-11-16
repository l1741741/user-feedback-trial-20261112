"""
Pixel-level tests for SVG mask rendering.

These tests check the luminance/alpha semantics of masks, the mask region
and coordinate spaces, mask reuse, and the interaction between element
opacity and mask opacity. See MASKS.md for the supported feature set.

"""

import io

import cairocffi as cairo
import pytest

from . import svg2pdf, svg2png

HEAD = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
TAIL = '</svg>'


def render(svg):
    """Render ``svg`` to an in-memory PNG image surface."""
    png = svg2png(bytestring=svg.encode())
    return cairo.ImageSurface.create_from_png(io.BytesIO(png))


def pixel(image, x, y):
    """Return the unpremultiplied (r, g, b, a) of pixel ``x, y``."""
    data = image.get_data()
    offset = y * image.get_stride() + x * 4
    blue, green, red, alpha = (int(value) for value in data[offset:offset+4])
    return red, green, blue, alpha


def assert_pixel(image, x, y, expected, tolerance=8):
    """Assert that pixel ``x, y`` is close to ``expected``."""
    actual = pixel(image, x, y)
    assert all(
        abs(a - e) <= tolerance for a, e in zip(actual, expected)), (
        f'pixel ({x}, {y}): got {actual}, expected {expected}')


def test_mask_luminance_levels():
    """Black, gray, white and semi-transparent mask colors."""
    image = render(HEAD + '''
      <defs>
        <mask id="m" maskUnits="userSpaceOnUse"
              x="0" y="0" width="100" height="100">
          <rect x="0" y="0" width="25" height="100" fill="black"/>
          <rect x="25" y="0" width="25" height="100" fill="rgb(128,128,128)"/>
          <rect x="50" y="0" width="25" height="100" fill="white"/>
          <rect x="75" y="0" width="25" height="100"
                fill="white" fill-opacity="0.5"/>
        </mask>
      </defs>
      <rect width="100" height="100" fill="red" mask="url(#m)"/>
    ''' + TAIL)
    assert_pixel(image, 12, 50, (0, 0, 0, 0))
    assert_pixel(image, 37, 50, (128, 0, 0, 128))
    assert_pixel(image, 62, 50, (255, 0, 0, 255))
    assert_pixel(image, 87, 50, (128, 0, 0, 128))


def test_mask_luminance_of_colors():
    """Colored mask content uses Rec. 709 luma coefficients."""
    image = render(HEAD + '''
      <defs>
        <mask id="m" maskUnits="userSpaceOnUse"
              x="0" y="0" width="100" height="100">
          <rect x="0" y="0" width="50" height="100" fill="rgb(0,255,0)"/>
          <rect x="50" y="0" width="50" height="100" fill="rgb(255,0,0)"/>
        </mask>
      </defs>
      <rect width="100" height="100" fill="white" mask="url(#m)"/>
    ''' + TAIL)
    # 0.7152 * 255 ≈ 182, 0.2126 * 255 ≈ 54
    assert_pixel(image, 25, 50, (182, 182, 182, 182))
    assert_pixel(image, 75, 50, (54, 54, 54, 54))


def test_mask_type_alpha():
    """With mask-type: alpha, only the alpha channel masks."""
    image = render(HEAD + '''
      <defs>
        <mask id="m" mask-type="alpha" maskUnits="userSpaceOnUse"
              x="0" y="0" width="100" height="100">
          <rect x="0" y="0" width="50" height="100" fill="black"/>
          <rect x="50" y="0" width="50" height="100"
                fill="red" fill-opacity="0.5"/>
        </mask>
      </defs>
      <rect width="100" height="100" fill="blue" mask="url(#m)"/>
    ''' + TAIL)
    # Black with full alpha masks fully with mask-type: alpha
    assert_pixel(image, 25, 50, (0, 0, 255, 255))
    assert_pixel(image, 75, 50, (0, 0, 128, 128))


def test_mask_type_alpha_from_style():
    """mask-type can be set through CSS."""
    image = render(HEAD + '''
      <defs>
        <mask id="m" style="mask-type:alpha" maskUnits="userSpaceOnUse"
              x="0" y="0" width="100" height="100">
          <rect width="100" height="100" fill="black"/>
        </mask>
      </defs>
      <rect width="100" height="100" fill="blue" mask="url(#m)"/>
    ''' + TAIL)
    assert_pixel(image, 50, 50, (0, 0, 255, 255))


def test_mask_gradient_edge():
    """A black-to-white gradient mask fades smoothly."""
    image = render('''
      <svg xmlns="http://www.w3.org/2000/svg" width="100" height="20">
        <defs>
          <linearGradient id="g" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0" stop-color="black"/>
            <stop offset="1" stop-color="white"/>
          </linearGradient>
          <mask id="m" maskUnits="userSpaceOnUse"
                x="0" y="0" width="100" height="20">
            <rect width="100" height="20" fill="url(#g)"/>
          </mask>
        </defs>
        <rect width="100" height="20" fill="red" mask="url(#m)"/>
      </svg>
    ''')
    alphas = [pixel(image, x, 10)[3] for x in (0, 25, 50, 75, 99)]
    assert alphas == sorted(alphas)
    assert alphas[0] <= 4
    assert 56 <= alphas[1] <= 74
    assert 120 <= alphas[2] <= 138
    assert 184 <= alphas[3] <= 202
    assert alphas[4] >= 251


def test_mask_region_limits_visibility():
    """Content outside the mask region is invisible."""
    image = render(HEAD + '''
      <defs>
        <mask id="m" maskUnits="userSpaceOnUse"
              x="20" y="20" width="30" height="30">
          <rect width="100" height="100" fill="white"/>
        </mask>
      </defs>
      <rect width="100" height="100" fill="red" mask="url(#m)"/>
    ''' + TAIL)
    assert_pixel(image, 30, 30, (255, 0, 0, 255))
    assert_pixel(image, 10, 30, (0, 0, 0, 0))
    assert_pixel(image, 60, 30, (0, 0, 0, 0))
    assert_pixel(image, 30, 60, (0, 0, 0, 0))


def test_mask_reuse_object_bounding_box():
    """One mask reused by objects of different sizes and positions."""
    image = render(HEAD + '''
      <defs>
        <mask id="m" maskContentUnits="objectBoundingBox">
          <rect x="0" y="0" width="0.5" height="1" fill="white"/>
        </mask>
      </defs>
      <rect x="10" y="10" width="40" height="20" fill="lime" mask="url(#m)"/>
      <rect x="50" y="60" width="20" height="30" fill="lime" mask="url(#m)"/>
    ''' + TAIL)
    # Each object gets its own half-masked copy of the mask
    assert_pixel(image, 15, 20, (0, 255, 0, 255))
    assert_pixel(image, 35, 20, (0, 0, 0, 0))
    assert_pixel(image, 55, 70, (0, 255, 0, 255))
    assert_pixel(image, 65, 70, (0, 0, 0, 0))


def test_mask_on_non_square_object():
    """objectBoundingBox units follow the geometry of an ellipse."""
    image = render(HEAD + '''
      <defs>
        <mask id="m" maskContentUnits="objectBoundingBox">
          <rect x="0" y="0" width="0.5" height="1" fill="white"/>
        </mask>
      </defs>
      <ellipse cx="50" cy="50" rx="40" ry="20" fill="red" mask="url(#m)"/>
    ''' + TAIL)
    assert_pixel(image, 30, 50, (255, 0, 0, 255))
    assert_pixel(image, 70, 50, (0, 0, 0, 0))


def test_mask_with_transformed_element():
    """userSpaceOnUse masks live in the element's user space."""
    image = render(HEAD + '''
      <defs>
        <mask id="m" maskUnits="userSpaceOnUse"
              x="0" y="0" width="100" height="100">
          <rect x="0" y="0" width="50" height="100" fill="white"/>
        </mask>
      </defs>
      <g transform="translate(20, 10)">
        <rect x="0" y="0" width="60" height="40" fill="red" mask="url(#m)"/>
      </g>
    ''' + TAIL)
    # Mask covers user-space x in [0, 50], i.e. device x in [20, 70]
    assert_pixel(image, 30, 20, (255, 0, 0, 255))
    assert_pixel(image, 75, 20, (0, 0, 0, 0))


def test_mask_nested_groups():
    """Group opacity inside mask content contributes to the mask value."""
    image = render(HEAD + '''
      <defs>
        <mask id="m" maskUnits="userSpaceOnUse"
              x="0" y="0" width="100" height="100">
          <g opacity="0.5">
            <rect x="0" y="0" width="50" height="100" fill="white"/>
          </g>
          <g><g opacity="0.5">
            <rect x="50" y="0" width="50" height="100" fill="white"/>
          </g></g>
        </mask>
      </defs>
      <rect width="100" height="100" fill="red" mask="url(#m)"/>
    ''' + TAIL)
    assert_pixel(image, 25, 50, (128, 0, 0, 128))
    assert_pixel(image, 75, 50, (128, 0, 0, 128))


def test_mask_opacity_applied_once():
    """Element opacity and mask opacity are each applied once."""
    # White mask, element opacity 0.5: result is 0.5, not 0.25
    image = render(HEAD + '''
      <defs>
        <mask id="m" maskUnits="userSpaceOnUse"
              x="0" y="0" width="100" height="100">
          <rect width="100" height="100" fill="white"/>
        </mask>
      </defs>
      <rect width="100" height="100" fill="red" opacity="0.5" mask="url(#m)"/>
    ''' + TAIL)
    assert_pixel(image, 50, 50, (128, 0, 0, 128))

    # Same through a group
    image = render(HEAD + '''
      <defs>
        <mask id="m" maskUnits="userSpaceOnUse"
              x="0" y="0" width="100" height="100">
          <rect width="100" height="100" fill="white"/>
        </mask>
      </defs>
      <g opacity="0.5">
        <rect width="100" height="100" fill="red" mask="url(#m)"/>
      </g>
    ''' + TAIL)
    assert_pixel(image, 50, 50, (128, 0, 0, 128))

    # 50% gray mask combined with element opacity 0.5 gives 0.25
    image = render(HEAD + '''
      <defs>
        <mask id="m" maskUnits="userSpaceOnUse"
              x="0" y="0" width="100" height="100">
          <rect width="100" height="100" fill="rgb(128,128,128)"/>
        </mask>
      </defs>
      <rect width="100" height="100" fill="red" opacity="0.5" mask="url(#m)"/>
    ''' + TAIL)
    assert_pixel(image, 50, 50, (64, 0, 0, 64), tolerance=10)


def test_mask_transparent_background_no_bleed():
    """Fully masked areas stay fully transparent, colors don't bleed."""
    image = render(HEAD + '''
      <defs>
        <mask id="m" maskUnits="userSpaceOnUse"
              x="0" y="0" width="100" height="100">
          <rect width="100" height="100" fill="black"/>
        </mask>
      </defs>
      <rect width="100" height="100" fill="red" mask="url(#m)"/>
    ''' + TAIL)
    assert_pixel(image, 50, 50, (0, 0, 0, 0), tolerance=0)


def test_mask_repeated_export_is_deterministic():
    """Exporting the same masked document twice gives identical bytes."""
    svg = (HEAD + '''
      <defs>
        <mask id="m" maskContentUnits="objectBoundingBox">
          <rect x="0" y="0" width="0.5" height="1" fill="white"/>
        </mask>
      </defs>
      <rect x="10" y="10" width="40" height="20" fill="lime" mask="url(#m)"/>
      <rect x="50" y="60" width="20" height="30" fill="lime" mask="url(#m)"/>
    ''' + TAIL).encode()
    assert svg2png(bytestring=svg) == svg2png(bytestring=svg)


def test_mask_in_pdf_export():
    """Masked documents can still be exported to PDF."""
    svg = (HEAD + '''
      <defs>
        <mask id="m" maskUnits="userSpaceOnUse"
              x="0" y="0" width="100" height="100">
          <rect width="100" height="100" fill="rgb(128,128,128)"/>
        </mask>
      </defs>
      <rect width="100" height="100" fill="red" mask="url(#m)"/>
    ''' + TAIL).encode()
    assert svg2pdf(bytestring=svg).startswith(b'%PDF')
