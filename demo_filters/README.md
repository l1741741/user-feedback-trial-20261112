# Filter offset demo

`filter_offset_demo.svg` is a small, font-free fixture that exercises the
filter behaviour changed in CairoSVG's `feOffset`, filter region and
primitive subregion handling. It is made of four recognizable layers:

| Layer | Geometry | What it checks |
| --- | --- | --- |
| Black frame | unfiltered sibling, `x=4 y=4 232x172`, 2px stroke | Unfiltered siblings keep their exact position |
| Red rectangle | `x=40 y=30 60x30`, filter `drop-shadow` | Semi-transparent shadow offset by `(24, 12)`, source on top |
| Green rectangle | `x=40 y=100 40x20`, filter `clipped-offset` | Filter region reduced to the source box clips the offset |
| Lime square | unfiltered sibling, `x=190 y=130 20x20` | No layer is moved by a neighbouring filter |

The drop shadow is the documented chain:

```xml
<filter id="drop-shadow" x="-50%" y="-50%" width="200%" height="200%">
  <feOffset in="SourceAlpha" dx="24" dy="12" result="offset-alpha"/>
  <feFlood flood-color="black" flood-opacity="0.5" result="shadow-color"/>
  <feComposite in="shadow-color" in2="offset-alpha" operator="in" result="shadow"/>
  <feMerge>
    <feMergeNode in="shadow"/>
    <feMergeNode in="SourceGraphic"/>
  </feMerge>
</filter>
```

The region of `clipped-offset` is `0% 0% 100% 100%` of the source box, so the
`(16, 8)` offset is clipped on the right and bottom edges: the visible green
area is `(56, 108)` to `(80, 120)`, not the full shifted rectangle.

## Render and verify offline

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib"  # macOS, if needed
.venv/bin/python demo_filters/render_demo.py
```

The script writes `filter_offset_demo.png` next to the fixture and asserts 17
interior/exterior pixel probes (source color, shadow alpha, transparent areas,
clipped region, untouched siblings). It exits non-zero on the first mismatch,
so it also works as a smoke test in CI.

Equivalent pixel checks run in `cairosvg/test_filter.py`:
`python -m pytest -q cairosvg/test_filter.py`.
