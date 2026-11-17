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
    """Return a copy of ``img`` with its colors replaced by their complement.

    The alpha channel, palette transparency and image geometry are left
    untouched, and the input image is never modified: transparent pixels
    stay transparent instead of being flattened onto a solid color.

    Paletted images (``P``) keep their palette: the palette colors are
    inverted while color indices and the transparency information stay
    as-is. CMYK images are inverted channel by channel in CMYK space.
    Modes without 8-bit color channels (``I``, ``I;16``, ``F``…) are
    converted through RGBA first, which clips channel values to the
    0–255 range; inverting them is therefore only approximate.

    """
    if img.mode in ('1', 'L', 'RGB'):
        return ImageOps.invert(img)
    if img.mode == 'P' and img.getpalette() is not None:
        # Invert palette colors, keep indices and transparency as-is
        return ImageOps.invert(img)
    if img.mode == 'CMYK':
        # Complement each channel in CMYK space
        return Image.merge(
            'CMYK', [ImageOps.invert(band) for band in img.split()])
    if img.mode in ('RGBA', 'LA'):
        # Invert color channels, keep alpha as-is
        *color, alpha = img.split()
        return Image.merge(
            img.mode, (*[ImageOps.invert(band) for band in color], alpha))
    # Other modes (I, I;16, F, HSV, YCbCr…): invert through 8-bit RGBA
    *rgb, alpha = img.convert('RGBA').split()
    return Image.merge(
        'RGBA', (*[ImageOps.invert(band) for band in rgb], alpha))
