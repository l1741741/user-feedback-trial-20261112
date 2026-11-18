# Shared-gradient brand icons (offline example)

This example shows several brand icons rendered side by side that all reuse
the **same public gradient definitions** through SVG `href` /
`xlink:href` references. It demonstrates the fix for gradient inheritance in
CairoSVG: referencing a shared gradient must not mutate the shared
definition, so drawing the icons in any order always gives the same colors.

## Files

- `brand-icons-order-1.svg` — six icons, drawn from icon 1 to icon 6.
- `brand-icons-order-2.svg` — the **same** icons and definitions, with the
  icon nodes emitted in reverse order (icon 6 first, icon 1 last). Each icon
  keeps the same slot and the same gradient reference.
- `render_and_check.py` — renders both SVGs to PNG with CairoSVG and samples
  the actual pixels to prove the two orders are identical and that every
  icon keeps its own color distribution.
- `brand-icons-order-1.png` / `brand-icons-order-2.png` — generated output.

## What each icon exercises

| Slot | Shape / style | Gradient usage |
| ---- | ------------- | -------------- |
| 1 | rounded square | plain reference: inherits the public blue→red horizontal gradient and its stops |
| 2 | square | overrides the direction (vertical) but keeps its **own** green→yellow stops |
| 3 | circle, `opacity="0.55"` | inherits the public gradient with a different opacity |
| 4 | diamond | radial reference with a `gradientTransform`, inheriting public stops |
| 5 | square | leaf of a 3-level chain (`leaf → mid → base`) with its own green→yellow stops |
| 6 | square | paints the chain's **intermediate** `mid` gradient directly; it must keep its inherited blue→red vertical gradient even though icon 5 resolved the same chain first |

Icons 5 and 6 are the key regression case. Before the fix, resolving `leaf`
first replaced the shared `mid` node inside CairoSVG's definition dictionary
(and, for documents loaded from a file URL, polluted the shared tree cache),
so painting `mid` afterwards produced a black/empty rectangle. With the fix
the resolution is non-destructive and order-independent.

## How inheritance is resolved

`resolve_def_href` in `cairosvg/defs.py`:

1. Walks the `href` chain to the effective definition.
2. Returns a **fresh** merged node for every reference instead of replacing
   entries in the shared `surface.gradients` / `surface.patterns`
   dictionaries.
3. Copies each referenced attribute that the referencing node does not
   define, while the referencing node keeps its own attributes and its own
   `<stop>` children (when it has none, it inherits the referenced stops).
4. Gives the synthetic node a private `tree_cache` so it never overwrites an
   entry of the document's shared cache (this mattered when the SVG was
   loaded from a file URL rather than a byte string).
5. Stops safely on a missing target or a reference loop, rendering the node
   with whatever attributes and stops it has instead of raising or
   polluting unrelated shapes.

## Running the check

Use the project virtualenv and pass the script path explicitly. Cairo is a
native shared library, so point the dynamic loader at the library location
on your machine (Homebrew on macOS shown below):

```bash
cd examples/gradient_icons
DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib" \
    ../../.venv/bin/python -I render_and_check.py
```

A successful run prints `RESULT: all checks passed`, writes the two PNGs,
and confirms that:

- both drawing orders produce byte-identical PNG output;
- every icon has the expected color distribution (sampled from real pixels);
- each icon slot matches across the two orders.

You can also render a single file directly:

```bash
DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib" \
    ../../.venv/bin/python -m cairosvg brand-icons-order-1.svg \
        -f png -o brand-icons-order-1.png
```
