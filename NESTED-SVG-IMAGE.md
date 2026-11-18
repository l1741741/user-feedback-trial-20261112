# Nested SVG images: isolation and size mapping

This note documents how CairoSVG renders an SVG document referenced by an
`<image>` element, what changed, and what is deliberately unchanged.

## What is fixed

An SVG document embedded with `<image href="icon.svg">` is a document of its
own: it has its own stylesheets, its own definitions (gradients, patterns,
clip paths, masks, filters) and its own coordinate system. It must therefore
be parsed and rendered with those, and only then its result is mapped into the
region of the `<image>` element.

Before this fix, the embedded document was drawn directly into the outer
surface:

- its definitions were stored in the outer surface's maps, keyed by id, so an
  icon's `gradient` or `clip` silently replaced an outer definition with the
  same id — and two icons defining the same id overwrote each other;
- it was sized with `node_format(..., reference=False)`, mixing the icon's
  `viewBox` with the outer document's units and size;
- the same cached tree was reused and mutated between embeddings;
- a broken embedded document raised and aborted the whole conversion.

The fix renders every embedded document on a dedicated `EmbeddedSurface` — a
Cairo recording surface — which is then painted into the `<image>` region.
Definitions, styles and coordinate systems cannot escape that surface, the
recording replays transparent areas as transparent, and it stays vector-based
for PDF, PS, EPS and SVG output (it is rasterized only by the PNG target, as
any other Cairo source).

## Size mapping

The intrinsic size of the embedded document is resolved in its own coordinate
system, with percentages and units relative to the `<image>` region (as for a
replaced element):

1. a fixed `width`/`height` on the root `<svg>` wins;
2. otherwise the `viewBox` width/height are used;
3. otherwise the `<image>` region size is used.

A percentage `width`/`height` on the embedded root is treated as "no intrinsic
size": it only repeats the region size and must not override a `viewBox`.

The icon is then rendered in a viewport of that intrinsic size, so its own
units and percentages resolve against its own size, and the `preserveAspectRatio`
attribute of the `<image>` element (`xMidYMid meet` by default) maps it into the
region. The region is clipped, so an icon painting outside its viewport cannot
bleed into the outer drawing. `opacity` on the `<image>` is applied to the
mapped result.

## Resources and access policy

Relative references inside an embedded document (`<image href="pixel.png">`,
`@import`) are resolved against that document's own source URL, exactly as
before: the tree is created with the embedded document's URL as its base.

The access policy is unchanged and is not relaxed by this fix:

- without `unsafe`, the default fetcher still only allows `data:` URLs, for the
  embedded document and for everything it references;
- with `unsafe`, external files are fetched as before (this is the documented
  opt-in for untrusted input).

## Failure handling

A malformed or unreadable embedded document (`xml.etree.ElementTree.ParseError`,
a defusedxml restriction, an unreadable resource, an undefined size) is ignored
and the conversion keeps drawing the following siblings.

Rendering is still guarded:

- the number of embedded documents rendered by one conversion counts towards
  the same 100 000 reference limit as `use`, and reaching it aborts the
  conversion on purpose instead of being silently skipped;
- a document that is already being rendered — an SVG embedding itself,
  directly or through other documents — is not rendered again, which breaks
  cycles that would otherwise recurse forever.

## Compatibility notes

- Rendering results change for documents embedding SVG images: this is the
  point of the fix. Documents whose embedded icons shared ids with the outer
  document, or relied on the outer size, render differently — and correctly.
- Vector output quickly replays the recording; very complex embedded icons may
  produce larger PDF/PS/SVG files than before, since their content is no
  longer flattened into the outer context.
- Embedded raster images keep the existing path: they are decoded with Pillow,
  pass through `map_image` (`invert_images`) and are painted as a Cairo
  pattern. `invert_images` also applies inside embedded SVG documents.
- The input SVG is never rasterized by CairoSVG itself to emulate this: no
  intermediate PNG replaces the embedded document, and no external tool is
  required.

## Offline case and tests

`test_nested_svg/` contains a main document embedding two icons that define the
same `gradient` and `clip` ids with different colors, an icon with a relative
raster resource, and a broken icon followed by siblings.
`cairosvg/test_embedded_image.py` renders those files and checks colors,
transparency, alignment and PNG dimensions from the actual PNG output, as well
as repeated embedding, transforms, relative resources, the access policy and
vector output.

Run them with:

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/path/to/cairo/lib"
.venv/bin/python -m pytest -q cairosvg/test_embedded_image.py cairosvg/test_api.py
```
