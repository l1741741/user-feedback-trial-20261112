"""Extended probes for textPath edge cases."""

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / 'text_path_tests'))

from probe import RECORDS, render  # noqa: E402

# Closed circular arc path: text should follow the circle continuously
render('circle-closed', '''
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
  <path id="c" d="M100,40 A60,60 0 1,1 99.99,40" fill="none"/>
  <text font-family="Arial" font-size="10">
    <textPath href="#c">AROUND</textPath>
  </text>
</svg>''')

# Two disjoint subpaths: letters after the first subpath must continue on
# the second one instead of disappearing
render('multi-subpath', '''
<svg xmlns="http://www.w3.org/2000/svg" width="220" height="120">
  <path id="p" d="M10 30 L60 30 M10 90 L200 90" fill="none"/>
  <text font-family="Arial" font-size="10">
    <textPath href="#p">ABCDEFGHIJ</textPath>
  </text>
</svg>''')

# Path with a transform applied
render('transformed-path', '''
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
  <g transform="translate(40 60) rotate(90)">
    <path id="p" d="M0 0 L100 0" fill="none"/>
    <text font-family="Arial" font-size="10">
      <textPath href="#p">AB</textPath>
    </text>
  </g>
</svg>''')

# text-anchor middle with startOffset 50%
render('anchor-middle', '''
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
  <path id="p" d="M10 50 L190 50" fill="none"/>
  <text font-family="Arial" font-size="10" text-anchor="middle">
    <textPath href="#p" startOffset="50%">AB</textPath>
  </text>
</svg>''')

# startOffset beyond path length: clamped, no glyphs leak outside
render('offset-clamp', '''
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
  <path id="p" d="M10 50 L60 50" fill="none"/>
  <text font-family="Arial" font-size="10">
    <textPath href="#p" startOffset="150%">AB</textPath>
  </text>
</svg>''')

# Same path referenced twice: geometry must be identical both times
render('repeated-reference', '''
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
  <path id="p" d="M20 40 Q100 10 180 40" fill="none"/>
  <text font-family="Arial" font-size="10">
    <textPath href="#p">FIRST</textPath>
  </text>
  <text font-family="Arial" font-size="10">
    <textPath href="#p">SECOND</textPath>
  </text>
</svg>''')

# Polyline with tick marks (the requested showcase geometry)
render('polyline-ticks', '''
<svg xmlns="http://www.w3.org/2000/svg" width="220" height="160">
  <path id="pl" d="M20 120 L60 60 L110 100 L160 40 L200 80" fill="none"/>
  <text font-family="Arial" font-size="12">
    <textPath href="#pl">POLYLINE TEXT</textPath>
  </text>
</svg>''')

# dx along the path shifts letters along the path direction
render('dx-along-path', '''
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
  <path id="p" d="M10 50 L190 50" fill="none"/>
  <text font-family="Arial" font-size="10">
    <textPath href="#p"><tspan dx="20">AB</tspan></textPath>
  </text>
</svg>''')
