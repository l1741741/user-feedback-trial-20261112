#!/usr/bin/env python3
"""Render the local paint-order demo SVG to PNG and export zoomed crops.

The SVG lives in ``assets/`` and is treated as input data only; this script
stays in its own directory and only receives file paths as arguments. Run
with the project virtualenv, e.g.::

    DYLD_FALLBACK_LIBRARY_PATH=... .venv/bin/python demo/render_demo.py
"""

import sys
from pathlib import Path

from PIL import Image

from cairosvg import svg2png

BASE_DIR = Path(__file__).resolve().parent
ASSET = BASE_DIR / 'assets' / 'paint_order_demo.svg'
OUTPUT_DIR = BASE_DIR / 'output'

# The SVG is 640x560 user units and renders 1:1 to pixels. Each row's path
# starts at (110, 90) and rows are translated by 0, 180 and 360 units.
ZOOM = 4
ROW_OFFSETS = (0, 180, 360)
ROW_LABELS = ('default', 'stroke-first', 'markers-first')
# Per-row crop boxes (base coordinates; the row offset is added on export).
# Start-marker area: the 22px red square is centered on the path start.
MARKER_BOX = (84, 66, 146, 124)
# Fill/stroke overlap on the top edge of the shape, away from any marker.
OVERLAP_BOX = (200, 72, 292, 116)

# Console probe points (base coordinates, row offset added per row).
MARKER_POINT = (110, 90)          # center of the start marker
RING_POINT = (230, 96)            # top edge band covered by stroke and fill
INNER_POINT = (230, 130)          # deep inside the fill


def export_crop(image, label, offset, name, base_box):
    """Save a zoomed crop of one row under ``output/``."""
    box = (base_box[0], base_box[1] + offset,
           base_box[2], base_box[3] + offset)
    crop = image.crop(box)
    width, height = crop.size
    zoomed = crop.resize((width * ZOOM, height * ZOOM), Image.NEAREST)
    crop_path = OUTPUT_DIR / f'{label}_{name}_x{ZOOM}.png'
    zoomed.save(crop_path)
    print(f'wrote {crop_path} ({zoomed.width}x{zoomed.height})')


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    png_path = OUTPUT_DIR / 'paint_order_demo.png'
    png = svg2png(url=str(ASSET))
    png_path.write_bytes(png)
    image = Image.open(png_path).convert('RGBA')
    print(f'wrote {png_path} ({image.width}x{image.height})')

    for label, offset in zip(ROW_LABELS, ROW_OFFSETS):
        export_crop(image, label, offset, 'marker', MARKER_BOX)
        export_crop(image, label, offset, 'overlap', OVERLAP_BOX)

    # Report the probe colors so the painting order is readable in the console
    # as well as in the images.
    pixels = image.load()
    print('\nprobe points (rows top to bottom):')
    for label, offset in zip(ROW_LABELS, ROW_OFFSETS):
        marker = pixels[MARKER_POINT[0], MARKER_POINT[1] + offset]
        ring = pixels[RING_POINT[0], RING_POINT[1] + offset]
        inner = pixels[INNER_POINT[0], INNER_POINT[1] + offset]
        print(f'  {label:14s} marker={marker} ring={ring} inner={inner}')


if __name__ == '__main__':
    sys.exit(main())
