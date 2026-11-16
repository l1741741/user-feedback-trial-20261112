"""
Images manager.

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


def image(surface, node):
    """Draw an image ``node``."""
    base_url = node.get('{http://www.w3.org/XML/1998/namespace}base')
    if not base_url and node.url:
        base_url = os.path.dirname(node.url) + '/'
    url = parse_url(node.get_href(), base_url)
    try:
        image_bytes = node.fetch_url(url, 'image/*')
    except Exception:
        # A resource that cannot be fetched is a broken image: skip it and
        # let the sibling nodes be drawn normally.
        return

    if len(image_bytes) < 5:
        return

    x, y = size(surface, node.get('x'), 'x'), size(surface, node.get('y'), 'y')
    width = size(surface, node.get('width'), 'x')
    height = size(surface, node.get('height'), 'y')

    if image_bytes.startswith(b'\x89PNG') and not surface.map_image:
        png_file = BytesIO(image_bytes)
    elif (
        image_bytes.startswith((b'<svg ', b'<?xml', b'<!DOC', b'\x1f\x8b'))
        or b'<svg' in image_bytes
    ):
        draw_svg_image(
            surface, node, url, image_bytes, x, y, width, height)
        return
    else:
        try:
            png_file = BytesIO()
            image = ImageOps.exif_transpose(
                Image.open(BytesIO(image_bytes)))

            if surface.map_image:
                image = surface.map_image(image)
            if image.mode == "CMYK":
                image = image.convert("RGB")
            image.save(png_file, 'PNG')
            png_file.seek(0)
        except Exception:
            # A raster image that cannot be decoded is a broken image: skip
            # it and keep drawing the sibling nodes.
            return

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
    """Draw an embedded SVG document in the image region.

    The embedded document is parsed and drawn with its own coordinates,
    styles and definitions: same-id gradients, clip paths and other
    definitions of different documents never collide, and the relative
    resources of the embedded document are resolved against its own URL.

    Any parsing or drawing failure turns the embedded document into a
    broken image and leaves the parent document rendering untouched.

    """
    if width <= 0 or height <= 0:
        return
    try:
        tree = Tree(
            url=url.geturl(), url_fetcher=node.url_fetcher,
            bytestring=image_bytes, tree_cache=surface.tree_cache,
            unsafe=node.unsafe)
        tree_width, tree_height, _ = node_format(
            surface, tree, reference=False)
        # An embedded document without an intrinsic size (no width/height,
        # no viewBox) uses the image region as its viewport, as required
        # by the SVG specification for referenced SVG documents.
        if not tree_width:
            tree_width = width
        if not tree_height:
            tree_height = height
        # The embedded document gets the image region as its own viewport:
        # its viewBox and preserveAspectRatio are then resolved by the
        # regular nested <svg> drawing, independently of the parent. The
        # tree may be cached and reused, so the original attributes are
        # restored after drawing.
        tree_width_attr = tree.get('width')
        tree_height_attr = tree.get('height')
        tree['width'] = str(width)
        tree['height'] = str(height)
        node.image_width = tree_width
        node.image_height = tree_height
        # The aspect-ratio alignment of an embedded SVG document is defined
        # by the document itself (its own preserveAspectRatio), not by the
        # image node: temporarily expose it through the node so that the
        # generic ratio-preservation logic applies the document's rule.
        document_ratio = tree.get('preserveAspectRatio', 'xMidYMid')
        image_ratio = node.get('preserveAspectRatio')
        node['preserveAspectRatio'] = document_ratio
        try:
            scale_x, scale_y, translate_x, translate_y = preserve_ratio(
                surface, node)
        finally:
            if image_ratio is None:
                del node['preserveAspectRatio']
            else:
                node['preserveAspectRatio'] = image_ratio
        # Alignment is applied once, at the image level above. Inside the
        # nested <svg> drawing the viewBox must therefore be mapped without
        # a second alignment. The tree may be cached and reused when the
        # same document is embedded several times, so the change is
        # restored after drawing.
        tree_ratio = tree.get('preserveAspectRatio')
        tree['preserveAspectRatio'] = 'none'
    except Exception:
        # Broken embedded document: the Cairo context is untouched, skip
        # the image and let the sibling nodes be drawn.
        return

    try:
        surface.context.save()
        # Clip the image region: content overflowing the region is
        # never painted on the parent document.
        surface.context.rectangle(x, y, width, height)
        surface.context.clip()
        # Map the document's intrinsic viewport to the image region using
        # the document's own meet/slice/none alignment (computed above);
        # the nested <svg> drawing then maps its viewBox without a second
        # alignment.
        surface.context.translate(x, y)
        surface.context.translate(*surface.context.get_current_point())
        surface.context.scale(scale_x, scale_y)
        surface.context.translate(translate_x, translate_y)
        opacity = float(node.get('opacity', 1))
        if opacity < 1:
            # Paint the whole embedded document as a group so that the
            # image opacity applies to the composited result.
            surface.context.push_group()
        with surface.isolated_definitions():
            surface.draw(tree)
        if opacity < 1:
            surface.context.pop_group_to_source()
            surface.context.paint_with_alpha(opacity)
    except Exception:
        # A drawing failure must not abort the parent document: the
        # context is restored below and the image is skipped.
        pass
    finally:
        surface.context.restore()
        # Restore the cached tree so another embedding of the same
        # document starts from its original attributes.
        for name, original in (
                ('width', tree_width_attr),
                ('height', tree_height_attr),
                ('preserveAspectRatio', tree_ratio)):
            if original is None:
                tree.pop(name, None)
            else:
                tree[name] = original


def invert_image(img):
    """Invert the colors of an image."""
    *rgb, a = img.convert('RGBA').split()
    return Image.merge('RGBA', (*map(ImageOps.invert, rgb), a))
