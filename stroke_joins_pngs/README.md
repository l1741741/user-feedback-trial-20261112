# Stroke joins, caps and miter limits — evidence

This folder contains the images used to verify the stroke fixes. Everything is
rendered with CairoSVG from SVGs stored in `make_sheets.py`; no external
renderer is needed to reproduce them.

```
python stroke_joins_pngs/make_sheets.py stroke_joins_pngs/after
python stroke_joins_pngs/make_comparisons.py
```

* `after/` — the fixed renderer.
* `before/` — the same sheets rendered with the sources from `HEAD` before the
  fix. Three cases could not be rendered at all and left a `-FAILED.txt` file
  with the exception instead of a PNG.
* `comparison/` — the two renders side by side. `-zoom.png` files crop the
  corner or cap under test and enlarge it with nearest-neighbour so the
  difference is visible pixel by pixel.
* `contact-sheet.png` — the seven fixed cases stacked for a quick review.

Every sheet is 800×800 (8 device pixels per user unit) so that a one-pixel
edge of a 6-unit stroke is clearly visible.

## What each sheet shows, and which boundary is covered by the fix

| Sheet | Before | After | Boundary covered |
| --- | --- | --- | --- |
| `sharp-angle-joins` | The red path, which asks for `miter` inside a `round` group, is rounded. | The red path keeps its miter tip. | A child's explicit join wins over the inherited one. |
| `round-angle-joins` | — | Round joins on every corner. | Control: the round join is unchanged. |
| `linejoin-override` | Both paths are round. | The right path is a miter. | `stroke-linejoin="miter"` is no longer silently ignored. |
| `linecap-override` | The red path, which asks for `butt` inside a `round` group, keeps round caps. | The red path ends flat at the endpoints, the blue one has square caps. | A child's explicit cap wins over the inherited one. |
| `zero-length-caps` | Only the round dots are painted: square caps and single-point subpaths of quadratic/cubic/close commands disappear. | Every `square` and `round` single-point subpath paints a square or a dot; `butt` still paints nothing. | Caps of zero-length subpaths, including `M… Z`, curves and `<line>`/`<polyline>`. |
| `miterlimit-invalid-values` | The whole document fails (`ValueError`). | Invalid values are ignored; the document renders. | Invalid `stroke-miterlimit` cannot abort a document. |
| `invalid-dash-does-not-poison` | The whole document fails with `CAIRO_STATUS_INVALID_DASH`. | The invalid dash list is ignored and the rectangle is drawn. | An invalid dash list cannot poison the drawing context. |
| `object-bounding-box` | The whole document fails with `CAIRO_STATUS_INVALID_MATRIX`. | The blue path and the red circle are clipped to their unit bounding boxes. | `clipPathUnits="objectBoundingBox"` uses the element's geometry box. |
| `mixed-segments` | Same outline. | Same outline (miter left, bevel right). | Regression guard: straight, quadratic, cubic and closing segments keep their join. |
| `short-and-zero-segments` | The single-point subpath is invisible. | The dot under the line is painted; the other outlines are identical. | Short segments, repeated vertices and a zero-length subpath in a real path. |
| `transformed-sharp-angle` | Same outline. | Same outline. | Regression guard: miter tips follow translate/rotate/scale. |

Two more cases are covered by the tests but have no separate image because
their bug was a crash rather than a visible difference:

* a marker whose content has no area (a straight line or a single point) used
  to raise `TypeError` while computing the marker scale;
* rendering the same parsed tree twice changed the result when it contained a
  `use`, a mask or an object-bounding-box pattern.

## How the expected values were chosen

The images are not self-referential: the expected outline of each case was
cross-checked with Chromium's SVG renderer (headless Chrome) at the same
scale, and with Cairo stroking the exact geometry directly. Where Cairo and
Chromium disagree, the SVG specification was followed:

* `stroke-linejoin` and `stroke-linecap` are case-insensitive keywords and must
  be applied when the element sets them, even inside a group with another
  value;
* `stroke-miterlimit` below 1 is invalid in SVG 1.1, so the inherited value
  applies. A numeric `0` is still forwarded to Cairo, because that is the
  behaviour existing documents rely on (and what this project did before);
* zero-length subpaths use the caps: a square or a dot for `square` and
  `round`, nothing for `butt`; a lone `M` does not start a subpath;
* an object bounding box contains the geometry only — never the stroke.

The automated versions live in `cairosvg/test_stroke_joins.py` (84 cases) and
`cairosvg/test_path_regression.py` (80 cases); both run offline in well under a
second.
