"""
Image element geometry tests.

These tests build their own local raster and SVG inputs, so they do not
depend on any network resource or on the large non-regression assets.

"""

import base64
import io

import pytest
from PIL import Image

from . import svg2png

BLUE = (0, 0, 255)
RED = (220, 20, 20)
GREEN = (20, 200, 20)
DARK_BLUE = (20, 20, 220)
YELLOW = (230, 220, 20)
MAGENTA = (230, 20, 230)
FLAT = (0, 200, 0)

TOLERANCE = 24


def close(pixel, expected, tolerance=TOLERANCE):
    """Whether two RGB colours are close enough for an antialiased sample."""
    return all(
        abs(actual - wanted) <= tolerance
        for actual, wanted in zip(pixel[:3], expected))


def render(
        tmp_path, body, width=240, height=240, name='wrapper.svg', **kwargs):
    """Render an SVG wrapper around ``body`` and return a PIL image.

    The wrapper is written next to the test inputs so that local image
    references resolve against a real file URL.

    """
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}">\n'
        f'<rect width="100%" height="100%" fill="#0000ff"/>\n'
        f'{body}\n</svg>'
    )
    path = tmp_path / name
    path.write_text(svg)
    output = svg2png(url=path, unsafe=True, **kwargs)
    return Image.open(io.BytesIO(output)).convert('RGBA')


def painted_bbox(image, background=BLUE):
    """Bounding box of the pixels that differ from the background."""
    points = [
        (x, y) for y in range(image.height) for x in range(image.width)
        if image.getpixel((x, y))[:3] != background]
    if not points:
        return None
    xs, ys = [point[0] for point in points], [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


@pytest.fixture(scope='module')
def images(tmp_path_factory):
    """Local raster inputs.

    ``flat`` is a fully opaque 64x32 image, used to check exact geometry.
    ``thumbnail`` is a 64x32 image with a two-pixel transparent border, four
    colour quadrants and a magenta center marker, used to check that
    transparent parts are composited and that the right part of the image is
    visible.

    """
    folder = tmp_path_factory.mktemp('images')

    flat = Image.new('RGBA', (64, 32), (*FLAT, 255))
    flat.save(folder / 'flat.png')

    thumbnail = Image.new('RGBA', (64, 32), (0, 0, 0, 0))
    for y in range(2, 30):
        for x in range(2, 62):
            if x < 32:
                colour = RED if y < 16 else DARK_BLUE
            else:
                colour = GREEN if y < 16 else YELLOW
            thumbnail.putpixel((x, y), (*colour, 255))
    for y in range(12, 20):
        for x in range(28, 36):
            thumbnail.putpixel((x, y), (*MAGENTA, 255))
    thumbnail.save(folder / 'thumbnail.png')

    return folder


def uri(images, name):
    return (images / name).as_uri()


def element(href, attributes):
    return f'<image {attributes} href="{href}"/>'


@pytest.mark.parametrize('preserve_aspect_ratio, samples', [
    # 64:32 content in a 160x160 viewport.  ``meet`` centers a full-width
    # band, ``slice`` fills the viewport by cropping the sides, ``none``
    # stretches both axes; in all three cases the quadrants keep their
    # relative positions.
    ('xMidYMid meet', {
        'TL': (50, 75, RED), 'TR': (150, 75, GREEN),
        'BL': (50, 125, DARK_BLUE), 'BR': (150, 125, YELLOW)}),
    ('xMidYMid slice', {
        'TL': (60, 60, RED), 'TR': (140, 60, GREEN),
        'BL': (60, 140, DARK_BLUE), 'BR': (140, 140, YELLOW)}),
    ('none', {
        'TL': (60, 80, RED), 'TR': (140, 80, GREEN),
        'BL': (60, 120, DARK_BLUE), 'BR': (140, 120, YELLOW)}),
])
def test_quadrants(
        tmp_path, images, preserve_aspect_ratio, samples):
    """Quadrants land at the right place whatever the alignment is."""
    rendered = render(tmp_path, element(
        uri(images, 'thumbnail.png'),
        'x="20" y="20" width="160" height="160" '
        f'preserveAspectRatio="{preserve_aspect_ratio}"'))
    for label, (x, y, colour) in samples.items():
        assert close(rendered.getpixel((x, y)), colour), label
    assert close(rendered.getpixel((100, 100)), MAGENTA)


def test_meet_letterboxes_and_slice_does_not(tmp_path, images):
    """``meet`` leaves free space, ``slice`` fills the viewport."""
    href = uri(images, 'flat.png')
    meet = render(tmp_path, element(
        href, 'x="20" y="20" width="160" height="160" '
        'preserveAspectRatio="xMidYMid meet"'))
    # Content is 160x80, centered: 40 transparent pixels above and below.
    assert painted_bbox(meet) == (20, 60, 179, 139)
    assert meet.getpixel((100, 50)) == (*BLUE, 255)
    assert meet.getpixel((100, 150)) == (*BLUE, 255)

    sliced = render(tmp_path, element(
        href, 'x="20" y="20" width="160" height="160" '
        'preserveAspectRatio="xMidYMid slice"'))
    # The content overflows left and right, and is clipped to the viewport.
    assert painted_bbox(sliced) == (20, 20, 179, 179)


def test_slice_alignment_keeps_requested_edge(tmp_path, images):
    """``xMinYMin`` keeps the left edge, ``xMaxYMax`` the right one."""
    href = uri(images, 'thumbnail.png')
    left = render(tmp_path, element(
        href, 'x="20" y="20" width="160" height="160" '
        'preserveAspectRatio="xMinYMin slice"'))
    assert close(left.getpixel((35, 60)), RED)
    assert close(left.getpixel((35, 140)), DARK_BLUE)
    # The right half has been cropped away, even at the right of the box.
    assert close(left.getpixel((170, 60)), RED)
    assert close(left.getpixel((170, 140)), DARK_BLUE)

    right = render(tmp_path, element(
        href, 'x="20" y="20" width="160" height="160" '
        'preserveAspectRatio="xMaxYMax slice"'))
    assert close(right.getpixel((165, 60)), GREEN)
    assert close(right.getpixel((165, 140)), YELLOW)
    # The left half has been cropped away.
    assert close(right.getpixel((35, 60)), GREEN)
    assert close(right.getpixel((35, 140)), YELLOW)


def test_none_overflow_stays_inside_the_viewport(tmp_path, images):
    """A stretched image must not paint outside of its own box."""
    rendered = render(tmp_path, element(
        uri(images, 'flat.png'),
        'x="40" y="60" width="150" height="70" preserveAspectRatio="none"'))
    assert painted_bbox(rendered) == (40, 60, 189, 129)


def test_opaque_edges_are_not_faded(tmp_path, images):
    """The last row and column of an opaque image stay opaque."""
    rendered = render(tmp_path, element(
        uri(images, 'flat.png'),
        'x="10" y="10" width="80" height="80" preserveAspectRatio="none"'),
        width=100, height=100)
    for point in ((10, 10), (89, 10), (10, 89), (89, 89)):
        assert close(rendered.getpixel(point), FLAT, tolerance=0)


@pytest.mark.parametrize('attributes, box', [
    ('x="20" y="20" width="160"', (20, 20, 179, 99)),
    ('x="20" y="20" height="80"', (20, 20, 179, 99)),
    ('x="20" y="20" width="50%" height="50%"', (20, 45, 119, 94)),
])
def test_missing_size_uses_intrinsic_ratio(
        tmp_path, images, attributes, box):
    """An unsized axis is deduced from the image ratio, not from pixels."""
    rendered = render(
        tmp_path, element(uri(images, 'flat.png'), attributes),
        width=200, height=200)
    assert painted_bbox(rendered) == box


@pytest.mark.parametrize('attributes', [
    'width="0" height="50"',
    'width="50" height="0"',
    'width="0" height="0"',
])
def test_zero_size_draws_nothing(tmp_path, images, attributes):
    rendered = render(tmp_path, element(
        uri(images, 'flat.png'), f'x="10" y="10" {attributes}'),
        width=100, height=100)
    assert painted_bbox(rendered) is None


def test_same_image_at_different_sizes_is_independent(tmp_path, images):
    """Each reference gets its own geometry, whatever came before."""
    href = uri(images, 'thumbnail.png')
    flat = uri(images, 'flat.png')
    body = (
        element(flat, 'x="0" y="0" width="160" height="80"') +
        element(flat, 'x="200" y="0" width="80" height="80" '
                'preserveAspectRatio="xMidYMid slice"') +
        element(flat, 'x="0" y="100" width="160" height="80" '
                'preserveAspectRatio="none"'))
    rendered = render(tmp_path, body, width=400, height=200)
    assert painted_bbox(rendered) == (0, 0, 279, 179)

    # The same source drawn three times, at three sizes, keeps its colours
    # at the same relative positions in each copy.
    body = (
        element(href, 'x="0" y="0" width="160" height="80"') +
        element(href, 'x="200" y="0" width="80" height="80" '
                'preserveAspectRatio="xMidYMid slice"') +
        element(href, 'x="0" y="100" width="160" height="80" '
                'preserveAspectRatio="none"'))
    rendered = render(tmp_path, body, width=400, height=200)
    assert close(rendered.getpixel((40, 20)), RED)
    assert close(rendered.getpixel((40, 120)), RED)
    assert close(rendered.getpixel((120, 60)), YELLOW)
    assert close(rendered.getpixel((120, 160)), YELLOW)
    # The sliced copy keeps its own box: nothing at x=190, full box at 240.
    assert rendered.getpixel((190, 40)) == (*BLUE, 255)
    assert close(rendered.getpixel((240, 40)), MAGENTA)


def test_transform_and_nesting(tmp_path, images):
    """Rotation, scaling and nested groups keep the image geometry."""
    href = uri(images, 'thumbnail.png')
    rotated = render(tmp_path, (
        '<g transform="translate(120,120) rotate(90)">'
        + element(href, 'x="-80" y="-40" width="160" height="80"')
        + '</g>'))
    assert close(rotated.getpixel((140, 80)), RED)
    assert close(rotated.getpixel((140, 160)), GREEN)
    assert close(rotated.getpixel((100, 80)), DARK_BLUE)
    assert close(rotated.getpixel((100, 160)), YELLOW)
    assert close(rotated.getpixel((120, 120)), MAGENTA)
    assert rotated.getpixel((10, 10)) == (*BLUE, 255)

    nested = render(tmp_path, (
        '<g transform="translate(20,20) scale(2)">'
        '<g transform="rotate(180 40 20)">'
        + element(href, 'x="0" y="0" width="80" height="40"')
        + '</g></g>'))
    assert close(nested.getpixel((140, 80)), RED)
    assert close(nested.getpixel((60, 80)), GREEN)
    assert close(nested.getpixel((140, 40)), DARK_BLUE)
    assert close(nested.getpixel((60, 40)), YELLOW)
    assert close(nested.getpixel((100, 60)), MAGENTA)

    # A nested ``svg`` viewport with its own viewBox and slice alignment.
    nested_svg = render(tmp_path, (
        '<svg x="20" y="20" width="160" height="160" '
        'viewBox="0 0 64 32" preserveAspectRatio="xMidYMid slice">'
        + element(href, 'x="0" y="0" width="64" height="32"')
        + '</svg>'))
    assert close(nested_svg.getpixel((100, 100)), MAGENTA)
    assert nested_svg.getpixel((10, 10)) == (*BLUE, 255)


def test_transparent_pixels_composite_with_background(tmp_path):
    """Alpha is composited for real: nothing is filled with black."""
    clear = Image.new('RGBA', (8, 8), (0, 0, 0, 0))
    path = tmp_path / 'clear.png'
    clear.save(path)
    rendered = render(tmp_path, element(
        path.as_uri(), 'x="10" y="10" width="40" height="40"'),
        width=60, height=60)
    assert rendered.getpixel((30, 30)) == (*BLUE, 255)

    half = Image.new('RGBA', (8, 8), (255, 255, 0, 128))
    path = tmp_path / 'half.png'
    half.save(path)
    rendered = render(tmp_path, element(
        path.as_uri(), 'x="10" y="10" width="40" height="40"'),
        width=60, height=60)
    blended = rendered.getpixel((30, 30))
    assert 100 <= blended[0] <= 155 and blended[2] >= 100


def test_palette_and_colour_key_transparency(tmp_path):
    """``tRNS`` transparency is honoured, as real alpha is."""
    palette = Image.new('P', (8, 8), 0)
    palette.putpalette([0, 0, 0] + [255, 0, 0] + [0] * 762)
    for y in range(8):
        for x in range(4, 8):
            palette.putpixel((x, y), 1)
    path = tmp_path / 'palette.png'
    palette.save(path, transparency=0)
    rendered = render(tmp_path, element(
        path.as_uri(),
        'x="4" y="4" width="32" height="32" preserveAspectRatio="none"'),
        width=40, height=40)
    assert rendered.getpixel((10, 20)) == (*BLUE, 255)
    assert close(rendered.getpixel((30, 20)), (255, 0, 0))

    keyed = Image.new('RGB', (8, 8), (0, 255, 0))
    keyed.putpixel((0, 0), (255, 0, 255))
    path = tmp_path / 'keyed.png'
    keyed.save(path, transparency=(255, 0, 255))
    rendered = render(tmp_path, element(
        path.as_uri(),
        'x="4" y="4" width="32" height="32" preserveAspectRatio="none"'),
        width=40, height=40)
    assert rendered.getpixel((5, 5)) == (*BLUE, 255)
    assert close(rendered.getpixel((20, 20)), (0, 255, 0))


@pytest.mark.parametrize('suffix', ['png', 'jpg'])
def test_exif_orientation_is_applied_before_measuring(tmp_path, suffix):
    """A rotated image keeps its displayed size and its top/bottom order."""
    image = Image.new('RGB', (64, 32), (200, 20, 20))
    for y in range(16, 32):
        for x in range(64):
            image.putpixel((x, y), (20, 20, 200))
    exif = Image.Exif()
    exif[0x0112] = 6  # Rotate 90° clockwise for display.
    path = tmp_path / f'oriented.{suffix}'
    image.save(path, exif=exif)

    # Without width and height, the intrinsic size is the displayed 32x64,
    # not the stored 64x32.
    rendered = render(tmp_path, element(
        path.as_uri(), 'x="10" y="10"'), width=120, height=120)
    assert painted_bbox(rendered) == (10, 10, 41, 73)

    # The original top (red) is now on the right.
    stretched = render(tmp_path, element(
        path.as_uri(),
        'x="10" y="10" width="32" height="64" preserveAspectRatio="none"'),
        width=120, height=120)
    assert close(stretched.getpixel((18, 40)), (20, 20, 200), tolerance=60)
    assert close(stretched.getpixel((33, 40)), (200, 20, 20), tolerance=60)


def embedded(document):
    """Build a data URL for a small SVG document."""
    return 'data:image/svg+xml;base64,' + base64.b64encode(
        document.encode()).decode()


HALVES = (
    '<rect width="50" height="100" fill="red"/>'
    '<rect x="50" width="50" height="100" fill="green"/>')


def test_embedded_svg_lands_in_its_viewport(tmp_path):
    """A referenced SVG covers exactly its viewport, with no drift."""
    href = embedded(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
        f'viewBox="0 0 100 100">{HALVES}</svg>')
    rendered = render(tmp_path, element(
        href, 'x="20" y="20" width="200" height="100" '
        'preserveAspectRatio="none"'))
    assert painted_bbox(rendered) == (20, 20, 219, 119)
    assert close(rendered.getpixel((60, 70)), (255, 0, 0))
    assert close(rendered.getpixel((180, 70)), (0, 128, 0))


def test_embedded_svg_honours_viewbox_origin(tmp_path):
    """A non-zero viewBox origin is applied once, not ignored."""
    href = embedded(
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" '
        'viewBox="10 20 100 100">'
        '<rect x="10" y="20" width="100" height="100" fill="red"/>'
        '<rect x="10" y="20" width="10" height="10" fill="lime"/>'
        '</svg>')
    rendered = render(tmp_path, element(
        href, 'x="20" y="20" width="200" height="100" '
        'preserveAspectRatio="none"'))
    # The viewBox origin is (10, 20): its first corner, marked in lime, must
    # land at the top-left corner of the viewport, not one viewBox-origin
    # away from it.
    assert painted_bbox(rendered) == (20, 20, 219, 119)
    assert close(rendered.getpixel((23, 23)), (0, 255, 0))
    assert close(rendered.getpixel((100, 70)), (255, 0, 0))


def test_embedded_svg_uses_its_own_preserve_aspect_ratio(tmp_path):
    """The referenced root's own alignment maps its viewBox into its box."""
    href = embedded(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" '
        'viewBox="0 0 100 100" preserveAspectRatio="xMidYMid slice">'
        '<rect width="100" height="25" fill="lime"/>'
        '<rect y="25" width="100" height="50" fill="red"/>'
        '</svg>')
    rendered = render(tmp_path, element(
        href, 'x="20" y="20" width="200" height="100" '
        'preserveAspectRatio="none"'))
    # The root is 200x100 and its 100x100 viewBox is sliced into it: scale 2,
    # so the top quarter of the viewBox keeps its full height at the top.
    # Treating the viewBox as a plain independent-axis scale would crop it.
    assert painted_bbox(rendered) == (20, 20, 219, 119)
    assert close(rendered.getpixel((100, 40)), (0, 255, 0))
    assert close(rendered.getpixel((100, 90)), (255, 0, 0))


def test_embedded_svg_overflow(tmp_path):
    """An ``overflow="visible"`` root may paint outside its content box."""
    body = (
        '<rect x="0" y="0" width="25" height="25" fill="lime"/>'
        '<rect x="50" y="0" width="50" height="50" fill="red"/>')

    def document(overflow):
        attribute = ' overflow="visible"' if overflow else ''
        return embedded(
            '<svg xmlns="http://www.w3.org/2000/svg" width="50" height="50"'
            f'{attribute}>{body}</svg>')

    for overflow, expected in ((True, (150, 100, 299, 199)),
                               (False, (150, 100, 199, 149))):
        rendered = render(tmp_path, element(
            document(overflow),
            'x="100" y="100" width="200" height="100" '
            'preserveAspectRatio="xMidYMid meet"'),
            width=300, height=300)
        assert painted_bbox(rendered) == expected


def test_embedded_svg_missing_size_and_opacity(tmp_path):
    """A referenced SVG with no size uses the viewport, and honours opacity."""
    href = embedded(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" '
        '><rect width="100%" height="100%" fill="red"/></svg>')
    rendered = render(tmp_path, element(
        href, 'x="20" y="20" width="200" height="100" opacity="0.5"'),
        width=300, height=200)
    pixel = rendered.getpixel((120, 70))
    assert 96 <= pixel[0] <= 160 and pixel[2] >= 90
    assert rendered.getpixel((10, 10)) == (*BLUE, 255)


def test_defer_keyword(tmp_path, images):
    """``defer`` is accepted and does not change the alignment."""
    href = uri(images, 'flat.png')
    with_defer = render(tmp_path, element(
        href, 'x="20" y="20" width="160" height="160" '
        'preserveAspectRatio="defer xMaxYMax meet"'))
    without = render(tmp_path, element(
        href, 'x="20" y="20" width="160" height="160" '
        'preserveAspectRatio="xMaxYMax meet"'))
    assert painted_bbox(with_defer) == painted_bbox(without) == (
        20, 100, 179, 179)


def test_repeated_renders_are_stable(tmp_path, images):
    """Several references and several conversions share no mutable state."""
    href = uri(images, 'thumbnail.png')
    body = (
        element(href, 'x="10" y="10" width="80" height="80" '
                'preserveAspectRatio="xMidYMid slice"') +
        element(href, 'x="100" y="10" width="80" height="80" '
                'preserveAspectRatio="xMidYMid slice"'))
    first = render(tmp_path, body, width=200, height=200)
    second = render(tmp_path, body, width=200, height=200)
    assert first.tobytes() == second.tobytes()
    left = [
        first.getpixel((x, y)) for y in range(10, 90) for x in range(10, 90)]
    right = [
        first.getpixel((x + 90, y))
        for y in range(10, 90) for x in range(10, 90)]
    assert all(close(a, b, tolerance=2) for a, b in zip(left, right))
