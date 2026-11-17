"""
Regression tests for the ``negate_colors`` and ``invert_images`` options.

These tests render a layout mixing vector shapes, a semi-transparent RGBA
PNG, a palette PNG with a transparent index, an RGB JPEG and a JPEG with
EXIF orientation, then check the actual output pixels for each combination
of the two options.

"""

import io
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from PIL import Image, ImageOps

from . import svg2png
from .__main__ import main
from .surface import PNGSurface

TOL = 3  # cairo premultiplied-alpha roundtrip tolerance


def make_rgba_png():
    """40x30 RGBA: left half alpha=128, right half opaque, transparent corner."""
    image = Image.new('RGBA', (40, 30), (10, 20, 230, 255))
    for x in range(20):
        for y in range(30):
            image.putpixel((x, y), (200, 30, 40, 128))
    for x in range(10):
        for y in range(10):
            image.putpixel((x, y), (0, 0, 0, 0))
    return image


def make_palette_png():
    """40x30 palette PNG, index 1 declared (semi-)transparent."""
    image = Image.new('P', (40, 30))
    image.putpalette([0, 128, 255, 250, 240, 10] + [0, 0, 0] * 254)
    for x in range(40):
        for y in range(30):
            image.putpixel((x, y), 0 if x < 20 else 1)
    image.info['transparency'] = bytes([255, 64])
    return image


def make_rgb_jpeg():
    """40x30 RGB JPEG."""
    return Image.new('RGB', (40, 30), (10, 200, 60))


def make_exif_jpeg():
    """30x20 JPEG, left red / right blue, EXIF orientation 6 (rotate 90 CW)."""
    image = Image.new('RGB', (30, 20))
    for x in range(30):
        for y in range(20):
            image.putpixel((x, y), (255, 0, 0) if x < 15 else (0, 0, 255))
    exif = Image.Exif()
    exif[0x0112] = 6  # Orientation: rotate 90° clockwise for display
    return image, exif


def to_bytes(image, fmt, **kwargs):
    buffer = io.BytesIO()
    image.save(buffer, fmt, **kwargs)
    return buffer.getvalue()


def data_url(mime, payload):
    import base64
    return f'data:{mime};base64,{base64.b64encode(payload).decode()}'


RGBA_BYTES = to_bytes(make_rgba_png(), 'PNG')
PALETTE_BYTES = to_bytes(make_palette_png(), 'PNG')
RGB_JPEG_BYTES = to_bytes(make_rgb_jpeg(), 'JPEG', quality=95)
_exif_image, _exif = make_exif_jpeg()
EXIF_JPEG_BYTES = to_bytes(_exif_image, 'JPEG', quality=95, exif=_exif)

SVG = f'''<svg xmlns="http://www.w3.org/2000/svg" width="240" height="170">
  <rect x="0" y="0" width="60" height="40" fill="#3c78b4"/>
  <rect x="0" y="45" width="60" height="40" fill="none"
        stroke="#112233" stroke-width="4"/>
  <image href="{data_url('image/png', RGBA_BYTES)}"
         x="70" y="0" width="40" height="30"/>
  <image href="{data_url('image/png', PALETTE_BYTES)}"
         x="120" y="0" width="40" height="30"/>
  <image href="{data_url('image/jpeg', RGB_JPEG_BYTES)}"
         x="170" y="0" width="40" height="30"/>
  <image href="{data_url('image/jpeg', EXIF_JPEG_BYTES)}"
         x="70" y="40" width="20" height="30"/>
  <image href="{data_url('image/png', RGBA_BYTES)}"
         x="170" y="40" width="80" height="60" preserveAspectRatio="none"/>
  <image href="{data_url('image/png', RGBA_BYTES)}" x="70" y="80"
         width="40" height="30" clip="rect(0px, 20px, 0px, 0px)"/>
</svg>'''


def render(svg=SVG, **kwargs):
    return Image.open(io.BytesIO(svg2png(svg, **kwargs)))


def assert_pixel(image, xy, expected, tol=TOL):
    actual = image.getpixel(xy)
    assert len(actual) == len(expected)
    for channel_actual, channel_expected in zip(actual, expected):
        assert abs(channel_actual - channel_expected) <= tol, (
            f'pixel {xy}: got {actual}, expected {expected}')


def complement(rgba):
    return tuple(255 - channel for channel in rgba[:3]) + rgba[3:]


def test_options_off_keep_output_identical():
    """Disabled (or absent) options give byte-identical output."""
    plain = svg2png(SVG)
    assert svg2png(SVG) == plain
    assert svg2png(SVG, negate_colors=False, invert_images=False) == plain


def test_negate_colors_affects_vectors_only():
    """negate_colors complements vector colors and leaves rasters alone."""
    base, negated = render(), render(negate_colors=True)
    assert_pixel(negated, (30, 20), (195, 135, 75, 255))  # #3c78b4 complement
    assert_pixel(negated, (1, 60), (238, 221, 204, 255))  # #112233 complement
    # Raster images untouched
    assert_pixel(negated, (95, 15), base.getpixel((95, 15)), tol=0)
    assert_pixel(negated, (130, 15), base.getpixel((130, 15)), tol=0)


def test_invert_images_affects_rasters_only():
    """invert_images complements raster pixels and leaves vectors alone."""
    base, inverted = render(), render(invert_images=True)
    assert_pixel(inverted, (30, 20), base.getpixel((30, 20)), tol=0)
    # Opaque and semi-transparent RGBA pixels: RGB inverted, alpha kept
    assert_pixel(inverted, (95, 15), (245, 235, 25, 255))
    assert_pixel(inverted, (75, 15), (55, 225, 215, 128))
    # Palette PNG: palette colors inverted, transparency index untouched
    assert_pixel(inverted, (130, 15), (255, 127, 0, 255))
    assert_pixel(inverted, (150, 15), (5, 15, 245, 64))


def test_combined_options():
    """Both options combine: vectors negated and rasters inverted, once."""
    both = render(negate_colors=True, invert_images=True)
    assert_pixel(both, (30, 20), (195, 135, 75, 255))
    assert_pixel(both, (95, 15), (245, 235, 25, 255))
    assert_pixel(both, (75, 15), (55, 225, 215, 128))


def test_transparent_pixels_are_not_flattened():
    """Fully transparent pixels keep alpha 0 in every combination."""
    for kwargs in (
            {}, {'negate_colors': True}, {'invert_images': True},
            {'negate_colors': True, 'invert_images': True}):
        image = render(**kwargs)
        assert image.getpixel((72, 2))[3] == 0
        # Semi-transparent pixel keeps its alpha, never becomes opaque
        assert abs(image.getpixel((75, 15))[3] - 128) <= TOL


def test_no_double_inversion_on_repeat_and_reuse():
    """Images are inverted exactly once, even when reused or re-exported."""
    base = render()
    inverted_bytes = svg2png(SVG, invert_images=True)
    # Repeating the export gives the same bytes (no accumulated inversion)
    assert svg2png(SVG, invert_images=True) == inverted_bytes
    inverted = Image.open(io.BytesIO(inverted_bytes))
    # Reused image (drawn scaled at 170,40 and clipped at 70,80) is
    # inverted once per paint, identically
    assert_pixel(inverted, (200, 60), (55, 225, 215, 128))
    assert_pixel(inverted, (75, 95), (55, 225, 215, 128))
    # Clipped-away part of the third copy stays transparent
    assert inverted.getpixel((100, 95))[3] == 0
    # A pixel is the complement of the normal render, not the original
    assert_pixel(inverted, (95, 15), complement(base.getpixel((95, 15))))
    # Exporting without options afterwards is unaffected
    assert svg2png(SVG) == svg2png(SVG)


def test_exif_orientation_geometry_preserved():
    """EXIF orientation is applied once; inversion doesn't move pixels."""
    expected = ImageOps.exif_transpose(
        Image.open(io.BytesIO(EXIF_JPEG_BYTES))).convert('RGB')
    assert expected.size == (20, 30)  # rotated: 30x20 becomes 20x30
    base, inverted = render(), render(invert_images=True)
    # The red left half becomes the top half after the 90° CW rotation
    assert_pixel(base, (80, 45), (255, 0, 0, 255), tol=10)
    assert_pixel(base, (80, 65), (0, 0, 255, 255), tol=10)
    assert_pixel(inverted, (80, 45), (0, 255, 255, 255), tol=10)
    assert_pixel(inverted, (80, 65), (255, 255, 0, 255), tol=10)


def test_caller_data_not_modified():
    """Rendering with the options never modifies the caller's objects."""
    image = make_rgba_png()
    before = to_bytes(image, 'PNG')
    pixels_before = image.tobytes()
    svg = SVG
    svg2png(svg, negate_colors=True, invert_images=True)
    assert to_bytes(image, 'PNG') == before
    assert image.tobytes() == pixels_before
    assert svg is SVG


def test_local_files_with_scoped_fetcher(tmp_path):
    """Local images render through a scoped fetcher, without unsafe=True.

    The color options don't widen resource access: with the default
    fetcher, non-data URLs are still blocked and render as blank.

    """
    (tmp_path / 'pixel.png').write_bytes(RGBA_BYTES)
    (tmp_path / 'layout.svg').write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="30">'
        '<image href="pixel.png" x="0" y="0" width="40" height="30"/>'
        '</svg>')

    def scoped_fetcher(url, resource_type=None):
        path = Path(unquote(urlparse(url).path)).resolve()
        if path.is_file() and path.parent == tmp_path.resolve():
            return path.read_bytes()
        raise ValueError(f'resource not allowed: {url}')

    image = Image.open(io.BytesIO(PNGSurface.convert(
        url=str(tmp_path / 'layout.svg'), url_fetcher=scoped_fetcher,
        invert_images=True)))
    assert_pixel(image, (35, 15), (245, 235, 25, 255))

    # Default safe fetcher: local file is not read, options don't change that
    image = Image.open(io.BytesIO(
        PNGSurface.convert(url=str(tmp_path / 'layout.svg'))))
    assert image.getpixel((35, 15))[3] == 0


def test_cli_options(tmp_path):
    """The -n and -i command-line options match the Python API."""
    svg_path = tmp_path / 'layout.svg'
    svg_path.write_text(SVG)
    png_path = tmp_path / 'out.png'
    main([str(svg_path), '-f', 'png', '-n', '-i', '-o', str(png_path)])
    assert png_path.read_bytes() == svg2png(
        SVG, negate_colors=True, invert_images=True)


@pytest.mark.parametrize('mode', ['1', 'L', 'LA', 'RGB', 'RGBA', 'P', 'CMYK'])
def test_invert_image_modes(mode):
    """invert_image gives stable results for common PIL modes."""
    from .image import invert_image
    if mode == 'P':
        image = Image.new('P', (2, 1))
        image.putpalette([10, 20, 30, 200, 210, 220] + [0, 0, 0] * 254)
        image.putpixel((0, 0), 0)
        image.putpixel((1, 0), 1)
        image.info['transparency'] = 1
    elif mode == 'CMYK':
        image = Image.new('CMYK', (2, 1))
        image.putpixel((0, 0), (0, 128, 200, 40))
        image.putpixel((1, 0), (255, 0, 10, 200))
    else:
        bands = len(mode) if mode != '1' else 1
        image = Image.new(mode, (2, 1))
        values = {'1': (255, 0), 'L': (10, 200), 'LA': ((10, 128), (200, 64)),
                  'RGB': ((10, 20, 30), (200, 210, 220)),
                  'RGBA': ((10, 20, 30, 128), (200, 210, 220, 64))}[mode]
        image.putpixel((0, 0), values[0])
        image.putpixel((1, 0), values[1])
        del bands
    inverted = invert_image(image)
    # Input image and mode are preserved, inversion is an involution
    assert inverted.mode == image.mode
    if mode == 'P':
        assert inverted.info.get('transparency') == 1
        assert inverted.getpalette()[:6] == [245, 235, 225, 55, 45, 35]
        assert image.getpalette()[:6] == [10, 20, 30, 200, 210, 220]
    else:
        twice = invert_image(inverted)
        assert twice.tobytes() == image.tobytes()
