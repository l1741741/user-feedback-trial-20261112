"""
Tests for ``viewBox`` / ``preserveAspectRatio`` mapping.

The same fixture icon -- four colored corner squares and a centered cross
-- is rendered into viewports of different aspect ratios. Every assertion
is derived from the SVG 1.1 coordinate-system math (meet/slice/none), so
the tests do not only check that something is painted: they verify where
each corner is clipped and where the center of the cross lands in device
pixels.

"""

import io

import pytest

from . import svg2png

# Fixture geometry, in viewBox user units
ICON = '''
  <rect x="0" y="0" width="20" height="20" fill="red"/>
  <rect x="80" y="0" width="20" height="20" fill="lime"/>
  <rect x="0" y="80" width="20" height="20" fill="blue"/>
  <rect x="80" y="80" width="20" height="20" fill="yellow"/>
  <rect x="45" y="10" width="10" height="80" fill="black"/>
  <rect x="10" y="45" width="80" height="10" fill="black"/>
'''

RED = (255, 0, 0, 255)
LIME = (0, 255, 0, 255)
BLUE = (0, 0, 255, 255)
YELLOW = (255, 255, 0, 255)
BLACK = (0, 0, 0, 255)
ORANGE = (255, 165, 0, 255)
WHITE = (255, 255, 255, 255)
TRANSPARENT = (0, 0, 0, 0)


def make_svg(width, height, viewbox, par, body=ICON, overflow=None):
    overflow_attr = f' overflow="{overflow}"' if overflow else ''
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" viewBox="{viewbox}" '
        f'preserveAspectRatio="{par}"{overflow_attr}>{body}</svg>')


def render(svg, **kwargs):
    import cairocffi as cairo
    png = svg2png(bytestring=svg.encode(), **kwargs)
    return cairo.ImageSurface.create_from_png(io.BytesIO(png))


def color_at(surface, x, y):
    """Premultiplied ARGB32 -> RGBA tuple, independent of native endianness."""
    data = bytes(surface.get_data())
    stride = surface.get_stride()
    offset = y * stride + x * 4
    b, g, r, a = data[offset:offset + 4]
    if a == 0:
        return TRANSPARENT
    # Cairo stores premultiplied values
    return (
        round(r * 255 / a), round(g * 255 / a),
        round(b * 255 / a), a)


def color_bbox(surface, color):
    """Bounding box ``(xmin, ymin, xmax, ymax)`` of exact ``color`` pixels."""
    width, height = surface.get_width(), surface.get_height()
    xs, ys = [], []
    for y in range(height):
        for x in range(width):
            if color_at(surface, x, y) == color:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def assert_color_absent(surface, color):
    assert color_bbox(surface, color) is None


def assert_bbox(surface, color, expected):
    bbox = color_bbox(surface, color)
    assert bbox is not None, f'{color} not found'
    assert bbox == expected, f'{color}: got {bbox}, expected {expected}'


# ---------------------------------------------------------------------------
# Reference mapping, mirroring the SVG spec formulas used by the renderer
# ---------------------------------------------------------------------------

def expected_mapping(viewport_w, viewport_h, viewbox, par):
    """Return the device rectangle occupied by the viewBox content."""
    vb_x, vb_y, vb_w, vb_h = viewbox
    sx = viewport_w / vb_w
    sy = viewport_h / vb_h
    align, _, meet_or_slice = par.partition(' ')
    if align == 'none':
        return sx, sy, vb_x, vb_y
    scale = max(sx, sy) if meet_or_slice == 'slice' else min(sx, sy)
    extra_x = viewport_w / scale - vb_w
    extra_y = viewport_h / scale - vb_h
    x_pos, y_pos = align[1:4].lower(), align[5:].lower()
    tx = vb_x + (extra_x / 2 if x_pos == 'mid'
                 else extra_x if x_pos == 'max' else 0)
    ty = vb_y + (extra_y / 2 if y_pos == 'mid'
                 else extra_y if y_pos == 'max' else 0)
    return scale, scale, tx, ty


def device_rect(viewport, viewbox, par, local_rect):
    """Map a rectangle in viewBox coordinates to device pixel coordinates."""
    w, h = viewport
    sx, sy, tx, ty = expected_mapping(w, h, viewbox, par)
    x, y, rw, rh = local_rect
    return (round((tx + x) * sx),
            round((ty + y) * sy),
            round((tx + x + rw) * sx) - 1,
            round((ty + y + rh) * sy) - 1)


CORNER_RECTS = {
    RED: (0, 0, 20, 20),
    LIME: (80, 0, 20, 20),
    BLUE: (0, 80, 20, 20),
    YELLOW: (80, 80, 20, 20),
}

# (viewport, viewBox, align) matrix: landscape, square, portrait containers
VIEWPORTS = [(200, 100), (100, 100), (100, 200)]
ALIGNS = [
    'xMinYMin meet', 'xMidYMid meet', 'xMaxYMax meet',
    'xMinYMin slice', 'xMidYMid slice', 'xMaxYMax slice',
    'none',
]
VIEWBOXES = [(0, 0, 100, 100), (10, 20, 100, 100), (-15, -25, 100, 100)]


@pytest.mark.parametrize('viewbox', VIEWBOXES)
@pytest.mark.parametrize('par', ALIGNS)
@pytest.mark.parametrize('viewport', VIEWPORTS)
def test_meet_slice_none_mapping(viewport, par, viewbox):
    """Corners and the cross center follow the spec mapping exactly."""
    w, h = viewport
    surface = render(make_svg(w, h, ' '.join(map(str, viewbox)), par))
    assert (surface.get_width(), surface.get_height()) == (w, h)

    sx, sy, tx, ty = expected_mapping(w, h, viewbox, par)
    for corner_color, local_rect in CORNER_RECTS.items():
        bbox = color_bbox(surface, corner_color)
        full_device = device_rect(viewport, viewbox, par, local_rect)
        x0, y0, x1, y1 = full_device
        # Intersect with the effective viewport: hidden parts must be clipped
        clipped = (max(x0, 0), max(y0, 0),
                   min(x1, w - 1), min(y1, h - 1))
        if clipped[0] > clipped[2] or clipped[1] > clipped[3]:
            assert_color_absent(surface, corner_color)
        else:
            assert_bbox(surface, corner_color, clipped)

    # The cross center (viewBox point 50, 50) maps to the viewport center
    center_x = round((tx + 50) * sx)
    center_y = round((ty + 50) * sy)
    if 0 <= center_x < w and 0 <= center_y < h:
        assert color_at(surface, center_x, center_y) == BLACK
    # And the device center is black whenever the cross covers it
    device_center = color_at(surface, w // 2, h // 2)
    cross_local_x = (w / 2 / sx) - tx
    cross_local_y = (h / 2 / sy) - ty
    in_vertical = 45 <= cross_local_x < 55 and 10 <= cross_local_y < 90
    in_horizontal = 10 <= cross_local_x < 90 and 45 <= cross_local_y < 55
    if in_vertical or in_horizontal:
        assert device_center == BLACK
    else:
        assert device_center in (TRANSPARENT, WHITE)


@pytest.mark.parametrize('par', ['xMidYMid meet', 'xMidYMid slice', 'none'])
def test_scale_and_output_dimensions(par):
    """The ``scale`` and output-size parameters participate in the mapping."""
    svg = make_svg(200, 100, '0 0 100 100', par)
    scaled = render(svg, scale=2)
    assert (scaled.get_width(), scaled.get_height()) == (400, 200)
    resized = render(svg, output_width=400, output_height=200)
    assert (resized.get_width(), resized.get_height()) == (400, 200)

    # Same logical geometry, twice as many pixels: allow the one-pixel
    # rounding difference inherent to rasterized rectangle edges.
    small = render(svg)
    for color in CORNER_RECTS:
        small_bbox = color_bbox(small, color)
        big_bbox = color_bbox(scaled, color)
        if small_bbox is None:
            assert big_bbox is None
        else:
            for got, base in zip(big_bbox, small_bbox):
                assert got in (base * 2, base * 2 + 1)
    # output_width/output_height and scale=2 produce the same geometry
    resized_bbox = color_bbox(resized, BLACK)
    scaled_bbox = color_bbox(scaled, BLACK)
    for got, scaled_value in zip(resized_bbox, scaled_bbox):
        assert abs(got - scaled_value) <= 1


def test_none_independent_axes():
    """``none`` stretches x and y independently and keeps both corners."""
    surface = render(make_svg(200, 50, '0 0 100 100', 'none'))
    # sx = 2, sy = 0.5: corners become 40x10 rectangles
    assert_bbox(surface, RED, (0, 0, 39, 9))
    assert_bbox(surface, LIME, (160, 0, 199, 9))
    assert_bbox(surface, BLUE, (0, 40, 39, 49))
    assert_bbox(surface, YELLOW, (160, 40, 199, 49))


def test_slice_is_clipped_to_effective_viewport():
    """Slice overflow never paints the neighboring layer of a nested SVG."""
    nested = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
        '<rect x="0" y="0" width="200" height="100" fill="white"/>'
        '<rect x="0" y="0" width="40" height="100" fill="orange"/>'
        '<rect x="160" y="0" width="40" height="100" fill="orange"/>'
        '<svg x="40" y="0" width="120" height="100" '
        'viewBox="-40 -40 100 100" preserveAspectRatio="xMidYMid slice">'
        + ICON + '</svg></svg>')
    surface = render(nested)

    # The orange sibling strips stay intact: no icon pixel leaks in them
    for x in range(0, 40):
        assert color_at(surface, x, 50) == ORANGE
    for x in range(160, 200):
        assert color_at(surface, x, 50) == ORANGE
    # Nothing escapes the nested viewport horizontally
    for y in range(100):
        assert color_at(surface, 39, y) == ORANGE
        assert color_at(surface, 160, y) == ORANGE


def test_nested_meet_and_slice_with_nonzero_origin():
    """Nested SVG honors its own viewBox origin and alignment."""
    nested = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">'
        '<rect x="0" y="0" width="120" height="120" fill="white"/>'
        '<svg x="10" y="10" width="100" height="60" '
        'viewBox="10 20 100 100" preserveAspectRatio="xMinYMin meet">'
        + ICON + '</svg></svg>')
    surface = render(nested)
    # scale = 0.6, tx = 10, ty = 20; red corner (0,0) -> inner (6, 12),
    # then shifted by the nested viewport origin (10, 10) -> (16, 22)
    expected = device_rect(
        (100, 60), (10, 20, 100, 100), 'xMinYMin meet', (0, 0, 20, 20))
    x0, y0, x1, y1 = expected
    assert_bbox(surface, RED, (x0 + 10, y0 + 10, x1 + 10, y1 + 10))
    # The area below the letterboxed nested viewport keeps the background
    assert color_at(surface, 60, 100) == WHITE


def test_multiple_calls_do_not_leak_state():
    """A slice render in one call cannot change the next document."""
    slice_svg = make_svg(200, 100, '0 0 100 100', 'xMidYMid slice')
    plain_svg = make_svg(100, 100, '0 0 100 100', 'xMidYMid meet')
    # Render the "dangerous" document several times, then a plain one
    for _ in range(3):
        render(slice_svg)
    plain = render(plain_svg)
    assert_bbox(plain, RED, (0, 0, 19, 19))
    assert_bbox(plain, YELLOW, (80, 80, 99, 99))
    assert color_at(plain, 50, 50) == BLACK
    # Two independent renders of the same input are byte-identical
    assert svg2png(bytestring=slice_svg.encode()) == \
        svg2png(bytestring=slice_svg.encode())


def test_degenerate_viewbox_error_contract():
    """Empty/degenerate view boxes raise ValueError and finish the surface."""
    for bad in ('0 0 0 100', '0 0 100 0', '0 0 0 0', '-1 -1 -5 10',
                '1 2 3'):
        with pytest.raises(ValueError):
            svg2png(bytestring=make_svg(100, 100, bad, 'xMidYMid meet')
                    .encode())


def test_degenerate_nested_viewbox_restores_context():
    """An invalid nested viewBox must not corrupt siblings or the surface."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<svg x="0" y="0" width="50" height="50" viewBox="0 0 0 0">'
        '<rect width="10" height="10" fill="red"/></svg>'
        '<rect x="10" y="10" width="80" height="80" fill="lime"/></svg>')
    with pytest.raises(ValueError):
        svg2png(bytestring=svg.encode())
    # The next, valid document still renders correctly (no leaked clip)
    surface = render(make_svg(100, 100, '0 0 100 100', 'xMidYMid meet'))
    assert_bbox(surface, LIME, (80, 0, 99, 19))
    assert color_at(surface, 50, 50) == BLACK


def test_finish_is_idempotent_and_error_path_finalizes():
    """A failed render finalizes the native surface; finish() stays safe."""
    import cairocffi

    from .parser import Tree
    from .surface import PDFSurface, PNGSurface

    for surface_class in (PNGSurface, PDFSurface):
        bad_tree = Tree(
            bytestring=b'<svg xmlns="http://www.w3.org/2000/svg" '
                       b'width="10" height="10" viewBox="0 0 0 0"/>')
        with pytest.raises(ValueError):
            surface_class(bad_tree, io.BytesIO(), 96)

        good_tree = Tree(
            bytestring=b'<svg xmlns="http://www.w3.org/2000/svg" '
                       b'width="10" height="10"/>')
        surface = surface_class(good_tree, io.BytesIO(), 96)
        surface.finish()
        surface.finish()  # must be a no-op, not a Cairo error
        assert surface._finished is True
        assert isinstance(surface.cairo, cairocffi.Surface)


def test_percentage_and_undefined_sizes():
    """Percent-sized nested viewports still resolve and map correctly."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60">'
        '<svg x="0" y="0" width="100%" height="100%" '
        'viewBox="0 0 100 100" preserveAspectRatio="xMidYMid meet">'
        + ICON + '</svg></svg>')
    surface = render(svg)
    assert (surface.get_width(), surface.get_height()) == (120, 60)
    # scale 0.6, centered horizontally: red corner at (30, 0)
    assert_bbox(surface, RED, (30, 0, 41, 11))


def test_overflow_visible_keeps_slice_content():
    """With overflow=visible the slice content is not clipped."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<rect width="100" height="100" fill="white"/>'
        '<svg x="0" y="0" width="100" height="50" viewBox="0 0 100 100" '
        'preserveAspectRatio="xMidYMid slice" overflow="visible">'
        + ICON + '</svg></svg>')
    surface = render(svg)
    # scale = 1, content shifted by -25 vertically; without a clip the
    # bottom corner keeps painting below the nested viewport boundary
    assert color_at(surface, 5, 60) == BLUE
    assert color_at(surface, 90, 60) == YELLOW
    # A hidden (default overflow) nested SVG clips that same content instead
    hidden_svg = svg.replace(' overflow="visible"', '')
    hidden = render(hidden_svg)
    assert color_at(hidden, 5, 60) == WHITE
    assert color_at(hidden, 90, 60) == WHITE
