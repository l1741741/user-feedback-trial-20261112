"""
Pattern tiling test suite.

These tests render real PNGs and check the tiling period, phase and
transparency of paint servers. They cover the interactions between pattern
units, viewBox, patternContentUnits, patternTransform and multiple
references of the same definition.

"""

import io

import cairocffi as cairo

from . import svg2png


def render(svg):
    """Render an SVG byte string to an in-memory image surface."""
    return cairo.ImageSurface.create_from_png(
        io.BytesIO(svg2png(bytestring=svg)))


def pixel(image, x, y):
    """Return the un-premultiplied RGBA tuple of a pixel."""
    data = bytes(image.get_data())
    index = y * image.get_stride() + x * 4
    blue, green, red, alpha = data[index:index + 4]
    if alpha:
        red = round(red * 255 / alpha)
        green = round(green * 255 / alpha)
        blue = round(blue * 255 / alpha)
    return red, green, blue, alpha


def color_runs(image, x0, x1, y, predicate):
    """Return the inclusive x-ranges where ``predicate`` is true on a row."""
    runs = []
    start = None
    for x in range(x0, x1):
        matching = predicate(*pixel(image, x, y))
        if matching and start is None:
            start = x
        if not matching and start is not None:
            runs.append((start, x - 1))
            start = None
    if start is not None:
        runs.append((start, x1 - 1))
    return runs


def is_green(r, g, b, a):
    return g > 100 and r < 60 and b < 90


def is_red(r, g, b, a):
    return r > 200 and g < 60 and b < 60


def is_transparent(r, g, b, a):
    return a == 0


GREEN = is_green
RED = is_red
TRANSPARENT = is_transparent


def test_userspace_tile_size_independent_of_host_bounds():
    """userSpaceOnUse tiles keep a fixed size whatever the host shape is."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"
         viewBox="0 0 200 100">
      <defs>
        <pattern id="p" width="10" height="10"
                 patternUnits="userSpaceOnUse">
          <rect width="10" height="10" fill="white"/>
          <rect width="5" height="5" fill="red"/>
        </pattern>
      </defs>
      <rect x="0" y="0" width="90" height="100" fill="url(#p)"/>
      <rect x="100" y="0" width="100" height="60" fill="url(#p)"/>
    </svg>'''
    image = render(svg)
    # Both shapes share the global user-space tiling: identical phase.
    for point in ((1, 1), (101, 1), (11, 11), (111, 11)):
        assert RED(*pixel(image, *point))
    for point in ((6, 6), (106, 6)):
        assert pixel(image, *point)[:3] == (255, 255, 255)


def test_object_bounding_box_tile_follows_each_shape():
    """objectBoundingBox tiles are sized from each shape's bounding box."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="220" height="120">
      <defs>
        <pattern id="p" width="20%" height="20%">
          <rect width="100%" height="100%" fill="white"/>
          <rect width="50%" height="50%" fill="green"/>
        </pattern>
      </defs>
      <rect x="0" y="0" width="100" height="100" fill="url(#p)"/>
      <rect x="120" y="0" width="80" height="80" fill="url(#p)"/>
    </svg>'''
    image = render(svg)
    # Shape A: 100px box, 20px tiles, green quarter = first 10px of each.
    assert color_runs(image, 0, 100, 5, GREEN) == [
        (0, 9), (20, 29), (40, 49), (60, 69), (80, 89)]
    # Shape B: 80px box, 16px tiles anchored at its own origin (120).
    assert color_runs(image, 120, 200, 5, GREEN) == [
        (120, 127), (136, 143), (152, 159), (168, 175), (184, 191)]


def test_object_bounding_box_origin_is_bbox_origin():
    """The tile phase starts at the shape origin, not at the canvas origin."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs>
        <pattern id="p" width="25%" height="25%">
          <rect width="100%" height="100%" fill="white"/>
          <rect width="50%" height="50%" fill="green"/>
        </pattern>
      </defs>
      <rect x="10" y="10" width="80" height="80" fill="url(#p)"/>
    </svg>'''
    image = render(svg)
    # 20px tiles starting at (10, 10); green covers the first 10px.
    assert color_runs(image, 10, 90, 15, GREEN) == [
        (10, 19), (30, 39), (50, 59), (70, 79)]


def test_pattern_reused_with_different_fill_opacity():
    """Reusing a pattern must not accumulate opacity across references."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60">
      <defs>
        <pattern id="p" width="10" height="10"
                 patternUnits="userSpaceOnUse">
          <rect width="10" height="10" fill="blue"/>
        </pattern>
      </defs>
      <rect x="0" y="0" width="60" height="60" fill="url(#p)"
            fill-opacity="0.5"/>
      <rect x="60" y="0" width="60" height="60" fill="url(#p)"
            fill-opacity="1"/>
    </svg>'''
    image = render(svg)
    assert pixel(image, 30, 30)[3] == 128
    assert pixel(image, 90, 30)[3] == 255


def test_pattern_reuse_does_not_consume_definition_size():
    """Each reference resolves width/height fresh from the definition."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="220" height="120">
      <defs>
        <pattern id="p" width="25%" height="25%">
          <rect width="100%" height="100%" fill="white"/>
          <rect width="50%" height="50%" fill="green"/>
        </pattern>
      </defs>
      <rect x="0" y="0" width="100" height="100" fill="url(#p)"/>
      <rect x="120" y="0" width="40" height="40" fill="url(#p)"/>
    </svg>'''
    # This used to raise a cairo MemoryError because the popped width grew
    # exponentially between references. With the fix, shape A gets 25px tiles
    # and shape B gets 10px tiles (25% of its own 40px box).
    image = render(svg)
    assert color_runs(image, 0, 100, 2, GREEN)[0] == (0, 11)  # 25px tile
    assert color_runs(image, 120, 160, 2, GREEN) == [
        (120, 124), (130, 134), (140, 144), (150, 154)]  # 10px tiles


def test_offset_tile_with_transparency_repeats_continuously():
    """Tiles with an origin offset and transparent whitespace stay seamless."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <defs>
        <pattern id="p" width="20" height="20" x="5" y="7"
                 patternUnits="userSpaceOnUse">
          <rect x="2" y="2" width="4" height="4" fill="red"/>
        </pattern>
      </defs>
      <rect width="200" height="100" fill="url(#p)"/>
    </svg>'''
    image = render(svg)
    # First dot center is at origin (5, 7) + local (4, 4) = (9, 11); it then
    # repeats every 20px. The gaps between dots stay fully transparent.
    for x in (9, 29, 49, 69):
        assert RED(*pixel(image, x, 11))
        assert TRANSPARENT(*pixel(image, x + 10, 11))


def test_rotated_pattern_keeps_tiling_origin_and_phase():
    """Rotation moves tiles and origin together, and is deterministic."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <defs>
        <pattern id="p" width="20" height="20" x="5" y="7"
                 patternUnits="userSpaceOnUse"
                 patternTransform="rotate(45)">
          <rect width="20" height="20" fill="white"/>
          <rect x="0" y="0" width="4" height="20" fill="purple"/>
        </pattern>
      </defs>
      <rect x="0" y="0" width="100" height="100" fill="url(#p)"/>
      <rect x="100" y="0" width="100" height="100" fill="url(#p)"/>
    </svg>'''
    first = svg2png(bytestring=svg)
    second = svg2png(bytestring=svg)
    assert first == second
    image = cairo.ImageSurface.create_from_png(io.BytesIO(first))
    # The rotated stripes are diagonal: a horizontal scan crosses them
    # periodically rather than producing solid vertical bands.
    purples = color_runs(
        image, 0, 200, 50,
        lambda r, g, b, a: r > 100 and b > 100 and g < 90)
    assert len(purples) > 2


def test_invalid_pattern_does_not_leak_previous_paint():
    """A zero-sized pattern paints nothing instead of reusing an old source."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="180" height="60">
      <defs>
        <pattern id="bad" width="0" height="10"/>
      </defs>
      <rect x="0" y="0" width="60" height="60" fill="red"/>
      <rect x="60" y="0" width="60" height="60" fill="url(#bad)"/>
    </svg>'''
    image = render(svg)
    assert RED(*pixel(image, 30, 30))
    assert TRANSPARENT(*pixel(image, 90, 30))


def test_empty_pattern_does_not_leak_previous_paint():
    """A pattern with no paint in its cells leaves the host untouched."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="180" height="60">
      <defs>
        <pattern id="empty" width="10" height="10"/>
      </defs>
      <rect x="0" y="0" width="60" height="60" fill="red"/>
      <rect x="60" y="0" width="60" height="60" fill="url(#empty)"/>
    </svg>'''
    image = render(svg)
    assert RED(*pixel(image, 30, 30))
    assert TRANSPARENT(*pixel(image, 90, 30))


def test_viewbox_pattern_maps_content_into_tile():
    """A viewBox scales the pattern content to fit the tile."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs>
        <pattern id="p" width="20" height="20" viewBox="0 0 10 10"
                 patternUnits="userSpaceOnUse">
          <rect width="10" height="10" fill="white"/>
          <rect width="5" height="5" fill="teal"/>
        </pattern>
      </defs>
      <rect width="100" height="100" fill="url(#p)"/>
    </svg>'''
    image = render(svg)
    # The 5x5 teal quarter is scaled 2x into the 20px tile (first 10px).
    assert pixel(image, 2, 2)[:3] == (0, 128, 128)
    assert pixel(image, 15, 15)[:3] == (255, 255, 255)


def test_pattern_content_units_object_bounding_box():
    """patternContentUnits=objectBoundingBox uses box fractions for content."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs>
        <pattern id="p" width="1" height="1"
                 patternContentUnits="objectBoundingBox">
          <rect x="0.1" y="0.1" width="0.2" height="0.2" fill="orange"/>
        </pattern>
      </defs>
      <rect width="100" height="100" fill="url(#p)"/>
    </svg>'''
    image = render(svg)
    r, g, b, a = pixel(image, 20, 20)
    assert (r, g, b) == (255, 165, 0) and a == 255
    assert TRANSPARENT(*pixel(image, 5, 5))


def test_non_invertible_pattern_transform_disables_paint():
    """A degenerate patternTransform must not crash and paints nothing."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="60" height="60">
      <defs>
        <pattern id="p" width="10" height="10"
                 patternUnits="userSpaceOnUse"
                 patternTransform="matrix(0,0,0,0,0,0)">
          <rect width="10" height="10" fill="blue"/>
        </pattern>
      </defs>
      <rect width="60" height="60" fill="url(#p)"/>
    </svg>'''
    image = render(svg)
    assert TRANSPARENT(*pixel(image, 30, 30))


def test_negative_sized_pattern_disables_paint_without_crashing():
    """A negative tile width paints nothing instead of raising MemoryError."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">
      <defs>
        <pattern id="p" width="-5" height="10"
                 patternUnits="userSpaceOnUse">
          <rect width="10" height="10" fill="blue"/>
        </pattern>
      </defs>
      <rect width="40" height="40" fill="url(#p)"/>
    </svg>'''
    image = render(svg)  # must not raise
    assert TRANSPARENT(*pixel(image, 5, 5))


def test_reuse_result_is_independent_of_reference_order():
    """Swapping two references must not change what each outline renders."""
    def build(first_opacity, second_opacity):
        svg = (
            f'''<svg xmlns="http://www.w3.org/2000/svg" width="120" '''
            f'''height="60">
          <defs>
            <pattern id="p" width="10" height="10"
                     patternUnits="userSpaceOnUse">
              <rect width="10" height="10" fill="blue"/>
            </pattern>
          </defs>
          <rect x="0" y="0" width="60" height="60" fill="url(#p)"
                fill-opacity="{first_opacity}"/>
          <rect x="60" y="0" width="60" height="60" fill="url(#p)"
                fill-opacity="{second_opacity}"/>
        </svg>''')
        return svg.encode()

    ab = render(build(0.25, 1))
    ba = render(build(1, 0.25))
    # The fully opaque half must be opaque regardless of draw order.
    assert pixel(ab, 90, 30)[3] == 255
    assert pixel(ba, 30, 30)[3] == 255
    # The quarter-opacity half must keep its own alpha in both orders.
    assert pixel(ab, 30, 30)[3] == 64
    assert pixel(ba, 90, 30)[3] == 64


def test_pattern_inherited_via_href():
    """A pattern referencing another pattern inherits its tiles."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg"
         xmlns:xlink="http://www.w3.org/1999/xlink" width="60" height="60">
      <defs>
        <pattern id="base" width="10" height="10"
                 patternUnits="userSpaceOnUse">
          <rect width="10" height="10" fill="teal"/>
        </pattern>
        <pattern id="derived" href="#base"/>
      </defs>
      <rect width="60" height="60" fill="url(#derived)"/>
    </svg>'''
    image = render(svg)
    assert pixel(image, 5, 5)[:3] == (0, 128, 128)


def test_pattern_can_paint_a_stroke():
    """Patterns work as a stroke source, not only as fills."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="60">
      <defs>
        <pattern id="p" width="8" height="8" patternUnits="userSpaceOnUse">
          <rect width="8" height="8" fill="orange"/>
        </pattern>
      </defs>
      <path d="M10 30 L90 30" stroke="url(#p)" stroke-width="10"
            fill="none"/>
    </svg>'''
    image = render(svg)
    assert pixel(image, 50, 30)[:3] == (255, 165, 0)
    assert TRANSPARENT(*pixel(image, 5, 5))


def test_pattern_content_can_use_a_gradient():
    """Paint servers nested inside pattern cells render correctly."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="60" height="60">
      <defs>
        <linearGradient id="g">
          <stop offset="0" stop-color="red"/>
          <stop offset="1" stop-color="yellow"/>
        </linearGradient>
        <pattern id="p" width="15" height="15"
                 patternUnits="userSpaceOnUse">
          <rect width="15" height="15" fill="url(#g)"/>
        </pattern>
      </defs>
      <rect width="60" height="60" fill="url(#p)"/>
    </svg>'''
    image = render(svg)
    assert pixel(image, 1, 7)[0] > 200          # red end
    assert pixel(image, 14, 7)[:2] == (255, 246)  # yellow end
