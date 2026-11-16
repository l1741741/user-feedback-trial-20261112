"""
Pixel-level regression tests for ``stroke-dasharray`` and
``stroke-dashoffset``.

The fixtures draw one-pixel-wide horizontal or vertical reference lines with
integer coordinates and a butt cap. Every sampled pixel center is then either
fully inside or fully outside a dash, so the run lengths read back from the
rendered PNG are exact and do not depend on antialiasing or on the Cairo
version. A run is reported as ``(painted, length)`` where ``painted`` is
``True`` for ink.

The expected values in this file are the SVG rules written out by hand:
percentage lengths resolve against the normalized viewport diagonal, an odd
value count is repeated to give a full dash/gap period, and the dash phase is
``dashoffset`` modulo the pattern length (with negative offsets counted
through the list as written, see ``helpers.DashArray.phase``).

"""

import io

import pytest
from PIL import Image

from . import svg2png
from .helpers import DashArray, InvalidDashValue, dash_array, dash_offset


def render(source, **options):
    """Render ``source`` and return a Pillow image in L mode."""
    png = svg2png(source.encode() if isinstance(source, str) else source,
                  **options)
    return Image.open(io.BytesIO(png)).convert('L')


def document(body, width=220, height=220):
    """Wrap ``body`` in a white SVG document."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}">'
        f'<rect width="{width}" height="{height}" fill="white"/>'
        f'{body}</svg>')


def runs(sequence):
    """Return ``[(value, length), ...]`` for a sequence of booleans."""
    result = []
    current = sequence[0]
    count = 0
    for value in sequence:
        if value == current:
            count += 1
        else:
            result.append((current, count))
            current, count = value, 1
    result.append((current, count))
    return result


def row_runs(image, y, x0, x1, threshold=128):
    """Return the ink runs of the row ``y`` between ``x0`` and ``x1``."""
    pixels = image.load()
    return runs([pixels[x, y] < threshold for x in range(x0, x1)])


def column_runs(image, x, y0, y1, threshold=128):
    """Return the ink runs of the column ``x`` between ``y0`` and ``y1``."""
    pixels = image.load()
    return runs([pixels[x, y] < threshold for y in range(y0, y1)])


def horizontal(dasharray, offset=None, width=220, stroke_width=1):
    """Draw a horizontal line from x=0 to x=219 on y=20."""
    offset = '' if offset is None else f' stroke-dashoffset="{offset}"'
    return document(
        f'<line x1="0" y1="20" x2="{width - 1}" y2="20" stroke="black" '
        f'stroke-width="{stroke_width}" stroke-dasharray="{dasharray}"'
        f'{offset}/>')


def test_even_dasharray():
    """A plain list paints dash/gap pairs from the start of the path."""
    image = render(horizontal('10 5'))
    assert row_runs(image, 20, 0, 60) == [
        (True, 10), (False, 5), (True, 10), (False, 5), (True, 10),
        (False, 5), (True, 10), (False, 5)]


def test_comma_and_whitespace_separators():
    """Commas, spaces and whitespace runs are interchangeable separators."""
    expected = row_runs(render(horizontal('10 5')), 20, 0, 60)
    for dasharray in ('10,5', '10 , 5', '10,\t5', ' 10 ,  5 '):
        assert row_runs(render(horizontal(dasharray)), 20, 0, 60) == expected


def test_odd_dasharray_is_repeated():
    """An odd count is repeated once: 5 3 2 behaves as 5 3 2 5 3 2."""
    odd = row_runs(render(horizontal('5 3 2')), 20, 0, 80)
    doubled = row_runs(render(horizontal('5 3 2 5 3 2')), 20, 0, 80)
    assert odd == doubled
    assert odd[:12] == [
        (True, 5), (False, 3), (True, 2), (False, 5), (True, 3), (False, 2),
        (True, 5), (False, 3), (True, 2), (False, 5), (True, 3), (False, 2)]


def test_single_value_alternates():
    """A one-value list is a dash and a gap of the same length."""
    assert row_runs(render(horizontal('7')), 20, 0, 42) == [
        (True, 7), (False, 7), (True, 7), (False, 7), (True, 7), (False, 7)]


def test_positive_offset_shifts_the_pattern():
    """A positive offset cuts the first dash by that distance."""
    assert row_runs(render(horizontal('10 5', offset='3')), 20, 0, 45) == [
        (True, 7), (False, 5), (True, 10), (False, 5), (True, 10),
        (False, 5), (True, 3)]


def test_offset_larger_than_period_wraps():
    """An offset is a position in the repeated pattern, not a skip count."""
    wrapped = row_runs(render(horizontal('10 5', offset='52')), 20, 0, 60)
    direct = row_runs(render(horizontal('10 5', offset='7')), 20, 0, 60)
    assert wrapped == direct


def test_negative_offset_moves_backwards():
    """A negative offset starts the pattern before the path start."""
    # Phase -1 of 10 5 is 14, which is 4 units into the 5-unit gap.
    assert row_runs(render(horizontal('10 5', offset='-1')), 20, 0, 45) == [
        (False, 1), (True, 10), (False, 5), (True, 10), (False, 5),
        (True, 10), (False, 4)]


def test_negative_offset_on_odd_list_uses_written_length():
    """Negative offsets are counted through the list as written.

    The written list is 5 3 2, so its length is 10 even though the painted
    pattern repeats over 20 units. A negative offset is a distance back from
    the path start, so -20 lands a whole written period back: the phase is 10
    (the start of the gap) and not 0 (the start of the first dash).
    """
    assert row_runs(render(horizontal('5 3 2', offset='-20')), 20, 0, 40) == [
        (False, 5), (True, 3), (False, 2), (True, 5), (False, 3), (True, 2),
        (False, 5), (True, 3), (False, 2), (True, 5), (False, 3), (True, 2)]

    # -1 is one unit back from the start, i.e. 9 units in: the last unit of
    # the third (dash) value, followed by the gap of the repeated list.
    assert row_runs(render(horizontal('5 3 2', offset='-1')), 20, 0, 24) == [
        (True, 1), (False, 5), (True, 3), (False, 2), (True, 5), (False, 3),
        (True, 2), (False, 3)]


def test_odd_list_positive_offset_matches_doubled_list():
    """Positive offsets on an odd list are offsets into the doubled list."""
    odd = row_runs(render(horizontal('5 3 2', offset='11')), 20, 0, 60)
    doubled = row_runs(
        render(horizontal('5 3 2 5 3 2', offset='11')), 20, 0, 60)
    assert odd == doubled


def test_zero_length_item():
    """A zero entry paints nothing but still advances the pattern."""
    # 4 0 0 is an even list: 4 dash, 0 gap, 0 dash repeated.
    assert row_runs(render(horizontal('4 0 0')), 20, 0, 24) == [
        (True, 4), (False, 4), (True, 4), (False, 4), (True, 4), (False, 4)]

    # 0 4 0 starts with a zero dash, so the path starts with a gap.
    assert row_runs(render(horizontal('0 4 0')), 20, 0, 24) == [
        (False, 4), (True, 4), (False, 4), (True, 4), (False, 4), (True, 4)]


def test_zero_length_dash_with_round_cap():
    """Zero-length dashes are dots when the cap paints outside the dash."""
    solid = render(horizontal('0 5', stroke_width=4))
    assert (True, 5) not in row_runs(solid, 20, 0, 60)

    dotted = render(
        document(
            '<line x1="0" y1="20" x2="59" y2="20" stroke="black" '
            'stroke-width="4" stroke-linecap="round" '
            'stroke-dasharray="0 5"/>'))
    painted = [value for value, _ in row_runs(dotted, 20, 0, 60)]
    assert painted.count(True) == 12
    assert painted.count(False) > 0


@pytest.mark.parametrize('dasharray', ('0', '0 0', '0.0 0.0', 'none', ''))
def test_no_dashing_values(dasharray):
    """Empty, ``none`` and all-zero lists paint a solid stroke."""
    assert row_runs(render(horizontal(dasharray)), 20, 0, 40) == [(True, 40)]


@pytest.mark.parametrize('dasharray', (
    '4 -2', '-4 2', 'nan 2', 'inf 2', '1e400 2', '10 foo 5', '4,,2', '4,',
    '1e1e1 2', '4 2;'))
def test_invalid_dasharray_draws_solid(dasharray):
    """Invalid lists are ignored instead of reaching Cairo."""
    assert row_runs(render(horizontal(dasharray)), 20, 0, 40) == [(True, 40)]


def test_invalid_dasharray_does_not_stop_rendering():
    """An invalid list must not poison the Cairo context."""
    source = document(
        '<line x1="0" y1="20" x2="219" y2="20" stroke="black" '
        'stroke-width="1" stroke-dasharray="4 -2"/>'
        '<line x1="0" y1="40" x2="219" y2="40" stroke="black" '
        'stroke-width="1"/>')
    image = render(source)
    assert row_runs(image, 20, 0, 30) == [(True, 30)]
    assert row_runs(image, 40, 0, 30) == [(True, 30)]


def test_solid_shape_does_not_inherit_previous_dashes():
    """Cairo keeps its dash state, so every node has to set it explicitly."""
    source = document(
        '<line x1="0" y1="20" x2="219" y2="20" stroke="black" '
        'stroke-width="1" stroke-dasharray="6 4"/>'
        '<line x1="0" y1="40" x2="219" y2="40" stroke="black" '
        'stroke-width="1"/>')
    image = render(source)
    assert row_runs(image, 20, 0, 20)[0] == (True, 6)
    assert row_runs(image, 40, 0, 30) == [(True, 30)]


def test_group_dasharray_is_inherited():
    """A dash array on a group applies to its children."""
    source = document(
        '<g stroke="black" stroke-width="1" stroke-dasharray="10 5">'
        '<line x1="0" y1="20" x2="219" y2="20"/></g>')
    assert row_runs(render(source), 20, 0, 30) == [
        (True, 10), (False, 5), (True, 10), (False, 5)]


@pytest.mark.parametrize('override', ('none', '0', '0 0'))
def test_override_to_solid_does_not_leak_previous_dashes(override):
    """A child can turn dashing off again, in the same document."""
    source = document(
        '<g stroke="black" stroke-width="1" stroke-dasharray="10 5">'
        f'<line x1="0" y1="20" x2="219" y2="20" '
        f'stroke-dasharray="{override}"/>'
        '</g>'
        '<line x1="0" y1="40" x2="219" y2="40" stroke="black" '
        'stroke-width="1"/>')
    image = render(source)
    assert row_runs(image, 20, 0, 30) == [(True, 30)]
    assert row_runs(image, 40, 0, 30) == [(True, 30)]


@pytest.mark.parametrize('override', ('10 -5', '4,,2', '', 'nan 5'))
def test_invalid_override_uses_inherited_value(override):
    """Invalid values are ignored, so the inherited pattern still applies."""
    source = document(
        '<g stroke="black" stroke-width="1" stroke-dasharray="10 5">'
        f'<line x1="0" y1="20" x2="219" y2="20" '
        f'stroke-dasharray="{override}"/>'
        '</g>')
    assert row_runs(render(source), 20, 0, 30) == [
        (True, 10), (False, 5), (True, 10), (False, 5)]


def test_invalid_dashoffset_uses_zero():
    """An unparsable offset is ignored rather than passed to Cairo."""
    assert row_runs(render(horizontal('10 5', offset='foo')), 20, 0, 30) == [
        (True, 10), (False, 5), (True, 10), (False, 5)]


@pytest.mark.parametrize('offset', (
    '1e300', '-1e300', '1e400', '-1e400', 'nan', 'inf', '-inf'))
def test_extreme_offsets_are_finite(offset):
    """Huge and non-finite offsets must not raise or produce NaN phases."""
    assert row_runs(render(horizontal('4 2', offset=offset)), 20, 0, 12) == [
        (True, 4), (False, 2), (True, 4), (False, 2)]


def test_percentage_lengths():
    """Percentages use the normalized viewport diagonal."""
    # 220x220 viewport: hypot(220, 220) / sqrt(2) == 220, so 10% is 22.
    assert row_runs(render(horizontal('10% 5%')), 20, 0, 66) == [
        (True, 22), (False, 11), (True, 22), (False, 11)]


def test_percentage_offset():
    """stroke-dashoffset percentages use the same reference length."""
    # 10% of 220 is 22; 22 modulo the 15-unit period is 7 into the first dash.
    assert row_runs(render(horizontal('10 5', offset='10%')), 20, 0, 33) == [
        (True, 3), (False, 5), (True, 10), (False, 5), (True, 10)]


def test_scientific_notation():
    """Exponents are numbers, in either case, with or without a sign."""
    expected = row_runs(render(horizontal('10 5')), 20, 0, 45)
    for dasharray in ('1e1 5e0', '1E1,5E0', '1e+1 5e-0'):
        assert row_runs(render(horizontal(dasharray)), 20, 0, 45) == expected


def test_units():
    """Explicit ``px`` lengths are user units; ``%`` is not ``px``."""
    assert row_runs(render(horizontal('10px 5px')), 20, 0, 45) == \
        row_runs(render(horizontal('10 5')), 20, 0, 45)


def test_output_scaling_keeps_user_units():
    """Dash lengths are user units, so they scale with the output size."""
    image = render(horizontal('10 5'), scale=2)
    assert row_runs(image, 40, 0, 120) == [
        (True, 20), (False, 10), (True, 20), (False, 10), (True, 20),
        (False, 10), (True, 20), (False, 10)]


def test_corner_keeps_the_phase():
    """The pattern continues around a corner instead of restarting."""
    source = document(
        '<polyline points="20,20 60,20 60,60" fill="none" stroke="black" '
        'stroke-width="1" stroke-dasharray="10 5"/>')
    image = render(source)
    # Corner at arc length 40, which is 10 into the 15-unit period: gap start.
    assert column_runs(image, 60, 20, 60)[:4] == [
        (False, 5), (True, 10), (False, 5), (True, 10)]


def test_closed_path_is_dashed_around_the_join():
    """A closed path is one continuous run of dashes."""
    source = document(
        '<path d="M20 20 H60 V60 H20 Z" fill="none" stroke="black" '
        'stroke-width="1" stroke-dasharray="10 5"/>')
    image = render(source)
    assert row_runs(image, 20, 20, 60)[:5] == [
        (True, 10), (False, 5), (True, 10), (False, 5), (True, 10)]
    assert column_runs(image, 60, 20, 60)[:4] == [
        (False, 5), (True, 10), (False, 5), (True, 10)]
    # The bottom edge runs right-to-left, so its phase differs from the top.
    assert row_runs(image, 60, 20, 60)[:4] == [
        (False, 5), (True, 10), (False, 5), (True, 10)]
    assert column_runs(image, 20, 20, 60)[:4] == [
        (True, 10), (False, 5), (True, 10), (False, 5)]


def test_each_subpath_restarts_the_pattern():
    """Every subpath starts its own dash sequence with the same phase."""
    source = document(
        '<path d="M0 20 H100 M0 40 H100" fill="none" stroke="black" '
        'stroke-width="1" stroke-dasharray="10 5" stroke-dashoffset="3"/>')
    image = render(source)
    assert row_runs(image, 20, 0, 45) == row_runs(image, 40, 0, 45)
    assert row_runs(image, 20, 0, 30)[:3] == [
        (True, 7), (False, 5), (True, 10)]


def test_empty_path_paints_nothing_and_leaves_no_dashes():
    """An empty path must neither paint nor change the next shape's dashes."""
    source = document(
        '<path d="" fill="none" stroke="black" stroke-width="1" '
        'stroke-dasharray="10 5"/>'
        '<line x1="0" y1="40" x2="219" y2="40" stroke="black" '
        'stroke-width="1"/>')
    image = render(source)
    assert row_runs(image, 20, 0, 60) == [(False, 60)]
    assert row_runs(image, 40, 0, 30) == [(True, 30)]


def test_zero_length_subpath_with_round_cap():
    """A zero-length subpath paints a dot when the cap allows it."""
    source = document(
        '<path d="M20 20 L20 20" fill="none" stroke="black" stroke-width="8" '
        'stroke-linecap="round" stroke-dasharray="5 5"/>')
    assert (True, 8) in row_runs(render(source), 20, 0, 40)


def test_repeated_export_is_identical():
    """Rendering the same document twice gives byte-identical PNGs."""
    source = horizontal('5 3 2', offset='-7').encode()
    outputs = {svg2png(source) for _ in range(3)}
    assert len(outputs) == 1


@pytest.mark.parametrize(('value', 'expected'), (
    ('none', None),
    ('None', None),
    ('0', None),
    ('0 0', None),
    ('4 2', [4.0, 2.0]),
    ('5 3 2', [5.0, 3.0, 2.0, 5.0, 3.0, 2.0]),
    ('1e1 5e0', [10.0, 5.0]),
    ('.5 2', [0.5, 2.0]),
    ('4 0 0', [4.0, 0.0, 0.0, 4.0, 0.0, 0.0]),
    ('1.2.3 4', [1.2, 0.3, 4.0, 1.2, 0.3, 4.0]),
))
def test_parse_dasharray(value, expected):
    """Parse valid lists into an even list of user lengths."""
    parsed = dash_array(None, value)
    if expected is None:
        assert parsed is None
    else:
        assert parsed.values == expected


@pytest.mark.parametrize('value', (
    '', '  ', '4 -2', '-4 2', 'nan 2', 'inf 2', '1e400 2', '10 foo 5',
    '4,,2', '4,', ',4', '10 5,', '1e1e1 2', '4 2;', '4 2 3 4q'))
def test_parse_dasharray_invalid(value):
    """Invalid lists raise instead of being half-understood."""
    with pytest.raises(InvalidDashValue):
        dash_array(None, value)


class _Surface:
    """Minimal surface stub for the size helper."""

    dpi = 96
    font_size = 12
    context_width = 220
    context_height = 220


def test_parse_dashoffset():
    """Offsets are single lengths; invalid ones raise."""
    assert dash_offset(None, None) is None
    assert dash_offset(None, '') is None
    assert dash_offset(None, '-7') == -7
    # ``px`` is user units, but there is no surface here to resolve them.
    assert dash_offset(None, '2px') == 0
    assert dash_offset(_Surface(), '2px') == 2
    for value in ('foo', 'nan', '1e400', '2 3', '4,'):
        with pytest.raises(InvalidDashValue):
            dash_offset(None, value)


def test_phase_rule():
    """DashArray.phase is the rule the pixel tests above rely on."""
    pattern = DashArray([5.0, 3.0, 2.0, 5.0, 3.0, 2.0], 10.0)
    assert pattern.phase(0) == 0
    assert pattern.phase(11) == 11
    assert pattern.phase(-1) == 9
    assert pattern.phase(-10) == 10
    assert pattern.phase(-20) == 10
    assert pattern.phase(-30) == 10
    even = DashArray([10.0, 5.0], 15.0)
    assert even.phase(-1) == 14
    assert even.phase(-15) == 0
