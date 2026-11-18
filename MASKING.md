# Mask rendering in CairoSVG

CairoSVG renders the SVG `<mask>` element with its two distinct semantics
kept separate. This document states exactly what is implemented and what is
not, so that the two mask kinds are never silently treated as the same
thing.

## The two mask semantics

### Luminance masks (default)

When `mask-type` is `luminance` (the default, and the only behavior defined
by SVG 1.1), the coverage of a mask pixel is

```
coverage = luminance(R, G, B) × alpha
```

* The luminance uses the sRGB / Rec. 709 coefficients
  `0.2126·R + 0.7152·G + 0.0722·B`.
* Pure black hides, pure white shows, and a color shows according to its
  relative luminance (for example red is darker than green).
* A fully transparent mask pixel always hides, regardless of its color.
  Mask content opacity (including group and element opacity *inside* the
  mask) multiplies the luminance.
* When `color-interpolation="linearRGB"` is set on the `<mask>` element,
  the channels are linearized (inverse sRGB gamma) before the luminance is
  computed. Otherwise the encoded sRGB values are used directly.

### Alpha masks (`mask-type="alpha"`)

When `mask-type="alpha"` (an SVG 2 feature) is set on the `<mask>`
element, only the mask content's alpha channel defines coverage; the RGB
channels are ignored. An opaque black shape therefore fully shows, whereas
the same shape in a luminance mask would fully hide.

Cairo itself only offers alpha masking (`Context.mask_surface`). CairoSVG
renders the mask content to an image surface and, for luminance masks,
computes the luminance-to-alpha conversion in Python before handing the
result to Cairo. Alpha masks skip the luminance step.

## Coordinate spaces and coverage

* `maskUnits` (the mask *region*: `x`, `y`, `width`, `height`) defaults to
  `objectBoundingBox`. With `userSpaceOnUse` the region is positioned in
  the user space in effect for the masked element, so the same mask reused
  by objects of different sizes or positions keeps a fixed position.
* `maskContentUnits` (the coordinates of the mask *contents*) defaults to
  `userSpaceOnUse`. With `objectBoundingBox`, content coordinates are
  fractions of each masked element's own bounding box, so a reused mask is
  independently re-scaled per object.
* Pixels outside the resolved mask region are always fully transparent.
  Transparent background pixels never leak color through the mask.
* The mask definition node is never mutated while rendering. Reusing one
  mask from several elements — including siblings and nested groups — gives
  each element its own independently computed mask image, and repeated
  exports are byte-identical.

## How opacity combines

These two alphas have different meanings and are each applied once:

* The **mask content's** opacity is part of the mask value
  (`luminance × alpha`).
* The **masked element's** own `opacity` (including a wrapping group's
  opacity) is applied to the composited result after masking.

So a group with `opacity="0.5"` masked by a `0.5`-alpha mask yields
`0.5 × 0.5 = 0.25` coverage. The element opacity is never folded into the
mask content and never multiplied twice.

## Supported

* Luminance masks and alpha masks (`mask-type`), distinguished explicitly.
* sRGB luminance and `color-interpolation="linearRGB"` on the `<mask>`
  element.
* `maskUnits` and `maskContentUnits`, both `userSpaceOnUse` and
  `objectBoundingBox`, including per-object reuse with different sizes and
  positions.
* Mask content built from shapes, paths, text, gradients, patterns,
  images, `<use>` and nested groups (including opacity inside the mask).
* Mask content and masked elements carrying transforms, and non-square
  objects.
* Nested masked groups and masks combined with group opacity.
* Output to PNG, PDF, PostScript/EPS and SVG (the mask is rasterized to an
  image surface in every case).

## Not supported yet

These are deliberately not guessed at; encountering them does not change
the documented behavior of the features above.

* Per-element CSS overrides of `mask-type` on the mask's children. Only the
  `mask-type` attribute on the `<mask>` element itself is honored.
* `color-interpolation` set on individual mask children rather than on the
  `<mask>` element.
* The SVG 2 longhand properties `mask-image`, `mask-mode`, `mask-repeat`,
  `mask-position`, `mask-size`, `mask-origin`, `mask-clip` and
  `mask-composite`, and multiple comma-separated mask layers. Only the SVG 1
  `<mask>` element referenced through the `mask` presentation attribute is
  implemented.
* `mask` references that resolve to elements other than a `<mask>`.
* Filter effects applied to a mask's content are ignored, matching the
  pre-existing limited filter support.
* Object bounding boxes for some elements (for example groups without
  geometry, or text without a computed box) fall back to the viewport when
  a box cannot be derived; this mirrors the existing gradient and pattern
  handling.

## Tests

`cairosvg/test_mask.py` renders offline SVGs made only from solid colors,
grays and semi-transparent blocks and asserts exact pixels. It covers
luminance versus alpha behavior, color coefficients, mask and group
opacity, region coverage and transparency, mask reuse in both coordinate
systems, transforms, non-square objects, gradient edges and deterministic
repeated exports.
