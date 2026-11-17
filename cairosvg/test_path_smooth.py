"""
Regression tests for the state saved and reset between SVG path commands.

The smooth cubic commands (S/s) and smooth quadratic commands (T/t) must only
inherit the reflected control point of an *eligible* previous command. After a
line, an arc, a move or a close, and when the smooth command starts a (sub)path,
they fall back to the default tangent defined by the SVG specification:

- the first cubic control point is the current point (absolute S) or a
  zero-length relative point (relative s);
- the reflected quadratic control point is the current point (absolute T) or a
  zero-length relative point (relative t).

The geometry is checked on the cubic Bézier segments actually emitted to the
Cairo context, comparing a smooth-written path against the equivalent path
expanded with explicit control points.

"""

import cairocffi
import pytest

from .helpers import PointError
from .parser import Tree
from .surface import Surface


class _SnapshotContext(cairocffi.Context):
    """Context keeping a copy of the path before it is consumed."""
    def __init__(self, target):
        super().__init__(target)
        self.snapshots = []

    def fill(self, *args, **kwargs):
        self.snapshots.append(list(self.copy_path()))

    def stroke(self, *args, **kwargs):
        self.snapshots.append(list(self.copy_path()))

    def fill_preserve(self, *args, **kwargs):
        self.snapshots.append(list(self.copy_path()))

    def stroke_preserve(self, *args, **kwargs):
        self.snapshots.append(list(self.copy_path()))


class _RecordingSurface(Surface):
    """Surface keeping the vector path operators for inspection."""
    device_units_per_user_units = 1

    def _create_surface(self, width, height):
        cairo_surface = cairocffi.RecordingSurface(
            cairocffi.CONTENT_COLOR_ALPHA, None)
        return cairo_surface, width, height

    @property
    def context(self):
        return self._context

    @context.setter
    def context(self, value):
        if not isinstance(value, _SnapshotContext) and value is not None:
            value = _SnapshotContext(value.get_target())
        self._context = value


def _path_segments(path_d, extra=''):
    """Return the Cairo path segments emitted for the given ``d`` string."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200">'
        f'<path d="{path_d}" fill="none" stroke="black" {extra}/></svg>')
    tree = Tree(bytestring=svg.encode())
    surface = _RecordingSurface(
        tree, output=None, dpi=96, output_width=300, output_height=200)
    segments = []
    for snapshot in surface.context.snapshots:
        for segment in snapshot:
            if segment not in segments:
                segments.append(segment)
    return segments


def _curves(path_d):
    """Return only the cubic curve segments (Cairo CURVE_TO tuples)."""
    return [segment[1] for segment in _path_segments(path_d)
            if segment[0] == cairocffi.PATH_CURVE_TO]


def _assert_same_curves(explicit_d, smooth_d):
    explicit = _curves(explicit_d)
    smooth = _curves(smooth_d)
    assert smooth == explicit


def test_smooth_cubic_after_cubic():
    """S/s reflect the second control point of a previous C/c/S/s."""
    _assert_same_curves(
        'M10 80 C40 10 65 10 95 80 C125 150 150 150 180 80',
        'M10 80 C40 10 65 10 95 80 S150 150 180 80')
    _assert_same_curves(
        'm10 80 c30 -70 55 -70 85 0 c30 70 55 70 85 0',
        'm10 80 c30 -70 55 -70 85 0 s55 70 85 0')


def test_smooth_cubic_chain_and_repeated_groups():
    """Chained S/s and implicit repeated coordinate groups stay smooth."""
    explicit = (
        'M10 80 C40 10 65 10 95 80 C125 150 150 150 180 80 '
        'C210 10 235 10 265 80')
    _assert_same_curves(explicit, 'M10 80 C40 10 65 10 95 80 '
                                  'S150 150 180 80 S235 10 265 80')
    # The letter is omitted for the second coordinate group.
    _assert_same_curves(explicit, 'M10 80 C40 10 65 10 95 80 '
                                  'S150 150 180 80 235 10 265 80')


def test_smooth_cubic_mixed_case_shares_absolute_state():
    """A relative c stores absolute control points used by a later S."""
    _assert_same_curves(
        'm10 80 c30 -70 55 -70 85 0 C125 150 150 150 180 80',
        'm10 80 c30 -70 55 -70 85 0 S150 150 180 80')


def test_smooth_cubic_starting_path_uses_default_tangent():
    """S/s as the first command must not crash and must use the default."""
    # Absolute S: first control point is the implicit current point (0, 0).
    assert _curves('S150 150 180 80') == [(0, 0, 150, 150, 180, 80)]
    # Relative s: first control point is a zero-length relative point.
    assert _curves('s140 70 170 0') == [(0, 0, 140, 70, 170, 0)]


def test_smooth_cubic_after_line_commands():
    """L/l, H/h and V/v reset the cubic reflection state."""
    _assert_same_curves(
        'M10 80 L95 80 C95 80 150 150 180 80',
        'M10 80 L95 80 S150 150 180 80')
    _assert_same_curves(
        'M10 80 H95 c0 0 55 70 85 0',
        'M10 80 H95 s55 70 85 0')
    _assert_same_curves(
        'M10 10 V80 C10 80 150 150 180 80',
        'M10 10 V80 S150 150 180 80')


def test_smooth_cubic_after_arc():
    """A/a are not cubic curves and reset the reflection state."""
    _assert_same_curves(
        'M10 80 A40 40 0 0 1 95 80 C95 80 150 150 180 80',
        'M10 80 A40 40 0 0 1 95 80 S150 150 180 80')


def test_smooth_cubic_after_zero_radius_arc():
    """An arc with rx=0 (drawn as a line) resets the reflection state."""
    _assert_same_curves(
        'M10 80 L95 80 C95 80 150 150 180 80',
        'M10 80 A0 40 0 0 1 95 80 S150 150 180 80')
    _assert_same_curves(
        'm10 80 l85 0 c0 0 55 70 85 0',
        'm10 80 a0 40 0 0 1 85 0 s55 70 85 0')


def test_smooth_cubic_after_move_and_close():
    """M/m and Z/z open a new subpath with fresh reflection state."""
    _assert_same_curves(
        'M10 80 C40 10 65 10 95 80 M100 100 C100 100 150 150 180 80',
        'M10 80 C40 10 65 10 95 80 M100 100 S150 150 180 80')
    _assert_same_curves(
        'M10 80 C40 10 65 10 95 80 Z M100 100 C100 100 150 150 180 80',
        'M10 80 C40 10 65 10 95 80 Z M100 100 S150 150 180 80')


def test_smooth_cubic_ignores_quadratic_state():
    """Q/q/T/t are not eligible previous commands for S/s."""
    _assert_same_curves(
        'M10 80 Q52 10 95 80 C95 80 150 150 180 80',
        'M10 80 Q52 10 95 80 S150 150 180 80')
    _assert_same_curves(
        'm10 80 q42 -70 85 0 c0 0 55 70 85 0',
        'm10 80 q42 -70 85 0 s55 70 85 0')


def test_smooth_quadratic_after_quadratic():
    """T/t reflect the control point of a previous Q/q/T/t."""
    _assert_same_curves(
        'M10 80 Q52 10 95 80 Q138 150 180 80',
        'M10 80 Q52 10 95 80 T180 80')
    # The reflected relative control point is (43, 70).
    _assert_same_curves(
        'm10 80 q42 -70 85 0 q43 70 85 0',
        'm10 80 q42 -70 85 0 t85 0')


def test_smooth_quadratic_chain_and_repeated_groups():
    """Chained T/t and implicit repeated coordinate groups stay smooth."""
    explicit = (
        'M10 80 Q52 10 95 80 Q138 150 180 80 Q222 10 265 80')
    _assert_same_curves(explicit, 'M10 80 Q52 10 95 80 T180 80 T265 80')
    _assert_same_curves(explicit, 'M10 80 Q52 10 95 80 T180 80 265 80')
    _assert_same_curves(
        'm10 80 q42 -70 85 0 q43 70 85 0 q42 -70 85 0',
        'm10 80 q42 -70 85 0 t85 0 85 0')


def test_smooth_quadratic_starting_path_uses_default_tangent():
    """T/t as the first command must not crash and must use the default."""
    # Absolute T: the control point is the current point (0, 0).
    assert _curves('T95 80') == _curves('M0 0 Q0 0 95 80')
    # Relative t: a zero-length relative control point.
    assert _curves('t85 0') == _curves('m0 0 q0 0 85 0')


def test_smooth_quadratic_after_interruptions():
    """Lines, moves, closes and cubic curves reset the quadratic state."""
    _assert_same_curves(
        'M10 10 L10 80 Q10 80 95 80',
        'M10 10 L10 80 T95 80')
    _assert_same_curves(
        'M10 80 C40 10 65 10 95 80 Q95 80 180 80',
        'M10 80 C40 10 65 10 95 80 T180 80')
    _assert_same_curves(
        'M10 80 Q52 10 95 80 M100 100 Q100 100 180 80',
        'M10 80 Q52 10 95 80 M100 100 T180 80')
    _assert_same_curves(
        'M10 80 Q52 10 95 80 Z M100 100 Q100 100 180 80',
        'M10 80 Q52 10 95 80 Z M100 100 T180 80')


def test_independent_paths_do_not_share_state():
    """A smooth command starting a second <path> gets a fresh default."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200">'
        '<path d="M10 80 C40 10 65 10 95 80"/>'
        '<path d="M100 100 S150 150 180 80"/></svg>')
    tree = Tree(bytestring=svg.encode())
    surface = _RecordingSurface(
        tree, output=None, dpi=96, output_width=300, output_height=200)
    curves = []
    for snapshot in surface.context.snapshots:
        for segment in snapshot:
            if segment[0] == cairocffi.PATH_CURVE_TO and segment not in curves:
                curves.append(segment)
    curves = [segment[1] for segment in curves]
    # The second path starts at (100, 100): its first control point must be
    # (100, 100), not the reflection of the first path's (65, 10).
    assert (100, 100, 150, 150, 180, 80) in curves
    assert all(segment[0] != 125 for segment in curves)


def test_malformed_smooth_command_does_not_draw_stale_curve():
    """Missing coordinates raise PointError and leave no smooth curve behind.

    Surface.draw() catches PointError to follow the existing error rule, so
    the path function itself is exercised directly here.

    """
    from .path import path as draw_path

    class PathNode(dict):
        """Minimal node object accepted by the path drawing function."""

    def build_surface():
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="300" '
               'height="200"/>')
        tree = Tree(bytestring=svg.encode())
        return _RecordingSurface(
            tree, output=None, dpi=96, output_width=300, output_height=200)

    for path_d in (
            'M10 80 C40 10 65 10 95 80 S150',
            'M10 80 Q52 10 95 80 T',
            'M10 80 s140'):
        surface = build_surface()
        node = PathNode(d=path_d)
        node.vertices = None
        with pytest.raises(PointError):
            draw_path(surface, node)

    # The valid prefix is still the only curve ever emitted: the broken
    # smooth command does not draw a curve with stale control points.
    surface = build_surface()
    node = PathNode(d='M10 80 C40 10 65 10 95 80 S150')
    node.vertices = None
    with pytest.raises(PointError):
        draw_path(surface, node)
    assert [segment for segment in surface.context.copy_path()
            if segment[0] == cairocffi.PATH_CURVE_TO] == [
        (cairocffi.PATH_CURVE_TO, (40, 10, 65, 10, 95, 80))]
