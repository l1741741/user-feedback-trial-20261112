"""Independent geometry reference for SVG path bounding boxes.

This module is deliberately independent from CairoSVG's own parsing: it
contains its own SVG path-data tokenizer/parser and computes tight bounding
boxes by two fully independent methods:

1. analytic extrema for cubic/quadratic Beziers and elliptical arcs
   (root finding on the derivative),
2. dense adaptive flattening (de Casteljau subdivision / arc sampling), used as
   a cross-check of the analytic code.

Everything works in plain user-space coordinates, with no Cairo dependency, so
it can run with ``python -I`` on any interpreter.
"""

import re
from math import (
    atan2, cos, fmod, hypot, pi, radians, sin, sqrt)

TAU = 2 * pi


def _angle(x, y):
    """Angle of vector (x, y) in [0, 2*pi)."""
    return fmod(TAU + atan2(y, x), TAU)


def cubic_extrema(p0, p1, p2, p3):
    """Return min/max x/y of the cubic Bezier with control points p0..p3."""
    xs = [p0[0], p3[0]]
    ys = [p0[1], p3[1]]
    for coord, out in ((0, xs), (1, ys)):
        a = -p0[coord] + 3 * p1[coord] - 3 * p2[coord] + p3[coord]
        b = 2 * (p0[coord] - 2 * p1[coord] + p2[coord])
        c = p1[coord] - p0[coord]
        for root in _quadratic_roots(a, b, c):
            if 0 < root < 1:
                out.append(_cubic_point(p0, p1, p2, p3, root)[coord])
    return min(xs), min(ys), max(xs), max(ys)


def _cubic_point(p0, p1, p2, p3, t):
    mt = 1 - t
    return (
        mt ** 3 * p0[0] + 3 * mt ** 2 * t * p1[0]
        + 3 * mt * t ** 2 * p2[0] + t ** 3 * p3[0],
        mt ** 3 * p0[1] + 3 * mt ** 2 * t * p1[1]
        + 3 * mt * t ** 2 * p2[1] + t ** 3 * p3[1])


def quadratic_extrema(p0, p1, p2):
    """Return min/max x/y of the quadratic Bezier p0/p1/p2."""
    xs = [p0[0], p2[0]]
    ys = [p0[1], p2[1]]
    for coord, out in ((0, xs), (1, ys)):
        a = p0[coord] - 2 * p1[coord] + p2[coord]
        b = 2 * (p1[coord] - p0[coord])
        if a:
            t = -b / (2 * a)
            if 0 < t < 1:
                out.append(_quadratic_point(p0, p1, p2, t)[coord])
    return min(xs), min(ys), max(xs), max(ys)


def _quadratic_point(p0, p1, p2, t):
    mt = 1 - t
    return (
        mt ** 2 * p0[0] + 2 * mt * t * p1[0] + t ** 2 * p2[0],
        mt ** 2 * p0[1] + 2 * mt * t * p1[1] + t ** 2 * p2[1])


def _quadratic_roots(a, b, c):
    if abs(a) < 1e-15:
        if abs(b) >= 1e-15:
            return (-c / b,)
        return ()
    disc = b * b - 4 * a * c
    if disc < 0:
        return ()
    if disc == 0:
        return (-b / (2 * a),)
    sq = sqrt(disc)
    return ((-b - sq) / (2 * a), (-b + sq) / (2 * a))


def arc_endpoint_to_center(x1, y1, rx, ry, phi, large, sweep, x2, y2):
    """SVG arc endpoint-to-center parameterization (SVG 1.1 F.6).

    Returns ``(cx, cy, rx, ry, theta1, dtheta)`` or ``None`` when the arc
    degenerates to a line.
    """
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0 or (x1 == x2 and y1 == y2):
        return None
    cphi, sphi = cos(phi), sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p = cphi * dx + sphi * dy
    y1p = -sphi * dx + cphi * dy

    # Radii too small: scale up (F.6.6)
    lam = x1p ** 2 / rx ** 2 + y1p ** 2 / ry ** 2
    if lam > 1:
        s = sqrt(lam)
        rx *= s
        ry *= s

    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    factor = sqrt(max(0, num / den))
    if large == sweep:
        factor = -factor
    cxp = factor * rx * y1p / ry
    cyp = -factor * ry * x1p / rx
    cx = cphi * cxp - sphi * cyp + (x1 + x2) / 2
    cy = sphi * cxp + cphi * cyp + (y1 + y2) / 2

    theta1 = _angle((x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = _angle(
        (-x1p - cxp) / rx, (-y1p - cyp) / ry) - theta1
    if not sweep and dtheta > 0:
        dtheta -= TAU
    elif sweep and dtheta < 0:
        dtheta += TAU
    return cx, cy, rx, ry, theta1, dtheta


def arc_extrema(x1, y1, rx, ry, phi, large, sweep, x2, y2):
    """Tight bounding box (minx, miny, maxx, maxy) of an elliptical arc."""
    params = arc_endpoint_to_center(
        x1, y1, rx, ry, phi, large, sweep, x2, y2)
    if params is None:
        return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
    cx, cy, rx, ry, theta1, dtheta = params
    cphi, sphi = cos(phi), sin(phi)

    def point(t):
        ex, ey = rx * cos(t), ry * sin(t)
        return (cx + cphi * ex - sphi * ey,
                cy + sphi * ex + cphi * ey)

    # Extreme angles of the rotated ellipse on each axis:
    # x'(t) = 0 -> tan(t) = -ry*sin(phi)/(rx*cos(phi))
    # y'(t) = 0 -> tan(t) =  ry*cos(phi)/(rx*sin(phi))
    candidates = [theta1, theta1 + dtheta]
    base_x = atan2(-ry * sphi, rx * cphi)
    base_y = atan2(ry * cphi, rx * sphi)
    for base in (base_x, base_y):
        for k in range(-2, 3):
            candidates.append(base + k * pi)

    lo = min(theta1, theta1 + dtheta)
    hi = max(theta1, theta1 + dtheta)
    pts = [(x1, y1), (x2, y2)]
    for t in candidates:
        if lo - 1e-12 <= t <= hi + 1e-12:
            pts.append(point(t))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def sample_arc(x1, y1, rx, ry, phi, large, sweep, x2, y2, steps=4000):
    """Dense sampling cross-check for :func:`arc_extrema`."""
    params = arc_endpoint_to_center(
        x1, y1, rx, ry, phi, large, sweep, x2, y2)
    if params is None:
        pts = [(x1, y1), (x2, y2)]
    else:
        cx, cy, rx, ry, theta1, dtheta = params
        cphi, sphi = cos(phi), sin(phi)
        pts = []
        for i in range(steps + 1):
            t = theta1 + dtheta * i / steps
            ex, ey = rx * cos(t), ry * sin(t)
            pts.append((cphi * ex - sphi * ey + cx,
                        sphi * ex + cphi * ey + cy))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def flatten_bbox(commands, steps=2000):
    """Reference bbox by uniformly flattening every segment.

    ``commands`` is a list of ``(letter, args)`` tuples using absolute
    coordinates with implicit smooth/arc control points already resolved
    (i.e. the output of ``geometry.resolve_commands``).
    """
    pts = []
    current = start = (0.0, 0.0)
    for letter, args in commands:
        if letter == 'M':
            current = start = (args[0], args[1])
            pts.append(current)
        elif letter == 'L':
            current = (args[0], args[1])
            pts.append(current)
        elif letter == 'H':
            current = (args[0], current[1])
            pts.append(current)
        elif letter == 'V':
            current = (current[0], args[0])
            pts.append(current)
        elif letter == 'C':
            p1, p2, p3 = (args[0], args[1]), (args[2], args[3]), \
                (args[4], args[5])
            for i in range(steps + 1):
                pts.append(_cubic_point(current, p1, p2, p3, i / steps))
            current = p3
        elif letter == 'Q':
            p1, p2 = (args[0], args[1]), (args[2], args[3])
            for i in range(steps + 1):
                pts.append(_quadratic_point(current, p1, p2, i / steps))
            current = p2
        elif letter == 'A':
            rx, ry, phi, large, sweep, ex, ey = args
            box = sample_arc(
                current[0], current[1], rx, ry, phi, large, sweep,
                ex, ey, steps=steps)
            pts.extend(((box[0], box[1]), (box[2], box[3])))
            current = (ex, ey)
        elif letter == 'Z':
            current = start
            pts.append(current)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------
# Independent SVG path-data parser and full analytic bounding box
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r'[MmLlHhVvCcSsQqTtAaZz]|[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?')


def parse_path_commands(d):
    """Parse SVG path data ``d`` into absolute ``(letter, args)`` commands.

    Implicit repetitions, relative commands, smooth curves (``S/s/T/t``) and
    the implicit ``lineto`` following ``moveto`` are all resolved here, so the
    output only contains the absolute letters
    ``M L H V C Q A Z`` with explicit control points. The parser is written
    from the SVG 1.1 specification and shares no code with CairoSVG.
    """
    tokens = _TOKEN.findall(d)
    i = 0
    n = len(tokens)
    current = [0.0, 0.0]
    start = [0.0, 0.0]
    last_letter = None
    last_cubic = None      # absolute previous cubic control point
    last_quad = None       # absolute previous quadratic control point
    commands = []

    def num():
        nonlocal i
        value = float(tokens[i])
        i += 1
        return value

    while i < n:
        token = tokens[i]
        if re.fullmatch(r'[MmLlHhVvCcSsQqTtAaZz]', token):
            letter = token
            i += 1
        else:
            # Implicit repetition keeps the previous letter; an implicit
            # moveto repeats as lineto per SVG 1.1 section 8.3.2.
            letter = {'m': 'l', 'M': 'L'}.get(last_letter, last_letter)

        relative = letter.islower()
        upper = letter.upper()

        if upper == 'Z':
            commands.append(('Z', ()))
            current = list(start)
            last_cubic = last_quad = None

        elif upper == 'M':
            x, y = num(), num()
            if relative:
                x += current[0]
                y += current[1]
            current = [x, y]
            start = list(current)
            commands.append(('M', (x, y)))
            last_cubic = last_quad = None

        elif upper == 'L':
            x, y = num(), num()
            if relative:
                x += current[0]
                y += current[1]
            commands.append(('L', (x, y)))
            current = [x, y]
            last_cubic = last_quad = None

        elif upper == 'H':
            x = num()
            if relative:
                x += current[0]
            commands.append(('H', (x,)))
            current[0] = x
            last_cubic = last_quad = None

        elif upper == 'V':
            y = num()
            if relative:
                y += current[1]
            commands.append(('V', (y,)))
            current[1] = y
            last_cubic = last_quad = None

        elif upper == 'C':
            x1, y1, x2, y2, x, y = (num() for _ in range(6))
            if relative:
                x1 += current[0]
                y1 += current[1]
                x2 += current[0]
                y2 += current[1]
                x += current[0]
                y += current[1]
            commands.append(('C', (x1, y1, x2, y2, x, y)))
            last_cubic = (x2, y2)
            last_quad = None
            current = [x, y]

        elif upper == 'S':
            x2, y2, x, y = (num() for _ in range(4))
            if relative:
                x2 += current[0]
                y2 += current[1]
                x += current[0]
                y += current[1]
            if last_letter and last_letter.upper() in ('C', 'S'):
                x1 = 2 * current[0] - last_cubic[0]
                y1 = 2 * current[1] - last_cubic[1]
            else:
                x1, y1 = current
            commands.append(('C', (x1, y1, x2, y2, x, y)))
            last_cubic = (x2, y2)
            last_quad = None
            current = [x, y]

        elif upper == 'Q':
            x1, y1, x, y = (num() for _ in range(4))
            if relative:
                x1 += current[0]
                y1 += current[1]
                x += current[0]
                y += current[1]
            commands.append(('Q', (x1, y1, x, y)))
            last_quad = (x1, y1)
            last_cubic = None
            current = [x, y]

        elif upper == 'T':
            x, y = num(), num()
            if relative:
                x += current[0]
                y += current[1]
            if last_letter and last_letter.upper() in ('Q', 'T'):
                x1 = 2 * current[0] - last_quad[0]
                y1 = 2 * current[1] - last_quad[1]
            else:
                x1, y1 = current
            commands.append(('Q', (x1, y1, x, y)))
            last_quad = (x1, y1)
            last_cubic = None
            current = [x, y]

        elif upper == 'A':
            rx, ry = num(), num()
            phi = radians(num())
            large = bool(num())
            sweep = bool(num())
            x, y = num(), num()
            if relative:
                x += current[0]
                y += current[1]
            commands.append(
                ('A', (rx, ry, phi, large, sweep, x, y)))
            last_cubic = last_quad = None
            current = [x, y]

        last_letter = letter

    return commands


def analytic_path_bbox(d):
    """Tight analytic bounding box of path data ``d``.

    Returns ``(minx, miny, maxx, maxy)`` or ``None`` for an empty path.
    """
    commands = parse_path_commands(d)
    if not commands:
        return None
    minx = miny = float('inf')
    maxx = maxy = float('-inf')
    current = start = (0.0, 0.0)

    def include(box):
        nonlocal minx, miny, maxx, maxy
        minx = min(minx, box[0])
        miny = min(miny, box[1])
        maxx = max(maxx, box[2])
        maxy = max(maxy, box[3])

    for letter, args in commands:
        if letter == 'M':
            start = current = (args[0], args[1])
            include((current[0], current[1], current[0], current[1]))
        elif letter == 'L':
            current = (args[0], args[1])
            include((current[0], current[1], current[0], current[1]))
        elif letter == 'H':
            current = (args[0], current[1])
            include((current[0], current[1], current[0], current[1]))
        elif letter == 'V':
            current = (current[0], args[0])
            include((current[0], current[1], current[0], current[1]))
        elif letter == 'C':
            p1, p2, p3 = (args[0], args[1]), (args[2], args[3]), \
                (args[4], args[5])
            include(cubic_extrema(current, p1, p2, p3))
            current = p3
        elif letter == 'Q':
            p1, p2 = (args[0], args[1]), (args[2], args[3])
            include(quadratic_extrema(current, p1, p2))
            current = p2
        elif letter == 'A':
            rx, ry, phi, large, sweep, ex, ey = args
            include(arc_extrema(
                current[0], current[1], rx, ry, phi, large, sweep, ex, ey))
            current = (ex, ey)
        elif letter == 'Z':
            current = start
    return minx, miny, maxx, maxy


def flattened_path_bbox(d, steps=2000):
    """Dense-flattening bounding box of path data ``d`` (cross-check)."""
    return flatten_bbox(parse_path_commands(d), steps=steps)
