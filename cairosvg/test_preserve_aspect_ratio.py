"""
Pixel-level tests for preserveAspectRatio handling.

Root and nested viewBoxes are rendered with meet, slice and none into
landscape and portrait containers. Test input carries four colored corner
marks (red/green/blue/yellow) and a black center cross so that alignment,
clipping and non-zero viewBox origins can be checked on actual pixels.

Run this file as a script to regenerate the sample PNGs in
``par-renderings/`` at the repository root.

"""

import base64
import io
import os

import pytest
from PIL import Image

from . import svg2png

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
YELLOW = (255, 255, 0)
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
MAGENTA = (255, 0, 255)

TOLERANCE = 40


def corners_body(x, y, width, height, size=20, stroke=4):
    """Four colored corner marks and a center cross in viewBox coords."""
    cx = x + width / 2
    cy = y + height / 2
    return f'''
  <rect x="{x}" y="{y}" width="{size}" height="{size}" fill="#ff0000"/>
  <rect x="{x + width - size}" y="{y}" width="{size}" height="{size}"
        fill="#00ff00"/>
  <rect x="{x}" y="{y + height - size}" width="{size}" height="{size}"
        fill="#0000ff"/>
  <rect x="{x + width - size}" y="{y + height - size}" width="{size}"
        height="{size}" fill="#ffff00"/>
  <line x1="{cx}" y1="{y}" x2="{cx}" y2="{y + height}"
        stroke="#000000" stroke-width="{stroke}"/>
  <line x1="{x}" y1="{cy}" x2="{x + width}" y2="{cy}"
        stroke="#000000" stroke-width="{stroke}"/>
'''


def root_svg(width, height, viewbox=(0, 0, 100, 100), par=None, body=None):
    par_attr = f' preserveAspectRatio="{par}"' if par else ''
    vb = ' '.join(str(v) for v in viewbox)
    if body is None:
        body = corners_body(*viewbox)
    return f'''<svg xmlns="http://www.w3.org/2000/svg"
 width="{width}" height="{height}" viewBox="{vb}"{par_attr}>{body}
</svg>'''


def image_doc(inner_svg, x, y, width, height, par, root=(200, 100),
              background=None):
    encoded = base64.b64encode(inner_svg.encode()).decode()
    bg = f'<rect width="{root[0]}" height="{root[1]}" fill="{background}"/>' \
        if background else ''
    return f'''<svg xmlns="http://www.w3.org/2000/svg"
 width="{root[0]}" height="{root[1]}">
  {bg}
  <image x="{x}" y="{y}" width="{width}" height="{height}"
         preserveAspectRatio="{par}"
         href="data:image/svg+xml;base64,{encoded}"/>
</svg>'''


def render(svg, **kwargs):
    kwargs.setdefault('background_color', 'white')
    png = svg2png(bytestring=svg.encode(), **kwargs)
    return Image.open(io.BytesIO(png)).convert('RGB')


def assert_pixel(image, x, y, expected):
    pixel = image.getpixel((x, y))
    assert all(abs(pixel[i] - expected[i]) <= TOLERANCE for i in range(3)), \
        f'pixel ({x}, {y}) is {pixel}, expected {expected}'


# Root viewBox, landscape and portrait containers

def test_root_meet_landscape():
    """meet in a 200x100 container: centered, blank bands left/right."""
    image = render(root_svg(200, 100))
    assert image.size == (200, 100)
    assert_pixel(image, 55, 5, RED)
    assert_pixel(image, 145, 5, GREEN)
    assert_pixel(image, 55, 95, BLUE)
    assert_pixel(image, 145, 95, YELLOW)
    assert_pixel(image, 100, 50, BLACK)  # center cross at container center
    assert_pixel(image, 25, 50, WHITE)  # meet band stays empty
    assert_pixel(image, 175, 50, WHITE)


def test_root_meet_portrait():
    """meet in a 100x200 container: centered, blank bands top/bottom."""
    image = render(root_svg(100, 200))
    assert_pixel(image, 5, 55, RED)
    assert_pixel(image, 95, 55, GREEN)
    assert_pixel(image, 5, 145, BLUE)
    assert_pixel(image, 95, 145, YELLOW)
    assert_pixel(image, 50, 100, BLACK)
    assert_pixel(image, 50, 25, WHITE)
    assert_pixel(image, 50, 175, WHITE)


def test_root_slice_landscape():
    """slice in a 200x100 container: corners cropped, center preserved."""
    image = render(root_svg(200, 100, par='xMidYMid slice'))
    assert_pixel(image, 5, 5, WHITE)  # top-left corner mark cropped away
    assert_pixel(image, 195, 5, WHITE)
    assert_pixel(image, 5, 95, WHITE)
    assert_pixel(image, 195, 95, WHITE)
    assert_pixel(image, 100, 50, BLACK)


def test_root_slice_portrait():
    image = render(root_svg(100, 200, par='xMidYMid slice'))
    assert_pixel(image, 5, 5, WHITE)
    assert_pixel(image, 95, 195, WHITE)
    assert_pixel(image, 50, 100, BLACK)


def test_root_none_stretches():
    """none: independent x/y scaling, corner marks reach the corners."""
    image = render(root_svg(200, 100, par='none'))
    assert_pixel(image, 10, 5, RED)
    assert_pixel(image, 190, 5, GREEN)
    assert_pixel(image, 10, 95, BLUE)
    assert_pixel(image, 190, 95, YELLOW)
    assert_pixel(image, 100, 50, BLACK)


def test_root_alignment_variants():
    image = render(root_svg(200, 100, par='xMinYMin meet'))
    assert_pixel(image, 5, 5, RED)
    assert_pixel(image, 95, 95, YELLOW)
    assert_pixel(image, 150, 50, WHITE)
    image = render(root_svg(200, 100, par='xMaxYMax meet'))
    assert_pixel(image, 105, 5, RED)
    assert_pixel(image, 195, 95, YELLOW)
    assert_pixel(image, 50, 50, WHITE)


def test_root_nonzero_viewbox_origin():
    """viewBox="50 50 100 100" must map like "0 0 100 100"."""
    for par, probes in (
            ('xMidYMid meet', ((55, 5, RED), (145, 95, YELLOW),
                               (25, 50, WHITE))),
            ('none', ((10, 5, RED), (190, 95, YELLOW))),
            ('xMidYMid slice', ((5, 5, WHITE), (100, 50, BLACK)))):
        image = render(root_svg(200, 100, viewbox=(50, 50, 100, 100),
                                par=par))
        for x, y, color in probes:
            assert_pixel(image, x, y, color)
        assert_pixel(image, 100, 50, BLACK)


# Output scaling parameters

def test_output_size_parameters():
    """output_width/output_height participate in the viewBox mapping."""
    image = render(root_svg(100, 100), output_width=200, output_height=100)
    assert image.size == (200, 100)
    assert_pixel(image, 55, 5, RED)
    assert_pixel(image, 145, 95, YELLOW)
    assert_pixel(image, 100, 50, BLACK)
    assert_pixel(image, 25, 50, WHITE)


def test_scale_parameter():
    image = render(root_svg(200, 100), scale=2)
    assert image.size == (400, 200)
    assert_pixel(image, 110, 10, RED)
    assert_pixel(image, 290, 190, YELLOW)
    assert_pixel(image, 200, 100, BLACK)
    assert_pixel(image, 50, 100, WHITE)


# Nested <svg> viewports

def nested_doc(par, x=50, y=25, width=100, height=50,
               viewbox=(0, 0, 100, 100), overflow=None, background=True):
    par_attr = f' preserveAspectRatio="{par}"' if par else ''
    overflow_attr = f' overflow="{overflow}"' if overflow else ''
    vb = ' '.join(str(v) for v in viewbox)
    bg = '<rect width="200" height="100" fill="#ff00ff"/>' \
        if background else ''
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
  {bg}
  <svg x="{x}" y="{y}" width="{width}" height="{height}" viewBox="{vb}"
       {par_attr}{overflow_attr}>{corners_body(*viewbox)}
  </svg>
</svg>'''


def test_nested_meet_with_offset():
    image = render(nested_doc('xMidYMid meet'))
    # scale 0.5, content centered in the 100x50 viewport at (50, 25)
    assert_pixel(image, 78, 28, RED)
    assert_pixel(image, 122, 72, YELLOW)
    assert_pixel(image, 100, 50, BLACK)
    assert_pixel(image, 55, 50, MAGENTA)  # meet band shows background


def test_nested_none_with_offset():
    image = render(nested_doc('none'))
    assert_pixel(image, 55, 28, RED)
    assert_pixel(image, 145, 72, YELLOW)
    assert_pixel(image, 100, 50, BLACK)


def test_nested_slice_clips_to_viewport():
    """slice overflow must not paint over sibling layers."""
    image = render(nested_doc('xMidYMid slice'))
    # Content would spill to y 0..100; viewport is y 25..75.
    assert_pixel(image, 100, 10, MAGENTA)  # above viewport: untouched
    assert_pixel(image, 100, 90, MAGENTA)  # below viewport: untouched
    assert_pixel(image, 10, 50, MAGENTA)  # outside viewport: untouched
    assert_pixel(image, 100, 50, BLACK)


def test_nested_slice_overflow_visible():
    """overflow="visible" lets slice content spill out of the viewport."""
    image = render(nested_doc('xMidYMid slice', overflow='visible'))
    assert_pixel(image, 55, 10, RED)  # top-left corner mark spills out
    assert_pixel(image, 145, 90, YELLOW)


def test_nested_nonzero_viewbox_origin():
    image = render(nested_doc('xMidYMid meet', viewbox=(50, 50, 100, 100)))
    assert_pixel(image, 78, 28, RED)
    assert_pixel(image, 122, 72, YELLOW)
    assert_pixel(image, 100, 50, BLACK)


def test_nested_degenerate_viewport_skipped():
    """A nested svg with zero width is silently skipped, state finalized."""
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <svg x="10" y="10" width="0" height="50" viewBox="0 0 100 100">
    <rect width="100" height="100" fill="#ff0000"/>
  </svg>
  <rect x="40" y="40" width="20" height="20" fill="#00ff00"/>
</svg>'''
    image = render(svg)
    assert_pixel(image, 50, 50, GREEN)  # drawing continues normally
    assert_pixel(image, 15, 15, WHITE)


# <use> of a <symbol> (nested viewBox through instantiation)

def test_use_symbol_preserve_aspect_ratio():
    for par, probe in (('xMidYMid meet', (55, 5, RED)),
                       ('none', (10, 5, RED)),
                       ('xMidYMid slice', (5, 5, WHITE))):
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg"
     width="200" height="100">
  <defs>
    <symbol id="icon" viewBox="0 0 100 100" preserveAspectRatio="{par}">
      {corners_body(0, 0, 100, 100)}
    </symbol>
  </defs>
  <use href="#icon" width="200" height="100"/>
</svg>'''
        image = render(svg)
        assert_pixel(image, *probe)
        assert_pixel(image, 100, 50, BLACK)


# <image> referencing an SVG document (nested viewBox in another document)

INNER = root_svg(100, 100)
INNER_OFFSET_VB = root_svg(100, 100, viewbox=(50, 50, 100, 100))
INNER_SCALED_VB = root_svg(100, 100, viewbox=(0, 0, 50, 50),
                           body=corners_body(0, 0, 50, 50, size=10))


def test_image_svg_meet():
    image = render(image_doc(INNER, 0, 0, 200, 100, 'xMidYMid meet'))
    assert_pixel(image, 55, 5, RED)
    assert_pixel(image, 145, 95, YELLOW)
    assert_pixel(image, 100, 50, BLACK)
    assert_pixel(image, 25, 50, WHITE)


def test_image_svg_none():
    image = render(image_doc(INNER, 0, 0, 200, 100, 'none'))
    assert_pixel(image, 10, 5, RED)
    assert_pixel(image, 190, 95, YELLOW)
    assert_pixel(image, 100, 50, BLACK)


def test_image_svg_slice_clipped():
    """slice overflow is clipped to the image viewport, siblings intact."""
    svg = image_doc(INNER, 50, 25, 100, 50, 'xMidYMid slice',
                    background='#ff00ff')
    image = render(svg)
    assert_pixel(image, 100, 10, MAGENTA)
    assert_pixel(image, 100, 90, MAGENTA)
    assert_pixel(image, 10, 50, MAGENTA)
    assert_pixel(image, 100, 50, BLACK)


def test_image_svg_nonzero_viewbox_origin():
    """Regression: the referenced viewBox origin must be honored."""
    for par, probes in (
            ('xMidYMid meet', ((55, 5, RED), (145, 95, YELLOW),
                               (25, 50, WHITE))),
            ('none', ((10, 5, RED), (190, 95, YELLOW))),
            ('xMidYMid slice', ((5, 5, WHITE),))):
        image = render(image_doc(INNER_OFFSET_VB, 0, 0, 200, 100, par))
        for x, y, color in probes:
            assert_pixel(image, x, y, color)
        assert_pixel(image, 100, 50, BLACK)


def test_image_svg_viewbox_scale():
    """viewBox smaller than the referenced viewport: scales compose."""
    image = render(image_doc(INNER_SCALED_VB, 0, 0, 200, 100,
                             'xMidYMid meet'))
    assert_pixel(image, 55, 5, RED)
    assert_pixel(image, 145, 95, YELLOW)
    assert_pixel(image, 100, 50, BLACK)
    assert_pixel(image, 25, 50, WHITE)


def test_image_svg_defer_keyword():
    """A leading "defer" token must not break parsing of the rest."""
    image = render(image_doc(INNER, 0, 0, 200, 100, 'defer xMidYMid slice'))
    assert_pixel(image, 5, 5, WHITE)
    assert_pixel(image, 100, 50, BLACK)
    image = render(image_doc(INNER, 0, 0, 200, 100, 'defer'))
    assert_pixel(image, 55, 5, RED)  # falls back to xMidYMid meet


def test_image_svg_degenerate_viewbox():
    """A degenerate referenced viewBox renders nothing, without crashing."""
    inner = '''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"
     viewBox="0 0 0 100"><rect width="100" height="100" fill="#ff0000"/>
</svg>'''
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
  <image x="0" y="0" width="200" height="100"
         href="data:image/svg+xml;base64,{base64.b64encode(inner.encode()).decode()}"/>
  <rect x="90" y="40" width="20" height="20" fill="#00ff00"/>
</svg>'''
    image = render(svg)
    assert_pixel(image, 50, 50, WHITE)  # degenerate image painted nothing
    assert_pixel(image, 100, 50, GREEN)  # state finalized, drawing continues


# Isolation between elements and between calls

def test_none_does_not_leak_into_next_image():
    """Independent scaling of one image must not affect the next one."""
    encoded = base64.b64encode(INNER.encode()).decode()
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="400" height="100">
  <image x="0" y="0" width="200" height="100" preserveAspectRatio="none"
         href="data:image/svg+xml;base64,{encoded}"/>
  <image x="200" y="0" width="200" height="100"
         href="data:image/svg+xml;base64,{encoded}"/>
</svg>'''
    image = render(svg)
    assert_pixel(image, 10, 5, RED)  # first image stretched to its corner
    assert_pixel(image, 255, 5, RED)  # second image centered (meet)
    assert_pixel(image, 225, 50, WHITE)  # its meet band is intact


def test_repeated_calls_are_deterministic():
    svg = root_svg(200, 100, par='none')
    first = svg2png(bytestring=svg.encode())
    second = svg2png(bytestring=svg.encode())
    assert first == second
    # A "none" render must not leak state into the next convert call.
    image = render(root_svg(200, 100))
    assert_pixel(image, 55, 5, RED)
    assert_pixel(image, 25, 50, WHITE)


# Degenerate root viewport: existing error contract

def test_empty_root_size_raises():
    with pytest.raises(ValueError, match='size is undefined'):
        svg2png(bytestring=b'<svg xmlns="http://www.w3.org/2000/svg" '
                           b'width="0" height="100"/>')


# Sample PNG generation (actual image results)

SAMPLES = {
    'root-meet-landscape': (root_svg(200, 100), {}),
    'root-meet-portrait': (root_svg(100, 200), {}),
    'root-slice-landscape': (root_svg(200, 100, par='xMidYMid slice'), {}),
    'root-none-landscape': (root_svg(200, 100, par='none'), {}),
    'root-nonzero-viewbox-meet': (
        root_svg(200, 100, viewbox=(50, 50, 100, 100)), {}),
    'nested-meet': (nested_doc('xMidYMid meet'), {}),
    'nested-slice-clipped': (nested_doc('xMidYMid slice'), {}),
    'nested-slice-overflow-visible': (
        nested_doc('xMidYMid slice', overflow='visible'), {}),
    'image-meet': (image_doc(INNER, 0, 0, 200, 100, 'xMidYMid meet'), {}),
    'image-slice-clipped': (
        image_doc(INNER, 50, 25, 100, 50, 'xMidYMid slice',
                  background='#ff00ff'), {}),
    'image-none': (image_doc(INNER, 0, 0, 200, 100, 'none'), {}),
    'image-nonzero-viewbox-meet': (
        image_doc(INNER_OFFSET_VB, 0, 0, 200, 100, 'xMidYMid meet'), {}),
    'output-size-params': (root_svg(100, 100),
                           {'output_width': 200, 'output_height': 100}),
}


def generate_samples(directory):
    """Render the sample documents to PNG files."""
    os.makedirs(directory, exist_ok=True)
    for name, (svg, kwargs) in SAMPLES.items():
        png = svg2png(bytestring=svg.encode(), background_color='white',
                      **kwargs)
        with open(os.path.join(directory, f'{name}.png'), 'wb') as file:
            file.write(png)


if __name__ == '__main__':
    generate_samples(os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'par-renderings'))
