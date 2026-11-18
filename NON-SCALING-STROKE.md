# `vector-effect="non-scaling-stroke"` support

This document records what CairoSVG actually does for the SVG
`vector-effect` property, and how it was verified. It only states support
that was measured on generated images; anything untested is called out as
such.

The offline comparison script is `examples/non_scaling_stroke_legend.py`. It
writes the images referenced below and the `report.txt` they are measured
from:

```bash
python -m examples.non_scaling_stroke_legend --out examples/output --pdf
```

## What “non-scaling-stroke” means here

Per SVG 2, the stroke outline is computed in the *host* coordinate space — the
viewport coordinate system of the furthest ancestral SVG viewport — and then
mapped back to user space. Consequently:

* the path geometry still follows the element, its groups and the root
  `viewBox`;
* the stroke width, line caps, line joins, `stroke-miterlimit` and
  `stroke-dasharray` are all measured in host space, so they do not stretch
  with the object transform;
* `markerUnits="strokeWidth"` markers are likewise drawn in host space (the
  spec says they become non-scaling too);
* stroke paint is still anchored in user space, exactly as for a plain
  stroke.

“Host space” is one CSS pixel per unit. The `width`/`height` of the
document and the `scale`, `output_width` and `output_height` convert options
count as device/viewport scale and *do* scale the non-scaling stroke; the
root `viewBox`, `transform` attributes and nested `<svg>` viewports do not.

`vector-effect` is not inherited (it is in `NOT_INHERITED_ATTRIBUTES` in the
parser). It applies to the graphics element that carries the attribute,
including elements reached through `<use>`.

Only the `non-scaling-stroke` value is implemented. Other `vector-effect`
values (`none`, `non-scaling-size`, `non-rotation`, `fixed-position`) keep
the default stroke rendering.

## How it is implemented

In `Surface.draw()`, an element with `vector-effect="non-scaling-stroke"`
has its current path copied, transformed into host coordinates, and stroked
with a temporary context matrix set to the host matrix. The paint source has
already been set while the user-space CTM was active, so Cairo keeps the
gradient/pattern anchored in user space. The context is saved and restored
around the whole operation, so nothing leaks to markers, siblings, or later
`<use>` clones.

The host matrix is computed once in `Surface.__init__` from the output size
and the device-unit ratio, before the root `viewBox` is applied.

## Verification on this machine

### Raster output (PNG) — verified

`cairosvg/test_vector_effect.py` rasterizes each case and measures real
pixels. It covers:

* translation, rotation, uniform and non-uniform scaling of the same
  outline, with and without the effect;
* line caps, miter joins, dash arrays and `markerUnits` markers;
* the same fill result for both stroke modes;
* nested transforms, a root `viewBox`, an output `scale=2`, a nested `<svg>`
  viewport;
* repeated `<use>` references and siblings after a non-scaling element;
* value parsing (case, trailing spaces) and unsupported values;
* PDF and SVG output calls (see below for the limits of those checks).

Run it with:

```bash
python -m pytest -q cairosvg/test_vector_effect.py
```

There are 31 test cases (29 test functions, one parametrized three times);
all pass in this repository. The offline legend run
prints `report.txt` with per-variant thickness:

| variant            | plain stroke (px)   | non-scaling stroke (px) |
| ------------------ | ------------------- | ----------------------- |
| translate          | 12.0 / 12.0         | 4.0 / 4.0               |
| rotate 30°         | 13.84 / 13.84       | 4.64 / 4.64             |
| uniform scale 2.5× | 30.0 / 30.0         | 4.0 / 4.0               |
| scale(3.2, 1.2)    | 14.4 / 38.4         | 4.0 / 4.0               |
| scale(1.2, 3.2)    | 38.4 / 14.4         | 4.0 / 4.0               |

(Thicknesses are the summed coverage of a scan across the outline edges,
horizontal / vertical. The helper draws the outline at a 3× viewport zoom,
so a plain stroke of `stroke-width=4` is 12 px; the rotation makes both
strokes look `1/cos(30°)` thicker along an axis-aligned scan.)

The edge centers stay identical between the two stroke modes in every
variant, which is the pixel-level evidence that the geometry is untouched.

### Vector output (PDF) — verified by rasterizing it

`--pdf` writes `legend.pdf` and, when `pdftoppm` is installed, rasterizes it
to `legend-pdf.png`. The PDF is produced by the same Cairo path, so the
outline is the one measured above. In this run `pdftoppm` (Poppler 26.05.0)
was available and the PDF was rasterized successfully.

### SVG output — partially verified

`svg2svg` re-renders the document through Cairo and serializes the resulting
paths. For a non-scaling stroke the output contains the host-space path and
the user `stroke-width`, with no `vector-effect` attribute — the visual
result is correct for that rendering, but the exported SVG is a flattened
snapshot rather than a reusable non-scaling definition. The test only checks
that the serialized document is produced and carries the expected width; it
does not verify pixel equality of the SVG output against the PNG.

### PS and EPS output — not verified on this machine

`svg2ps` and `svg2eps` go through the same Cairo stroking code and are
expected to behave like PDF, but no PostScript interpreter or rasterizer
(Ghostscript or equivalent) is available here, so this run makes no
pixel-level claim about them. The existing `test_api.py` only checks that
both formats are produced for a document without vector effects; no
`non-scaling-stroke` case is checked for them.

### Not supported

* Other `vector-effect` values.
* Text painting (`show_text`) in host space: glyph outlines are stroked from
  the text path, which is what is covered; the exact behaviour of
  `vector-effect` on live text metrics follows the same “outline in host
  space” rule but has not been measured against another implementation.
* `markerUnits="userSpaceOnUse"` markers deliberately stay in user space, as
  the SVG specification describes for marker sizing.
* Content rendered into its own surface — `<pattern>`, `<mask>` and a
  rasterized `<image>` — establishes its own host space for that surface.
  This only matters for documents that combine those features with very
  different object scaling; it has not been compared against a browser.

## Documents used for comparison

The offline legend is generated by the same run, so the images referenced in
`report.txt` are always consistent with the measurements:

* `legend.png`, `legend.svg` — the sheet with five variants × two
  stroke modes shown in the report;
* `joins.png`, `joins.svg` — line caps, miter join and 8/4 dashes;
* `outline-<variant>-<stroke>.png` — the isolated images the report measures;
* `legend-pdf.png` — the rasterized PDF (only with `--pdf`).
