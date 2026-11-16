# Nested viewports and percentages

This note describes how CairoSVG resolves percentage lengths and viewport
state in nested `svg` elements (including `symbol` used through `use`), what
was wrong, and how to verify the behaviour locally.

## Rules implemented

A percentage on an element is relative to the **active viewport**, expressed in
that element's own user coordinate system:

| Element | Percentage reference |
| --- | --- |
| `svg`/`symbol` with `viewBox` | the viewBox size (`width`, `height` of the viewBox) |
| `svg`/`symbol` without `viewBox` | the viewport size (`width`/`height`, defaults to `100%` of the parent) |
| any other element | the size of the viewport it is drawn in |

The mapping performed by `preserveAspectRatio` does not change this reference:
letterboxing, cropping (`slice`) and non-uniform scaling (`none`) all keep the
same user-unit length. A non-zero `viewBox` origin (`min-x`, `min-y`) only
translates the content, it does not shift the percentage origin.

Leaving a nested `svg` restores the parent viewport. A node without
`width`/`height` uses `100%` of the parent viewport; it never inherits the size
of a previously drawn sibling or of a previously referenced `symbol`.

Two error cases are handled explicitly:

- a `viewBox` with a **zero** width or height defines an **empty viewport**:
  the element and its children are not rendered;
- a `viewBox` that is empty or malformed (wrong number of values, negative
  size) is ignored, as browsers do, so the element keeps the parent viewport.
  Previously a three-value `viewBox` raised `IndexError`.

`preserveAspectRatio` is not inherited (it is not an inherited property in
SVG); the value of a nested `svg` comes from that `svg` only.

For `use`:

- `width` and `height` on the `use` override the corresponding dimension of
  the referenced `svg`/`symbol` **independently**, so a single dimension works
  and the other one keeps the referenced element's value (or `100%`);
- a referenced `symbol` behaves like a nested `svg` with the same viewport and
  the same geometry, including inside a letterboxed parent viewport;
- the mapping used is the one of the referenced element: a
  `preserveAspectRatio` on the `use` element itself has no effect, matching
  browsers.

## What was wrong

`size()` resolved percentages against `surface.context_width` and
`surface.context_height`. Those values were set to `rect_width / scale_x`
(the *visible* viewport size in user units) and were pushed by `set_context_size`
during `draw()` for the current node — so:

1. inside a letterboxed/cropped nested viewport the ratio of the two axes was
   wrong, and percentages were resolved against the outer canvas for one axis,
   which distorted exported PNG proportions;
2. the state was saved before the node handler ran and restored after, so a
   nested `svg` leaked its viewport into following siblings;
3. `node_format()` replaced a missing `width`/`height` with the viewBox size,
   so a nested `svg` without size adopted the viewBox size instead of `100%`
   of its parent;
4. `use` copied both dimensions only when *both* were present, so a single
   dimension was silently ignored;
5. a three-value `viewBox` raised `IndexError`, and a zero-size `viewBox` was
   drawn with a scale of 1 instead of rendering nothing.

## Verification

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/Users/lichao/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib:/opt/homebrew/lib"
.venv/bin/python -m pytest -q cairosvg/test_api.py cairosvg/test_viewport.py
.venv/bin/python demo/render_demo.py
```

`cairosvg/test_viewport.py` renders real PNGs and checks exact pixel colours at
points computed from the geometry (no reference images, no fonts). Each check
below compares a 50%-sized rectangle with an explicit 5-unit rectangle, so the
two must coincide exactly; a wrong reference makes them differ by a visible
amount.

`demo/render_demo.py` renders `demo/assets/viewport_percent.svg` (720x700) and
writes 4x crops of every panel to `demo/output/`. The panels are:

1. nested `svg` with `meet` — 140x70 viewport, 10x10 viewBox;
2. the same viewport through `<use>` of a `symbol`;
3. nested `svg` with `preserveAspectRatio="none"`;
4. a percentage-sized sibling after a nested `svg` (uses the outer canvas);
5. three levels of viewports (300x190, 40x40, 20x20);
6. one symbol reused at two different sizes;
7. a percentage ruler (ticks every 10% of its own viewBox);
8. a non-zero `viewBox` origin (`20 30`).

The overall image and every crop were checked against the geometry of the
reference renderer (headless Chrome); the remaining differences are sub-pixel
antialiasing on circles.
