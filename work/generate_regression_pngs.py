#!/usr/bin/env python3
"""Render one PNG per fixture used by cairosvg/test_viewport.py.

The SVG sources are kept in sync with the pixel tests: each entry pairs the
test function name with its document. Re-run after touching the tests to
refresh the regression images in regression_pngs/test_viewport/.
"""

import os

import cairosvg

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(
    HERE, os.pardir, 'regression_pngs', 'test_viewport')

FIXTURES = {
    'percent_width_height_on_nested_svg': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <rect width="100" height="100" fill="white"/>
      <svg x="10" y="10" width="50%" height="40%">
        <rect width="100%" height="100%" fill="red"/>
      </svg>
    </svg>''',
    'percent_shapes_inside_nested_viewport': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <rect width="200" height="100" fill="white"/>
      <svg x="20" y="10" width="80" height="40">
        <rect x="0" y="0" width="50%" height="50%" fill="red"/>
        <circle cx="50%" cy="50%" r="25%" fill="blue"/>
      </svg>
    </svg>''',
    'percent_resolution_at_each_nesting_level': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
      <rect width="200" height="200" fill="white"/>
      <svg x="0" y="0" width="50%" height="50%">
        <circle cx="50%" cy="50%" r="50%" fill="blue"/>
      </svg>
    </svg>''',
    'percent_stroke_width_uses_nested_viewport': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
      <rect width="200" height="200" fill="white"/>
      <svg x="20" y="20" width="40" height="40">
        <line x1="0" y1="20" x2="40" y2="20" stroke="black"
              stroke-width="10%"/>
      </svg>
      <line x1="0" y1="100" x2="40" y2="100" stroke="black"
            stroke-width="10%"/>
    </svg>''',
    'nonzero_viewbox_origin_rect': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">
      <rect width="80" height="80" fill="white"/>
      <svg x="0" y="0" width="80" height="80" viewBox="20 20 40 40">
        <rect x="20" y="20" width="50%" height="50%" fill="blue"/>
      </svg>
    </svg>''',
    'nonzero_viewbox_origin_circle': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">
      <rect width="80" height="80" fill="white"/>
      <svg x="0" y="0" width="80" height="80" viewBox="20 20 40 40">
        <circle cx="50%" cy="50%" r="25%" fill="blue"/>
      </svg>
    </svg>''',
    'meet_letterbox': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="200">
      <rect width="100" height="200" fill="white"/>
      <svg x="0" y="0" width="100" height="200" viewBox="0 0 100 100"
           preserveAspectRatio="xMidYMid meet">
        <rect width="100" height="100" fill="red"/>
      </svg>
    </svg>''',
    'slice_clip': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <rect width="200" height="100" fill="white"/>
      <svg x="0" y="0" width="200" height="100" viewBox="0 0 100 100"
           preserveAspectRatio="xMidYMid slice">
        <rect width="100" height="100" fill="red"/>
      </svg>
    </svg>''',
    'preserve_aspect_ratio_none': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <rect width="200" height="100" fill="white"/>
      <svg x="20" y="10" width="80" height="40" viewBox="0 0 40 40"
           preserveAspectRatio="none">
        <rect width="40" height="40" fill="red"/>
        <circle cx="50%" cy="50%" r="25%" fill="blue"/>
      </svg>
    </svg>''',
    'sibling_percentage_restore': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <rect width="100" height="100" fill="white"/>
      <svg x="0" y="0" width="40" height="40" viewBox="0 0 8 8">
        <rect width="8" height="8" fill="red"/>
      </svg>
      <rect x="60" y="0" width="50%" height="50%" fill="blue"/>
    </svg>''',
    'deeply_nested_restore': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <rect width="100" height="100" fill="white"/>
      <svg x="10" y="10" width="80" height="80" viewBox="0 0 80 80">
        <rect width="80" height="80" fill="#eeeeee"/>
        <svg x="10" y="10" width="40" height="40" viewBox="0 0 20 20">
          <rect x="-10" y="-10" width="40" height="40" fill="red"/>
          <rect width="20" height="20" fill="blue"/>
        </svg>
        <rect x="10" y="55" width="50%" height="10" fill="green"/>
      </svg>
      <rect x="0" y="92" width="50%" height="8" fill="black"/>
    </svg>''',
    'empty_viewport_no_leak': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">
      <rect width="120" height="120" fill="white"/>
      <svg x="0" y="0" width="60" height="60" viewBox="0 0 30 30">
        <svg width="0" height="0"/>
        <circle cx="50%" cy="50%" r="50%" fill="blue"/>
      </svg>
      <rect x="60" y="0" width="50%" height="50%" fill="red"/>
    </svg>''',
    'symbol_use_parity': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
      <defs>
        <symbol id="s" viewBox="0 0 20 20">
          <circle cx="50%" cy="50%" r="50%" fill="blue"/>
        </symbol>
      </defs>
      <use href="#s" x="10" y="10" width="40" height="40"/>
    </svg>''',
    'use_single_width': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs>
        <symbol id="s" viewBox="0 0 10 20">
          <rect width="10" height="20" fill="red"/>
        </symbol>
      </defs>
      <rect width="100" height="100" fill="white"/>
      <use href="#s" x="0" y="0" width="40"/>
    </svg>''',
    'use_single_height': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs>
        <symbol id="s" viewBox="0 0 10 20">
          <rect width="10" height="20" fill="red"/>
        </symbol>
      </defs>
      <rect width="100" height="100" fill="white"/>
      <use href="#s" x="0" y="30" height="40"/>
    </svg>''',
    'use_viewbox_attribute': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <defs>
        <symbol id="s">
          <rect width="25" height="25" fill="blue"/>
        </symbol>
      </defs>
      <rect width="200" height="100" fill="white"/>
      <use href="#s" x="10" y="10" width="80" height="80"
           viewBox="0 0 50 50"/>
    </svg>''',
    'masked_use_idempotent': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs>
        <mask id="m">
          <rect width="100" height="50" fill="white"/>
        </mask>
        <rect id="r" width="100" height="100" fill="blue"/>
      </defs>
      <use href="#r" mask="url(#m)"/>
    </svg>''',
    'embedded_svg_image': '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">
      <rect width="120" height="80" fill="white"/>
      <image id="embedded" x="20" y="10" width="40" height="40"/>
    </svg>''',
}


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, source in FIXTURES.items():
        if name == 'embedded_svg_image':
            # data-URL embedded SVG, built at runtime to keep the fixture
            # readable above
            import base64
            inner = (
                b'<svg xmlns="http://www.w3.org/2000/svg" width="40" '
                b'height="40"><rect width="40" height="40" '
                b'fill="blue"/></svg>')
            uri = ('data:image/svg+xml;base64,'
                   + base64.b64encode(inner).decode())
            source = source.replace('id="embedded"',
                                    f'href="{uri}" id="embedded"')
        cairosvg.svg2png(
            bytestring=source.encode(),
            write_to=os.path.join(OUT, f'{name}.png'))
        print('wrote', os.path.join('regression_pngs', 'test_viewport',
                                    f'{name}.png'))


if __name__ == '__main__':
    main()
