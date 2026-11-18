"""
Pixel-level regression tests for ``viewBox`` / ``preserveAspectRatio``
handling, at the root and inside nested ``svg`` containers.

The shared fixture is a 40x30 icon with four colored corner quadrants and a
white center cross. Probe points are chosen with analytic geometry and never
sit on an antialiased edge, so exact RGB comparisons are deterministic across
Cairo versions.

Colors:
    white  = (255, 255, 255)
    gray   = (128, 128, 128)  (container background)
    red    = (255, 0, 0)      top-left quadrant
    lime   = (0, 255, 0)      top-right quadrant
    blue   = (0, 0, 255)      bottom-left quadrant
    yellow = (255, 255, 0)    bottom-right quadrant

"""

import io

import pytest

from . import svg2png
from .helpers import (
    parse_preserve_aspect_ratio, parse_viewbox, preserve_ratio)

WHITE = (255, 255, 255)
GRAY = (128, 128, 128)
RED = (255, 0, 0)
LIME = (0, 255, 0)
BLUE = (0, 0, 255)
YELLOW = (255, 255, 0)

# The four corner quadrants and the center cross, in user units (viewBox 40x30).
# The cross is 4 user units thick so it keeps pure-white interior pixels even
# at the smallest scale factor used in these tests (0.75 -> 3 device pixels).
ICON_BODY = '''
  <rect x="0"  y="0"  width="20" height="15" fill="red"/>
  <rect x="20" y="0"  width="20" height="15" fill="lime"/>
  <rect x="0"  y="15" width="20" height="15" fill="blue"/>
  <rect x="20" y="15" width="20" height="15" fill="yellow"/>
  <rect x="18" y="0" width="4" height="30" fill="white"/>
  <rect x="0" y="13" width="40" height="4" fill="white"/>'''


def icon_svg(width, height, viewbox, par=None, nested=True,
             x=0, y=0, background=True):
    """Build an SVG with the corner icon, either at the root or nested."""
    par_attr = f'preserveAspectRatio="{par}"' if par else ''
    bg = (f'<rect width="{width}" height="{height}" fill="gray"/>'
          if background else '')
    if nested:
        return f'''<svg xmlns="http://www.w3.org/2000/svg"
            width="{width}" height="{height}">{bg}
          <svg x="{x}" y="{y}" width="{width - x}" height="{height - y}"
               viewBox="{viewbox}" {par_attr}>{ICON_BODY}</svg></svg>'''
    return f'''<svg xmlns="http://www.w3.org/2000/svg"
        width="{width}" height="{height}" viewBox="{viewbox}"
        {par_attr}>{bg}{ICON_BODY}</svg>'''


def pixels(svg_source):
    """Render and return ``(image, accessor)``."""
    from PIL import Image
    png = svg2png(
        svg_source.encode() if isinstance(svg_source, str) else svg_source)
    image = Image.open(io.BytesIO(png)).convert('RGB')
    return image, image.load()


def assert_pixels(accessor, expected):
    for (x, y), color in expected.items():
        assert accessor[x, y] == color, (
            f'pixel ({x}, {y}) = {accessor[x, y]}, expected {color}')


# ---------------------------------------------------------------------------
# meet / slice / none across portrait and landscape containers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('width, height', (
    (30, 60),   # portrait container, icon is landscape (40x30)
    (60, 30),   # landscape container
    (40, 40),   # squarish container
))
def test_meet_letterboxes_and_keeps_corners(width, height):
    """meet scales uniformly to fit and centers; corners outside the content
    box show the container background, content corners keep their colors."""
    svg = icon_svg(width, height, '0 0 40 30', 'xMidYMid meet')
    image, px = pixels(svg)
    assert image.size == (width, height)
    scale = min(width / 40, height / 30)
    content_w, content_h = 40 * scale, 30 * scale
    off_x, off_y = (width - content_w) / 2, (height - content_h) / 2

    # Interior points of each colored quadrant (2 user units inside a corner).
    def user(ux, uy):
        return int(off_x + ux * scale), int(off_y + uy * scale)

    expected = {
        user(2, 2): RED,
        user(38, 2): LIME,
        user(2, 28): BLUE,
        user(38, 28): YELLOW,
    }
    # Horizontal cross bar: user y 13..17 spans the full viewBox width. Find
    # its rendered row range (with one pixel of antialiasing margin) and check
    # a white pixel inside it, robust against fractional scale factors.
    cross_y0 = max(0, int(off_y + 13 * scale) - 1)
    cross_y1 = min(height - 1, int(off_y + 17 * scale) + 1)
    cross_x = int(off_x + 5 * scale)
    cross_rows = {px[cross_x, y] for y in range(cross_y0, cross_y1 + 1)}
    assert WHITE in cross_rows
    # A point just outside the centered content box is the background.
    if off_x > 1:
        expected[(1, height // 2)] = GRAY
        expected[(width - 2, height // 2)] = GRAY
    if off_y > 1:
        expected[(width // 2, 1)] = GRAY
        expected[(width // 2, height - 2)] = GRAY
    assert_pixels(px, expected)


@pytest.mark.parametrize('width, height', (
    (30, 60),
    (60, 30),
    (40, 40),
))
def test_meet_letterbox_edges_are_background(width, height):
    """Explicit check that meet leaves background-colored letterbox bands."""
    svg = icon_svg(width, height, '0 0 40 30', 'xMidYMid meet')
    _, px = pixels(svg)
    scale = min(width / 40, height / 30)
    content_w, content_h = 40 * scale, 30 * scale
    off_x, off_y = (width - content_w) / 2, (height - content_h) / 2
    if off_y > 1:
        # Vertical letterbox: top and bottom bands are background.
        assert px[width // 2, 1] == GRAY
        assert px[width // 2, height - 2] == GRAY
    if off_x > 1:
        # Horizontal letterbox: left and right bands are background.
        assert px[1, height // 2] == GRAY
        assert px[width - 2, height // 2] == GRAY


@pytest.mark.parametrize('width, height', (
    (30, 60),
    (60, 30),
    (40, 40),
))
def test_slice_fills_viewport_and_clips_overflow(width, height):
    """slice scales uniformly to cover the whole viewport; the viewport is
    entirely painted and the overflowing corners are clipped at the edges."""
    svg = icon_svg(width, height, '0 0 40 30', 'xMidYMid slice')
    image, px = pixels(svg)
    assert image.size == (width, height)
    scale = max(width / 40, height / 30)
    content_w, content_h = 40 * scale, 30 * scale
    off_x, off_y = (width - content_w) / 2, (height - content_h) / 2

    def user(ux, uy):
        return int(off_x + ux * scale), int(off_y + uy * scale)

    # The viewport corners must be fully covered (no letterbox) by whichever
    # quadrant maps there.
    corner_points = (
        (1, 1), (width - 2, 1), (1, height - 2), (width - 2, height - 2))
    for point in corner_points:
        assert px[point] != GRAY
    # Colored quadrants that remain inside the viewport keep their color.
    expected = {}
    for ux, uy, color in (
            (2, 2, RED), (38, 2, LIME), (2, 28, BLUE), (38, 28, YELLOW)):
        point = user(ux, uy)
        if 0 <= point[0] < width and 0 <= point[1] < height:
            expected[point] = color
    # Horizontal cross bar, probed away from the vertical bar.
    expected[user(5, 15)] = WHITE
    assert_pixels(px, expected)


@pytest.mark.parametrize('width, height', (
    (30, 60),
    (60, 30),
    (40, 40),
))
def test_none_stretches_independently_to_fill(width, height):
    """none scales x and y independently; the four corner quadrants are
    stretched onto the four viewport corners and the cross stays centered."""
    svg = icon_svg(width, height, '0 0 40 30', 'none')
    image, px = pixels(svg)
    assert image.size == (width, height)
    assert_pixels(px, {
        (1, 1): RED,
        (width - 2, 1): LIME,
        (1, height - 2): BLUE,
        (width - 2, height - 2): YELLOW,
        # Horizontal center cross bar spans the full stretched width.
        (width // 4, height // 2): WHITE,
    })


def test_meet_slice_none_portrait_and_landscape_together():
    """One document exercising all three modes in portrait and landscape
    nested containers; each viewport keeps its own geometry."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="150" height="90">
      <rect width="150" height="90" fill="gray"/>
      <svg x="5" y="15" width="30" height="60" viewBox="0 0 40 30"
           preserveAspectRatio="xMidYMid meet">{ICON_BODY}</svg>
      <svg x="45" y="15" width="30" height="60" viewBox="0 0 40 30"
           preserveAspectRatio="xMidYMid slice">{ICON_BODY}</svg>
      <svg x="85" y="30" width="60" height="30" viewBox="0 0 40 30"
           preserveAspectRatio="xMidYMid meet">{ICON_BODY}</svg>
      <svg x="85" y="100" width="0" height="0" viewBox="0 0 40 30"/>
    </svg>'''
    _, px = pixels(svg)
    # Portrait meet (at x 5..35, y 15..75): meet content 30x22.5 centered,
    # i.e. y 33.75..56.25. Probe x=10 (left half, clear of the center cross).
    assert px[10, 35] == RED             # top quadrant, interior
    assert px[10, 55] == BLUE            # bottom quadrant, interior
    assert px[10, 20] == GRAY            # letterbox above the content
    assert px[10, 58] == GRAY            # letterbox below the content
    # Portrait slice (at x 45..75): fills the 30x60 viewport, clipped at edges.
    assert px[46, 16] == RED
    assert px[74, 74] == YELLOW
    assert px[50, 45] == WHITE
    assert px[44, 45] == GRAY            # outside the viewport, left edge
    # Landscape meet (at x 85..145, y 30..60): content 40x30, x 95..135.
    assert px[96, 31] == RED
    assert px[134, 58] == YELLOW
    assert px[86, 45] == GRAY            # letterbox left of the content
    assert px[144, 45] == GRAY           # letterbox right


# ---------------------------------------------------------------------------
# Alignment variants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('par, top_color, low_color', (
    # Portrait 30x60: meet content is 30 wide, 22.5 tall. x=5 stays in the
    # left red/blue half, clear of the white center cross.
    # xMinYMin: content y 0..22.5 (flush top); xMaxYMax: content y 37.5..60.
    ('xMinYMin meet', RED, GRAY),       # top painted, lower area is letterbox
    ('xMaxYMax meet', GRAY, BLUE),      # top letterbox, bottom painted
))
def test_meet_alignment_variants(par, top_color, low_color):
    svg = icon_svg(30, 60, '0 0 40 30', par)
    _, px = pixels(svg)
    assert px[5, 3] == top_color        # near the top edge
    assert px[5, 53] == low_color       # near the bottom edge


def test_slice_alignment_variants():
    """YMin and YMax slice keep different parts of the overflowing content:
    YMin keeps the bottom of the content visible, YMax the top."""
    # Landscape 60x30, slice scale 1.5: content is 60 wide, 45 tall. Probing
    # x=5 (left half, clear of the center cross), the visible red/blue split
    # moves with the vertical alignment.
    px_min = pixels(
        icon_svg(60, 30, '0 0 40 30', 'xMinYMin slice'))[1]
    px_max = pixels(
        icon_svg(60, 30, '0 0 40 30', 'xMinYMax slice'))[1]
    # YMin: content shifted up, the bottom blue quadrant occupies y 26..29.
    assert px_min[5, 28] == BLUE
    assert px_min[5, 2] == RED
    # YMax: content shifted down, the top red quadrant is only y 0..3.
    assert px_max[5, 2] == RED
    assert px_max[5, 28] == BLUE
    # The red/blue boundary is 15 device pixels lower under YMin.
    def first_blue_row(px):
        for y in range(30):
            if px[5, y] == BLUE:
                return y
        raise AssertionError('blue quadrant not found')
    assert first_blue_row(px_min) - first_blue_row(px_max) == 15
    # Both fill the viewport horizontally: no gray letterbox anywhere.
    assert all(px_min[x, 15] != GRAY for x in range(0, 60, 6))
    assert all(px_max[x, 15] != GRAY for x in range(0, 60, 6))


# ---------------------------------------------------------------------------
# Non-zero viewBox origin
# ---------------------------------------------------------------------------

def test_nonzero_viewbox_origin_meet():
    """A viewBox min-x/min-y offset maps through the same transform; the
    corner at the viewBox origin lands on the content box origin."""
    # Viewport at (10, 10), 40x60; viewBox -5 -5 40 30. meet scale 4/3:
    # content 53.33x40, horizontally centered (offset 3.33), vertically flush.
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <rect width="100" height="100" fill="gray"/>
      <svg x="10" y="10" width="40" height="60" viewBox="-5 -5 40 30"
           preserveAspectRatio="xMidYMid meet">
        <rect x="-5" y="-5" width="20" height="15" fill="red"/>
        <rect x="15" y="-5" width="20" height="15" fill="lime"/>
        <rect x="-5" y="10" width="20" height="15" fill="blue"/>
        <rect x="15" y="10" width="20" height="15" fill="yellow"/>
      </svg>
    </svg>'''
    _, px = pixels(svg)
    # meet scale 4/3: content box is 40 wide, 30 tall, vertically centered in
    # the 60-tall viewport, so it maps to device x 10..50, y 25..55.
    # viewBox origin (-5,-5) therefore lands on device (10, 25).
    assert px[12, 27] == RED             # top-left quadrant, interior
    assert px[48, 27] == LIME            # top-right quadrant
    assert px[12, 53] == BLUE            # bottom-left quadrant
    assert px[48, 53] == YELLOW
    assert px[30, 15] == GRAY            # above the viewport
    assert px[9, 40] == GRAY             # left of the viewport
    assert px[51, 40] == GRAY            # right of the viewport (clipped)
    assert px[30, 56] == GRAY            # below the content, inside viewport


def test_nonzero_viewbox_origin_slice_clips_at_viewport():
    """With a non-zero origin, slice overflow is still clipped at the
    viewport rectangle and never paints the surrounding container."""
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">
      <rect width="120" height="120" fill="gray"/>
      <svg x="20" y="20" width="60" height="40" viewBox="100 200 40 30"
           preserveAspectRatio="xMidYMid slice">
        <rect x="100" y="200" width="40" height="30" fill="red"/>
      </svg>
      <rect x="80" y="20" width="40" height="40" fill="blue"/>
    </svg>'''
    _, px = pixels(svg)
    assert px[21, 21] == RED                # inside the slice viewport
    assert px[79, 58] == RED                # far interior corner
    assert px[19, 40] == GRAY               # clipped left of the viewport
    assert px[81, 40] == BLUE               # sibling layer untouched
    assert px[100, 40] == BLUE


# ---------------------------------------------------------------------------
# Clipping isolation and state cleanup
# ---------------------------------------------------------------------------

def test_slice_overflow_does_not_cover_sibling():
    """Content larger than the viewport under slice is clipped to the
    effective viewport, so a following sibling layer stays intact."""
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60">
      <rect width="120" height="60" fill="white"/>
      <svg x="0" y="0" width="80" height="60" viewBox="0 0 40 40"
           preserveAspectRatio="xMidYMid slice">
        <rect x="-50" y="-50" width="140" height="140" fill="red"/>
      </svg>
      <rect x="80" y="0" width="40" height="60" fill="blue"/>
    </svg>'''
    _, px = pixels(svg)
    assert px[40, 30] == RED
    assert px[79, 30] == RED
    assert px[80, 30] == BLUE               # boundary: sibling not overwritten
    assert px[100, 30] == BLUE


def test_none_does_not_affect_following_image():
    """Independent scaling on one nested svg must not leak into the next
    nested svg, which keeps its default meet behavior."""
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60">
      <rect width="120" height="60" fill="white"/>
      <svg x="0" y="0" width="60" height="60" viewBox="0 0 40 20"
           preserveAspectRatio="none">
        <rect width="40" height="20" fill="red"/>
      </svg>
      <svg x="60" y="0" width="60" height="60" viewBox="0 0 40 20">
        <rect width="40" height="20" fill="blue"/>
      </svg>
    </svg>'''
    _, px = pixels(svg)
    assert px[30, 30] == RED                # first: stretched fill
    # Second: meet scale 1.5 -> content 60x30 centered, letterbox top/bottom.
    assert px[90, 5] == WHITE
    assert px[90, 20] == BLUE
    assert px[90, 54] == WHITE


def test_repeated_calls_are_independent_and_deterministic():
    svg = icon_svg(40, 40, '0 0 40 30', 'none')
    outputs = [svg2png(svg.encode()) for _ in range(4)]
    assert len({out for out in outputs}) == 1
    # A different document rendered between two identical ones cannot change
    # their result.
    other = icon_svg(60, 30, '0 0 40 30', 'xMidYMid slice')
    svg2png(other.encode())
    assert svg2png(svg.encode()) == outputs[0]


def test_nested_viewport_state_restored_after_error():
    """A malformed viewBox raises, the cairo state is cleaned up, and a later
    independent conversion still renders correctly."""
    bad = '''<svg xmlns="http://www.w3.org/2000/svg" width="60" height="60">
      <g><g>
        <svg x="0" y="0" width="50" height="50" viewBox="1 2 3">
          <rect width="10" height="10" fill="red"/>
        </svg>
      </g></g>
    </svg>'''
    with pytest.raises(ValueError):
        svg2png(bad.encode())
    good = icon_svg(40, 40, '0 0 40 30', 'none')
    _, px = pixels(good)
    assert px[1, 1] == RED and px[38, 38] == YELLOW


def test_draw_state_save_restore_balanced_on_error():
    """Every context.save() performed while descending into a failing nested
    viewport must have a matching restore(), so the cairo drawing state does
    not leak into later drawing on the same surface."""
    import cairocffi as cairo

    from .parser import Tree
    from .surface import PNGSurface

    bad = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="60" height="60">'
        b'<g><g><svg x="0" y="0" width="50" height="50" viewBox="1 2 3">'
        b'<rect width="10" height="10" fill="red"/></svg></g></g></svg>')
    tree = Tree(bytestring=bad)
    surf = PNGSurface.__new__(PNGSurface)
    surf.cairo = cairo.ImageSurface(cairo.FORMAT_ARGB32, 60, 60)
    surf.context = cairo.Context(surf.cairo)

    saves, restores = [], []
    real_save, real_restore = surf.context.save, surf.context.restore
    surf.context.save = lambda: (saves.append(1), real_save())
    surf.context.restore = lambda: (restores.append(1), real_restore())

    # Minimum state Surface.draw() touches.
    surf.parent_node = None
    surf.font_size = 12
    surf.context_width = surf.context_height = 60
    surf.markers = surf.gradients = surf.patterns = surf.masks = {}
    surf.paths = surf.filters = surf.images = {}
    surf.stroke_and_fill = True
    surf.text_path_width = 0
    surf.cursor_position = surf.cursor_d_position = [0, 0]
    surf.map_rgba = surf.map_image = None
    surf.dpi = 96
    surf.reference_count = 0
    surf.tree_cache = {}

    with pytest.raises(ValueError):
        surf.draw(tree)
    assert len(saves) == len(restores) and len(saves) > 0


# ---------------------------------------------------------------------------
# Degenerate / malformed viewBox error contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('viewbox', (
    '0 0 0 0', '0 0 100 0', '0 0 0 100',
))
def test_degenerate_viewbox_does_not_crash(viewbox):
    """Degenerate viewBoxes disable the viewBox mapping (identity scale)
    instead of raising ZeroDivisionError or producing zero-scale output."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="50" height="50"
        viewBox="{viewbox}"><rect width="10" height="10" fill="red"/></svg>'''
    image, px = pixels(svg)
    assert image.size == (50, 50)
    assert px[2, 2] == RED
    # Nested degenerate viewBox behaves the same.
    nested = f'''<svg xmlns="http://www.w3.org/2000/svg" width="50" height="50">
      <svg x="5" y="5" width="40" height="40" viewBox="{viewbox}">
        <rect width="10" height="10" fill="red"/>
      </svg></svg>'''
    image, px = pixels(nested)
    assert image.size == (50, 50)
    assert px[7, 7] == RED


@pytest.mark.parametrize('viewbox', ('1 2 3', 'a b c d', '1 2 3 4 5'))
def test_malformed_viewbox_raises_value_error(viewbox):
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="50" height="50"
        viewBox="{viewbox}"><rect width="10" height="10" fill="red"/></svg>'''
    with pytest.raises(ValueError):
        svg2png(svg.encode())


def test_zero_sized_surface_keeps_historical_contract():
    with pytest.raises(ValueError, match='SVG size is undefined'):
        svg2png(b'<svg xmlns="http://www.w3.org/2000/svg" '
                b'width="0" height="0"/>')


# ---------------------------------------------------------------------------
# preserveAspectRatio parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value, expected', (
    (None, ('xMidYMid', 'meet')),
    ('', ('xMidYMid', 'meet')),
    ('xMidYMid', ('xMidYMid', 'meet')),
    ('xMidYMid meet', ('xMidYMid', 'meet')),
    ('xMinYMax slice', ('xMinYMax', 'slice')),
    ('none', ('none', 'meet')),
    ('defer xMidYMid meet', ('xMidYMid', 'meet')),
    ('garbage', ('xMidYMid', 'meet')),
    ('xMidYMid bogus', ('xMidYMid', 'meet')),
    ('xmidymid meet', ('xMidYMid', 'meet')),
))
def test_parse_preserve_aspect_ratio(value, expected):
    assert parse_preserve_aspect_ratio(value) == expected


@pytest.mark.parametrize('value, expected', (
    (None, None),
    ('', None),
    ('0 0 40 30', (0.0, 0.0, 40.0, 30.0)),
    ('-5 -5.5 10 20', (-5.0, -5.5, 10.0, 20.0)),
    ('0,0,40,30', (0.0, 0.0, 40.0, 30.0)),
    ('0 0 0 0', None),
    ('0 0 40 0', None),
    ('0 0 0 30', None),
))
def test_parse_viewbox(value, expected):
    assert parse_viewbox(value) == expected


@pytest.mark.parametrize('value', ('1 2 3', 'a b c d', '1 2 3 4 5'))
def test_parse_viewbox_malformed(value):
    with pytest.raises(ValueError):
        parse_viewbox(value)


def test_empty_preserve_aspect_ratio_attribute_renders():
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"
        viewBox="0 0 40 40" preserveAspectRatio="">
      <rect width="40" height="40" fill="lime"/></svg>'''
    _, px = pixels(svg)
    assert px[5, 5] == LIME


# ---------------------------------------------------------------------------
# Output scaling participates in the mapping
# ---------------------------------------------------------------------------

def test_scale_and_output_width_preserve_mapping():
    svg = icon_svg(50, 50, '0 0 40 30', 'xMidYMid meet')
    scaled = svg2png(svg.encode(), scale=2)
    resized = svg2png(
        svg.encode(), output_width=100, output_height=100)
    from PIL import Image
    img_scale = Image.open(io.BytesIO(scaled)).convert('RGB')
    img_resize = Image.open(io.BytesIO(resized)).convert('RGB')
    assert img_scale.size == img_resize.size == (100, 100)
    # 50x50 meet of a 40x30 viewBox: scale 1.25, content 50x37.5 centered at
    # y 6.25; at scale 2 the content is y 12.5..87.5, letterboxed top/bottom.
    for image in (img_scale, img_resize):
        px = image.load()
        assert px[50, 5] == GRAY             # top letterbox
        assert px[50, 95] == GRAY            # bottom letterbox
        assert px[50, 50] == WHITE           # horizontal center cross bar
        # Top-half quadrants (y=30 is above the cross bar, inside the content).
        assert px[10, 30] == RED
        assert px[90, 30] == LIME


def test_preserve_ratio_helper_returns_finite_scales():
    class FakeNode(dict):
        tag = 'svg'

    node = FakeNode()
    node.image_width = 40
    node.image_height = 30
    # No surface attributes are needed for the svg branch with explicit size.
    scale_x, scale_y, tx, ty = preserve_ratio(
        None, node, width=60, height=30)
    assert scale_x == scale_y == 1.0
    assert tx == 10.0 and ty == 0.0


# ---------------------------------------------------------------------------
# use / symbol viewports
# ---------------------------------------------------------------------------

SYMBOL = f'''<symbol id="icon" viewBox="0 0 40 30">{ICON_BODY}</symbol>'''


def test_symbol_reused_twice_keeps_independent_geometry():
    """Two uses of one symbol each apply their own viewport; the first cannot
    affect the second."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="130" height="70">
      <defs>{SYMBOL}</defs>
      <rect width="130" height="70" fill="gray"/>
      <use href="#icon" x="5" y="5" width="40" height="60"
           preserveAspectRatio="none"/>
      <use href="#icon" x="55" y="20" width="70" height="30"/>
    </svg>'''
    _, px = pixels(svg)
    # First use, none: stretched onto the whole 40x60 box at (5,5).
    assert px[6, 6] == RED
    assert px[44, 64] == YELLOW
    # Second use, default meet in 70x30 at (55,20): scale 1, content 40x30
    # centered horizontally -> device x 70..110, y 20..50.
    assert px[71, 21] == RED
    assert px[108, 48] == YELLOW
    assert px[56, 35] == GRAY              # letterbox left of second content
    assert px[111, 35] == GRAY             # letterbox right


def test_use_single_dimension_still_applies():
    """A width-only or height-only use keeps the given dimension and resolves
    the other through the viewport defaults."""
    svg_w = f'''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs>{SYMBOL}</defs>
      <rect width="100" height="100" fill="gray"/>
      <use href="#icon" x="0" y="0" width="40"/>
    </svg>'''
    image_w, px_w = pixels(svg_w)
    assert image_w.size == (100, 100)
    # Width 40; missing height defaults to 100% of the parent (100). meet of
    # 40x30 in 40x100 -> scale 1, content 40x30 centered vertically.
    assert px_w[2, 36] == RED
    assert px_w[38, 64] == YELLOW
    assert px_w[20, 5] == GRAY             # letterbox above the content
    assert px_w[20, 95] == GRAY

    svg_h = f'''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs>{SYMBOL}</defs>
      <rect width="100" height="100" fill="gray"/>
      <use href="#icon" x="0" y="0" height="30"/>
    </svg>'''
    _, px_h = pixels(svg_h)
    # Height 30; missing width defaults to 100 (100% of parent). meet of
    # 40x30 in 100x30 -> scale 1, content 40x30 centered horizontally.
    assert px_h[31, 2] == RED
    assert px_h[68, 28] == YELLOW
    assert px_h[5, 15] == GRAY             # letterbox left of the content
    assert px_h[95, 15] == GRAY


def test_use_preserve_aspect_ratio_does_not_leak_between_references():
    """Different preserveAspectRatio values on sibling uses stay isolated."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="150" height="60">
      <defs>{SYMBOL}</defs>
      <rect width="150" height="60" fill="gray"/>
      <use href="#icon" x="5" y="5" width="50" height="50"
           preserveAspectRatio="none"/>
      <use href="#icon" x="60" y="5" width="40" height="50"
           preserveAspectRatio="xMidYMid meet"/>
      <use href="#icon" x="105" y="5" width="40" height="50"
           preserveAspectRatio="xMidYMid slice"/>
    </svg>'''
    _, px = pixels(svg)
    # none: fills its whole 50x50 box at (5,5).
    assert px[6, 6] == RED and px[54, 54] == YELLOW
    # meet in 40x50 at (60,5): scale 1, content 40x30 centered vertically ->
    # device y 15..45, with gray letterbox above and below.
    assert px[65, 16] == RED
    assert px[95, 44] == YELLOW
    assert px[80, 8] == GRAY and px[80, 52] == GRAY
    # slice in 40x50 at (105,5): covers the full viewport, y 5..55.
    assert px[110, 6] == RED and px[140, 54] == YELLOW
    assert px[106, 30] == WHITE            # center cross visible inside
