"""
Images manager.

"""

import os.path
from io import BytesIO
from xml.etree.ElementTree import ParseError

from PIL import Image, ImageOps

from .helpers import node_format, preserve_ratio, size
from .parser import Tree
from .surface import cairo
from .url import parse_url

IMAGE_RENDERING = {
    'optimizeQuality': cairo.FILTER_BEST,
    'optimizeSpeed': cairo.FILTER_FAST,
}


class RenderingAborted(Exception):
    """Raised when a safety limit is reached while rendering.

    It is deliberately not a subclass of the exceptions caught for broken
    embedded documents (``ValueError``, ``OSError``…), so that it aborts the
    whole conversion instead of being silently ignored as an invalid image.

    """


class _ReferenceViewport:
    """Minimal surface-like object used to resolve embedded sizes.

    ``size`` needs a viewport, a font size and a DPI to resolve percentages
    and units.  Embedded documents have to be sized against the region of the
    ``<image>`` element, not against the outer document, so they are given
    this small stand-in instead of the outer surface.

    """

    def __init__(self, surface, width, height):
        self.context_width = width
        self.context_height = height
        self.dpi = surface.dpi
        self.font_size = surface.font_size


def is_svg_image(image_bytes):
    """Tell whether ``image_bytes`` contains an SVG document."""
    return (
        image_bytes.startswith((b'<svg ', b'<?xml', b'<!DOC', b'\x1f\x8b'))
        or b'<svg' in image_bytes)


def embedded_size(surface, tree, width, height):
    """Return the intrinsic ``(width, height)`` of an embedded document.

    The size is resolved in the embedded document's own coordinate system,
    with percentages and units relative to the ``<image>`` region, as for a
    replaced element.  A root with a fixed size keeps it; percentage sizes,
    missing sizes and ``viewBox`` fall back to the region or to the
    ``viewBox`` dimensions.

    """
    reference = _ReferenceViewport(surface, width, height)
    tree_width = _fixed_size(reference, tree.get('width'))
    tree_height = _fixed_size(reference, tree.get('height'))
    if not tree_width or not tree_height:
        _, _, viewbox = node_format(reference, tree)
        if viewbox:
            tree_width = tree_width or viewbox[2]
            tree_height = tree_height or viewbox[3]
    tree_width = tree_width or width
    tree_height = tree_height or height
    if not tree_width or not tree_height:
        return None
    # Render the document in a viewport of its own intrinsic size, so that its
    # own units and percentages are resolved against that size.
    tree['width'] = tree_width
    tree['height'] = tree_height
    return tree_width, tree_height


def _fixed_size(reference, value):
    """Resolve an absolute size attribute, ignoring percentages.

    A percentage root size only repeats the ``<image>`` region size, so it
    does not tell anything about the intrinsic size of the document.

    """
    if value is None or str(value).strip().endswith('%'):
        return 0
    return size(reference, value, 'x')


def image(surface, node):
    """Draw an image ``node``."""
    base_url = node.get('{http://www.w3.org/XML/1998/namespace}base')
    if not base_url and node.url:
        base_url = os.path.dirname(node.url) + '/'
    url = parse_url(node.get_href(), base_url)
    try:
        image_bytes = node.fetch_url(url, 'image/*')
    except (OSError, ValueError):
        # Unreadable image: keep drawing the following siblings.
        return

    if len(image_bytes) < 5:
        return

    x, y = size(surface, node.get('x'), 'x'), size(surface, node.get('y'), 'y')
    width = size(surface, node.get('width'), 'x')
    height = size(surface, node.get('height'), 'y')

    # PNG data is detected first: unlike SVG detection, it cannot be fooled by
    # image payloads that happen to contain ``<svg`` bytes.
    png_data = image_bytes.startswith(b'\x89PNG') and not surface.map_image
    if not png_data and is_svg_image(image_bytes):
        draw_svg_image(surface, node, url, image_bytes, x, y, width, height)
        return

    if png_data:
        png_file = BytesIO(image_bytes)
    else:
        png_file = BytesIO()
        image = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes)))

        if surface.map_image:
            image = surface.map_image(image)
        if image.mode == "CMYK":
            image = image.convert("RGB")
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


def draw_svg_image(surface, node, url, image_bytes, x, y, width, height):
    """Draw an SVG document embedded in an ``<image>`` element.

    The embedded document is parsed and rendered on its own surface: its
    stylesheets, definitions and coordinate system are its own, and the
    resulting (vector) image is then mapped into the ``<image>`` region of the
    outer surface.  A broken embedded document is ignored so that its siblings
    are still drawn.

    """
    from .surface import EmbeddedSurface  # circular import

    # Modelled on ``use``: bound the number of embedded documents that a
    # single conversion may render, so that a small input cannot trigger an
    # exponential amount of work (embedding the same image many times).
    surface.reference_count += 1
    if not node.unsafe and surface.reference_count > 100_000:
        raise RenderingAborted(
            'Abort rendering: more than 100 000 embedded documents')

    # Never render a document that is already being rendered: an SVG embedding
    # itself, directly or through other documents, is a cycle.
    embedded_urls = getattr(surface, 'embedded_urls', frozenset())
    if url.geturl() in embedded_urls:
        return

    try:
        tree = Tree(
            url=url.geturl(), url_fetcher=node.url_fetcher,
            bytestring=image_bytes, tree_cache=surface.tree_cache,
            unsafe=node.unsafe)

        # Resolve the intrinsic size of the embedded document in its own
        # coordinate system.  Percentages and units are relative to the
        # image region, as for a replaced element.
        tree_format = embedded_size(surface, tree, width, height)
        if tree_format is None:
            return
        tree_width, tree_height = tree_format

        # Render the whole document on its own surface.  A recording surface
        # keeps vector output vector-based and lets transparent areas stay
        # transparent when it is replayed.
        embedded = EmbeddedSurface(
            tree, None, surface.dpi, parent_width=width, parent_height=height,
            map_rgba=surface.map_rgba, map_image=surface.map_image,
            reference_count=surface.reference_count,
            embedded_urls=embedded_urls | {url.geturl()})
        # Share the reference counter with the embedded document, so that
        # references and embeddings nested in it count towards the same limit.
        surface.reference_count = embedded.reference_count
    except (ParseError, TypeError, ValueError, OSError, cairo.Error):
        # Invalid or unreadable embedded document: keep drawing the siblings.
        return

    node.image_width = tree_width
    node.image_height = tree_height
    width = width or tree_width
    height = height or tree_height
    scale_x, scale_y, translate_x, translate_y = preserve_ratio(
        surface, node, width, height)

    opacity = float(node.get('opacity', 1))
    surface.context.save()
    surface.context.translate(x, y)

    # Clip to the image region: what the embedded document paints outside of
    # its own viewport must not bleed into the outer drawing.
    surface.context.rectangle(0, 0, width, height)
    surface.context.clip()

    # Paint the recorded document, mapped into the image region
    surface.context.scale(scale_x, scale_y)
    surface.context.translate(translate_x, translate_y)
    surface.context.set_source_surface(embedded.cairo, 0, 0)
    if opacity < 1:
        surface.context.paint_with_alpha(opacity)
    else:
        surface.context.paint()
    surface.context.restore()


def invert_image(img):
    """Invert the colors of an image."""
    *rgb, a = img.convert('RGBA').split()
    return Image.merge('RGBA', (*map(ImageOps.invert, rgb), a))
