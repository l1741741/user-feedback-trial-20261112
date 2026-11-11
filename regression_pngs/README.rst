# Viewport regression PNGs

These PNGs are rendered from the small, font-free, pure-color SVG fixtures
embedded in `cairosvg/test_viewport.py` (one file per test; the two
`test_use_single_dimension` variants are suffixed `_0`/`_1`).

Each test asserts exact RGB values at interior/exterior pixels chosen by
analytic geometry, so antialiased edges are never used as comparison points.
Regenerate them from the test sources with:

```bash
.venv/bin/python -m cairosvg <svg> -o regression_pngs/<name>.png
```

or simply run the pixel checks directly:

```bash
.venv/bin/python -m pytest -q cairosvg/test_viewport.py
```

Coverage:

- nested `svg` positioning in parent coordinates and viewport clipping
  (default and explicit `overflow=hidden`)
- `overflow=visible` painting outside the nested viewport
- percentage-sized nested `svg` against the parent viewport
- sibling percentage resolution after exiting a nested viewport
- deeply nested viewport restore (transform, size and clip)
- one `symbol` reused at two different sizes without state pollution
- `use` with only one of width/height specified, for both `symbol` and `svg`
- `preserveAspectRatio` meet / slice / none and non-zero viewBox origin
- per-reference `preserveAspectRatio` on reused symbols
- plain `use` of a `g` keeping its historical behavior
