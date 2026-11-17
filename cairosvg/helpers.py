"""
Surface helpers.

"""

import re
from math import atan2, cos, hypot, isfinite, radians, sin, tan

from .surface import cairo
from .url import parse_url

UNITS = {
    'mm': 1 / 25.4,
    'cm': 1 / 2.54,
    'in': 1,
    'pt': 1 / 72,
    'pc': 1 / 6,
    'px': None,
}

PAINT_URL = re.compile(r'(url\(.+\)) *(.*)')
PATH_LETTERS = 'achlmqstvzACHLMQSTVZ'
RECT = re.compile(r'rect\( ?(.+?) ?\)')


class PointError(Exception):
    """Exception raised when parsing a point fails."""


def distance(x1, y1, x2, y2):
    """Get the distance between two points."""
    return hypot(x2 - x1, y2 - y1)


def paint(value):
    """Extract from value an uri and a color.

    See http://www.w3.org/TR/SVG/painting.html#SpecifyingPaint

    """
    if not value:
        return None, None

    value = value.strip()
    match = PAINT_URL.search(value)
    if match:
        source = parse_url(match.group(1)).fragment
        color = match.group(2) or None
    else:
        source = None
        color = value or None

    return (source, color)


def node_format(surface, node, reference=True):
    """Return ``(width, height, viewbox)`` of ``node``.

    If ``reference`` is ``True``, we can rely on surface size to resolve
    percentages.

    """
    reference_size = 'xy' if reference else (0, 0)
    width = size(surface, node.get('width', '100%'), reference_size[0])
    height = size(surface, node.get('height', '100%'), reference_size[1])
    viewbox = node.get('viewBox')
    if viewbox:
        viewbox = re.sub('[ \n\r\t,]+', ' ', viewbox)
        viewbox = tuple(float(position) for position in viewbox.split())
        width = width or viewbox[2]
        height = height or viewbox[3]
    return width, height, viewbox


def normalize(string):
    """Normalize a string corresponding to an array of various values."""
    string = string.replace('E', 'e')
    string = re.sub('(?<!e)-', ' -', string)
    string = re.sub('[ \n\r\t,]+', ' ', string)
    string = re.sub(r'(\.[0-9-]+)(?=\.)', r'\1 ', string)
    return string.strip()


def point(surface, string):
    """Return ``(x, y, trailing_text)`` from ``string``."""
    match = re.match('(.*?) (.*?)(?: |$)', string)
    if match:
        x, y = match.group(1, 2)
        string = string[match.end():]
        return (size(surface, x, 'x'), size(surface, y, 'y'), string)
    else:
        raise PointError


def point_angle(cx, cy, px, py):
    """Return angle between x axis and point knowing given center."""
    return atan2(py - cy, px - cx)


def preserve_ratio(surface, node, width=None, height=None):
    """Manage the ratio preservation."""
    if node.tag == 'marker':
        width = width or size(surface, node.get('markerWidth', '3'), 'x')
        height = height or size(surface, node.get('markerHeight', '3'), 'y')
        _, _, viewbox = node_format(surface, node)
        viewbox_width, viewbox_height = viewbox[2:]
    elif node.tag in ('svg', 'image', 'g'):
        node_width, node_height, _ = node_format(surface, node)
        width = width or node_width
        height = height or node_height
        viewbox_width, viewbox_height = node.image_width, node.image_height
    else:
        raise TypeError(
            f'Root node is {node.tag}. Should be one of '
            'marker, svg, image, or g.'
        )

    translate_x = 0
    translate_y = 0
    scale_x = width / viewbox_width if viewbox_width > 0 else 1
    scale_y = height / viewbox_height if viewbox_height > 0 else 1

    aspect_ratio = node.get('preserveAspectRatio', 'xMidYMid').split()
    align = aspect_ratio[0]
    if align == 'none':
        x_position = 'min'
        y_position = 'min'
    else:
        meet_or_slice = aspect_ratio[1] if len(aspect_ratio) > 1 else None
        if meet_or_slice == 'slice':
            scale_value = max(scale_x, scale_y)
        else:
            scale_value = min(scale_x, scale_y)
        scale_x = scale_y = scale_value
        x_position = align[1:4].lower()
        y_position = align[5:].lower()

    if node.tag == 'marker':
        translate_x = -size(surface, node.get('refX', '0'), 'x')
        translate_y = -size(surface, node.get('refY', '0'), 'y')
    else:
        translate_x = 0
        if x_position == 'mid':
            translate_x = (width / scale_x - viewbox_width) / 2
        elif x_position == 'max':
            translate_x = width / scale_x - viewbox_width

        translate_y = 0
        if y_position == 'mid':
            translate_y += (height / scale_y - viewbox_height) / 2
        elif y_position == 'max':
            translate_y += height / scale_y - viewbox_height

    return scale_x, scale_y, translate_x, translate_y


def clip_marker_box(surface, node, scale_x, scale_y):
    """Get the clip ``(x, y, width, height)`` of the marker box."""
    width = size(surface, node.get('markerWidth', '3'), 'x')
    height = size(surface, node.get('markerHeight', '3'), 'y')
    _, _, viewbox = node_format(surface, node)
    viewbox_width, viewbox_height = viewbox[2:]

    align = node.get('preserveAspectRatio', 'xMidYMid').split(' ')[0]
    x_position = 'min' if align == 'none' else align[1:4].lower()
    y_position = 'min' if align == 'none' else align[5:].lower()

    clip_x = viewbox[0]
    if x_position == 'mid':
        clip_x += (viewbox_width - width / scale_x) / 2.
    elif x_position == 'max':
        clip_x += viewbox_width - width / scale_x

    clip_y = viewbox[1]
    if y_position == 'mid':
        clip_y += (viewbox_height - height / scale_y) / 2.
    elif y_position == 'max':
        clip_y += viewbox_height - height / scale_y

    return clip_x, clip_y, width / scale_x, height / scale_y


def quadratic_points(x1, y1, x2, y2, x3, y3):
    """Return the quadratic points to create quadratic curves."""
    xq1 = x2 * 2 / 3 + x1 / 3
    yq1 = y2 * 2 / 3 + y1 / 3
    xq2 = x2 * 2 / 3 + x3 / 3
    yq2 = y2 * 2 / 3 + y3 / 3
    return xq1, yq1, xq2, yq2, x3, y3


def rotate(x, y, angle):
    """Rotate a point of an angle around the origin point."""
    return x * cos(angle) - y * sin(angle), y * cos(angle) + x * sin(angle)


# CSS keyword offsets for the two transform-origin axes. Each value is the
# fraction of the reference box dimension added to its origin.
_ORIGIN_X_KEYWORDS = {'left': 0, 'center': .5, 'right': 1}
_ORIGIN_Y_KEYWORDS = {'top': 0, 'center': .5, 'bottom': 1}


def _origin_coordinate(surface, token, reference_box, axis):
    """Resolve one transform-origin component to user-space coordinates.

    ``axis`` is ``'x'`` or ``'y'``. ``reference_box`` is the
    ``(x, y, width, height)`` tuple used to resolve percentages and CSS
    keywords. An invalid or unparsable token resolves to the center of the
    reference box instead of raising, so a malformed component can never
    poison the rendering of the node or of any following sibling.

    """
    box_x, box_y, box_width, box_height = reference_box
    keywords = _ORIGIN_X_KEYWORDS if axis == 'x' else _ORIGIN_Y_KEYWORDS
    box_origin = box_x if axis == 'x' else box_y
    box_size = box_width if axis == 'x' else box_height
    token = (token or '').strip().lower()

    if token in keywords:
        return box_origin + keywords[token] * box_size

    try:
        if token.endswith('%'):
            percentage = float(token[:-1])
            return box_origin + percentage * box_size / 100
        # Explicit lengths (px, pt, em, unitless user units, ...). The
        # bare ``size()`` helper returns 0 on unparsable input, so validate
        # the token first: an illegal coordinate then falls back to the
        # box center instead of the box origin.
        if not _is_length_token(token):
            return box_origin + box_size / 2
        return box_origin + size(surface, token, axis)
    except (TypeError, ValueError):
        return box_origin + box_size / 2


def _is_length_token(token):
    """Return whether ``token`` is a parseable SVG length or percentage."""
    if not token:
        return False
    if token.endswith('%'):
        token = token[:-1]
    else:
        for unit in ('em', 'ex', 'ch', *UNITS):
            if token.endswith(unit):
                token = token[:-len(unit)]
                break
    try:
        float(token)
    except ValueError:
        return False
    return True


def get_transform_reference_box(surface, node):
    """Return the reference box used to resolve ``transform-origin``.

    See https://www.w3.org/TR/css-transforms-1/#transform-box

    The box is returned as an ``(x, y, width, height)`` tuple in the local
    coordinate system of ``node`` (i.e. before the node's own transform is
    applied).

    Supported ``transform-box`` values:

    * ``view-box`` (the default kept for backwards compatibility, matching
      the SVG 1.1 behaviour of resolving origins against the nearest SVG
      viewport): the nearest SVG viewport, positioned at its origin.
    * ``fill-box``: the object bounding box of the element, computed from
      its geometry. When the box is missing or degenerate (zero width or
      height), resolution falls back to the viewport box.
    * ``content-box`` and ``border-box``: SVG geometry has no CSS padding or
      border, so they are treated as ``fill-box``.
    * ``stroke-box``: CairoSVG does not compute stroke extents, so it is
      treated as ``fill-box``.

    Any unknown or invalid value falls back to ``view-box``.

    """
    # Imported here to avoid a circular import at module load time.
    from .bounding_box import (
        calculate_bounding_box, is_non_empty_bounding_box)

    viewport_box = (
        0, 0, surface.context_width or 0, surface.context_height or 0)

    transform_box = (node.get('transform-box') or 'view-box').strip().lower()

    if transform_box in ('fill-box', 'stroke-box',
                         'content-box', 'border-box'):
        try:
            bounding_box = calculate_bounding_box(surface, node)
        except Exception:
            bounding_box = None
        if is_non_empty_bounding_box(bounding_box):
            return tuple(float(value) for value in bounding_box)
        # Degenerate (zero-width/height) or unavailable object box: the
        # specification leaves origins on an empty box undefined; use the
        # viewport instead of producing NaN coordinates.
        return viewport_box

    # 'view-box' and any unrecognized value use the nearest SVG viewport.
    return viewport_box


def parse_transform_origin(surface, node):
    """Resolve ``node``'s ``transform-origin`` to an ``(x, y)`` tuple.

    See https://www.w3.org/TR/css-transforms-1/#transform-origin-property

    Returns ``None`` when no usable origin is declared, in which case the
    transform is applied about the local coordinate origin ``(0, 0)`` as in
    SVG 1.1. One or two components are accepted; a missing second component
    defaults to ``center``. Percentage components are fractions of the
    reference box dimensions, keyword and explicit-length components are
    supported as well.

    The function never raises: an illegal declaration is ignored (the node
    is transformed as if the property were absent) and cannot leak any
    state to other nodes.

    """
    value = node.get('transform-origin')
    if not value:
        return None

    tokens = normalize(value).split(' ')
    # A legal declaration has one or two components.
    if not 1 <= len(tokens) <= 2:
        return None

    x_token, y_token = tokens[0], None
    if len(tokens) == 2:
        y_token = tokens[1]

    # A vertical keyword given alone applies to the y axis; the missing
    # horizontal component defaults to center.
    if x_token in _ORIGIN_Y_KEYWORDS:
        y_token, x_token = x_token, 'center'
    if y_token is None:
        y_token = 'center'

    try:
        reference_box = get_transform_reference_box(surface, node)
        origin_x = _origin_coordinate(
            surface, x_token, reference_box, 'x')
        origin_y = _origin_coordinate(
            surface, y_token, reference_box, 'y')
    except Exception:
        return None

    if not all(isfinite(value) for value in (origin_x, origin_y)):
        return None
    return origin_x, origin_y


def _legacy_transform_origin(surface, transform_origin):
    """Resolve a raw transform-origin string for direct ``transform()`` calls.

    This mirrors the historical keyword/length resolution (against the
    current viewport) and is only used when ``transform()`` is invoked
    directly with a string rather than through a rendered node, which goes
    through :func:`parse_transform_origin`. Percentages and illegal tokens
    are handled defensively: a failure returns ``None`` so the node is
    transformed about ``(0, 0)`` instead of raising.

    """
    try:
        tokens = normalize(transform_origin).split(' ')
        if not 1 <= len(tokens) <= 2:
            return None
        if len(tokens) == 1:
            x_token, y_token = tokens[0], 'center'
        else:
            x_token, y_token = tokens
        if x_token in _ORIGIN_Y_KEYWORDS:
            x_token = 'center'

        width = surface.context_width or 0
        height = surface.context_height or 0
        if x_token in _ORIGIN_X_KEYWORDS:
            origin_x = _ORIGIN_X_KEYWORDS[x_token] * width
        else:
            origin_x = size(surface, x_token, 'x')
        if y_token in _ORIGIN_Y_KEYWORDS:
            origin_y = _ORIGIN_Y_KEYWORDS[y_token] * height
        else:
            origin_y = size(surface, y_token, 'y')
        if all(isfinite(value) for value in (origin_x, origin_y)):
            return origin_x, origin_y
    except (AttributeError, TypeError, ValueError):
        pass
    return None


def transform(surface, transform_string, gradient=None,
              transform_origin=None, origin_xy=None):
    """Transform ``surface`` or ``gradient`` if supplied using ``string``.

    See http://www.w3.org/TR/SVG/coords.html#TransformAttribute

    The rotation center can be supplied either as a raw
    ``transform_origin`` CSS string (kept for backwards compatibility with
    direct callers) or as a pre-resolved ``origin_xy`` ``(x, y)`` tuple in
    local user units. When both are given, ``origin_xy`` wins.

    """
    if not transform_string and not (transform_origin or origin_xy):
        return

    transformations = re.findall(
        r'(\w+) ?\( ?(.*?) ?\)', normalize(transform_string or ''))
    matrix = cairo.Matrix()

    # Resolve the origin once, outside the matrix bookkeeping, so that a
    # malformed value can never leave a half-applied translate behind.
    if origin_xy is None and transform_origin:
        origin_xy = _legacy_transform_origin(surface, transform_origin)

    if origin_xy is not None:
        origin_x, origin_y = origin_xy
        matrix.translate(origin_x, origin_y)

    for transformation_type, transformation in transformations:
        try:
            values = [
                size(surface, value)
                for value in transformation.split(' ')]
        except (TypeError, ValueError):
            # Skip this malformed transform operation but keep processing
            # the remaining ones instead of aborting the whole node.
            continue
        if transformation_type == 'matrix' and len(values) == 6:
            matrix = cairo.Matrix(*values).multiply(matrix)
        elif transformation_type == 'rotate' and values:
            angle = radians(float(values.pop(0)))
            x, y = values[:2] if len(values) >= 2 else (0, 0)
            matrix.translate(x, y)
            matrix.rotate(angle)
            matrix.translate(-x, -y)
        elif transformation_type == 'skewX' and values:
            tangent = tan(radians(float(values[0])))
            matrix = cairo.Matrix(1, 0, tangent, 1, 0, 0).multiply(matrix)
        elif transformation_type == 'skewY' and values:
            tangent = tan(radians(float(values[0])))
            matrix = cairo.Matrix(1, tangent, 0, 1, 0, 0).multiply(matrix)
        elif transformation_type == 'translate' and values:
            if len(values) == 1:
                values += (0,)
            matrix.translate(*values[:2])
        elif transformation_type == 'scale' and values:
            if len(values) == 1:
                values = 2 * values
            matrix.scale(*values[:2])

    if origin_xy is not None:
        matrix.translate(-origin_x, -origin_y)

    try:
        matrix.invert()
    except cairo.Error:
        # Matrix not invertible, clip the surface to an empty path
        active_path = surface.context.copy_path()
        surface.context.new_path()
        surface.context.clip()
        surface.context.append_path(active_path)
    else:
        if gradient:
            # When applied on gradient use already inverted matrix (mapping
            # from user space to gradient space)
            matrix_now = gradient.get_matrix()
            gradient.set_matrix(matrix_now.multiply(matrix))
        else:
            matrix.invert()
            surface.context.transform(matrix)


def clip_rect(string):
    """Parse the rect value of a clip."""
    match = RECT.search(normalize(string or ''))
    return match.group(1).split(' ') if match else []


def rotations(node):
    """Retrieves the original rotations of a `text` or `tspan` node."""
    if 'rotate' in node:
        original_rotate = [
            float(i) for i in normalize(node['rotate']).strip().split(' ')]
        return original_rotate
    return []


def pop_rotation(node, original_rotate, rotate):
    """Removes the rotations of a node that are already used."""
    node['rotate'] = ' '.join(
        str(rotate.pop(0) if rotate else original_rotate[-1])
        for i in range(len(node.text)))


def zip_letters(xl, yl, dxl, dyl, rl, word):
    """Returns a list with the current letter's positions (x, y and rotation).

    E.g.: for letter 'L' with positions x = 10, y = 20 and rotation = 30:
    >>> [[10, 20, 30], 'L']

    Store the last value of each position and pop the first one in order to
    avoid setting an x,y or rotation value that have already been used.

    """
    return (
        ([pl.pop(0) if pl else None for pl in (xl, yl, dxl, dyl, rl)], char)
        for char in word)


def flatten(node):
    """Flatten the text of a node and its children."""
    flattened_text = [node.text or '']
    for child in list(node):
        flattened_text.append(flatten(child))
        flattened_text.append(child.tail or '')
        node.remove(child)
    return ''.join(flattened_text)


def size(surface, string, reference='xy'):
    """Replace a ``string`` with units by a float value.

    If ``reference`` is a float, it is used as reference for percentages. If it
    is ``'x'``, we use the viewport width as reference. If it is ``'y'``, we
    use the viewport height as reference. If it is ``'xy'``, we use
    ``hypot(viewport_width, viewport_height) / 2 ** .5`` as reference.

    """
    if not string:
        return 0

    try:
        return float(string)
    except ValueError:
        # Not a float, try something else
        pass

    # No surface (for parsing only)
    if surface is None:
        return 0

    string = normalize(string).split(' ', 1)[0]
    if string.endswith('%'):
        if reference == 'x':
            reference = surface.context_width or 0
        elif reference == 'y':
            reference = surface.context_height or 0
        elif reference == 'xy':
            reference = (
                hypot(surface.context_width, surface.context_height) / 2 ** .5
            )
        return float(string[:-1]) * reference / 100
    elif string.endswith('em'):
        return surface.font_size * float(string[:-2])
    elif string.endswith('ex'):
        # Assume that 1em == 2ex
        return surface.font_size * float(string[:-2]) / 2
    elif string.endswith('ch'):
        # A '0' must be assumed to be 0.5em wide.
        return surface.font_size * float(string[:-2]) / 2

    for unit, coefficient in UNITS.items():
        if string.endswith(unit):
            number = float(string[:-len(unit)])
            return number * (surface.dpi * coefficient if coefficient else 1)

    # Unknown size
    return 0
