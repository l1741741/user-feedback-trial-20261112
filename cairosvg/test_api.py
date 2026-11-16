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


# Tests for the handling of 'display' and 'visibility'. They render actual
# images and check pixels (not only parsed attributes), using overlapping
# colors and visible borders as visible/hidden controls.

DISPLAY_VISIBILITY_HEAD = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
    '<rect width="100" height="100" fill="white"/>')
DISPLAY_VISIBILITY_TAIL = '</svg>'
RED = (255, 0, 0)
BLUE = (0, 0, 255)
WHITE = (255, 255, 255)


def render_pixels(svg_string):
    """Render an SVG string and return a pixel getter."""
    from PIL import Image
    png = svg2png(
        bytestring=svg_string.encode(), output_width=100, output_height=100)
    image = Image.open(io.BytesIO(png)).convert('RGB')
    return image.getpixel


def test_visibility_override():
    """Visible children of hidden groups must be painted."""
    # Explicitly visible child of a hidden group, over a blue background
    for hidden, visible in (
            ('visibility="hidden"', 'visibility="visible"'),
            ('style="visibility:hidden"', 'style="visibility:visible"')):
        pixel = render_pixels(
            DISPLAY_VISIBILITY_HEAD +
            '<rect x="10" y="10" width="80" height="80" fill="blue"/>'
            f'<g {hidden}><rect x="20" y="20" width="60" height="60" '
            f'fill="red" stroke="black" stroke-width="4" {visible}/></g>' +
            DISPLAY_VISIBILITY_TAIL)
        assert pixel((50, 50)) == RED
        assert pixel((20, 50)) == (0, 0, 0)  # the child's border is drawn
        assert pixel((15, 15)) == BLUE

    # Same through a stylesheet
    pixel = render_pixels(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<style>.hid { visibility: hidden } .vis { visibility: visible }'
        '</style>'
        '<rect width="100" height="100" fill="white"/>'
        '<rect x="10" y="10" width="80" height="80" fill="blue"/>'
        '<g class="hid"><rect class="vis" x="20" y="20" width="60" '
        'height="60" fill="red"/></g></svg>')
    assert pixel((50, 50)) == RED

    # Hidden child without override stays hidden
    pixel = render_pixels(
        DISPLAY_VISIBILITY_HEAD +
        '<g visibility="hidden">'
        '<rect x="10" y="10" width="80" height="80" fill="red"/></g>' +
        DISPLAY_VISIBILITY_TAIL)
    assert pixel((50, 50)) == WHITE

    # 'collapse' is treated as 'hidden'
    pixel = render_pixels(
        DISPLAY_VISIBILITY_HEAD +
        '<rect x="10" y="10" width="80" height="80" fill="red" '
        'visibility="collapse"/>' + DISPLAY_VISIBILITY_TAIL)
    assert pixel((50, 50)) == WHITE


def test_display_none_branch():
    """display:none removes the whole branch, without any leftover."""
    # display:none on use: nothing is drawn
    pixel = render_pixels(
        DISPLAY_VISIBILITY_HEAD +
        '<defs><rect id="r" x="10" y="10" width="80" height="80" '
        'fill="red"/></defs>'
        '<use href="#r" display="none"/>' + DISPLAY_VISIBILITY_TAIL)
    assert pixel((50, 50)) == WHITE

    # display:none on image: nothing is drawn
    pixel = render_pixels(
        DISPLAY_VISIBILITY_HEAD +
        '<image href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAUAAAAF'
        'CAYAAACNbyblAAAAHElEQVQI12P4//8/w38GIAXDIBKE0DHxgljNBAAO9TXL0Y4OHwAA'
        'AABJRU5ErkJggg==" x="10" y="10" width="80" height="80" '
        'display="none"/>' + DISPLAY_VISIBILITY_TAIL)
    assert pixel((50, 50)) == WHITE

    # No markers are drawn for hidden or none-displayed paths
    for attribute in ('visibility="hidden"', 'display="none"'):
        pixel = render_pixels(
            DISPLAY_VISIBILITY_HEAD +
            '<defs><marker id="m" markerWidth="10" markerHeight="10" '
            'refX="5" refY="5"><rect width="10" height="10" fill="red"/>'
            '</marker></defs>'
            f'<path d="M 20 50 L 80 50" stroke="blue" {attribute} '
            'marker-start="url(#m)" marker-end="url(#m)"/>' +
            DISPLAY_VISIBILITY_TAIL)
        assert pixel((20, 50)) == WHITE
        assert pixel((80, 50)) == WHITE

    # Siblings before and after a hidden branch are unaffected
    pixel = render_pixels(
        DISPLAY_VISIBILITY_HEAD +
        '<rect x="10" y="10" width="20" height="80" fill="red"/>'
        '<g display="none">'
        '<rect x="40" y="10" width="20" height="80" fill="red"/></g>'
        '<rect x="70" y="10" width="20" height="80" fill="blue"/>' +
        DISPLAY_VISIBILITY_TAIL)
    assert pixel((20, 50)) == RED
    assert pixel((50, 50)) == WHITE
    assert pixel((80, 50)) == BLUE


def test_display_visibility_text_layout():
    """Hidden text keeps layout, none-displayed text has no layout effect."""
    # A display:none tspan doesn't advance the text cursor: same as 'AC'
    with_none = svg2png(bytestring=(
        DISPLAY_VISIBILITY_HEAD +
        '<text x="10" y="50" font-size="40" fill="red">'
        'A<tspan display="none">WWWW</tspan>C</text>' +
        DISPLAY_VISIBILITY_TAIL).encode())
    adjacent = svg2png(bytestring=(
        DISPLAY_VISIBILITY_HEAD +
        '<text x="10" y="50" font-size="40" fill="red">AC</text>' +
        DISPLAY_VISIBILITY_TAIL).encode())
    assert with_none == adjacent

    # A visibility:hidden tspan is not painted but still advances the cursor
    pixel = render_pixels(
        DISPLAY_VISIBILITY_HEAD +
        '<text x="10" y="50" font-size="20" fill="red">'
        'A<tspan visibility="hidden">WW</tspan>C</text>' +
        DISPLAY_VISIBILITY_TAIL)
    assert pixel((45, 45)) == WHITE  # hidden glyphs leave no mark
    # The trailing C is shifted right, past the hidden glyphs
    assert any(
        pixel((x, y)) == RED
        for x in range(55, 75) for y in range(35, 50))

    # Hidden text leaves no ghost path behind for following shapes
    pixel = render_pixels(
        DISPLAY_VISIBILITY_HEAD +
        '<text x="5" y="90" font-size="80" visibility="hidden" '
        'fill="red">B</text>'
        '<rect x="70" y="10" width="20" height="20" fill="blue"/>' +
        DISPLAY_VISIBILITY_TAIL)
    assert pixel((30, 70)) == WHITE
    assert pixel((80, 20)) == BLUE


def test_display_visibility_references():
    """use and clip-path references follow display/visibility semantics."""
    # Hidden use with an explicitly visible grandchild: grandchild is drawn
    pixel = render_pixels(
        DISPLAY_VISIBILITY_HEAD +
        '<defs><g id="g"><rect x="10" y="10" width="80" height="80" '
        'fill="red" visibility="visible"/></g></defs>'
        '<use href="#g" visibility="hidden"/>' + DISPLAY_VISIBILITY_TAIL)
    assert pixel((50, 50)) == RED

    # A hidden use instance doesn't affect other instances of the same
    # element (no input tree mutation), and rendering twice is stable
    svg_string = (
        DISPLAY_VISIBILITY_HEAD +
        '<defs><rect id="r" x="10" y="10" width="80" height="80" '
        'fill="red"/></defs>'
        '<use href="#r" visibility="hidden"/>'
        '<use href="#r"/>' + DISPLAY_VISIBILITY_TAIL)
    assert svg2png(bytestring=svg_string.encode()) == svg2png(
        bytestring=svg_string.encode())
    pixel = render_pixels(svg_string)
    assert pixel((50, 50)) == RED

    # A display:none child of a clipPath contributes no geometry and
    # doesn't discard the geometry of its siblings
    pixel = render_pixels(
        DISPLAY_VISIBILITY_HEAD +
        '<defs><clipPath id="c">'
        '<rect x="0" y="0" width="50" height="100"/>'
        '<rect x="50" y="0" width="50" height="100" display="none"/>'
        '</clipPath></defs>'
        '<rect width="100" height="100" fill="red" clip-path="url(#c)"/>' +
        DISPLAY_VISIBILITY_TAIL)
    assert pixel((25, 50)) == RED
    assert pixel((75, 50)) == WHITE
