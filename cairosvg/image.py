"""
Images manager.

The geometry of an ``image`` element is the combination of three independent
things:

* the intrinsic size of the referenced content (pixel size of a raster image,
  or the width, height and viewBox of a referenced SVG document),
* the viewport defined by the ``x``, ``y``, ``width`` and ``height``
  attributes, where a missing size is deduced from the intrinsic ratio,
* the ``preserveAspectRatio`` alignment that places the intrinsic content
  inside that viewport.

Mixing those three up (for example using the number of pixels of a file for
the viewport, or ignoring an Exif orientation when measuring a JPEG) is what
crops thumbnails wrongly.  Everything is computed again for each reference,
so that the same file can be drawn at different sizes without shared state.

"""

import os.path
from io import BytesIO

from PIL import Image, ImageChops, ImageOps

from .helpers import size
from .parser import Tree
from .surface import cairo
from .url import parse_url

IMAGE_RENDERING = {
    'optimizeQuality': cairo.FILTER_BEST,
    'optimizeSpeed': cairo.FILTER_FAST,
}

ALIGNMENTS = {'min': 0, 'mid': .5, 'max': 1}

# Raster modes written to PNG without conversion.
PNG_MODES = frozenset((
    '1', 'L', 'LA', 'La', 'P', 'RGB', 'RGBA', 'RGBa',
    'I', 'I;16', 'I;16B', 'I;16L', 'I;16N'))

SVG_PREFIXES = (b'<svg ', b'<?xml', b'<!DOC', b'\x1f\x8b')


def _looks_like_svg(image_bytes):
    """Whether fetched ``image_bytes`` contain an SVG document."""
    return image_bytes.startswith(SVG_PREFIXES) or b'<svg' in image_bytes


def _length(surface, value, axis, base=None):
    """Parse a length attribute, returning ``None`` when absent or ``auto``.

    An explicit zero returns ``0``: "no size given" and "size is zero" must
    not be confused, as only the second one makes the image invisible.
    Percentages resolve against ``base`` when given, else against the current
    viewport (as for the ``width`` and ``height`` of the image element).

    """
    if value is None:
        return None
    value = str(value).strip()
    if not value or value.lower() == 'auto':
        return None
    if base is not None and value.endswith('%'):
        return float(value[:-1]) * base / 100
    return size(surface, value, axis)


def _view_box(value):
    """Parse a ``viewBox`` attribute as a tuple of four floats."""
    if not value:
        return None
    try:
        values = tuple(float(part) for part in value.replace(',', ' ').split())
    except ValueError:
        return None
    return values if len(values) == 4 else None


def _preserve_aspect_ratio(node):
    """Return ``(x_align, y_align, mode)`` from ``preserveAspectRatio``.

    ``x_align`` and ``y_align`` are ``min``, ``mid``, ``max`` or ``none``.
    The ``defer`` keyword is accepted and ignored.  Invalid or unknown values
    fall back to the SVG default, ``xMidYMid meet``.

    """
    values = node.get('preserveAspectRatio', 'xMidYMid').split()
    if values and values[0] == 'defer':
        values = values[1:]
    align = values[0] if values else 'xMidYMid'
    mode = values[1] if len(values) > 1 else 'meet'
    if mode not in ('meet', 'slice'):
        mode = 'meet'
    if align == 'none':
        return 'none', 'none', mode
    if (len(align) == 8 and align[0] == 'x' and align[4] == 'Y' and
            align[1:4].lower() in ALIGNMENTS and
            align[5:8].lower() in ALIGNMENTS):
        return align[1:4].lower(), align[5:8].lower(), mode
    return 'mid', 'mid', mode


def _aspect_ratio(width, height):
    """Intrinsic ratio of a content box, or ``None`` when undefined."""
    if width > 0 and height > 0:
        return width / height
    return None


def _fit(content_width, content_height, width, height, node):
    """Scale and translate intrinsic content into a viewport.

    Return ``(scale_x, scale_y, translate_x, translate_y)`` in intrinsic
    content units: drawing the content scaled by ``scale_x, scale_y`` then
    translating it by ``translate_x, translate_y`` fills the viewport as
    requested by ``preserveAspectRatio``.  ``none`` uses independent axis
    scales and no translation; ``meet`` and ``slice`` use a common scale and
    align the free space (meet) or the overflow (slice) according to the
    alignment keywords.

    """
    scale_x = width / content_width
    scale_y = height / content_height
    x_align, y_align, mode = _preserve_aspect_ratio(node)
    if x_align == 'none':
        return scale_x, scale_y, 0, 0
    if mode == 'slice':
        scale = max(scale_x, scale_y)
    else:
        scale = min(scale_x, scale_y)
    translate_x = (width / scale - content_width) * ALIGNMENTS[x_align]
    translate_y = (height / scale - content_height) * ALIGNMENTS[y_align]
    return scale, scale, translate_x, translate_y


def _content_clip(context, x, y, width, height, scale_x, scale_y,
                  translate_x, translate_y, content_width, content_height):
    """Clip to the intersection of the viewport and the scaled content.

    ``meet`` can leave free space around the content, which must stay
    transparent, while ``slice`` and ``none`` cover the whole viewport.  In
    all cases the intersection is exactly the region the content covers.

    """
    left = max(x, x + translate_x * scale_x)
    top = max(y, y + translate_y * scale_y)
    right = min(x + width, x + (translate_x + content_width) * scale_x)
    bottom = min(
        y + height, y + (translate_y + content_height) * scale_y)
    if right > left and bottom > top:
        context.rectangle(left, top, right - left, bottom - top)
        context.clip()
    else:
        context.rectangle(0, 0, 0, 0)
        context.clip()


def _viewport(surface, node, content_width, content_height):
    """Resolve the image viewport ``(width, height)`` in user units.

    A missing (or ``auto``) ``width`` or ``height`` is deduced from the
    intrinsic size while keeping the intrinsic ratio.  Explicit values,
    including explicit zeros, always win.

    """
    width = _length(surface, node.get('width'), 'x')
    height = _length(surface, node.get('height'), 'y')
    if width is None and height is None:
        return content_width, content_height
    ratio = _aspect_ratio(content_width, content_height)
    if ratio is None:
        # Without an intrinsic ratio, an unsized axis has no length of its
        # own: use the other axis, else the attribute defaults to zero.
        width = width if width is not None else height or 0
        height = height if height is not None else width or 0
    elif width is None:
        width = height * ratio
    elif height is None:
        height = width / ratio
    return width, height


def _draw_raster(surface, node, image_surface, x, y, width, height):
    """Paint a raster image into the ``x, y, width, height`` viewport."""
    content_width = image_surface.get_width()
    content_height = image_surface.get_height()
    if content_width <= 0 or content_height <= 0:
        return

    scale_x, scale_y, translate_x, translate_y = _fit(
        content_width, content_height, width, height, node)

    pattern = cairo.SurfacePattern(image_surface)
    pattern.set_filter(IMAGE_RENDERING.get(
        node.get('image-rendering'), cairo.FILTER_GOOD))
    # Extend the border pixels to the content edge: with the default
    # ``EXTEND_NONE`` the outermost pixels would be blended with transparency
    # and a fully opaque image would show a faded last row and column.
    pattern.set_extend(cairo.EXTEND_PAD)

    opacity = float(node.get('opacity', 1))
    context = surface.context
    context.save()
    _content_clip(
        context, x, y, width, height, scale_x, scale_y,
        translate_x, translate_y, content_width, content_height)
    context.translate(x, y)
    context.scale(scale_x, scale_y)
    context.translate(translate_x, translate_y)
    context.set_source(pattern)
    context.paint_with_alpha(opacity)
    context.restore()

    # Historical attributes, kept for consumers reading them.  They describe
    # this reference only and are never read back while drawing.
    node.image_width = content_width
    node.image_height = content_height


def _svg_intrinsic_size(
        surface, tree, viewport_width=None, viewport_height=None):
    """Return ``(width, height, viewbox)`` of a referenced SVG document.

    ``width`` and ``height`` describe the document's own coordinate system:
    an explicit attribute wins, a missing one is deduced from the ``viewBox``
    ratio, and the image viewport is the last resort for documents that have
    neither.  Percentage widths and heights resolve against the image
    viewport, given as ``viewport_width`` and ``viewport_height``.
    ``viewbox`` is ``None`` or a four-values tuple with a strictly positive
    size.

    """
    viewbox = _view_box(tree.get('viewBox'))
    if viewbox is not None and (viewbox[2] <= 0 or viewbox[3] <= 0):
        viewbox = None
    width = _length(surface, tree.get('width'), 'x', viewport_width)
    height = _length(surface, tree.get('height'), 'y', viewport_height)
    if width is None and height is None:
        if viewbox is not None:
            width, height = viewbox[2], viewbox[3]
        else:
            width, height = viewport_width or 0, viewport_height or 0
    elif width is None:
        width = height * viewbox[2] / viewbox[3] if viewbox else height
    elif height is None:
        height = width * viewbox[3] / viewbox[2] if viewbox else width
    return width, height, viewbox


def _draw_embedded(surface, node, tree, x, y, width, height):
    """Draw an SVG document referenced by an ``image`` element."""
    # A document with no size of its own is drawn in the image viewport's
    # coordinate system.
    content_width, content_height, viewbox = _svg_intrinsic_size(
        surface, tree, width, height)
    if content_width <= 0 or content_height <= 0:
        return

    scale_x, scale_y, translate_x, translate_y = _fit(
        content_width, content_height, width, height, node)

    context = surface.context
    context.save()
    # The image viewport clips everything: the overflowing part of a
    # ``slice`` crop must not paint outside of it.  Inside the viewport, the
    # referenced document is free to paint outside its own content box when
    # its root sets ``overflow="visible"``.
    context.rectangle(x, y, width, height)
    context.clip()
    context.translate(x, y)
    context.scale(scale_x, scale_y)
    context.translate(translate_x, translate_y)

    # The root ``svg`` of the referenced document is drawn as its own
    # viewport: it clips to its intrinsic box unless ``overflow`` is
    # visible.  This happens in the document's own coordinate system.
    if tree.get('overflow', 'hidden') != 'visible':
        context.rectangle(0, 0, content_width, content_height)
        context.clip()

    if viewbox:
        viewbox_x, viewbox_y, viewbox_width, viewbox_height = viewbox
        # Map the viewBox into the intrinsic box with the document's own
        # preserveAspectRatio, then honour the viewBox origin.
        inner_scale_x, inner_scale_y, inner_x, inner_y = _fit(
            viewbox_width, viewbox_height, content_width, content_height,
            tree)
        context.translate(inner_x, inner_y)
        context.scale(inner_scale_x, inner_scale_y)
        context.translate(-viewbox_x, -viewbox_y)
        user_width, user_height = viewbox_width, viewbox_height
    else:
        user_width, user_height = content_width, content_height

    opacity = float(node.get('opacity', 1))
    if opacity < 1:
        context.push_group()

    # Percentages inside the referenced document resolve against its own
    # coordinate system, not against the outer SVG viewport.
    old_context_size = surface.context_width, surface.context_height
    surface.context_width, surface.context_height = user_width, user_height
    try:
        surface.draw(tree)
    finally:
        surface.context_width, surface.context_height = old_context_size
        if opacity < 1:
            context.pop_group_to_source()
            context.paint_with_alpha(opacity)
        context.restore()

    # Historical attributes, kept for consumers reading them.
    node.image_width = content_width
    node.image_height = content_height


def _orientation(image):
    """Return the Exif orientation of ``image``, defaulting to ``1``."""
    try:
        return image.getexif().get(0x0112, 1) or 1
    except (AttributeError, OSError, SyntaxError, ValueError):
        return 1


def _bake_alpha(image):
    """Turn any transparency information into a real alpha channel.

    Raster formats store transparency as an alpha channel, as a palette entry
    or as a colour key (``tRNS``).  Cairo only composites the first form, so
    the other two are converted to RGBA here.  Transparent pixels then
    composite with what is below them; they are never filled with an opaque
    black or white background.

    """
    transparency = image.info.get('transparency')
    if image.mode in ('RGBA', 'RGBa', 'LA', 'La', 'PA'):
        return image.convert('RGBA')
    if image.mode == 'P' and transparency is not None:
        return image.convert('RGBA')
    if image.mode in ('L', 'RGB') and transparency is not None:
        if image.mode == 'L' and isinstance(transparency, int):
            alpha = image.point(
                lambda value: 0 if value == transparency else 255)
        elif (image.mode == 'RGB' and
                isinstance(transparency, (tuple, list)) and
                len(transparency) == 3):
            key = Image.new('RGB', image.size, tuple(transparency))
            difference = ImageChops.difference(image, key).convert('L')
            # ``difference`` is zero exactly on the transparent colour key.
            alpha = difference.point(lambda value: 0 if value == 0 else 255)
        else:
            return image.convert('RGBA')
        rgba = image.convert('RGBA')
        rgba.putalpha(alpha)
        return rgba
    return image


def _needs_decoding(image_bytes, surface):
    """Whether a PNG must be decoded before Cairo can draw it.

    A plain PNG can be handed to Cairo as it is.  Exif orientation and image
    mapping change the pixels, so those files are decoded and re-encoded.

    """
    if surface.map_image:
        return True
    try:
        image = Image.open(BytesIO(image_bytes))
    except (OSError, ValueError):
        return False
    try:
        return _orientation(image) != 1
    finally:
        image.close()


def _prepare_raster(surface, image_bytes):
    """Decode a raster image and re-encode it as PNG for Cairo.

    The Exif orientation is applied before the image is measured, so the
    intrinsic size is the real, displayed pixel size.  Transparency is kept
    as an actual alpha channel, and modes that PNG cannot store are converted
    without adding an opaque background.

    """
    image = Image.open(BytesIO(image_bytes))
    image.load()
    if _orientation(image) != 1:
        image = ImageOps.exif_transpose(image)
    if surface.map_image:
        image = surface.map_image(image)
    image = _bake_alpha(image)
    if image.mode not in PNG_MODES:
        image = image.convert('RGBA' if 'A' in image.mode else 'RGB')
    png_file = BytesIO()
    image.save(png_file, 'PNG')
    png_file.seek(0)
    return png_file


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

    if _looks_like_svg(image_bytes):
        tree = Tree(
            url=url.geturl(), url_fetcher=node.url_fetcher,
            bytestring=image_bytes, tree_cache=surface.tree_cache,
            unsafe=node.unsafe)
        content_width, content_height, _ = _svg_intrinsic_size(
            surface, tree,
            _length(surface, node.get('width'), 'x'),
            _length(surface, node.get('height'), 'y'))
        width, height = _viewport(
            surface, node, content_width, content_height)
        if width <= 0 or height <= 0:
            return
        _draw_embedded(surface, node, tree, x, y, width, height)
        return

    if image_bytes.startswith(b'\x89PNG') and not _needs_decoding(
            image_bytes, surface):
        png_file = BytesIO(image_bytes)
    else:
        png_file = _prepare_raster(surface, image_bytes)
    image_surface = cairo.ImageSurface.create_from_png(png_file)
    width, height = _viewport(
        surface, node, image_surface.get_width(), image_surface.get_height())
    if width <= 0 or height <= 0:
        return
    _draw_raster(surface, node, image_surface, x, y, width, height)


def invert_image(img):
    """Invert the colors of an image."""
    *rgb, a = img.convert('RGBA').split()
    return Image.merge('RGBA', (*map(ImageOps.invert, rgb), a))
