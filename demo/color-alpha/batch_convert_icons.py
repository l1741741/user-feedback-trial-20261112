#!/usr/bin/env python3
"""Batch convert SVG icons to PNG with the local CairoSVG stack.

This mirrors a typical web-design-tool export pipeline: a directory of SVG
files whose colors arrive through presentation attributes, inline
``style`` declarations and gradient stops is converted to PNG icons in one
run. Every input file is handled independently, so one malformed color (or
document) never prevents the remaining icons from being converted.

Usage::

    python demo/color-alpha/batch_convert_icons.py [input_dir] [output_dir]

With no arguments it converts the sample fixtures shipped next to this
script, so the demo is fully reproducible offline.

"""

import pathlib
import sys

from cairosvg import svg2png

SAMPLE_ICONS = {
    'icon-hex-alpha.svg': '''<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"
    viewBox="0 0 48 48">
  <rect width="48" height="48" rx="10" fill="#3366ff80"/>
  <circle cx="24" cy="24" r="12" fill="#ff336688"/>
</svg>
''',
    'icon-modern-rgb.svg': '''<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"
    viewBox="0 0 48 48">
  <rect width="48" height="48" rx="10"
        style="fill: rgb(51 102 255 / 50%)"/>
  <circle cx="24" cy="24" r="12"
          style="fill: rgba(100%, 20%, 40%, 53.3%)"/>
</svg>
''',
    'icon-gradient.svg': '''<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48"
    viewBox="0 0 48 48">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="rgb(255 0 0 / 60%)"/>
      <stop offset="1" stop-color="#0066ff99"/>
    </linearGradient>
  </defs>
  <rect width="48" height="48" rx="10" fill="url(#g)"/>
  <rect x="14" y="14" width="20" height="20" rx="4"
        fill="rgba(255,255,255,0.35)" fill-opacity="0.8"/>
</svg>
''',
    'icon-legacy-and-named.svg': '''<svg xmlns="http://www.w3.org/2000/svg"
    width="48" height="48" viewBox="0 0 48 48">
  <rect width="48" height="48" rx="10" fill="teal"/>
  <path d="M12 36 L24 12 L36 36 Z"
        fill="rgba(255, 255, 255, 0.6)" stroke="white"
        stroke-width="2" stroke-opacity="0.8"/>
</svg>
''',
    'icon-invalid-color.svg': '''<svg xmlns="http://www.w3.org/2000/svg"
    width="48" height="48" viewBox="0 0 48 48">
  <!-- hsl() is outside the supported subset: it must not break the batch -->
  <rect width="48" height="48" rx="10" fill="hsl(220 80% 60%)"/>
  <circle cx="24" cy="24" r="12" fill="rgb(255 0 0 / 75%)"/>
</svg>
''',
}


def write_samples(input_dir):
    input_dir.mkdir(parents=True, exist_ok=True)
    for name, content in SAMPLE_ICONS.items():
        path = input_dir / name
        if not path.exists():
            path.write_text(content, encoding='utf-8')


def convert_one(svg_path, png_path, scale=2):
    """Convert one SVG to a PNG, returning an error string or ``None``."""
    try:
        png = svg2png(url=str(svg_path), output_width=48 * scale,
                      output_height=48 * scale)
        png_path.write_bytes(png)
    except Exception as exc:  # one bad file must not abort the batch
        return f'{type(exc).__name__}: {exc}'
    return None


def main(input_dir, output_dir):
    input_dir = pathlib.Path(input_dir)
    output_dir = pathlib.Path(output_dir)
    write_samples(input_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    converted, failed = [], []
    for svg_path in sorted(input_dir.glob('*.svg')):
        png_path = output_dir / f'{svg_path.stem}.png'
        error = convert_one(svg_path, png_path)
        if error is None:
            converted.append(png_path.name)
        else:
            failed.append((svg_path.name, error))

    print(f'converted {len(converted)} icon(s) -> {output_dir}')
    for name in converted:
        print(f'  OK   {name}')
    for name, error in failed:
        print(f'  FAIL {name}: {error}')
    # The invalid-color sample still converts: its bad color falls back to
    # opaque black instead of raising, exercising the compatibility path.
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    here = pathlib.Path(__file__).parent
    source = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 \
        else here / 'icons'
    target = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 \
        else here / 'output' / 'icons'
    main(source, target)
