"""Generate magnified before/after comparison PNGs for the stroke fixes.

Renders identical SVG fixtures against two source trees:
  * the pristine pinned commit (work/orig)
  * the fixed working copy (repo root)

Outputs side-by-side comparison PNGs into work/png/compare.
"""
import importlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, 'test_stroke_artifacts', '_scratch')
ORIG = os.path.join(WORK, 'orig')
OUT = os.path.join(ROOT, 'test_stroke_artifacts', 'png')
os.makedirs(OUT, exist_ok=True)


def ensure_orig():
    """Materialize a pristine full cairosvg package from git HEAD.

    It is written as ``work/orig/orig_cairosvg`` so it can be imported under a
    name that does not collide with the fixed ``cairosvg`` package.
    """
    pkg = os.path.join(ORIG, 'orig_cairosvg')
    if os.path.isdir(pkg):
        shutil.rmtree(pkg)
    os.makedirs(pkg, exist_ok=True)
    files = subprocess.check_output(
        ['git', '-C', ROOT, 'ls-tree', '--name-only', 'HEAD', 'cairosvg/'])
    names = [f.decode().split('/', 1)[1]
             for f in files.splitlines() if f.decode().endswith(('.py', 'VERSION'))]
    for name in names:
        data = subprocess.check_output(
            ['git', '-C', ROOT, 'show', f'HEAD:cairosvg/{name}'])
        with open(os.path.join(pkg, name), 'wb') as fh:
            fh.write(data)


FIXTURES = {
    # 1. Sharp angle: default miter (spike) vs miterlimit clamp vs round/bevel
    'sharp_angle': '''
<svg xmlns="http://www.w3.org/2000/svg" width="300" height="260" font-family="sans-serif" font-size="14">
  <g fill="none" stroke="#111" stroke-width="14">
    <path d="M40 200 L80 40 L120 200" stroke-linejoin="miter" stroke-miterlimit="100"/>
    <path d="M130 200 L170 40 L210 200" stroke-linejoin="miter" stroke-miterlimit="2"/>
    <path d="M220 200 L260 40 L300 200" stroke-linejoin="bevel"/>
  </g>
  <g fill="#333" text-anchor="middle">
    <text x="80" y="230">miter 100 (spike)</text>
    <text x="170" y="230">miter 2 (clamped)</text>
    <text x="260" y="230">bevel</text>
  </g>
</svg>''',
    # 2. Inherited round join/cap reset by explicit miter/butt
    'join_reset': '''
<svg xmlns="http://www.w3.org/2000/svg" width="300" height="180" font-family="sans-serif" font-size="13">
  <g stroke-linejoin="round" stroke-linecap="round" fill="none"
     stroke="#111" stroke-width="20">
    <path d="M20 120 L60 40 L100 120"/>
    <path d="M120 120 L160 40 L200 120" stroke-linejoin="miter" stroke-linecap="butt"/>
    <path d="M220 120 L260 40 L300 120" stroke-linejoin="bogus" stroke-linecap="nope"/>
  </g>
  <g fill="#333" text-anchor="middle">
    <text x="60" y="155">inherited round</text>
    <text x="160" y="155">explicit miter/butt</text>
    <text x="260" y="155">invalid -> default</text>
  </g>
</svg>''',
    # 3. Mixed line + quadratic + cubic closed contour under each join
    'mixed_closed': '''
<svg xmlns="http://www.w3.org/2000/svg" width="300" height="140" font-family="sans-serif" font-size="13">
  <g fill="none" stroke="#111" stroke-width="10">
    <path d="M20 90 L50 30 Q75 60 100 35 C125 15 140 80 165 45 L140 100 Z"
          stroke-linejoin="miter"/>
    <path d="M170 90 L200 30 Q225 60 250 35 C275 15 290 80 315 45 L290 100 Z"
          stroke-linejoin="round" transform="translate(-10,0) scale(0.85)"/>
  </g>
</svg>''',
    # 4. Short and zero-length segments with caps
    'zero_length': '''
<svg xmlns="http://www.w3.org/2000/svg" width="300" height="140" font-family="sans-serif" font-size="13">
  <g fill="none" stroke="#111" stroke-width="12" stroke-linecap="round">
    <path d="M30 100 L30 100 L70 30 L70 30 L110 100 L110 100"/>
  </g>
  <g fill="none" stroke="#c0392b" stroke-width="12" stroke-linecap="square">
    <path d="M160 100 L160 100 L200 30 L200 30 L240 100 L240 100"/>
  </g>
  <g fill="#333" text-anchor="middle">
    <text x="70" y="128">round cap + zero-length</text>
    <text x="200" y="128">square cap + zero-length</text>
  </g>
</svg>''',
    # 5. Invalid miterlimit must still render the whole document
    'invalid_miter': '''
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="160" font-family="sans-serif" font-size="13">
  <path d="M20 120 L100 30 L180 120" fill="none" stroke="#c0392b"
        stroke-width="12" stroke-miterlimit="garbage"/>
  <rect x="20" y="20" width="30" height="24" fill="#27ae60"/>
  <text x="60" y="150" fill="#333">miterlimit="garbage"</text>
</svg>''',
}


def render_all(module):
    rendered = {}
    for name, svg in FIXTURES.items():
        try:
            rendered[name] = module.svg2png(bytestring=svg.encode())
        except Exception as exc:  # the pristine tree is expected to crash here
            from PIL import Image, ImageDraw
            w, h = 320, 180
            img = Image.new('RGBA', (w, h), (255, 240, 240, 255))
            draw = ImageDraw.Draw(img)
            draw.rectangle((0, 0, w - 1, h - 1), outline=(200, 0, 0, 255), width=3)
            msg = f'{type(exc).__name__}:'
            draw.text((12, 70), msg, fill=(180, 0, 0, 255))
            draw.text((12, 92), str(exc)[:34], fill=(180, 0, 0, 255))
            buf = io.BytesIO()
            img.save(buf, format='PNG')
            rendered[name] = buf.getvalue()
    return rendered


def compose(before, after):
    import cairocffi as cairo
    from PIL import Image

    def to_image(png):
        return Image.open(io.BytesIO(png)).convert('RGBA')

    pages = []
    for name in FIXTURES:
        a = to_image(before[name])
        b = to_image(after[name])
        w = max(a.width, b.width)
        h = max(a.height, b.height)
        pad, label_h = 12, 26
        page = Image.new('RGBA', (w * 2 + pad * 3, h + label_h * 2 + pad),
                         (255, 255, 255, 255))
        page.paste(a, (pad, label_h), a)
        page.paste(b, (w + pad * 2, label_h), b)
        pages.append((name, page, w, h, pad, label_h))

    total_h = sum(p[1].height + 8 for p in pages) + 40
    total_w = max(p[1].width for p in pages)
    sheet = Image.new('RGBA', (total_w, total_h), (255, 255, 255, 255))
    draw_y = 8
    from PIL import ImageDraw
    draw = ImageDraw.Draw(sheet)
    for name, page, w, h, pad, label_h in pages:
        draw.text((pad, draw_y), f'{name}:  LEFT = original (pinned)   '
                  f'RIGHT = fixed', fill=(0, 0, 0, 255))
        sheet.paste(page, (0, draw_y + 22), page)
        draw_y += page.height + 8
    return sheet


def main():
    ensure_orig()
    sys.path.insert(0, ROOT)
    # Fixed tree.
    import cairosvg as fixed
    after = render_all(fixed)

    # Pristine tree, isolated under a unique package name.
    sys.path.insert(0, ORIG)
    import orig_cairosvg as original
    before = render_all(original)

    sheet = compose(before, after)
    out_path = os.path.join(OUT, 'stroke_join_comparison.png')
    sheet.convert('RGB').save(out_path)
    print('wrote', out_path, sheet.size)

    # Also dump individual after-fix PNGs for direct inspection.
    for name, png in after.items():
        with open(os.path.join(OUT, f'{name}_fixed.png'), 'wb') as fh:
            fh.write(png)
    for name, png in before.items():
        with open(os.path.join(OUT, f'{name}_original.png'), 'wb') as fh:
            fh.write(png)


if __name__ == '__main__':
    main()
