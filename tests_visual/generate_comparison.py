"""
Generate local visual comparison artifacts for the state-isolation fix.

For every scenario this writes, into the output directory:

* ``<name>_combined.png`` - special / failing layers in front of baseline
                            content;
* ``<name>_diff.png``     - amplified per-pixel difference between the
                            baseline region of the combined render and the
                            standalone baseline (black means identical);
* a contact sheet ``contact_sheet.png`` grouping all scenarios.

Run with the project virtualenv, e.g.::

    DYLD_FALLBACK_LIBRARY_PATH=... .venv/bin/python -I \
        tests_visual/generate_comparison.py <output_dir>
"""

import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw

import cairocffi as cairo

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import cairosvg  # noqa: E402
from cairosvg.surface import PNGSurface  # noqa: E402

BROKEN_PNG = (
    'data:image/png;base64,'
    'iVBORw0KGgoAAAANSUhEUgAAAAUAAAAFCAAAAABad+Rb')

W = H = 120
HEADER = (
    '<svg xmlns="http://www.w3.org/2000/svg" '
    f'width="{W}" height="{H}">')
BASELINE_RECT = (
    '<rect x="70" y="70" width="40" height="40" fill="red"/>')


def render(svg, **kwargs):
    if 'url_fetcher' in kwargs:
        png = PNGSurface.convert(bytestring=svg, **kwargs)
    else:
        png = cairosvg.svg2png(svg, **kwargs)
    return Image.open(io.BytesIO(png)).convert('RGBA')


def baseline_svg():
    return (HEADER + BASELINE_RECT + '</svg>').encode()


SCENARIOS = {
    'transform': (
        HEADER +
        '<g transform="translate(10,10) rotate(25) scale(1.2)" '
        'opacity="0.4">'
        '<rect width="30" height="30" fill="blue"/></g>'
        + BASELINE_RECT + '</svg>').encode(),
    'clip_path': (
        HEADER +
        '<defs><clipPath id="c">'
        '<circle cx="25" cy="25" r="20"/></clipPath></defs>'
        '<g clip-path="url(#c)" transform="translate(4,4)">'
        '<rect width="50" height="50" fill="blue"/></g>'
        + BASELINE_RECT + '</svg>').encode(),
    'mask': (
        HEADER +
        '<defs><mask id="m"><rect width="50" height="50" fill="white"/>'
        '<circle cx="25" cy="25" r="12" fill="black"/></mask></defs>'
        '<g mask="url(#m)" transform="translate(2,2)">'
        '<rect width="50" height="50" fill="blue"/></g>'
        + BASELINE_RECT + '</svg>').encode(),
    'filter_operator': (
        HEADER +
        '<defs><filter id="f" x="-20%" y="-20%" '
        'width="140%" height="140%">'
        '<feOffset dx="6" dy="4"/>'
        '<feBlend mode="multiply" in="SourceGraphic"/>'
        '</filter></defs>'
        '<g filter="url(#f)">'
        '<rect x="5" y="5" width="30" height="30" fill="blue"/></g>'
        + BASELINE_RECT + '</svg>').encode(),
    'opacity_group': (
        HEADER +
        '<g opacity="0.35">'
        '<rect x="5" y="5" width="25" height="25" fill="blue"/>'
        '<rect x="20" y="20" width="25" height="25" fill="green"/></g>'
        + BASELINE_RECT + '</svg>').encode(),
    'pattern_source': (
        HEADER +
        '<defs><pattern id="p" width="8" height="8" '
        'patternUnits="userSpaceOnUse">'
        '<rect width="8" height="8" fill="blue"/>'
        '<rect width="4" height="4" fill="white"/></pattern></defs>'
        '<rect x="2" y="2" width="50" height="50" fill="url(#p)"/>'
        + BASELINE_RECT + '</svg>').encode(),
    'empty_layers': (
        HEADER +
        '<g transform="translate(30,30) rotate(45)" opacity="0.2">'
        '<rect width="0" height="40" fill="blue"/></g>'
        '<g display="none">'
        '<rect width="40" height="40" fill="blue"/></g><g></g>'
        + BASELINE_RECT + '</svg>').encode(),
}

FAILING = (
    HEADER +
    '<g opacity="0.5" transform="translate(8,8) rotate(20)">'
    '<rect width="40" height="40" fill="blue"/>'
    f'<image href="{BROKEN_PNG}" x="2" y="2" width="20" height="20"/>'
    '</g>' + BASELINE_RECT + '</svg>').encode()


def diff_image(a, b):
    """Amplified RGBA difference; fully black when the images are identical."""
    assert a.size == b.size
    out = Image.new('RGBA', a.size)
    pa, pb, po = a.load(), b.load(), out.load()
    for y in range(a.size[1]):
        for x in range(a.size[0]):
            delta = tuple(abs(c1 - c2) for c1, c2 in zip(pa[x, y], pb[x, y]))
            rgb = tuple(min(255, d * 8) for d in delta[:3])
            po[x, y] = rgb + ((255,) if any(delta) else (0,))
    return out


def main(output_dir):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    baseline = render(baseline_svg())
    baseline.save(out / '00_baseline.png')

    sheet_rows = []
    for name, svg in SCENARIOS.items():
        combined = render(svg)
        combined.save(out / f'{name}_combined.png')
        region_diff = diff_image(
            combined.crop((70, 70, 110, 110)),
            baseline.crop((70, 70, 110, 110)))
        region_diff.save(out / f'{name}_diff.png')
        identical = region_diff.getchannel('A').getextrema()[1] == 0
        sheet_rows.append((name, combined, region_diff, identical))

    # Failure scenario: render must raise the real PNG error, then the next
    # entry-point call must reproduce the baseline exactly.
    error = None
    try:
        cairosvg.svg2png(FAILING)
    except cairo.CairoError as exc:
        error = str(exc)
    after = render(baseline_svg())
    after.save(out / 'after_exception_baseline.png')
    reentry_diff = diff_image(after, baseline)
    reentry_diff.save(out / 'after_exception_diff.png')
    reentry_ok = reentry_diff.getchannel('A').getextrema()[1] == 0

    # Contact sheet
    label_h, cell = 18, 120
    sheet = Image.new(
        'RGBA', (cell * 2 + 24,
                 len(sheet_rows) * (cell + label_h) + 120),
        (255, 255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    draw.text(
        (8, 4),
        'columns: combined render | baseline-region diff '
        '(black = identical)', fill='black')
    for row, (name, combined, region_diff, identical) in enumerate(sheet_rows):
        top = 24 + row * (cell + label_h)
        state = 'IDENTICAL' if identical else 'DIFF!!'
        draw.text((8, top), f'{name}  {state}', fill='black')
        sheet.paste(combined, (8, top + label_h), combined)
        full_diff = Image.new('RGBA', (W, H), (0, 0, 0, 0))
        full_diff.paste(region_diff, (70, 70))
        sheet.paste(
            full_diff, (8 + cell + 8, top + label_h), full_diff)
    fail_top = 24 + len(sheet_rows) * (cell + label_h)
    draw.text((8, fail_top), f'failing render raised: {error}', fill='black')
    state = 'IDENTICAL' if reentry_ok else 'DIFF!!'
    draw.text(
        (8, fail_top + 16),
        f'baseline after exception: {state}', fill='black')
    sheet.paste(after, (8, fail_top + 36), after)
    sheet.save(out / 'contact_sheet.png')

    print('wrote', out)
    for name, _, _, identical in sheet_rows:
        print(f'  {name}: baseline region identical = {identical}')
    print('  propagated error:', error)
    print('  re-entry identical:', reentry_ok)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'visual_output')
