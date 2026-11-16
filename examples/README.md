# `<image>` rendering examples

Self-contained, offline rendering examples for the sizing, positioning and
alignment of the SVG `<image>` element. Nothing here fetches a network
resource: the raster inputs are generated locally from solid colors.

## Run

```bash
python examples/render_examples.py
```

The script writes its inputs into `examples/assets/` and one PNG per scenario
plus a labeled `contact_sheet.png` into `examples/output/`.

## What each scenario demonstrates

| Output | What it proves |
| --- | --- |
| `landscape_meet / slice / none` | The three fit modes in a landscape region: meet keeps the whole image with gutters, slice fills the region and crops the overflow, none stretches non-uniformly. |
| `portrait_meet / slice / none` | The same three modes in a portrait (narrow, tall) region. |
| `align_minmin / align_maxmax` | Non-zero `preserveAspectRatio` alignment anchors the slice crop to the requested corner. |
| `nested_rotate` | A slice image inside a rotated, translated group is clipped to the transformed region and never overdraws outside it. |
| `exif_jpeg / exif_png` | EXIF orientation 6 is applied to JPEG **and** PNG alike; the red corner ends up top-right in both, instead of being ignored on the PNG path. |
| `alpha_compositing` | The image's transparent border shows the green backdrop through real alpha — it is never filled opaque black. |
| `repeated` | Three references to one image at different sizes are rendered independently. |
| `zero_size` | Explicit `width="0"` / `height="0"` images paint nothing; the backdrop is untouched. |
| `clip_guard` | A fully opaque image under slice stays inside its viewport rectangle (the magenta would reveal any leak). |

The source image is a 40×30 RGBA PNG with a 4px transparent border, four
opaque corner quadrants (red / green / blue / yellow) and a white cross with
a black dot marking its center, so crop range and orientation are directly
visible.
