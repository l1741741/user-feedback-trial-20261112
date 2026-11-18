"""Tests for the ``negate_colors`` and ``invert_images`` options.

The tests build their own raster assets in ``tmp_path`` so that they do not
depend on binary fixtures or on the network.

"""

import io
from struct import pack
from zlib import compress, crc32

import pytest
from PIL import Image

from . import svg2png
from .colors import color, negate_color
from .image import invert_image, prepare_image
from .parser import Tree
from .surface import PNGSurface

SVG_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" '
    'width="240" height="160">{body}</svg>')


def render(body, base='', **options):
    """Render an SVG body and return the decoded RGBA image."""
    svg = SVG_TEMPLATE.format(body=body).encode()
    png = svg2png(
        bytestring=svg, unsafe=True, url='file://' + base + '/',
        **options)
    return Image.open(io.BytesIO(png)).convert('RGBA')


def png_bytes(image, **options):
    """Save ``image`` as PNG bytes."""
    buffer = io.BytesIO()
    image.save(buffer, 'PNG', **options)
    return buffer.getvalue()


def write_png(path, image, **options):
    path.write_bytes(png_bytes(image, **options))
    return path.as_uri()


@pytest.fixture
def assets(tmp_path):
    """Create the raster assets used by the tests."""
    rgba = Image.new('RGBA', (64, 64), (90, 10, 160, 0))
    for y in range(32):
        for x in range(32):
            rgba.putpixel((x, y), (200, 30, 40, 255))
        for x in range(32, 64):
            rgba.putpixel((x, y), (10, 220, 50, 128))
    for y in range(32, 64):
        for x in range(32, 64):
            rgba.putpixel((x, y), (250, 240, 0, 255))
    write_png(tmp_path / 'rgba.png', rgba)

    palette = Image.new('P', (64, 64), 0)
    palette.putpalette([255, 0, 0, 0, 0, 255] + [0, 0, 0] * 254)
    palette.paste(1, (32, 0, 64, 64))
    palette.save(tmp_path / 'palette.png', transparency=1)

    write_png(tmp_path / 'rgb.png', Image.new('RGB', (64, 64), (30, 120, 200)))

    colour_key = Image.new('RGB', (64, 64), (255, 0, 0))
    colour_key.paste((0, 0, 0), (32, 0, 64, 64))
    colour_key.save(
        tmp_path / 'colour_key.png', transparency=(0, 0, 0))

    oriented = Image.new('RGB', (48, 24), (10, 200, 10))
    oriented.paste((220, 20, 20), (0, 0, 24, 24))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise
    oriented.save(
        tmp_path / 'oriented.jpg', 'JPEG', quality=100, exif=exif)
    oriented.save(tmp_path / 'oriented.png', 'PNG', exif=exif)

    write_png(tmp_path / 'tile.png', rgba)
    Image.new('CMYK', (32, 32), (0, 255, 255, 0)).save(
        tmp_path / 'cmyk.tif')
    return tmp_path


def image_svg(assets, name, **attributes):
    """SVG snippet drawing one asset."""
    size = ' '.join(
        f'{key}="{value}"' for key, value in attributes.items())
    return (
        f'<image {size} xlink:href="{assets / name}"/>')


def close(actual, expected, tolerance=3):
    """Compare RGBA tuples with a tolerance for resampling."""
    return all(
        abs(a - e) <= tolerance for a, e in zip(actual, expected))


def test_vectors_follow_negate_colors_only(assets):
    body = (
        '<rect x="0" y="0" width="120" height="160" fill="#ff8000"/>'
        '<rect x="120" y="0" width="120" height="160" fill="#3366cc"/>'
        f'{image_svg(assets, "rgb.png", x=0, y=0, width="1", height="1")}')

    normal = render(body)
    negated = render(body, negate_colors=True)
    inverted = render(body, invert_images=True)

    assert close(normal.getpixel((60, 80)), (255, 128, 0, 255))
    assert close(negated.getpixel((60, 80)), (0, 127, 255, 255))
    assert inverted.getpixel((60, 80)) == normal.getpixel((60, 80))
    assert close(negated.getpixel((180, 80)), (204, 153, 51, 255))


def test_semi_transparent_vector_keeps_alpha(assets):
    body = (
        '<rect width="240" height="160" fill="#ff0000" '
        'fill-opacity="0.5"/>')
    normal = render(body)
    negated = render(body, negate_colors=True)
    # On a transparent canvas both are red and cyan at 50% opacity: the
    # alpha channel must be identical, only the RGB channels are negated.
    assert abs(normal.getpixel((120, 80))[3] -
               negated.getpixel((120, 80))[3]) <= 1
    assert normal.getpixel((120, 80))[:3] == (255, 0, 0)
    assert negated.getpixel((120, 80))[:3] == (0, 255, 255)


def test_gradient_and_pattern_follow_negate_colors(assets):
    body = (
        '<defs>'
        '<linearGradient id="g" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#ff0000"/>'
        '<stop offset="1" stop-color="#0000ff"/>'
        '</linearGradient>'
        '<pattern id="p" width="40" height="40" patternUnits="userSpaceOnUse">'
        '<rect width="20" height="20" fill="#ff8000"/>'
        '<image x="20" y="20" width="20" height="20" '
        f'xlink:href="{assets / "rgba.png"}"/>'
        '</pattern>'
        '</defs>'
        '<rect width="120" height="160" fill="url(#g)"/>'
        '<rect x="120" width="120" height="160" fill="url(#p)"/>')

    normal = render(body)
    negated = render(body, negate_colors=True)
    inverted = render(body, invert_images=True)

    # Gradient stops are vectors: only negate_colors changes them.
    assert close(negated.getpixel((5, 10)),
                 tuple(255 - value for value in normal.getpixel((5, 10))[:3])
                 + (255,))
    assert inverted.getpixel((5, 10)) == normal.getpixel((5, 10))

    # Pattern vector rectangles keep following negate_colors...
    normal_swatch = normal.getpixel((130, 10))
    negated_swatch = negated.getpixel((130, 10))
    assert close(negated_swatch, (
        255 - normal_swatch[0], 255 - normal_swatch[1],
        255 - normal_swatch[2], 255))
    # ... and the raster tile inside the pattern follows invert_images.
    normal_tile = normal.getpixel((150, 30))
    inverted_tile = inverted.getpixel((150, 30))
    assert close(inverted_tile, (
        255 - normal_tile[0], 255 - normal_tile[1], 255 - normal_tile[2],
        255))
    assert negated.getpixel((150, 30)) == normal_tile


def test_pattern_subsurface_inherits_both_options(assets):
    # Patterns are visible artwork: their vector and raster content must
    # follow the same options as the rest of the document.
    body = (
        '<defs><pattern id="p" width="40" height="40" '
        'patternUnits="userSpaceOnUse">'
        '<rect width="20" height="40" fill="#ff8000"/>'
        '<image x="20" y="0" width="20" height="40" '
        f'xlink:href="{assets / "rgb.png"}"/>'
        '</pattern></defs>'
        '<rect width="240" height="160" fill="url(#p)"/>')

    normal = render(body)
    negated = render(body, negate_colors=True)
    inverted = render(body, invert_images=True)

    normal_vector, normal_raster = normal.getpixel((5, 20)), normal.getpixel(
        (30, 20))
    assert close(normal_vector, (255, 128, 0, 255))
    assert close(normal_raster, (30, 120, 200, 255))
    assert close(negated.getpixel((5, 20)), (0, 127, 255, 255))
    assert negated.getpixel((30, 20)) == normal_raster
    assert inverted.getpixel((5, 20)) == normal_vector
    assert close(inverted.getpixel((30, 20)), (225, 135, 55, 255))


def test_images_keep_alpha_and_honour_each_flag(assets):
    picture = image_svg(
        assets, 'rgba.png', x=0, y=0, width=128, height=128)
    body = f'<rect width="240" height="160" fill="#101820"/>{picture}'

    normal = render(body)
    negated = render(body, negate_colors=True)
    inverted = render(body, invert_images=True)
    both = render(body, negate_colors=True, invert_images=True)

    points = {
        'opaque': ((30, 30), (200, 30, 40, 255)),
        'semi': ((100, 30), None),
        'transparent': ((30, 100), None),
        'background': ((140, 8), None),
        'opaque second': ((100, 100), (250, 240, 0, 255)),
    }
    opaque = points['opaque'][1]
    assert close(normal.getpixel(points['opaque'][0]), opaque)
    assert close(negated.getpixel(points['opaque'][0]), opaque)
    assert close(inverted.getpixel(points['opaque'][0]),
                 (55, 225, 215, 255))
    assert close(inverted.getpixel(points['opaque second'][0]),
                 (5, 15, 255, 255))
    # Transparent pixels never become black: they show exactly the same
    # color as a background-only pixel of the same render (the background
    # itself is negated by ``negate_colors``, since it is a vector fill).
    for image in (normal, negated, inverted, both):
        transparent = image.getpixel(points['transparent'][0])
        background = image.getpixel(points['background'][0])
        assert transparent == background
        assert transparent[3] == 255
    # Semi-transparent pixels keep their alpha while colors are inverted.
    assert normal.getpixel(points['semi'][0])[3] == 255  # over background
    assert inverted.getpixel(points['semi'][0])[3] == 255


def test_alpha_preserved_without_background(assets):
    body = image_svg(assets, 'rgba.png', width='64', height='64')
    for options in (
            {}, {'negate_colors': True}, {'invert_images': True},
            {'negate_colors': True, 'invert_images': True}):
        image = render(body, **options)
        assert image.getpixel((16, 16))[3] == 255
        assert image.getpixel((48, 16))[3] == 128
        assert image.getpixel((16, 48))[3] == 0
        assert image.getpixel((48, 48))[3] == 255
        assert image.getpixel((48, 16))[:3] == (
            (245, 36, 205) if options.get('invert_images') else (10, 219, 50))


@pytest.mark.parametrize('name', ['palette.png', 'colour_key.png'])
def test_indexed_and_colour_key_transparency(assets, name):
    body = image_svg(assets, name, width='64', height='64')
    normal = render(body)
    inverted = render(body, invert_images=True)

    assert normal.getpixel((16, 16)) == (255, 0, 0, 255)
    assert normal.getpixel((48, 16))[3] == 0
    assert inverted.getpixel((16, 16)) == (0, 255, 255, 255)
    assert inverted.getpixel((48, 16))[3] == 0


def test_exif_orientation_is_applied_before_inversion(assets):
    for name in ('oriented.jpg', 'oriented.png'):
        body = (
            '<rect width="240" height="160" fill="white"/>'
            f'{image_svg(assets, name, x=0, y=0, width="96", height="160")}')
        normal = render(body)
        inverted = render(body, invert_images=True)
        # Orientation 6 turns the 48×24 red/green strip into a 24×48 image
        # with red on top and green at the bottom: 240×160 viewport, the
        # image keeps its ratio and is centred horizontally.
        for image in (normal,):
            top = image.getpixel((48, 40))
            bottom = image.getpixel((48, 120))
            assert (top[0] > 180) and (top[1] < 80), (name, top)
            assert (bottom[1] > 150) and (bottom[0] < 90), (name, bottom)
        # Inverted colors keep the same geometry: cyan band on top,
        # magenta band at the bottom.
        assert close(inverted.getpixel((48, 40)), (35, 235, 235, 255), 8)
        assert close(inverted.getpixel((48, 120)), (245, 54, 245, 255), 8)


def test_options_off_match_default_and_are_repeatable(assets):
    picture = image_svg(
        assets, 'rgba.png', x=10, y=10, width=96, height=96)
    body = f'<rect width="240" height="160" fill="#204060"/>{picture}'
    svg = SVG_TEMPLATE.format(body=body).encode()
    options = {'url': 'file://' + str(assets) + '/', 'unsafe': True}
    default = svg2png(bytestring=svg, **options)
    off = svg2png(
        bytestring=svg, negate_colors=False, invert_images=False, **options)
    assert default == off

    first = svg2png(bytestring=svg, invert_images=True, **options)
    second = svg2png(bytestring=svg, invert_images=True, **options)
    third = svg2png(bytestring=svg, invert_images=True, **options)
    assert first == second == third
    # Choosing a different combination afterwards is not affected either.
    assert svg2png(bytestring=svg, **options) == default


def test_double_inversion_restores_input(assets):
    picture = image_svg(assets, 'rgb.png', x=0, y=0, width=64, height=64)
    body = f'<rect width="240" height="160" fill="#204060"/>{picture}'
    normal = render(body)
    twice = render(body, invert_images=True)
    # Inverting once and then back gives the original image pixel back.
    double = invert_image(invert_image(normal))
    assert double.getpixel((32, 32)) == normal.getpixel((32, 32))


def test_image_functions_do_not_mutate_input(tmp_path):
    rgba = Image.open(io.BytesIO(png_bytes(
        Image.new('RGBA', (2, 1), (200, 30, 40, 128))))).convert('RGBA')
    rgba.putpixel((1, 0), (0, 0, 0, 0))
    before = [rgba.getpixel((0, 0)), rgba.getpixel((1, 0))]
    inverted = invert_image(rgba)
    assert [rgba.getpixel((0, 0)), rgba.getpixel((1, 0))] == before
    assert inverted.getpixel((0, 0)) == (55, 225, 215, 128)
    assert inverted.getpixel((1, 0))[3] == 0
    # Nothing stays aliased between the input and the result.
    rgba.putpixel((0, 0), (0, 255, 0, 255))
    assert inverted.getpixel((0, 0)) == (55, 225, 215, 128)


def test_custom_map_image_receives_a_copy(assets):
    calls = []

    def mapper(image):
        calls.append(image.mode)
        image.putpixel((0, 0), (1, 2, 3, 4))  # must not leak anywhere
        return image

    picture = image_svg(assets, 'rgb.png', width='64', height='64')
    svg = SVG_TEMPLATE.format(body=picture).encode()
    tree = Tree(
        bytestring=svg, url_fetcher=_fetcher(assets), unsafe=True)
    buffer = io.BytesIO()
    png_surface = PNGSurface(tree, buffer, 96, map_image=mapper)
    png_surface.finish()
    assert calls
    image = Image.open(io.BytesIO(buffer.getvalue())).convert('RGBA')
    assert image.getpixel((10, 10)) == (30, 120, 200, 255)


def _fetcher(assets):
    def fetch(url, resource_type):
        return (assets / url.rsplit('/', 1)[-1]).read_bytes()
    return fetch


def test_clipped_and_scaled_reuse(assets):
    clipped = image_svg(
        assets, 'rgb.png', x=0, y=0, width=96, height=96)
    clipped = clipped.replace(
        '/>', ' clip-path="url(#c)"/>')
    scaled = image_svg(assets, 'rgb.png', x=120, y=0, width=40, height=40)
    body = (
        '<defs><clipPath id="c" clipPathUnits="userSpaceOnUse">'
        '<circle cx="48" cy="48" r="40"/></clipPath></defs>'
        '<rect width="240" height="160" fill="#101820"/>'
        f'{clipped}{scaled}')
    normal = render(body)
    inverted = render(body, invert_images=True)

    assert close(normal.getpixel((48, 48)), (30, 120, 200, 255))
    assert close(inverted.getpixel((48, 48)), (225, 135, 55, 255))
    # Outside the clip the background remains untouched.
    assert normal.getpixel((4, 4)) == (16, 24, 32, 255)
    assert inverted.getpixel((4, 4)) == (16, 24, 32, 255)
    # The second, scaled copy is inverted as well.
    assert close(inverted.getpixel((140, 20)), (225, 135, 55, 255))


def test_cmyk_image(assets):
    body = image_svg(assets, 'cmyk.tif', width='32', height='32')
    normal = render(body)
    inverted = render(body, invert_images=True)
    assert close(normal.getpixel((16, 16)), (255, 0, 0, 255))
    assert close(inverted.getpixel((16, 16)), (0, 255, 255, 255))


def _gray16_png(path):
    def chunk(chunk_type, data):
        content = chunk_type + data
        return (
            pack('>I', len(data)) + content + pack('>I', crc32(content)))

    rows = b''
    for _ in range(16):
        row = b'\x00'
        for x in range(16):
            row += pack('>H', 1000 if x < 8 else 65535)
        rows += row
    path.write_bytes(
        b'\x89PNG\r\n\x1a\n' +
        chunk(b'IHDR', pack('>IIBBBBB', 16, 16, 16, 0, 0, 0, 0)) +
        chunk(b'IDAT', compress(rows)) +
        chunk(b'IEND', b''))
    return path


def test_16_bit_grayscale_is_scaled_not_clamped(tmp_path):
    path = _gray16_png(tmp_path / 'gray16.png')
    body = (
        '<rect width="240" height="160" fill="white"/>'
        f'<image x="0" y="0" width="64" height="64" xlink:href="{path}"/>')
    normal = render(body)
    inverted = render(body, invert_images=True)
    assert close(normal.getpixel((10, 10)), (3, 3, 3, 255))
    assert close(normal.getpixel((50, 10)), (255, 255, 255, 255))
    assert close(inverted.getpixel((10, 10)), (252, 252, 252, 255))
    assert close(inverted.getpixel((50, 10)), (0, 0, 0, 255))


def test_unsupported_modes_raise_a_clear_error():
    for mode in ('F', 'HSV', 'LAB', 'YCbCr'):
        image = Image.new(mode, (2, 2))
        with pytest.raises(ValueError, match='Unsupported image mode'):
            prepare_image(image)


def test_negate_color_helper():
    assert negate_color((0.2, 0.4, 0.6, 0.35)) == (0.8, 0.6, 0.4, 0.35)
    assert negate_color((0, 0, 0, 0)) == (1, 1, 1, 0)
    assert negate_color((1, 1, 1, 1)) == (0, 0, 0, 1)


def test_rgba_color_parsing_keeps_alpha_fraction():
    assert color('rgba(255, 0, 0, 0.5)') == (1, 0, 0, 0.5)
    assert color('rgba(255, 0, 0, 50%)') == (1, 0, 0, 0.5)
    assert color('rgba(100%, 0%, 0%, 0.25)') == (1, 0, 0, 0.25)
    assert color('rgba(0, 0, 0, 0)') == (0, 0, 0, 0)


def test_cli_flags(tmp_path):
    svg = tmp_path / 'input.svg'
    svg.write_bytes(SVG_TEMPLATE.format(
        body='<rect width="240" height="160" fill="#ff8000"/>').encode())

    output = tmp_path / 'normal.png'
    from .__main__ import main
    main([str(svg), '-f', 'png', '-o', str(output)])
    normal = Image.open(output).convert('RGBA')
    assert normal.getpixel((20, 20)) == (255, 128, 0, 255)

    main([str(svg), '-f', 'png', '-n', '-o', str(output)])
    negated = Image.open(output).convert('RGBA')
    assert negated.getpixel((20, 20)) == (0, 127, 255, 255)

    main([str(svg), '-f', 'png', '-i', '-o', str(output)])
    inverted = Image.open(output).convert('RGBA')
    assert inverted.getpixel((20, 20)) == (255, 128, 0, 255)
