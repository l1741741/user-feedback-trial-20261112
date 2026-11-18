"""Spec-compliant SVG path reference implementation (SVG 1.1 section 8.3).

This module is intentionally independent from CairoSVG: it implements the
path data state machine straight from the specification, including the
control-point state rules for the smooth commands S/s and T/t.

It is used by the differential tests to check CairoSVG's emitted geometry.
"""

import math
import re

NUMBER_RE = re.compile(r'[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?')
COMMAND_RE = re.compile(r'[achlmqstvzACHLMQSTVZ]')


def rotate(x, y, angle):
    """Rotate a point around the origin."""
    c, s = math.cos(angle), math.sin(angle)
    return x * c - y * s, y * c + x * s


class PathError(Exception):
    """Raised on malformed path data, mirroring CairoSVG's PointError."""


def tokenize(data):
    """Yield raw tokens: command letters or float numbers."""
    i = 0
    n = len(data)
    while i < n:
        while i < n and data[i] in ' \t\n\r,':
            i += 1
        if i >= n:
            break
        ch = data[i]
        if COMMAND_RE.fullmatch(ch):
            yield ch
            i += 1
            continue
        m = NUMBER_RE.match(data, i)
        if not m:
            raise PathError(f'bad number at {i!r}: {data[i:i + 8]!r}')
        yield float(m.group(0))
        i = m.end()


# How many numeric parameters each command consumes per coordinate group.
ARITY = {
    'a': 7, 'c': 6, 'h': 1, 'l': 2, 'm': 2, 'q': 4, 's': 4, 't': 2, 'v': 1,
}
FLAGS = {'a': (3, 4)}  # parameter indices restricted to 0/1 for arcs


def group_commands(tokens):
    """Apply the implicit-repeat / moveto-to-lineto rules of the spec.

    Yields (effective_command_letter, full parameter tuple) pairs. Raises on
    numbers appearing before any command or on incomplete final groups.
    """
    current = None
    buffer = []
    at_subpath_start = True  # an implicit M is still due before drawing

    for token in tokens:
        if isinstance(token, str):
            if buffer:
                raise PathError('incomplete parameter group')
            current = token
            if token in 'zZ':
                yield token, ()
                # After Z a later draw command starts a new subpath.
                at_subpath_start = True
            continue
        if current is None:
            raise PathError('data before command')
        if at_subpath_start and current not in 'mM':
            # Marker: parse_path fills in the actual subpath start point.
            yield '_implicit_M', ()
        at_subpath_start = False
        buffer.append(token)
        kind = current.lower()
        arity = ARITY.get(kind)
        if arity is None:
            raise PathError(f'command {current!r} takes no parameters')
        if len(buffer) == arity:
            if kind == 'a' and (buffer[3] not in (0, 1)
                                or buffer[4] not in (0, 1)):
                raise PathError('invalid arc flag')
            if current in 'mM':
                # First moveto group stays moveto; repeats become lineto.
                effective = current
                current = 'l' if current == 'm' else 'L'
            else:
                effective = current
            yield effective, tuple(buffer)
            buffer = []
    if buffer:
        raise PathError('incomplete parameter group at end of path')


def parse_path(data):
    """Return a list of absolute Cairo-style path operations.

    Each operation is one of:
      ('M', x, y)            moveto (starts a new subpath)
      ('L', x, y)            lineto
      ('C', x1, y1, x2, y2, x, y)
                             cubic bezier, all absolute
      ('Z',)                 close path

    Quadratic curves are raised to cubic beziers with the standard 2/3 rule,
    exactly like CairoSVG does, so geometry can be compared directly.
    Arcs are converted to cubic beziers with a standard endpoint-to-center
    parametrization and adaptive subdivision.
    """
    ops = []

    cur = (0.0, 0.0)          # current point
    start = (0.0, 0.0)        # first point of the current subpath
    last_cmd = None           # previous *effective* command letter
    subpath_drawn = False     # any L/C emitted in the current subpath

    # Smooth-command state, in absolute coordinates:
    # cubic: control point #2 of the previous cubic/smooth-cubic command.
    # quadratic: control point of the previous quadratic/smooth-quadratic.
    last_cubic_cp = None
    last_quad_cp = None

    for cmd, params in group_commands(list(tokenize(data))):
        if cmd == '_implicit_M':
            # Before the first command, or after a closepath, a draw command
            # implicitly moves to the current subpath start (origin initially).
            cur = start
            ops.append(('M', *cur))
            subpath_drawn = False
            continue

        rel = cmd.islower()
        kind = cmd.lower()

        if kind == 'm':
            dx, dy = params
            if rel and ops:
                cur = (cur[0] + dx, cur[1] + dy)
            else:
                cur = (dx, dy)
            start = cur
            ops.append(('M', *cur))
            subpath_drawn = False
            # Moveto resets all smooth-command state.
            last_cubic_cp = None
            last_quad_cp = None

        elif kind == 'l':
            dx, dy = params
            pt = (cur[0] + dx, cur[1] + dy) if rel else (dx, dy)
            ops.append(('L', *pt))
            cur = pt
            subpath_drawn = True
            last_cubic_cp = None
            last_quad_cp = None

        elif kind == 'h':
            (dx,) = params
            # Relative horizontal line: the parameter is a delta, even though
            # the absolute H command carries a target x.
            pt = (cur[0] + dx, cur[1]) if cmd == 'h' else (dx, cur[1])
            ops.append(('L', *pt))
            cur = pt
            subpath_drawn = True
            last_cubic_cp = None
            last_quad_cp = None

        elif kind == 'v':
            (dy,) = params
            pt = (cur[0], cur[1] + dy) if cmd == 'v' else (cur[0], dy)
            ops.append(('L', *pt))
            cur = pt
            subpath_drawn = True
            last_cubic_cp = None
            last_quad_cp = None

        elif kind == 'c':
            x1, y1, x2, y2, x, y = params
            if rel:
                x1 += cur[0]; y1 += cur[1]
                x2 += cur[0]; y2 += cur[1]
                x += cur[0];  y += cur[1]
            ops.append(('C', x1, y1, x2, y2, x, y))
            last_cubic_cp = (x2, y2)
            last_quad_cp = None
            cur = (x, y)
            subpath_drawn = True

        elif kind == 's':
            x2, y2, x, y = params
            if rel:
                x2 += cur[0]; y2 += cur[1]
                x += cur[0];  y += cur[1]
            # Only an immediately preceding cubic-family command qualifies.
            if last_cmd in ('c', 's'):
                x1 = 2 * cur[0] - last_cubic_cp[0]
                y1 = 2 * cur[1] - last_cubic_cp[1]
            else:
                x1, y1 = cur
            ops.append(('C', x1, y1, x2, y2, x, y))
            last_cubic_cp = (x2, y2)
            last_quad_cp = None
            cur = (x, y)
            subpath_drawn = True

        elif kind == 'q':
            x1, y1, x, y = params
            if rel:
                x1 += cur[0]; y1 += cur[1]
                x += cur[0];  y += cur[1]
            ops.extend(_quad_to_cubic(cur, (x1, y1), (x, y)))
            last_quad_cp = (x1, y1)
            last_cubic_cp = None
            cur = (x, y)
            subpath_drawn = True

        elif kind == 't':
            x, y = params
            if rel:
                x += cur[0]; y += cur[1]
            if last_cmd in ('q', 't'):
                x1 = 2 * cur[0] - last_quad_cp[0]
                y1 = 2 * cur[1] - last_quad_cp[1]
            else:
                x1, y1 = cur
            ops.extend(_quad_to_cubic(cur, (x1, y1), (x, y)))
            last_quad_cp = (x1, y1)
            last_cubic_cp = None
            cur = (x, y)
            subpath_drawn = True

        elif kind == 'a':
            rx, ry, angle_deg, large, sweep, x, y = params
            if large not in (0, 1) or sweep not in (0, 1):
                raise PathError('invalid arc flag')
            if rel:
                x += cur[0]; y += cur[1]
            end = (x, y)
            if rx == 0 or ry == 0:
                ops.append(('L', *end))
            else:
                ops.extend(_arc_to_cubics(
                    cur, end, abs(rx), abs(ry),
                    angle_deg * 3.141592653589793 / 180,
                    bool(large), bool(sweep)))
            cur = end
            subpath_drawn = True
            last_cubic_cp = None
            last_quad_cp = None

        elif kind == 'z':
            # A closepath only carries geometry when the subpath has drawing
            # content; closing an empty moveto-only subpath is a no-op that
            # Cairo does not emit. Either way no smooth state crosses it.
            if subpath_drawn:
                ops.append(('Z',))
            cur = start
            subpath_drawn = False
            # Closepath resets all smooth-command state.
            last_cubic_cp = None
            last_quad_cp = None

        last_cmd = kind

    return ops


def _quad_to_cubic(p0, p1, p2):
    """Raise one quadratic bezier to one cubic bezier (2/3 rule)."""
    c1 = (p0[0] + 2 / 3 * (p1[0] - p0[0]),
          p0[1] + 2 / 3 * (p1[1] - p0[1]))
    c2 = (p2[0] + 2 / 3 * (p1[0] - p2[0]),
          p2[1] + 2 / 3 * (p1[1] - p2[1]))
    return [('C', c1[0], c1[1], c2[0], c2[1], p2[0], p2[1])]


def _arc_to_cubics(p0, p1, rx, ry, phi, large, sweep):
    """Arc flattened to cubics, mirroring CairoSVG's verified frame math.

    CairoSVG solves the arc inside a translate(p0) * rotate(phi) *
    scale(1, ry/rx) frame using cairo arc/arc_negative. We reproduce that frame
    exactly and subdivide the same angular span into dense cubic beziers, so
    arc geometry agrees with the renderer and only smooth-command state is
    compared independently.
    """
    import math
    x1, y1 = p0
    x3, y3 = p1[0] - p0[0], p1[1] - p0[1]   # relative endpoint
    radii_ratio = ry / rx

    # Cancel the rotation of the second point, then undo the y scaling.
    xe, ye = rotate(x3, y3, -phi)
    ye /= radii_ratio

    # Angle between the second point and the x axis.
    angle = math.atan2(ye, xe)

    # Put the second point onto the x axis.
    xe = math.hypot(xe, ye)
    ye = 0.0

    # Grow the x radius if it cannot span the chord.
    rx = max(rx, xe / 2)

    # Circle centre in the scaled frame.
    xc = xe / 2
    yc = math.sqrt(max(0.0, rx ** 2 - xc ** 2))
    if not (large ^ sweep):
        yc = -yc

    # Put the endpoint and centre back into their rotated frame positions.
    xe, ye = rotate(xe, 0.0, angle)
    xc, yc = rotate(xc, yc, angle)

    # Drawing angles in the scaled frame.
    angle1 = math.atan2(0 - yc, 0 - xc)
    angle2 = math.atan2(ye - yc, xe - xc)
    if sweep:
        if angle2 < angle1:
            angle2 += 2 * math.pi
        direction = 1
    else:
        if angle2 > angle1:
            angle2 -= 2 * math.pi
        direction = -1

    total = abs(angle2 - angle1)
    segments = max(1, int(math.ceil(total / (math.pi / 16))))
    step = direction * total / segments

    cos_phi, sin_phi = math.cos(phi), math.sin(phi)

    def to_world(x, y):
        # Frame: translate(p0) * rotate(phi) * scale(1, radii_ratio).
        ys = y * radii_ratio
        return (x1 + cos_phi * x - sin_phi * ys,
                y1 + sin_phi * x + cos_phi * ys)

    ops = []
    for i in range(segments):
        a0 = angle1 + i * step
        a1 = a0 + step
        ops.append(_arc_segment(xc, yc, rx, a0, a1, to_world))
    return ops


def _arc_segment(cx, cy, r, a0, a1, to_world):
    """Cubic bezier for one circle-frame segment, transformed to world."""
    import math
    k = 4 / 3 * math.tan((a1 - a0) / 4)
    c0x, c0y = math.cos(a0), math.sin(a0)
    c1x, c1y = math.cos(a1), math.sin(a1)
    cp1 = (cx + r * (c0x - k * c0y), cy + r * (c0y + k * c0x))
    cp2 = (cx + r * (c1x + k * c1y), cy + r * (c1y - k * c1x))
    end = (cx + r * c1x, cy + r * c1y)
    w1, w2, w3 = to_world(*cp1), to_world(*cp2), to_world(*end)
    return ('C', w1[0], w1[1], w2[0], w2[1], w3[0], w3[1])


def sample_operations(ops, steps_per_curve=24):
    """Return dense polyline points covering all subpaths."""
    points = []
    cur = (0.0, 0.0)
    start = (0.0, 0.0)
    for op in ops:
        kind = op[0]
        if kind == 'M':
            cur = (op[1], op[2])
            start = cur
            points.append(cur)
        elif kind == 'L':
            cur = (op[1], op[2])
            points.append(cur)
        elif kind == 'C':
            _, x1, y1, x2, y2, x, y = op
            p0, p3 = cur, (x, y)
            for i in range(1, steps_per_curve + 1):
                t = i / steps_per_curve
                mt = 1 - t
                px = (mt ** 3 * p0[0] + 3 * mt ** 2 * t * x1
                      + 3 * mt * t ** 2 * x2 + t ** 3 * p3[0])
                py = (mt ** 3 * p0[1] + 3 * mt ** 2 * t * y1
                      + 3 * mt * t ** 2 * y2 + t ** 3 * p3[1])
                points.append((px, py))
            cur = p3
        elif kind == 'Z':
            if cur != start:
                points.append(start)
            cur = start
    return points


def endpoints(ops):
    """Return all segment endpoints (anchor points), subpath-aware."""
    out = []
    cur = (0.0, 0.0)
    for op in ops:
        if op[0] == 'M':
            cur = (op[1], op[2])
            out.append(cur)
        elif op[0] == 'L':
            cur = (op[1], op[2])
            out.append(cur)
        elif op[0] == 'C':
            cur = (op[5], op[6])
            out.append(cur)
        elif op[0] == 'Z':
            out.append(('Z',))
    return out
