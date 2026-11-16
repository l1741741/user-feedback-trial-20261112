"""Render the group-compositing comparison sheet.

Renders each fixture SVG with CairoSVG, samples interior pixels of the
non-overlapping and overlapping regions, and writes:

- ``compositing_fixtures/compare.png``: a labelled visual grid
- ``compositing_fixtures/pixels.txt``: the sampled RGBA values

The overlap and non-overlap bands are sampled separately: a single number
(or the file size) cannot tell group from per-child opacity apart.

"""

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

from cairosvg import svg2png

FIXTURES = Path(__file__).resolve().parent.parent / 'compositing_fixtures'
CELL = 100          # fixture viewport is 100x100
SCALE = 2
LABEL_WIDTH = 250
PADDING = 8

HEADER = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
    '<rect width="100" height="100" fill="#ffffff"/>')

SHAPES = (
    '<rect x="10" y="10" width="60" height="60" fill="#f00"/>'
    '<rect x="30" y="30" width="60" height="60" fill="#00f"/>')
SHAPES_OPACITY = (
    '<rect x="10" y="10" width="60" height="60" fill="#f00" opacity="0.5"/>'
    '<rect x="30" y="30" width="60" height="60" fill="#00f" opacity="0.5"/>')
MASK = (
    '<defs><clipPath id="c">'
    '<rect x="0" y="0" width="50" height="50"/></clipPath>'
    '<mask id="full"><rect width="100" height="100" fill="#fff"/></mask>'
    '<mask id="half"><rect width="100" height="100" fill="#fff" '
    'fill-opacity="0.5"/></mask></defs>')

# Sample points: non-overlapping red, overlap, non-overlapping blue, outside.
POINTS = {'red-only': (15, 15), 'overlap': (40, 40),
          'blue-only': (80, 80), 'background': (95, 95)}

CASES = [
    ('group opacity 0.5', f'<g opacity="0.5">{SHAPES}</g>'),
    ('per-child opacity 0.5', f'<g>{SHAPES_OPACITY}</g>'),
    ('nested group 0.5*0.5',
     f'<g opacity="0.5"><g opacity="0.5">{SHAPES}</g></g>'),
    ('group 0.5 + clip',
     f'{MASK}<g opacity="0.5" clip-path="url(#c)">{SHAPES}</g>'),
    ('group 0.5 + full mask',
     f'{MASK}<g opacity="0.5" mask="url(#full)">{SHAPES}</g>'),
    ('group 0.5 + half mask',
     f'{MASK}<g opacity="0.5" mask="url(#half)">{SHAPES}</g>'),
    ('use opacity 0.5',
     f'<defs><g id="u">{SHAPES}</g></defs>'
     '<use href="#u" opacity="0.5"/>'),
    ('empty group, then sibling',
     '<g opacity="0.5"/>'
     '<rect x="50" y="50" width="40" height="40" fill="#0a0"/>'),
    ('zero-opacity group, then sibling',
     '<g opacity="0"><rect x="10" y="10" width="60" height="60" '
     'fill="#f00"/></g>'
     '<rect x="50" y="50" width="40" height="40" fill="#0a0"/>'),
]


def render(body):
    png = svg2png(bytestring=(HEADER + body + '</svg>').encode())
    return Image.open(BytesIO(png)).convert('RGBA')


def main():
    images = [(label, render(body)) for label, body in CASES]

    lines = []
    for label, image in images:
        pixels = {name: image.getpixel(point) for name, point in POINTS.items()}
        lines.append(label)
        for name, rgba in pixels.items():
            lines.append(f'  {name:11} rgb{rgba[:3]} alpha={rgba[3]}')
    (FIXTURES / 'pixels.txt').write_text('\n'.join(lines) + '\n')

    cell = CELL * SCALE
    columns = 3
    rows = (len(images) + columns - 1) // columns
    width = columns * (LABEL_WIDTH + cell + PADDING) + PADDING
    height = rows * (cell + 2 * PADDING) + PADDING
    sheet = Image.new('RGB', (width, height), (245, 245, 245))
    draw = ImageDraw.Draw(sheet)

    for index, (label, image) in enumerate(images):
        row, column = divmod(index, columns)
        x = PADDING + column * (LABEL_WIDTH + cell + PADDING)
        y = PADDING + row * (cell + 2 * PADDING)
        draw.multiline_text(
            (x, y + 4), label.replace(' + ', '\n+ '), fill=(0, 0, 0))
        tile = image.resize((cell, cell), Image.NEAREST)
        sheet.paste(tile, (x + LABEL_WIDTH, y))
        # Mark the sampled points on the tile.
        for point in POINTS.values():
            cx = x + LABEL_WIDTH + (point[0] + .5) * SCALE
            cy = y + (point[1] + .5) * SCALE
            draw.ellipse((cx - 2, cy - 2, cx + 2, cy + 2), outline=(0, 0, 0))

    sheet.save(FIXTURES / 'compare.png')
    print((FIXTURES / 'pixels.txt').read_text())


if __name__ == '__main__':
    main()
