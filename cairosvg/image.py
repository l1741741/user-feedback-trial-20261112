"""
Images manager.

Raster images go through Pillow when they need to be transformed (EXIF
orientation, ``invert_images``, CMYK conversion…).  The ``invert_images``
option only changes the RGB channels of a decoded image: alpha values are
kept as-is, transparent pixels stay transparent, and the source bytes or
Pillow objects given by the caller are never modified.

"""

import os.path
from io import BytesIO

from PIL import Image, ImageOps

from .helpers import node_format, preserve_ratio, size
from .parser import Tree
from .surface import cairo
from .url import parse_url

IMAGE_RENDERING = {
    'optimizeQuality': cairo.FILTER_BEST,
    'optimizeSpeed': cairo.FILTER_FAST,
}

PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
EXIF_ORIENTATION = 0x0112

# Modes that PNG and Cairo both handle natively: they are kept untouched so
# that disabling the options gives byte-identical images.
NATIVE_MODES = frozenset(('1', 'L', 'LA', 'P', 'RGB', 'RGBA'))
# Grayscale modes using samples in the 0..65535 range.
HIGH_DEPTH_GRAY_MODES = frozenset((
    'I', 'I;16', 'I;16B', 'I;16L', 'I;16N'))
# Modes without a well-defined 8-bit RGB equivalent.  They are reported
# instead of being silently converted to black or white.
UNSUPPORTED_MODES = frozenset(('F', 'HSV', 'LAB', 'YCbCr'))


def image(surface, node):
    """Draw an image ``node``."""
    base_url = node.get('{http://www.w3.org/XML/1998/namespace}base')
    if not base_url and node.url:
        base_url = os.path.dirname(node.url) + '/'
    url = parse_url(node.get_href(), base_url)
    image_bytes = node.fetch_url(url, 'image/*')

    if len(image_bytes) < 5:
        return

    x, y = size(surface, node.get('x'), 'x'), size(surface, node.get('y'), 'y')
    width = size(surface, node.get('width'), 'x')
    height = size(surface, node.get('height'), 'y')

    if image_bytes.startswith(PNG_SIGNATURE) and not surface.map_image and (
            not has_exif_orientation(image_bytes)):
        # Fast path: Cairo reads PNG natively and pixels are left untouched,
        # so that disabling the options gives the exact original output.
        # PNG files carrying a real EXIF orientation must go through Pillow,
        # or their orientation would depend on ``invert_images``.
        png_file = BytesIO(image_bytes)
    elif (
        image_bytes.startswith((b'<svg ', b'<?xml', b'<!DOC', b'\x1f\x8b'))
        or b'<svg' in image_bytes
    ):
        if 'x' in node:
            del node['x']
        if 'y' in node:
            del node['y']
        tree = Tree(
            url=url.geturl(), url_fetcher=node.url_fetcher,
            bytestring=image_bytes, tree_cache=surface.tree_cache,
            unsafe=node.unsafe)
        tree_width, tree_height, viewbox = node_format(
            surface, tree, reference=False)
        if viewbox:
            tree_scale_x = tree_width / viewbox[2]
            tree_scale_y = tree_height / viewbox[3]
        else:
            tree_width = tree['width'] = width
            tree_height = tree['height'] = height
            tree_scale_x = tree_scale_y = 1
        node.image_width = tree_width or width
        node.image_height = tree_height or height
        scale_x, scale_y, translate_x, translate_y = preserve_ratio(
            surface, node)

        # Clip image region
        surface.context.rectangle(x, y, width, height)
        surface.context.clip()

        # Draw image
        surface.context.save()
        surface.context.translate(x, y)
        surface.context.translate(*surface.context.get_current_point())
        surface.context.scale(scale_x * tree_scale_x, scale_y * tree_scale_y)
        surface.context.translate(translate_x, translate_y)
        surface.draw(tree)
        surface.context.restore()
        return
    else:
        png_file = BytesIO()
        image = open_image(image_bytes)

        # Apply the EXIF orientation first, so that inversion and alpha
        # handling act on the displayed image.
        # ``map_image`` receives the same decoded image as before and works
        # on its own copy: the caller’s image object and the original bytes
        # are never modified.
        image = ImageOps.exif_transpose(image)
        if surface.map_image:
            image = surface.map_image(image)
        # Normalize the mode for Cairo: palette transparency is preserved,
        # and high-depth grayscale is scaled instead of being clamped.
        # Saving as 8-bit PNG is required for inversion to act on the
        # displayed values.
        image = prepare_image(image, invert=bool(surface.map_image))
        image.save(png_file, 'PNG')
        png_file.seek(0)

    image_surface = cairo.ImageSurface.create_from_png(png_file)
    image_surface.pattern = cairo.SurfacePattern(image_surface)
    image_surface.pattern.set_filter(IMAGE_RENDERING.get(
        node.get('image-rendering'), cairo.FILTER_GOOD))

    node.image_width = image_surface.get_width()
    node.image_height = image_surface.get_height()
    width = width or node.image_width
    height = height or node.image_height
    scale_x, scale_y, translate_x, translate_y = preserve_ratio(
        surface, node, width, height)

    # Clip image region (if necessary)
    if not (translate_x == 0 and
            translate_y == 0 and
            width == scale_x * node.image_width and
            height == scale_y * node.image_height):
        surface.context.rectangle(x, y, width, height)
        surface.context.clip()

    # Paint raster image
    opacity = float(node.get('opacity', 1))
    surface.context.save()
    surface.context.translate(x, y)
    surface.context.scale(scale_x, scale_y)
    surface.context.translate(translate_x, translate_y)
    surface.context.set_source(image_surface.pattern)
    surface.context.paint_with_alpha(opacity)
    surface.context.restore()


def has_exif_orientation(image_bytes):
    """Return whether ``image_bytes`` carries a non-trivial EXIF rotation.

    PNG files are the only format handled natively by Cairo; their ``eXIf``
    chunk is detected without decoding pixels.  The orientation value is
    read only when the chunk exists, so that PNGs without meaningful
    orientation keep taking the lossless fast path and their output does not
    depend on ``invert_images``.  Other formats always go through Pillow,
    which applies the orientation itself.

    """
    if not image_bytes.startswith(PNG_SIGNATURE) or not _png_has_chunk(
            image_bytes, b'eXIf'):
        return False
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            orientation = image.getexif().get(EXIF_ORIENTATION)
    except Exception:  # pragma: no cover - malformed metadata
        return False
    return orientation not in (None, 1)


def _png_has_chunk(image_bytes, chunk_type):
    """Return whether a PNG file contains a chunk of the given type."""
    position = len(PNG_SIGNATURE)
    while position + 8 <= len(image_bytes):
        length = int.from_bytes(image_bytes[position:position + 4], 'big')
        if image_bytes[position + 4:position + 8] == chunk_type:
            return True
        if image_bytes[position + 4:position + 8] == b'IEND':
            break
        position += 12 + length  # length, type, data and CRC
    return False


def open_image(image_bytes):
    """Open a copy of ``image_bytes`` as a fully loaded Pillow image."""
    image = Image.open(BytesIO(image_bytes))
    # Load before the underlying buffer is garbage-collected, and detach the
    # result from any shared lazily-decoded state.
    image.load()
    return image.copy()


def invert_image(img):
    """Invert the color channels of an image, keeping alpha untouched.

    The returned image is always in ``RGBA`` mode.  Fully transparent pixels
    stay fully transparent instead of being flattened to an opaque black
    background, and the input object is never modified.  Palette images keep
    the transparency of their palette, and high-depth grayscale is scaled to
    8 bits instead of being clamped.

    """
    image = prepare_image(img, invert=True)
    red, green, blue, alpha = image.split()
    return Image.merge(
        'RGBA', (*map(ImageOps.invert, (red, green, blue)), alpha))


def to_rgba(img):
    """Return an ``RGBA`` copy of ``img`` with its transparency preserved."""
    # ``convert('RGBA')`` honours the ``transparency`` info of palette and
    # RGB images (tRNS chunks); a raw ``split()`` would ignore it and turn
    # transparent pixels into opaque ones.
    return img.convert('RGBA') if img.mode != 'RGBA' else img.copy()


def prepare_image(img, invert=False):
    """Return a mode Cairo can read from a PNG, without touching alpha.

    Modes that PNG and Cairo already support are kept as they are, so that
    conversions only happen when needed.  Palette images keep the
    ``transparency`` info of their palette, and high-depth grayscale is
    scaled to 8 bits instead of being clamped; modes without a defined
    8-bit output raise a ``ValueError`` so that callers get an explicit
    message instead of silently wrong colors.

    ``invert`` requests an ``RGBA`` result, as needed before inverting.

    """
    mode = img.mode
    if mode in UNSUPPORTED_MODES:
        raise ValueError(
            f'Unsupported image mode {mode!r}: CairoSVG can only render '
            '8-bit RGB(A) and grayscale images; convert the input to RGB '
            'or RGBA first')
    if mode in HIGH_DEPTH_GRAY_MODES:
        # Pillow's ``convert('RGB')`` clamps 16-bit samples to 255, losing
        # all detail.  Scale the 16-bit range down explicitly instead.
        # ``point`` is not implemented for the byte-ordered ``I;16B/L/N``
        # variants, so go through the native ``I`` mode first.
        if mode != 'I':
            img = img.convert('I')
        scaled = img.point(lambda value: value * (255 / 65535)).convert('L')
        return to_rgba(scaled) if invert else scaled
    if mode == 'CMYK':
        # Cairo has no CMYK support; let Pillow do the standard conversion.
        img = img.convert('RGB')
        return to_rgba(img) if invert else img
    if mode == 'La':
        # ``La`` (premultiplied) has no direct ``RGBA`` conversion, but its
        # ``LA`` equivalent does.
        img = img.convert('LA')
    if invert:
        return to_rgba(img)
    if mode in NATIVE_MODES:
        return img
    return img.convert('RGBA')
