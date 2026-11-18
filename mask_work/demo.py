"""Build a self-contained mask demo from offline SVG inputs.

For every probe SVG this renders the document *now* with the installed
CairoSVG, writes the resulting PNG, and emits an HTML page that shows each
input source next to its freshly rendered result. It does not use cached or
reference images: every PNG is produced during this run.

Usage:
    python mask_work/demo.py
Outputs:
    mask_work/demo/index.html and mask_work/demo/*.png
"""
import html
import shutil
from pathlib import Path

import cairosvg

ROOT = Path(__file__).resolve().parent
SVG_DIR = ROOT / 'svg'
DEMO_DIR = ROOT / 'demo'

# Human-readable explanation of what each probe verifies.
CAPTIONS = {
    '01_luminance': 'Luminance mask: black hides, gray 50% is half, white shows.',
    '02_color_luminance': 'Color luminance: red/green/blue differ by Rec.709 luma.',
    '03_mask_alpha': 'Mask content opacity scales coverage (0.25/0.5/0.75).',
    '04_reuse_userspace': 'One userSpace mask reused by a transformed second object.',
    '05_outside_transparent': 'Outside the mask region stays fully transparent.',
    '06_group_opacity': 'Group opacity 0.5 x mask alpha 0.5 = 0.25, applied once.',
    '07_gradient': 'A gradient mask gives a matching smooth alpha ramp.',
    '08_nested_groups': 'Nested masked groups; each mask and opacity stays distinct.',
    '09_sibling_reuse': 'Sibling elements reuse one mask without node mutation.',
    '10_bbox_reuse': 'objectBoundingBox mask re-fits each object of different size.',
    '11_alpha_vs_luminance': 'mask-type alpha vs luminance produce different results.',
    '12_transform_nonsquare': 'Rotated/translated non-square objects, shared mask.',
    '13_linear_rgb': 'color-interpolation=linearRGB changes the gray coverage.',
    '14_mask_gradient_group': 'Gradient plus a translucent group inside the mask.',
    '15_mask_use': 'Mask content referenced through <use>.',
    '16_leaf_opacity_mask': 'Leaf opacity and mask alpha each applied exactly once.',
}


def main():
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)
    DEMO_DIR.mkdir(parents=True)

    cards = []
    for svg_path in sorted(SVG_DIR.glob('*.svg')):
        name = svg_path.stem
        png_path = DEMO_DIR / f'{name}.png'
        # Render from the file on disk during this run; nothing is cached.
        cairosvg.svg2png(url=str(svg_path), write_to=str(png_path))
        source = svg_path.read_text()
        caption = CAPTIONS.get(name, '')
        cards.append(f'''
        <section class="card">
          <h2>{html.escape(name)}</h2>
          <p class="caption">{html.escape(caption)}</p>
          <div class="panes">
            <figure>
              <figcaption>Input SVG</figcaption>
              <pre>{html.escape(source)}</pre>
            </figure>
            <figure>
              <figcaption>Rendered this run (PNG)</figcaption>
              <img src="{name}.png" alt="{html.escape(name)} render">
            </figure>
          </div>
        </section>''')

    index = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CairoSVG mask semantics demo</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; background:#fafafa; }}
  h1 {{ margin-bottom: .25rem; }}
  .meta {{ color:#555; margin-top:0; }}
  .card {{ background:#fff; border:1px solid #ddd; border-radius:8px;
           padding:1rem 1.25rem; margin:1.25rem 0; }}
  .panes {{ display:flex; gap:1.5rem; align-items:flex-start; flex-wrap:wrap; }}
  figure {{ margin:0; flex:1; min-width:280px; }}
  figcaption {{ font-weight:600; margin-bottom:.4rem; }}
  pre {{ background:#111; color:#eee; padding:.75rem; border-radius:6px;
         font-size:11px; overflow:auto; max-height:340px; }}
  img {{ max-width:100%; border:1px solid #ccc; background:
         repeating-conic-gradient(#eee 0 25%, #fff 0 50%) 0 0/16px 16px; }}
  .caption {{ color:#444; }}
</style>
</head>
<body>
  <h1>CairoSVG <code>&lt;mask&gt;</code> semantics</h1>
  <p class="meta">Rendered on demand with CairoSVG {cairosvg.VERSION}.
     Each PNG next to its input was generated during this demo run.</p>
  {''.join(cards)}
</body>
</html>'''
    (DEMO_DIR / 'index.html').write_text(index)
    print(f'Wrote {len(cards)} demos to {DEMO_DIR}')


if __name__ == '__main__':
    main()
