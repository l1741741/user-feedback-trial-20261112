"""
Paths manager.

"""

from collections import deque
from math import atan2, copysign, hypot, pi, radians, sin, cos
from re import finditer

from .bounding_box import calculate_bounding_box
from .helpers import (
    PATH_LETTERS, clip_marker_box, node_format, normalize, point, point_angle,
    preserve_ratio, quadratic_points, rotate, size)
from .url import parse_url

# Sentinel appended to ``node.vertices`` by the ``z``/``Z`` close-path command
# and by ``polygon`` elements. Unlike the ``None`` used between two distinct
# subpaths, it marks the closing vertex of a *closed* subpath: the marker drawn
# there behaves like a mid marker joining the last and the first segments.
CLOSE_PATH = object()


def quadratic_angles(x1, y1, x2, y2, x3, y3):
    """Return the tangent directions of a quadratic Bézier curve.

    The two returned angles are respectively the direction of the tangent at
    the start point and at the end point of the curve, in radians in the
    user-space coordinate system (y axis pointing down). The tangent of a
    quadratic Bézier at its endpoints is aligned with the line joining the
    endpoint to its control point.

    """
    return (
        point_angle(x1, y1, x2, y2),
        point_angle(x2, y2, x3, y3),
    )


def cubic_angles(x0, y0, x1, y1, x2, y2, x3, y3):
    """Return the tangent directions of a cubic Bézier curve.

    The tangent at the endpoints of a cubic Bézier is aligned with the line
    joining the endpoint to its neighboring control point. Degenerate
    tangents (endpoint and control point at the same location) fall back on
    the next available derivative direction.

    """
    start = point_angle(x0, y0, x1, y1)
    if (x0, y0) == (x1, y1):
        # First derivative is zero: use the second derivative direction.
        start = point_angle(x1, y1, x2, y2)
        if (x1, y1) == (x2, y2):
            start = point_angle(x2, y2, x3, y3)
    end = point_angle(x2, y2, x3, y3)
    if (x2, y2) == (x3, y3):
        end = point_angle(x1, y1, x2, y2)
        if (x1, y1) == (x2, y2):
            end = point_angle(x0, y0, x1, y1)
    return start, end


def marker_mid_angle(incoming, outgoing):
    """Return the orientation of a mid marker between two segments.

    ``incoming`` is the tangent direction of the segment ending at the
    vertex, ``outgoing`` the tangent direction of the segment starting at the
    vertex, both in radians.

    The marker points along the bisector of the angle between the incoming and
    outgoing directions, as required by the SVG marker algorithm. When the two
    segments are collinear the bisector is simply the common direction. When
    they are exactly opposite the bisector is not defined: the incoming
    tangent is then used, matching the SVG 2 marker algorithm.

    """
    sum_y = sin(incoming) + sin(outgoing)
    sum_x = cos(incoming) + cos(outgoing)
    if hypot(sum_x, sum_y) < 1e-9:
        return incoming
    return atan2(sum_y, sum_x)


def _parse_marker_vertices(vertices):
    """Split raw path vertices into structured marker placements.

    The geometry builders store, for every subpath, an alternating sequence of
    points ``(x, y)`` and segment angle pairs ``(start_angle, end_angle)``:
    ``point, segment, point, segment, ..., point``. For every segment type
    (line, arc, cubic or quadratic Bézier) the two values are the tangent
    direction at the segment start and at its end, expressed as outgoing
    directions in radians; a straight line stores the same angle twice.
    A ``None`` entry separates two independent (open) subpaths while the
    :data:`CLOSE_PATH` sentinel closes the current subpath.

    Points and angle pairs are both float pairs, so they are told apart by
    their position in the alternating stream rather than by their values.

    Zero-length segments (including runs of consecutive ones) do not define a
    direction: they are skipped so that the surrounding non-degenerate
    segments provide the marker orientation, instead of propagating the
    undefined tangent of an empty segment.

    Return a list of subpaths, each one being a tuple ``(closed, placements)``
    where ``placements`` is a list of ``(point, angle, position)`` tuples with
    position in ``('start', 'mid', 'end')``.

    """
    subpaths = []
    points = []
    segments = []
    closed = False

    def flush():
        if not points:
            return
        # Open subpath: ``segments[i]`` goes from ``points[i]`` to
        # ``points[i + 1]`` (one fewer segment than points). Closed subpath:
        # there is one additional segment closing ``points[-1]`` back onto
        # ``points[0]``. A ``None`` entry marks a zero-length segment.
        valid = [i for i, segment in enumerate(segments) if segment is not None]
        placements = []
        if valid:
            if closed:
                # Every vertex of a closed subpath joins two segments and
                # therefore gets a mid marker only: the closing vertex is not
                # also a start/end marker position.
                for i in range(len(points)):
                    previous = segments[i - 1]
                    following = segments[i]
                    if previous is not None and following is not None:
                        angle = marker_mid_angle(
                            previous[1], following[0])
                    elif following is not None:
                        angle = following[0]
                    else:
                        angle = previous[1]
                    placements.append((points[i], angle, 'mid'))
            else:
                first, last = valid[0], valid[-1]
                # Start marker: outgoing tangent of the first non-degenerate
                # segment.
                placements.append(
                    (points[0], segments[first][0], 'start'))
                # Mid markers at every interior vertex, using the nearest
                # defined tangent on each side of the vertex.
                for i in range(1, len(points) - 1):
                    previous = next(
                        (j for j in reversed(valid) if j < i), None)
                    following = next(
                        (j for j in valid if j >= i), None)
                    if previous is None or following is None:
                        # Interior vertex only reachable through zero-length
                        # segments at one end: no defined direction.
                        continue
                    placements.append((
                        points[i],
                        marker_mid_angle(
                            segments[previous][1], segments[following][0]),
                        'mid'))
                # End marker: outgoing tangent of the last non-degenerate
                # segment.
                placements.append(
                    (points[-1], segments[last][1], 'end'))
        else:
            # A subpath made only of zero-length segments has no defined
            # direction. A closed one collapses onto a single vertex, which
            # is still a mid position; an open one keeps both endpoints.
            if closed:
                placements.append((points[0], 0, 'mid'))
            else:
                placements.append((points[0], 0, 'start'))
                if len(points) > 1:
                    placements.append((points[-1], 0, 'end'))
        subpaths.append((closed, placements))

    pending_segment = None
    expect_point = True
    for item in vertices:
        if item is None:
            flush()
            points, segments, closed = [], [], False
            pending_segment = None
            expect_point = True
        elif item is CLOSE_PATH:
            # The pending angle pair is the implicit closing segment.
            if pending_segment is not None:
                start = points[-1]
                if points and start == points[0]:
                    segments.append(None)
                else:
                    segments.append(pending_segment)
                pending_segment = None
            closed = True
            flush()
            points, segments, closed = [], [], False
            expect_point = True
        elif expect_point:
            # Coordinate point ending the pending segment (if any).
            if pending_segment is not None:
                start = points[-1] if points else None
                if start is not None and item == start:
                    segments.append(None)
                else:
                    segments.append(pending_segment)
                pending_segment = None
            points.append(item)
            expect_point = False
        else:
            # Angle pair belonging to the segment ending at the next point.
            pending_segment = item
            expect_point = True

    flush()
    return subpaths


def draw_markers(surface, node):
    """Draw the markers attached to a path ``node``."""
    if not getattr(node, 'vertices', None):
        return

    markers = {}
    common_marker = parse_url(node.get('marker', '')).fragment
    for position in ('start', 'mid', 'end'):
        attribute = f'marker-{position}'
        if attribute in node:
            markers[position] = parse_url(node[attribute]).fragment
        else:
            markers[position] = common_marker

    if not any(markers.values()):
        return

    stroke_width = size(surface, node.get('stroke-width', '1'))

    for closed, placements in _parse_marker_vertices(list(node.vertices)):
        for point, angle, position in placements:
            marker = markers[position]
            if not marker:
                continue
            marker_node = surface.markers.get(marker)
            if marker_node is None:
                continue

            _draw_marker(
                surface, node, marker_node, point, angle, position,
                stroke_width)


def _marker_geometry(surface, node, marker_node, stroke_width):
    """Return the local-to-user transform and clip box of a marker.

    The marker transformation follows the SVG 1.1 marker algorithm:

    1. an additional uniform scale equal to the host stroke width when
       ``markerUnits`` is the default ``strokeWidth`` (``userSpaceOnUse``
       markers are not scaled);
    2. the marker viewport scaling, derived from ``markerWidth`` /
       ``markerHeight`` and the ``viewBox`` (with ``preserveAspectRatio``);
    3. a translation placing ``(refX, refY)`` at the reference point.

    Every value is computed from the marker node and the *current* host node,
    so the same marker referenced by paths with different stroke widths is
    rendered independently and never shares mutated state.

    """
    if marker_node.get('markerUnits') == 'userSpaceOnUse':
        units_scale = 1
    else:
        units_scale = stroke_width

    viewbox = node_format(surface, marker_node)[2]
    if viewbox:
        scale_x, scale_y, translate_x, translate_y = preserve_ratio(
            surface, marker_node)
        clip_box = clip_marker_box(surface, marker_node, scale_x, scale_y)
    else:
        # Without a viewBox the marker content is expressed in the marker's
        # own user units: only the markerUnits scaling applies, and the
        # reference point is translated to the origin.
        marker_width = size(surface,
                            marker_node.get('markerWidth', '3'), 'x')
        marker_height = size(surface,
                             marker_node.get('markerHeight', '3'), 'y')
        bounding_box = calculate_bounding_box(surface, marker_node)

        translate_x = -size(surface, marker_node.get('refX', '0'), 'x')
        translate_y = -size(surface, marker_node.get('refY', '0'), 'y')
        if bounding_box[2] and bounding_box[3]:
            scale_x = scale_y = min(
                marker_width / bounding_box[2],
                marker_height / bounding_box[3])
        else:
            scale_x = scale_y = 1
        clip_box = None

    return units_scale, scale_x, scale_y, translate_x, translate_y, clip_box


def _draw_marker(surface, node, marker_node, point, angle, position,
                 stroke_width):
    """Draw one marker instance at ``point`` with the given orientation.

    ``angle`` is the true user-space direction in which the marker should
    point, in radians with the y axis pointing down (0 = east, π/2 = south).
    A marker geometry pointing toward the local +x axis renders with its tip
    on the reference point, so a zero rotation points west: turning a travel
    direction into the matching Cairo rotation therefore requires
    ``pi - angle``. This single conversion lets every geometry builder keep
    storing unmodified travel directions regardless of the segment type.

    """
    (units_scale, scale_x, scale_y, translate_x, translate_y,
     clip_box) = _marker_geometry(surface, node, marker_node, stroke_width)

    # An explicit numeric orient keeps its fixed angle; "auto" follows the
    # path direction everywhere, and "auto-start-reverse" additionally flips
    # the start marker. Mid and end markers behave like "auto" in that case.
    node_angle = marker_node.get('orient', '0')
    if node_angle not in ('auto', 'auto-start-reverse'):
        # Numeric orient values follow the SVG/Cairo clockwise convention.
        rotation = radians(float(node_angle))
    else:
        if node_angle == 'auto-start-reverse' and position == 'start':
            angle += pi
        rotation = pi - angle

    # The path being drawn must not be consumed by the marker content.
    temp_path = surface.context.copy_path()
    surface.context.new_path()

    try:
        for child in marker_node.children:
            surface.context.save()
            surface.context.translate(*point)
            surface.context.rotate(rotation)
            surface.context.scale(units_scale, units_scale)
            surface.context.scale(scale_x, scale_y)
            surface.context.translate(translate_x, translate_y)

            # Add clipping (if present and requested)
            overflow = marker_node.get('overflow', 'hidden')
            if clip_box and overflow in ('hidden', 'scroll'):
                surface.context.save()
                surface.context.rectangle(*clip_box)
                surface.context.clip()
                surface.context.restore()

            surface.draw(child)
            surface.context.restore()
    finally:
        surface.context.append_path(temp_path)


def path(surface, node):
    """Draw a path ``node``."""
    string = node.get('d', '')

    node.vertices = deque()

    for letter in PATH_LETTERS:
        string = string.replace(letter, f' {letter} ')

    last_letter = None
    string = normalize(string)

    # Keep the current point because Cairo's get_current_point is not accurate
    # enough. See https://github.com/Kozea/CairoSVG/issues/111.
    if surface.context.has_current_point():
        current_point = surface.context.get_current_point()
    else:
        surface.context.move_to(0, 0)
        current_point = 0, 0

    matches = deque(match.groups() for match in finditer(
        f'([{PATH_LETTERS}])([^{PATH_LETTERS}]*)', string.strip()))
    while matches:
        letter, string = matches.popleft()
        string = string.strip()

        if last_letter in (None, 'z', 'Z') and letter not in 'mM':
            node.vertices.append(current_point)
            first_path_point = current_point

        if last_letter in (None, 'm', 'M', 'z', 'Z'):
            first_path_point = None
        if letter not in (None, 'm', 'M', 'z', 'Z') and (
                first_path_point is None):
            first_path_point = current_point

        if letter in 'aA':
            # Elliptic curve
            surface.context.set_tolerance(0.00001)
            x1, y1 = current_point
            rx, ry, string = point(surface, string)
            rotation, string = string.split(' ', 1)
            rotation = radians(float(rotation))

            # The large and sweep values are not always separated from the
            # following values. These flags can only be 0 or 1, so reading a
            # single digit suffices.
            large, string = string[0], string[1:].strip()
            sweep, string = string[0], string[1:].strip()

            # Retrieve end point and set remainder (before checking flags)
            x3, y3, string = point(surface, string)

            # Only allow 0 or 1 for flags
            large, sweep = int(large), int(sweep)
            if large not in (0, 1) or sweep not in (0, 1):
                continue
            large, sweep = bool(large), bool(sweep)

            if letter == 'A':
                # Absolute x3 and y3, convert to relative
                x3 -= x1
                y3 -= y1

            # rx=0 or ry=0 means straight line
            if not rx or not ry:
                if string := string.strip():
                    # As we replace the current operation by l, we must be sure
                    # that the next letter is set to the real current letter
                    # (a or A) in case it’s omitted
                    matches.appendleft((letter, string))
                matches.appendleft(('l', f'{x3} {y3}'))
                continue

            radii_ratio = ry / rx

            # Cancel the rotation of the second point
            xe, ye = rotate(x3, y3, -rotation)
            ye /= radii_ratio

            # Find the angle between the second point and the x axis
            angle = point_angle(0, 0, xe, ye)

            # Put the second point onto the x axis
            xe = hypot(xe, ye)
            ye = 0

            # Update the x radius if it is too small
            rx = max(rx, xe / 2)

            # Find one circle centre
            xc = xe / 2
            yc = (rx ** 2 - xc ** 2) ** .5

            # Choose between the two circles according to flags
            if not (large ^ sweep):
                yc = -yc

            # Define the arc sweep
            arc = (
                surface.context.arc if sweep else surface.context.arc_negative)

            # Put the second point and the center back to their positions
            xe, ye = rotate(xe, 0, angle)
            xc, yc = rotate(xc, yc, angle)

            # Find the drawing angles
            angle1 = point_angle(xc, yc, 0, 0)
            angle2 = point_angle(xc, yc, xe, ye)

            # Store the tangent angles. ``angle1`` / ``angle2`` locate the
            # endpoints relative to the arc center (the radial direction);
            # the travel tangent is perpendicular to that radius. A forward
            # arc (``arc``) turns by +pi/2 from the radial direction, while
            # a reverse arc (``arc_negative``) turns by -pi/2.
            tangent_sign = 1 if sweep else -1
            tangent1 = angle1 + tangent_sign * pi / 2
            tangent2 = angle2 + tangent_sign * pi / 2
            node.vertices.append((tangent1, tangent2))

            # Draw the arc
            surface.context.save()
            surface.context.translate(x1, y1)
            surface.context.rotate(rotation)
            surface.context.scale(1, radii_ratio)
            arc(xc, yc, rx, angle1, angle2)
            surface.context.restore()
            current_point = current_point[0] + x3, current_point[1] + y3

        elif letter == 'c':
            # Relative curve
            x, y = current_point
            x1, y1, string = point(surface, string)
            x2, y2, string = point(surface, string)
            x3, y3, string = point(surface, string)
            node.vertices.append(cubic_angles(
                0, 0, x1, y1, x2, y2, x3, y3))
            surface.context.rel_curve_to(x1, y1, x2, y2, x3, y3)
            current_point = current_point[0] + x3, current_point[1] + y3

            # Save absolute values for x and y, useful if next letter is s or S
            x1 += x
            x2 += x
            x3 += x
            y1 += y
            y2 += y
            y3 += y

        elif letter == 'C':
            # Curve
            x1, y1, string = point(surface, string)
            x2, y2, string = point(surface, string)
            x3, y3, string = point(surface, string)
            node.vertices.append(cubic_angles(
                *current_point, x1, y1, x2, y2, x3, y3))
            surface.context.curve_to(x1, y1, x2, y2, x3, y3)
            current_point = x3, y3

        elif letter == 'h':
            # Relative horizontal line
            x, string = (string + ' ').split(' ', 1)
            old_x, old_y = current_point
            angle = 0 if size(surface, x, 'x') > 0 else pi
            node.vertices.append((angle, angle))
            x = size(surface, x, 'x')
            surface.context.rel_line_to(x, 0)
            current_point = current_point[0] + x, current_point[1]

        elif letter == 'H':
            # Horizontal line
            x, string = (string + ' ').split(' ', 1)
            old_x, old_y = current_point
            angle = 0 if size(surface, x, 'x') > old_x else pi
            node.vertices.append((angle, angle))
            x = size(surface, x, 'x')
            surface.context.line_to(x, old_y)
            current_point = x, current_point[1]

        elif letter == 'l':
            # Relative straight line
            x, y, string = point(surface, string)
            angle = point_angle(0, 0, x, y)
            node.vertices.append((angle, angle))
            surface.context.rel_line_to(x, y)
            current_point = current_point[0] + x, current_point[1] + y

        elif letter == 'L':
            # Straight line
            x, y, string = point(surface, string)
            old_x, old_y = current_point
            angle = point_angle(old_x, old_y, x, y)
            node.vertices.append((angle, angle))
            surface.context.line_to(x, y)
            current_point = x, y

        elif letter == 'm':
            # Current point relative move
            x, y, string = point(surface, string)
            if last_letter and last_letter not in 'zZ':
                node.vertices.append(None)
            surface.context.rel_move_to(x, y)
            current_point = current_point[0] + x, current_point[1] + y

        elif letter == 'M':
            # Current point move
            x, y, string = point(surface, string)
            if last_letter and last_letter not in 'zZ':
                node.vertices.append(None)
            surface.context.move_to(x, y)
            current_point = x, y

        elif letter == 'q':
            # Relative quadratic curve
            x, y = current_point
            x1, y1 = 0, 0
            x2, y2, string = point(surface, string)
            x3, y3, string = point(surface, string)
            xq1, yq1, xq2, yq2, xq3, yq3 = quadratic_points(
                x1, y1, x2, y2, x3, y3)
            surface.context.rel_curve_to(xq1, yq1, xq2, yq2, xq3, y3)
            node.vertices.append(quadratic_angles(0, 0, x2, y2, x3, y3))
            current_point = current_point[0] + x3, current_point[1] + y

        elif letter == 'Q':
            # Quadratic curve
            x1, y1 = current_point
            x2, y2, string = point(surface, string)
            x3, y3, string = point(surface, string)
            xq1, yq1, xq2, yq2, xq3, yq3 = quadratic_points(
                x1, y1, x2, y2, x3, y3)
            surface.context.curve_to(xq1, yq1, xq2, yq2, xq3, y3)
            node.vertices.append(
                quadratic_angles(x1, y1, x2, y2, x3, y3))
            current_point = x3, y3

        elif letter == 's':
            # Relative smooth curve
            x, y = current_point
            x1 = x3 - x2 if last_letter in 'csCS' else 0
            y1 = y3 - y2 if last_letter in 'csCS' else 0
            x2, y2, string = point(surface, string)
            x3, y3, string = point(surface, string)
            node.vertices.append(cubic_angles(
                0, 0, x1, y1, x2, y2, x3, y3))
            surface.context.rel_curve_to(x1, y1, x2, y2, x3, y3)
            current_point = current_point[0] + x3, current_point[1] + y3

            # Save absolute values for x and y, useful if next letter is s or S
            x1 += x
            x2 += x
            x3 += x
            y1 += y
            y2 += y
            y3 += y

        elif letter == 'S':
            # Smooth curve
            x, y = current_point
            x1 = x3 + (x3 - x2) if last_letter in 'csCS' else x
            y1 = y3 + (y3 - y2) if last_letter in 'csCS' else y
            x2, y2, string = point(surface, string)
            x3, y3, string = point(surface, string)
            node.vertices.append(cubic_angles(
                x, y, x1, y1, x2, y2, x3, y3))
            surface.context.curve_to(x1, y1, x2, y2, x3, y3)
            current_point = x3, y3

        elif letter == 't':
            # Relative quadratic curve end
            if last_letter not in 'QqTt':
                x2, y2, x3, y3 = 0, 0, 0, 0
            elif last_letter in 'QT':
                x2 -= x1
                y2 -= y1
                x3 -= x1
                y3 -= y1
            x2 = x3 - x2
            y2 = y3 - y2
            x1, y1 = 0, 0
            x3, y3, string = point(surface, string)
            xq1, yq1, xq2, yq2, xq3, yq3 = quadratic_points(
                x1, y1, x2, y2, x3, y3)
            node.vertices.append(quadratic_angles(0, 0, x2, y2, x3, y3))
            surface.context.rel_curve_to(xq1, yq1, xq2, yq2, xq3, yq3)
            current_point = current_point[0] + x3, current_point[1] + y

        elif letter == 'T':
            # Quadratic curve end
            abs_x, abs_y = current_point
            if last_letter not in 'QqTt':
                x2, y2, x3, y3 = abs_x, abs_y, abs_x, abs_y
            elif last_letter in 'qt':
                x2 += abs_x
                y2 += abs_y
                x3 += abs_x
                y3 += abs_y
            x2 = abs_x + (x3 - x2)
            y2 = abs_y + (y3 - y2)
            x1, y1 = abs_x, abs_y
            x3, y3, string = point(surface, string)
            xq1, yq1, xq2, yq2, xq3, yq3 = quadratic_points(
                x1, y1, x2, y2, x3, y3)
            node.vertices.append(
                quadratic_angles(x1, y1, x2, y2, x3, y3))
            surface.context.curve_to(xq1, yq1, xq2, yq2, xq3, yq3)
            current_point = x3, y3

        elif letter == 'v':
            # Relative vertical line
            y, string = (string + ' ').split(' ', 1)
            old_x, old_y = current_point
            angle = copysign(pi / 2, size(surface, y, 'y'))
            node.vertices.append((angle, angle))
            y = size(surface, y, 'y')
            surface.context.rel_line_to(0, y)
            current_point = current_point[0], current_point[1] + y

        elif letter == 'V':
            # Vertical line
            y, string = (string + ' ').split(' ', 1)
            old_x, old_y = current_point
            angle = copysign(pi / 2, size(surface, y, 'y') - old_y)
            node.vertices.append((angle, angle))
            y = size(surface, y, 'y')
            surface.context.line_to(old_x, y)
            current_point = current_point[0], y

        elif letter in 'zZ' and first_path_point:
            # End of path. The implicit closing segment joins the current
            # point back to the first point: store its tangent angles before
            # the close-path sentinel so that the closing vertex can be
            # treated like any other mid marker position.
            closing_angle = point_angle(*current_point, *first_path_point)
            node.vertices.append((closing_angle, closing_angle))
            node.vertices.append(CLOSE_PATH)
            surface.context.close_path()
            current_point = first_path_point

        if letter not in 'zZ':
            node.vertices.append(current_point)

        if string := string.strip():
            next_letter = {'m': 'l', 'M': 'L'}.get(letter, letter)
            matches.appendleft((next_letter, string))
        last_letter = letter
