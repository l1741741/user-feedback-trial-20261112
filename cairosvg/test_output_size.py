"""
Output size and effective viewport tests.

These tests check the contract between the requested output size and the
viewport that is really rendered:

* a raster surface rounds its size to whole pixels, and the root viewport must
  cover that rounded grid exactly (no stray transparent row or column, no
  clipped content);
* physical units (``in``, ``cm``, ``mm``, ``pt``, ``pc``), the ``dpi``,
  ``parent_width``/``parent_height``, ``scale`` and ``viewBox`` must not
  override each other out of place;
* fractional and very small sizes have a defined rounding behaviour, and
  invalid sizes are rejected instead of writing an empty file.

The fixtures below are font-free and use full-bleed colours or sharp corner
marks, so the assertions can look at exact pixel coordinates instead of
antialiased edges wherever possible.

"""

import io

import pytest
from PIL import Image

from . import svg2eps, svg2pdf, svg2png, svg2ps, svg2svg

# A document whose content covers its whole viewport. ``width``/``height`` are
# filled in by ``bleed`` so that each test can pick its own unit.
BLEED = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
    'height="{height}" viewBox="{viewbox}">'
    '<rect x="{x}" y="{y}" width="{rw}" height="{rh}" fill="#ff0000"/>'
    '</svg>')
# Four corner marks and centre lines, used to check that the viewport is
# neither shifted nor clipped when the output size does not match the pixel
# grid of the document.
MARKS = '''\
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
     viewBox="{viewbox}">
  <rect x="{x}" y="{y}" width="2" height="2" fill="#ff0000"/>
  <rect x="{right}" y="{y}" width="2" height="2" fill="#00ff00"/>
  <rect x="{x}" y="{bottom}" width="2" height="2" fill="#0000ff"/>
  <rect x="{right}" y="{bottom}" width="2" height="2" fill="#ffff00"/>
  <rect x="{center}" y="{y}" width="1" height="1" fill="#ff00ff"/>
  <rect x="{x}" y="{center}" width="1" height="1" fill="#00ffff"/>
</svg>'''

BASE = 24


def bleed(width, height, viewbox=None, origin=(0, 0), size=(BASE, BASE)):
    """Return a full-bleed document using ``width`` and ``height``."""
    origin_x, origin_y = origin
    box_width, box_height = size
    if viewbox is None:
        viewbox = f'{origin_x} {origin_y} {box_width} {box_height}'
    return BLEED.format(
        width=width, height=height, viewbox=viewbox,
        x=origin_x, y=origin_y, rw=box_width, rh=box_height).encode()


def marks(size='24', viewbox=None):
    """Return the corner-mark document."""
    viewbox = viewbox or f'0 0 {BASE} {BASE}'
    return MARKS.format(
        width=size, height=size, viewbox=viewbox,
        x=0, y=0, right=BASE - 2, bottom=BASE - 2, center=BASE // 2).encode()


def render(data, **options):
    """Render ``data`` and return ``(png, ((width, height), pixel))``."""
    png = svg2png(data, **options)
    return png, load_png(png)


def load_png(png):
    """Decode a PNG byte string and return ``((width, height), pixel)``."""
    image = Image.open(io.BytesIO(png)).convert('RGBA')
    width, height = image.size
    data = image.load()

    def pixel(x, y):
        """Return the ``(red, green, blue, alpha)`` pixels at ``x``, ``y``."""
        return data[x, y]

    return (width, height), pixel


def assert_edges_opaque(pixel, width, height, color=(255, 0, 0)):
    """Check that every border pixel is painted with ``color``."""
    for x in range(width):
        assert pixel(x, 0) == (*color, 255), f'top edge at x={x}'
        assert pixel(x, height - 1) == (*color, 255), f'bottom edge at x={x}'
    for y in range(height):
        assert pixel(0, y) == (*color, 255), f'left edge at y={y}'
        assert pixel(width - 1, y) == (*color, 255), f'right edge at y={y}'


@pytest.mark.parametrize('unit,width,height', (
    ('24', 24, 24),  # pixels, integral
    ('24.5', 24, 24),  # fractional pixels, rounds down
    ('1cm', 38, 38),  # 37.795… px at 96 dpi, rounds up
    ('10mm', 38, 38),  # same physical size
    ('1mm', 4, 4),  # 3.779… px, the smallest physical unit case
    ('0.3in', 29, 29),  # 28.8 px
    ('18pt', 24, 24),
    ('1pt', 1, 1),  # 1.333… px
    ('1pc', 16, 16),
))
def test_units_and_rounding_cover_the_output(unit, width, height):
    """Each unit keeps its own size and fully covers the rounded surface."""
    png, ((actual_width, actual_height), pixel) = render(bleed(unit, unit))
    assert (actual_width, actual_height) == (width, height)
    assert_edges_opaque(pixel, actual_width, actual_height)
    # The content is a full-bleed square, so the centre is painted too.
    assert pixel(actual_width // 2, actual_height // 2) == (255, 0, 0, 255)


@pytest.mark.parametrize('dpi,size', ((72, 28), (96, 38), (300, 118)))
def test_dpi_scales_physical_units_only(dpi, size):
    """``dpi`` converts physical units, and never pixel units."""
    _, ((width, height), pixel) = render(bleed('1cm', '1cm'), dpi=dpi)
    assert (width, height) == (size, size)
    assert_edges_opaque(pixel, width, height)

    _, ((width, height), pixel) = render(bleed('24', '24'), dpi=dpi)
    assert (width, height) == (24, 24)
    assert_edges_opaque(pixel, width, height)


@pytest.mark.parametrize('width,height', (
    (37.5, 37.5), (25.5, 25.5), (0.6, 0.6), (12.25, 12.25), (96.5, 96.5)))
def test_fractional_output_size_has_no_transparent_sliver(width, height):
    """A fractional requested size is rounded once, and fully painted."""
    _, ((actual_width, actual_height), pixel) = render(
        bleed('24', '24'), output_width=width)
    assert (actual_width, actual_height) == (
        round(width), round(height))
    assert_edges_opaque(pixel, actual_width, actual_height)


@pytest.mark.parametrize('scale', (1.1, 1.37, 1.5, 1.771, 2.25))
def test_fractional_scale_has_no_transparent_sliver(scale):
    """``scale`` rounding uses the same contract as explicit sizes."""
    _, ((width, height), pixel) = render(bleed('24', '24'), scale=scale)
    assert (width, height) == (round(24 * scale), round(24 * scale))
    assert_edges_opaque(pixel, width, height)


def test_single_output_size_keeps_aspect_ratio():
    """Only one of the two sizes is given: the other one is deduced."""
    data = bleed('24', '12', size=(BASE, 12))
    _, ((width, height), pixel) = render(data, output_width=38)
    assert (width, height) == (38, 19)
    assert_edges_opaque(pixel, width, height)

    _, ((width, height), pixel) = render(data, output_height=38)
    assert (width, height) == (76, 38)
    assert_edges_opaque(pixel, width, height)

    # Fractional inputs are rounded once, without losing the ratio first.
    _, ((width, height), pixel) = render(data, output_width=37.5)
    assert (width, height) == (38, 19)
    assert_edges_opaque(pixel, width, height)


def test_both_output_sizes_are_honoured():
    """Both sizes are used as-is, even when they change the aspect ratio."""
    data = bleed('24', '12', size=(BASE, 12))
    _, ((width, height), pixel) = render(
        data, output_width=50, output_height=50)
    assert (width, height) == (50, 50)
    # The default preserveAspectRatio letterboxes the document: the centre
    # band is painted, the top and bottom rows are not. Nothing is clipped on
    # the left or right, and the painted band is flush with both sides.
    painted = [y for y in range(height)
               if pixel(width // 2, y)[3] == 255]
    assert painted[0] > 0 and painted[-1] < height - 1
    assert pixel(0, painted[0])[3] == 255
    assert pixel(width - 1, painted[0])[3] == 255
    # The requested ratio is not silently replaced by the document ratio: the
    # painted band is about half the requested height, not all of it.
    assert 20 <= len(painted) <= 25


def test_nonzero_viewbox_origin_is_not_shifted():
    """A non-zero viewBox origin is applied once, not once per dimension."""
    data = bleed('24', '24', viewbox='10 20 24 24', origin=(10, 20))
    for options in (
            {}, {'output_width': 48}, {'scale': 1.5},
            {'output_width': 37.5}, {'dpi': 300}):
        _, ((width, height), pixel) = render(data, **options)
        assert_edges_opaque(pixel, width, height)


def test_corner_marks_stay_inside_the_viewport():
    """Corner marks show the viewport is neither shifted nor clipped."""
    for options in (
            {}, {'output_width': 50}, {'scale': 1.5},
            {'output_width': 37.5}, {'dpi': 72}):
        _, ((width, height), pixel) = render(marks(), **options)
        assert width == height and width >= BASE
        factor = width / BASE
        # Pixel centres of the 2×2 corner marks, and of the centre lines.
        left, right = int(1 * factor), int(23 * factor - 1)
        top, bottom = int(1 * factor), int(23 * factor - 1)
        assert pixel(left, top)[:3] == (255, 0, 0)
        assert pixel(right, top)[:3] == (0, 255, 0)
        assert pixel(left, bottom)[:3] == (0, 0, 255)
        assert pixel(right, bottom)[:3] == (255, 255, 0)
        assert pixel(width // 2, int(0.5 * factor))[:3] == (255, 0, 255)
        assert pixel(int(0.5 * factor), height // 2)[:3] == (0, 255, 255)


@pytest.mark.parametrize('requested,actual', (
    # The raster surface contract is ``int(round(size))`` with Python's
    # round-half-to-even, applied once to the requested size. Values below
    # half a pixel round to 0 and are rejected as undefined.
    (0.4, None), (0.5, None), (0.500001, 1), (0.6, 1), (1.4, 1),
    (1.5, 2), (2.5, 2), (3.5, 4), (4.5, 4), (12.25, 12), (12.75, 13),
    (37.5, 38), (96.5, 96)))
def test_rounding_contract(requested, actual):
    """The PNG size is ``int(round(requested))``, not an ad-hoc rule."""
    if actual is None:
        with pytest.raises(ValueError, match='The SVG size is undefined'):
            svg2png(bleed('24', '24'), output_width=requested)
        return
    _, ((width, height), pixel) = render(
        bleed('24', '24'), output_width=requested)
    assert (width, height) == (actual, actual)
    assert_edges_opaque(pixel, width, height)


def test_percentages_follow_the_actual_output_size():
    """Percentages use the rounded surface, not the requested size."""
    data = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" '
        f'viewBox="0 0 {BASE} {BASE}">'
        f'<rect width="50%" height="50%" fill="#ff0000"/>'
        '</svg>').encode()
    _, ((width, height), pixel) = render(
        data, output_width=37.5, output_height=37.5,
        parent_width=100, parent_height=100)
    assert (width, height) == (38, 38)
    # Percentages resolve against the 24-user-unit viewport, so the rect is
    # 12 units wide. It is scaled by 37.5 / 24 and then stretched onto the
    # rounded 38 px surface: 12 * 37.5 / 24 * 38 / 37.5 = 19 px exactly.
    # The pixel at x=18 is therefore fully painted and x=19 is not.
    assert pixel(18, 18) == (255, 0, 0, 255)
    assert pixel(19, 19)[3] == 0
    assert pixel(0, 0) == (255, 0, 0, 255)


def test_transparent_area_stays_transparent():
    """Unpainted area keeps alpha 0 and lands at the scaled position."""
    data = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="12" '
        'viewBox="0 0 24 12"><rect width="12" height="6" fill="#ff0000"/>'
        '</svg>').encode()
    _, ((width, height), pixel) = render(data, output_width=37.5)
    assert (width, height) == (38, 19)
    # The painted quarter is opaque and flush with the top-left edges.
    assert pixel(0, 0) == (255, 0, 0, 255)
    assert pixel(17, 8) == (255, 0, 0, 255)
    # The document's own empty half stays transparent: geometry follows the
    # 24×12 user-unit viewport stretched onto the rounded 38×19 surface, so
    # the painted 12×6 units end at pixel (18, 9).
    assert pixel(19, 0)[3] == 0
    assert pixel(0, 10)[3] == 0
    assert pixel(width // 2, height // 2)[3] == 0
    # Both far edges are transparent by design (the document is not
    # full-bleed), and no extra unpainted row or column was appended.
    assert pixel(width - 1, height - 1)[3] == 0
    assert pixel(width - 1, 0)[3] == 0
    assert pixel(0, height - 1)[3] == 0


def test_background_color_covers_the_rounded_surface():
    """A background paints the whole rounded grid, edges included."""
    _, ((width, height), pixel) = render(
        bleed('24', '24'), output_width=37.5, background_color='#0000ff')
    assert (width, height) == (38, 38)
    assert_edges_opaque(pixel, width, height, color=(255, 0, 0))
    # Corners are still red: the document covers the surface entirely.
    assert pixel(width - 1, height - 1) == (255, 0, 0, 255)


def test_repeated_calls_are_identical():
    """Rendering the same document twice gives the same bytes."""
    data = bleed('1cm', '1cm')
    first = svg2png(data, output_width=37.5)
    second = svg2png(data, output_width=37.5)
    assert first == second
    first = svg2png(marks(), scale=1.37)
    second = svg2png(marks(), scale=1.37)
    assert first == second


def test_parent_size_does_not_override_the_document_size():
    """``parent_width``/``parent_height`` only resolve percentages."""
    data = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
        'viewBox="0 0 24 24"><rect width="100%" height="100%" '
        'fill="#ff0000"/></svg>').encode()
    _, ((width, height), pixel) = render(
        data, parent_width=100, parent_height=100)
    assert (width, height) == (24, 24)
    assert_edges_opaque(pixel, width, height)

    data = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="50%" height="50%" '
        'viewBox="0 0 24 24"><rect width="24" height="24" '
        'fill="#ff0000"/></svg>').encode()
    _, ((width, height), pixel) = render(
        data, parent_width=100, parent_height=100)
    assert (width, height) == (50, 50)
    assert_edges_opaque(pixel, width, height)


@pytest.mark.parametrize('size', (
    0, -1, 0.0, -0.5, float('nan'), float('inf'), 0.4, 0.5))
def test_invalid_output_size_is_rejected(size):
    """Invalid sizes raise before a surface, and so a file, is created."""
    with pytest.raises(ValueError, match='The SVG size is undefined'):
        svg2png(bleed('24', '24'), output_width=size)


@pytest.mark.parametrize('scale', (0, -1, float('nan'), float('inf')))
def test_invalid_scale_is_rejected(scale):
    """``scale`` obeys the same contract as explicit output sizes."""
    with pytest.raises(ValueError, match='The SVG size is undefined'):
        svg2png(bleed('24', '24'), scale=scale)


@pytest.mark.parametrize('width,height', (
    ('0', '24'), ('24', '0'), ('-24', '24'), ('24', '-24')))
def test_undefined_document_size_is_rejected(width, height):
    """A document without a drawable size is still rejected as before."""
    data = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}"><rect width="1" height="1"/>'
        '</svg>').encode()
    with pytest.raises(ValueError, match='The SVG size is undefined'):
        svg2png(data)


def test_invalid_size_does_not_write_a_file(tmp_path):
    """A rejected conversion must not leave a truncated output file."""
    output = tmp_path / 'output.png'
    with pytest.raises(ValueError, match='The SVG size is undefined'):
        svg2png(bleed('24', '24'), output_width=0, write_to=str(output))
    assert not output.exists()


@pytest.mark.parametrize('format_name', ('pdf', 'ps', 'eps', 'svg'))
def test_invalid_size_does_not_write_vector_files(tmp_path, format_name):
    """Vector surfaces also reject the size before opening the file."""
    converters = {
        'pdf': svg2pdf, 'ps': svg2ps, 'eps': svg2eps, 'svg': svg2svg}
    output = tmp_path / f'output.{format_name}'
    with pytest.raises(ValueError, match='The SVG size is undefined'):
        converters[format_name](
            bleed('24', '24'), output_height=-1, write_to=str(output))
    assert not output.exists()


def test_smallest_renderable_output():
    """The smallest valid output is one pixel, and it is painted."""
    _, ((width, height), pixel) = render(
        bleed('24', '24'), output_width=1)
    assert (width, height) == (1, 1)
    assert pixel(0, 0) == (255, 0, 0, 255)


def test_output_size_does_not_change_with_repeated_renders():
    """The viewport state does not leak between conversions."""
    data = bleed('1cm', '1cm')
    sizes = [
        load_png(svg2png(data, output_width=width))[0]
        for width in (10, 37.5, 10, 50, 37.5)]
    assert sizes == [(10, 10), (38, 38), (10, 10), (50, 50), (38, 38)]
