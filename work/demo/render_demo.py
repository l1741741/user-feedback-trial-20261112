#!/usr/bin/env python3
"""Render the marker demo SVG into a PNG using the local CairoSVG checkout.

Run from the repository root::

    python work/demo/render_demo.py

The output is written next to the SVG as ``marker_demo.png``. The script
exits non-zero if the Cairo shared library cannot be loaded, so it can be
used directly as a smoke test of the marker rendering pipeline.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cairosvg import svg2png  # noqa: E402

SVG_PATH = Path(__file__).with_name('marker_demo.svg')
PNG_PATH = SVG_PATH.with_suffix('.png')


def main():
    png = svg2png(url=str(SVG_PATH))
    PNG_PATH.write_bytes(png)
    print(f'wrote {PNG_PATH} ({len(png)} bytes)')


if __name__ == '__main__':
    main()
