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

    if (
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

        # Clip image region and draw the nested document, keeping the clip
        # local to this element so it cannot affect later siblings.
        surface.context.save()
        surface.context.rectangle(x, y, width, height)
        surface.context.clip()
        surface.context.translate(x, y)
        surface.context.translate(*surface.context.get_current_point())
        surface.context.scale(scale_x * tree_scale_x, scale_y * tree_scale_y)
        surface.context.translate(translate_x, translate_y)
        surface.draw(tree)
        surface.context.restore()
        return
    else:
        # Every raster format goes through Pillow: this is the only path that
        # honors EXIF orientation tags and the optional map_image callback.
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
    # An explicitly zero dimension disables rendering (the element has no
    # viewport); a *missing* dimension instead falls back to the raster's
    # intrinsic size below.
    if (('width' in node and width == 0) or
            ('height' in node and height == 0)):
        return
    width = width or node.image_width
    height = height or node.image_height
    scale_x, scale_y, translate_x, translate_y = preserve_ratio(
        surface, node, width, height)

    # The image is always clipped to its viewport rectangle: "meet" leaves
    # gutters inside it, "slice" overflows it and "none" fills it after a
    # non-uniform scale -- in every case nothing may paint outside.
    surface.context.save()
    surface.context.rectangle(x, y, width, height)
    surface.context.clip()

    # Paint raster image. Real alpha is kept: transparent pixels composite
    # with whatever is below instead of being filled with an opaque color.
    opacity = float(node.get('opacity', 1))
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
