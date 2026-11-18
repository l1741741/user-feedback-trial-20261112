# Mask work area

Offline probes and demos for the SVG `<mask>` luminance/alpha fix. These
files are development and verification artifacts; the shipped implementation
lives in `cairosvg/mask.py` and the permanent tests in
`cairosvg/test_mask.py`.

## Layout

* `svg/` — hand-written offline SVGs using only solid colors, grays and
  semi-transparent blocks. No external resources.
* `probe.py` — renders every probe with the installed CairoSVG and prints
  sampled RGBA pixels for direct numeric checking.
* `demo.py` — renders each SVG *during the run* and builds `demo/index.html`
  showing every input source next to its freshly rendered PNG. There are no
  cached reference images; rerun it to regenerate.
* `out/` — PNGs produced by the probe runs and spot checks.
* `demo/` — generated demo page and per-probe PNGs.

## Run

Cairo needs its shared library on the fallback path on this machine:

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/Users/lichao/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib:/opt/homebrew/lib"
.venv/bin/python -I mask_work/probe.py
.venv/bin/python -I mask_work/demo.py
```

## What the probes cover

| Probe | Verifies |
| --- | --- |
| 01_luminance | black hides, gray is partial, white shows |
| 02_color_luminance | Rec.709 luma of red/green/blue |
| 03_mask_alpha | mask content opacity scales coverage |
| 04_reuse_userspace | userSpace mask reused by a transformed object |
| 05_outside_transparent | nothing bleeds outside the mask region |
| 06_group_opacity | group opacity × mask alpha applied once |
| 07_gradient | smooth luminance-to-alpha gradient edge |
| 08_nested_groups | nested masked groups keep distinct semantics |
| 09_sibling_reuse | siblings reuse one mask without mutation |
| 10_bbox_reuse | objectBoundingBox mask re-fits each object |
| 11_alpha_vs_luminance | the two `mask-type` modes differ |
| 12_transform_nonsquare | rotated/translated non-square objects |
| 13_linear_rgb | `color-interpolation="linearRGB"` |
| 14_mask_gradient_group | gradient and translucent group in a mask |
| 15_mask_use | `<use>` inside mask content |
| 16_leaf_opacity_mask | leaf opacity and mask alpha each applied once |
