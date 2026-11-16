"""
Regression tests for the smooth path commands (S/s, T/t).

The SVG specification says a smooth segment only inherits the reflected
control point of an *eligible* previous curve:

- S/s follows a C/c/S/s segment;
- T/t follows a Q/q/T/t segment.

Any other command (straight line, arc, move, close) resets the inherited
control point and the smooth segment then uses the current point as its
first control point. The state is local to a path: subpaths and independent
path nodes never share it. These tests capture the actual Cairo path emitted
by :mod:`cairosvg.path` instead of relying on rendered pixels.
"""

import cairocffi as cairo
import pytest

from .path import path as draw_path

CU = cairo.PATH_CURVE_TO


class RecordingSurface:
    """Minimal surface recording the operations emitted by ``path``."""

    def __init__(self):
        self._recording = cairo.RecordingSurface(
            cairo.CONTENT_COLOR_ALPHA, None)
        self.context = cairo.Context(self._recording)
        self.markers = {}
        self.parent_node = {}

    def draw(self, node):
        """Markers are irrelevant here."""


class PathNode(dict):
    def __init__(self, d):
        super().__init__()
        self['d'] = d
        self.vertices = None
        self.children = []


def emitted_curves(d):
    """Return the cubic Bézier segments (6-tuples) emitted for ``d``."""
    surface = RecordingSurface()
    draw_path(surface, PathNode(d))
    return [
        points for kind, points in surface.context.copy_path()
        if kind == CU]


def emitted_operations(d):
    surface = RecordingSurface()
    draw_path(surface, PathNode(d))
    return list(surface.context.copy_path())


def _flatten(sequences):
    return [value for sequence in sequences for value in sequence]


def assert_curves_match(d1, d2, abs=1e-9):
    assert _flatten(emitted_curves(d1)) == pytest.approx(
        _flatten(emitted_curves(d2)), abs=abs)


# Explicit control point writing vs. smooth writing must produce the same
# cubic segment. Each pair is the same geometric curve expressed twice.
EQUIVALENT_CUBIC = [
    ('M10 80 C 40 10 65 10 95 80 C 125 150 150 150 180 80',
     'M10 80 C 40 10 65 10 95 80 S 150 150 180 80'),
    ('m10 80 c 30 -70 55 -70 85 0 c 30 70 55 70 85 0',
     'm10 80 c 30 -70 55 -70 85 0 s 55 70 85 0'),
    ('M10 80 C 40 10 65 10 95 80 C 125 150 150 150 180 80'
     ' C 210 10 215 10 240 80',
     'M10 80 C 40 10 65 10 95 80 S 150 150 180 80 S 215 10 240 80'),
    # mixed absolute / relative commands
    ('M10 80 c 30 -70 55 -70 85 0 C 125 150 150 150 180 80',
     'M10 80 c 30 -70 55 -70 85 0 S 150 150 180 80'),
    ('m10 80 C 40 10 65 10 95 80 c 30 70 55 70 85 0',
     'm10 80 C 40 10 65 10 95 80 s 55 70 85 0'),
]


@pytest.mark.parametrize('explicit,smooth', EQUIVALENT_CUBIC)
def test_smooth_cubic_matches_explicit_controls(explicit, smooth):
    assert_curves_match(explicit, smooth, abs=1e-9)


EQUIVALENT_QUADRATIC = [
    # Q then T: the reflected quadratic control point is (150, 10) for a
    # control point (40, 10) mirrored around the current point (95, 80).
    ('M10 80 Q 40 10 95 80 Q 150 150 180 80',
     'M10 80 Q 40 10 95 80 T 180 80'),
    ('m10 80 q 30 -70 85 0 q 55 70 85 0',
     'm10 80 q 30 -70 85 0 t 85 0'),
    ('M10 80 q 30 -70 85 0 Q 150 150 180 80',
     'M10 80 q 30 -70 85 0 T 180 80'),
    ('m10 80 Q 40 10 95 80 t 85 0',
     'm10 80 Q 40 10 95 80 q 55 70 85 0'),
]


@pytest.mark.parametrize('explicit,smooth', EQUIVALENT_QUADRATIC)
def test_smooth_quadratic_matches_explicit_controls(explicit, smooth):
    assert_curves_match(explicit, smooth, abs=1e-7)


# A smooth command with no eligible predecessor uses the current point as
# its first control point, so the first two points of the emitted cubic
# coincide with the current point.
DEFAULT_TANGENT = [
    ('M10 80 S 150 150 180 80', (10, 80)),
    ('m10 80 s 140 70 170 0', (10, 80)),
    ('M10 80 T 180 80', (10, 80)),
    ('m10 80 t 170 0', (10, 80)),
]


@pytest.mark.parametrize('d,current', DEFAULT_TANGENT)
def test_smooth_without_predecessor_uses_current_point(d, current):
    curve = emitted_curves(d)[0]
    assert curve[:2] == pytest.approx(current, abs=1e-9)


INTERRUPTERS = [
    # straight lines, in every casing
    'M10 80 C 40 10 65 10 95 80 L 120 80 S 150 150 180 80',
    'M10 80 C 40 10 65 10 95 80 l 25 0 s 30 70 60 0',
    'M10 80 C 40 10 65 10 95 80 H 120 S 150 150 180 80',
    'M10 80 C 40 10 65 10 95 80 h 25 s 30 70 60 0',
    'M10 80 C 40 10 65 10 95 80 V 200 S 150 150 180 80',
    'M10 80 C 40 10 65 10 95 80 v 120 s 30 -50 60 -120',
    # a quadratic does not make a cubic eligible and vice versa
    'M10 80 C 40 10 65 10 95 80 Q 140 20 180 80 S 220 150 260 80',
    'M10 80 Q 40 10 95 80 C 120 150 150 150 180 80 T 260 80',
    'M10 80 Q 40 10 95 80 L 140 80 T 180 80',
]


@pytest.mark.parametrize('d', INTERRUPTERS)
def test_interrupting_command_resets_cubic_reflection(d):
    """After a non-cubic command, S/s starts from the current point."""
    operations = emitted_operations(d)
    last_curve = [
        points for kind, points in operations if kind == CU][-1]
    # The current point when the smooth starts is the end point of the
    # previous drawn segment.
    previous_end = None
    for kind, points in operations:
        if kind == CU and points == last_curve:
            break
        if kind == cairo.PATH_LINE_TO:
            previous_end = points
        elif kind == CU:
            previous_end = points[-2:]
    assert last_curve[:2] == pytest.approx(previous_end, abs=1e-9)


def test_close_resets_relative_smooth_cubic():
    # The relative s after Z must not reflect the control point of the curve
    # drawn before the close: its first control is the subpath start point.
    d = 'M10 80 C 40 10 65 10 95 80 Z s 190 -60 230 0'
    last_curve = emitted_curves(d)[-1]
    assert last_curve[:2] == pytest.approx((10, 80), abs=1e-9)
    # Explicit equivalent: the reflected control collapses onto (10, 80).
    explicit = 'M10 80 C 40 10 65 10 95 80 Z C 10 80 200 20 240 80'
    assert emitted_curves(d)[-1] == pytest.approx(
        emitted_curves(explicit)[-1], abs=1e-9)

def test_close_resets_relative_smooth_quadratic():
    d = 'M10 80 Q 40 10 95 80 Z t 230 0'
    last_curve = emitted_curves(d)[-1]
    # Quadratic control point equals the current (subpath start) point, hence
    # the cubic conversion puts the first control point at 1/3·P0 + 2/3·P0.
    assert last_curve[:2] == pytest.approx((10, 80), abs=1e-7)


def test_move_starts_fresh_subpath_state():
    d = ('M10 80 C 40 10 65 10 95 80 S 150 150 180 80 '
         'M 200 80 S 280 10 320 80')
    curves = emitted_curves(d)
    # The smooth of the second subpath has no eligible predecessor: its first
    # control point is its own current point (200, 80).
    assert curves[-1][:2] == pytest.approx((200, 80), abs=1e-9)


def test_quadratic_state_not_shared_between_subpaths():
    d = 'M10 80 Q 40 10 95 80 T 180 80 M 200 80 T 320 80'
    curves = emitted_curves(d)
    assert curves[-1][:2] == pytest.approx((200, 80), abs=1e-7)


def test_two_independent_paths_do_not_share_state():
    first = RecordingSurface()
    draw_path(first, PathNode('M10 80 C 40 10 65 10 95 80'))
    second = RecordingSurface()
    draw_path(second, PathNode('M10 80 S 150 150 180 80'))
    second_curve = [
        points for kind, points in second.context.copy_path()
        if kind == CU][0]
    assert second_curve[:2] == pytest.approx((10, 80), abs=1e-9)


def test_repeated_smooth_coordinate_groups():
    # Implicit command repetition must keep chaining the reflection.
    chained = 'M10 80 S 60 20 95 80 S 150 150 180 80 S 220 20 260 80'
    explicit = ('M10 80 C 10 80 60 20 95 80 '
                'C 130 140 150 150 180 80 '
                'C 210 10 220 20 260 80')
    assert_curves_match(chained, explicit, abs=1e-9)
    # Relative variant, including repeated coordinate groups.
    rel = 'm10 80 s 50 -60 85 0 s 55 70 85 0'
    rel_explicit = 'm10 80 c 0 0 50 -60 85 0 c 35 60 55 70 85 0'
    assert_curves_match(rel, rel_explicit, abs=1e-9)


def test_leading_smooth_command_does_not_crash():
    # Regression: last_letter was None and `None in 'csCS'` raised TypeError.
    for d in ('S 100 100 200 100', 's 100 100 200 100',
              'T 200 100', 't 200 100'):
        operations = emitted_operations(d)
        assert any(kind == CU for kind, _ in operations)


# Specification-equivalence guards for the reflection state. A relative
# smooth command (s/t) drawn after a close or any non-curve command must
# restart from the current point rather than reflect an earlier control
# point; an immediately repeated smooth group keeps chaining normally.
# These pin the behaviour the explicit-state rewrite is required to preserve.
STALE_STATE_CASES = [
    # close followed by a relative cubic smooth, including implicit repeat
    ('M10 80 C 40 10 65 10 95 80 Z s 190 -60 230 0',
     'M10 80 C 40 10 65 10 95 80 Z C 10 80 200 20 240 80'),
    ('M10 80 C 40 10 65 10 95 80 z s 50 -60 85 0 55 70 85 0',
     'M10 80 C 40 10 65 10 95 80 z '
     'C 10 80 60 20 95 80 C 130 140 150 150 180 80'),
    # close followed by a relative quadratic smooth
    ('M10 80 Q 40 10 95 80 Z t 230 0',
     'M10 80 Q 40 10 95 80 Z Q 10 80 240 80'),
    ('M10 80 q 30 -70 85 0 z t 85 0 60 0',
     'M10 80 q 30 -70 85 0 z Q 10 80 95 80 Q 180 80 155 80'),
    # straight lines reset both reflection states, relative commands too
    ('M10 80 C 40 10 65 10 95 80 H 120 s 30 70 60 0',
     'M10 80 C 40 10 65 10 95 80 H 120 C 120 80 150 150 180 80'),
    ('M10 80 Q 40 10 95 80 V 200 t 60 0',
     'M10 80 Q 40 10 95 80 V 200 Q 95 200 155 200'),
    # cubic state must not leak into a quadratic smooth and vice versa
    ('M10 80 C 40 10 65 10 95 80 Q 140 20 180 80 S 220 150 260 80',
     'M10 80 C 40 10 65 10 95 80 Q 140 20 180 80 '
     'C 180 80 220 150 260 80'),
    ('M10 80 Q 40 10 95 80 C 120 150 150 150 180 80 T 260 80',
     'M10 80 Q 40 10 95 80 C 120 150 150 150 180 80 Q 180 80 260 80'),
]


@pytest.mark.parametrize('smooth,explicit', STALE_STATE_CASES)
def test_relative_smooth_restarts_after_interruption(smooth, explicit):
    # Compare only the last curve, which is the segment drawn after the
    # interrupting command.
    assert emitted_curves(smooth)[-1] == pytest.approx(
        emitted_curves(explicit)[-1], abs=1e-7)
