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


# Tests for ``use`` element instantiation: each instance must get its own
# position, size, transform and style, without altering the shared
# definition or the other instances.

THEME_A = (0, 102, 204, 255)  # #0066cc as (red, green, blue, alpha)
THEME_B = (204, 102, 0, 255)  # #cc6600 as (red, green, blue, alpha)
WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)
TRANSPARENT = (0, 0, 0, 0)


def png_image(png_bytes):
    """Return a cairo image surface from PNG ``png_bytes``."""
    return cairo.ImageSurface.create_from_png(io.BytesIO(png_bytes))


def png_pixel(png_bytes, x, y):
    """Return the ``(red, green, blue, alpha)`` pixel of a PNG image."""
    image = png_image(png_bytes)
    data = image.get_data()
    offset = (y * image.get_width() + x) * 4
    blue, green, red, alpha = data[offset:offset + 4]
    return red, green, blue, alpha


def png_pixels(png_bytes):
    """Return the whole pixel buffer of a PNG image."""
    image = png_image(png_bytes)
    return bytes(image.get_data())


def test_use_xy_and_transform():
    """A ``use`` instance applies ``x``/``y`` inside its own ``transform``."""
    png = svg2png(b'''
<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <defs><rect id="r" width="10" height="10" fill="red"/></defs>
  <use href="#r" transform="translate(50, 0) scale(2)" x="10" y="5"/>
</svg>''')
    # translate(x, y) applies first: rect covers (10..20, 5..15), then
    # scale(2): (20..40, 10..30), then translate(50, 0): (70..90, 10..30)
    assert png_pixel(png, 75, 15) == (255, 0, 0, 255)
    assert png_pixel(png, 69, 15) == TRANSPARENT
    assert png_pixel(png, 91, 15) == TRANSPARENT
    assert png_pixel(png, 75, 31) == TRANSPARENT


def test_use_symbol_partial_size():
    """A missing ``width``/``height`` on ``use`` defaults to 100%."""
    template = b'''
<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <defs>
    <symbol id="s" viewBox="0 0 10 10">
      <rect width="10" height="10" fill="red"/>
    </symbol>
  </defs>
  <use href="#s" %s/>
</svg>'''
    # Only a width: height defaults to 100% of the viewport, the symbol
    # keeps its ratio and is centered in the 50x100 instance viewport
    png = svg2png(template % b'width="50"')
    assert png_pixel(png, 25, 50) == (255, 0, 0, 255)
    assert png_pixel(png, 75, 50) == TRANSPARENT
    assert png_pixel(png, 25, 10) == TRANSPARENT
    # Only a height: width defaults to 100% of the viewport
    png = svg2png(template % b'height="50"')
    assert png_pixel(png, 50, 25) == (255, 0, 0, 255)
    assert png_pixel(png, 50, 75) == TRANSPARENT
    assert png_pixel(png, 10, 25) == TRANSPARENT


def test_use_attribute_priority():
    """Explicit content attributes win over attributes inherited from use."""
    png = svg2png(b'''
<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <defs>
    <g id="g">
      <rect width="20" height="20" fill="blue"/>
      <rect x="30" width="20" height="20"/>
    </g>
  </defs>
  <use href="#g" fill="red"/>
</svg>''')
    # The explicit fill="blue" of the content wins over the inherited one
    assert png_pixel(png, 10, 10) == (0, 0, 255, 255)
    # The content without fill inherits the use element's one
    assert png_pixel(png, 40, 10) == (255, 0, 0, 255)


def test_use_shared_definition_not_altered():
    """Instances never rewrite the shared definition or the use element."""
    template = b'''
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
  <defs>
    <symbol id="s" viewBox="0 0 10 10">
      <rect width="10" height="10"/>
    </symbol>
  </defs>
  %s
</svg>'''
    instance_big = (
        b'<use href="#s" x="0" y="0" width="50" height="50" fill="red"/>')
    instance_small = (
        b'<use href="#s" x="100" y="0" width="20" height="20" fill="lime"/>')
    png_ab = svg2png(template % (instance_big + instance_small))
    png_ba = svg2png(template % (instance_small + instance_big))
    # Swapping the drawing order gives the same icons
    assert png_pixels(png_ab) == png_pixels(png_ba)
    # The second, smaller instance doesn't shrink the shared definition
    assert png_pixel(png_ab, 25, 25) == (255, 0, 0, 255)
    assert png_pixel(png_ab, 75, 25) == TRANSPARENT
    assert png_pixel(png_ab, 110, 10) == (0, 255, 0, 255)
    assert png_pixel(png_ab, 130, 10) == TRANSPARENT

    # Rendering the same parsed tree twice gives the same result: the use
    # element keeps its x/y/viewBox/mask attributes after the first render
    tree = parser.Tree(bytestring=template % (instance_big + instance_small))
    first = io.BytesIO()
    surface.PNGSurface(tree, first, 96).finish()
    second = io.BytesIO()
    surface.PNGSurface(tree, second, 96).finish()
    assert first.getvalue() == second.getvalue()
    assert png_pixel(first.getvalue(), 25, 25) == (255, 0, 0, 255)


def test_use_nested_reference():
    """Multi-layer references compose positions and inherited styles."""
    png = svg2png(b'''
<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <defs>
    <symbol id="icon" viewBox="0 0 10 10">
      <rect width="10" height="10"/>
    </symbol>
    <g id="button">
      <use href="#icon" width="20" height="20"/>
      <circle cx="18" cy="2" r="2" fill="lime"/>
    </g>
  </defs>
  <use href="#button" x="30" y="40" fill="red"/>
</svg>''')
    # The icon inherits red through both reference layers
    assert png_pixel(png, 40, 50) == (255, 0, 0, 255)
    # The badge is positioned relative to the nested instance
    assert png_pixel(png, 48, 42) == (0, 255, 0, 255)


def test_use_missing_reference():
    """A missing reference is skipped, later instances are still drawn."""
    png = svg2png(b'''
<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <defs><rect id="r" width="10" height="10" fill="red"/></defs>
  <use href="#missing"/>
  <use href="#r" x="40" y="40"/>
</svg>''')
    assert png_pixel(png, 45, 45) == (255, 0, 0, 255)


def test_use_reference_cycle():
    """Reference cycles are skipped without condemning other instances."""
    # Indirect cycle a <-> b, followed by a legitimate instance
    png = svg2png(b'''
<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <defs>
    <g id="a"><rect width="5" height="5" fill="blue"/><use href="#b"/></g>
    <g id="b"><use href="#a"/></g>
    <rect id="ok" width="10" height="10" fill="red"/>
  </defs>
  <use href="#a"/>
  <use href="#ok" x="50" y="50"/>
</svg>''')
    assert png_pixel(png, 2, 2) == (0, 0, 255, 255)
    assert png_pixel(png, 55, 55) == (255, 0, 0, 255)

    # Direct self-reference
    png = svg2png(b'''
<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <use id="u" href="#u" x="10" y="10"/>
  <rect x="40" y="40" width="10" height="10" fill="red"/>
</svg>''')
    assert png_pixel(png, 45, 45) == (255, 0, 0, 255)

    # A diamond (an element referenced twice, but not on the same
    # instantiation stack) is not a cycle
    png = svg2png(b'''
<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
  <defs>
    <rect id="d" width="10" height="10" fill="red"/>
    <g id="b"><use href="#d" x="10"/></g>
    <g id="c"><use href="#d" x="50"/></g>
    <g id="a"><use href="#b"/><use href="#c"/></g>
  </defs>
  <use href="#a" y="20"/>
</svg>''')
    assert png_pixel(png, 15, 25) == (255, 0, 0, 255)
    assert png_pixel(png, 55, 25) == (255, 0, 0, 255)


def test_use_reference_limit():
    """Runaway reference instantiation keeps aborting with ValueError."""
    levels = 18
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<defs>']
    for level in range(levels):
        parts.append(
            f'<g id="g{level}">'
            f'<use href="#g{level + 1}"/><use href="#g{level + 1}"/></g>')
    parts.append(f'<rect id="g{levels}" width="1" height="1"/>')
    parts.append('</defs><use href="#g0"/></svg>')
    with pytest.raises(ValueError, match='Abort rendering'):
        svg2png(''.join(parts).encode())


# Legend exercising the instantiation rules: two theme colors, corner
# marks, and icons instantiated at various positions and scales through
# direct, partial-size, transformed and nested references.
USE_LEGEND = b'''
<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160">
  <defs>
    <symbol id="icon-a" viewBox="0 0 20 20">
      <rect width="20" height="20"/>
      <path d="M0 0h6l-6 6z" fill="#ffffff"/>
    </symbol>
    <symbol id="icon-b" viewBox="0 0 20 20">
      <rect width="20" height="20" fill="#cc6600"/>
      <path d="M20 20h-6l6-6z" fill="#ffffff"/>
    </symbol>
    <g id="badge">
      <use href="#icon-a" width="20" height="20"/>
      <circle cx="18" cy="2" r="2" fill="#cc6600"/>
    </g>
  </defs>
  <rect width="4" height="4" fill="#000000"/>
  <rect x="236" width="4" height="4" fill="#000000"/>
  <rect y="156" width="4" height="4" fill="#000000"/>
  <rect x="236" y="156" width="4" height="4" fill="#000000"/>
  <use href="#icon-a" x="20" y="20" width="40" height="40" fill="#0066cc"/>
  <svg x="100" y="20" width="60" height="60">
    <use href="#icon-a" width="30" fill="#0066cc"/>
  </svg>
  <use href="#icon-b" x="180" y="20" width="20" height="20"
       transform="rotate(45 190 30)"/>
  <svg x="20" y="95" width="50" height="50">
    <use href="#icon-b" height="40"/>
  </svg>
  <use href="#badge" x="100" y="100" fill="#0066cc"/>
  <use href="#icon-a" x="180" y="100" width="30" height="30" fill="#0066cc"/>
</svg>'''

# The same legend with the drawing order of the instances swapped
USE_LEGEND_SWAPPED = b'''
<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160">
  <defs>
    <symbol id="icon-a" viewBox="0 0 20 20">
      <rect width="20" height="20"/>
      <path d="M0 0h6l-6 6z" fill="#ffffff"/>
    </symbol>
    <symbol id="icon-b" viewBox="0 0 20 20">
      <rect width="20" height="20" fill="#cc6600"/>
      <path d="M20 20h-6l6-6z" fill="#ffffff"/>
    </symbol>
    <g id="badge">
      <use href="#icon-a" width="20" height="20"/>
      <circle cx="18" cy="2" r="2" fill="#cc6600"/>
    </g>
  </defs>
  <use href="#icon-a" x="180" y="100" width="30" height="30" fill="#0066cc"/>
  <use href="#badge" x="100" y="100" fill="#0066cc"/>
  <svg x="20" y="95" width="50" height="50">
    <use href="#icon-b" height="40"/>
  </svg>
  <use href="#icon-b" x="180" y="20" width="20" height="20"
       transform="rotate(45 190 30)"/>
  <svg x="100" y="20" width="60" height="60">
    <use href="#icon-a" width="30" fill="#0066cc"/>
  </svg>
  <use href="#icon-a" x="20" y="20" width="40" height="40" fill="#0066cc"/>
  <rect x="236" y="156" width="4" height="4" fill="#000000"/>
  <rect y="156" width="4" height="4" fill="#000000"/>
  <rect x="236" width="4" height="4" fill="#000000"/>
  <rect width="4" height="4" fill="#000000"/>
</svg>'''


def test_use_legend(tmp_path):
    """Export the legend as PNG and check its key pixels and stability."""
    png = svg2png(USE_LEGEND, write_to=str(tmp_path / 'legend.png'))
    assert png is None  # output was written to the given file
    png = read_file(str(tmp_path / 'legend.png'))
    assert png.startswith(MAGIC_NUMBERS['PNG'])
    image = png_image(png)
    assert image.get_width() == 240
    assert image.get_height() == 160

    # Corner marks
    assert png_pixel(png, 2, 2) == BLACK
    assert png_pixel(png, 237, 2) == BLACK
    assert png_pixel(png, 2, 157) == BLACK
    assert png_pixel(png, 237, 157) == BLACK

    # Instance at (20, 20) scaled to 40x40 in theme color A, with its
    # white corner mark scaled accordingly
    assert png_pixel(png, 40, 40) == THEME_A
    assert png_pixel(png, 23, 21) == WHITE
    assert png_pixel(png, 40, 22) == THEME_A

    # Instance with only a width: height defaults to 100% of the inner
    # 60-high viewport, content is centered vertically
    assert png_pixel(png, 115, 50) == THEME_A
    assert png_pixel(png, 115, 25) == TRANSPARENT

    # Instance with only a height, explicit content color (theme B) wins
    assert png_pixel(png, 45, 115) == THEME_B
    assert png_pixel(png, 45, 140) == TRANSPARENT
    assert png_pixel(png, 63, 133) == WHITE

    # Instance with x/y combined with a rotation: the center is preserved
    assert png_pixel(png, 190, 30) == THEME_B
    assert png_pixel(png, 181, 21) == TRANSPARENT

    # Nested reference: the icon inherits theme A through both layers,
    # the badge keeps its own theme B dot
    assert png_pixel(png, 110, 110) == THEME_A
    assert png_pixel(png, 118, 102) == THEME_B

    # Last instance, drawn after all the others on the same definition
    assert png_pixel(png, 195, 115) == THEME_A

    # Empty background
    assert png_pixel(png, 150, 140) == TRANSPARENT

    # Swapping the drawing order of the instances gives the same pixels
    png_swapped = svg2png(USE_LEGEND_SWAPPED)
    assert png_pixels(png_swapped) == png_pixels(png)
