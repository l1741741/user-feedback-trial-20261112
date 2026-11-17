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
    image_bytes = node.fetch_url(url, 'image/*')

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
            if not (viewbox[2] and viewbox[3]):
                # An empty viewBox disables rendering of the document
                return
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
        # The alignment computed by preserve_ratio is given in tree units,
        # it has to be converted to the tree's user units; the viewBox
        # origin is already given in these user units
        surface.context.translate(
            translate_x / tree_scale_x, translate_y / tree_scale_y)
        if viewbox:
            surface.context.translate(-viewbox[0], -viewbox[1])
        # The embedded document establishes a new viewport: resolve its
        # percentage lengths against this viewport, not against the outer
        # one, and restore the outer viewport once it is drawn
        old_context_size = surface.context_width, surface.context_height
        if viewbox:
            surface.context_width, surface.context_height = viewbox[2:4]
        else:
            surface.context_width, surface.context_height = (
                tree_width, tree_height)
        surface.draw(tree)
        surface.context_width, surface.context_height = old_context_size
        surface.context.restore()
        return
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


def invert_image(img):
    """Invert the colors of an image."""
    *rgb, a = img.convert('RGBA').split()
    return Image.merge('RGBA', (*map(ImageOps.invert, rgb), a))
