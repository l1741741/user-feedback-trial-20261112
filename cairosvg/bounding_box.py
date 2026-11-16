"""
Calculate bounding boxes for SVG shapes and paths.

A bounding box is a ``(minx, miny, width, height)`` tuple.

Two different notions must not be mixed up:

* The boxes computed here are *geometry* (fill) bounding boxes expressed in the
  local user-space coordinate system of the node. They only follow the path's
  geometry, never its style: stroke width, markers, line caps and joins do not
  extend them. They are the boxes used by the ``objectBoundingBox`` unit of
  gradients, patterns and clip paths, see
  https://www.w3.org/TR/SVG/coords.html#ObjectBoundingBox
* A *stroke* bounding box would additionally include the painted stroke,
  markers and line caps. Cairo computes that one itself when painting (for
  example through ``fill_extents`` / ``stroke_extents``); this module never
  tries to emulate it and never looks at paint attributes.

The computation is purely analytical and read-only: it parses the geometry
attributes of a node without creating any Cairo path, mutating the node (apart
from caching the result on it) or changing the current point used by later
drawing operations.

"""

from math import (
    atan2, cos, fmod, isinf, pi, radians, sin, sqrt, tau)

from .features import match_features
from .helpers import PATH_LETTERS, normalize, point, size
from .parser import Tree
from .url import parse_url

EMPTY_BOUNDING_BOX = float('inf'), float('inf'), 0, 0

# Numerical tolerance used when deciding that a polynomial coefficient or a
# coordinate difference is zero. Path coordinates are SVG user units (pixels),
# so boxes of documents drawn at ordinary scales are well above this scale.
_EPSILON = 1e-12


def calculate_bounding_box(surface, node):
    """Calculate ``node``'s geometry bounding box.

    See https://www.w3.org/TR/SVG/coords.html#ObjectBoundingBox

    The result is cached on the node. The cache stores geometry boxes only and
    is never invalidated by paint attributes (``stroke``, ``stroke-width``,
    markers...), as those have no influence on an ``objectBoundingBox``.
    """
    if 'bounding_box' not in node and node.tag in BOUNDING_BOX_METHODS:
        bounding_box = BOUNDING_BOX_METHODS[node.tag](surface, node)
        if is_non_empty_bounding_box(bounding_box):
            node['bounding_box'] = bounding_box
    return node.get('bounding_box')


def bounding_box_rect(surface, node):
    """Get the bounding box of a ``rect`` node."""
    x, y = size(surface, node.get('x'), 'x'), size(surface, node.get('y'), 'y')
    width = size(surface, node.get('width'), 'x')
    height = size(surface, node.get('height'), 'y')
    return x, y, width, height


def bounding_box_image(surface, node):
    """Get the bounding box of an ``image`` node.

    Only the placement rectangle is known without decoding the image; it is
    exactly the region the element occupies in user space.
    """
    x, y = size(surface, node.get('x'), 'x'), size(surface, node.get('y'), 'y')
    width = size(surface, node.get('width'), 'x')
    height = size(surface, node.get('height'), 'y')
    return x, y, width, height


def bounding_box_circle(surface, node):
    """Get the bounding box of a ``circle`` node."""
    cx = size(surface, node.get('cx'), 'x')
    cy = size(surface, node.get('cy'), 'y')
    r = size(surface, node.get('r'))
    return cx - r, cy - r, 2 * r, 2 * r


def bounding_box_ellipse(surface, node):
    """Get the bounding box of an ``ellipse`` node."""
    rx = size(surface, node.get('rx'), 'x')
    ry = size(surface, node.get('ry'), 'y')
    cx = size(surface, node.get('cx'), 'x')
    cy = size(surface, node.get('cy'), 'y')
    return cx - rx, cy - ry, 2 * rx, 2 * ry


def bounding_box_line(surface, node):
    """Get the geometry bounding box of a ``line`` node.

    The box only contains the two endpoints; stroke caps are deliberately
    ignored as they belong to the stroke bounding box.
    """
    x1, y1, x2, y2 = tuple(
        size(surface, node.get(position), position[0])
        for position in ('x1', 'y1', 'x2', 'y2'))
    x, y = min(x1, x2), min(y1, y2)
    width, height = max(x1, x2) - x, max(y1, y2) - y
    return x, y, width, height


def bounding_box_polyline(surface, node):
    """Get the bounding box of a ``polyline`` or ``polygon`` node."""
    bounding_box = EMPTY_BOUNDING_BOX
    points = []
    normalized_points = normalize(node.get('points', ''))
    while normalized_points:
        x, y, normalized_points = point(surface, normalized_points)
        points.append((x, y))
    if points:
        bounding_box = extend_bounding_box(bounding_box, points)
    return bounding_box


def bounding_box_path(surface, node):
    """Get the tight geometry bounding box of a ``path`` node.

    Every segment is handled analytically: line segments contribute their
    endpoints, quadratic and cubic Beziers contribute the roots of their
    derivative (their true extrema, not merely their control points), and
    elliptical arcs contribute their extreme points after the standard
    endpoint-to-center parameterization.

    Multiple subpaths, relative commands, smooth curves (``S/s/T/t``) and
    closed paths (``Z/z``) are tracked explicitly. The function never mutates
    the node nor any drawing state.
    """
    path_data = node.get('d', '')

    # Normalize path data for correct parsing
    for letter in PATH_LETTERS:
        path_data = path_data.replace(letter, f' {letter} ')
    path_data = normalize(path_data)

    bounding_box = EMPTY_BOUNDING_BOX
    previous_x = previous_y = 0.0
    # First point of the current subpath, used by Z/z.
    subpath_x = subpath_y = 0.0
    # Last explicit cubic (x2, y2) and quadratic (x1, y1) control points, in
    # absolute coordinates, used to resolve smooth S/s and T/t commands.
    previous_cubic_control = None
    previous_quad_control = None
    letter = 'M'    # Move as default
    while path_data:
        path_data = path_data.strip()
        if path_data.split(' ', 1)[0] in PATH_LETTERS:
            letter, path_data = (path_data + ' ').split(' ', 1)

        if letter in 'aA':
            # Elliptical arc curve
            rx, ry, path_data = point(None, path_data)
            rotation, path_data = path_data.split(' ', 1)
            rotation = radians(float(rotation))

            # The large and sweep values are not always separated from the
            # following values, here is the crazy parser
            large, path_data = path_data[0], path_data[1:].strip()
            while not large[-1].isdigit():
                large, path_data = large + path_data[0], path_data[1:].strip()
            sweep, path_data = path_data[0], path_data[1:].strip()
            while not sweep[-1].isdigit():
                sweep, path_data = sweep + path_data[0], path_data[1:].strip()

            large, sweep = bool(int(large)), bool(int(sweep))

            x, y, path_data = point(None, path_data)

            # Relative coordinate, convert to absolute
            if letter == 'a':
                x += previous_x
                y += previous_y

            # Tight box of the arc itself, including its extreme points
            arc_bounding_box = bounding_box_elliptical_arc(
                previous_x, previous_y, rx, ry, rotation, large, sweep, x, y)
            ax, ay, arc_width, arc_height = arc_bounding_box
            bounding_box = extend_bounding_box(
                bounding_box,
                ((ax, ay), (ax + arc_width, ay + arc_height)))
            previous_x, previous_y = x, y
            previous_cubic_control = previous_quad_control = None

        elif letter in 'cC':
            # Cubic Bezier curve
            x1, y1, path_data = point(None, path_data)
            x2, y2, path_data = point(None, path_data)
            x, y, path_data = point(None, path_data)

            # Relative coordinates, convert to absolute
            if letter == 'c':
                x1 += previous_x
                y1 += previous_y
                x2 += previous_x
                y2 += previous_y
                x += previous_x
                y += previous_y

            # Extend with the true extrema of the curve, not only its control
            # polygon, then record the last control point for S/s.
            minx, miny, maxx, maxy = cubic_bezier_bounding_box(
                (previous_x, previous_y), (x1, y1), (x2, y2), (x, y))
            bounding_box = extend_bounding_box(
                bounding_box, ((minx, miny), (maxx, maxy)))
            previous_cubic_control = (x2, y2)
            previous_quad_control = None
            previous_x, previous_y = x, y

        elif letter in 'hH':
            # Horizontal line
            x, path_data = (path_data + ' ').split(' ', 1)
            x = size(surface, x, 'x')

            # Relative coordinate, convert to absolute
            if letter == 'h':
                x += previous_x

            bounding_box = extend_bounding_box(
                bounding_box, ((x, previous_y),))
            previous_x = x
            previous_cubic_control = previous_quad_control = None

        elif letter in 'lL':
            # Line
            x, y, path_data = point(None, path_data)

            # Relative coordinate, convert to absolute
            if letter == 'l':
                x += previous_x
                y += previous_y

            bounding_box = extend_bounding_box(bounding_box, ((x, y),))
            previous_x, previous_y = x, y
            previous_cubic_control = previous_quad_control = None

        elif letter in 'mM':
            # Move, possibly starting a new subpath
            x, y, path_data = point(None, path_data)

            # Relative coordinate, convert to absolute
            if letter == 'm':
                x += previous_x
                y += previous_y

            bounding_box = extend_bounding_box(bounding_box, ((x, y),))
            previous_x, previous_y = x, y
            subpath_x, subpath_y = x, y
            previous_cubic_control = previous_quad_control = None

        elif letter in 'tT':
            # Smooth quadratic Bezier curve: the first control point is the
            # reflection of the previous quadratic control point about the
            # current point (or the current point itself after a non-quadratic
            # command). The old code ignored it entirely and used only the
            # endpoint, clipping the box on curved paths.
            x, y, path_data = point(None, path_data)
            if letter == 't':
                x += previous_x
                y += previous_y
            if previous_quad_control is not None:
                x1 = 2 * previous_x - previous_quad_control[0]
                y1 = 2 * previous_y - previous_quad_control[1]
            else:
                x1, y1 = previous_x, previous_y
            minx, miny, maxx, maxy = quadratic_bezier_bounding_box(
                (previous_x, previous_y), (x1, y1), (x, y))
            bounding_box = extend_bounding_box(
                bounding_box, ((minx, miny), (maxx, maxy)))
            previous_quad_control = (x1, y1)
            previous_cubic_control = None
            previous_x, previous_y = x, y

        elif letter in 'qQ':
            # Quadratic Bezier curve
            x1, y1, path_data = point(None, path_data)
            x, y, path_data = point(None, path_data)

            # Relative coordinates, convert to absolute
            if letter == 'q':
                x1 += previous_x
                y1 += previous_y
                x += previous_x
                y += previous_y

            # Extend with the true extrema of the curve
            minx, miny, maxx, maxy = quadratic_bezier_bounding_box(
                (previous_x, previous_y), (x1, y1), (x, y))
            bounding_box = extend_bounding_box(
                bounding_box, ((minx, miny), (maxx, maxy)))
            previous_quad_control = (x1, y1)
            previous_cubic_control = None
            previous_x, previous_y = x, y

        elif letter in 'sS':
            # Smooth cubic Bezier curve: the first control point is the
            # reflection of the previous cubic control point about the current
            # point (or the current point itself after a non-cubic command).
            x2, y2, path_data = point(None, path_data)
            x, y, path_data = point(None, path_data)
            if letter == 's':
                x2 += previous_x
                y2 += previous_y
                x += previous_x
                y += previous_y
            if previous_cubic_control is not None:
                x1 = 2 * previous_x - previous_cubic_control[0]
                y1 = 2 * previous_y - previous_cubic_control[1]
            else:
                x1, y1 = previous_x, previous_y
            minx, miny, maxx, maxy = cubic_bezier_bounding_box(
                (previous_x, previous_y), (x1, y1), (x2, y2), (x, y))
            bounding_box = extend_bounding_box(
                bounding_box, ((minx, miny), (maxx, maxy)))
            previous_cubic_control = (x2, y2)
            previous_quad_control = None
            previous_x, previous_y = x, y

        elif letter in 'vV':
            # Vertical line
            y, path_data = (path_data + ' ').split(' ', 1)
            y = size(surface, y, 'y')

            # Relative coordinate, convert to absolute
            if letter == 'v':
                y += previous_y

            bounding_box = extend_bounding_box(
                bounding_box, ((previous_x, y),))
            previous_y = y
            previous_cubic_control = previous_quad_control = None

        elif letter in 'zZ':
            # Close the current subpath: the current point returns to the
            # subpath start. The closing segment is a straight line whose
            # endpoints are already part of the accumulated box.
            previous_x, previous_y = subpath_x, subpath_y
            previous_cubic_control = previous_quad_control = None

        path_data = path_data.strip()

    return bounding_box


def bounding_box_text(surface, node):
    """Get the bounding box of a ``text`` node.

    Text outlines depend on the loaded fonts and are only known while the text
    is being drawn; :mod:`cairosvg.text` stores the font-metrics based box on
    the node. There is no analytical geometry available before rendering, so
    an unrendered text node reports no box.
    """
    return node.get('text_bounding_box')


def _derivative_roots(a, b, c):
    """Roots of ``a*t² + b*t + c = 0`` that lie strictly inside ``(0, 1)``."""
    if abs(a) < _EPSILON:
        if abs(b) >= _EPSILON:
            root = -c / b
            if 0 < root < 1:
                return (root,)
        return ()
    discriminant = b * b - 4 * a * c
    if discriminant < 0:
        return ()
    if discriminant == 0:
        root = -b / (2 * a)
        return (root,) if 0 < root < 1 else ()
    square_root = sqrt(discriminant)
    return tuple(
        root for root in (
            (-b - square_root) / (2 * a),
            (-b + square_root) / (2 * a))
        if 0 < root < 1)


def cubic_bezier_bounding_box(p0, p1, p2, p3):
    """Return the tight ``(minx, miny, maxx, maxy)`` of a cubic Bezier.

    The curve is ``B(t) = (1-t)³p0 + 3(1-t)²t·p1 + 3(1-t)t²·p2 + t³p3`` with
    ``t`` in ``[0, 1]``. Each coordinate is a cubic polynomial whose extrema
    are the endpoints and the roots of its quadratic derivative.
    """
    minx = miny = float('inf')
    maxx = maxy = float('-inf')
    for coordinate in (0, 1):
        p0c, p1c, p2c, p3c = (
            point[coordinate] for point in (p0, p1, p2, p3))
        # Coefficients of the derivative B'(t) = 3(a·t² + b·t + c)
        a = -p0c + 3 * p1c - 3 * p2c + p3c
        b = 2 * (p0c - 2 * p1c + p2c)
        c = p1c - p0c
        values = [p0c, p3c]
        for root in _derivative_roots(a, b, c):
            one_minus_t = 1 - root
            values.append(
                one_minus_t ** 3 * p0c
                + 3 * one_minus_t ** 2 * root * p1c
                + 3 * one_minus_t * root ** 2 * p2c
                + root ** 3 * p3c)
        if coordinate == 0:
            minx, maxx = min(values), max(values)
        else:
            miny, maxy = min(values), max(values)
    return minx, miny, maxx, maxy


def quadratic_bezier_bounding_box(p0, p1, p2):
    """Return the tight ``(minx, miny, maxx, maxy)`` of a quadratic Bezier.

    The curve is ``B(t) = (1-t)²p0 + 2(1-t)t·p1 + t²p2``. Each coordinate is a
    quadratic polynomial; its derivative is linear and has at most one root in
    ``(0, 1)``.
    """
    minx = miny = float('inf')
    maxx = maxy = float('-inf')
    for coordinate in (0, 1):
        p0c, p1c, p2c = p0[coordinate], p1[coordinate], p2[coordinate]
        a = p0c - 2 * p1c + p2c
        b = 2 * (p1c - p0c)
        values = [p0c, p2c]
        if abs(a) >= _EPSILON:
            root = -b / (2 * a)
            if 0 < root < 1:
                one_minus_t = 1 - root
                values.append(
                    one_minus_t ** 2 * p0c
                    + 2 * one_minus_t * root * p1c
                    + root ** 2 * p2c)
        if coordinate == 0:
            minx, maxx = min(values), max(values)
        else:
            miny, maxy = min(values), max(values)
    return minx, miny, maxx, maxy


def angle(bx, by):
    """Get the angle between vector (1,0) and vector (bx,by), in [0, 2π)."""
    return fmod(tau + atan2(by, bx), tau)


def bounding_box_elliptical_arc(x1, y1, rx, ry, phi, large, sweep, x, y):
    """Get the tight bounding box of an elliptical arc.

    The SVG endpoint parameters are first converted to the center
    parameterization (SVG 1.1 appendix F.6); the box then follows from the
    points of the arc where the tangent is horizontal or vertical, plus the
    two endpoints.

    Degenerate cases have a defined, stable behavior:

    * a zero radius or identical endpoints draw a straight line (or nothing),
      so the box is the endpoints' box,
    * radii too small for the chord are scaled up exactly as mandated by the
      specification,
    * negative radii are treated as their absolute value.
    """
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0 or (x1 == x and y1 == y):
        return min(x, x1), min(y, y1), abs(x - x1), abs(y - y1)

    center = _arc_center_parameterization(
        x1, y1, rx, ry, phi, large, sweep, x, y)
    cx, cy, rx, ry, theta1, delta_theta = center
    cphi, sphi = cos(phi), sin(phi)

    def arc_point(theta):
        ellipse_x = rx * cos(theta)
        ellipse_y = ry * sin(theta)
        return (
            cx + cphi * ellipse_x - sphi * ellipse_y,
            cy + sphi * ellipse_x + cphi * ellipse_y)

    # Angles at which the rotated ellipse has a vertical (x extremum) or
    # horizontal (y extremum) tangent. Each equation has two solutions pi
    # apart; examine a few periods around the arc to catch every candidate.
    extremal_angles = (
        atan2(-ry * sphi, rx * cphi),
        atan2(ry * cphi, rx * sphi))
    interval_start = min(theta1, theta1 + delta_theta)
    interval_end = max(theta1, theta1 + delta_theta)
    points = [(x1, y1), (x, y)]
    for base_angle in extremal_angles:
        for period in range(-2, 3):
            theta = base_angle + period * pi
            if interval_start - _EPSILON <= theta <= interval_end + _EPSILON:
                points.append(arc_point(theta))

    x_values = [point[0] for point in points]
    y_values = [point[1] for point in points]
    minx, maxx = min(x_values), max(x_values)
    miny, maxy = min(y_values), max(y_values)
    return minx, miny, maxx - minx, maxy - miny


def _arc_center_parameterization(x1, y1, rx, ry, phi, large, sweep, x2, y2):
    """Convert SVG arc endpoint parameters to center parameters.

    Returns ``(cx, cy, rx, ry, theta1, delta_theta)`` where ``delta_theta``
    follows the sweep flag and is in ``(-2π, 2π)``. Mirrors SVG 1.1
    appendix F.6, including the radius correction of step F.6.6.
    """
    cphi, sphi = cos(phi), sin(phi)
    half_dx = (x1 - x2) / 2
    half_dy = (y1 - y2) / 2
    x1prime = cphi * half_dx + sphi * half_dy
    y1prime = -sphi * half_dx + cphi * half_dy

    # Correct radii too small to reach the endpoints (F.6.6)
    lambda_factor = (
        x1prime ** 2 / rx ** 2 + y1prime ** 2 / ry ** 2)
    if lambda_factor > 1:
        scale = sqrt(lambda_factor)
        rx *= scale
        ry *= scale

    numerator = (
        rx ** 2 * ry ** 2
        - rx ** 2 * y1prime ** 2 - ry ** 2 * x1prime ** 2)
    denominator = (
        rx ** 2 * y1prime ** 2 + ry ** 2 * x1prime ** 2)
    factor = sqrt(max(0, numerator / denominator))
    if large == sweep:
        factor = -factor

    cxprime = factor * rx * y1prime / ry
    cyprime = -factor * ry * x1prime / rx
    cx = cphi * cxprime - sphi * cyprime + (x1 + x2) / 2
    cy = sphi * cxprime + cphi * cyprime + (y1 + y2) / 2

    theta1 = angle(
        (x1prime - cxprime) / rx, (y1prime - cyprime) / ry)
    delta_theta = angle(
        (-x1prime - cxprime) / rx, (-y1prime - cyprime) / ry) - theta1
    if not sweep and delta_theta > 0:
        delta_theta -= tau
    elif sweep and delta_theta < 0:
        delta_theta += tau
    return cx, cy, rx, ry, theta1, delta_theta


def bounding_box_group(surface, node):
    """Get the bounding box of a ``g`` node."""
    bounding_box = EMPTY_BOUNDING_BOX
    for child in node.children:
        bounding_box = combine_bounding_box(
            bounding_box, calculate_bounding_box(surface, child))
    return bounding_box


def bounding_box_use(surface, node):
    """Get the bounding box of a ``use`` node."""
    href = parse_url(node.get_href()).geturl()
    tree = Tree(
        url=href, url_fetcher=node.url_fetcher, parent=node,
        unsafe=node.unsafe)
    if not match_features(tree.xml_tree):
        return None
    return calculate_bounding_box(surface, tree)


def extend_bounding_box(bounding_box, points):
    """Extend the ``bounding_box`` by the points."""
    minx, miny, width, height = bounding_box
    maxx, maxy = (
        float('-inf') if isinf(minx) else minx + width,
        float('-inf') if isinf(miny) else miny + height)
    x_list, y_list = zip(*points)
    minx, miny, maxx, maxy = (
        min(minx, *x_list), min(miny, *y_list),
        max(maxx, *x_list), max(maxy, *y_list))
    return minx, miny, maxx - minx, maxy - miny


def combine_bounding_box(bounding_box, another_bounding_box):
    """Combine the ``bounding_box`` with ``another_bounding_box``."""
    if is_valid_bounding_box(another_bounding_box):
        minx, miny, width, height = another_bounding_box
        maxx, maxy = minx + width, miny + height
        bounding_box = extend_bounding_box(
            bounding_box, ((minx, miny), (maxx, maxy)))
    return bounding_box


def is_valid_bounding_box(bounding_box):
    """Know whether bounding box has been initialized."""
    # If 'minx' or 'miny' is set, 'maxx' and 'maxy' will also be set (resulting
    # in a valid bounding box)
    return bounding_box and not isinf(bounding_box[0] + bounding_box[1])


def is_non_empty_bounding_box(bounding_box):
    """Know whether bounding box is valid and has a size.

    A zero-width or zero-height geometry (a horizontal line, a point...) is a
    valid box but cannot define an ``objectBoundingBox`` coordinate system, so
    gradients and patterns reject it; see the callers in :mod:`cairosvg.defs`.
    """
    return is_valid_bounding_box(bounding_box) and 0 not in bounding_box[2:]


BOUNDING_BOX_METHODS = {
    'rect': bounding_box_rect,
    'image': bounding_box_image,
    'circle': bounding_box_circle,
    'ellipse': bounding_box_ellipse,
    'line': bounding_box_line,
    'polyline': bounding_box_polyline,
    'polygon': bounding_box_polyline,
    'path': bounding_box_path,
    'text': bounding_box_text,
    'tspan': bounding_box_text,
    'textPath': bounding_box_text,
    'g': bounding_box_group,
    'use': bounding_box_use,
    'marker': bounding_box_group,
}
