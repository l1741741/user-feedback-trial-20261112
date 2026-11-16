"""Probe nested tspan behavior inside a textPath."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'text_path_tests'))

from probe import render  # noqa: E402

# Nested tspans inside one textPath: letters must keep advancing
render('nested-tspans', '''
<svg xmlns="http://www.w3.org/2000/svg" width="220" height="100">
  <path id="p" d="M10 50 L210 50" fill="none"/>
  <text font-family="Arial" font-size="10">
    <textPath href="#p">
      <tspan>AB</tspan><tspan>CD</tspan>
    </textPath>
  </text>
</svg>''')

# Two separate text elements: the second must start fresh at path origin
render('sibling-textpaths', '''
<svg xmlns="http://www.w3.org/2000/svg" width="220" height="100">
  <path id="p" d="M10 50 L210 50" fill="none"/>
  <text font-family="Arial" font-size="10">
    <textPath href="#p">FIRST</textPath>
  </text>
  <text font-family="Arial" font-size="10">
    <textPath href="#p">SECOND</textPath>
  </text>
</svg>''')

# Plain text following a textPath on a short path: cursor must be clean
render('plain-after-textpath', '''
<svg xmlns="http://www.w3.org/2000/svg" width="220" height="100">
  <path id="p" d="M10 50 L14 50" fill="none"/>
  <text font-family="Arial" font-size="10">
    <textPath href="#p">TOOLONG</textPath>
  </text>
  <text x="10" y="90" font-family="Arial" font-size="10">AFTER</text>
</svg>''')

# A path with an empty subpath between two real ones
render('empty-subpath-between', '''
<svg xmlns="http://www.w3.org/2000/svg" width="220" height="120">
  <path id="p" d="M10 30 L60 30 M100 30 L100 30 M10 90 L160 90" fill="none"/>
  <text font-family="Arial" font-size="10">
    <textPath href="#p">ABCDEFGHIJKLMN</textPath>
  </text>
</svg>''')
