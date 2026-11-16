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

# One ``<length-percentage>`` or ``<number>`` of a stroke dash array. The
# leading sign, a fractional part without integer digits (``.5``) and an
# exponent (``1e1``, ``1E-2``) are all valid, as are the unit suffixes.
DASH_VALUE = re.compile(
    r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(%|[a-zA-Z]+)?')

# The beginning of a number, used to tell an adjacent value (``1.2.3`` is
# ``1.2`` followed by ``.3``) from a syntax error.
DASH_START = re.compile(r'[+-]?(?:\d|\.)')


class PointError(Exception):
    """Exception raised when parsing a point fails."""


class InvalidDashValue(ValueError):
    """Exception raised when a dash value must be ignored and inherited."""


class DashArray:
    """Validated ``stroke-dasharray`` lengths in the current user space.

    ``values`` always has an even length: an odd ``<dasharray>`` is repeated
    once so that the list describes alternating dash/gap pairs. The
    ``specified_sum`` is the sum of the lengths exactly as authored, before
    that repetition; :meth:`phase` needs it to give negative offsets the same
    meaning as the reference renderers.

    """

    __slots__ = ('values', 'specified_sum')

    def __init__(self, values, specified_sum):
        self.values = values
        self.specified_sum = specified_sum

    @property
    def period(self):
        """Length of one full repetition of the painted pattern."""
        return sum(self.values)

    def phase(self, offset):
        """Return the non-negative distance into ``values`` to start at.

        ``offset`` is a ``stroke-dashoffset`` length. A non-negative offset is
        simply a position in the repeated pattern. A negative offset starts
        before the beginning of the list, and the reference renderers count
        that distance through the list as written (before an odd count is
        repeated) and then place the result in the repeated pattern. Keeping
        the two sums separate is what makes the phase reproducible instead of
        depending on a renderer's internal normalization.

        """
        period = self.period
        if period <= 0:
            return 0
        if offset >= 0:
            return offset % period
        return (self.specified_sum - (-offset % self.specified_sum)) % period


# Units accepted in a dash length, on top of the absolute ``UNITS``.
DASH_UNITS = frozenset(UNITS) | {'%', 'em', 'ex', 'ch'}


def _dash_length(surface, number, unit):
    """Convert a dash number and unit to a user-unit length, or ``None``."""
    if unit and unit not in DASH_UNITS:
        # Unknown units are invalid. ``size`` would quietly return 0.
        return None
    try:
        length = size(surface, f'{number}{unit}' if unit else number)
    except (ValueError, OverflowError):
        return None
    return length if isfinite(length) else None


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


def transform(surface, transform_string, gradient=None, transform_origin=None):
    """Transform ``surface`` or ``gradient`` if supplied using ``string``.

    See http://www.w3.org/TR/SVG/coords.html#TransformAttribute

    """
    if not transform_string:
        return

    transformations = re.findall(
        r'(\w+) ?\( ?(.*?) ?\)', normalize(transform_string))
    matrix = cairo.Matrix()

    if transform_origin:
        origin = transform_origin.split(' ')
        origin_x = origin[0]
        if len(origin) == 1:
            if origin_x in ('top', 'bottom'):
                origin_y = origin_x
                origin_x = surface.width / 2
            else:
                origin_y = surface.height / 2
        elif len(origin) > 1:
            if origin_x in ('top', 'bottom'):
                origin_y = origin_x
                origin_x = origin[1]
            else:
                origin_y = origin[1]
        else:
            return

        if origin_x == 'center':
            origin_x = surface.width / 2
        elif origin_x == 'left':
            origin_x = 0
        elif origin_x == 'right':
            origin_x = surface.width
        else:
            origin_x = size(surface, origin_x, 'x')

        if origin_y == 'center':
            origin_y = surface.height / 2
        elif origin_y == 'top':
            origin_y = 0
        elif origin_y == 'bottom':
            origin_y = surface.height
        else:
            origin_y = size(surface, origin_y, 'y')

        matrix.translate(float(origin_x), float(origin_y))

    for transformation_type, transformation in transformations:
        values = [size(surface, value) for value in transformation.split(' ')]
        if transformation_type == 'matrix':
            matrix = cairo.Matrix(*values).multiply(matrix)
        elif transformation_type == 'rotate':
            angle = radians(float(values.pop(0)))
            x, y = values or (0, 0)
            matrix.translate(x, y)
            matrix.rotate(angle)
            matrix.translate(-x, -y)
        elif transformation_type == 'skewX':
            tangent = tan(radians(float(values[0])))
            matrix = cairo.Matrix(1, 0, tangent, 1, 0, 0).multiply(matrix)
        elif transformation_type == 'skewY':
            tangent = tan(radians(float(values[0])))
            matrix = cairo.Matrix(1, tangent, 0, 1, 0, 0).multiply(matrix)
        elif transformation_type == 'translate':
            if len(values) == 1:
                values += (0,)
            matrix.translate(*values[:2])
        elif transformation_type == 'scale':
            if len(values) == 1:
                values = 2 * values
            matrix.scale(*values[:2])

    if transform_origin:
        matrix.translate(-float(origin_x), -float(origin_y))

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


def dash_array(surface, value):
    """Return the dash pattern for a ``stroke-dasharray`` value.

    The result is ``None`` when the property paints a solid stroke: an unset
    value, ``none``, or a list whose values are all zero. Any other list that
    is not a valid SVG <dasharray>, including an empty one, raises
    :class:`InvalidDashValue` so that the caller can apply the CSS "invalid
    at computed-value time" rule instead of silently turning the bad entry
    into a dash.

    Valid values follow SVG: a list of <length-percentage> values separated by
    either a comma with optional surrounding white space or by white space,
    all non-negative and at least one non-zero. An odd number of values is
    repeated once to give an even list, so the pattern is always made of
    alternating dash/gap pairs. Percentages are resolved against the
    normalized viewport diagonal, like other stroke lengths.

    """
    if value is None:
        return None
    source = value.strip()
    if not source:
        # An empty value is not the ``none`` keyword: it is invalid and
        # therefore ignored, so the inherited pattern applies.
        raise InvalidDashValue(value)
    if source.lower() == 'none':
        return None
    dashes = []
    position = 0
    length = len(source)
    while True:
        while position < length and source[position] in ' \t\r\n\f':
            position += 1
        match = DASH_VALUE.match(source, position)
        if match is None:
            raise InvalidDashValue(value)
        dash = _dash_length(surface, match.group(1), match.group(2))
        if dash is None or dash < 0:
            raise InvalidDashValue(value)
        dashes.append(dash)
        position = match.end()
        if position >= length:
            break
        spaced = False
        while position < length and source[position] in ' \t\r\n\f':
            position += 1
            spaced = True
        if position < length and source[position] == ',':
            position += 1
            while position < length and source[position] in ' \t\r\n\f':
                position += 1
            if position >= length:
                # A trailing comma is a syntax error.
                raise InvalidDashValue(value)
        elif position < length and not spaced:
            if match.group(2) or not DASH_START.match(source[position]):
                # Adjacent values are only allowed as numbers without units,
                # as in ``1.2.3`` meaning ``1.2`` then ``.3``.
                raise InvalidDashValue(value)
    if not dashes:
        raise InvalidDashValue(value)
    if not any(dashes):
        return None
    specified_sum = sum(dashes)
    if len(dashes) % 2:
        dashes *= 2
    return DashArray(dashes, specified_sum)


def dash_offset(surface, value):
    """Return the dash phase for a ``stroke-dashoffset`` value.

    ``None`` and empty values are zero. A value that cannot be parsed as a
    finite number raises :class:`InvalidDashValue`, so the caller can treat it
    as if the property were not specified. Negative offsets are valid and are
    resolved against the pattern by :meth:`DashArray.phase`.

    """
    if value is None:
        return None
    source = value.strip()
    if not source:
        return None
    match = DASH_VALUE.match(source)
    if match is None or match.end() != len(source):
        raise InvalidDashValue(value)
    offset = _dash_length(surface, match.group(1), match.group(2))
    if offset is None:
        raise InvalidDashValue(value)
    return offset
