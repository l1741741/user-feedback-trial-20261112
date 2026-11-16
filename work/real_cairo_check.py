"""Capture the real Cairo path emitted by CairoSVG (not a fake context)
and check smooth-command control points around arcs and closes."""
import sys

sys.path.insert(0, '/Users/lichao/.local/share/user-feedback/graphics/20261117/render-20261117-12592-path-smooth')

import cairocffi as cairo
from cairosvg.parser import Tree
from cairosvg.surface import Surface
from cairosvg.path import path as draw_path


class RecSurface:
    def __init__(self):
        self.surf = cairo.RecordingSurface(cairo.CONTENT_COLOR_ALPHA, None)
        self.context = cairo.Context(self.surf)
        self.markers = {}
        self.parent_node = {}

    def draw(self, node):
        pass


class Node(dict):
    def __init__(self, d):
        super().__init__()
        self['d'] = d
        self.vertices = None
        self.children = []


def cairo_ops(d):
    surface = RecSurface()
    node = Node(d)
    draw_path(surface, node)
    out = []
    p = surface.context.copy_path()
    for kind, points in p:
        if kind == cairo.PATH_MOVE_TO:
            out.append(('M', round(points[0], 6), round(points[1], 6)))
        elif kind == cairo.PATH_LINE_TO:
            out.append(('L', round(points[0], 6), round(points[1], 6)))
        elif kind == cairo.PATH_CURVE_TO:
            out.append(('C', *(round(v, 6) for v in points)))
        elif kind == cairo.PATH_CLOSE_PATH:
            out.append(('Z',))
    return out


# After an arc/line/close, the smooth curve's first control point must equal
# the current point (no reflection). Extract the first C following a reset.
checks = [
    ('M10 80 C 40 10 65 10 95 80 A 30 30 0 0 1 150 80 S 220 10 260 80',
     'arc then S: first control == current (150,80)'),
    ('M10 80 C 40 10 65 10 95 80 A 0 30 0 0 1 150 80 S 220 10 260 80',
     'degenerate arc then S: first control == current (150,80)'),
    ('M10 80 C 40 10 65 10 95 80 Z s 190 -60 230 0',
     'Z then relative s: first control == current (10,80)'),
    ('M10 80 Q 40 10 95 80 Z t 230 0',
     'Z then relative t: quadratic control == current (10,80)'),
]
ok_all = True
for d, desc in checks:
    ops = cairo_ops(d)
    print(desc)
    print('  ', ops)
    if 'arc' in desc:
        smooth = [o for o in ops if o[0] == 'C'][-1]
        if (smooth[1], smooth[2]) != (150.0, 80.0):
            ok_all = False
            print('   FAIL: expected first control (150,80)')
    elif desc.startswith('Z then relative s'):
        smooth = [o for o in ops if o[0] == 'C'][-1]
        if (smooth[1], smooth[2]) != (10.0, 80.0):
            ok_all = False
            print('   FAIL: expected first control (10,80)')
    elif desc.startswith('Z then relative t'):
        # quadratic with control == current => cubic first control is
        # 1/3 current + 2/3 control == current point (10,80)
        smooth = [o for o in ops if o[0] == 'C'][-1]
        if abs(smooth[1] - 10.0) > 1e-6 or abs(smooth[2] - 80.0) > 1e-6:
            ok_all = False
            print('   FAIL: expected cubic first control at (10,80)')
print('ALL OK' if ok_all else 'FAILURES PRESENT')
sys.exit(0 if ok_all else 1)
