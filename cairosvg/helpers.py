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


# ``transform`` functions allowed in the SVG transform attribute. The CSS
# Transform Level 2 functions (``translateX``, ``rotate3d``…) are *not* valid
# here: an unknown function makes the whole attribute invalid.
TRANSFORM_FUNCTIONS = {
    'matrix': 6,
    'rotate': (1, 3),
    'scale': (1, 2),
    'skewX': 1,
    'skewY': 1,
    'translate': (1, 2),
}

TRANSFORM_TOKEN = re.compile(
    r'\s*([a-zA-Z]+)\s*\(\s*([^()]*?)\s*\)\s*')
NUMBER = re.compile(r'[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?$', re.I)

# Keywords accepted by ``transform-origin`` and the value they stand for.
ORIGIN_KEYWORDS = {
    'left': (0., 'x'),
    'right': (1., 'x'),
    'top': (0., 'y'),
    'bottom': (1., 'y'),
    'center': (.5, 'xy'),
}

# Reference boxes supported for ``transform-box``. ``border-box`` and
# ``content-box`` are only defined for CSS layout boxes; for SVG elements they
# are treated as ``stroke-box`` and ``fill-box`` respectively.
TRANSFORM_BOXES = (
    'view-box', 'fill-box', 'stroke-box', 'border-box', 'content-box')

LENGTH_UNITS = (
    ('px', None), ('in', 96.), ('cm', 96. / 2.54), ('mm', 96. / 25.4),
    ('pt', 96. / 72.), ('pc', 96. / 6.))


def parse_transform(transform_string):
    """Parse an SVG ``transform`` attribute into a list of operations.

    Return ``[(name, [float, ...]), ...]`` or ``None`` when the attribute is
    invalid. According to the SVG specification a single invalid function
    invalidates the whole attribute; ``None`` therefore means "no transform".
    The parser is strict on purpose: percentages, CSS units and the CSS-only
    transform functions are rejected, as browsers do.
    """
    if transform_string is None:
        return None
    string = transform_string.strip()
    if not string:
        return []
    operations = []
    position = 0
    while position < len(string):
        match = TRANSFORM_TOKEN.match(string, position)
        if not match:
            return None
        name, arguments = match.groups()
        arity = TRANSFORM_FUNCTIONS.get(name)
        if arity is None:
            return None
        arguments = arguments.replace(',', ' ').split()
        if not all(NUMBER.match(argument) for argument in arguments):
            return None
        if isinstance(arity, tuple):
            if len(arguments) not in arity:
                return None
        elif len(arguments) != arity:
            return None
        operations.append((name, [float(argument) for argument in arguments]))
        position = match.end()
    return operations


def parse_origin_token(token):
    """Parse one ``transform-origin`` token.

    Return ``(value, axis)`` where ``value`` is a number or a keyword, and
    ``axis`` is ``'x'``, ``'y'`` or ``'xy'`` (``center`` is valid on both
    axes). Lengths are kept as strings, to be resolved by the caller.
    """
    token = token.strip().lower()
    if token in ORIGIN_KEYWORDS:
        return ORIGIN_KEYWORDS[token]
    return token, None


def absolute_length(string, font_size):
    """Convert an absolute length to user units, or ``None`` if invalid.

    Percentages are not accepted here; they are resolved by the caller
    against the reference box. Relative units use the element font size.
    """
    string = string.strip().lower()
    try:
        return float(string)
    except ValueError:
        pass
    for unit, factor in LENGTH_UNITS:
        if string.endswith(unit) and len(string) > len(unit):
            try:
                number = float(string[:-len(unit)])
            except ValueError:
                return None
            return number if factor is None else number * factor
    for unit, factor in (('em', 1.), ('ex', .5), ('ch', .5)):
        if string.endswith(unit) and len(string) > len(unit):
            try:
                return float(string[:-len(unit)]) * factor * font_size
            except ValueError:
                return None
    return None


def is_length(token):
    """Whether ``token`` looks like a length or a percentage."""
    if token.endswith('%'):
        try:
            float(token[:-1])
        except ValueError:
            return False
        return True
    return absolute_length(token, 12) is not None


def _keyword_origin(token, axis, reference_size):
    """Resolve a keyword token on one axis, or ``None`` if invalid there."""
    value, token_axis = token
    if token_axis not in ('xy', axis):
        return None
    return value * reference_size


def _length_origin(token, reference_size, font_size):
    """Resolve a length or percentage token, or ``None`` if invalid."""
    value, token_axis = token
    if token_axis is not None:
        return None
    if value.endswith('%'):
        try:
            return float(value[:-1]) * reference_size / 100
        except ValueError:
            return None
    return absolute_length(value, font_size)


def parse_transform_origin(transform_origin, box, font_size=12):
    """Parse a ``transform-origin`` value against a reference box.

    ``box`` is a ``(x, y, width, height)`` tuple expressed in the element's
    user coordinates. Return ``(origin_x, origin_y)``, or ``None`` when the
    value is invalid; callers fall back to the box origin, which is the
    property's initial ``0 0``.

    The syntax follows CSS Transforms Level 1: one or two values, or three
    values whose last one is a length (the z offset, ignored for 2D drawing).
    Percentages and keywords are resolved against the reference box.
    """
    if not transform_origin:
        return None
    tokens = transform_origin.replace(',', ' ').split()
    if not 1 <= len(tokens) <= 3:
        return None
    if len(tokens) == 3:
        # Third value is a z offset: a plain length, ignored for 2D drawing.
        if '%' in tokens[2] or not is_length(tokens[2]):
            return None
        tokens = tokens[:2]

    parsed = [parse_origin_token(token) for token in tokens]
    for (value, axis) in parsed:
        if axis is None and not is_length(value):
            return None

    x, y, width, height = box

    def resolve_x(token):
        if token[1] is not None:
            return _keyword_origin(token, 'x', width)
        return _length_origin(token, width, font_size)

    def resolve_y(token):
        if token[1] is not None:
            return _keyword_origin(token, 'y', height)
        return _length_origin(token, height, font_size)

    if len(parsed) == 1:
        axis = parsed[0][1]
        if axis == 'x':
            origin_x, origin_y = resolve_x(parsed[0]), height / 2
        elif axis == 'y':
            origin_x, origin_y = width / 2, resolve_y(parsed[0])
        else:
            origin_x, origin_y = resolve_x(parsed[0]), resolve_y(parsed[0])
    else:
        first, second = parsed
        first_axis, second_axis = first[1], second[1]
        if first_axis in ('x', 'y') and second_axis in ('x', 'y'):
            # Two keywords: they must describe different axes.
            if first_axis == second_axis:
                return None
            if first_axis == 'x':
                origin_x, origin_y = resolve_x(first), resolve_y(second)
            else:
                origin_x, origin_y = resolve_x(second), resolve_y(first)
        else:
            # At least one value is a length/percentage; the other may be a
            # keyword, which resolve_* rejects on the wrong axis.
            origin_x, origin_y = resolve_x(first), resolve_y(second)
    if origin_x is None or origin_y is None:
        return None
    return x + origin_x, y + origin_y


def transform_matrix(surface, transform_string, transform_origin=None,
                     box=None, font_size=None):
    """Return the matrix for an SVG ``transform``, or ``None``.

    ``None`` means the transform is invalid (and must be ignored). The
    returned matrix is the user-space matrix (not inverted), composed in SVG
    order: the leftmost operation is the outermost one.

    ``transform_origin`` is resolved against ``box`` (in the element's user
    coordinates), defaulting to the current viewport when ``box`` is ``None``.
    """
    if not transform_string and not transform_origin:
        return None

    operations = parse_transform(transform_string or '')
    if operations is None:
        return None

    origin = None
    if transform_origin:
        if box is None:
            box = (0, 0,
                   getattr(surface, 'viewbox_width', None) or
                   surface.context_width or 0,
                   getattr(surface, 'viewbox_height', None) or
                   surface.context_height or 0)
        if font_size is None:
            font_size = surface.font_size
        parsed_origin = parse_transform_origin(
            transform_origin, box, font_size)
        # An invalid value falls back to the initial ``0 0`` origin, i.e. the
        # top-left corner of the reference box, as browsers do. The transform
        # itself is still applied.
        origin = parsed_origin if parsed_origin is not None else box[:2]
        if not operations and parsed_origin is not None:
            # No transform to offset.
            return None

    matrix = cairo.Matrix()
    if origin is not None:
        matrix.translate(*origin)

    for transformation_type, values in operations:
        if transformation_type == 'matrix':
            matrix = cairo.Matrix(*values).multiply(matrix)
        elif transformation_type == 'rotate':
            angle = radians(values[0])
            x, y = (values[1:] + [0, 0])[:2]
            matrix.translate(x, y)
            matrix.rotate(angle)
            matrix.translate(-x, -y)
        elif transformation_type == 'skewX':
            tangent = tan(radians(values[0]))
            matrix = cairo.Matrix(1, 0, tangent, 1, 0, 0).multiply(matrix)
        elif transformation_type == 'skewY':
            tangent = tan(radians(values[0]))
            matrix = cairo.Matrix(1, tangent, 0, 1, 0, 0).multiply(matrix)
        elif transformation_type == 'translate':
            if len(values) == 1:
                values = values + [0]
            matrix.translate(*values[:2])
        elif transformation_type == 'scale':
            if len(values) == 1:
                values = 2 * values
            matrix.scale(*values[:2])

    if origin is not None:
        matrix.translate(-origin[0], -origin[1])
    return matrix


def transform(surface, transform_string, gradient=None, transform_origin=None,
              box=None, font_size=None):
    """Transform ``surface`` or ``gradient`` if supplied using ``string``.

    See http://www.w3.org/TR/SVG/coords.html#TransformAttribute

    ``box`` is the reference box used by ``transform-origin``, in the
    element's user coordinates. When ``None``, the current viewport is used.
    ``font_size`` overrides the surface font size for ``em`` units.

    An invalid ``transform`` or ``transform-origin`` leaves the element
    untransformed and never leaks state into the next element.
    """
    matrix = transform_matrix(
        surface, transform_string, transform_origin, box, font_size)
    if matrix is None:
        return

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
