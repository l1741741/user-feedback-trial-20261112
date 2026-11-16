# Gradient inheritance through `href`

CairoSVG lets a `<linearGradient>` or a `<radialGradient>` inherit the
parameters and the `<stop>`s of another gradient with `href` (or the
deprecated `xlink:href`). This folder contains an offline example showing the
supported behaviour.

## Rules

A gradient using `href="#other"`:

* inherits every attribute of the referenced gradient it does not set itself:
  `x1`, `y1`, `x2`, `y2`, `cx`, `cy`, `r`, `fx`, `fy`, `gradientUnits`,
  `gradientTransform`, `spreadMethod`;
* inherits the `<stop>` children of the referenced gradient **only if it has
  no `<stop>` of its own**;
* keeps its own attributes and its own stops when both are given: the
  referrer always wins;
* follows the whole `href` chain, a nearest definition winning over a more
  distant one;
* ignores missing references and cycles: the attributes and stops declared
  locally are used, exactly like before.

Inheriting never modifies the referenced gradient. A definition shared by
several shapes, with different opacities and transforms, is therefore rendered
the same way whatever the order of the drawing nodes.

`gradientUnits` (object bounding box or user space), the coordinate reference
(`gradientUnits="userSpaceOnUse"` coordinates are in user units, otherwise in
fractions of the object bounding box), `spreadMethod` and `gradientTransform`
are all inherited and applied as usual.

## Example

```xml
<defs>
  <linearGradient id="spline" x1="0%" y1="0%" x2="100%" y2="0%">
    <stop offset="0" stop-color="#e4002b"/>
    <stop offset="0.5" stop-color="#7a007a"/>
    <stop offset="1" stop-color="#0033e4"/>
  </linearGradient>

  <!-- Same stops, vertical direction. -->
  <linearGradient id="bar" href="#spline"
                  x1="0%" y1="0%" x2="0%" y2="100%"/>

  <!-- Second inheritance level: inherits everything from bar. -->
  <linearGradient id="bar-2" href="#bar" y1="0%" y2="100%"/>

  <!-- Inherits the spline stops and spread method, becomes radial. -->
  <radialGradient id="dot" href="#spline"
                  cx="50%" cy="50%" r="50%"/>

  <!-- Own stops win over the inherited ones. -->
  <linearGradient id="bar-own" href="#bar">
    <stop offset="0" stop-color="#00b000"/>
    <stop offset="1" stop-color="#003000"/>
  </linearGradient>
</defs>

<rect width="90" height="100" fill="url(#spline)"/>
<rect x="100" width="90" height="100" fill="url(#bar)"/>
<rect x="200" width="90" height="100" fill="url(#dot)" opacity="0.5"/>
```

## Files

* `icons.svg`: five icons side by side, referencing shared gradients with
  different shapes, opacity and transform.
* `icons-reordered.svg`: the same icons with the drawing nodes in reverse
  order.

Both files render to byte-identical PNGs:

```bash
cairosvg examples/gradient-inheritance/icons.svg -o /tmp/icons.png
cairosvg examples/gradient-inheritance/icons-reordered.svg -o /tmp/reordered.png
cmp /tmp/icons.png /tmp/reordered.png
```

or, without writing files:

```bash
python - <<'EOF'
from cairosvg import svg2png
folder = 'examples/gradient-inheritance/'
assert (svg2png(url=folder + 'icons.svg') ==
        svg2png(url=folder + 'icons-reordered.svg'))
print('identical')
EOF
```

The same check, plus per-pixel color assertions on the rendered PNG, is part
of the test suite:

```bash
pytest cairosvg/test_gradient_inheritance.py
```
