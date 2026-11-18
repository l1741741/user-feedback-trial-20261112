# Smooth-command demos and differential tooling

This directory holds local, runnable evidence for the SVG path smooth-command
(`S/s`, `T/t`) control-point fix. The project virtualenv must be able to load
the native Cairo library; on this machine that means:

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/Users/lichao/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib:/opt/homebrew/lib"
```

## Demos

- `generate_smooth_demo.py` renders `demos/output/smooth_comparison.png` and
  `smooth_isolation.png`. Each comparison panel draws the same trajectory once
  with explicit `C/c`/`Q/q` control points and once with the `S/s`/`T/t`
  shorthand; the two rows should look identical. The isolation figure shows two
  independent `<path>` elements that must not share control-point state.
- `verify_equivalence.py` renders each explicit/smooth pair to a minimal PNG
  and asserts the byte streams are identical, covering cubic and quadratic
  curves, relative commands, chained smooth groups, and the line/moveto/
  closepath/arc reset rules.

```bash
.venv/bin/python demos/generate_smooth_demo.py
.venv/bin/python demos/verify_equivalence.py
```

## Differential fuzzer

`../tools/diff_fuzz.py` drives `cairosvg.path.path()` onto a Cairo recording
surface and compares the emitted path primitives against an independent
spec-compliant parser in `../tools/svg_path_reference.py`. Arc-free paths are
compared segment-by-segment with exact control points; paths containing arcs
use a scale-normalized trajectory distance because the two implementations
subdivide arcs independently.

```bash
.venv/bin/python tools/diff_fuzz.py 6000
```

The reference parser is deliberately independent of CairoSVG for everything
except the arc frame math (which mirrors the renderer so arc subdivision
noise does not mask smooth-command mistakes).
