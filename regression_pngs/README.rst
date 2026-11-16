# Filter regression PNGs

These PNGs are rendered from the small, font-free, pure-color SVG fixtures in
`cairosvg/test_filter.py` (one file per test, named after the test). Each test
asserts exact RGBA values at interior/exterior pixels chosen by analytic
geometry, so antialiased edges are never used as comparison points.

Regenerate them with the fixtures from the tests:

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib"  # macOS, if needed
.venv/bin/python -m pytest -q cairosvg/test_filter.py
```

or render the interactive demo, which writes its own PNG:

```bash
.venv/bin/python demo_filters/render_demo.py
```

Coverage:

- `feOffset` in user space: positive and negative offsets, own `transform`,
  ancestor `transform`, and a filter applied to a group
- no other layer moving when a filtered element is offset
- `primitiveUnits="objectBoundingBox"`: separate x and y scales on a
  non-square object
- filter region clipping (`objectBoundingBox` and `userSpaceOnUse`)
- primitive subregion clipping, absolute and percentage values
- zero-sized filter region disabling the element
- empty filter producing transparent black
- missing filter and unsupported filter keeping the source graphic
- semi-transparent drop shadow via `feOffset` + `feFlood` + `feComposite` +
  `feMerge`
- `feBlend` modes and `feComposite` operators
- filter `href` inheritance (attributes and primitives)
- repeated conversions and multiple filtered instances being deterministic

`regression_pngs/` holds one canonical image per behaviour; the pixel probes in
the tests are the authoritative checks.
