"""Regression tests for SVG path smooth-command control-point state.

These tests pin down the rules the renderer must follow for the S/s and T/t
commands:

* a smooth command only inherits the reflection of an eligible immediately
  preceding curve control point (cubic for S/s, quadratic for T/t);
* after a line, arc, moveto or closepath the default tangent is the current
  point, i.e. the first control point collapses onto the current point;
* relative commands and repeated implicit coordinate groups behave the same as
  their absolute counterparts;
* subpaths and independent paths never share drawing state;
* malformed commands follow the existing PointError rule instead of silently
  drawing a stale curve.

Geometry is read back from a recording surface so the tests check exactly the
path primitives handed to Cairo.
"""

import math

import cairocffi
import pytest

from .helpers import PointError
from .path import path

TOL = 1e-3


class FakeNode(dict):
    def __init__(self, d):
        super().__init__()
        self['d'] = d
        self.vertices = None


def emit(d):
    """Return the normalized Cairo path operations produced for ``d``."""
    recording = cairocffi.RecordingSurface(
        cairocffi.CONTENT_COLOR_ALPHA, (0, 0, 1000, 1000))
    context = cairocffi.Context(recording)
    path(type('Surface', (), {'context': context})(), FakeNode(d))
    raw = context.copy_path()
    recording.finish()
    ops = []
    for kind, coords in raw:
        if kind == cairocffi.PATH_MOVE_TO:
            ops.append(('M', *coords))
        elif kind == cairocffi.PATH_LINE_TO:
            ops.append(('L', *coords))
        elif kind == cairocffi.PATH_CURVE_TO:
            ops.append(('C', *coords))
        elif kind == cairocffi.PATH_CLOSE_PATH:
            ops.append(('Z',))
    return ops


def curves(ops):
    return [op for op in ops if op[0] == 'C']


def assert_close(actual, expected, tol=TOL):
    # Accept either a single op tuple or a list of ops.
    if actual and isinstance(actual[0], str):
        actual = [actual]
    if expected and isinstance(expected[0], str):
        expected = [expected]
    assert len(actual) == len(expected), (actual, expected)
    for a, e in zip(actual, expected):
        assert a[0] == e[0]
        for xa, xe in zip(a[1:], e[1:]):
            assert math.isclose(xa, xe, abs_tol=tol), (actual, expected)


def test_explicit_cubic_matches_smooth_cubic():
    """An S after a C equals the explicit reflection of the C's 2nd point."""
    # C ... 50 10 with second control point (40, 20); reflected about (50,10)
    # gives (60, 0), so the S's first control point must be (60, 0).
    smooth = curves(emit('M10 10 C 20 20 40 20 50 10 S 80 0 90 10'))
    explicit = curves(emit('M10 10 C 20 20 40 20 50 10 C 60 0 80 0 90 10'))
    assert_close(smooth, explicit)
    assert_close(smooth[-1], ('C', 60.0, 0.0, 80.0, 0.0, 90.0, 10.0))


def test_relative_cubic_smooth_reflection():
    """Lowercase c/s use the same reflection in relative coordinates."""
    # c ends at (50,10); its absolute 2nd control point is (40,20), reflected
    # about (50,10) to (60,0), i.e. a relative delta of (10,-10).
    smooth = curves(emit('m10 10 c 10 10 30 10 40 0 s 30 -10 40 0'))
    explicit = curves(emit('m10 10 c 10 10 30 10 40 0 c 10 -10 30 -10 40 0'))
    assert_close(smooth, explicit)


def test_smooth_cubic_after_line_uses_current_point():
    """After a line, S's first control point defaults to the current point."""
    op = curves(emit('M10 10 L 50 10 S 80 0 90 10'))[-1]
    assert_close(op, ('C', 50.0, 10.0, 80.0, 0.0, 90.0, 10.0))
    # h/v variants and relative forms must agree.
    assert_close(curves(emit('M10 10 H 50 S 80 0 90 10'))[-1],
                 ('C', 50.0, 10.0, 80.0, 0.0, 90.0, 10.0))
    assert_close(curves(emit('M10 10 V 50 S 80 60 90 50'))[-1],
                 ('C', 10.0, 50.0, 80.0, 60.0, 90.0, 50.0))
    assert_close(curves(emit('M10 10 l 40 0 s 30 -10 40 0'))[-1],
                 ('C', 50.0, 10.0, 80.0, 0.0, 90.0, 10.0))


def test_smooth_cubic_after_moveto_starts_without_state():
    """A moveto starts a fresh subpath: no inherited cubic control point."""
    op = curves(emit('M10 10 C 20 20 40 20 50 10 M 60 10 S 90 0 100 10'))[-1]
    assert_close(op, ('C', 60.0, 10.0, 90.0, 0.0, 100.0, 10.0))
    op = curves(emit('M10 10 C 20 20 40 20 50 10 m 10 0 s 30 -10 40 0'))[-1]
    assert_close(op, ('C', 60.0, 10.0, 90.0, 0.0, 100.0, 10.0))


def test_smooth_cubic_after_closepath_resets_state():
    """Closepath resets the inherited cubic control point."""
    op = curves(emit('M10 10 C 20 20 40 20 50 10 L 60 40 Z S 90 40 100 30'))[-1]
    # The new subpath starts again at (10,10), which becomes the first point.
    assert_close(op, ('C', 10.0, 10.0, 90.0, 40.0, 100.0, 30.0))


def test_smooth_cubic_after_arc_resets_state():
    """An arc is not a cubic: the following S uses the current point."""
    op = curves(emit('M10 10 A 20 20 0 0 1 50 10 S 80 0 90 10'))[-1]
    assert_close(op, ('C', 50.0, 10.0, 80.0, 0.0, 90.0, 10.0))
    # A zero-radius arc degrades to a line and must reset state too.
    op = curves(emit('M10 10 a 0 0 0 0 1 40 0 s 30 -10 40 0'))[-1]
    assert_close(op, ('C', 50.0, 10.0, 80.0, 0.0, 90.0, 10.0))


def test_chained_smooth_cubic_reflects_each_previous_point():
    """Repeated S coordinate groups keep reflecting the latest control point."""
    ops = curves(emit(
        'M10 10 C 20 20 40 20 50 10 S 70 0 80 10 S 100 20 110 10'))
    # First S: reflect (40,20) about (50,10) -> (60,0).
    assert_close(ops[1], ('C', 60.0, 0.0, 70.0, 0.0, 80.0, 10.0))
    # Second S: reflect its second point (70,0) about (80,10) -> (90,20).
    assert_close(ops[2], ('C', 90.0, 20.0, 100.0, 20.0, 110.0, 10.0))


def test_explicit_quadratic_matches_smooth_quadratic():
    """A T after a Q equals the explicit reflected quadratic control point."""
    # Q control point (30,10), end (50,50); reflected about (50,50) -> (70,90).
    smooth = curves(emit('M10 50 Q 30 10 50 50 T 90 50'))
    explicit_quad = ('M10 50 Q 30 10 50 50 Q 70 90 90 50')
    assert_close(smooth, curves(emit(explicit_quad)))


def test_relative_quadratic_smooth_all_cases():
    """q/t and mixed case chains match their absolute equivalents."""
    cases = [
        ('m10 50 q 20 -40 40 0 t 40 0', 'M10 50 Q 30 10 50 50 T 90 50'),
        ('M10 50 Q 30 10 50 50 t 40 0', 'M10 50 Q 30 10 50 50 T 90 50'),
        ('m10 50 q 20 -40 40 0 T 90 50', 'M10 50 Q 30 10 50 50 T 90 50'),
        ('M10 50 Q 30 10 50 50 T 90 50 T 130 50',
         'M10 50 Q 30 10 50 50 T 90 50 T 130 50'),
    ]
    for relative, absolute in cases:
        assert_close(curves(emit(relative)), curves(emit(absolute)))


def test_smooth_quadratic_after_interrupting_commands():
    """T after line/cubic/moveto/arc/closepath defaults to the current point."""
    assert_close(curves(emit('M10 50 L 50 50 T 90 50'))[-1],
                 curves(emit('M10 50 L 50 50 Q 50 50 90 50'))[-1])
    assert_close(curves(emit('M10 50 C 20 10 40 10 50 50 T 90 50'))[-1],
                 curves(emit('M10 50 C 20 10 40 10 50 50 Q 50 50 90 50'))[-1])
    assert_close(curves(emit('M10 50 Q 30 10 50 50 M 60 50 T 100 50'))[-1],
                 curves(emit('M60 50 Q 60 50 100 50'))[-1])
    assert_close(curves(emit('M10 50 A 20 20 0 0 1 50 50 T 90 50'))[-1],
                 curves(emit('M10 50 A 20 20 0 0 1 50 50 Q 50 50 90 50'))[-1])


def test_cubic_and_quadratic_state_do_not_cross():
    """The two curve families keep independent smooth-command state."""
    # A Q between C and S must clear the cubic reflection.
    op = curves(emit('M10 10 C 20 20 40 20 50 10 Q 60 0 80 10 T 120 10'))[-1]
    # T follows a Q so it reflects the quadratic point, not any cubic point.
    assert_close(op, curves(emit('M10 10 C 20 20 40 20 50 10 Q 60 0 80 10 '
                                 'Q 100 20 120 10'))[-1])
    # A C between Q and S clears the quadratic reflection; S instead reflects
    # that cubic's 2nd control point (80,90) about its end (90,50) -> (100,10).
    op = curves(emit('M10 50 Q 30 10 50 50 C 60 90 80 90 90 50 S 120 10 130 50'))[-1]
    assert_close(op, ('C', 100.0, 10.0, 120.0, 10.0, 130.0, 50.0))


def test_smooth_command_as_first_command_does_not_crash():
    """A path starting with a smooth command uses the implicit origin point."""
    # Regression: last_letter was None and `None in 'csCS'` raised TypeError.
    assert_close(curves(emit('M10 10 S 40 0 50 10')),
                 [('C', 10.0, 10.0, 40.0, 0.0, 50.0, 10.0)])
    assert_close(curves(emit('m10 10 s 30 -10 40 0')),
                 [('C', 10.0, 10.0, 40.0, 0.0, 50.0, 10.0)])
    first_t = curves(emit('M10 50 T 50 50'))[0]
    assert_close(first_t, ('C', 10.0, 50.0, 23.332, 50.0, 50.0, 50.0))
    assert emit('T 50 50')  # implicit moveto to origin, must not raise
    assert emit('t 40 0')
    assert emit('S 40 0 50 10')
    assert emit('s 30 10 40 0')


def test_subpaths_do_not_share_state():
    """A second subpath's smooth command ignores the first subpath's state."""
    two = emit('M10 10 C 20 20 40 20 50 10 S 80 0 90 10 '
               'M 10 90 S 60 80 90 90')
    isolated = emit('M10 90 S 60 80 90 90')
    # The final curve of the two-subpath path equals the standalone path.
    assert_close(curves(two)[-1], curves(isolated)[-1])
    two = emit('M10 50 Q 30 10 50 50 T 90 50 M 10 130 T 90 130')
    isolated = emit('M10 130 T 90 130')
    assert_close(curves(two)[-1], curves(isolated)[-1])


def test_two_independent_paths_are_independent():
    """Drawing state never leaks between separate path() invocations."""
    first = emit('M10 10 C 20 20 40 20 50 10 S 80 0 90 10')
    second = emit('M10 10 S 80 0 90 10')
    # The second path never saw the first path's cubic control point.
    assert curves(second)[0][1:3] == (10.0, 10.0)
    assert curves(first)[-1][1:3] == (60.0, 0.0)


def test_malformed_command_raises_point_error():
    """Truncated smooth commands raise PointError rather than drawing."""
    for d in ('M10 10 S 80 0', 'M10 10 T', 'M10 10 c 1 2 3'):
        with pytest.raises(PointError):
            emit(d)


def test_repeated_coordinate_groups():
    """Implicitly repeated commands carry state across every coordinate set."""
    cubic = curves(emit('M10 10 c 10 10 30 10 40 0 s 20 -10 30 0 s 20 10 30 0'))
    assert len(cubic) == 3
    quad = curves(emit('M10 50 Q 30 10 50 50 T 90 50 T 130 50 T 170 50'))
    assert len(quad) == 4
