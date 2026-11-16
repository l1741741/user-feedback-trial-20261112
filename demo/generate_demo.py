#!/usr/bin/env python3
"""Generate a self-contained PNG demo of the transform-origin work.

The demo renders a series of small SVG panels and composes them, together
with reference crosshairs marking the expected rotation centers, into one
PNG that can be inspected without any tooling beyond Pillow and CairoSVG.

Run from the repository root with the project virtualenv::

    .venv/bin/python demo/generate_demo.py

The output is written to ``demo/transform_demo.png`` alongside an HTML
summary (``demo/index.html``) describing every panel and its expected
geometry.

"""

import io
import os
import sys

# Allow running straight from a checkout without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cairosvg  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

PANEL_SIZE = 200
MARGIN = 24
COLUMNS = 2
BACKGROUND = (255, 255, 255)
PANEL_BG = (250, 250, 252)
BORDER = (180, 184, 190)
TEXT = (40, 44, 52)
CROSSHAIR = (30, 110, 220)
GRID = (225, 228, 233)
TEXT_BLOCK_HEIGHT = 78
HEADER_HEIGHT = 70

# Each panel: (title, description, svg body, expected center in device px,
# expected corner markers as (x, y, label)).
PANELS = [
    (
        '1. Explicit length origin',
        'rotate(90) about origin 100,100: the rect pivots on the '
        'crosshair and its center stays put.',
        '<rect x="70" y="90" width="60" height="20" fill="#d64545" '
        'stroke="#7a1f1f" stroke-width="1" '
        'transform="rotate(90)" transform-origin="100 100"/>',
        (100, 100),
        [(100, 100, 'center fixed')],
    ),
    (
        '2. Keyword origin, viewBox x2',
        '"center" resolves in local units (50,50) -> device (100,100), '
        'not at the device corner.',
        '<rect x="35" y="45" width="30" height="10" fill="#d64545" '
        'stroke="#7a1f1f" stroke-width="1" '
        'transform="rotate(90)" transform-origin="center"/>',
        (100, 100),
        [(100, 100, 'local 50,50')],
        '0 0 100 100',
    ),
    (
        '3. Nested group, local origin',
        'Parent translate(40,50); child rotates about local (40,40). '
        'The parent then moves the rotated result.',
        '<g transform="translate(40,50)">'
        '<rect x="20" y="30" width="40" height="20" fill="#d64545" '
        'stroke="#7a1f1f" stroke-width="1" '
        'transform="rotate(90)" transform-origin="40 40"/></g>',
        (80, 90),
        [(80, 90, 'local origin + parent')],
    ),
    (
        '4. transform-box: fill-box',
        '"center" targets the rect bounding box, so off-center geometry '
        'rotates about its own middle.',
        '<rect x="30" y="110" width="80" height="30" fill="#d64545" '
        'stroke="#7a1f1f" stroke-width="1" '
        'transform="rotate(90)" transform-origin="center" '
        'transform-box="fill-box"/>',
        (70, 125),
        [(70, 125, 'fill-box center')],
    ),
    (
        '5. Percentage origin in group',
        '50% 50% references the local viewport inside the translated '
        'group; the axis travels with the group.',
        '<g transform="translate(20,20)">'
        '<rect x="30" y="50" width="60" height="20" fill="#d64545" '
        'stroke="#7a1f1f" stroke-width="1" '
        'transform="rotate(90)" transform-origin="50% 50%"/></g>',
        (120, 120),
        [(120, 120, 'rotation axis')],
    ),
    (
        '6. Composition order',
        'Nested translate(110,110) then rotate(90): rotation acts first '
        'on geometry, translation moves the result.',
        '<g transform="translate(110,110)">'
        '<rect x="20" y="0" width="30" height="14" fill="#2e7d32" '
        'stroke="#144016" stroke-width="1" transform="rotate(90)"/></g>',
        None,
        [(103, 145, 'rotate then translate')],
    ),
    (
        '7. Invalid origin isolation',
        'transform-origin="garbage" on the red rect is ignored; the '
        'blue sibling renders exactly where declared.',
        '<rect x="20" y="20" width="30" height="30" fill="#d64545" '
        'transform="rotate(90)" transform-origin="garbage"/>'
        '<rect x="110" y="110" width="50" height="50" fill="#2f6fd0"/>',
        None,
        [(135, 135, 'sibling ok')],
    ),
    (
        '8. Reused node keeps position',
        'One rotated group is instantiated twice with use; both copies '
        'show identical footprints at their own x/y.',
        '<defs><g id="blade"><rect x="0" y="0" width="40" height="16" '
        'fill="#d64545" stroke="#7a1f1f" stroke-width="1" '
        'transform="rotate(90)" transform-origin="20 8"/></g></defs>'
        '<use href="#blade" x="50" y="60"/>'
        '<use href="#blade" x="130" y="110"/>',
        None,
        [(70, 68, 'use #1'), (150, 118, 'use #2')],
    ),
]


def render_panel(svg_body, view_box=None):
    view_box_attr = f' viewBox="{view_box}"' if view_box else ''
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{PANEL_SIZE}" '
        f'height="{PANEL_SIZE}"{view_box_attr}>'
        # Light coordinate grid drawn in SVG so scaling is visible.
        '<g stroke="#e1e4e9" stroke-width="1">'
        + ''.join(
            f'<line x1="{i}" y1="0" x2="{i}" y2="{PANEL_SIZE}"/>'
            f'<line x1="0" y1="{i}" x2="{PANEL_SIZE}" y2="{i}"/>'
            for i in range(0, PANEL_SIZE, 20))
        + '</g>'
        f'{svg_body}</svg>')
    png = cairosvg.svg2png(svg.encode())
    return Image.open(io.BytesIO(png)).convert('RGBA')


# The exact origin-resolution logic that shipped before the fix: the
# "center" keyword was measured against the device surface size (200px) even
# under a viewBox that scales user units 2x, so the rect rotated about a
# point twice as far away as the spec defines and left the canvas. Kept here
# only to render the "before" half of the comparison panel.
class _LegacySurface:
    """Minimal stand-in reproducing the pre-fix device-sized origin."""

    def __init__(self, view_box):
        import cairocffi as cairo
        self.width = self.height = PANEL_SIZE
        self.cairo = cairo.ImageSurface(
            cairo.FORMAT_ARGB32, PANEL_SIZE, PANEL_SIZE)
        self.context = cairo.Context(self.cairo)
        if view_box:
            # viewBox 0 0 100 100 -> 2x user-to-device scale.
            self.context.scale(2, 2)

    def legacy_origin_render(self):
        import math
        import cairocffi as cairo
        matrix = cairo.Matrix()
        matrix.translate(self.width / 2, self.height / 2)
        matrix.rotate(math.radians(90))
        matrix.translate(-self.width / 2, -self.height / 2)
        matrix.invert()
        self.context.transform(matrix)
        self.context.set_source_rgb(0.84, 0.27, 0.27)
        self.context.rectangle(35, 45, 30, 10)
        self.context.fill()
        buffer = io.BytesIO()
        self.cairo.write_to_png(buffer)
        return Image.open(io.BytesIO(buffer.getvalue())).convert('RGBA')


def load_font(size):
    for path in (
        '/System/Library/Fonts/Supplemental/Arial.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    title_font = load_font(16)
    body_font = load_font(12)
    label_font = load_font(11)
    tag_font = load_font(13)

    # Extra full-width before/after comparison panel.
    comparison = {
        'title': '9. Before / after: keyword center with viewBox x2',
        'description': (
            'The old code measured "center" against the 200px device '
            'surface even though the viewBox scales user units 2x, so the '
            'rect pivoted around device (100,100) interpreted as user '
            '(100,100) and flew off-canvas. The fix resolves the origin in '
            'local user space.'),
        'view_box': '0 0 100 100',
        'body': (
            '<rect x="35" y="45" width="30" height="10" fill="#d64545" '
            'stroke="#7a1f1f" stroke-width="1" '
            'transform="rotate(90)" transform-origin="center"/>'),
    }

    rows = (len(PANELS) + COLUMNS - 1) // COLUMNS + 1
    cell_height = TEXT_BLOCK_HEIGHT + PANEL_SIZE
    width = COLUMNS * PANEL_SIZE + (COLUMNS + 1) * MARGIN
    height = (HEADER_HEIGHT + rows * cell_height
              + (rows + 1) * MARGIN + 30)
    sheet = Image.new('RGB', (width, height), BACKGROUND)
    draw = ImageDraw.Draw(sheet)

    draw.text(
        (MARGIN, 18),
        'CairoSVG transform-origin / transform-box verification demo',
        fill=TEXT, font=title_font)
    draw.text(
        (MARGIN, 42),
        'Blue crosshairs and dots mark expected centers and corner '
        'positions; grid spacing is 20 device pixels.',
        fill=(90, 96, 106), font=body_font)

    html_parts = [
        '<!doctype html><meta charset="utf-8">',
        '<title>transform demo</title>',
        '<style>body{font-family:sans-serif;margin:24px;color:#282c34}'
        '.grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}'
        '.panel{border:1px solid #b4b8be;border-radius:8px;padding:12px}'
        'h2{font-size:15px;margin:0 0 6px}p{font-size:12px;margin:0 0 10px}'
        '</style><h1>Panel index</h1><div class="grid">']

    for index, panel in enumerate(PANELS):
        title, description, body = panel[0], panel[1], panel[2]
        expected_center = panel[3]
        markers = panel[4]
        view_box = panel[5] if len(panel) > 5 else None

        row, col = divmod(index, COLUMNS)
        x0 = MARGIN + col * (PANEL_SIZE + MARGIN)
        y0 = (HEADER_HEIGHT + MARGIN
              + row * (cell_height + MARGIN))

        draw.text((x0, y0), title, fill=TEXT, font=title_font)
        # Wrap against the real measured text width so lines never cross
        # the neighboring column.
        words, lines, current = description.split(), [], ''
        for word in words:
            candidate = f'{current} {word}'.strip()
            if draw.textlength(candidate, font=body_font) > PANEL_SIZE:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
        for line_index, line in enumerate(lines[:3]):
            draw.text(
                (x0, y0 + 22 + line_index * 16), line,
                fill=(90, 96, 106), font=body_font)

        image_y = y0 + TEXT_BLOCK_HEIGHT
        panel_image = render_panel(body, view_box)
        draw.rectangle(
            (x0 - 1, image_y - 1, x0 + PANEL_SIZE, image_y + PANEL_SIZE),
            fill=PANEL_BG, outline=BORDER)
        sheet.paste(panel_image, (x0, image_y), panel_image)

        overlay = ImageDraw.Draw(sheet)
        if expected_center is not None:
            cx, cy = x0 + expected_center[0], image_y + expected_center[1]
            overlay.line(
                (cx - 9, cy, cx + 9, cy), fill=CROSSHAIR, width=1)
            overlay.line(
                (cx, cy - 9, cx, cy + 9), fill=CROSSHAIR, width=1)
            overlay.ellipse(
                (cx - 3, cy - 3, cx + 3, cy + 3), outline=CROSSHAIR)
        for mx, my, label in markers:
            px, py = x0 + mx, image_y + my
            overlay.ellipse(
                (px - 3, py - 3, px + 3, py + 3), fill=CROSSHAIR)
            overlay.text((px + 6, py - 7), label,
                         fill=CROSSHAIR, font=label_font)

        html_parts.append(
            f'<div class="panel"><h2>{title}</h2><p>{description}</p>'
            f'<p><b>viewBox:</b> {view_box or "none"}</p></div>')

    # --- Full-width before/after comparison row ---------------------------
    comp_y = (HEADER_HEIGHT + MARGIN
              + (rows - 1) * (cell_height + MARGIN))
    draw.text(
        (MARGIN, comp_y), comparison['title'], fill=TEXT, font=title_font)
    words, lines, current = comparison['description'].split(), [], ''
    for word in words:
        candidate = f'{current} {word}'.strip()
        if draw.textlength(candidate, font=body_font) > width - 2 * MARGIN:
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    for line_index, line in enumerate(lines[:3]):
        draw.text(
            (MARGIN, comp_y + 22 + line_index * 16), line,
            fill=(90, 96, 106), font=body_font)

    comp_image_y = comp_y + TEXT_BLOCK_HEIGHT
    half_width = (width - 3 * MARGIN) // 2
    before = _LegacySurface(
        comparison['view_box']).legacy_origin_render().resize(
        (half_width, half_width))
    after = render_panel(
        comparison['body'], comparison['view_box']).resize(
        (half_width, half_width))
    for tag, image, x in (
            ('BEFORE (legacy device-sized origin)', before, MARGIN),
            ('AFTER (local user-space origin)', after,
             2 * MARGIN + half_width)):
        draw.rectangle(
            (x - 1, comp_image_y - 1, x + half_width,
             comp_image_y + half_width),
            fill=PANEL_BG, outline=BORDER)
        sheet.paste(image, (x, comp_image_y), image)
        draw.text(
            (x, comp_image_y + half_width + 6), tag,
            fill=(180, 60, 60) if tag.startswith('BEFORE')
            else (30, 120, 60), font=tag_font)

    html_parts.append('</div>')
    output_png = os.path.join(here, 'transform_demo.png')
    sheet.save(output_png)
    with open(os.path.join(here, 'index.html'), 'w') as html:
        html.write('\n'.join(html_parts))
    print(f'wrote {output_png} ({sheet.width}x{sheet.height})')
    print(f'wrote {os.path.join(here, "index.html")}')


if __name__ == '__main__':
    main()
