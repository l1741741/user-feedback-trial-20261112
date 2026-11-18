"""Render probe SVGs and dump sample pixels for inspection.

Run with the repo venv (needs cairo DYLD path on this machine).
Read-only with respect to the cairosvg package: it only renders files.
"""
import io
import sys
from pathlib import Path

from PIL import Image

import cairosvg

SVG_DIR = Path(__file__).parent / 'svg'
OUT_DIR = Path(__file__).parent / 'out'


def render(name):
    svg_path = SVG_DIR / f'{name}.svg'
    png = cairosvg.svg2png(url=str(svg_path))
    (OUT_DIR / f'{name}.png').write_bytes(png)
    return Image.open(io.BytesIO(png)).convert('RGBA')


def sample(img, points):
    return {f'({x},{y})': img.getpixel((x, y)) for x, y in points}


def main():
    OUT_DIR.mkdir(exist_ok=True)
    probes = {
        '01_luminance': [(25, 50), (125, 50), (250, 50)],
        '02_color_luminance': [(50, 50), (150, 50), (250, 50)],
        '03_mask_alpha': [(50, 50), (150, 50), (250, 50)],
        '04_reuse_userspace': [(120, 100), (300, 100), (240, 100), (10, 10)],
        '05_outside_transparent': [(10, 10), (100, 100), (190, 190)],
        '06_group_opacity': [(100, 50)],
        '07_gradient': [(5, 50), (50, 50), (100, 50), (150, 50), (195, 50)],
        '08_nested_groups': [(150, 75), (20, 20), (250, 120), (290, 140)],
    }
    for name, points in probes.items():
        img = render(name)
        print(f'== {name} {img.size} ==')
        for point, rgba in sample(img, points).items():
            print(f'  {point}: RGBA={rgba}')


if __name__ == '__main__':
    sys.exit(main())
