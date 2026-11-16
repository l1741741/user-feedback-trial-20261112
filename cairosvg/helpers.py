"""
Surface helpers.

"""

import re
from math import atan2, cos, hypot, radians, sin, tan

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

# Keyword values accepted by ``transform-origin`` (see CSS Transforms 1).
ORIGIN_X_KEYWORDS = {'left': 0, 'center': 0.5, 'right': 1}
ORIGIN_Y_KEYWORDS = {'top': 0, 'center': 0.5, 'bottom': 1}
# ``transform-box`` values that are actually honored; anything else (including
# the historical ``border-box`` / ``content-box``) falls back to view-box.
SUPPORTED_TRANSFORM_BOXES = frozenset(('view-box', 'fill-box'))


class PointError(Exception):
    """Exception raised when parsing a point fails."""


class TransformOriginError(Exception):
    """Exception raised when a ``transform-origin`` value cannot be resolved.

    Raising (and catching this locally) keeps a malformed declaration on one
    node from leaking state into the following nodes: the node is rendered
    without the origin adjustment instead of corrupting the Cairo context.

    """


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


def _parse_transform_functions(transform_string):
    """Split a ``transform`` attribute into ``(name, args_string)`` pairs."""
    return re.findall(
        r'(\w+) ?\( ?(.*?) ?\)', normalize(transform_string))


# Sentinels used only to select the percentage reference inside
# ``_origin_length`` while keeping the call sites readable.
_X_AXIS = object()
_Y_AXIS = object()


def _transform_reference_box(surface, node):
    """Return the reference box ``(x, y, width, height)`` for the origin.

    The box is always expressed in the local user space in which the node's
    own ``transform`` is applied, i.e. before the node's transform but after
    every ancestor transform already installed on the Cairo context.

    ``view-box`` (the default, matching the SVG 1.1 / CSS behavior for SVG
    elements) uses the nearest SVG viewport. ``fill-box`` uses the object
    bounding box computed from the node's geometry, excluding the node's own
    transform. Unknown values fall back to ``view-box``.

    Return ``None`` when a requested ``fill-box`` cannot be established
    (missing or degenerate geometry): the caller then ignores the origin.

    """
    box = (node.get('transform-box') or 'view-box').strip().lower()
    if box not in SUPPORTED_TRANSFORM_BOXES:
        # Unknown values (border-box, content-box, typos) use view-box.
        box = 'view-box'
    if box == 'fill-box':
        # Local import: bounding_box.py itself imports from this module.
        from .bounding_box import (
            calculate_bounding_box, is_non_empty_bounding_box)
        bounding_box = calculate_bounding_box(surface, node)
        if not is_non_empty_bounding_box(bounding_box):
            return None
        return bounding_box
    # The nearest SVG viewport as established by the active viewBox: its
    # origin is the viewBox min-x/min-y mapped into local user space, not
    # always (0, 0).
    view_x, view_y = getattr(surface, 'viewport_origin', (0, 0))
    return (view_x, view_y, surface.context_width or 0,
            surface.context_height or 0)


def resolve_transform_origin(surface, node):
    """Resolve ``node``'s ``transform-origin`` to a local-space ``(x, y)``.

    See https://www.w3.org/TR/css-transforms-1/#transform-origin-property

    Supported declarations:

    * one or two tokens, each being a length (``px`` and the other SVG/CSS
      units), a percentage, or a position keyword;
    * a single keyword or length applies to both axes when appropriate
      (``top``/``bottom`` imply a centered x, ``left``/``right`` imply a
      centered y, a bare length sets x and centers y);
    * percentages are relative to the reference box selected by
      ``transform-box``;
    * the default ``50% 50%`` is only applied when the attribute is present,
      preserving the SVG 1.1 behavior of rotating around (0, 0) otherwise.

    Return ``None`` when the attribute is absent or cannot be resolved. A
    failure never raises out of this function so that a broken declaration
    cannot affect the following nodes.

    """
    transform_origin = node.get('transform-origin')
    if not transform_origin:
        return None
    try:
        reference_box = _transform_reference_box(surface, node)
    except Exception:
        return None
    if reference_box is None:
        # fill-box requested but no usable geometry: behave as if no origin
        # was specified, per the spec's degenerate-object handling.
        return None
    box_x, box_y, box_width, box_height = reference_box

    tokens = normalize(transform_origin).split()
    if not tokens or len(tokens) > 2:
        return None
    try:
        if len(tokens) == 1:
            token = tokens[0].lower()
            if token in ('top', 'bottom'):
                x = box_x + box_width / 2
                y = box_y + ORIGIN_Y_KEYWORDS[token] * box_height
            elif token in ('left', 'right'):
                x = box_x + ORIGIN_X_KEYWORDS[token] * box_width
                y = box_y + box_height / 2
            elif token == 'center':
                x = box_x + box_width / 2
                y = box_y + box_height / 2
            else:
                x = box_x + _origin_length(surface, tokens[0], box_width,
                                           _X_AXIS)
                y = box_y + box_height / 2
        else:
            first, second = tokens[0].lower(), tokens[1].lower()
            # The vertical keyword may be written first ("top left").
            if first in ORIGIN_Y_KEYWORDS and first not in ORIGIN_X_KEYWORDS:
                y = box_y + ORIGIN_Y_KEYWORDS[first] * box_height
                x = box_x + _origin_length(surface, tokens[1], box_width,
                                           _X_AXIS)
            else:
                x = box_x + _origin_length(surface, tokens[0], box_width,
                                           _X_AXIS)
                y = box_y + _origin_length(surface, tokens[1], box_height,
                                           _Y_AXIS)
    except (TransformOriginError, ValueError, TypeError):
        return None
    return x, y


def _origin_length(surface, token, reference, axis):
    """Resolve one origin component against one reference-box dimension."""
    token = token.strip().lower()
    if not token:
        raise TransformOriginError('empty transform-origin component')
    keyword_map = (ORIGIN_X_KEYWORDS if axis is _X_AXIS
                   else ORIGIN_Y_KEYWORDS)
    fraction = keyword_map.get(token)
    if fraction is not None:
        return fraction * reference
    normalized = normalize(token)
    if normalized.endswith('%'):
        return float(normalized[:-1]) * reference / 100
    # Bare numbers and unit lengths. Reject anything the size parser can only
    # map to zero by ignorance, so typos such as "garbage" are ignored rather
    # than silently becoming an origin of 0.
    if not _looks_like_length(normalized):
        raise TransformOriginError(
            f'unrecognized transform-origin token {token!r}')
    reference_name = 'x' if axis is _X_AXIS else 'y'
    return size(surface, normalized, reference_name)


def _looks_like_length(token):
    """Check that ``token`` is a number or a number with a known unit."""
    if re.match(r'^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?$', token):
        return True
    known_units = tuple(UNITS) + ('%', 'em', 'ex', 'ch')
    return any(
        re.match(r'^[+-]?(\d+\.?\d*|\.\d+)(e[+-]?\d+)?'
                 + re.escape(unit) + r'$', token)
        for unit in known_units)


def transform(surface, transform_string, gradient=None,
              transform_origin=None, node=None):
    """Transform ``surface`` or ``gradient`` if supplied using ``string``.

    See http://www.w3.org/TR/SVG/coords.html#TransformAttribute

    ``transform_origin`` may be the raw ``transform-origin`` attribute value;
    when ``node`` is given it is resolved through
    :func:`resolve_transform_origin` (including ``transform-box``), otherwise
    the value is interpreted as two local-space coordinates for backward
    compatibility with gradient/pattern callers that never pass an origin
    today.

    The forward matrix is built directly in SVG application order (the first
    function listed in the attribute is applied first to the geometry), then
    inverted once before being handed to Cairo, whose matrices map user space
    onto device space. Malformed input is skipped without touching the
    context so later nodes keep a clean transformation state.

    """
    if not transform_string:
        return

    transformations = _parse_transform_functions(transform_string)
    if not transformations:
        return

    origin = None
    if node is not None:
        origin = resolve_transform_origin(surface, node)
    elif transform_origin:
        # Legacy path: explicit numeric origin in local user space.
        try:
            parts = normalize(transform_origin).split()
            origin = (float(parts[0]), float(parts[1]))
        except (ValueError, TypeError):
            origin = None

    matrix = cairo.Matrix()
    if origin is not None:
        matrix.translate(origin[0], origin[1])

    for transformation_type, transformation in transformations:
        try:
            values = [
                size(surface, value)
                for value in transformation.split(' ')]
            if transformation_type == 'matrix':
                if len(values) != 6:
                    continue
                # SVG matrix(a,b,c,d,e,f) maps onto cairo.Matrix(xx=a, yx=b,
                # xy=c, yy=d, x0=e, y0=f); multiply on the left so that
                # functions listed earlier act first on the geometry.
                matrix = cairo.Matrix(*values).multiply(matrix)
            elif transformation_type == 'rotate':
                angle = radians(float(values[0]))
                x, y = (values[1:3] if len(values) >= 3 else (0, 0))
                matrix.translate(x, y)
                matrix.rotate(angle)
                matrix.translate(-x, -y)
            elif transformation_type == 'skewX':
                tangent = tan(radians(float(values[0])))
                matrix = cairo.Matrix(
                    1, 0, tangent, 1, 0, 0).multiply(matrix)
            elif transformation_type == 'skewY':
                tangent = tan(radians(float(values[0])))
                matrix = cairo.Matrix(
                    1, tangent, 0, 1, 0, 0).multiply(matrix)
            elif transformation_type == 'translate':
                if len(values) == 1:
                    values.append(0)
                matrix.translate(values[0], values[1])
            elif transformation_type == 'scale':
                if not values:
                    continue
                if len(values) == 1:
                    values.append(values[0])
                matrix.scale(values[0], values[1])
            else:
                # Unknown function: ignore instead of aborting the document.
                continue
        except (ValueError, TypeError, IndexError):
            # Skip the malformed function but keep processing the remaining
            # ones; the context state stays consistent.
            continue

    if origin is not None:
        matrix.translate(-origin[0], -origin[1])

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
