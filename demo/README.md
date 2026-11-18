# `paint-order` demo

This folder contains the demo used to check and show the `paint-order`
support.

* `paint-order.svg` is the local input file. The three first panels share the
  exact same path geometry, stroke width and marker; only the paint order and
  the translation placing the panels differ. The last panel shows inheritance
  from a group with a local override.
* `paint-order.png` is the rendered output.
* `paint-order-zoom.png` shows the overlapping areas of the four panels,
  cropped from the rendered PNG and scaled up. The geometry in the SVG is
  never modified by the demo.

Regenerate both PNGs with:

```bash
python demo/render_demo.py
```

The script uses CairoSVG itself (`cairosvg.svg2png`) and only crops/scales
the result with Pillow; it does not touch the input shapes.
