"""Generate a self-explaining demo image of the fixed text chunk layout.

The SVG draws left/right baseline ticks and center reference lines so the
relative position of every independently anchored chunk can be checked
visually. Output: work/demo/text_chunks_demo.png
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cairosvg

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'demo')
os.makedirs(OUT, exist_ok=True)
FONT = 'Helvetica'

# Four panels, each with explicit reference geometry.
svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="720" height="560"
     font-family="{FONT}">
  <rect width="720" height="560" fill="white"/>

  <!-- Panel 1: multiple x values, middle anchor -->
  <text x="24" y="34" font-size="16" fill="#333">
    1) x="160 560", text-anchor=middle: each chunk centers on its own x
  </text>
  <line x1="160" y1="50" x2="160" y2="150" stroke="#bbb" stroke-dasharray="4 4"/>
  <line x1="560" y1="50" x2="560" y2="150" stroke="#bbb" stroke-dasharray="4 4"/>
  <line x1="24" y1="100" x2="696" y2="100" stroke="#eee"/>
  <text font-size="26" text-anchor="middle" x="160 560" y="100 140"
        fill="#c0392b">ABCD</text>

  <!-- Panel 2: nested tspan dx does not leak to the next line -->
  <text x="24" y="194" font-size="16" fill="#333">
    2) nested tspan dx=15 stays on its line; the next tspan restarts at x=80
  </text>
  <line x1="80" y1="210" x2="80" y2="320" stroke="#bbb" stroke-dasharray="4 4"/>
  <text font-size="26" fill="#1e8449">
    <tspan x="80" y="240">A<tspan dx="15">B</tspan>C</tspan>
    <tspan x="80" y="300">DEF</tspan>
  </text>

  <!-- Panel 3: independent text nodes, mixed anchor order -->
  <text x="24" y="354" font-size="16" fill="#333">
    3) separate text elements with end / middle / start anchors on x=360
  </text>
  <line x1="360" y1="370" x2="360" y2="480" stroke="#bbb" stroke-dasharray="4 4"/>
  <text font-size="26" text-anchor="end"    x="360" y="400" fill="#1f618d">end</text>
  <text font-size="26" text-anchor="middle" x="360" y="440" fill="#8e44ad">middle</text>
  <text font-size="26" text-anchor="start"  x="360" y="480" fill="#d68910">start</text>

  <!-- Panel 4: per-glyph rotate and letter-spacing semantics -->
  <text x="24" y="524" font-size="16" fill="#333">
    4) rotate="30 0" applies per glyph; letter-spacing adds advance only
  </text>
  <text font-size="26" x="80" y="548" letter-spacing="8" rotate="30 0 0 0"
        fill="#2c3e50">Spin</text>
</svg>'''

path = os.path.join(OUT, 'text_chunks_demo.png')
cairosvg.svg2png(bytestring=svg.encode(), write_to=path,
                 output_width=720, output_height=560)
print('wrote', path)
