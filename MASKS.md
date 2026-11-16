# SVG mask support in CairoSVG

This document describes how CairoSVG renders SVG masks (`<mask>`), which
behaviors are supported, and which are not. The two mask types —
**luminance** and **alpha** — are distinct features and are rendered
differently; they are never silently treated as equivalent.

## Masking value semantics

The masking value of a mask pixel depends on the `mask-type` property of
the `<mask>` element:

- **`mask-type: luminance` (default, SVG 1.1 behavior):** the masking
  value is the luminance of the mask color multiplied by its alpha:
  `mask = (0.2126·R + 0.7152·G + 0.0722·B) · A` (Rec. 709 luma
  coefficients, applied to sRGB channel values). A black mask pixel hides
  the element, a white pixel shows it, a 50% gray pixel shows it at 50%
  opacity, and a fully transparent pixel hides it regardless of its color.
- **`mask-type: alpha`:** only the alpha channel of the mask content is
  used; colors are ignored. A black opaque mask pixel masks fully.

`mask-type` may be set as an XML attribute, in an inline `style`
attribute, or from a `<style>` stylesheet, and is read from the `<mask>`
element only.

## Supported

- `mask="url(#id)"` (attribute or CSS `mask` property) on shapes, paths,
  text, images and groups, referencing a `<mask>` in the same document.
- `maskUnits="userSpaceOnUse"` and `maskUnits="objectBoundingBox"`
  (default) for the mask region (`x`, `y`, `width`, `height`, defaulting
  to `-10%`, `-10%`, `120%`, `120%`). With `objectBoundingBox`, the region
  is a fraction of the bounding box of the element referencing the mask.
  Pixels outside the mask region are always masked out.
- `maskContentUnits="userSpaceOnUse"` (default) and
  `maskContentUnits="objectBoundingBox"` for the mask content coordinate
  system. With `userSpaceOnUse`, mask content shares the user space of
  the masked element (including its transforms); with
  `objectBoundingBox`, content coordinates are fractions of the masked
  element's bounding box.
- Mask content made of shapes, paths, text, images, gradients, patterns,
  and nested groups — including group opacity inside the mask, which
  contributes to the masking value.
- Reuse of one `<mask>` by several elements: the bounding-box-dependent
  geometry is resolved per referencing element, so objects of different
  sizes and positions each get a correct mask.
- Opacity combination: the masked element's (or group's) `opacity` and
  the mask's masking value are multiplied exactly once each — element
  opacity is applied to the masked result, mask opacity comes from the
  mask content itself.
- Transparent backgrounds: fully masked pixels are fully transparent
  (`rgba(0,0,0,0)`); no color bleeds into masked-out areas.
- All output formats (PNG, PDF, PS, SVG) and repeated exports of the same
  document (rendering is deterministic and mask definitions are not
  mutated between uses).

## Not supported / limitations

- **Masks are rasterized**, including for vector outputs (PDF, PS, SVG):
  the mask is rendered to a bitmap at the target device's resolution
  (pixels for PNG, points for PDF/PS) and applied as an image mask.
  Vector outputs therefore contain a raster mask rather than vector mask
  content.
- Only same-document fragment references (`url(#id)`) are resolved;
  masks referenced from external files are not fetched.
- The CSS `mask` shorthand is only understood when it is a plain
  `url(#id)` reference; mask layers, `mask-position`, `mask-size`,
  `mask-repeat`, `mask-mode`, etc. are not supported.
- Luminance is computed directly on sRGB channel values without color
  management; `color-interpolation` (e.g. `linearRGB`) is ignored.
- Filter effects (`filter` property) inside mask content are limited to
  CairoSVG's general (partial) filter support.
- An `opacity` attribute on the `<mask>` element itself is ignored (the
  `<mask>` element is never rendered directly); put opacity on the mask's
  content instead.
- A `mask` attribute on a `<use>` element is ignored (pre-existing
  limitation of `<use>` handling).

## Implementation notes

`Surface.draw` paints the masked element into a Cairo group, then
`defs.paint_mask` renders the mask content into an offscreen image in the
masked element's user space, converts it to masking values
(`defs.apply_mask_values`), and applies it with `Context.mask` through a
pattern positioned in device space. The element's opacity is folded into
the masking values at that point, so it must not be (and is not) applied
again to the element's fill/stroke.

Pixel-level regression tests live in `cairosvg/test_mask.py`; runnable
demos with visual outputs live in `demo/`.
