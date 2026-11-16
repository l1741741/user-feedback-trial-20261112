# `vector-effect: non-scaling-stroke`

CairoSVG supports the SVG `vector-effect` presentation attribute and, in
particular, its `non-scaling-stroke` keyword.

```svg
<rect x="10" y="10" width="80" height="60"
      fill="none" stroke="black" stroke-width="4"
      vector-effect="non-scaling-stroke"/>
```

## What it does

With a plain stroke, `stroke-width` is expressed in the local user coordinate
system, so every transform applied to the element (or an ancestor group, or
the root viewport) scales the painted thickness. A non-uniform scale such as
`scale(2, 3)` makes the vertical edges twice and the horizontal edges three
times as thick.

With `non-scaling-stroke` only the *scale meaning* of the stroke changes:

- the path **geometry is not moved** — it still follows the object, group and
  viewport transforms exactly as before;
- the stroke **thickness is constant in device units**, regardless of the
  (possibly non-uniform, nested or rotated) transforms in effect;
- `stroke-linecap`, `stroke-linejoin`, `stroke-miterlimit`, the dash array and
  dash offset keep working and keep their own units — caps, joins, dashes and
  markers are not turned into unrelated sizes;
- gradient and pattern stroke paint keeps aligning with the geometry.

The attribute is inherited, so setting it on a `<g>` applies it to every
stroked descendant. Only the exact keyword `non-scaling-stroke` is honored;
the default (`none`) and any other value keep the historical, transform-scaled
behavior. Plain strokes are unchanged.

## How it is implemented

When a non-scaling stroke is painted, CairoSVG:

1. reads the full user-to-device matrix of the current Cairo context;
2. flattens the current path and maps every point to device space with that
   matrix, then strokes it under an identity transform;
3. converts the line width and dash lengths once from user units to device
   units, so they keep a constant screen size;
4. leaves the gradient/pattern source matrix untouched. Cairo interprets a
   pattern matrix relative to the CTM active at paint time, and the
   pre-transformed device-space path combined with the identity CTM reaches
   exactly the user space in which the paint was defined, so the paint keeps
   aligning with the geometry (verified to be pixel-identical to a plain
   stroke for both `objectBoundingBox` and `userSpaceOnUse` gradients).

Because the whole operation is wrapped in a Cairo `save()`/`restore()`, the
reset transform, line width, dash state and source changes never leak to the
next element, including across nested transforms and repeated `<use>`
references.

## Verified support per output format

The support below is the range actually exercised and checked (on real
rendered output), not merely declared.

| Output | non-scaling-stroke supported | How it is verified |
| --- | --- | --- |
| **PNG** (raster) | yes | Pixel measurements of horizontal and vertical edge thickness for translate / rotate / uniform / non-uniform variants; see `cairosvg/test_vector_effect.py` and the legend below |
| **PDF** (vector) | yes | PDF is rasterized with Poppler (`pdftoppm`, 96 DPI) and the same thickness measurements are made on the rasterized page; the non-scaling width matches a root-level plain stroke of the same width |
| **Cairo SVG** (vector) | yes | The emitted SVG contains the path pre-transformed to device coordinates with a fixed, unscaled `stroke-width`; re-rendering that SVG reproduces the constant thickness |
| **PS / EPS** (vector) | yes | Operator-level inspection of the PostScript output: the stroke uses a fixed `setlinewidth` and the path coordinates are pre-transformed, while plain strokes keep their scaling `concat` matrix. No local PostScript rasterizer is available in this environment, so PS is not pixel-checked |

Notes and limitations:

- Markers keep their own `markerUnits` sizing and are not resized by the
  non-scaling stroke handling (verified by identical marker ink counts with
  and without the effect).
- A non-invertible (for example zero-scale) transform does not raise; it is
  handled through the existing CairoSVG degenerate-transform path.
- Other `vector-effect` keywords defined elsewhere (for example
  `non-scaling-size` on symbols) are not implemented and are ignored.

## Offline legend

`generate_legend.py` renders the same square outline under four transforms,
with a plain and a non-scaling stroke, measures the real edge thickness in the
produced PNG, and writes:

- `legend.png` — the plain vs non-scaling comparison montage;
- `legend-annotated.png` — the same montage with the measured thickness of two
  perpendicular edges of every square;
- `thickness-measurements.json` — the numeric measurements.

Representative result for a `stroke-width="4"` square:

| variant | plain stroke (edge A / edge B) | non-scaling (edge A / edge B) |
| --- | --- | --- |
| translate | 4 px / 4 px | 4 px / 4 px |
| rotate 30° | 4 px / 4 px | 4 px / 4 px |
| scale(2, 2) | 8 px / 8 px | 4 px / 4 px |
| scale(2, 3) | 8 px / 12 px (anisotropic) | 4 px / 4 px (uniform) |

Run it from the repository root:

```sh
DYLD_FALLBACK_LIBRARY_PATH=<path-to-the-cairo-libraries> \
    .venv/bin/python -I docs/vector-effect/generate_legend.py
```
