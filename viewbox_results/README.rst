# viewBox / preserveAspectRatio rendering results

Real PNG output produced by the fixed CairoSVG code. The shared input is a
40x30 icon with four colored corner quadrants and a white center cross:

- red    = top-left quadrant
- lime   = top-right quadrant
- blue   = bottom-left quadrant
- yellow = bottom-right quadrant
- white  = center cross (4 user units thick)
- gray   = container background

Regenerate every image with::

    .venv/bin/python -I viewbox_results/generate.py

Each individual image is rendered at 4x scale; the three `contact_*.png`
sheets collect them with labels.

## Files

### Nested and root containers (`nested_*`, `root_*`, `contact_nested_and_root.png`)

The same icon placed in portrait (30x60), landscape (60x30) and square
(40x40) containers, in `meet`, `slice` and `none` modes, both as a nested
`<svg>` and as the document root.

- `meet`: uniform scale to fit, centered; gray letterbox bands appear where
  the container aspect ratio does not match the icon.
- `slice`: uniform scale to cover the whole viewport; overflowing corners are
  clipped at the viewport edges, no gray remains inside.
- `none`: independent x/y scaling; the quadrants are stretched onto the four
  viewport corners while the cross stays centered.

### Non-zero viewBox origin (`nonzero_origin_*`, `contact_nonzero_origin.png`)

ViewBox `-10 -20 40 30` in a 60x60 container. The min-x/min-y offset maps
through the same meet/slice/none transform and the content stays centered and
correctly clipped.

### Isolation and error handling (`contact_isolation_and_errors.png`)

- `slice_clip_sibling.png`: a slice viewport whose content overflows is
  clipped to its 80px-wide effective viewport; the blue sibling layer on the
  right is never overwritten.
- `none_then_default_meet.png`: the first nested image uses `none`
  (independent scaling, fills its box); the second keeps the default `meet`
  letterboxing, proving the `none` state does not leak to the next image.
- `degenerate_viewbox_identity.png`: a `viewBox="0 0 0 0"` no longer produces
  zero-scale (blank) output or a division error; the identity mapping is used
  and the red rectangle renders.

## Error contract

- Malformed `viewBox` (wrong value count or non-numeric) raises `ValueError`
  on every output surface (PNG/PDF/PS/EPS/SVG).
- A zero-sized document keeps the historical
  `ValueError('The SVG size is undefined')`.
- Invalid or empty `preserveAspectRatio` falls back to the specification
  default `xMidYMid meet` instead of raising `IndexError` or misaligning.
- When a nested viewport fails, every saved cairo state is restored (see
  `test_draw_state_save_restore_balanced_on_error`).
