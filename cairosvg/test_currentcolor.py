"""
Regression tests for ``currentColor`` resolution between definitions,
references and instances (``symbol``/``use``, gradients, strokes).

``currentColor`` is resolved at paint time, against the ``color`` property
inherited by the context where the element is actually drawn (a ``use``
instance, a themed group), never against the context where the shared
definition lives. Shared definitions are never modified, so siblings and
later instances are not polluted by earlier draws.

These tests render real PNGs and check actual pixel values (fill, stroke
and gradient endpoints) with a small dependency-free PNG decoder.

"""

import struct
import zlib

from . import svg2png


def read_png(png_bytes):
    """Decode a PNG byte string into a ``(width, height, rows)`` tuple.

    ``rows[y][x * 4:(x + 1) * 4]`` is the straight (non-premultiplied)
    RGBA pixel at ``(x, y)``.

    """
    pos = 8
    idat = b''
    while pos < len(png_bytes):
        length, = struct.unpack('>I', png_bytes[pos:pos + 4])
        chunk_type = png_bytes[pos + 4:pos + 8]
        if chunk_type == b'IHDR':
            width, height = struct.unpack('>II', png_bytes[pos + 8:pos + 16])
        elif chunk_type == b'IDAT':
            idat += png_bytes[pos + 8:pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(idat)
    stride = width * 4 + 1
    rows = []
    previous = bytearray(width * 4)
    for y in range(height):
        filter_type = raw[y * stride]
        line = bytearray(raw[y * stride + 1:(y + 1) * stride])
        if filter_type == 1:
            for i in range(4, len(line)):
                line[i] = (line[i] + line[i - 4]) & 255
        elif filter_type == 2:
            for i in range(len(line)):
                line[i] = (line[i] + previous[i]) & 255
        elif filter_type == 3:
            for i in range(len(line)):
                left = line[i - 4] if i >= 4 else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 255
        elif filter_type == 4:
            for i in range(len(line)):
                left = line[i - 4] if i >= 4 else 0
                up = previous[i]
                up_left = previous[i - 4] if i >= 4 else 0
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                predictor = (
                    left if pa <= pb and pa <= pc else
                    up if pb <= pc else up_left)
                line[i] = (line[i] + predictor) & 255
        rows.append(bytes(line))
        previous = line
    return width, height, rows


def render_pixels(svg, width, height):
    """Render ``svg`` to PNG and return a ``pixel(x, y) -> RGBA`` getter."""
    png = svg2png(bytestring=svg.encode('utf-8'),
                  output_width=width, output_height=height)
    _, _, rows = read_png(png)

    def pixel(x, y):
        return tuple(rows[y][x * 4:(x + 1) * 4])

    return pixel


# One document with two themes, the same symbol referenced once per theme,
# a gradient stop and a stroke using currentColor, plus a plain path
# control and a default sibling to detect pollution.
THEMES_SVG = '''
<svg xmlns="http://www.w3.org/2000/svg" width="400" height="120"
     viewBox="0 0 400 120">
  <defs>
    <linearGradient id="themeGradient" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="currentColor"/>
      <stop offset="1" stop-color="#ffffff"/>
    </linearGradient>
    <symbol id="icon" viewBox="0 0 100 100">
      <rect x="5" y="5" width="40" height="40" fill="currentColor"/>
      <circle cx="70" cy="25" r="20" fill="url(#themeGradient)"/>
      <path d="M10 70 L90 70" stroke="currentColor" stroke-width="8"
            fill="none"/>
    </symbol>
  </defs>
  <g color="#ff0000">
    <use href="#icon" x="0" y="0" width="100" height="100"/>
  </g>
  <g color="#0000ff">
    <use href="#icon" x="100" y="0" width="100" height="100"/>
  </g>
  <g color="#800080">
    <rect x="205" y="5" width="40" height="40" fill="currentColor"/>
  </g>
  <rect x="305" y="5" width="40" height="40"/>
</svg>
'''


def test_currentcolor_two_themes_same_symbol():
    """Same symbol, two themes: fill, stroke and gradient follow the theme."""
    pixel = render_pixels(THEMES_SVG, 400, 120)

    # Fill: currentColor resolves against each instance's theme color
    assert pixel(25, 25) == (255, 0, 0, 255)
    assert pixel(125, 25) == (0, 0, 255, 255)

    # Stroke: same per-instance resolution
    assert pixel(50, 70) == (255, 0, 0, 255)
    assert pixel(150, 70) == (0, 0, 255, 255)

    # Gradient endpoints: left stop is currentColor (theme), right is white
    red_left, red_right = pixel(52, 25), pixel(88, 25)
    assert red_left[0] > 200 and red_left[1] < 80 and red_left[2] < 80
    assert all(channel > 200 for channel in red_right[:3])
    blue_left = pixel(152, 25)
    assert blue_left[2] > 200 and blue_left[0] < 80 and blue_left[1] < 80

    # Plain path control: currentColor works outside definitions too
    assert pixel(225, 25) == (128, 0, 128, 255)

    # Sibling drawn after the uses keeps the default fill: no pollution
    assert pixel(325, 25) == (0, 0, 0, 255)


def test_currentcolor_order_swap_is_stable():
    """Swapping theme order doesn't change results (no shared-state leak)."""
    swapped = THEMES_SVG.replace('#ff0000', '#00ff00').replace(
        '<g color="#00ff00">\n    <use href="#icon" x="0"',
        '<g color="#0000ff">\n    <use href="#icon" x="0"').replace(
        '<g color="#0000ff">\n    <use href="#icon" x="100"',
        '<g color="#00ff00">\n    <use href="#icon" x="100"')
    pixel = render_pixels(swapped, 400, 120)

    # First group is now blue, second is green: each instance is independent
    assert pixel(25, 25) == (0, 0, 255, 255)
    assert pixel(125, 25) == (0, 255, 0, 255)
    blue_left = pixel(52, 25)
    assert blue_left[2] > 200 and blue_left[0] < 80
    green_left = pixel(152, 25)
    assert green_left[1] > 200 and green_left[0] < 80


CASCADE_SVG = '''
<svg xmlns="http://www.w3.org/2000/svg" width="300" height="120"
     viewBox="0 0 300 120">
  <symbol id="plain" viewBox="0 0 100 100">
    <rect x="5" y="5" width="40" height="40"/>
    <path d="M10 70 L90 70" stroke-width="8" fill="none"/>
  </symbol>
  <symbol id="painted" viewBox="0 0 100 100">
    <rect x="5" y="5" width="40" height="40" fill="currentColor"/>
  </symbol>
  <!-- Instance fill/stroke cascade into content that doesn't set them -->
  <use href="#plain" x="0" y="0" width="100" height="100"
       fill="#ffa500" stroke="#000080"/>
  <!-- Content's own fill wins over the instance's fill (SVG style rules);
       currentColor still follows the instance context -->
  <g color="#008000">
    <use href="#painted" x="100" y="0" width="100" height="100"
         fill="#ffff00"/>
  </g>
  <!-- Outer CSS style + local color override on the instance -->
  <g style="color: #ff0000">
    <use href="#painted" x="200" y="0" width="100" height="100"
         style="color: #0000ff"/>
  </g>
</svg>
'''


def test_instance_cascade_and_override_priority():
    """Instance paint cascades only where content doesn't set its own."""
    pixel = render_pixels(CASCADE_SVG, 300, 120)

    # use fill/stroke inherit into the unpainted symbol content
    assert pixel(25, 25) == (255, 165, 0, 255)
    assert pixel(50, 70) == (0, 0, 128, 255)

    # The symbol's own fill="currentColor" beats the use's fill="yellow";
    # currentColor follows the instance context (green), not the definition
    assert pixel(125, 25) == (0, 128, 0, 255)

    # Local style on the instance overrides the outer group's style
    assert pixel(225, 25) == (0, 0, 255, 255)


NESTED_SVG = '''
<svg xmlns="http://www.w3.org/2000/svg" width="300" height="120"
     viewBox="0 0 300 120">
  <linearGradient id="fade" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="currentColor"/>
    <stop offset="1" stop-color="#ffffff"/>
  </linearGradient>
  <symbol id="dot" viewBox="0 0 100 100">
    <rect x="5" y="5" width="40" height="40" fill="currentColor"/>
    <circle cx="70" cy="25" r="20" fill="url(#fade)"/>
  </symbol>
  <g id="alias"><use href="#dot" width="100" height="100"/></g>
  <!-- Nested use: theme color crosses two levels of references -->
  <g color="#ff00ff">
    <use href="#alias" x="0" y="0" width="100" height="100"/>
  </g>
  <!-- Missing color: currentColor falls back to the initial value, black -->
  <use href="#dot" x="100" y="0" width="100" height="100"/>
  <!-- inherit keyword: color inherits through groups, fill="inherit" with
       no parent fill falls back to the initial value (black) -->
  <g color="#123456">
    <g>
      <rect x="205" y="5" width="40" height="40" fill="currentColor"/>
    </g>
    <rect x="255" y="5" width="40" height="40" fill="inherit"/>
  </g>
</svg>
'''


def test_nested_use_missing_color_and_inherit():
    """Nested use, missing color and inherit have explainable results."""
    pixel = render_pixels(NESTED_SVG, 300, 120)

    # Nested use: fill and gradient follow the outer theme (magenta)
    assert pixel(25, 25) == (255, 0, 255, 255)
    magenta_left = pixel(52, 25)
    assert magenta_left[0] > 200 and magenta_left[2] > 200
    assert magenta_left[1] < 80

    # Missing color: currentColor is black (initial value)
    assert pixel(125, 25) == (0, 0, 0, 255)
    black_left = pixel(152, 25)
    assert all(channel < 60 for channel in black_left[:3])

    # color inherits through nested groups
    assert pixel(225, 25) == (18, 52, 86, 255)

    # fill="inherit" without a parent fill: initial value (black)
    assert pixel(275, 25) == (0, 0, 0, 255)


OPACITY_SVG = '''
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120"
     viewBox="0 0 200 120">
  <symbol id="block" viewBox="0 0 100 100">
    <rect x="5" y="5" width="90" height="90" fill="currentColor"/>
  </symbol>
  <g color="#ff0000" opacity="0.5">
    <use href="#block" x="0" y="0" width="100" height="100"/>
  </g>
  <g color="#ff0000">
    <use href="#block" x="100" y="0" width="100" height="100"/>
  </g>
</svg>
'''


def test_instances_with_different_opacity_are_independent():
    """Group opacity applies per instance and doesn't leak to siblings."""
    pixel = render_pixels(OPACITY_SVG, 200, 120)

    translucent = pixel(50, 50)
    assert translucent[:3] == (255, 0, 0)
    assert 120 <= translucent[3] <= 135

    # The second instance keeps full opacity
    assert pixel(150, 50) == (255, 0, 0, 255)
