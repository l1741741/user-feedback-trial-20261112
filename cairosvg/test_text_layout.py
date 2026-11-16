"""
Regression tests for SVG text chunk layout and text-anchor handling.

These tests render real text with the cairo "toy" font API (a sans-serif font
available on the build machine) and measure the ink of individual glyph runs.
Positions are asserted against SVG 1.1 text layout rules with a small tolerance
to absorb font anti-aliasing differences; no spacing is faked with literal
spaces and no pre-rendered glyph bitmaps are used.

They cover:
  * a new text chunk started by an absolute x/y position, including mid-node
    ``x`` lists and ``tspan`` overrides, with per-chunk text-anchor;
  * ``dx``/``dy`` accumulation and reset, including nested ``tspan`` whose
    offsets must not leak into a later sibling line;
  * per-glyph ``rotate`` and ``letter-spacing``;
  * independent text elements whose result does not depend on the cursor state
    left by previously drawn text, regardless of drawing/anchor order;
  * empty and whitespace-only text, nested spans and repeated exports.

"""

import hashlib
import io
import math

import cairocffi as cairo

from . import svg2png

FONT = 'Helvetica'
# Anti-aliasing tolerance in output pixels (measured ink edges/centers).
TOL = 3.0


def _render(svg, width=400, height=200):
    png = svg2png(
        bytestring=svg.encode(), output_width=width, output_height=height)
    return png


def _ink_lines(png, color, width, height):
    """Return per-line ink boxes (x0, y0, x1, y1) for pixels near ``color``."""
    from PIL import Image
    if len(color) == 4:
        color = '#' + ''.join(c * 2 for c in color[1:])
    target = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    image = Image.open(io.BytesIO(png)).convert('RGBA')
    pixels = image.load()
    cells = set()
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if a > 40 and all(
                    abs(v - t) < 45 for v, t in zip((r, g, b), target)):
                cells.add((x, y))
    # 8-connectivity components, then merged into visual lines.
    boxes = []
    while cells:
        start = cells.pop()
        stack = [start]
        xs, ys = [start[0]], [start[1]]
        while stack:
            cx, cy = stack.pop()
            for nx in (cx - 1, cx, cx + 1):
                for ny in (cy - 1, cy, cy + 1):
                    if (nx, ny) in cells:
                        cells.remove((nx, ny))
                        stack.append((nx, ny))
                        xs.append(nx)
                        ys.append(ny)
        boxes.append([min(xs), min(ys), max(xs), max(ys)])
    lines = []
    for box in sorted(boxes, key=lambda b: (b[1], b[0])):
        x0, y0, x1, y1 = box
        for line in lines:
            if not (y1 < line[1] - 2 or y0 > line[3] + 2):
                line[0] = min(line[0], x0)
                line[1] = min(line[1], y0)
                line[2] = max(line[2], x1)
                line[3] = max(line[3], y1)
                break
        else:
            lines.append([x0, y0, x1, y1])
    return [tuple(line) for line in sorted(lines, key=lambda l: l[1])]


def _font_extents(text, size=20, family=FONT):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 10, 10)
    context = cairo.Context(surface)
    context.select_font_face(family)
    context.set_font_size(size)
    return context.text_extents(text)


def _svg(body, width=400, height=200):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}">'
        f'<rect width="{width}" height="{height}" fill="white"/>'
        f'{body}</svg>')


def test_multiple_x_values_create_independent_centered_chunks():
    """Each entry of an x list starts a chunk anchored on its own position."""
    body = (
        f'<line x1="100" y1="0" x2="100" y2="120" stroke="#888"/>'
        f'<line x1="300" y1="0" x2="300" y2="120" stroke="#888"/>'
        f'<text font-family="{FONT}" font-size="20" text-anchor="middle" '
        f'x="100 300" y="40 80" fill="#d00">ABCD</text>')
    lines = _ink_lines(_render(_svg(body, 400, 120), 400, 120), '#d00',
                       400, 120)
    assert len(lines) == 2
    for box, anchor in zip(lines, (100, 300)):
        center = (box[0] + box[2]) / 2
        assert abs(center - anchor) <= TOL


def test_tspan_x_overrides_start_independently_anchored_chunks():
    """An absolute x on a sibling tspan starts a new self-anchored chunk."""
    body = (
        f'<line x1="40" y1="0" x2="40" y2="120" stroke="#888"/>'
        f'<line x1="260" y1="0" x2="260" y2="120" stroke="#888"/>'
        f'<text font-family="{FONT}" font-size="20" text-anchor="middle" '
        f'fill="#b0b">'
        f'<tspan x="40" y="50">left</tspan>'
        f'<tspan x="260" y="90">right</tspan></text>')
    png = _render(_svg(body, 460, 120), 460, 120)
    lines = _ink_lines(png, '#b0b', 460, 120)
    assert len(lines) == 2
    scale = 460 / 460
    for box, anchor in zip(lines, (40, 260)):
        center = (box[0] + box[2]) / 2
        assert abs(center - anchor * scale) <= TOL


def test_nested_tspan_dx_does_not_leak_to_next_line():
    """A nested span's dx is dropped when a later span sets an absolute x."""
    body = (
        f'<line x1="60" y1="0" x2="60" y2="140" stroke="#888"/>'
        f'<text font-family="{FONT}" font-size="20" fill="#070">'
        f'<tspan x="60" y="40">A<tspan dx="15">B</tspan>C</tspan>'
        f'<tspan x="60" y="90">DEF</tspan></text>')
    png = _render(_svg(body, 400, 140), 400, 140)
    lines = _ink_lines(png, '#070', 400, 140)
    assert len(lines) == 2
    first, second = lines
    # Both lines start at the same absolute x; the second ignores the inner
    # dx=15 entirely.
    ext_a = _font_extents('ABC')[0]
    ext_d = _font_extents('DEF')[0]
    assert abs(first[0] - (60 + ext_a)) <= TOL
    assert abs(second[0] - (60 + ext_d)) <= TOL
    # The B glyph is pushed right relative to a no-dx reference while the next
    # line is not.
    assert first[2] > second[0] or first[0] < second[0]


def test_dx_and_dy_are_reset_on_matching_axis_only():
    """An absolute x clears dx but keeps an accumulated dy and vice versa."""
    from .parser import Tree
    from .surface import PNGSurface
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200">'
        f'<text font-family="{FONT}" font-size="20">'
        '<tspan x="40" y="60" dx="10" dy="5">A'
        '<tspan dx="7">B</tspan></tspan>'
        '<tspan x="40" y="120">C</tspan></text></svg>')
    tree = Tree(bytestring=svg.encode())
    surface = PNGSurface(tree, io.BytesIO(), 96, None, None, None,
                         1, 300, 200)
    from .text import _layout_text
    chunks = _layout_text(surface, tree.children[0])
    glyphs = [g for chunk in chunks for g in chunk['glyphs']]
    a, b, c = glyphs
    assert (a['x'], a['y']) == (50.0, 65.0)
    assert abs(b['x'] - (50 + a['extents'][4] + 7)) < 1e-6
    assert b['y'] == 65.0
    # New absolute x/y chunk: relative offsets do not carry over.
    assert (c['x'], c['y']) == (40.0, 120.0)


def test_independent_text_nodes_ignore_previous_anchor_state():
    """Mixed-anchor text elements each align to their own x only."""
    body = (
        f'<line x1="200" y1="0" x2="200" y2="140" stroke="#888"/>'
        f'<text font-family="{FONT}" font-size="20" text-anchor="end" '
        f'x="200" y="40" fill="#00c">end1</text>'
        f'<text font-family="{FONT}" font-size="20" text-anchor="middle" '
        f'x="200" y="80" fill="#c0c">mid2</text>'
        f'<text font-family="{FONT}" font-size="20" text-anchor="start" '
        f'x="200" y="120" fill="#c80">sta3</text>')
    png = _render(_svg(body, 400, 140), 400, 140)
    end = _ink_lines(png, '#00c', 400, 140)[0]
    mid = _ink_lines(png, '#c0c', 400, 140)[0]
    start = _ink_lines(png, '#c80', 400, 140)[0]
    assert abs((mid[0] + mid[2]) / 2 - 200) <= TOL
    assert abs(start[0] - 200) <= TOL
    ext = _font_extents('end1')
    expected_right = 200 - (ext[4] - ext[0] - ext[2])
    assert abs(end[2] - expected_right) <= TOL


def test_drawing_order_does_not_change_positions():
    """The same nodes in reverse order produce identical per-node ink boxes."""
    def render_order(reverse):
        nodes = [
            ('end', '#00c', 'end', 'end1', 40),
            ('mid', '#c0c', 'middle', 'mid2', 80),
            ('start', '#c80', 'start', 'sta3', 120),
        ]
        if reverse:
            nodes = list(reversed(nodes))
        body = ['<line x1="200" y1="0" x2="200" y2="140" stroke="#888"/>']
        for _, color, anchor, label, y in nodes:
            body.append(
                f'<text font-family="{FONT}" font-size="20" '
                f'text-anchor="{anchor}" x="200" y="{y}" fill="{color}">'
                f'{label}</text>')
        png = _render(_svg(''.join(body), 400, 140), 400, 140)
        return {
            color: _ink_lines(png, color, 400, 140)[0]
            for _, color, _, _, _ in nodes}
    first = render_order(False)
    second = render_order(True)
    for color in first:
        assert first[color] == second[color]


def test_per_glyph_rotate_list_and_persistence():
    """A rotate list maps to glyphs; a single value persists."""
    from .parser import Tree
    from .surface import PNGSurface
    from .text import _layout_text

    def layout(rotate):
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200">'
            f'<text font-family="{FONT}" font-size="20" x="40" y="60" '
            f'rotate="{rotate}">ABC</text></svg>')
        tree = Tree(bytestring=svg.encode())
        surface = PNGSurface(tree, io.BytesIO(), 96, None, None, None,
                             1, 300, 200)
        chunks = _layout_text(surface, tree.children[0])
        return [g['rotation'] for c in chunks for g in c['glyphs']]

    rotations = layout('30 0 0')
    assert abs(math.degrees(rotations[0]) - 30) < 1e-6
    assert abs(math.degrees(rotations[1])) < 1e-6
    assert all(abs(math.degrees(r) - 15) < 1e-6 for r in layout('15'))


def test_letter_spacing_inserts_space_before_non_first_glyphs():
    body_no = (
        f'<text font-family="{FONT}" font-size="24" x="40" y="50" '
        f'fill="#039">AB</text>')
    body_ls = (
        f'<text font-family="{FONT}" font-size="24" x="40" y="50" '
        f'letter-spacing="10" fill="#039">AB</text>')

    def second_glyph_x(body):
        png = _render(_svg(body, 300, 80), 300, 80)
        box = _ink_lines(png, '#039', 300, 80)[0]
        # Split the single line box into glyph column clusters.
        from PIL import Image
        image = Image.open(io.BytesIO(png)).convert('RGBA')
        pixels = image.load()
        columns = sorted({
            x for y in range(80) for x in range(300)
            if pixels[x, y][3] > 40 and pixels[x, y][2] > 120
            and pixels[x, y][0] < 90})
        groups = []
        for x in columns:
            if not groups or x > groups[-1][1] + 1:
                groups.append([x, x])
            else:
                groups[-1][1] = x
        assert len(groups) == 2
        return groups[1][0], box

    plain_x, _ = second_glyph_x(body_no)
    spaced_x, _ = second_glyph_x(body_ls)
    assert spaced_x - plain_x >= 9


def test_empty_and_whitespace_text_do_not_shift_later_content():
    body = (
        f'<text font-family="{FONT}" font-size="24" x="40" y="100" '
        f'fill="#060"><tspan></tspan>'
        f'<tspan x="40" y="100">after-empty</tspan></text>')
    png = _render(_svg(body, 300, 160), 300, 160)
    box = _ink_lines(png, '#060', 300, 160)[0]
    assert abs(box[0] - 40) <= TOL
    # Fully empty / whitespace-only nodes must render without raising.
    for empty in (
            '<text font-family="%s" font-size="20" x="10" y="40"></text>',
            '<text font-family="%s" font-size="20" x="10" y="40">   </text>',
            '<text font-family="%s" font-size="20" x="10" y="40"/>'):
        svg = _svg(empty % FONT, 200, 100)
        assert _render(svg, 200, 100)[:8] == b'\x89PNG\r\n\x1a\n'


def test_repeated_export_is_deterministic():
    body = (
        f'<text font-family="{FONT}" font-size="20" text-anchor="end" '
        f'x="200" y="40" fill="#00c">end1</text>'
        f'<text font-family="{FONT}" font-size="20" text-anchor="middle" '
        f'x="200" y="90" fill="#c0c">mid2</text>')
    svg = _svg(body, 300, 140)
    digests = {
        hashlib.sha256(_render(svg, 300, 140)).hexdigest()
        for _ in range(5)}
    assert len(digests) == 1


def test_reused_tree_renders_identically_each_time():
    """A Tree rendered by several surfaces keeps the same pixel output."""
    from PIL import Image, ImageChops
    from .parser import Tree
    from .surface import PNGSurface
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="120">'
        f'<text font-family="{FONT}" font-size="20" x="20" y="60" '
        f'fill="#039">he<tspan dx="5">ll</tspan>o</text></svg>')
    tree = Tree(bytestring=svg.encode())

    def draw():
        output = io.BytesIO()
        surface = PNGSurface(tree, output, 96, None, None, None, 1, 300, 120)
        surface.finish()
        return Image.open(io.BytesIO(output.getvalue())).convert('RGBA')

    images = [draw() for _ in range(3)]
    for earlier, later in zip(images, images[1:]):
        assert ImageChops.difference(earlier, later).getbbox() is None
    pixels = images[-1].load()
    ink = sum(
        1 for y in range(120) for x in range(300)
        if pixels[x, y][3] > 40 and pixels[x, y][2] > 120)
    assert ink > 50


def test_stroked_text_without_fill_still_renders():
    body = (
        f'<text font-family="{FONT}" font-size="24" x="40" y="80" '
        f'fill="none" stroke="#c33" stroke-width="1.5">STROKED</text>')
    png = _render(_svg(body, 300, 120), 300, 120)
    box = _ink_lines(png, '#c33', 300, 120)[0]
    assert box[2] - box[0] > 60


def test_root_position_seeds_text_held_in_child_tspans():
    """An x/y on the root text element positions glyphs that live in tspans.

    x/y are non-inherited attributes, so the engine must seed the first child
    glyph from the root's leading position rather than starting at (0, 0).
    """
    body = (
        f'<text font-family="{FONT}" font-size="20" x="40" y="40 90 140" '
        f'fill="#003399">'
        f'<tspan>line1</tspan><tspan>line2</tspan>'
        f'<tspan>line3</tspan></text>')
    png = _render(_svg(body, 300, 160), 300, 160)
    from PIL import Image
    image = Image.open(io.BytesIO(png)).convert('RGBA')
    pixels = image.load()
    columns = [
        x for y in range(160) for x in range(300)
        if pixels[x, y][3] > 40 and pixels[x, y][2] > 100
        and pixels[x, y][0] < 100]
    # The first glyph of every child line keeps the root's leading x.
    assert abs(min(columns) - 40) <= TOL


def test_anchor_wrapping_text_renders_without_duplication():
    """An <a> element wrapping text/tspans draws the glyphs exactly once."""
    body = (
        f'<text font-family="{FONT}" font-size="20" fill="#007700">'
        '<a href="#t">'
        '<tspan x="30" y="50">one</tspan>'
        '<tspan x="30" y="90">two</tspan></a></text>')
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="120" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink">'
        f'<rect width="300" height="120" fill="white"/>{body}</svg>')
    png = _render(svg, 300, 120)
    lines = _ink_lines(png, '#007700', 300, 120)
    # Two distinct lines, each rendered a single time.
    assert len(lines) == 2
    assert all(abs(box[0] - 30) <= TOL for box in lines)


def test_per_tspan_font_size_and_weight_are_applied():
    """A larger bold tspan inside a text element keeps its own font metrics."""
    from .parser import Tree
    from .surface import PNGSurface
    from .text import _layout_text
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="360" height="140">'
        f'<text x="20" y="60" font-family="{FONT}" font-size="20">'
        'small<tspan font-size="36" font-weight="bold">BIG</tspan>'
        '<tspan>normal</tspan></text></svg>')
    tree = Tree(bytestring=svg.encode())
    surface = PNGSurface(tree, io.BytesIO(), 96, None, None, None,
                         1, 360, 140)
    glyphs = {
        g['letter']: g
        for chunk in _layout_text(surface, tree.children[0])
        for g in chunk['glyphs']}
    # The bold 36px capitals are well over 1.5x the height of the 20px glyphs.
    assert glyphs['B']['extents'][3] > glyphs['s']['extents'][3] * 1.5
    assert glyphs['B']['node'].get('font-weight') == 'bold'


def test_per_tspan_fill_color_is_preserved():
    """A tspan overriding fill is painted with its own color, not the root's."""
    body = (
        f'<text font-family="{FONT}" font-size="24" x="30" y="60" fill="#333333">'
        'before<tspan fill="#c0392b">RED</tspan>after</text>')
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="100">'
        f'<rect width="300" height="100" fill="white"/>{body}</svg>')
    png = _render(svg, 300, 100)
    from PIL import Image
    image = Image.open(io.BytesIO(png)).convert('RGBA')
    pixels = image.load()
    red = sum(
        1 for y in range(100) for x in range(300)
        if pixels[x, y][3] > 40 and pixels[x, y][0] > 150
        and pixels[x, y][1] < 110 and pixels[x, y][2] < 90)
    # The red tspan must produce a substantial amount of red ink.
    assert red > 150
