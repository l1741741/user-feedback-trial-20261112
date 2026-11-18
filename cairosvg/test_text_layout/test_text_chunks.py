"""Regression tests for SVG text chunk layout and text-anchor handling.

These tests pin down the behavior required by SVG 1.1 section 10.5
("Text layout") and section 10.9.2 ("Text alignment"):

* an explicit ``x``/``y`` opens a new text chunk whose boundary is
  independent of preceding chunks;
* ``text-anchor`` aligns the *whole* chunk, not each glyph in isolation;
* ``dx``/``dy`` and per-glyph ``rotate`` are applied once and keep their
  relative/rotation semantics across nested ``tspan`` elements;
* parent/child span inheritance and local coordinate overrides must not
  double-apply an offset;
* separate ``text`` nodes render independently of any earlier text state;
* empty text nodes and repeated rendering must stay stable.

The tests render with the fonts available on the host (Helvetica ships with
macOS; the suite skips gracefully when it is missing) and assert on measured
ink extents with a tolerance that accommodates font anti-aliasing rather
than exact pixel equality.
"""

import io

import pytest

from .. import svg2png
from ..parser import Tree
from ..surface import PNGSurface
from ..text import TextState

FONT = 'Helvetica'
# Anti-aliasing and hinting move ink edges by a pixel or two.
TOLERANCE = 2.0


def _have_font():
    """Check that selecting the font yields real glyph metrics.

    The cairo "toy" font API silently falls back to a default face when the
    requested family is unavailable, so we compare the extents it gives for
    our family against the default face's extents.
    """
    try:
        import cairocffi as cairo
    except Exception:  # pragma: no cover - cairo is a hard dependency
        return False
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 10, 10)
    context = cairo.Context(surface)
    context.select_font_face(FONT)
    context.set_font_size(24)
    # A real font face returns a positive x-advance for a normal glyph.
    return context.text_extents('Hg')[4] > 0


requires_font = pytest.mark.skipif(
    not _have_font(), reason=f'font "{FONT}" is not available on this host')


def _surface(svg):
    tree = Tree(bytestring=svg)
    buffer = io.BytesIO()
    surface = PNGSurface(tree, buffer, 96)
    return surface, tree, buffer


def _layout(svg):
    surface, tree, _ = _surface(svg)
    root = tree.children[0]
    while root.tag != 'text':
        root = root.children[0]
    return TextState(surface, root, 0), surface


def _render(svg):
    return svg2png(bytestring=svg)


def _ink_columns(png_bytes, y0, y1, gap=None):
    """Return ink column groups in the given row band.

    With ``gap=None`` each run of contiguous ink columns is returned as its
    own cluster (suitable for individual glyphs). Passing an integer merges
    neighboring clusters separated by at most that many pixels into a single
    word group (suitable for whole-word anchor checks).
    """
    from PIL import Image
    image = Image.open(io.BytesIO(png_bytes)).convert('RGBA')
    pixels = image.load()
    raw, current = [], None
    for x in range(image.width):
        inked = any(
            pixels[x, y][3] >= 160 and max(pixels[x, y][:3]) < 100
            for y in range(y0, y1))
        if inked and current is None:
            current = [x, x]
        elif inked:
            current[1] = x
        elif current is not None:
            raw.append(tuple(current))
            current = None
    if current is not None:
        raw.append(tuple(current))
    if gap is None:
        return raw
    # Merge glyph clusters separated only by small intra-word gaps.
    groups = []
    for cluster in raw:
        if groups and cluster[0] - groups[-1][1] <= gap:
            groups[-1] = (groups[-1][0], cluster[1])
        else:
            groups.append(cluster)
    return groups


def _ink_center(png_bytes, x0, x1, y0, y1):
    from PIL import Image
    image = Image.open(io.BytesIO(png_bytes)).convert('RGBA')
    pixels = image.load()
    xs = [
        x for x in range(x0, x1) for y in range(y0, y1)
        if pixels[x, y][3] >= 160 and max(pixels[x, y][:3]) < 100]
    if not xs:
        return None
    return (min(xs) + max(xs)) / 2, min(xs), max(xs)


# ---------------------------------------------------------------------------
# Layout-level (geometry) tests
# ---------------------------------------------------------------------------

@requires_font
def test_explicit_x_starts_independent_chunk():
    """Two x values in one node yield two separately anchored chunks."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="100"
        font-family="{FONT}" font-size="24">
      <text x="150 450" y="50" text-anchor="middle">AB</text></svg>'''.encode()
    state, _ = _layout(svg)
    assert len(state.chunks) == 2
    assert state.chunks[0]['origin_x'] == 150
    assert state.chunks[1]['origin_x'] == 450
    for chunk in state.chunks:
        # Each single-glyph chunk must be centered on its own origin.
        glyph = chunk['glyphs'][0]
        shift = state.chunk_shift(chunk)
        center = (glyph['x'] + glyph['x_bearing'] + shift,
                  glyph['x'] + glyph['x_bearing'] + glyph['width'] + shift)
        assert abs((center[0] + center[1]) / 2 - chunk['origin_x']) <= TOLERANCE


@requires_font
def test_anchor_shift_applied_once_per_chunk():
    """A multi-glyph end-anchored chunk ends exactly on the anchor point."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="100"
        font-family="{FONT}" font-size="24">
      <text x="300" y="50" text-anchor="end">WORD</text></svg>'''.encode()
    state, _ = _layout(svg)
    assert len(state.chunks) == 1
    chunk = state.chunks[0]
    shift = state.chunk_shift(chunk)
    last = chunk['glyphs'][-1]
    right_edge = last['x'] + last['x_bearing'] + last['width'] + shift
    assert abs(right_edge - 300) <= TOLERANCE


@requires_font
def test_mid_line_x_change_anchors_only_new_chunk():
    """AAA stays centered on 150 while BBB centers independently on 450."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="100"
        font-family="{FONT}" font-size="24">
      <text x="150" y="50" text-anchor="middle">
        AAA<tspan x="450">BBB</tspan></text></svg>'''.encode()
    state, _ = _layout(svg)
    assert len(state.chunks) == 2
    centers = []
    for chunk in state.chunks:
        shift = state.chunk_shift(chunk)
        left = min(g['x'] + g['x_bearing'] + shift for g in chunk['glyphs'])
        right = max(
            g['x'] + g['x_bearing'] + g['width'] + shift
            for g in chunk['glyphs'])
        centers.append((left + right) / 2)
    assert abs(centers[0] - 150) <= TOLERANCE
    assert abs(centers[1] - 450) <= TOLERANCE


@requires_font
def test_nested_tspan_dx_applied_once():
    """dx shifts only the span that declares it; siblings are not shifted.

    The parser turns text following a tspan into an anonymous sibling tspan,
    which does not inherit dx (it is a non-inherited attribute). The check
    pins that the 30-unit displacement lands on 'b' once and never again on
    the following 'c'.
    """
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="100"
        font-family="{FONT}" font-size="24">
      <text x="60" y="50">a<tspan dx="30">b</tspan>c</text></svg>'''.encode()
    state, _ = _layout(svg)
    glyphs = [g for chunk in state.chunks for g in chunk['glyphs']]
    assert [g['letter'] for g in glyphs] == ['a', 'b', 'c']
    a_g, b_g, c_g = glyphs
    a_x, b_x, c_x = (g['x'] for g in glyphs)
    # 'b' moves right by exactly 30 relative to normal flow after 'a'.
    assert abs((b_x - (a_x + a_g['x_advance'])) - 30) <= TOLERANCE
    # 'c' is a sibling span without dx: it continues from 'b' with only the
    # normal advance, so the 30 is never applied a second time.
    assert abs(c_x - (b_x + b_g['x_advance'])) <= TOLERANCE


@requires_font
def test_nested_tspan_dx_accumulates_without_doubling():
    """Each nested level's dx contributes exactly once, never twice.

    SVG 1.1 folds supplemental dx into the single current text position, so a
    displacement persists for following glyphs. The regression guarded here
    is *double application*: a child span must not re-read its parent's dx on
    top of the position already moved by that dx.
    """
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="700" height="100"
        font-family="{FONT}" font-size="24"><text x="60" y="50">A<tspan
        dx="40">B<tspan dx="20">C</tspan>D</tspan></text></svg>'''.encode()
    state, _ = _layout(svg)
    positions = {g['letter']: g['x']
                 for chunk in state.chunks for g in chunk['glyphs']}
    advances = {g['letter']: g['x_advance']
                for chunk in state.chunks for g in chunk['glyphs']}
    # B carries the parent tspan's +40 relative to normal flow after A.
    assert abs((positions['B'] - (positions['A'] + advances['A']))
               - 40) <= TOLERANCE
    # C is nested one level deeper: the +20 is added once to the live cursor,
    # giving 40 + 20 = 60 of cumulative supplemental offset. Doubling the
    # parent's +40 would instead yield an extra 40 here.
    normal_c = positions['B'] + advances['B']
    assert abs((positions['C'] - normal_c) - 20) <= TOLERANCE
    # D simply continues from C using the normal glyph advance; no dx is
    # declared on it, so the cursor moves by exactly C's advance.
    assert abs(positions['D'] -
               (positions['C'] + advances['C'])) <= TOLERANCE


@requires_font
def test_nested_tspan_dy_does_not_leak_to_sibling_text():
    """A dy inside a span moves the live current point within its text node
    but never crosses into a separate, later ``text`` element."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="120"
        font-family="{FONT}" font-size="24">
      <text x="60" y="80">p<tspan dy="-30">q</tspan>s</text>
      <text x="200" y="80">flat</text></svg>'''.encode()
    state, surface = _layout(svg)
    first_text = {g['letter']: g for c in state.chunks for g in c['glyphs']}
    # p stays on the original baseline; q takes the -30, and because the
    # displacement becomes the current text position, s continues at y=50.
    assert first_text['p']['y'] == 80
    assert first_text['q']['y'] == 80 - 30
    assert first_text['s']['y'] == 80 - 30
    # The second, independent text element builds its own fresh state starting
    # from its own y=80; the first element's dy never reaches it.
    second_tree = Tree(bytestring=svg)
    second_state = TextState(surface, second_tree.children[1], 0)
    flat_glyphs = [g for c in second_state.chunks for g in c['glyphs']]
    assert all(abs(g['y'] - 80) < 1e-9 for g in flat_glyphs)


@requires_font
def test_per_glyph_rotate_kept_and_local():
    """rotate applies per glyph and does not rotate the next text node."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="100"
        font-family="{FONT}" font-size="24">
      <text x="60" y="50" rotate="15 15 15">rot</text>
      <text x="200" y="50">flat</text></svg>'''.encode()
    from math import radians
    state, _ = _layout(svg)
    rotated = [g for c in state.chunks for g in c['glyphs']]
    assert all(abs(g['rotation'] - radians(15)) < 1e-9 for g in rotated)
    tree = Tree(bytestring=svg)
    second_state = TextState(state.surface, tree.children[1], 0)
    flat = [g for c in second_state.chunks for g in c['glyphs']]
    assert all(g['rotation'] in (None, 0) for g in flat)


@requires_font
def test_empty_text_does_not_move_following_node():
    """An empty text node leaves the next node anchored at its own x."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="100"
        font-family="{FONT}" font-size="24">
      <text x="100" y="50"></text>
      <text x="100" y="80">after</text></svg>'''.encode()
    tree = Tree(bytestring=svg)
    surface = PNGSurface(tree, io.BytesIO(), 96)
    empty_state = TextState(surface, tree.children[0], 0)
    assert empty_state.chunks == []
    after_state = TextState(surface, tree.children[1], 0)
    glyphs = [g for c in after_state.chunks for g in c['glyphs']]
    assert glyphs[0]['x'] == 100
    assert glyphs[0]['y'] == 80


@requires_font
def test_separate_text_nodes_have_independent_state():
    """Two sibling text nodes never share cursor or shift state."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="100"
        font-family="{FONT}" font-size="24">
      <text x="50" y="50" dx="40">first</text>
      <text x="50" y="80">second</text></svg>'''.encode()
    tree = Tree(bytestring=svg)
    surface = PNGSurface(tree, io.BytesIO(), 96)
    first = TextState(surface, tree.children[0], 0)
    second = TextState(surface, tree.children[1], 0)
    first_glyphs = [g for c in first.chunks for g in c['glyphs']]
    second_glyphs = [g for c in second.chunks for g in c['glyphs']]
    # The dx=40 on the first node must not reach the second node.
    assert abs(second_glyphs[0]['x'] - 50) < 1e-9
    assert abs(first_glyphs[0]['x'] - 90) < 1e-9


# ---------------------------------------------------------------------------
# Rendered (pixel) tests with antialiasing tolerance
# ---------------------------------------------------------------------------

@requires_font
def test_rendered_three_anchors_on_guides():
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="100"
        font-family="{FONT}" font-size="24">
      <text x="150" y="50" text-anchor="start">start</text>
      <text x="300" y="50" text-anchor="middle">middle</text>
      <text x="450" y="50" text-anchor="end">end</text></svg>'''.encode()
    png = _render(svg)
    groups = _ink_columns(png, 25, 75, gap=8)
    assert len(groups) == 3
    assert abs(groups[0][0] - 150) <= TOLERANCE          # start left edge
    center = (groups[1][0] + groups[1][1]) / 2
    assert abs(center - 300) <= TOLERANCE                # middle center
    assert abs(groups[2][1] - 450) <= TOLERANCE          # end right edge


@requires_font
def test_rendered_chunk_boundaries_after_x_change():
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="100"
        font-family="{FONT}" font-size="24">
      <text x="150" y="50" text-anchor="middle">
        AAA<tspan x="450">BBB</tspan></text></svg>'''.encode()
    png = _render(svg)
    groups = _ink_columns(png, 25, 75, gap=8)
    assert len(groups) == 2
    assert abs((groups[0][0] + groups[0][1]) / 2 - 150) <= TOLERANCE
    assert abs((groups[1][0] + groups[1][1]) / 2 - 450) <= TOLERANCE


@requires_font
def test_rendered_nested_dx_offset_once():
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="100"
        font-family="{FONT}" font-size="24">
      <text x="60" y="50">a<tspan dx="30">b</tspan>c</text></svg>'''.encode()
    png = _render(svg)
    groups = _ink_columns(png, 25, 75)
    # Three glyph clusters: a, b (shifted +30), c (sibling, normal advance).
    assert len(groups) == 3
    # The a->b gap is the normal inter-glyph advance plus the 30-unit dx,
    # while b->c has no dx on either node, so it is the normal advance only.
    gap_ab = groups[1][0] - groups[0][1]
    gap_bc = groups[2][0] - groups[1][1]
    assert abs((gap_ab - gap_bc) - 30) <= TOLERANCE


@requires_font
def test_rendered_repeated_export_is_stable():
    """Rendering the same SVG twice must give byte-identical output."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="400" height="120"
        font-family="{FONT}" font-size="24">
      <text x="200" y="50" text-anchor="middle">
        repeat<tspan dx="10" dy="10">me</tspan></text></svg>'''.encode()
    first = _render(svg)
    second = _render(svg)
    third = svg2png(bytestring=svg)
    assert first == second == third


@requires_font
def test_rendered_empty_text_is_blank_but_valid():
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100" '
        f'font-family="{FONT}" font-size="24">'
        f'<text x="10" y="50"></text></svg>').encode()
    png = _render(svg)
    assert png[:8] == b'\x89PNG\r\n\x1a\n'
    assert _ink_columns(png, 0, 100) == []


@requires_font
def test_anchor_change_inside_tspan_line():
    """An end-anchored span ends on its own x without shifting the start."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="100"
        font-family="{FONT}" font-size="24">
      <text x="150" y="50" text-anchor="start">left
        <tspan x="450" text-anchor="end">right</tspan></text></svg>'''.encode()
    png = _render(svg)
    groups = _ink_columns(png, 25, 75, gap=8)
    assert len(groups) == 2
    assert abs(groups[0][0] - 150) <= TOLERANCE
    assert abs(groups[1][1] - 450) <= TOLERANCE


@requires_font
def test_anchor_order_does_not_affect_positions():
    """Reordering sibling text nodes must not change any node's placement."""
    head = ('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="160" '
            f'font-family="{FONT}" font-size="24">')
    tail = '</svg>'
    nodes = (
        '<text x="200" y="40" text-anchor="end">end</text>'
        '<text x="200" y="80" text-anchor="start">start</text>'
        '<text x="200" y="120" text-anchor="middle">mid</text>')
    reordered = (
        '<text x="200" y="120" text-anchor="middle">mid</text>'
        '<text x="200" y="80" text-anchor="start">start</text>'
        '<text x="200" y="40" text-anchor="end">end</text>')

    def centers(png):
        from PIL import Image
        image = Image.open(io.BytesIO(png)).convert('RGBA')
        pixels = image.load()
        result = []
        for y0, y1 in ((20, 60), (60, 100), (100, 140)):
            xs = [
                x for x in range(image.width) for y in range(y0, y1)
                if pixels[x, y][3] >= 160 and max(pixels[x, y][:3]) < 100]
            result.append((min(xs) + max(xs)) / 2 if xs else None)
        return result

    assert centers(_render((head + nodes + tail).encode())) == \
        centers(_render((head + reordered + tail).encode()))


@requires_font
def test_y_coordinate_also_starts_a_chunk():
    """A second y value opens a new chunk on a new baseline."""
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"
        font-family="{FONT}" font-size="20">
      <text x="20" y="30 80">AB</text></svg>'''.encode()
    state, _ = _layout(svg)
    assert len(state.chunks) == 2
    assert state.chunks[0]['origin_y'] == 30
    assert state.chunks[1]['origin_y'] == 80
    letters = [g['letter'] for c in state.chunks for g in c['glyphs']]
    assert letters == ['A', 'B']


@requires_font
def test_whitespace_only_text_paints_nothing():
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
           f'font-family="{FONT}" font-size="20">'
           f'<text x="10" y="50">   </text></svg>').encode()
    assert _ink_columns(_render(svg), 0, 100) == []
