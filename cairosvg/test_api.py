"""
CairoSVG API test suite.

These tests can be used as deployment tests.

"""

import io
import os
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


# xml:space and text whitespace handling.
#
# The render tests draw real text with a locally available monospace font
# and compare live PNG renders (never pre-made images), allowing a small
# anti-aliasing tolerance.

FONT_CANDIDATES = (
    ('Menlo', '/System/Library/Fonts/Menlo.ttc'),
    ('Courier', '/System/Library/Fonts/Courier.ttc'),
    ('DejaVu Sans Mono',
     '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'),
    ('Liberation Mono',
     '/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf'),
)
FONT_FAMILY = next(
    (name for name, path in FONT_CANDIDATES if os.path.exists(path)), None)
requires_font = pytest.mark.skipif(
    FONT_FAMILY is None, reason='no known monospace font available')

SIGN_WIDTH = 240
SIGN_HEIGHT = 160


def sign_svg(content, extra=''):
    """Wrap ``content`` in a 240x160 sign SVG."""
    return f'''<svg xmlns="http://www.w3.org/2000/svg"
      xmlns:xlink="http://www.w3.org/1999/xlink"
      width="{SIGN_WIDTH}" height="{SIGN_HEIGHT}">
      {extra}
      {content}
    </svg>'''


def find_text(node):
    """Return the first ``text`` node in a parsed tree."""
    if node.tag == 'text':
        return node
    for child in node.children:
        found = find_text(child)
        if found is not None:
            return found


def flattened_text(node):
    """Return the concatenated text of a parsed text ``node``."""
    return (node.text or '') + ''.join(
        flattened_text(child) for child in node.children)


def flatten_sign(content, extra=''):
    """Parse a sign and return the flattened text of its text element."""
    tree = parser.Tree(bytestring=sign_svg(content, extra))
    return flattened_text(find_text(tree))


def png_pixels(png):
    """Return (width, height, rows) of a PNG, rows of (r, g, b, a) tuples."""
    image = cairo.ImageSurface.create_from_png(io.BytesIO(png))
    width, height = image.get_width(), image.get_height()
    stride = image.get_stride()
    data = image.get_data()
    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            offset = y * stride + x * 4
            blue, green, red, alpha = data[offset:offset + 4]
            row.append((red, green, blue, alpha))
        rows.append(row)
    return width, height, rows


def assert_png_similar(png_1, png_2, tolerance=8, max_different_ratio=0.01):
    """Assert that two PNG renders match, with anti-aliasing tolerance."""
    width_1, height_1, rows_1 = png_pixels(png_1)
    width_2, height_2, rows_2 = png_pixels(png_2)
    assert (width_1, height_1) == (width_2, height_2)
    different = 0
    for row_1, row_2 in zip(rows_1, rows_2):
        for pixel_1, pixel_2 in zip(row_1, row_2):
            if any(abs(channel_1 - channel_2) > tolerance
                   for channel_1, channel_2 in zip(pixel_1, pixel_2)):
                different += 1
    assert different <= max_different_ratio * width_1 * height_1


def ink_columns(png, threshold=384):
    """Return the sorted x coordinates of dark pixels in a PNG."""
    _, _, rows = png_pixels(png)
    columns = set()
    for row in rows:
        for x, (red, green, blue, _) in enumerate(row):
            if red + green + blue < threshold:
                columns.add(x)
    return sorted(columns)


def render_sign(content, extra=''):
    """Render a sign to PNG bytes, black text on white."""
    return svg2png(
        bytestring=sign_svg(content, extra), background_color='white')


def test_handle_white_spaces():
    """Newlines, tabs and consecutive spaces follow the xml:space rules."""
    # Default: newlines removed, tabs become spaces, spaces collapse
    assert parser.handle_white_spaces('a\nb', False) == 'ab'
    assert parser.handle_white_spaces('a\tb', False) == 'a b'
    assert parser.handle_white_spaces('a   b', False) == 'a b'
    # Preserve: newlines and tabs become spaces, repetitions stay
    assert parser.handle_white_spaces('a\nb', True) == 'a b'
    assert parser.handle_white_spaces('a\t  b', True) == 'a   b'
    # Empty input stays empty
    assert parser.handle_white_spaces(None, False) == ''
    assert parser.handle_white_spaces('', True) == ''


def test_xml_space_default_flattening():
    """Default xml:space collapses whitespace across nested elements."""
    # Newlines are removed, tabs become spaces, spaces collapse
    assert flatten_sign('<text>a\n\tb   c</text>') == 'a b c'
    # Leading and trailing spaces are stripped at text element boundaries
    assert flatten_sign('<text>  a  </text>') == 'a'
    # Spaces collapse across tspan boundaries
    assert flatten_sign('<text>a <tspan> b</tspan></text>') == 'a b'
    # A nested tspan ending with a space still collapses with the tail text
    assert flatten_sign(
        '<text><tspan><tspan>a </tspan></tspan> b</text>') == 'a b'
    # An empty tspan neither breaks collapsing nor drops characters
    assert flatten_sign('<text>a <tspan></tspan> b</text>') == 'a b'
    # The last character of a nested tspan is the one that matters, not the
    # last character of its parent's own text
    assert flatten_sign(
        '<text><tspan>a <tspan>b</tspan></tspan> c</text>') == 'a b c'
    # Tail text after a child is kept, trailing space at the end is stripped
    assert flatten_sign('<text><tspan>a</tspan> tail  </text>') == 'a tail'
    # Whitespace-only text disappears
    assert flatten_sign('<text>  \n  </text>') == ''


def test_xml_space_preserve_and_nested_default():
    """xml:space=preserve keeps spaces; children can switch back."""
    # Preserve keeps repeated spaces and turns newlines/tabs into spaces
    assert flatten_sign(
        '<text xml:space="preserve">a   b</text>') == 'a   b'
    assert flatten_sign(
        '<text xml:space="preserve">a\n\tb</text>') == 'a  b'
    # An explicit default child collapses again inside a preserved parent
    assert flatten_sign(
        '<text xml:space="preserve">a  '
        '<tspan xml:space="default">b   c</tspan></text>') == 'a  b c'
    # …and a preserved grandchild keeps its spaces again
    assert flatten_sign(
        '<text xml:space="preserve">a  '
        '<tspan xml:space="default">b   '
        '<tspan xml:space="preserve">c  d</tspan></tspan></text>'
    ) == 'a  b c  d'


@requires_font
def test_xml_space_default_matches_explicit_text():
    """Collapsed whitespace renders like the equivalent explicit text."""
    text_attributes = (
        f'x="20" y="80" font-family="{FONT_FAMILY}" font-size="20"')
    messy = f'''<text {text_attributes}>Signed
        <tspan>  by </tspan>
        <tspan><tspan>shop </tspan></tspan>
        <tspan></tspan>  319
      </text>'''
    explicit = f'<text {text_attributes}>Signed by shop 319</text>'
    assert flatten_sign(messy) == 'Signed by shop 319'
    assert_png_similar(render_sign(messy), render_sign(explicit))


@requires_font
def test_xml_space_preserve_matches_explicit_text():
    """Preserved whitespace renders like the equivalent explicit spaces."""
    text_attributes = (
        f'x="20" y="80" font-family="{FONT_FAMILY}" font-size="20"')
    messy = (
        f'<text xml:space="preserve" {text_attributes}>'
        'Signed   by\n\tshop</text>')
    explicit = (
        f'<text xml:space="preserve" {text_attributes}>'
        'Signed   by  shop</text>')
    assert flatten_sign(messy) == 'Signed   by  shop'
    assert_png_similar(render_sign(messy), render_sign(explicit))


@requires_font
def test_xml_space_nested_default_matches_explicit_text():
    """A default child in a preserved parent renders like explicit text."""
    text_attributes = (
        f'x="20" y="80" font-family="{FONT_FAMILY}" font-size="20"')
    messy = (
        f'<text xml:space="preserve" {text_attributes}>A  '
        f'<tspan xml:space="default">B   C</tspan></text>')
    explicit = (
        f'<text xml:space="preserve" {text_attributes}>A  B C</text>')
    assert flatten_sign(messy) == 'A  B C'
    assert_png_similar(render_sign(messy), render_sign(explicit))


@requires_font
def test_xml_space_anchor_and_dx_match_explicit_text():
    """text-anchor and x/dx use the actually laid out (collapsed) text."""
    text_attributes = (
        f'x="120" y="80" text-anchor="middle" dx="0 2 0 2 0 0 2" '
        f'font-family="{FONT_FAMILY}" font-size="20"')
    # The whitespace mess stays in a single text node so that the dx list
    # (which is not inherited by tspan children) covers the same characters
    messy = f'<text {text_attributes}>A  B\n\t C   D</text>'
    explicit = f'<text {text_attributes}>A B C D</text>'
    assert flatten_sign(messy) == 'A B C D'
    assert_png_similar(render_sign(messy), render_sign(explicit))

    # The collapsed text is horizontally centered on the anchor point:
    # its ink is symmetric around x=120 on the 240-wide sign
    columns = ink_columns(render_sign(explicit))
    assert columns
    center = (columns[0] + columns[-1]) / 2
    assert abs(center - 120) <= 2


@requires_font
def test_xml_space_preserve_renders_wider_than_default():
    """Preserved consecutive spaces take actual room on the PNG."""
    text_attributes = (
        f'x="20" y="80" font-family="{FONT_FAMILY}" font-size="20"')
    default_columns = ink_columns(render_sign(
        f'<text {text_attributes}>A   B</text>'))
    preserve_columns = ink_columns(render_sign(
        f'<text xml:space="preserve" {text_attributes}>A   B</text>'))
    # Both start at the same x position…
    assert abs(default_columns[0] - preserve_columns[0]) <= 1
    # …but the preserved render is wider by roughly two space widths
    default_width = default_columns[-1] - default_columns[0]
    preserve_width = preserve_columns[-1] - preserve_columns[0]
    assert preserve_width > default_width + 10


@requires_font
def test_xml_space_shared_definition_reused():
    """Shared text definitions survive multiple references and exports."""
    extra = f'''<defs><text id="label"
        font-family="{FONT_FAMILY}" font-size="20">
        <tspan>AB </tspan>  <tspan>CD</tspan>  </text></defs>'''
    content = '''<use xlink:href="#label" x="10" y="40"/>
      <use xlink:href="#label" x="10" y="80"/>'''

    # The shared definition's text is normalized once, without losing or
    # duplicating characters
    tree = parser.Tree(bytestring=sign_svg(content, extra))

    def find_by_id(node, id_):
        if node.get('id') == id_:
            return node
        for child in node.children:
            found = find_by_id(child, id_)
            if found is not None:
                return found

    assert flattened_text(find_by_id(tree, 'label')) == 'AB CD'

    # Repeated exports of the same document give identical bytes
    assert render_sign(content, extra) == render_sign(content, extra)

    # Both references render like two explicit text elements
    explicit = (
        f'<text x="10" y="40" font-family="{FONT_FAMILY}" font-size="20">'
        f'AB CD</text>'
        f'<text x="10" y="80" font-family="{FONT_FAMILY}" font-size="20">'
        f'AB CD</text>')
    assert_png_similar(render_sign(content, extra), render_sign(explicit))
