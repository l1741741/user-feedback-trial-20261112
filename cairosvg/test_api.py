"""
CairoSVG API test suite.

These tests can be used as deployment tests.

"""

import io
import sys

import cairocffi as cairo
import pytest

from . import SURFACES, VERSION, parser, surface, svg2pdf, svg2png
from .__main__ import main

MAGIC_NUMBERS = {
    'SVG': b'<?xml',
    'PNG': b'\211PNG\r\n\032\n',
    'PDF': b'%PDF',
    'PS': b'%!',
}

SVG_SAMPLE = b'''\
<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" version="1.1" width="4in" height="5in">
  <rect x="5" y="10" width="13" height="15"
        fill="lime" stroke="black" stroke-width="1" />
</svg>
'''


@pytest.mark.parametrize('format_name', MAGIC_NUMBERS)
def test_formats(format_name):
    """Convert to a given format and test that output looks right."""
    content = SURFACES[format_name].convert(SVG_SAMPLE)
    assert content.startswith(MAGIC_NUMBERS[format_name])


def read_file(filename):
    """Shortcut to return the whole content of a file as a byte string."""
    with open(filename, 'rb') as file_object:
        return file_object.read()


def test_api(tmp_path):
    """Test the Python API with various parameters."""
    expected_content = svg2png(SVG_SAMPLE)
    # Already tested above: just a sanity check:
    assert expected_content.startswith(MAGIC_NUMBERS['PNG'])

    # Read from a byte string
    assert svg2png(SVG_SAMPLE) == expected_content
    assert svg2png(bytestring=SVG_SAMPLE) == expected_content

    file_like = io.BytesIO(SVG_SAMPLE)
    # Read from a file-like object
    assert svg2png(file_obj=file_like) == expected_content

    file_like = io.BytesIO()
    # Write to a file-like object
    svg2png(SVG_SAMPLE, write_to=file_like)
    assert file_like.getvalue() == expected_content

    temp_0 = tmp_path / 'sample_0.svg'
    temp_0.write_bytes(SVG_SAMPLE)

    # Read from a filename
    assert svg2png(url=str(temp_0)) == expected_content
    assert svg2png(url=f'file://{temp_0}') == expected_content

    # Read from a pathlib.Path
    assert svg2png(url=temp_0) == expected_content

    with temp_0.open('rb') as file_object:
        # Read from a real file object
        assert svg2png(file_obj=file_object) == expected_content

    temp_1 = tmp_path / 'result_1.png'
    with temp_1.open('wb') as file_object:
        # Write to a real file object
        svg2png(SVG_SAMPLE, write_to=file_object)
    assert read_file(str(temp_1)) == expected_content

    temp_2 = tmp_path / 'result_2.png'
    # Write to a filename
    svg2png(SVG_SAMPLE, write_to=str(temp_2))
    assert read_file(str(temp_2)) == expected_content

    file_like = io.BytesIO()
    try:
        svg2png(write_to=file_like)
    except TypeError:
        pass
    else:  # pragma: no cover
        raise Exception('TypeError not raised')


def test_low_level_api():
    """Test the low-level Python API with various parameters."""
    expected_content = svg2png(SVG_SAMPLE)

    # Same as above, longer version
    tree = parser.Tree(bytestring=SVG_SAMPLE)
    file_like = io.BytesIO()
    png_surface = surface.PNGSurface(tree, file_like, 96)
    png_surface.finish()
    assert file_like.getvalue() == expected_content

    png_result = cairo.ImageSurface.create_from_png(
        io.BytesIO(expected_content))
    expected_width = png_result.get_width()
    expected_height = png_result.get_height()

    # Abstract surface
    png_surface = surface.PNGSurface(tree, None, 96)
    assert png_surface.width == expected_width
    assert png_surface.height == expected_height
    assert cairo.SurfacePattern(png_surface.cairo)


def test_script(tmp_path):
    """Test the ``cairosvg`` script and the ``main`` function."""
    expected_png = svg2png(SVG_SAMPLE)[:100]
    expected_pdf = svg2pdf(SVG_SAMPLE)[:100]

    def test_main(args, exit_=False, input_=None, full=False):
        """Test main called with given ``args``.

        If ``exit_`` is ``True``, check that ``SystemExit`` is raised. We then
        assume that the program output is an unicode string.

        If ``input_`` is given, use this stream as input stream.

        """
        sys.argv = ['cairosvg'] + args
        old_stdin, old_stdout = sys.stdin, sys.stdout

        output_buffer = io.BytesIO()
        sys.stdout = io.TextIOWrapper(output_buffer)

        if input_:
            sys.stdin = open(input_, 'rb')
            sys.stdin.buffer = sys.stdin

        if exit_:
            try:
                main()
            except SystemExit:
                pass
            else:  # pragma: no cover
                raise Exception('CairoSVG did not exit')
        else:
            main()

        sys.stdout.flush()
        output = output_buffer.getvalue()
        sys.stdin, sys.stdout = old_stdin, old_stdout
        return output if full else output[:100]

    svg_path = tmp_path / "test.svg"
    svg_path.write_bytes(SVG_SAMPLE)
    svg_filename = str(svg_path)

    assert test_main(['--help'], exit_=True).startswith(b'usage: ')
    assert test_main(['--version'], exit_=True).strip() == (
        VERSION.encode('ascii'))

    assert test_main([svg_filename]) == expected_pdf
    assert test_main([svg_filename, '-d', '96', '-f', 'pdf']) == expected_pdf
    assert test_main([svg_filename, '-f', 'png']) == expected_png
    assert test_main(['-'], input_=svg_filename) == expected_pdf

    # Test DPI
    output = test_main([svg_filename, '-d', '10', '-f', 'png'], full=True)
    image = cairo.ImageSurface.create_from_png(io.BytesIO(output))
    assert image.get_width() == 40
    assert image.get_height() == 50

    temp_1 = tmp_path / 'result_1'
    # Default to PDF
    assert not test_main([svg_filename, '-o', str(temp_1)])
    assert read_file(temp_1)[:100] == expected_pdf

    temp_2 = tmp_path / 'result_2.png'
    # Guess from the file extension
    assert not test_main([svg_filename, '-o', str(temp_2)])
    assert read_file(temp_2)[:100] == expected_png

    temp_3 = tmp_path / 'result_3.png'
    # Explicit -f wins
    assert not test_main([svg_filename, '-o', str(temp_3), '-f', 'pdf'])
    assert read_file(str(temp_3))[:100] == expected_pdf


# ---------------------------------------------------------------------------
# fill-rule / clip-rule regression tests
#
# These tests assert the actual rendered coverage at geometrically known
# pixels instead of checking parsed attributes or comparing against a
# hand-drawn reference image. Every sample is a 10x10 user-unit SVG
# rendered at 20px per unit; a probe reads the pixel at the center of a
# user-unit cell.
# ---------------------------------------------------------------------------

PIXEL_SCALE = 20
RING_PATH = 'M1,1 H9 V9 H1 Z M3,3 H7 V7 H3 Z'


def _render_surface(svg_text):
    """Render an SVG string and return the cairo image surface."""
    png = svg2png(
        svg_text.encode(),
        output_width=10 * PIXEL_SCALE,
        output_height=10 * PIXEL_SCALE)
    return cairo.ImageSurface.create_from_png(io.BytesIO(png))


def _pixel(surface, x, y):
    """Return the unpremultiplied RGBA pixel at user-unit (x, y)."""
    data = surface.get_data()
    width = surface.get_width()
    px = int(x * PIXEL_SCALE + PIXEL_SCALE // 2)
    py = int(y * PIXEL_SCALE + PIXEL_SCALE // 2)
    offset = (py * width + px) * 4
    blue, green, red, alpha = data[offset:offset + 4]
    if alpha == 0:
        return 0, 0, 0, 0
    return (
        round(red * 255 / alpha),
        round(green * 255 / alpha),
        round(blue * 255 / alpha),
        alpha,
    )


RED = (255, 0, 0, 255)
BLUE = (0, 0, 255, 255)
EMPTY = (0, 0, 0, 0)


def test_fill_rule_concentric_same_winding():
    """Two same-winding subpaths: only evenodd punches a hole."""
    template = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><path fill="red" fill-rule="{}" d="'
        + RING_PATH + '"/></svg>')
    nonzero = _render_surface(template.format('nonzero'))
    assert _pixel(nonzero, 2, 2) == RED
    assert _pixel(nonzero, 5, 5) == RED
    evenodd = _render_surface(template.format('evenodd'))
    assert _pixel(evenodd, 2, 2) == RED
    assert _pixel(evenodd, 5, 5) == EMPTY


def test_fill_rule_opposite_winding():
    """Opposite winding punches a hole under both rules."""
    path = 'M1,1 H9 V9 H1 Z M3,3 V7 H7 V3 Z'
    template = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><path fill="red" fill-rule="{}" d="'
        + path + '"/></svg>')
    for rule in ('nonzero', 'evenodd'):
        image = _render_surface(template.format(rule))
        assert _pixel(image, 2, 2) == RED
        assert _pixel(image, 5, 5) == EMPTY


def test_fill_rule_self_intersecting_polygon():
    """A bowtie polygon renders with stable fill under both rules."""
    template = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><polygon fill="red" fill-rule="{}" '
        'points="1,9 9,9 1,1 9,1"/></svg>')
    for rule in ('nonzero', 'evenodd'):
        image = _render_surface(template.format(rule))
        assert _pixel(image, 5, 2) == RED
        assert _pixel(image, 5, 8) == RED
        assert _pixel(image, 3, 5) == EMPTY
        assert _pixel(image, 7, 5) == EMPTY


def test_fill_rule_group_inheritance_and_override():
    """fill-rule inherits through groups and is locally overridable."""
    inherited = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><g fill="red" fill-rule="evenodd"><path d="'
        + RING_PATH + '"/></g></svg>')
    image = _render_surface(inherited)
    assert _pixel(image, 5, 5) == EMPTY

    overridden = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><g fill="red" fill-rule="evenodd">'
        '<path fill-rule="nonzero" d="' + RING_PATH + '"/></g></svg>')
    image = _render_surface(overridden)
    assert _pixel(image, 5, 5) == RED


def test_fill_rule_does_not_leak_between_siblings():
    """An evenodd path must not change the rule of the next sibling."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10">'
        '<path fill="red" fill-rule="evenodd" d="' + RING_PATH + '"/>'
        '<path fill="blue" d="' + RING_PATH + '"/></svg>')
    image = _render_surface(svg)
    # Blue uses the default nonzero rule: its inner square stays filled.
    assert _pixel(image, 5, 5) == BLUE
    assert _pixel(image, 2, 2) == BLUE


def test_fill_rule_unclosed_and_polyline_outlines():
    """Implicit closure keeps fill-rule results stable."""
    unclosed = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><path fill="red" fill-rule="{}" '
        'd="M2,2 H8 V8 H2 V2 M4,4 H6 V6 H4 V4"/></svg>')
    assert _pixel(_render_surface(unclosed.format('nonzero')), 5, 5) == RED
    assert _pixel(_render_surface(unclosed.format('evenodd')), 5, 5) == EMPTY

    polyline = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><polyline fill="red" fill-rule="evenodd" '
        'points="1,1 9,1 9,9 1,9 1,1 3,3 7,3 7,7 3,7 3,3"/></svg>')
    image = _render_surface(polyline)
    assert _pixel(image, 2, 2) == RED
    assert _pixel(image, 5, 5) == EMPTY


def test_clip_rule_evenodd_and_nonzero():
    """clip-rule controls holes in clipPath geometry."""
    template = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><defs><clipPath id="c"><path clip-rule="{}" d="'
        + RING_PATH + '"/></clipPath></defs>'
        '<rect width="10" height="10" fill="red" clip-path="url(#c)"/></svg>')
    nonzero = _render_surface(template.format('nonzero'))
    assert _pixel(nonzero, 2, 2) == RED
    assert _pixel(nonzero, 5, 5) == RED
    evenodd = _render_surface(template.format('evenodd'))
    assert _pixel(evenodd, 2, 2) == RED
    assert _pixel(evenodd, 5, 5) == EMPTY


def test_clip_rule_inherited_from_clip_path():
    """A clip-rule on the clipPath element reaches its children."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><defs><clipPath id="c" clip-rule="evenodd">'
        '<path d="' + RING_PATH + '"/></clipPath></defs>'
        '<rect width="10" height="10" fill="red" clip-path="url(#c)"/></svg>')
    image = _render_surface(svg)
    assert _pixel(image, 2, 2) == RED
    assert _pixel(image, 5, 5) == EMPTY


def test_clip_rule_mixed_children_union():
    """Mixed-rule clipPath children union; each keeps its own rule."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><defs><clipPath id="c">'
        '<path clip-rule="evenodd" '
        'd="M0,0 H5 V10 H0 Z M1,3 H4 V7 H1 Z"/>'
        '<path clip-rule="nonzero" d="M5,0 H10 V10 H5 Z"/>'
        '</clipPath></defs>'
        '<rect width="10" height="10" fill="red" clip-path="url(#c)"/></svg>')
    image = _render_surface(svg)
    assert _pixel(image, 2, 1) == RED      # evenodd ring body
    assert _pixel(image, 2, 5) == EMPTY    # evenodd ring hole
    assert _pixel(image, 7, 5) == RED      # nonzero sibling region


def test_clip_path_group_transform_and_nesting():
    """Transforms and nested groups inside a clipPath keep the hole."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><defs><clipPath id="c" clip-rule="evenodd">'
        '<g transform="scale(2)"><g transform="translate(0.5,0.5)">'
        '<path d="M0,0 H4 V4 H0 Z M1,1 H3 V3 H1 Z"/>'
        '</g></g></clipPath></defs>'
        '<rect width="10" height="10" fill="red" clip-path="url(#c)"/></svg>')
    image = _render_surface(svg)
    assert _pixel(image, 2, 2) == RED
    assert _pixel(image, 5, 5) == EMPTY
    assert _pixel(image, 9, 9) == EMPTY


def test_clip_path_object_bounding_box():
    """objectBoundingBox units and evenodd holes both keep working."""
    bbox = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><defs><clipPath id="c" '
        'clipPathUnits="objectBoundingBox">'
        '<rect x="0.25" y="0.25" width="0.5" height="0.5"/></clipPath></defs>'
        '<rect x="2" y="2" width="6" height="6" fill="red" '
        'clip-path="url(#c)"/></svg>')
    image = _render_surface(bbox)
    assert _pixel(image, 5, 5) == RED
    assert _pixel(image, 2, 5) == EMPTY

    bbox_hole = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><defs><clipPath id="c" '
        'clipPathUnits="objectBoundingBox">'
        '<path clip-rule="evenodd" '
        'd="M0,0 H1 V1 H0 Z M0.25,0.25 H0.75 V0.75 H0.25 Z"/>'
        '</clipPath></defs>'
        '<rect width="10" height="10" fill="red" clip-path="url(#c)"/></svg>')
    image = _render_surface(bbox_hole)
    # The bbox of the full-canvas rect is the whole viewport, so the
    # outer ring covers (0,0)-(10,10) and the inner hole maps to
    # (2.5,2.5)-(7.5,7.5).
    assert _pixel(image, 1, 1) == RED
    assert _pixel(image, 5, 5) == EMPTY


def test_clip_rule_ignores_fill_rule_inside_clip_path():
    """Within a clipPath, clip-rule (not fill-rule) decides the region."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><defs><clipPath id="c">'
        '<path fill-rule="evenodd" clip-rule="nonzero" d="' + RING_PATH
        + '"/></clipPath></defs>'
        '<rect width="10" height="10" fill="red" clip-path="url(#c)"/></svg>')
    image = _render_surface(svg)
    assert _pixel(image, 5, 5) == RED


def test_clip_path_degenerate_children():
    """Empty clip geometry is skipped; siblings still define the region."""
    empty_first = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><defs><clipPath id="c">'
        '<path d=""/><path d="M2,2 H8 V8 H2 Z"/></clipPath></defs>'
        '<rect width="10" height="10" fill="red" clip-path="url(#c)"/></svg>')
    image = _render_surface(empty_first)
    assert _pixel(image, 5, 5) == RED
    assert _pixel(image, 1, 1) == EMPTY

    all_empty = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><defs><clipPath id="c"><path d=""/></clipPath>'
        '</defs><rect width="10" height="10" fill="red" '
        'clip-path="url(#c)"/></svg>')
    image = _render_surface(all_empty)
    assert _pixel(image, 5, 5) == EMPTY


def test_clip_inside_opacity_group():
    """Clipping and a transparent group preserve the same visible area."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><defs><clipPath id="c">'
        '<path clip-rule="evenodd" d="' + RING_PATH + '"/></clipPath></defs>'
        '<g opacity="0.5"><rect width="10" height="10" fill="red" '
        'clip-path="url(#c)"/></g></svg>')
    image = _render_surface(svg)
    assert _pixel(image, 5, 5) == EMPTY
    r, g, b, a = _pixel(image, 2, 2)
    assert (r, g, b) == (255, 0, 0)
    assert a == 128


def test_same_outline_used_for_fill_and_clip():
    """Reusing one outline with evenodd for both fill and clip matches."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" '
        'viewBox="0 0 10 10"><defs>'
        '<path id="o" d="' + RING_PATH + '"/>'
        '<clipPath id="c"><use href="#o" clip-rule="evenodd"/></clipPath>'
        '</defs><use href="#o" fill="red" fill-rule="evenodd"/>'
        '<rect width="10" height="10" fill="blue" opacity="0.5" '
        'clip-path="url(#c)"/></svg>')
    image = _render_surface(svg)
    assert _pixel(image, 5, 5) == EMPTY
    r, g, b, a = _pixel(image, 2, 2)
    assert a == 255 and r == 127 and b == 128
