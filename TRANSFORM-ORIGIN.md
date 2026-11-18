# Transform origin and transform-box support

This document describes how CairoSVG applies SVG `transform` and CSS
Transforms' `transform-origin` / `transform-box`, the scope of that support,
and how to check the geometry yourself.

The behavior and the reference values in the regression tests were
cross-checked against Chrome's `getScreenCTM()` and its rendering of the same
markup, which implements CSS Transforms Level 1 for SVG.

## Summary

- Transform lists are applied in SVG order: the **leftmost function is the
  outermost**, so `translate(…) scale(…)` scales the geometry and then moves
  it, while `scale(…) translate(…)` moves it and then scales the result.
- A child's transform is applied in its parent's user space: the ancestor
  transform is the outer one. Nested groups therefore compose
  `parent ∘ child`, not the reverse.
- `transform-origin` accepts lengths, percentages and the `left` / `center` /
  `right` / `top` / `bottom` keywords, in one-, two- and three-value form.
- `transform-box` selects the reference box used to resolve percentages and
  keywords on `transform-origin`: `view-box` (default), `fill-box`,
  `stroke-box`, plus `content-box` and `border-box` aliases.
- Invalid `transform` or `transform-origin` values never raise and never leak
  state into another element. An invalid transform is ignored; an invalid
  origin falls back to the initial `0 0` of its reference box.
- Rendering the same parsed tree twice is idempotent.

## Transform list order

The SVG transform attribute is a list evaluated left to right, each function
nested inside the previous one. In matrix terms, for
`transform="A B C"` the effective matrix is `A · B · C`: a point of the
element is first mapped by `C`, then `B`, then `A`.

| Attribute | Result |
| --- | --- |
| `translate(100,50) scale(2)` | scale first, then translate: `(10,10) → (120,70)` |
| `scale(2) translate(100,50)` | translate first, then scale: `(10,10) → (220,120)` |

The same rule holds across elements: a group's transform is applied after
(outer to) its children's transforms. `matrix`, `rotate`, `skewX`, `skewY`,
`scale` and `translate` all compose the same way.

## transform-origin syntax

| Form | Example | Meaning |
| --- | --- | --- |
| one length/percentage | `30%` | x position; y is `50%` |
| one x keyword | `left`, `right`, `center` | x position; y is `50%` |
| one y keyword | `top`, `bottom` | y position; x is `50%` |
| two values | `10px 20px`, `left top`, `center 25%` | x, then y |
| two values, y keyword first | `top left`, `top center` | keyword order is free |
| three values | `10px 20px 30px` | third is a z offset, ignored for 2D |

`center` is valid on both axes. Two keywords on the same axis (`left right`,
`top bottom`) make the value invalid.

Lengths may use any absolute CSS unit (`px`, `in`, `cm`, `mm`, `pt`, `pc`) and
the font-relative `em`, `ex`, `ch`, resolved against the element's
`font-size`. Percentages are resolved against the width (x) or height (y) of
the reference box.

## Reference boxes (`transform-box`)

| Value | Box used |
| --- | --- |
| `view-box` (default) | `0 0` to the viewBox size; the viewBox `min-x`/`min-y` is **not** added |
| `fill-box` | the object bounding box, ignoring the stroke |
| `stroke-box` | the object bounding box, expanded by half the painted stroke width |
| `content-box` | treated as `fill-box` (SVG has no CSS content box) |
| `border-box` | treated as `stroke-box` (SVG has no CSS border box) |

Notes:

- The default is `view-box`. For the outermost `<svg>`, its own transform uses
  the viewport size and an initial origin of `50% 50%` (the viewport centre),
  as browsers do; all other elements start at `0 0`.
- With a letterboxed viewport (for example `width="400" height="200"` with a
  square viewBox), `view-box` percentages use the viewBox size, not the
  letterboxed viewport width/height.
- For a `<g>`, the object box covers the children **with their own
  transforms**; definitions (`<defs>`, gradients, masks, …) are ignored.
- For `<use>`, the box is the referenced content, and the `use` element's own
  `x`/`y` are not part of it.
- For an inner `<svg>`, the element's own box is its contents in its own user
  (viewBox) coordinates; inside an ancestor's box it contributes its viewport
  rectangle, as it clips to that viewport.
- `<image>` uses its `x`/`y`/`width`/`height` rectangle. `<text>` uses the
  measured glyph box; when it is needed before layout, CairoSVG lays the text
  out without painting.

`transform-box` is a CSS property, not an SVG presentation attribute: browsers
only honor it through `style="transform-box:fill-box"`. CairoSVG also accepts
it as a plain attribute for convenience. An unknown value is ignored and
behaves as `view-box`.

## Invalid values and state

- An invalid `transform` (unknown function, wrong arity, percentage, CSS-only
  function such as `translateX`, or a unit such as `deg`) is **ignored**: the
  element is drawn untransformed.
- An invalid `transform-origin` (unknown keyword, wrong token count, a length
  on a keyword axis, …) **falls back to `0 0` of the reference box**, the
  property's initial value; the transform itself is still applied.
- A non-invertible matrix (e.g. `scale(0)`) clips the element to an empty path
  instead of painting it.
- Attributes consumed while drawing a reference (`x`, `y`, `viewBox`, `mask`
  on `<use>`; `x`/`y` on `<image>`) and the pattern definition are restored or
  copied afterwards, so the same parsed tree can be rendered repeatedly with
  identical output.

## Known limitations

- 3D transforms and the third `transform-origin` value are parsed but have no
  3D effect: CairoSVG renders 2D only.
- Transform functions named in CSS `transform` (rather than the SVG attribute)
  are not supported beyond what the parser already inherited.
- `stroke-box` uses half the painted stroke width; non-uniform stroke scaling
  by an ancestor is not accounted for, so heavily skewed strokes can be a few
  pixels off.
- Text `fill-box`/`stroke-box` uses CairoSVG's own text bounding box, which is
  built from the same metrics the glyphs are painted with. It is consistent
  between CairoSVG runs, but it does not match a browser's ink extent (the
  box spans the baseline and advance width rather than the glyph outline), so
  percentages on text can land differently than in Chrome. Use explicit
  lengths for text when a browser-identical origin matters.
- Filters and masks are applied after the element's transform; their own
  coordinate systems are unchanged by this work.

## Verifying the geometry

```bash
# Pixel-level regression tests (colours, probes and bounds)
.venv/bin/python -m pytest -q cairosvg/test_transform_origin.py

# Demo images showing origins, corners and layer order
.venv/bin/python demo_transform_origin.py
# → demo_pngs/transform_origin_*.png

# The generated SVG, if you want to inspect it in a browser
.venv/bin/python demo_transform_origin.py --stdout
```

The demo scenes cover:

- `centres` — the same rotation with the default origin versus
  `fill-box` + `50% 50%`;
- `asymmetric` — an off-centre rectangle rotated about all four corners;
- `layers` — nested groups, each rotating about its own box centre;
- `ordering` — the same two operations in both list orders;
- `use_reuse` — one referenced rectangle placed twice with different origins.

Each demo is plain geometry with a dashed outline of the untransformed shape
and a blue dot at the rotation origin, so the centre and corner positions can
be measured by eye.
