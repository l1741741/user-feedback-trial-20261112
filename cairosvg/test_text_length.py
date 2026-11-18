"""
Tests for ``textLength``/``lengthAdjust`` and letter/word spacing.

These tests render real PNGs and measure the ink (non-transparent pixels)
instead of trusting metric return values. Expected positions are derived
from font metrics measured with the same Cairo toy font backend and the
same font size used for drawing, so that font metrics and final drawing
are checked against each other. Nothing about a specific font's glyph
widths is hardcoded: sample texts are measured, never assumed.

Supported scope (see TEXT-LENGTH.md for details):

- ``textLength`` on ``text``/``tspan`` applies to the element's own text
  chunk; ``lengthAdjust="spacing"`` (default) only changes inter-letter
  spacing, ``lengthAdjust="spacingAndGlyphs"`` also scales the glyphs.
- Invalid (unparsable, negative, zero) lengths and empty content leave
  the text at its natural width.
- Letter and word spacing continue across sibling ``tspan`` boundaries.

Error sources, all exercised with tolerances below:

- Ink edges sit inside the advance-based target range by the first/last
  glyph's side bearings (scaled too when glyphs are scaled).
- Hinting rounds positions to device pixels; antialiasing spreads ink by
  roughly one pixel around the true edge.
- The toy font backend performs no kerning, so per-letter advances add up
  exactly to the whole-string advance.

"""

import io

import cairocffi as cairo
import pytest

from . import svg2png

FONT_FAMILY = 'sans-serif'  # always available through the toy font API
FONT_SIZE = 40
# Positions are rounded to device pixels and antialiasing spills by ~1px.
PIXEL_TOLERANCE = 2
# Side bearings of common glyphs stay well below 12% of the font size.
BEARING_TOLERANCE = FONT_SIZE * 0.12

SAMPLE_TEXTS = ['AB', 'HELLO', 'HELLO WORLD', 'ABC DEF GH']


def render(body, width=700, height=400):
    """Render ``body`` in an SVG document and return PNG bytes."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}">{body}</svg>')
    return svg2png(bytestring=svg.encode('utf-8'))


def text_element(content, *, x=100, y=60, extra=''):
    """A text element using the reference font and size."""
    return (
        f'<text x="{x}" y="{y}" font-size="{FONT_SIZE}" '
        f'font-family="{FONT_FAMILY}" {extra}>{content}</text>')


def ink_range(png, y0=0, y1=None):
    """Horizontal range of non-transparent pixels in rows ``[y0, y1)``."""
    surface = cairo.ImageSurface.create_from_png(io.BytesIO(png))
    width, height = surface.get_width(), surface.get_height()
    y1 = height if y1 is None else y1
    data = bytes(surface.get_data())
    columns = [
        x for y in range(y0, y1) for x in range(width)
        if data[y * width * 4 + x * 4 + 3] > 0]
    return (min(columns), max(columns)) if columns else None


def text_extents(text):
    """Cairo toy-font extents measured like CairoSVG measures them."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    context = cairo.Context(surface)
    context.select_font_face(FONT_FAMILY)
    context.set_font_size(FONT_SIZE)
    return context.text_extents(text)


def natural_advance(text, letter_spacing=0, word_spacing=0):
    """Advance of a text run, spacing included, from measured metrics."""
    return (
        sum(text_extents(char)[4] for char in text)
        + letter_spacing * (len(text) - 1)
        + word_spacing * text.count(' '))


def predicted_ink_range(x0, target, text, glyph_scale=1):
    """Ink range expected inside advance range ``[x0, x0 + target]``.

    The first and last glyphs keep their side bearings (scaled by
    ``glyph_scale`` when glyphs are scaled), so the ink sits strictly
    inside the advance-based target range.

    """
    first_bearing = text_extents(text[0])[0]
    last = text_extents(text[-1])
    last_right_bearing = last[4] - last[0] - last[2]
    return (
        x0 + first_bearing * glyph_scale,
        x0 + target - last_right_bearing * glyph_scale)


def assert_ink_matches(png, expected, y0=0, y1=100):
    """Check the measured ink range against the metric-based prediction."""
    measured = ink_range(png, y0, y1)
    assert measured is not None, 'no ink found, nothing was drawn'
    start, end = measured
    expected_start, expected_end = expected
    assert abs(start - expected_start) <= PIXEL_TOLERANCE, (
        f'ink starts at {start}, metrics predict {expected_start}')
    assert abs(end - expected_end) <= PIXEL_TOLERANCE, (
        f'ink ends at {end}, metrics predict {expected_end}')


@pytest.mark.parametrize('content', SAMPLE_TEXTS)
@pytest.mark.parametrize('adjust', ['spacing', 'spacingAndGlyphs'])
def test_short_and_long_texts_share_declared_box(content, adjust):
    """Short and long texts laid out in the same label box.

    Whatever their natural width (shorter or longer than the target),
    texts declaring the same ``textLength`` at the same ``x`` cover the
    same target range.

    """
    target = 300
    glyph_scale = (
        target / natural_advance(content)
        if adjust == 'spacingAndGlyphs' else 1)
    png = render(text_element(
        content, extra=f'textLength="{target}" lengthAdjust="{adjust}"'))
    assert_ink_matches(
        png, predicted_ink_range(100, target, content, glyph_scale))


def test_short_and_long_texts_in_one_document():
    """A short and a long label share the same box in a single render."""
    target = 300
    short, long_ = 'AB', 'A MUCH LONGER LABEL'
    png = render(
        text_element(short, y=60, extra=f'textLength="{target}"') +
        text_element(long_, y=160, extra=f'textLength="{target}"'))
    assert_ink_matches(png, predicted_ink_range(100, target, short), 0, 100)
    assert_ink_matches(
        png, predicted_ink_range(100, target, long_), 100, 200)


@pytest.mark.parametrize('anchor,ratio', [
    ('start', 0), ('middle', 0.5), ('end', 1)])
@pytest.mark.parametrize('adjust', ['spacing', 'spacingAndGlyphs'])
def test_text_anchor_combinations(anchor, ratio, adjust):
    """Anchors align the declared target range, not the natural width."""
    target = 300
    x = 350  # centered so that end-anchored text stays on the canvas
    content = 'HELLO WORLD'
    glyph_scale = (
        target / natural_advance(content)
        if adjust == 'spacingAndGlyphs' else 1)
    png = render(text_element(
        content, x=x,
        extra=(f'text-anchor="{anchor}" textLength="{target}" '
               f'lengthAdjust="{adjust}"')))
    assert_ink_matches(
        png,
        predicted_ink_range(x - target * ratio, target, content, glyph_scale),
        0, 100)


@pytest.mark.parametrize('adjust', ['spacing', 'spacingAndGlyphs'])
def test_single_character(adjust):
    """A single character has no gap: spacing cannot adjust it, glyphs can."""
    target = 120
    content = 'W'
    natural = render(text_element(content))
    stretched = render(text_element(
        content,
        extra=f'textLength="{target}" lengthAdjust="{adjust}"'))
    if adjust == 'spacing':
        # No inter-letter gap to adjust: the rendering is unchanged.
        assert stretched == natural
    else:
        # The glyph itself is scaled to reach the declared length.
        glyph_scale = target / natural_advance(content)
        assert_ink_matches(
            stretched, predicted_ink_range(100, target, content, glyph_scale))
        assert stretched != natural


def test_empty_content_is_stable():
    """Empty content with a declared length draws nothing and stays put."""
    empty = render(
        text_element('', extra='textLength="300"') +
        text_element('', x=50, y=160, extra='textLength="300"'))
    assert ink_range(empty) is None
    # Same as a document without any text element.
    assert empty == render('')


def test_letter_spacing_continues_across_tspans():
    """Spacing must not break at a tspan boundary."""
    one_chunk = render(text_element('ABCD', extra='letter-spacing="5"'))
    chunked = render(text_element('AB<tspan>CD</tspan>',
                                  extra='letter-spacing="5"'))
    assert chunked == one_chunk
    deeper = render(text_element(
        'A<tspan>B<tspan>C</tspan>D</tspan>', extra='letter-spacing="5"'))
    assert deeper == one_chunk


def test_nested_tspan_with_own_text_length():
    """A tspan carries its own textLength, applied to its own chunk."""
    target = 200
    content = 'AB'
    png = render(text_element(
        f'<tspan textLength="{target}">{content}</tspan>'))
    assert_ink_matches(png, predicted_ink_range(100, target, content))


def test_explicit_position_lists_are_stable():
    """Explicit x/dx lists combined with textLength give stable results."""
    body = (
        f'<text x="100 250 400" y="60" font-size="{FONT_SIZE}" '
        f'font-family="{FONT_FAMILY}" dx="0 5 0" textLength="200">ABC</text>')
    first = render(body)
    assert first == render(body)  # repeated calls give identical output
    start, _ = ink_range(first, 0, 100)
    # The first letter keeps its declared position.
    expected_start = 100 + text_extents('A')[0]
    assert abs(start - expected_start) <= PIXEL_TOLERANCE


@pytest.mark.parametrize('invalid', ['bogus', '10emx', '-50', '0', ''])
def test_invalid_text_length_is_ignored(invalid):
    """Unparsable, negative or zero lengths keep the natural layout."""
    natural = render(text_element('HELLO'))
    for adjust in ('spacing', 'spacingAndGlyphs'):
        assert render(text_element(
            'HELLO',
            extra=f'textLength="{invalid}" lengthAdjust="{adjust}"')) == natural


def test_repeated_calls_are_deterministic():
    """Two identical labels in one document reach identical ranges."""
    body = (
        text_element('HELLO WORLD', y=60, extra='textLength="300"') +
        text_element('HELLO WORLD', y=160, extra='textLength="300"'))
    png = render(body)
    assert png == render(body)
    assert ink_range(png, 0, 100) == ink_range(png, 100, 200)


@pytest.mark.parametrize('adjust', ['spacing', 'spacingAndGlyphs'])
def test_following_text_inherits_nothing(adjust):
    """Text after an adjusted label keeps its own scale and spacing."""
    following = text_element('HELLO', y=160)
    label = text_element(
        'LABEL', y=60,
        extra=f'textLength="350" lengthAdjust="{adjust}"')
    combined = render(label + following)
    solo = render(following)
    # The band holding the second line is pixel-identical to a solo render.
    surface_a = cairo.ImageSurface.create_from_png(io.BytesIO(combined))
    surface_b = cairo.ImageSurface.create_from_png(io.BytesIO(solo))
    width = surface_a.get_width()
    band_a = bytes(surface_a.get_data())[100 * width * 4:200 * width * 4]
    band_b = bytes(surface_b.get_data())[100 * width * 4:200 * width * 4]
    assert band_a == band_b


def test_metrics_match_drawing():
    """Natural layout: PNG ink edges match measured font metrics."""
    content = 'HELLO WORLD'
    png = render(text_element(content))
    natural = natural_advance(content)
    assert_ink_matches(png, predicted_ink_range(100, natural, content))


def test_letter_and_word_spacing_advance():
    """Declared letter/word spacing shifts the end of the run accordingly."""
    content = 'AB CD'
    letter_spacing, word_spacing = 6, 20
    png = render(text_element(
        content,
        extra=(f'letter-spacing="{letter_spacing}" '
               f'word-spacing="{word_spacing}"')))
    natural = natural_advance(content, letter_spacing, word_spacing)
    assert_ink_matches(png, predicted_ink_range(100, natural, content))


def test_text_length_with_letter_spacing():
    """textLength wins over letter-spacing: the run hits the target."""
    target = 300
    content = 'HELLO'
    letter_spacing = 6
    png = render(text_element(
        content,
        extra=f'textLength="{target}" letter-spacing="{letter_spacing}"'))
    assert_ink_matches(png, predicted_ink_range(100, target, content))
