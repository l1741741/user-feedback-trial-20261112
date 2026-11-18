"""
Tests for SVG documents embedded in ``<image>`` elements.

Embedded documents must be parsed and rendered with their own coordinate
system, styles and definitions, then mapped into the ``<image>`` region of the
outer document.  These tests are offline: all the resources are found in the
``test_nested_svg`` folder, and the expected values are read from the actual
PNG output.

"""

import base64
import io
from pathlib import Path

import cairocffi as cairo
import pytest
from PIL import Image

from . import svg2png

CURRENT_FOLDER = Path(__file__).resolve().parent.parent / 'test_nested_svg'


def render(name, **options):
    """Render a fixture of this folder to a PNG and return a Pillow image."""
    png = svg2png(url=str(CURRENT_FOLDER / name), **options)
    return Image.open(io.BytesIO(png)).convert('RGBA')


def data_url(svg):
    """Embed ``svg`` in a data URL usable by an ``<image>`` element."""
    return (
        'data:image/svg+xml;base64,' +
        base64.b64encode(svg.encode('utf-8')).decode('ascii'))


def pixel(image, x, y):
    """Return the ``(red, green, blue)`` of a pixel."""
    return image.getpixel((x, y))[:3]


def assert_color(image, x, y, expected, tolerance=24):
    """Check that a pixel is close to ``expected``."""
    actual = pixel(image, x, y)
    assert all(
        abs(channel - expected_channel) <= tolerance
        for channel, expected_channel in zip(actual, expected)), (
            f'pixel {x},{y} is {actual}, expected {expected}')


requires_cairo = pytest.mark.skipif(
    cairo.version_info < (1, 10), reason='needs a recent Cairo')


def test_embedded_ids_and_colors_do_not_leak():
    """Two embedded icons with the same ids keep their own colors.

    Both icons define a ``gradient`` and a ``clip`` with the same ids but with
    different colors and geometries.  The outer document defines its own
    ``gradient`` and ``clip`` too, and draws with them *after* the images.

    Before the fix, the icons' ``gradient`` replaced the outer one and the
    icons' ``clip`` was kept in the outer state, so the outer shape drawn
    after the images was painted with the last icon's color and clipped by
    the last icon's circle.

    """
    image = render('main.svg', unsafe=True)

    # Each icon uses its own gradient, not one of the other documents.
    assert_color(image, 50, 20, (0, 238, 0))   # green icon, top
    assert_color(image, 50, 80, (0, 143, 0))   # green icon, bottom
    assert_color(image, 150, 20, (0, 0, 238))  # blue icon, top
    assert_color(image, 150, 80, (0, 0, 143))  # blue icon, bottom

    # The outer document still uses its own red-to-yellow gradient.
    assert_color(image, 5, 5, (255, 7, 0))
    assert_color(image, 195, 295, (255, 249, 0))
    assert_color(image, 20, 255, (255, 33, 0))

    # The shape drawn after the images is cyan, painted with the outer
    # ``fill`` and clipped by the outer ``clip`` rectangle only: it keeps the
    # outer shape outside of the icons' circles.
    assert_color(image, 130, 210, (0, 255, 255))
    # Outside of the outer `clip` rectangle: the outer background only.
    assert_color(image, 195, 210, (255, 249, 0), tolerance=12)

    # The gap between the two icons shows the outer background only.
    assert_color(image, 95, 50, (255, 122, 0))


def test_embedded_gradient_is_clipped_to_its_own_circle():
    """An icon's clip path is resolved in the icon's coordinate system."""
    image = render('main.svg', unsafe=True)
    # The green icon is a circle: its own corners stay unpainted, so the
    # outer background shows through.
    assert pixel(image, 50, 20) != pixel(image, 12, 12)
    assert_color(image, 12, 12, (255, 16, 0), tolerance=40)
    # The blue icon is clipped by *its* circle, at its own position.
    assert_color(image, 112, 12, (255, 143, 0), tolerance=40)
    assert_color(image, 150, 50, (0, 0, 191))


def test_embedded_size_and_viewbox():
    """Repeated embedding maps each icon into its own image region."""
    image = render('main.svg', unsafe=True)
    # The same green icon is embedded at 40x40 and 80x80.
    assert_color(image, 30, 120, (0, 190, 0))
    assert_color(image, 100, 140, (0, 191, 0))
    # The small embed is clipped at x=50: the space between the two embeds
    # (x=50..60) keeps the outer background.
    assert_color(image, 52, 120, (255, 67, 0), tolerance=40)


def test_embedded_transparency():
    """Areas not painted by an icon stay transparent.

    The icons are circles inscribed in their 100x100 image region, so the
    region corners are left unpainted and the white background shows through;
    an opaque black square would mean the transparent parts of the recorded
    document were not cleared.

    """
    image = render('transparent.svg', unsafe=True)
    # The four corners of the icon regions keep the white background.
    for point in ((0, 0), (99, 0), (0, 99), (100, 0), (199, 99), (100, 99)):
        assert_color(image, *point, (255, 255, 255))
    # The icons themselves are painted in their own colors.
    assert_color(image, 50, 50, (0, 191, 0))
    assert_color(image, 150, 50, (0, 0, 191))


def test_embedded_relative_resource():
    """A relative resource of an embedded document uses its own folder."""
    image = render('main.svg', unsafe=True)
    # The icon in icons/sub/ embeds pixel.png relatively.
    assert_color(image, 25, 210, (255, 128, 0))
    # Its own gradient, with an intrinsic size different from its viewBox.
    assert_color(image, 85, 210, (142, 0, 142))


def test_repeated_embedded_document_isolation():
    """Repeated embeddings of one document do not alter each other.

    The icon is a 20x20 square with a 20x20 viewBox, embedded once at 40x40
    (upscaled) and once at 40x20 (kept square and letterboxed horizontally by
    the default ``xMidYMid meet``).  Each embedding has to use the icon's own
    viewBox; sharing or mutating the cached tree between embeddings would make
    the second one resize the first one's content.

    """
    image = render('repeated.svg', unsafe=True)
    # First embedding: 40x40, fully green.
    assert_color(image, 20, 20, (0, 255, 0))
    assert_color(image, 35, 35, (0, 255, 0))
    # Second embedding: its 20x20 content is centered in the 40x20 region.
    assert_color(image, 110, 10, (0, 255, 0))
    assert_color(image, 125, 10, (0, 255, 0))
    # The letterboxed region around the second embedding stays white.
    assert_color(image, 102, 10, (255, 255, 255))
    assert_color(image, 150, 10, (255, 255, 255))


def test_embedded_relative_resource_respects_access_policy():
    """The default policy still forbids fetching external files.

    The embedded document is passed as a data URL (always allowed), but the
    raster resource it references relatively must not be fetched without
    ``unsafe``.  With ``unsafe``, it is fetched from the icon's own folder.

    """
    icon = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" '
        'viewBox="0 0 20 20">'
        '<image href="../pixel.png" x="0" y="0" width="20" height="20"/>'
        '</svg>')
    outer = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
        '<rect width="40" height="20" fill="#000000"/>'
        f'<image href="{data_url(icon)}" width="20" height="20"/></svg>')
    # Relative to the data URL there is no file: nothing is painted either way.
    safe = Image.open(io.BytesIO(svg2png(bytestring=outer))).convert('RGBA')
    assert pixel(safe, 10, 10) == (0, 0, 0)

    # With a real file URL as source, the relative pixel is resolved against
    # the icon's own folder, and only when external access is allowed.
    src = CURRENT_FOLDER / 'icons' / 'sub' / 'relative.svg'
    outer = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
        '<rect width="40" height="20" fill="#000000"/>'
        f'<image href="{src.as_uri()}" width="40" height="20"/></svg>')
    safe = Image.open(io.BytesIO(svg2png(bytestring=outer))).convert('RGBA')
    assert pixel(safe, 10, 10) == (0, 0, 0)
    allowed = Image.open(
        io.BytesIO(svg2png(bytestring=outer, unsafe=True))).convert('RGBA')
    assert_color(allowed, 10, 10, (255, 128, 0))


def test_embedded_transform():
    """The outer transform applies to the mapped embedded icon."""
    image = render('transformed.svg', unsafe=True)
    # The icon is a 40x40 circle scaled by 1.5 and centered at (80, 80).
    for point in ((80, 55), (55, 80), (80, 105), (105, 80)):
        assert pixel(image, *point)[1] > 100
    # The circle spans x and y from 50 to 110, so its corners stay clear.
    assert_color(image, 50, 50, (255, 255, 255), tolerance=2)
    assert_color(image, 110, 110, (255, 255, 255), tolerance=2)
    # Well outside of the circle, the white background remains.
    assert_color(image, 20, 20, (255, 255, 255))
    # The sibling drawn after the image is still there.
    assert_color(image, 80, 150, (0, 0, 255))


def test_embedded_viewbox_only_uses_image_region():
    """An icon without intrinsic size falls back to its own viewBox.

    The ``viewBox`` aspect ratio (2:1) must be resolved in the icon's own
    coordinate system, not from the outer document's size.  The icon is
    stretched over the image region with ``preserveAspectRatio="none"``.

    """
    icon = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 20">'
        '<rect width="20" height="20" fill="#00ff00"/>'
        '<rect x="20" width="20" height="20" fill="#0000ff"/></svg>')
    outer = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="20">'
        '<rect width="200" height="20" fill="#ff0000"/>'
        f'<image href="{data_url(icon)}" width="40" height="20" '
        'preserveAspectRatio="none"/></svg>')
    image = Image.open(io.BytesIO(svg2png(bytestring=outer))).convert('RGBA')
    assert_color(image, 5, 10, (0, 255, 0))
    assert_color(image, 35, 10, (0, 0, 255))
    assert_color(image, 100, 10, (255, 0, 0))


def test_embedded_size_is_its_intrinsic_size():
    """An icon with an explicit size keeps that size in a larger region.

    The icon declares a 20x10 viewport with a 40x40 viewBox, so its own
    rendering letterboxes the square viewBox inside its 20x10 viewport.  That
    20x10 intrinsic picture must then be centered in the 40x40 image region by
    the default ``xMidYMid meet``.  Before the fix, the viewBox was stretched
    over the whole region and shrunk back, placing the picture 5 pixels too
    high.

    """
    icon = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10" '
        'viewBox="0 0 40 40">'
        '<rect width="40" height="40" fill="#00ff00"/></svg>')
    outer = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">'
        '<rect width="80" height="80" fill="#000000"/>'
        f'<image href="{data_url(icon)}" x="20" y="20" width="40" '
        'height="40"/></svg>')
    image = Image.open(io.BytesIO(svg2png(bytestring=outer))).convert('RGBA')
    green = [
        (x, y) for y in range(80) for x in range(80)
        if pixel(image, x, y)[1] > 200 and pixel(image, x, y)[0] < 60]
    assert (min(x for x, _ in green), min(y for _, y in green)) == (30, 30)
    assert (max(x for x, _ in green), max(y for _, y in green)) == (49, 49)
    # Outside of the picture, the background is still visible.
    assert_color(image, 25, 25, (0, 0, 0))
    assert_color(image, 55, 40, (0, 0, 0))
    assert_color(image, 40, 25, (0, 0, 0))


def test_broken_embedded_document_keeps_siblings():
    """A malformed embedded document is ignored, siblings are drawn."""
    image = render('broken-sibling.svg', unsafe=True)
    assert_color(image, 20, 20, (255, 0, 0))
    assert_color(image, 100, 20, (0, 0, 255))
    # Nothing is painted for the broken image.
    assert image.getpixel((60, 20))[3] == 0


def test_embedded_opacity():
    """Opacity is applied to the mapped embedded document."""
    icon = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
        '<rect width="20" height="20" fill="#ffffff"/></svg>')
    outer = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
        '<rect width="40" height="20" fill="#000000"/>'
        f'<image href="{data_url(icon)}" width="20" height="20" '
        'opacity="0.5"/></svg>')
    image = Image.open(io.BytesIO(svg2png(bytestring=outer))).convert('RGBA')
    assert_color(image, 10, 10, (128, 128, 128))
    assert_color(image, 30, 10, (0, 0, 0))


def test_embedded_preserve_aspect_ratio():
    """``preserveAspectRatio`` on the image element is honoured."""
    icon = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" '
        'viewBox="0 0 40 40">'
        '<rect width="40" height="40" fill="#0000ff"/></svg>')
    outer = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
        '<rect width="40" height="20" fill="#ff0000"/>'
        f'<image href="{data_url(icon)}" width="40" height="20" '
        'preserveAspectRatio="none"/></svg>')
    image = Image.open(io.BytesIO(svg2png(bytestring=outer))).convert('RGBA')
    # Stretched to fill the whole region.
    assert_color(image, 2, 10, (0, 0, 255))
    assert_color(image, 38, 10, (0, 0, 255))


def test_embedded_cycle_is_broken():
    """A document embedding itself is not rendered recursively.

    The inner document embeds itself again.  Rendering must stop at the cycle
    and keep drawing the siblings instead of recursing until the interpreter
    gives up.

    """
    image = render('cycle-outer.svg', unsafe=True)
    # The first level of the cyclic document is shown…
    assert_color(image, 5, 5, (0, 255, 0))
    # …and the sibling after the image is drawn.
    assert_color(image, 35, 35, (0, 0, 255))


def test_png_payload_containing_svg_bytes_is_a_png():
    """A raster image is not mistaken for an SVG document.

    The SVG detection looks for ``<svg`` bytes in the payload; a PNG whose
    text chunks contain them must still be decoded as a PNG.

    """
    from PIL import PngImagePlugin

    png = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text('comment', '<svg> not a document')
    Image.new('RGB', (10, 10), (255, 128, 0)).save(
        png, 'PNG', pnginfo=info)
    raw = png.getvalue()
    assert b'<svg' in raw
    url = (
        'data:image/png;base64,' +
        base64.b64encode(raw).decode('ascii'))
    outer = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
        f'<image href="{url}" width="20" height="20"/></svg>')
    image = Image.open(io.BytesIO(svg2png(bytestring=outer))).convert('RGBA')
    assert_color(image, 10, 10, (255, 128, 0))


@requires_cairo
def test_embedded_is_not_rasterized():
    """Embedded documents stay vector-based in vector output formats."""
    from . import svg2svg

    # This case only embeds vector icons, so no raster fallback is needed.
    output = svg2svg(url=str(CURRENT_FOLDER / 'transparent.svg'), unsafe=True)
    assert b'data:image/png' not in output
    # The icons' gradients and clips are replayed as vector operations.
    assert output.count(b'linearGradient') >= 2
