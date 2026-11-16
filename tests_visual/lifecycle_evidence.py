"""
Before/after evidence for defects that pristine CairoSVG really exhibits.

Runs the same probes twice by import path: once against a pristine checkout
(``git archive`` of HEAD) and once against the fixed working tree. Writes a
plain-text report into the output directory.

Usage::

    .venv/bin/python -I tests_visual/lifecycle_evidence.py <pristine_dir> <out>
"""

import gc
import io
import subprocess
import sys
import weakref
from pathlib import Path

import cairocffi as cairo

BROKEN_PNG_DATA_URI = (
    'data:image/png;base64,'
    'iVBORw0KGgoAAAANSUhEUgAAAAUAAAAFCAAAAABad+Rb')

CLIP_DOC = f'''<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">
  <defs><clipPath id="c">
    <image href="{BROKEN_PNG_DATA_URI}" width="10" height="10"/>
  </clipPath></defs>
  <rect width="60" height="60" fill="blue" clip-path="url(#c)"/>
</svg>'''.encode()

MASK_DOC = f'''<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">
  <defs><mask id="m">
    <rect width="50" height="50" fill="white"/>
    <image href="{BROKEN_PNG_DATA_URI}" width="10" height="10"/>
  </mask></defs>
  <g mask="url(#m)"><rect width="60" height="60" fill="blue"/></g>
</svg>'''.encode()

USE_DOC = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
    b'<defs><g id="target">'
    b'<image href="data:image/png;base64,'
    b'iVBORw0KGgoAAAANSUhEUgAAAAUAAAAFCAAAAABad+Rb" '
    b'width="10" height="10"/></g></defs>'
    b'<use href="#target"/></svg>')

PATTERN_DOC = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="60" height="60">'
    b'<defs><pattern id="p" width="8" height="8" '
    b'patternUnits="userSpaceOnUse">'
    b'<rect width="8" height="8" fill="blue"/>'
    b'</pattern></defs>'
    b'<rect width="50" height="50" fill="url(#p)"/></svg>')


def probe(import_root):
    import importlib
    sys.path.insert(0, import_root)
    for name in list(sys.modules):
        if name.startswith('cairosvg'):
            del sys.modules[name]
    cairosvg = importlib.import_module('cairosvg')
    from cairosvg.parser import Tree
    from cairosvg.surface import PNGSurface
    import cairosvg.surface as surface_module

    result = {}

    def fail_and_capture(document):
        tree = Tree(bytestring=document)
        captured = {}
        original = PNGSurface.__init__

        def spy(self, *args, **kwargs):
            captured['surface'] = self
            return original(self, *args, **kwargs)

        PNGSurface.__init__ = spy
        try:
            try:
                PNGSurface(tree, io.BytesIO(), 96)
            except cairo.CairoError:
                pass
        finally:
            PNGSurface.__init__ = original
        return captured['surface']

    clip_surface = fail_and_capture(CLIP_DOC)
    result['clip node tag after failure'] = next(
        iter(clip_surface.paths.values())).tag
    result['stroke_and_fill after clip failure'] = (
        clip_surface.stroke_and_fill)

    mask_surface = fail_and_capture(MASK_DOC)
    result['mask node tag after failure'] = next(
        iter(mask_surface.masks.values())).tag

    use_surface = fail_and_capture(USE_DOC)
    result['reference_count after failed <use>'] = (
        use_surface.reference_count)

    result['cairo surface closed after failures'] = (
        clip_surface.cairo is None and mask_surface.cairo is None
        and use_surface.cairo is None)

    # Pattern temporary surface lifetime
    refs = []
    original = surface_module.SVGSurface.__init__

    def spy_nested(self, *args, **kwargs):
        refs.append(weakref.ref(self))
        return original(self, *args, **kwargs)

    surface_module.SVGSurface.__init__ = spy_nested
    pattern_surface = PNGSurface(
        Tree(bytestring=PATTERN_DOC), None, 96)
    held_while_alive = refs and refs[0]() is not None
    pattern_surface.finish()
    surface_module.SVGSurface.__init__ = original
    gc.collect()
    result['pattern temp alive until finish'] = held_while_alive
    result['pattern temp released after finish'] = all(
        ref() is None for ref in refs)

    sys.path.remove(import_root)
    return result


def main(pristine_dir, output_path):
    fixed_root = str(Path(__file__).resolve().parents[1])
    pristine = probe(pristine_dir)
    fixed = probe(fixed_root)

    expected = {
        'clip node tag after failure': 'clipPath',
        'mask node tag after failure': 'mask',
        'stroke_and_fill after clip failure': True,
        'reference_count after failed <use>': 0,
        'cairo surface closed after failures': True,
        'pattern temp alive until finish': True,
        'pattern temp released after finish': True,
    }

    lines = [
        'CairoSVG rendering-state isolation: pristine vs fixed',
        '=' * 64,
        f'{"probe":42} {"pristine":>9} {"fixed":>7}',
        '-' * 64,
    ]
    for key in expected:
        ok = fixed[key] == expected[key]
        lines.append(
            f'{key:42} {str(pristine[key]):>9} '
            f'{str(fixed[key]):>7}  {"OK" if ok else "WRONG"}')
    report = '\n'.join(lines)
    Path(output_path).write_text(report + '\n')
    print(report)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
