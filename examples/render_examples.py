"""Real rendering examples for the ``<image>`` element fixes.

This script is fully self-contained and never touches the network:

1. It builds local raster assets (four colored corner quadrants, a
   transparent border and a center marker) in ``examples/assets``.
2. It renders the preserveAspectRatio semantic matrix -- meet / slice / none
   in landscape and portrait containers, a nested transformed container,
   EXIF orientation, real-alpha compositing, repeated references and zero
   dimensions -- to PNG files in ``examples/output``.
3. It assembles a labeled contact sheet from those PNGs.

Run it with the project virtualenv::

    python examples/render_examples.py
"""

import io
import os

from PIL import Image, ImageDraw, ImageFont

import cairosvg

HERE = os.path.dirname(os.path.abspath(__file__))
ASSET_DIR = os.path.join(HERE, 'assets')
OUT_DIR = os.path.join(HERE, 'output')
os.makedirs(ASSET_DIR, exist_ok=True)
os.makedirs(OUT_DIR, exist_ok=True)

W, H, BORDER = 40, 30, 4
RED, GREEN, BLUE, YELLOW = (
    (255, 0, 0, 255), (0, 200, 0, 255), (0, 0, 255, 255), (255, 200, 0, 255))


def build_assets():
    image = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    qw, qh = (W - 2 * BORDER) // 2, (H - 2 * BORDER) // 2
    draw.rectangle((BORDER, BORDER, BORDER + qw - 1, BORDER + qh - 1), RED)
    draw.rectangle((BORDER + qw, BORDER, W - BORDER - 1, BORDER + qh - 1), GREEN)
    draw.rectangle((BORDER, BORDER + qh, BORDER + qw - 1, H - BORDER - 1), BLUE)
    draw.rectangle((BORDER + qw, BORDER + qh, W - BORDER - 1, H - BORDER - 1),
                   YELLOW)
    cx, cy = W // 2 - 1, H // 2 - 1
    draw.line((cx - 5, cy, cx + 5, cy), fill=(255, 255, 255, 255))
    draw.line((cx, cy - 4, cx, cy + 4), fill=(255, 255, 255, 255))
    draw.rectangle((cx - 1, cy - 1, cx + 1, cy + 1), fill=(0, 0, 0, 255))
    marker = os.path.join(ASSET_DIR, 'markers.png')
    image.save(marker, 'PNG')

    # Opaque variant makes any clip overdraw unmistakable.
    opaque = Image.new('RGBA', image.size, (255, 0, 255, 255))
    opaque.alpha_composite(image)
    opaque_path = os.path.join(ASSET_DIR, 'opaque.png')
    opaque.save(opaque_path, 'PNG')

    # The same picture as JPEG and PNG carrying EXIF orientation 6.
    rgb = image.convert('RGB')
    exif = rgb.getexif()
    exif[274] = 6
    jpg = os.path.join(ASSET_DIR, 'markers_exif6.jpg')
    rgb.save(jpg, 'JPEG', exif=exif)
    png_exif = os.path.join(ASSET_DIR, 'markers_exif6.png')
    rgb.save(png_exif, 'PNG', exif=exif)
    return marker, opaque_path, jpg, png_exif


def render_png(name, body, width, height):
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">'
        f'<rect width="{width}" height="{height}" fill="white"/>{body}</svg>')
    data = cairosvg.svg2png(bytestring=svg.encode(), unsafe=True)
    path = os.path.join(OUT_DIR, name + '.png')
    with open(path, 'wb') as handle:
        handle.write(data)
    return path, Image.open(io.BytesIO(data)).convert('RGBA')


def image_tag(href, x, y, w, h, par):
    return (f'<image href="{href}" x="{x}" y="{y}" width="{w}" height="{h}" '
            f'preserveAspectRatio="{par}"/>')


def build_examples(marker, opaque, jpg, png_exif):
    panels = []  # (label, image)

    def add(label, name, body, width, height):
        path, image = render_png(name, body, width, height)
        panels.append((label, image))
        return path

    # --- Landscape container (region wider than the image aspect) --------
    for par in ('xMidYMid meet', 'xMidYMid slice', 'none'):
        add(f'landscape {par}', 'landscape_' + par.split()[1] if ' ' in par
            else 'landscape_none',
            image_tag(marker, 15, 15, 90, 50, par), 120, 80)

    # --- Portrait container ----------------------------------------------
    for par in ('xMidYMid meet', 'xMidYMid slice', 'none'):
        label = 'portrait ' + (par.split()[1] if ' ' in par else 'none')
        add(label, label.replace(' ', '_'),
            image_tag(marker, 35, 10, 40, 80, par), 110, 100)

    # --- Non-zero alignment positions in a wide slice container ----------
    add('slice xMinYMin', 'align_minmin',
        image_tag(marker, 10, 20, 100, 40, 'xMinYMin slice'), 120, 80)
    add('slice xMaxYMax', 'align_maxmax',
        image_tag(marker, 10, 20, 100, 40, 'xMaxYMax slice'), 120, 80)

    # --- Nested transformed container ------------------------------------
    add('nested rotate(90) slice', 'nested_rotate',
        '<g transform="translate(70,10) rotate(90)">'
        + image_tag(marker, 0, 0, 70, 50, 'xMidYMid slice') + '</g>',
        140, 120)

    # --- EXIF orientation honored for JPEG and PNG alike -----------------
    add('EXIF-6 jpeg', 'exif_jpeg',
        image_tag(jpg, 10, 10, 75, 50, 'none'), 95, 70)
    add('EXIF-6 png', 'exif_png',
        image_tag(png_exif, 10, 10, 75, 50, 'none'), 95, 70)

    # --- Real alpha: transparent border over a colored backdrop ----------
    add('alpha over green', 'alpha_compositing',
        '<rect width="100" height="70" fill="rgb(10,200,10)"/>'
        + image_tag(marker, 15, 20, 70, 30, 'none'), 100, 70)

    # --- Repeated references at different sizes stay independent ---------
    add('repeated references', 'repeated',
        image_tag(marker, 5, 25, 30, 22, 'xMidYMid meet')
        + image_tag(marker, 45, 10, 60, 52, 'xMidYMid meet')
        + image_tag(marker, 115, 25, 20, 45, 'xMidYMid meet'), 145, 75)

    # --- Explicit zero dimensions render nothing -------------------------
    add('zero width/height', 'zero_size',
        '<rect width="90" height="60" fill="rgb(0,170,0)"/>'
        f'<image href="{marker}" x="10" y="10" width="0" height="40"/>'
        f'<image href="{marker}" x="50" y="10" width="30" height="0"/>',
        90, 60)

    # --- Clip guard: opaque image must stay inside its region ------------
    add('clip guard slice', 'clip_guard',
        image_tag(opaque, 25, 20, 70, 50, 'xMaxYMax slice'), 120, 90)

    return panels


def build_contact_sheet(panels):
    font = ImageFont.load_default()
    label_h, pad, cols = 16, 12, 3
    cell_w = max(img.width for _, img in panels) + pad
    cell_h = max(img.height for _, img in panels) + label_h + pad
    rows = (len(panels) + cols - 1) // cols
    sheet = Image.new('RGBA', (cols * cell_w + pad, rows * cell_h + pad),
                      (235, 235, 235, 255))
    draw = ImageDraw.Draw(sheet)
    for index, (label, img) in enumerate(panels):
        col, row = index % cols, index // cols
        ox = pad + col * cell_w
        oy = pad + row * cell_h
        draw.text((ox, oy), label, fill=(0, 0, 0, 255), font=font)
        sheet.alpha_composite(img, (ox, oy + label_h))
    path = os.path.join(OUT_DIR, 'contact_sheet.png')
    sheet.convert('RGB').save(path, 'PNG')
    return path


def main():
    marker, opaque, jpg, png_exif = build_assets()
    panels = build_examples(marker, opaque, jpg, png_exif)
    sheet = build_contact_sheet(panels)
    print(f'Generated {len(panels)} example renders in {OUT_DIR}')
    print(f'Contact sheet: {sheet}')


if __name__ == '__main__':
    main()
