#!/usr/bin/env python3
"""Render the offline textPath demo SVG to PNG.

Run with the project virtualenv and the native cairo library available, e.g.::

    export DYLD_FALLBACK_LIBRARY_PATH="/path/to/cairo/lib:/opt/homebrew/lib"
    .venv/bin/python -I work/demo/render_demo.py

``-I`` keeps the interpreter isolated so only the project package and its
declared dependencies are used.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cairosvg  # noqa: E402

SVG_PATH = Path(__file__).with_name('text_path_demo.svg')
PNG_PATH = SVG_PATH.with_suffix('.png')

cairosvg.svg2png(
        url=str(SVG_PATH), write_to=str(PNG_PATH), output_width=1520,
        output_height=2360)
print(f'wrote {PNG_PATH}')
