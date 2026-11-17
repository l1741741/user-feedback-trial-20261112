"""
Tests for ``textLength`` and ``letter-spacing`` layout.

The tests use a font that is actually available on the machine (the first
of a small list that Cairo can resolve) and measure the rendered PNG, not
only the layout return values.  They also compare against the natural
layout computed from the same font, so no expected pixel position is
hard-coded to one particular font build.

"""

import io

import cairocffi as cairo
import pytest
from PIL import Image

import cairosvg
from . import parser, surface
from .text_layout import build_layout

FONT_CANDIDATES = (
    'DejaVu Sans',
    'Liberation Sans',
    'Verdana',
    'Arial',
    'Helvetica',
    'sans-serif',
)


def find_font():
    """Return the first candidate font family Cairo can actually use.

    Cairo's toy API silently substitutes a default face for unknown
    families, so we compare the metrics of a probe string against a
    deliberately bogus family: a different advance means the family is
    really installed.

    """
    probe_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 8)
    context = cairo.Context(probe_surface)
    context.set_font_size(40)
    context.select_font_face(
        'CairoSVG Test Font That Does Not Exist',
        cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    fallback = context.text_extents('Hamburgefonstiv')
    for family in FONT_CANDIDATES:
        context.select_font_face(
            family, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        if context.text_extents('Hamburgefonstiv') != fallback:
            return family
    return 'sans-serif'


FONT = find_font()
FONT_ATTRIBUTES = f'font-family="{FONT}" font-size="30"'
WIDTH = 700
HEIGHT = 200


def render(body, width=WIDTH, height=HEIGHT):
    """Render an SVG body (without the ``svg`` wrapper) to a PIL image."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}">{body}</svg>')
    png = cairosvg.svg2png(bytestring=svg.encode())
    return Image.open(io.BytesIO(png)).convert('RGBA')


def ink_box(image, threshold=8):
    """Return the ``(left, top, right, bottom)`` box of painted pixels."""
    alpha = image.getchannel('A').point(
        lambda value: 255 if value > threshold else 0)
    return alpha.getbbox()


def column_runs(image, threshold=8, gap=1):
    """Return painted horizontal runs, one per glyph cluster.

    A run is split whenever there is a gap wider than ``gap`` pixels, which
    is enough to separate the letters of the test strings.

    """
    alpha = image.getchannel('A').load()
    width, height = image.size
    columns = []
    for x in range(width):
        for y in range(height):
            if alpha[x, y] > threshold:
                columns.append(x)
                break
    if not columns:
        return []
    runs = []
    start = previous = columns[0]
    for x in columns[1:]:
        if x > previous + gap:
            runs.append((start, previous))
            start = x
        previous = x
    runs.append((start, previous))
    return runs


def metrics(text, font_size=30):
    """Return per-character advances and the first/last ink offsets."""
    probe = cairo.ImageSurface(cairo.FORMAT_ARGB32, 8, 8)
    context = cairo.Context(probe)
    context.select_font_face(
        FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    context.set_font_size(font_size)
    advances = [context.text_extents(char)[4] for char in text]
    first = context.text_extents(text[0])
    last = context.text_extents(text[-1])
    return {
        'advances': advances,
        'natural': sum(advances),
        'left_bearing': first[0],
        'last_trailing': last[4] - (last[0] + last[2]),
    }


def layout_root(body, font_size=30):
    """Return ``(layouts, node)`` for the first ``text`` of ``body``."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}">{body}</svg>')
    tree = parser.Tree(bytestring=svg.encode())
    png_surface = surface.PNGSurface(tree, None, 96)
    node = next(child for child in tree.children if child.tag == 'text')
    png_surface.font_size = font_size
    return build_layout(png_surface, node), node


def layout_for(body, font_size=30):
    """Return ``(characters, node)`` for the first ``text`` of ``body``."""
    layouts, node = layout_root(body, font_size)
    return layouts[id(node)], node


def run_layout_for(body, font_size=30):
    """Return ``(characters, node)`` for the whole text run of ``body``."""
    layouts, node = layout_root(body, font_size)

    def walk(current):
        ordered = list(layouts[id(current)])
        for child in current.children:
            if child.tag in ('tspan', 'a'):
                ordered.extend(walk(child))
        return ordered

    return walk(node), node


def assert_close(actual, expected, tolerance=1.5):
    assert abs(actual - expected) <= tolerance, (
        f'{actual:.3f} != {expected:.3f} (±{tolerance})')


@pytest.mark.parametrize('text', ['Hi', 'Hello there'])
def test_text_length_is_reached(text):
    """The advance box of a calibrated run matches ``textLength``."""
    target = 360
    characters, _ = layout_for(
        f'<text x="40" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{target}">{text}</text>')
    start = characters[0].x
    end = characters[-1].end
    assert_close(end - start, target, tolerance=1e-6)


@pytest.mark.parametrize('text', ['Hi', 'Hello there'])
def test_text_length_measured_on_png(text):
    """The PNG ink box of a calibrated run matches its declared width.

    The ink box is narrower than the advance box by the first glyph's left
    side bearing and the last glyph's right side bearing; that difference is
    measured from the same font, not hard-coded.

    """
    target, x_position = 340, 40
    info = metrics(text)
    image = render(
        f'<text x="{x_position}" y="120" {FONT_ATTRIBUTES} '
        f'textLength="{target}">{text}</text>')
    box = ink_box(image)
    assert box is not None
    assert_close(box[0], x_position + info['left_bearing'], tolerance=2)
    assert_close(
        box[2], x_position + target - info['last_trailing'], tolerance=2)


def test_short_and_long_text_same_target():
    """Short and long labels share the same start and end positions."""
    target, x_position = 420, 30
    short, long = 'Hi', 'Hello world, this is much longer'
    short_info, long_info = metrics(short), metrics(long)
    assert long_info['natural'] > target > short_info['natural']

    short_image = render(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{target}">{short}</text>')
    long_image = render(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{target}">{long}</text>')
    short_box, long_box = ink_box(short_image), ink_box(long_image)

    # The last glyphs differ ('i' vs 'r'), so compare against each font
    # metric rather than assuming identical right bearings.
    assert_close(short_box[0], long_box[0], tolerance=1)
    assert_close(
        short_box[2],
        x_position + target - short_info['last_trailing'], tolerance=2)
    assert_close(
        long_box[2],
        x_position + target - long_info['last_trailing'], tolerance=2)


def test_long_text_is_compressed():
    """A label longer than ``textLength`` really gets narrower."""
    text, target = 'Hello world', 100
    natural = metrics(text)['natural']
    assert natural > target + 20

    default_image = render(
        f'<text x="30" y="90" {FONT_ATTRIBUTES}>{text}</text>')
    calibrated_image = render(
        f'<text x="30" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{target}">{text}</text>')
    default_box, calibrated_box = (
        ink_box(default_image), ink_box(calibrated_image))
    default_width = default_box[2] - default_box[0]
    calibrated_width = calibrated_box[2] - calibrated_box[0]
    # The target is 100; ink is a little narrower than the advance box.
    assert calibrated_width < target
    assert calibrated_width < default_width * 0.6


def test_spacing_only_moves_gaps_not_glyphs():
    """``lengthAdjust="spacing"`` stretches gaps but keeps glyph widths."""
    text, target = 'Him', 300
    default_runs = column_runs(render(
        f'<text x="30" y="90" {FONT_ATTRIBUTES}>{text}</text>'))
    stretched_runs = column_runs(render(
        f'<text x="30" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{target}">{text}</text>'))
    assert len(default_runs) == len(stretched_runs) == 3

    # The run really reaches the requested width...
    info = metrics(text)
    assert_close(
        stretched_runs[-1][1], 30 + target - info['last_trailing'],
        tolerance=2)
    # ...but the glyphs keep their natural ink width.
    for default, stretched in zip(default_runs, stretched_runs):
        default_width = default[1] - default[0]
        stretched_width = stretched[1] - stretched[0]
        assert_close(stretched_width, default_width, tolerance=1)


def test_spacing_and_glyphs_scales_glyphs():
    """``lengthAdjust="spacingAndGlyphs"`` widens the glyphs themselves."""
    text, target = 'Him', 300
    default_runs = column_runs(render(
        f'<text x="30" y="90" {FONT_ATTRIBUTES}>{text}</text>'))
    scaled_runs = column_runs(render(
        f'<text x="30" y="90" {FONT_ATTRIBUTES} textLength="{target}" '
        f'lengthAdjust="spacingAndGlyphs">{text}</text>'))
    assert len(default_runs) == len(scaled_runs) == 3

    natural = metrics(text)['natural']
    expected_scale = target / natural
    for default, scaled in zip(default_runs, scaled_runs):
        default_width = default[1] - default[0]
        scaled_width = scaled[1] - scaled[0]
        assert_close(
            scaled_width, default_width * expected_scale, tolerance=2)


def test_single_character_spacing_keeps_natural_width():
    """``spacing`` cannot stretch a single character; no gap exists."""
    text, target = 'W', 400
    default_image = render(
        f'<text x="30" y="90" {FONT_ATTRIBUTES}>{text}</text>')
    calibrated_image = render(
        f'<text x="30" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{target}">{text}</text>')
    assert ink_box(calibrated_image) == ink_box(default_image)


def test_single_character_glyphs_scales():
    """``spacingAndGlyphs`` does scale a one-character label."""
    text, target, x_position = 'W', 300, 30
    info = metrics(text)
    image = render(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{target}" lengthAdjust="spacingAndGlyphs">{text}</text>')
    box = ink_box(image)
    scale = target / info['natural']
    assert_close(box[0], x_position + info['left_bearing'] * scale,
                 tolerance=2)
    assert_close(
        box[2], x_position + target - info['last_trailing'] * scale,
        tolerance=2)


@pytest.mark.parametrize('anchor, factor', [
    ('start', 0), ('middle', 0.5), ('end', 1)])
def test_anchors_use_calibrated_width(anchor, factor):
    """``text-anchor`` is applied to the calibrated, not natural, width."""
    text, target, x_position = 'Hi there', 320, 350
    info = metrics(text)
    image = render(
        f'<text x="{x_position}" y="120" text-anchor="{anchor}" '
        f'{FONT_ATTRIBUTES} textLength="{target}">{text}</text>')
    box = ink_box(image)
    shift = target * factor
    assert_close(
        box[0],
        x_position - shift + info['left_bearing'],
        tolerance=2)
    assert_close(
        box[2],
        x_position - shift + target - info['last_trailing'],
        tolerance=2)


def test_nested_tspan_keeps_calibration_continuous():
    """Characters split across ``tspan`` share one calibrated run."""
    target, x_position = 360, 30
    text = 'abcdef'
    info = metrics(text)
    bound = render(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{target}">{text}</text>')
    split = render(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{target}">ab<tspan>cd</tspan>ef</text>')
    assert ink_box(split) == ink_box(bound)
    assert_close(
        ink_box(split)[2],
        x_position + target - info['last_trailing'], tolerance=2)


def test_nested_text_length_keeps_its_own_width():
    """A nested ``textLength`` block is exactly as wide as requested."""
    inner_target, outer_target = 100, 420
    body = (
        f'<text x="20" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{outer_target}">ab<tspan textLength="{inner_target}">'
        f'cd</tspan>ef</text>')
    layouts, node = layout_root(body)
    inner = next(child for child in node.children if child.tag == 'tspan')
    block = layouts[id(inner)]
    assert_close(block[-1].end - block[0].x, inner_target, tolerance=1e-6)

    # The outer run is calibrated too, so it still ends at its own target.
    ordered, _ = run_layout_for(body)
    assert_close(
        ordered[-1].end - ordered[0].x, outer_target, tolerance=1e-6)


def test_nested_text_length_block_moves_as_one():
    """An outer calibration shifts a calibrated inner block rigidly."""
    outer, inner = 420, 100
    layouts, node = layout_root(
        f'<text x="20" y="90" {FONT_ATTRIBUTES} textLength="{outer}">'
        f'ab<tspan textLength="{inner}">cde</tspan>f</text>')
    tspan = next(child for child in node.children if child.tag == 'tspan')
    block = layouts[id(tspan)]
    # All characters of the block move by the same amount, so the block's
    # internal spacing is untouched.
    gaps = [following.x - previous.x
            for previous, following in zip(block, block[1:])]
    natural_gaps = metrics('cde')['advances'][:-1]
    assert_close(gaps[0], natural_gaps[0] + (inner - metrics('cde')['natural'])
                 / 2, tolerance=1e-6)


def test_letter_spacing_crosses_tspan_boundary():
    """Letter spacing is continuous across child elements."""
    spacing, x_position = 12, 30
    text = 'abcd'
    info = metrics(text)
    plain = render(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES}>{text}</text>')
    spaced = render(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'letter-spacing="{spacing}">{text}</text>')
    split = render(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'letter-spacing="{spacing}">ab<tspan>cd</tspan></text>')
    nested = render(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'letter-spacing="{spacing}"><tspan>ab</tspan><tspan>cd</tspan>'
        f'</text>')

    spaced_box = ink_box(spaced)
    assert ink_box(split) == spaced_box
    assert ink_box(nested) == spaced_box
    # Three gaps inside the text; the last character's trailing spacing is
    # not painted.
    assert_close(
        spaced_box[2] - ink_box(plain)[2],
        spacing * (len(text) - 1),
        tolerance=1)


def test_letter_spacing_affects_anchor():
    """Anchoring includes the extra inter-character spacing."""
    spacing, x_position, target = 14, 360, 320
    text = 'abcd'
    info = metrics(text)
    image = render(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{target}" letter-spacing="{spacing}">{text}</text>')
    box = ink_box(image)
    assert_close(
        box[0], x_position + info['left_bearing'], tolerance=2)
    # The advance box (which ends with the last character's trailing spacing)
    # is the target, so the painted end sits one right side bearing plus one
    # letter spacing before it.
    assert_close(
        box[2],
        x_position + target - info['last_trailing'] - spacing,
        tolerance=2)


def test_dx_is_applied_once():
    """``dx`` shifts its character once, also inside a nested ``tspan``."""
    text, x_position, offset = 'abcd', 40, 25
    bound, _ = run_layout_for(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'dx="{offset}">{text}</text>')
    split, _ = run_layout_for(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'>{text[:2]}<tspan dx="{offset}">{text[2:]}</tspan></text>')

    assert_close(bound[0].x, x_position + offset, tolerance=1e-6)
    # The nested dx only shifts the first character of the tspan, and the
    # characters after it keep their natural, single-shift spacing.
    assert_close(split[0].x, x_position, tolerance=1e-6)
    assert_close(split[2].x, split[1].end + offset, tolerance=1e-6)
    assert_close(split[3].x, split[2].x + split[2].advance, tolerance=1e-6)


def test_dy_is_applied_once():
    """``dy`` moves the following text once, not once per character."""
    text, x_position = 'abcd', 40
    characters, _ = run_layout_for(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'>ab<tspan dy="18">{text[2:]}</tspan></text>')
    baseline = characters[0].y
    assert_close(characters[2].y, baseline + 18, tolerance=1e-6)
    assert_close(characters[3].y, baseline + 18, tolerance=1e-6)


def test_explicit_x_list_is_pinned_and_calibrated():
    """Explicit positions survive calibration and start a new chunk."""
    text, first, second, target = 'abcd', 40, 220, 420
    image = render(
        f'<text x="{first} {second}" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{target}">{text}</text>')
    info = metrics(text)
    runs = column_runs(image)
    assert len(runs) == len(text)

    # The first character sits at the first explicit coordinate.
    assert_close(runs[0][0], first + info['left_bearing'], tolerance=2)
    # The character after the second explicit coordinate starts exactly at
    # the requested position (plus its own left bearing).
    assert_close(
        runs[1][0], second + metrics('b')['left_bearing'], tolerance=3)
    # Characters after the pinned one flow from it, not from the old
    # natural position.
    assert runs[2][0] > second
    assert runs[3][0] > runs[2][1]

    characters, _ = layout_for(
        f'<text x="{first} {second}" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{target}">{text}</text>')
    assert_close(characters[0].x, first, tolerance=1e-6)
    assert_close(characters[1].x, second, tolerance=1e-6)

    # Calibration is distributed over all three gaps before the explicit
    # positions are applied, then the whole tail shifts with the pinned
    # character.  So the gap after the pinned character is its natural
    # advance plus the same per-gap adjustment as everywhere else.
    # Pinned characters ignore the gap adjustment, so only check the gaps
    # that are free to flow.
    natural = info['natural']
    step = (target - natural) / (len(text) - 1)
    for index in range(1, len(characters) - 1):
        previous, following = characters[index], characters[index + 1]
        assert_close(
            following.x - previous.x,
            previous.advance + step, tolerance=1e-6)


def test_repeated_conversion_is_stable():
    """Converting twice gives identical pixels, including a reused tree."""
    body = (
        f'<text x="40" y="90" {FONT_ATTRIBUTES} textLength="330">'
        f'Stable <tspan>layout</tspan></text>')
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}">{body}</svg>').encode()
    first = cairosvg.svg2png(bytestring=svg)
    second = cairosvg.svg2png(bytestring=svg)
    assert first == second

    tree = parser.Tree(bytestring=svg)
    outputs = []
    for _ in range(2):
        output = io.BytesIO()
        surface.PNGSurface(tree, output, 96).finish()
        outputs.append(output.getvalue())
    assert outputs[0] == outputs[1]
    assert outputs[0] == first


def test_default_text_layout_unchanged():
    """Text without ``textLength`` still follows the legacy code path."""
    body = f'<text x="40" y="90" {FONT_ATTRIBUTES}>Hello world</text>'
    assert cairosvg.svg2png(bytestring=(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}">{body}</svg>').encode())
    # An explicit natural textLength produces the same pixels as no
    # textLength at all, which proves calibration is the identity there.
    natural = metrics('Hello world')['natural']
    default_image = render(body)
    identical_image = render(
        f'<text x="40" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{natural}">Hello world</text>')
    assert ink_box(default_image) == ink_box(identical_image)


def test_following_text_does_not_inherit_scaling():
    """A calibrated glyph stretch never leaks into a later ``text``."""
    scaled = (
        f'<text x="40" y="60" {FONT_ATTRIBUTES} textLength="500" '
        f'lengthAdjust="spacingAndGlyphs">Scaled</text>'
        f'<text x="40" y="160" {FONT_ATTRIBUTES}>Normal</text>')
    alone = f'<text x="40" y="160" {FONT_ATTRIBUTES}>Normal</text>'
    scaled_image = render(scaled, height=240)
    alone_image = render(alone, height=240)
    lower_scaled = scaled_image.crop((0, 120, WIDTH, 240))
    lower_alone = alone_image.crop((0, 120, WIDTH, 240))
    assert ink_box(lower_scaled) == ink_box(lower_alone)
    assert lower_scaled.tobytes() == lower_alone.tobytes()


@pytest.mark.parametrize('value', [
    '-100', 'bogus', '', 'none', '1e999', 'NaN'])
def test_invalid_text_length_is_ignored(value):
    """Invalid lengths fall back to the natural layout."""
    text = 'Hello'
    default_image = render(
        f'<text x="40" y="90" {FONT_ATTRIBUTES}>{text}</text>')
    invalid_image = render(
        f'<text x="40" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{value}">{text}</text>')
    assert ink_box(invalid_image) == ink_box(default_image)


def test_zero_text_length_keeps_natural_layout():
    """A zero length cannot be reached by scaling and is ignored."""
    text = 'Hello'
    default_image = render(
        f'<text x="40" y="90" {FONT_ATTRIBUTES}>{text}</text>')
    zero_image = render(
        f'<text x="40" y="90" {FONT_ATTRIBUTES} '
        f'textLength="0">{text}</text>')
    assert ink_box(zero_image) == ink_box(default_image)


def test_empty_text_and_empty_tspan():
    """Empty content is stable and does not disturb following text."""
    body = (
        f'<text x="40" y="60" {FONT_ATTRIBUTES} textLength="400"></text>'
        f'<text x="40" y="120" {FONT_ATTRIBUTES} textLength="400">'
        f'a<tspan></tspan>b</text>'
        f'<text x="40" y="180" {FONT_ATTRIBUTES}>After</text>')
    image = render(body, height=240)
    assert ink_box(image) is not None
    after = render(
        f'<text x="40" y="180" {FONT_ATTRIBUTES}>After</text>', height=240)
    assert ink_box(image.crop((0, 140, WIDTH, 240))) == ink_box(
        after.crop((0, 140, WIDTH, 240)))

    # An empty text with textLength contributes no ink at all.
    empty = render(
        f'<text x="40" y="90" {FONT_ATTRIBUTES} textLength="400"></text>')
    assert ink_box(empty) is None


def test_units_and_percentages():
    """``em`` and ``%`` lengths are resolved before layout."""
    text, x_position = 'Hi', 40
    em_value, em_size = 10, 30
    em_image = render(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{em_value}em">{text}</text>')
    pixel_image = render(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{em_value * em_size}">{text}</text>')
    assert ink_box(em_image) == ink_box(pixel_image)
    assert_close(
        ink_box(em_image)[2],
        x_position + em_value * em_size - metrics(text)['last_trailing'],
        tolerance=2)

    percent_value = 50
    percent_image = render(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{percent_value}%">{text}</text>')
    pixel_image = render(
        f'<text x="{x_position}" y="90" {FONT_ATTRIBUTES} '
        f'textLength="{percent_value * WIDTH / 100}">{text}</text>')
    assert ink_box(percent_image) == ink_box(pixel_image)
    assert_close(
        ink_box(percent_image)[2],
        x_position + percent_value * WIDTH / 100 -
        metrics(text)['last_trailing'],
        tolerance=2)


def test_text_path_is_not_recalibrated():
    """``textPath`` keeps its own layout and ignores the new engine."""
    body = (
        f'<defs><path id="curve" d="M30 140 C150 20 350 20 470 140"/></defs>'
        f'<text {FONT_ATTRIBUTES} textLength="100">'
        f'<textPath href="#curve">On a path</textPath></text>')
    default_image = render(body)
    without_length = render(
        body.replace(' textLength="100"', ''))
    assert ink_box(default_image) == ink_box(without_length)
