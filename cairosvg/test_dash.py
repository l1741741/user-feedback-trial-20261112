"""
Tests for stroke dashing.

These tests cover the SVG semantics of ``stroke-dasharray`` and
``stroke-dashoffset``: parsing of the various length notations, the treatment
of odd lists, zero and negative entries, the phase/offset relationship,
inheritance and local overrides, and the robustness required so that invalid
declarations never break a render or leak onto other nodes.

"""

import io
import signal
from math import hypot, isclose

import cairocffi as cairo
import pytest

from . import svg2png
from .helpers import parse_dash_array, parse_dash_offset


class FakeSurface:
    """Minimal surface exposing what :func:`size` needs to resolve lengths."""
    context_width = 400
    context_height = 300
    font_size = 16
    dpi = 96


SURFACE = FakeSurface()
PERCENT_REFERENCE = hypot(400, 300) / 2 ** 0.5


# ---------------------------------------------------------------------------
# Parsing unit tests
# ---------------------------------------------------------------------------

def test_dash_array_absent_or_none_is_solid():
    assert parse_dash_array(SURFACE, '') == ([], False, True)
    assert parse_dash_array(SURFACE, None) == ([], False, True)
    assert parse_dash_array(SURFACE, 'none') == ([], False, True)
    assert parse_dash_array(SURFACE, 'NONE') == ([], False, True)


def test_dash_array_even_list():
    dashes, dashed, stroked = parse_dash_array(SURFACE, '10,4')
    assert dashed and stroked
    assert dashes == [10.0, 4.0]


def test_dash_array_odd_list_is_doubled():
    # An odd list is repeated so on and off lengths keep their SVG meaning.
    assert parse_dash_array(SURFACE, '10,4,2') == (
        [10.0, 4.0, 2.0, 10.0, 4.0, 2.0], True, True)
    # A single entry becomes an equal on/off pair.
    assert parse_dash_array(SURFACE, '6') == ([6.0, 6.0], True, True)


def test_dash_array_all_zero_disables_stroke():
    dashes, dashed, stroked = parse_dash_array(SURFACE, '0,0')
    assert not dashed and not stroked
    assert dashes == [0.0, 0.0]
    # A lone zero is an odd list, doubled to two zeros.
    assert parse_dash_array(SURFACE, '0') == ([0.0, 0.0], False, False)


def test_dash_array_zero_entries_are_otherwise_kept():
    assert parse_dash_array(SURFACE, '0,0,6,4') == (
        [0.0, 0.0, 6.0, 4.0], True, True)
    assert parse_dash_array(SURFACE, '0,4') == ([0.0, 4.0], True, True)


def test_dash_array_negative_entry_is_invalid_solid():
    assert parse_dash_array(SURFACE, '5,-2') == ([], False, True)
    assert parse_dash_array(SURFACE, '-5') == ([], False, True)


def test_dash_array_non_finite_entry_is_invalid_solid():
    assert parse_dash_array(SURFACE, 'nan,4') == ([], False, True)
    assert parse_dash_array(SURFACE, 'inf,4') == ([], False, True)
    assert parse_dash_array(SURFACE, '1e999,4') == ([], False, True)


def test_dash_array_scientific_notation():
    assert parse_dash_array(SURFACE, '1e1, 4e0') == (
        [10.0, 4.0], True, True)
    assert parse_dash_array(SURFACE, '1.5e2') == (
        [150.0, 150.0], True, True)


def test_dash_array_percent_uses_viewport_diagonal():
    dashes, _, _ = parse_dash_array(SURFACE, '10%,2%')
    assert isclose(dashes[0], PERCENT_REFERENCE / 10)
    assert isclose(dashes[1], PERCENT_REFERENCE * 2 / 100)


def test_dash_array_font_and_physical_units():
    dashes, _, _ = parse_dash_array(SURFACE, '2em,1em')
    assert dashes == [32.0, 16.0]
    dashes, _, _ = parse_dash_array(SURFACE, '6pt')
    assert isclose(dashes[0], 6 * 96 / 72)


def test_dash_array_normalizes_whitespace_and_commas():
    assert parse_dash_array(SURFACE, ' 10  4 ') == (
        [10.0, 4.0], True, True)
    assert parse_dash_array(SURFACE, '10 ,4, 2') == (
        [10.0, 4.0, 2.0, 10.0, 4.0, 2.0], True, True)


def test_dash_offset():
    assert parse_dash_offset(SURFACE, '') == 0
    assert parse_dash_offset(SURFACE, None) == 0
    assert parse_dash_offset(SURFACE, '-3') == -3.0
    assert parse_dash_offset(SURFACE, '5') == 5.0
    assert parse_dash_offset(SURFACE, 'nan') == 0
    assert parse_dash_offset(SURFACE, 'inf') == 0
    assert isclose(
        parse_dash_offset(SURFACE, '10%'), PERCENT_REFERENCE / 10)


# ---------------------------------------------------------------------------
# Rendering tests, observed on exported PNGs
# ---------------------------------------------------------------------------

def render_png(inner, width=220, height=60):
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}">{inner}</svg>').encode()
    return svg2png(bytestring=svg)


def surface_from(png_bytes):
    return cairo.ImageSurface.create_from_png(io.BytesIO(png_bytes))


def ink_runs(png_bytes, y, x0=0, x1=None):
    """Return ``(start, length)`` runs of non-transparent pixels on a row."""
    surface = surface_from(png_bytes)
    if x1 is None:
        x1 = surface.get_width()
    data = bytes(surface.get_data())
    stride = surface.get_stride()

    def alpha(position):
        return data[y * stride + position * 4 + 3]

    return _runs(alpha, x0, x1)


def vertical_ink_runs(png_bytes, x, y0=0, y1=None):
    """Return ``(start, length)`` ink runs in a column."""
    surface = surface_from(png_bytes)
    if y1 is None:
        y1 = surface.get_height()
    data = bytes(surface.get_data())
    stride = surface.get_stride()

    def alpha(position):
        return data[position * stride + x * 4 + 3]

    return _runs(alpha, y0, y1)


def _runs(alpha, start, end):
    runs = []
    previous = False
    run_start = start
    for position in range(start, end):
        inked = alpha(position) > 0
        if inked and not previous:
            run_start = position
        elif not inked and previous:
            runs.append((run_start, position - run_start))
        previous = inked
    if previous:
        runs.append((run_start, end - run_start))
    return runs


def line(dasharray=None, dashoffset=None, x1=10, y=30, x2=210, width=220):
    attrs = ['stroke="black"', 'stroke-width="4"',
             f'x1="{x1}"', f'y1="{y}"', f'x2="{x2}"', f'y2="{y}"']
    if dasharray is not None:
        attrs.append(f'stroke-dasharray="{dasharray}"')
    if dashoffset is not None:
        attrs.append(f'stroke-dashoffset="{dashoffset}"')
    return render_png(f'<line {" ".join(attrs)}/>', width=width)


def test_solid_stroke_is_one_continuous_run():
    runs = ink_runs(line(), 30, 5, 215)
    assert runs == [(10, 200)]


def test_dashed_stroke_basic_pattern():
    runs = ink_runs(line('10,4'), 30, 5, 215)
    # 10 user units of ink starting at x=10, then a 4-unit gap, and so on.
    assert runs[0] == (10, 10)
    assert runs[1][0] == 24
    assert all(length == 10 for _, length in runs[:-1])


def test_odd_list_matches_its_doubled_form():
    odd = ink_runs(line('10,4,2'), 30, 5, 215)
    doubled = ink_runs(line('10,4,2,10,4,2'), 30, 5, 215)
    assert odd == doubled


def test_positive_offset_shortens_first_dash():
    runs = ink_runs(line('10,4', dashoffset=3), 30, 5, 215)
    assert runs[0] == (10, 7)
    assert runs[1] == (21, 10)


def test_negative_offset_shifts_pattern_forward():
    runs = ink_runs(line('10,4', dashoffset=-3), 30, 5, 215)
    assert runs[0][0] == 13
    assert runs[0][1] == 10


def test_offset_by_a_full_cycle_is_identity():
    baseline = ink_runs(line('10,4'), 30, 5, 215)
    assert ink_runs(line('10,4', dashoffset=14), 30, 5, 215) == baseline
    assert ink_runs(line('10,4', dashoffset=-14), 30, 5, 215) == baseline


def test_all_zero_dash_array_paints_nothing():
    assert ink_runs(line('0,0'), 30, 5, 215) == []
    assert ink_runs(line('0'), 30, 5, 215) == []


def test_disabled_stroke_does_not_retain_its_path_for_later_nodes():
    # fill_preserve() keeps the path for stroking. When an all-zero dash
    # array disables the stroke, that path must be dropped or the next node
    # would stroke it together with its own path.
    png = render_png(
        '<line x1="10" y1="15" x2="210" y2="15" stroke="black" '
        'stroke-width="4" stroke-dasharray="0,0"/>'
        '<path d="M10 45 L120 45 M140 45 L210 45" stroke="black" '
        'stroke-width="4" stroke-dasharray="20,10"/>', height=60)
    # Nothing may be painted on the disabled first line's row.
    assert ink_runs(png, 15, 5, 215) == []
    # The second path paints only its own two subpaths: no ink bridges the
    # 120..140 gap, which a retained first path would have filled.
    assert ink_runs(png, 45, 121, 139) == []


def test_mixed_zero_array_still_dashes():
    runs = ink_runs(line('0,0,6,4'), 30, 5, 215)
    assert runs[0] == (10, 6)
    assert all(length == 6 for _, length in runs[:-1])


def test_invalid_dash_array_falls_back_to_solid():
    assert ink_runs(line('5,-2'), 30, 5, 215) == [(10, 200)]
    assert ink_runs(line('nan,4'), 30, 5, 215) == [(10, 200)]
    assert ink_runs(line('inf,4'), 30, 5, 215) == [(10, 200)]


def test_invalid_dash_does_not_leak_into_next_sibling():
    png = render_png(
        '<g stroke="black" stroke-width="4">'
        '<line x1="10" y1="15" x2="210" y2="15" stroke-dasharray="5,-2"/>'
        '<line x1="10" y1="45" x2="210" y2="45"/></g>', height=60)
    # The second line has no dash array and must be fully solid.
    assert ink_runs(png, 45, 5, 215) == [(10, 200)]


def test_dash_array_inheritance_and_override():
    png = render_png(
        '<g stroke="black" stroke-width="4" stroke-dasharray="8,4">'
        '<line x1="10" y1="15" x2="210" y2="15"/>'
        '<line x1="10" y1="45" x2="210" y2="45" stroke-dasharray="none"/>'
        '</g>', height=60)
    inherited = ink_runs(png, 15, 5, 215)
    assert inherited[0] == (10, 8)
    assert ink_runs(png, 45, 5, 215) == [(10, 200)]


def test_dash_continues_through_polyline_corner():
    # Two segments of 100 units: the phase carries across the corner instead
    # of restarting there.
    polyline = render_png(
        '<polyline points="10,30 110,30 110,80" fill="none" stroke="black" '
        'stroke-width="4" stroke-dasharray="10,4"/>', height=100)
    # An isolated 50-unit vertical segment whose phase is advanced by the 100
    # units of the first leg (100 % 14 == 2), expressed with a +2 offset so
    # the reference dash starts at pattern index 2, like the polyline corner.
    reference = render_png(
        '<line x1="110" y1="30" x2="110" y2="80" stroke="black" '
        'stroke-width="4" stroke-dasharray="10,4" stroke-dashoffset="2"/>',
        width=220, height=100)
    # Sample a column clear of the miter join at the corner.
    assert vertical_ink_runs(polyline, 109, 34, 82) == vertical_ink_runs(
        reference, 109, 34, 82)
    # If the phase restarted at the corner, this offset reference would not
    # match: a fresh segment starts with a full 10-unit dash at y=30.
    fresh = render_png(
        '<line x1="110" y1="30" x2="110" y2="80" stroke="black" '
        'stroke-width="4" stroke-dasharray="10,4"/>', width=220, height=100)
    assert vertical_ink_runs(fresh, 109, 34, 82) != vertical_ink_runs(
        polyline, 109, 34, 82)


def test_each_subpath_restarts_its_phase():
    png = render_png(
        '<path d="M10 30 L100 30 M120 30 L210 30" stroke="black" '
        'stroke-width="4" stroke-dasharray="10,4"/>')
    first = ink_runs(png, 30, 5, 105)
    second = ink_runs(png, 30, 115, 215)
    # Both subpaths begin with a full 10-unit dash at their own start.
    assert first[0] == (10, 10)
    assert second[0] == (120, 10)


def test_closed_path_dashes_around_the_whole_perimeter():
    png = render_png(
        '<rect x="20" y="20" width="160" height="120" fill="none" '
        'stroke="black" stroke-width="6" stroke-dasharray="10,4"/>',
        width=200, height=160)
    surface = surface_from(png)

    def alpha(x, y):
        data = bytes(surface.get_data())
        return data[y * surface.get_stride() + x * 4 + 3]

    # Every edge, including the closing left edge, carries some ink.
    assert any(alpha(20, y) for y in range(20, 140))
    assert any(alpha(x, 20) for x in range(20, 180))
    assert any(alpha(180, y) for y in range(20, 140))
    assert any(alpha(x, 140) for x in range(20, 180))


def test_scaled_stroke_width_and_dash_pattern():
    png = render_png(
        '<g transform="scale(2)">'
        '<line x1="5" y1="15" x2="105" y2="15" stroke="black" '
        'stroke-width="3" stroke-dasharray="10,5"/></g>', height=50)
    surface = surface_from(png)
    data = bytes(surface.get_data())
    stride = surface.get_stride()
    rows = [y for y in range(surface.get_height())
            if data[y * stride + 40 + 3] > 0]
    # A width of 3 user units becomes 6 device pixels under scale(2).
    assert rows[-1] - rows[0] + 1 == 6
    # The 10/5 pattern is scaled to 20/10 device pixels.
    assert ink_runs(png, rows[len(rows) // 2], 5, 215)[:2] == [
        (10, 20), (40, 20)]


def test_percent_dash_array_renders():
    png = line('10%,10%')
    assert len(ink_runs(png, 30, 5, 215)) > 1


def test_empty_and_degenerate_paths_do_not_render_or_hang():
    for inner in (
            '<path d="" stroke="black" stroke-width="4" '
            'stroke-dasharray="5,2"/>',
            '<path d="M10 10" stroke="black" stroke-width="4" '
            'stroke-dasharray="5,2"/>',
            '<circle cx="100" cy="30" r="0" stroke="black" '
            'stroke-dasharray="5,2"/>'):
        png = render_png(inner)
        assert ink_runs(png, 30, 0, 220) == []


@pytest.mark.parametrize('dasharray', (
    '0', '0,0', '0 0 0', '-1,2', '2,-1', 'nan', 'inf', '1e999,0',
    '0,nan', '0,inf'))
def test_pathological_dash_arrays_terminate(dasharray):
    # None of these may hang the renderer or raise, however Cairo itself
    # would treat some of them. The call simply returns a valid PNG.
    def render():
        return render_png(
            f'<line x1="10" y1="30" x2="210" y2="30" stroke="black" '
            f'stroke-width="4" stroke-dasharray="{dasharray}"/>')

    if hasattr(signal, 'SIGALRM'):
        # Keep the failure local if rendering ever spins, instead of relying
        # on a session-level timeout.
        def alarm_handler(signum, frame):  # pragma: no cover - failure path
            raise AssertionError(f'rendering {dasharray!r} did not terminate')

        old_handler = signal.signal(signal.SIGALRM, alarm_handler)
        signal.alarm(5)
        try:
            png = render()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
    else:  # pragma: no cover - Windows has no SIGALRM
        png = render()
    assert png[:8] == b'\x89PNG\r\n\x1a\n'


def test_repeated_exports_are_deterministic():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="220" height="60">'
        '<line x1="10" y1="30" x2="210" y2="30" stroke="black" '
        'stroke-width="4" stroke-dasharray="10,4" '
        'stroke-dashoffset="-3"/></svg>').encode()
    assert svg2png(bytestring=svg) == svg2png(bytestring=svg)
