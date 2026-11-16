#!/usr/bin/env python3
"""Render the nested-viewport percentage demo and export diagnostic crops.

The SVG in ``assets/`` is inert input data; this script only reads paths given
on the command line and writes to ``output/``. Run with the project venv::

    DYLD_FALLBACK_LIBRARY_PATH=... .venv/bin/python demo/render_demo.py

Options::

    --svg PATH     input SVG (default: assets/viewport_percent.svg)
    --out DIR      output directory (default: output)
    --zoom N       zoom factor for the crops (default: 4)

Outputs:

* ``viewport_percent.png``: the whole demo, 640x560.
* ``crop_<n>.png``: 4x nearest-neighbour enlargements of the areas that show
  the nesting behaviour, with their pixel geometry printed on stdout.
"""

import argparse
from pathlib import Path

from PIL import Image

from cairosvg import svg2png

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SVG = BASE_DIR / 'assets' / 'viewport_percent.svg'
DEFAULT_OUT = BASE_DIR / 'output'

# (name, left, top, right, bottom) in demo coordinates.
CROPS = (
    ('crop_1_nested_vs_use', 10, 10, 710, 135),
    ('crop_2_three_levels', 10, 240, 350, 465),
    ('crop_3_symbol_reuse', 350, 240, 710, 465),
    ('crop_4_sibling_percent', 545, 10, 710, 235),
    ('crop_5_ruler', 10, 490, 350, 700),
    ('crop_6_viewbox_origin', 350, 490, 710, 700),
)


def render(svg_path, out_dir, zoom):
    out_dir.mkdir(parents=True, exist_ok=True)
    png = svg2png(url=str(svg_path))
    full_path = out_dir / 'viewport_percent.png'
    full_path.write_bytes(png)

    image = Image.open(full_path).convert('RGBA')
    outputs = [full_path]
    for name, left, top, right, bottom in CROPS:
        crop = image.crop((left, top, right, bottom))
        crop = crop.resize(
            (crop.width * zoom, crop.height * zoom), Image.NEAREST)
        path = out_dir / f'{name}.png'
        crop.save(path)
        outputs.append(path)
    return image, outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--svg', type=Path, default=DEFAULT_SVG)
    parser.add_argument('--out', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--zoom', type=int, default=4)
    args = parser.parse_args()

    image, outputs = render(args.svg, args.out, args.zoom)
    print(f'rendered {args.svg} -> {image.width}x{image.height}')
    for path in outputs:
        print(f'  {path}')


if __name__ == '__main__':
    main()
