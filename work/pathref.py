"""Reference SVG path parser (spec-correct) + CairoSVG output capture.

Normalizes paths to absolute commands M, L, C, Z (quadratics converted to
cubics with the same formula CairoSVG uses) so they can be compared with the
cairo path CairoSVG actually emits.
"""
import math
import re
import sys

sys.path.insert(0, '/Users/lichao/.local/share/user-feedback/graphics/20261117/render-20261117-12592-path-smooth')

from cairosvg.helpers import quadratic_points  # noqa: E402

NUM = re.compile(r'[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?')
CMD = re.compile(r'[a-zA-Z]')


def tokenize(d):
    tokens = []
    i = 0
    while i < len(d):
        c = d[i]
        if c.isspace() or c == ',':
            i += 1
        elif CMD.fullmatch(c):
            tokens.append(c)
            i += 1
        else:
            m = NUM.match(d, i)
            if not m:
                raise ValueError(f'bad token at {i}: {d[i:i+10]!r}')
            tokens.append(float(m.group()))
            i = m.end()
    return tokens


def reference_path(d):
    """Return list of ('M',x,y) ('L',x,y) ('C',x1,y1,x2,y2,x,y) ('Z',)."""
    toks = tokenize(d)
    out = []
    i = 0
    cur = (0.0, 0.0)
    start = None
    last_cmd = None
    # last cubic control point (absolute, the second control point) and end
    last_cubic_cp = None
    last_quad_cp = None

    def nums(n):
        nonlocal i
        vals = toks[i:i + n]
        if len(vals) != n or any(isinstance(v, str) for v in vals):
            raise ValueError('not enough parameters')
        i += n
        return vals

    while i < len(toks):
        cmd = toks[i]
        if not isinstance(cmd, str):
            raise ValueError('expected command letter')
        i += 1
        rel = cmd.islower()
        c = cmd.upper()

        def pt():
            x, y = nums(2)
            if rel:
                return cur[0] + x, cur[1] + y
            return x, y

        if c == 'M':
            p = pt()
            out.append(('M', *p))
            cur = p
            start = p
            last_cmd = 'M'
            last_cubic_cp = None
            last_quad_cp = None
            # subsequent coordinate pairs are implicit L/l
            while i < len(toks) and not isinstance(toks[i], str):
                p = pt()
                out.append(('L', *p))
                cur = p
                last_cmd = 'L'
        elif c == 'L':
            while True:
                p = pt()
                out.append(('L', *p))
                cur = p
                last_cmd = 'L'
                last_cubic_cp = None
                last_quad_cp = None
                if i >= len(toks) or isinstance(toks[i], str):
                    break
        elif c == 'H':
            while True:
                (x,) = nums(1)
                if rel:
                    x += cur[0]
                out.append(('L', x, cur[1]))
                cur = (x, cur[1])
                last_cmd = 'L'
                last_cubic_cp = None
                last_quad_cp = None
                if i >= len(toks) or isinstance(toks[i], str):
                    break
        elif c == 'V':
            while True:
                (y,) = nums(1)
                if rel:
                    y += cur[1]
                out.append(('L', cur[0], y))
                cur = (cur[0], y)
                last_cmd = 'L'
                last_cubic_cp = None
                last_quad_cp = None
                if i >= len(toks) or isinstance(toks[i], str):
                    break
        elif c == 'C':
            while True:
                x1, y1, x2, y2, x3, y3 = nums(6)
                if rel:
                    x1 += cur[0]; y1 += cur[1]
                    x2 += cur[0]; y2 += cur[1]
                    x3 += cur[0]; y3 += cur[1]
                out.append(('C', x1, y1, x2, y2, x3, y3))
                cur = (x3, y3)
                last_cubic_cp = (x2, y2)
                last_quad_cp = None
                last_cmd = 'C'
                if i >= len(toks) or isinstance(toks[i], str):
                    break
        elif c == 'S':
            while True:
                x2, y2, x3, y3 = nums(4)
                if rel:
                    x2 += cur[0]; y2 += cur[1]
                    x3 += cur[0]; y3 += cur[1]
                if last_cmd == 'C':
                    x1 = 2 * cur[0] - last_cubic_cp[0]
                    y1 = 2 * cur[1] - last_cubic_cp[1]
                else:
                    x1, y1 = cur
                out.append(('C', x1, y1, x2, y2, x3, y3))
                cur = (x3, y3)
                last_cubic_cp = (x2, y2)
                last_quad_cp = None
                last_cmd = 'C'
                if i >= len(toks) or isinstance(toks[i], str):
                    break
        elif c == 'Q':
            while True:
                x2, y2, x3, y3 = nums(4)
                if rel:
                    x2 += cur[0]; y2 += cur[1]
                    x3 += cur[0]; y3 += cur[1]
                q = quadratic_points(cur[0], cur[1], x2, y2, x3, y3)
                out.append(('C', *q))
                cur = (x3, y3)
                last_quad_cp = (x2, y2)
                last_cubic_cp = None
                last_cmd = 'Q'
                if i >= len(toks) or isinstance(toks[i], str):
                    break
        elif c == 'T':
            while True:
                x3, y3 = nums(2)
                if rel:
                    x3 += cur[0]; y3 += cur[1]
                if last_cmd == 'Q':
                    x2 = 2 * cur[0] - last_quad_cp[0]
                    y2 = 2 * cur[1] - last_quad_cp[1]
                else:
                    x2, y2 = cur
                q = quadratic_points(cur[0], cur[1], x2, y2, x3, y3)
                out.append(('C', *q))
                cur = (x3, y3)
                last_quad_cp = (x2, y2)
                last_cubic_cp = None
                last_cmd = 'Q'
                if i >= len(toks) or isinstance(toks[i], str):
                    break
        elif c == 'Z':
            out.append(('Z',))
            cur = start
            last_cmd = 'Z'
            last_cubic_cp = None
            last_quad_cp = None
        else:
            raise ValueError(f'unsupported cmd {cmd}')
    return out


class FakeContext:
    """Records only what's needed to compare curve/line/move/close output."""

    def __init__(self):
        self.cur = None
        self.subpath_start = None
        self.ops = []

    def has_current_point(self):
        return self.cur is not None

    def get_current_point(self):
        return self.cur

    def move_to(self, x, y):
        self.cur = (x, y)
        self.subpath_start = (x, y)
        self.ops.append(('M', x, y))

    def rel_move_to(self, x, y):
        self.move_to(self.cur[0] + x, self.cur[1] + y)

    def line_to(self, x, y):
        self.cur = (x, y)
        self.ops.append(('L', x, y))

    def rel_line_to(self, x, y):
        self.line_to(self.cur[0] + x, self.cur[1] + y)

    def curve_to(self, x1, y1, x2, y2, x3, y3):
        self.cur = (x3, y3)
        self.ops.append(('C', x1, y1, x2, y2, x3, y3))

    def rel_curve_to(self, x1, y1, x2, y2, x3, y3):
        x, y = self.cur
        self.curve_to(x + x1, y + y1, x + x2, y + y2, x + x3, y + y3)

    def close_path(self):
        # Real Cairo restores the current point to the subpath start.
        self.ops.append(('Z',))
        if self.subpath_start is not None:
            self.cur = self.subpath_start

    # arc-related calls are not expected in the fuzz set
    def set_tolerance(self, t):
        pass

    def save(self):
        pass

    def restore(self):
        pass

    def translate(self, *a):
        pass

    def rotate(self, *a):
        pass

    def scale(self, *a):
        pass

    def arc(self, *a):
        raise AssertionError('arc not supported in fake context')

    arc_negative = arc


class FakeSurface:
    def __init__(self):
        self.context = FakeContext()


class FakeNode(dict):
    def __init__(self, d):
        super().__init__()
        self['d'] = d
        self.vertices = None


def cairosvg_path(d):
    from cairosvg.path import path as draw_path
    surface = FakeSurface()
    node = FakeNode(d)
    draw_path(surface, node)
    ops = surface.context.ops
    # CairoSVG seeds an empty context with move_to(0, 0) before the first
    # command; drop that leading origin move so it compares with the reference.
    if ops[:1] == [('M', 0, 0)]:
        ops = ops[1:]
    return ops


def _canonicalize(ops):
    # Normalize no-op closes so both sides compare equally:
    # - a second Z while already closed is a no-op per the spec;
    # - a Z closing a subpath that only contains a move (no drawn segment)
    #   is a geometric no-op, and Cairo drops it from the recorded path.
    out = []
    for op in ops:
        if op == ('Z',):
            if out and out[-1] == ('Z',):
                continue
            if len(out) == 1 and out[0][0] == 'M':
                continue
            # M ... Z with nothing between the last M and this Z
            last_move = max(
                (i for i, o in enumerate(out) if o[0] == 'M'), default=-1)
            if last_move >= 0 and all(
                    o[0] == 'M' for o in out[last_move:]):
                continue
        out.append(op)
    return out


def paths_equivalent(ref, got, tol=1e-9):
    ref = _canonicalize(ref)
    got = _canonicalize(got)
    if len(ref) != len(got):
        return False, f'length {len(ref)} vs {len(got)}'
    for n, (a, b) in enumerate(zip(ref, got)):
        if a[0] != b[0]:
            return False, f'op {n}: {a[0]} vs {b[0]}'
        for va, vb in zip(a[1:], b[1:]):
            if abs(va - vb) > tol * max(1.0, abs(va), abs(vb)):
                return False, f'op {n}: {a} vs {b}'
    return True, ''
