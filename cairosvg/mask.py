"""
SVG ``<mask>`` rendering.

Two distinct mask semantics coexist in SVG and must never be conflated:

* **luminance masks** (the SVG 1.1 default, ``mask-type: luminance``): the
  mask content's *luminance* defines coverage and its alpha multiplies it.
  Black hides, white shows, colors contribute by their relative luminance,
  and transparent pixels hide regardless of their color.
* **alpha masks** (``mask-type: alpha``, SVG 2): only the mask content's
  alpha channel defines coverage; the RGB channels are ignored.

Cairo's ``Context.mask_surface`` only implements alpha masking, so the mask
content is rendered to an image surface and converted into an alpha-only
mask surface in Python, following the SVG compositing rules.

Supported and unsupported features are documented in ``MASKING.md``.

"""

import copy
import sys

from .bounding_box import (
    calculate_bounding_box, is_non_empty_bounding_box)
from .helpers import size
from .surface import cairo
# sRGB coefficients of the Rec. 709 / sRGB luminance function.
LUMA_R = 0.2126
LUMA_G = 0.7152
LUMA_B = 0.0722

_LITTLE_ENDIAN = sys.byteorder == 'little'


def _mask_type(mask_node):
    """Return ``'luminance'`` or ``'alpha'`` for ``mask_node``.

    Only the ``mask-type`` attribute on the ``<mask>`` element itself is
    honored; per-child CSS overrides are not supported (see MASKING.md).
    """
    mask_type = mask_node.get('mask-type', 'luminance').strip().lower()
    return 'alpha' if mask_type == 'alpha' else 'luminance'


def _linear_rgb(mask_node):
    """Whether mask values are computed in linearized RGB."""
    return mask_node.get('color-interpolation', 'auto') == 'linearRGB'


def _srgb_to_linear(value):
    """Inverse sRGB gamma encoding, used for ``linearRGB`` interpolation."""
    return value / 12.92 if value <= 0.04045 else (
        ((value + 0.055) / 1.055) ** 2.4)


def _mask_region(surface, node, mask_node):
    """Resolve the mask region ``(x, y, width, height)`` in user units.

    Returns ``None`` when the region is empty, in which case the mask hides
    the whole element.
    """
    if mask_node.get('maskUnits') == 'userSpaceOnUse':
        x = size(surface, mask_node.get('x', '-10%'), 'x')
        y = size(surface, mask_node.get('y', '-10%'), 'y')
        width = size(surface, mask_node.get('width', '120%'), 'x')
        height = size(surface, mask_node.get('height', '120%'), 'y')
    else:
        # objectBoundingBox: lengths are fractions of the masked element's
        # bounding box. A bare number and a percentage both scale with the
        # box; routing them through size() with the box size as reference
        # would collapse "120%" to 1.2 user units, so the multiplication is
        # done here instead.
        bounding_box = calculate_bounding_box(surface, node)
        if not is_non_empty_bounding_box(bounding_box):
            # No geometry to derive a box from: fall back to the viewport,
            # like the gradient and pattern code does elsewhere.
            box_x = box_y = 0
            box_width = surface.context_width
            box_height = surface.context_height
        else:
            box_x, box_y, box_width, box_height = bounding_box
        x = box_x + box_width * size(surface, mask_node.get('x', '-10%'), 1)
        y = box_y + box_height * size(surface, mask_node.get('y', '-10%'), 1)
        width = box_width * size(
            surface, mask_node.get('width', '120%'), 1)
        height = box_height * size(
            surface, mask_node.get('height', '120%'), 1)

    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def _make_wrapper(mask_node, region, content_matrix):
    """Build a throwaway ``<g>`` node rendering the mask content.

    The original definition node is never mutated: the same mask can be
    reused by several elements, including siblings, with independent
    geometry.

    The sub-surface is sized to the mask region in device pixels. The
    wrapper's ``transform`` maps the mask content's coordinate system onto
    that buffer:

    * the region translation (``-x, -y``) makes parent-user-space content
      land at the right place in the region-sized image;
    * the objectBoundingBox content matrix additionally scales the
      (0..1) content space onto the masked element's bounding box.

    So the resulting alpha image already covers the region in parent user
    space and needs no further transform when it is applied.
    """
    x, y, width, height = region
    wrapper = copy.copy(mask_node)
    wrapper.children = mask_node.children
    wrapper.tag = 'g'
    # The mask content carries its own opacity semantics (folded into the
    # mask value); the masked element's group opacity is applied separately
    # by the caller and must not leak in here.
    wrapper['opacity'] = '1'
    wrapper['x'] = '0'
    wrapper['y'] = '0'
    wrapper['width'] = str(width)
    wrapper['height'] = str(height)
    wrapper.pop('viewBox', None)

    # Cairo Matrix(a, b, c, d, e, f) == SVG matrix(a b c d e f).
    matrix = cairo.Matrix(xx=1, yy=1)
    matrix.translate(-x, -y)
    matrix = content_matrix.multiply(matrix)
    wrapper['transform'] = (
        f'matrix({matrix.xx} {matrix.yx} {matrix.xy} {matrix.yy} '
        f'{matrix.x0} {matrix.y0})')
    return wrapper


def _content_matrix(surface, node, mask_node):
    """Matrix mapping objectBoundingBox mask content into parent user space.

    Returns the identity for ``userSpaceOnUse`` content (the default).
    """
    matrix = cairo.Matrix(xx=1, yy=1)
    if mask_node.get('maskContentUnits') == 'objectBoundingBox':
        bounding_box = calculate_bounding_box(surface, node)
        if is_non_empty_bounding_box(bounding_box):
            box_x, box_y, box_width, box_height = bounding_box
            matrix.translate(box_x, box_y)
            matrix.scale(box_width, box_height)
    return matrix


def _to_alpha_mask(mask_surface, mask_node):
    """Convert a rendered mask image into an alpha-only image surface.

    The returned surface has the same device-pixel dimensions as the input;
    its RGB channels are black and its alpha channel is the SVG mask value
    (luminance × content alpha, or content alpha alone for alpha masks).
    """
    source = mask_surface.cairo
    source.flush()
    width, height = source.get_width(), source.get_height()
    stride = source.get_stride()
    data = bytearray(source.get_data())
    linear = _linear_rgb(mask_node)
    luminance_mode = _mask_type(mask_node) == 'luminance'

    # ARGB32 is stored premultiplied. In memory the bytes are B,G,R,A on
    # little-endian machines (and A,R,G,B on big-endian ones).
    if _LITTLE_ENDIAN:
        b_index, g_index, r_index, a_index = 0, 1, 2, 3
    else:  # pragma: no cover - no big-endian CI, but keep the mapping true
        a_index, r_index, g_index, b_index = 0, 1, 2, 3

    for row in range(height):
        row_start = row * stride
        for column in range(width):
            offset = row_start + column * 4
            alpha = data[offset + a_index] / 255
            if alpha == 0:
                # Fully transparent mask pixels hide everything, whatever
                # the (undefined, often zeroed by premultiplication) color.
                coverage = 0
            else:
                r = data[offset + r_index] / 255 / alpha
                g = data[offset + g_index] / 255 / alpha
                b = data[offset + b_index] / 255 / alpha
                if luminance_mode:
                    if linear:
                        r = _srgb_to_linear(r)
                        g = _srgb_to_linear(g)
                        b = _srgb_to_linear(b)
                    coverage = LUMA_R * r + LUMA_G * g + LUMA_B * b
                else:
                    coverage = 1
            value = max(0, min(255, round(coverage * alpha * 255)))
            data[offset] = 0
            data[offset + 1] = 0
            data[offset + 2] = 0
            data[offset + 3] = value

    result = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    result.flush()
    result.get_data()[:] = bytes(data)
    result.mark_dirty()
    return result


def paint_mask(surface, node, name, opacity):
    """Paint the current group source through the mask named ``name``.

    The group source is already set on the context by the caller. The
    element's group ``opacity`` is applied exactly once when the masked
    result is composited, and the mask content's own opacity is part of the
    mask value rather than being multiplied into the element.
    """
    mask_node = surface.masks[name]
    region = _mask_region(surface, node, mask_node)
    if region is None:
        # An empty mask region hides the element entirely.
        surface.context.paint_with_alpha(0)
        return
    x, y, width, height = region

    from .surface import PNGSurface
    content_matrix = _content_matrix(surface, node, mask_node)
    wrapper = _make_wrapper(mask_node, region, content_matrix)
    mask_surface = PNGSurface(
        wrapper, None, surface.dpi, surface,
        parent_width=width, parent_height=height)
    alpha_surface = _to_alpha_mask(mask_surface, mask_node)

    context = surface.context
    # Render the masked result into a temporary group so that the element's
    # own opacity is applied after masking, never multiplied twice.
    context.push_group()
    context.save()
    # Everything outside the mask region stays fully transparent: the
    # masking operation is clipped to the region.
    context.rectangle(x, y, width, height)
    context.clip()
    context.translate(x, y)
    # The alpha image already contains the mask content mapped onto the
    # region (including any objectBoundingBox content scaling); only map its
    # device pixels back to region-sized user units.
    context.scale(
        width / mask_surface.width, height / mask_surface.height)
    context.mask_surface(alpha_surface)
    context.restore()
    context.pop_group_to_source()
    context.paint_with_alpha(opacity)
