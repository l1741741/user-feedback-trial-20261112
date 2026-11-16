"""Probe where CairoSVG actually puts textPath glyphs.

Records the current transformation matrix before each glyph is drawn by
wrapping the cairo context methods used by cairosvg.text. Run with the
project venv, e.g.::

    .venv/bin/python -I text_path_tests/probe.py
"""

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cairocffi as cairo  # noqa: E402
from cairosvg import svg2png  # noqa: E402

RECORDS = []

_orig_text_path = cairo.Context.text_path
_orig_show_text = cairo.Context.show_text


def _record(ctx, letter, kind):
    if not letter or letter.isspace():
        return
    matrix = ctx.get_matrix()
    px, py = ctx.get_current_point()
    # Map current point through the CTM to get user-space position
    x = matrix.xx * px + matrix.xy * py + matrix.x0
    y = matrix.yx * px + matrix.yy * py + matrix.y0
    angle = __import__('math').atan2(matrix.yx, matrix.xx)
    RECORDS.append((letter, round(x, 2), round(y, 2),
                    round(__import__('math').degrees(angle), 1)))


def text_path(self, string):
    _record(self, string, 'path')
    return _orig_text_path(self, string)


def show_text(self, string):
    _record(self, string, 'text')
    return _orig_show_text(self, string)


cairo.Context.text_path = text_path
cairo.Context.show_text = show_text


def render(name, svg):
    RECORDS.clear()
    svg2png(bytestring=svg.encode('utf-8'))
    print(f'--- {name} ---')
    for record in RECORDS:
        print('  ', record)
    print()


if __name__ == '__main__':
    # 1. Percentage startOffset on a short path
    render('percent-startoffset', '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <path id="p" d="M10 50 L110 50" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath href="#p" startOffset="50%">AB</textPath>
      </text>
    </svg>''')

    # 2. Reverse curve (right-to-left path)
    render('reverse-path', '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <path id="p" d="M150 50 L50 50" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath href="#p">AB</textPath>
      </text>
    </svg>''')

    # 3. Short path then sibling normal text
    render('short-then-sibling', '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <path id="p" d="M10 50 L14 50" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath href="#p">TOOLONG</textPath>
      </text>
      <text x="10" y="90" font-family="Arial" font-size="10">SIBLING</text>
    </svg>''')

    # 4. Zero-length path
    render('zero-length', '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <path id="p" d="M10 50 L10 50" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath href="#p">AB</textPath>
      </text>
      <text x="10" y="90" font-family="Arial" font-size="10">SIBLING</text>
    </svg>''')
