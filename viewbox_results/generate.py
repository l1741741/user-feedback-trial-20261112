"""Render the four-color corner + center-cross icon into every required
viewport / preserveAspectRatio combination and save real PNG results.

Run with the project virtualenv, e.g.::

    .venv/bin/python -I viewbox_results/generate.py
"""
import os

from PIL import Image, ImageDraw

import cairosvg

HERE = os.path.dirname(os.path.abspath(__file__))

# 40x30 icon: four colored corner quadrants + 4-unit white center cross.
ICON_BODY = '''
  <rect x="0"  y="0"  width="20" height="15" fill="red"/>
  <rect x="20" y="0"  width="20" height="15" fill="lime"/>
  <rect x="0"  y="15" width="20" height="15" fill="blue"/>
  <rect x="20" y="15" width="20" height="15" fill="yellow"/>
  <rect x="18" y="0" width="4" height="30" fill="white"/>
  <rect x="0" y="13" width="40" height="4" fill="white"/>'''


def render_png(svg, path, scale=4):
    """Render at ``scale`` so small geometry is easy to inspect."""
    png = cairosvg.svg2png(svg.encode(), scale=scale)
    with open(path, 'wb') as fd:
        fd.write(png)
    return Image.open(path).convert('RGB')


def nested(width, height, par, viewbox='0 0 40 30', x=0, y=0,
           sibling=None, background='gray'):
    sib = sibling or ''
    return f'''<svg xmlns="http://www.w3.org/2000/svg"
        width="{width}" height="{height}">
      <rect width="{width}" height="{height}" fill="{background}"/>
      <svg x="{x}" y="{y}" width="{width - x}" height="{height - y}"
           viewBox="{viewbox}" preserveAspectRatio="{par}">{ICON_BODY}</svg>
      {sib}
    </svg>'''


def root(width, height, par, viewbox='0 0 40 30'):
    return f'''<svg xmlns="http://www.w3.org/2000/svg"
        width="{width}" height="{height}" viewBox="{viewbox}"
        preserveAspectRatio="{par}">
      <rect width="{width}" height="{height}" fill="gray"/>
      {ICON_BODY}</svg>'''


def label(draw, text, x, y):
    draw.text((x, y), text, fill='black')


def main():
    tiles = []
    containers = [('portrait', 30, 60), ('landscape', 60, 30), ('square', 40, 40)]
    modes = ['xMidYMid meet', 'xMidYMid slice', 'none']

    # 1) Nested containers, all three modes.
    for cname, w, h in containers:
        for mode in modes:
            svg = nested(w, h, mode)
            fname = f'nested_{cname}_{mode.split()[1] if " " in mode else mode}.png'
            img = render_png(svg, os.path.join(HERE, fname))
            tiles.append((f'{cname} {mode}', img))

    # 2) Root-level viewport, portrait and landscape.
    for cname, w, h in (('portrait', 30, 60), ('landscape', 60, 30)):
        for mode in modes:
            svg = root(w, h, mode)
            fname = f'root_{cname}_{mode.split()[1] if " " in mode else mode}.png'
            img = render_png(svg, os.path.join(HERE, fname))
            tiles.append((f'root {cname} {mode}', img))

    # 3) Non-zero viewBox origin (nested, meet/slice/none).
    nz_tiles = []
    for mode in modes:
        svg = nested(60, 60, mode, viewbox='-10 -20 40 30', x=10, y=10)
        fname = f'nonzero_origin_{mode.split()[1] if " " in mode else mode}.png'
        img = render_png(svg, os.path.join(HERE, fname))
        nz_tiles.append((f'vb -10 -20 {mode}', img))

    # 4) slice overflow clipped, sibling layer preserved.
    slice_sibling = '''<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60">
      <rect width="120" height="60" fill="white"/>
      <svg x="0" y="0" width="80" height="60" viewBox="0 0 40 40"
           preserveAspectRatio="xMidYMid slice">
        <rect x="-50" y="-50" width="140" height="140" fill="red"/>
      </svg>
      <rect x="80" y="0" width="40" height="60" fill="blue"/>
    </svg>'''
    img = render_png(slice_sibling, os.path.join(HERE, 'slice_clip_sibling.png'))
    sibling_tile = ('slice clipped, sibling blue', img)

    # 5) none on first image must not change a following default-meet image.
    none_then_meet = '''<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60">
      <rect width="120" height="60" fill="white"/>
      <svg x="0" y="0" width="60" height="60" viewBox="0 0 40 20"
           preserveAspectRatio="none">
        <rect width="40" height="20" fill="red"/>
      </svg>
      <svg x="60" y="0" width="60" height="60" viewBox="0 0 40 20">
        <rect width="40" height="20" fill="blue"/>
      </svg>
    </svg>'''
    img = render_png(
        none_then_meet, os.path.join(HERE, 'none_then_default_meet.png'))
    none_tile = ('none then meet', img)

    # 6) Degenerate viewBox: identity mapping, no crash, no zero scale.
    degenerate = '''<svg xmlns="http://www.w3.org/2000/svg" width="60" height="60"
        viewBox="0 0 0 0">
      <rect width="20" height="20" fill="red"/>
    </svg>'''
    img = render_png(
        degenerate, os.path.join(HERE, 'degenerate_viewbox_identity.png'))
    deg_tile = ('degenerate viewBox (0 0 0 0)', img)

    # Build labeled contact sheets for quick visual review.
    def sheet(rows, out_name):
        font = None
        pad, label_h, gap = 8, 16, 12
        widths = [im.width for _, im in rows]
        heights = [im.height + label_h for _, im in rows]
        sheet_w = max(widths) + 2 * pad
        sheet_h = sum(heights) + gap * (len(rows) - 1) + 2 * pad
        canvas = Image.new('RGB', (sheet_w, sheet_h), 'white')
        draw = ImageDraw.Draw(canvas)
        y = pad
        for text, im in rows:
            label(draw, text, pad, y)
            canvas.paste(im, (pad, y + label_h))
            y += im.height + label_h + gap
        canvas.save(os.path.join(HERE, out_name))

    sheet(tiles, 'contact_nested_and_root.png')
    sheet(nz_tiles, 'contact_nonzero_origin.png')
    sheet([sibling_tile, none_tile, deg_tile], 'contact_isolation_and_errors.png')
    print('wrote', len(tiles) + len(nz_tiles) + 3, 'images to', HERE)


if __name__ == '__main__':
    main()
