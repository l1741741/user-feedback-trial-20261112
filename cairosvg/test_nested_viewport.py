"""
Nested viewport test suite.

Test that percentage lengths (widths, heights, circle radii, line
positions) are resolved against the viewport established by the element
they belong to: nested ``svg`` tags, ``symbol`` tags referenced by
``use`` tags, SVG documents embedded with ``image`` tags, and ``marker``
content. Also test that the outer coordinate system is restored after
leaving a nested viewport.

"""

import base64
import io

from PIL import Image

from . import svg2png

WHITE = (255, 255, 255, 255)
RED = (255, 0, 0, 255)
GREEN = (0, 128, 0, 255)
BLUE = (0, 0, 255, 255)


def _b64(svg):
    """Encode an SVG document as base64 for data URIs."""
    return base64.b64encode(svg.encode('utf-8')).decode('ascii')


def render(source, **kwargs):
    """Render ``source`` to a PIL image."""
    png = svg2png(bytestring=source.encode('utf-8'), **kwargs)
    return Image.open(io.BytesIO(png)).convert('RGBA')


def assert_pixel(image, x, y, color):
    """Assert that pixel at ``(x, y)`` is exactly ``color``."""
    assert image.getpixel((x, y)) == color, (
        f'pixel ({x}, {y}) is {image.getpixel((x, y))}, expected {color}')


def test_nested_svg_percentages_use_inner_viewport():
    """Percentages in a nested svg are resolved against its viewport."""
    image = render('''
        <svg width="400" height="400" xmlns="http://www.w3.org/2000/svg">
          <rect width="400" height="400" fill="white"/>
          <svg x="50" y="50" width="200" height="200"
               viewBox="0 0 100 100">
            <rect width="50%" height="50%" fill="red"/>
            <circle cx="50%" cy="50%" r="25%" fill="blue"/>
          </svg>
        </svg>''')
    # Viewport is 200px wide for 100 user units: 50% is 100 device pixels
    assert_pixel(image, 60, 60, RED)
    assert_pixel(image, 140, 60, RED)
    assert_pixel(image, 160, 60, WHITE)  # not 50% of the 400px canvas
    # Circle center is at 50% of the inner viewport: (150, 150) device
    # pixels, radius is 25% of the inner viewport diagonal: 50 pixels
    assert_pixel(image, 150, 150, BLUE)
    assert_pixel(image, 150, 190, BLUE)
    assert_pixel(image, 150, 210, WHITE)


def test_nested_svg_viewbox_origin_and_preserve_aspect_ratio():
    """viewBox with non-zero origin and preserveAspectRatio is mapped."""
    image = render('''
        <svg width="400" height="300" xmlns="http://www.w3.org/2000/svg">
          <rect width="400" height="300" fill="white"/>
          <svg x="20" y="20" width="200" height="100"
               viewBox="10 10 100 100"
               preserveAspectRatio="xMidYMid meet">
            <rect x="10" y="10" width="50%" height="50%" fill="red"/>
          </svg>
        </svg>''')
    # meet: scale is min(200/100, 100/100) = 1, content is centered
    # horizontally: 50 user units are added on the left. Percentages are
    # resolved against the letterboxed viewport: 200x100 user units.
    # Red rectangle: user (10, 10), 50% x 50% = 100x50 user units,
    # mapped to device (70, 20) -> (170, 70).
    assert_pixel(image, 75, 25, RED)
    assert_pixel(image, 165, 65, RED)
    assert_pixel(image, 65, 25, WHITE)  # letterbox + viewBox origin
    assert_pixel(image, 175, 25, WHITE)  # 50% of 200 units, not of 400px
    assert_pixel(image, 75, 75, WHITE)  # 50% of 100 units, not of 300px


def test_outer_coordinate_system_restored_after_nested_svg():
    """Outer shapes keep the outer coordinate system after a nested svg."""
    image = render('''
        <svg width="400" height="400" xmlns="http://www.w3.org/2000/svg">
          <rect width="400" height="400" fill="white"/>
          <svg x="50" y="50" width="200" height="200"
               viewBox="0 0 100 100">
            <rect width="50%" height="50%" fill="red"/>
          </svg>
          <rect x="300" y="300" width="25%" height="25%" fill="green"/>
        </svg>''')
    # The green rectangle belongs to the root viewport: 25% is 100
    # device pixels, not 25% of the nested viewport
    assert_pixel(image, 350, 350, GREEN)
    assert_pixel(image, 305, 305, GREEN)
    assert_pixel(image, 250, 350, WHITE)
    assert_pixel(image, 350, 250, WHITE)


def test_sibling_viewports_dont_leak_into_each_other():
    """A nested svg without size doesn't reuse its sibling's size."""
    template = '''
        <svg width="480" height="240" xmlns="http://www.w3.org/2000/svg">
          <rect width="480" height="240" fill="white"/>
          %s
        </svg>'''
    with_viewbox = '''
          <svg x="20" y="20" width="200" height="200"
               viewBox="0 0 100 100">
            <rect width="50%" height="50%" fill="red"/>
          </svg>'''
    without_viewbox = '''
          <svg x="240" y="20" width="50%" height="50%">
            <rect width="50%" height="50%" fill="blue"/>
          </svg>'''
    # Whatever the sibling order is, results must be the same
    for children in (
            with_viewbox + without_viewbox,
            without_viewbox.replace('240', '240') + with_viewbox):
        image = render(template % children)
        # First viewport: 200px for 100 user units, red is 50% = 100px
        assert_pixel(image, 30, 30, RED)
        assert_pixel(image, 110, 110, RED)
        assert_pixel(image, 130, 130, WHITE)
        # Second viewport: 50% of the root = 240x120px, blue is 50% of
        # this viewport, not of the previous sibling's one
        assert_pixel(image, 250, 30, BLUE)
        assert_pixel(image, 350, 70, BLUE)
        assert_pixel(image, 370, 30, WHITE)
        assert_pixel(image, 250, 90, WHITE)


def test_deeply_nested_viewports():
    """Percentages are resolved against the closest viewport."""
    image = render('''
        <svg width="480" height="480" xmlns="http://www.w3.org/2000/svg">
          <rect width="480" height="480" fill="white"/>
          <svg x="40" y="40" width="400" height="400"
               viewBox="0 0 200 200">
            <svg x="20" y="20" width="100" height="100"
                 viewBox="0 0 100 100">
              <rect width="50%" height="50%" fill="red"/>
              <circle cx="50%" cy="50%" r="25%" fill="blue"/>
            </svg>
          </svg>
        </svg>''')
    # Inner viewport: 40 + 20*2 = 80 device pixels, 200px for 100 user
    # units. Red is 50% = 100px, circle center is (180, 180), r = 50px.
    assert_pixel(image, 90, 90, RED)
    assert_pixel(image, 170, 90, RED)
    assert_pixel(image, 190, 90, WHITE)
    assert_pixel(image, 180, 180, BLUE)
    assert_pixel(image, 220, 180, BLUE)
    assert_pixel(image, 240, 180, WHITE)


def test_symbol_use_matches_direct_nesting():
    """A symbol referenced by use renders like a directly nested svg."""
    icon = '''
            <rect width="50%" height="50%" fill="red"/>
            <circle cx="50%" cy="50%" r="25%" fill="blue"/>
            <line x1="25%" y1="0" x2="25%" y2="100%"
                  stroke="green" stroke-width="2"/>'''
    direct = render('''
        <svg width="400" height="400" xmlns="http://www.w3.org/2000/svg">
          <rect width="400" height="400" fill="white"/>
          <svg x="50" y="50" width="200" height="200"
               viewBox="0 0 100 100">%s
          </svg>
        </svg>''' % icon)
    via_use = render('''
        <svg width="400" height="400" xmlns="http://www.w3.org/2000/svg"
             xmlns:xlink="http://www.w3.org/1999/xlink">
          <rect width="400" height="400" fill="white"/>
          <defs>
            <symbol id="icon" viewBox="0 0 100 100">%s
            </symbol>
          </defs>
          <use xlink:href="#icon" x="50" y="50" width="200"
               height="200"/>
        </svg>''' % icon)
    assert direct.tobytes() == via_use.tobytes()


def test_image_embedded_svg_percentages():
    """Percentages in an SVG embedded with image use the image viewport."""
    embedded = '''
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
          <rect width="50%" height="50%" fill="red"/>
          <circle cx="50%" cy="50%" r="25%" fill="blue"/>
          <line x1="75%" y1="0" x2="75%" y2="100%"
                stroke="green" stroke-width="2"/>
        </svg>'''
    image = render('''
        <svg width="400" height="400" xmlns="http://www.w3.org/2000/svg"
             xmlns:xlink="http://www.w3.org/1999/xlink">
          <rect width="400" height="400" fill="white"/>
          <image x="50" y="50" width="200" height="200"
                 xlink:href="data:image/svg+xml;base64,%s"/>
        </svg>''' % _b64(embedded))
    # The embedded document is 100x100 user units in a 200px region
    assert_pixel(image, 60, 60, RED)
    assert_pixel(image, 140, 60, RED)
    assert_pixel(image, 160, 60, WHITE)  # not 50% of the 400px canvas
    assert_pixel(image, 150, 150, BLUE)
    assert_pixel(image, 150, 190, BLUE)
    assert_pixel(image, 150, 210, WHITE)
    # Line at 75% of the embedded viewport: 50 + 150 = 200 device pixels
    assert_pixel(image, 200, 100, GREEN)
    assert_pixel(image, 220, 100, WHITE)


def test_image_embedded_svg_without_size():
    """An embedded SVG without size doesn't reuse the outer size."""
    with_viewbox_only = '''
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
          <rect width="50%" height="50%" fill="red"/>
        </svg>'''
    without_size = '''
        <svg xmlns="http://www.w3.org/2000/svg">
          <rect width="50%" height="50%" fill="blue"/>
        </svg>'''
    image = render('''
        <svg width="400" height="400" xmlns="http://www.w3.org/2000/svg"
             xmlns:xlink="http://www.w3.org/1999/xlink">
          <rect width="400" height="400" fill="white"/>
          <image x="20" y="20" width="160" height="160"
                 xlink:href="data:image/svg+xml;base64,%s"/>
          <image x="220" y="20" width="160" height="160"
                 xlink:href="data:image/svg+xml;base64,%s"/>
        </svg>''' % (_b64(with_viewbox_only), _b64(without_size)))
    # 50% of the 160px image regions, not 50% of the 400px canvas
    assert_pixel(image, 30, 30, RED)
    assert_pixel(image, 90, 90, RED)
    assert_pixel(image, 110, 110, WHITE)
    assert_pixel(image, 230, 30, BLUE)
    assert_pixel(image, 290, 90, BLUE)
    assert_pixel(image, 310, 110, WHITE)


def test_image_embedded_svg_viewbox_origin():
    """A non-zero viewBox origin is mapped to the image region origin."""
    embedded = '''
        <svg xmlns="http://www.w3.org/2000/svg" width="160" height="160"
             viewBox="10 10 80 80">
          <rect x="10" y="10" width="40" height="40" fill="red"/>
        </svg>'''
    image = render('''
        <svg width="300" height="300" xmlns="http://www.w3.org/2000/svg"
             xmlns:xlink="http://www.w3.org/1999/xlink">
          <rect width="300" height="300" fill="white"/>
          <image x="50" y="50" width="160" height="160"
                 xlink:href="data:image/svg+xml;base64,%s"/>
        </svg>''' % _b64(embedded))
    # User unit (10, 10) is the origin of the image region, scale is 2
    assert_pixel(image, 55, 55, RED)
    assert_pixel(image, 125, 125, RED)
    assert_pixel(image, 135, 135, WHITE)


def test_image_embedded_svg_preserve_aspect_ratio():
    """preserveAspectRatio letterboxing uses the right offset units."""
    embedded = '''
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"
             viewBox="0 0 50 50">
          <rect width="50" height="50" fill="red"/>
        </svg>'''
    image = render('''
        <svg width="300" height="200" xmlns="http://www.w3.org/2000/svg"
             xmlns:xlink="http://www.w3.org/1999/xlink">
          <rect width="300" height="200" fill="white"/>
          <image x="50" y="50" width="200" height="100"
                 xlink:href="data:image/svg+xml;base64,%s"/>
        </svg>''' % _b64(embedded))
    # meet: the 100x100 document fits the 200x100 region with a 50
    # device pixels horizontal letterbox on each side
    assert_pixel(image, 105, 100, RED)
    assert_pixel(image, 195, 100, RED)
    assert_pixel(image, 95, 100, WHITE)
    assert_pixel(image, 205, 100, WHITE)


def test_image_embedded_svg_empty_viewbox():
    """An embedded SVG with an empty viewBox doesn't crash or render."""
    embedded = '''
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 0 0">
          <rect width="50" height="50" fill="red"/>
        </svg>'''
    image = render('''
        <svg width="200" height="200" xmlns="http://www.w3.org/2000/svg"
             xmlns:xlink="http://www.w3.org/1999/xlink">
          <rect width="200" height="200" fill="white"/>
          <image x="10" y="10" width="100" height="100"
                 xlink:href="data:image/svg+xml;base64,%s"/>
        </svg>''' % _b64(embedded))
    assert image.size == (200, 200)
    assert image.tobytes() == bytes(WHITE) * (200 * 200)


def test_marker_percentages_use_marker_viewport():
    """Percentages in marker content use the marker viewport."""
    image = render('''
        <svg width="400" height="200" xmlns="http://www.w3.org/2000/svg">
          <rect width="400" height="200" fill="white"/>
          <defs>
            <marker id="dot" markerWidth="20" markerHeight="20"
                    viewBox="0 0 20 20" refX="10" refY="10"
                    markerUnits="userSpaceOnUse">
              <circle cx="50%" cy="50%" r="50%" fill="red"/>
            </marker>
          </defs>
          <line x1="100" y1="100" x2="300" y2="100"
                stroke="black" stroke-width="2"
                marker-start="url(#dot)" marker-end="url(#dot)"/>
        </svg>''')
    # Marker viewport is 20x20 user units: the dot is centered on the
    # line ends with a radius of 10 device pixels
    assert_pixel(image, 100, 100, RED)
    assert_pixel(image, 108, 100, RED)
    assert_pixel(image, 300, 100, RED)
    assert_pixel(image, 292, 100, RED)
    # Not 50% of the 400x200 outer canvas
    assert_pixel(image, 100, 60, WHITE)
    assert_pixel(image, 115, 110, WHITE)
    assert_pixel(image, 85, 90, WHITE)


def test_output_size_interface_with_nested_viewports():
    """output_width/output_height/scale keep working with nested svg."""
    source = '''
        <svg width="400" height="400" xmlns="http://www.w3.org/2000/svg">
          <rect width="400" height="400" fill="white"/>
          <svg x="50" y="50" width="200" height="200"
               viewBox="0 0 100 100">
            <rect width="50%" height="50%" fill="red"/>
          </svg>
        </svg>'''
    image = render(source, output_width=800, output_height=800)
    assert image.size == (800, 800)
    # Everything is scaled by 2: red goes from (100, 100) to (300, 300)
    assert_pixel(image, 120, 120, RED)
    assert_pixel(image, 280, 280, RED)
    assert_pixel(image, 320, 320, WHITE)
    image = render(source, scale=2)
    assert image.size == (800, 800)
    assert_pixel(image, 280, 280, RED)
