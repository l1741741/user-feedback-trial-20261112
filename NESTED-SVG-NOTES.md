# Nested SVG image: rendering isolation and size mapping

CairoSVG draws SVG documents embedded through `<image href="…svg">` as
vector content directly on the parent Cairo context. The embedded
document is **not** rasterized: its shapes, gradients, text and nested
images are parsed and painted with Cairo like the rest of the document.

## Problems fixed

Before this change, embedded SVG documents shared the rendering state of
the parent surface, which produced three classes of defects:

1. **Definition and style collisions.** Markers, gradients, patterns,
   masks, clip paths, filters and image definitions of every document
   were stored in the same dictionaries on the surface. Two documents
   defining `<linearGradient id="paint">` with different colors
   overwrote each other, and a same-id definition inside an icon could
   replace the definition of the main document. The same problem applied
   to `clipPath`, `mask`, `pattern`, `filter` and marker ids.
2. **Unstable size mapping.** The intrinsic width/height and viewBox of
   the embedded document were partially resolved against the parent
   viewport, the document's own `preserveAspectRatio` was ignored (the
   image node's default `xMidYMid meet` was used instead), and the
   viewBox scaling was applied twice. Icons with a viewBox and a
   transparent background were therefore stretched or offset
   unpredictably instead of being mapped deterministically onto the
   image region.
3. **Failure propagation.** A malformed or unreadable embedded document
   raised while parsing or drawing and aborted the whole conversion, so
   the sibling elements of the broken image were never painted.

## Behavior after the fix

### Document isolation (`surface.isolated_definitions`)

While an embedded SVG document is drawn, the surface temporarily uses
fresh, empty definition dictionaries
(`markers`, `gradients`, `patterns`, `masks`, `paths`, `filters`,
`images`). The parent dictionaries are restored as soon as the nested
document has been drawn, even if drawing fails. Consequences:

- references such as `fill="url(#paint)"` or `clip-path="url(#frame)"`
  inside an embedded document resolve against that document's own
  definitions;
- identical ids in different documents never collide and never leak to
  the parent or to sibling images;
- documents nested several levels deep each get their own scope;
- sub-surfaces created by masks and patterns inside the embedded
  document inherit the isolated dictionaries.

The CSS style sheets of each document are still parsed independently by
`Tree`, so document-level styles remain scoped as well.

### Coordinate and size mapping (`image.draw_svg_image`)

1. The embedded document is parsed with its own URL. Relative resources
   referenced by the embedded document (other images, fonts, style
   sheets) are therefore resolved against the embedded document's
   location, not the parent's location.
2. A document without intrinsic width/height uses the image region as
   its viewport, following the SVG sizing rules for referenced SVG
   documents.
3. The document's own `preserveAspectRatio`
   (`xMidYMid meet` by default, `none`, `… slice`, alignments) drives
   the mapping of its intrinsic viewport onto the image region. The
   alignment is applied exactly once; the nested `<svg>` drawing maps
   the viewBox without a second alignment.
4. The image region is clipped, so content overflowing the region can
   never paint onto the parent document. Transparent areas of the
   embedded document stay transparent (no background is synthesized).
5. `opacity` on the `<image>` node alpha-composites the whole embedded
   document (previously it was ignored for SVG images).
6. A transform on the `<image>` node or an ancestor group is applied
   normally, before the document's own coordinate system.

### Failure containment

- A resource that cannot be fetched, an embedded document that cannot be
  parsed, or an embedded document that fails while drawing is treated as
  a broken image: it is skipped and the parent document, including all
   sibling elements, is still rendered.
- The Cairo context is saved before drawing and always restored, so a
   failure cannot leave clipping or transforms on the parent context.
- Raster images that cannot be decoded are handled the same way.

### Resource access policy is unchanged

The fix does not introduce any new fetching capability:

- the embedded document reuses the same URL fetcher as the node that
  embeds it; when no explicit trusted fetcher is provided and
  `unsafe=False` (the default), `cairosvg.url.safe_fetch` still only
  permits `data:` URLs and returns an empty placeholder for any other
  URL, including `file:` and network URLs;
- no network access, entity expansion or external DTD resolution is
  enabled — the `unsafe` flag semantics are untouched;
- the offline demo only uses `file://` URLs located inside its own
  directory through an explicit, restricted fetcher.

## Compatibility notes

- Public Python and command-line APIs are unchanged; no function
  signature changed and no new dependency was added.
- Raster (PNG/JPEG/…) image handling is unchanged except that decode
  errors now skip the image instead of aborting the whole render, which
  matches the SVG requirement that broken images be treated as empty.
- Rendered output for well-formed documents without id collisions is
  intended to be identical or more spec-compliant. Two observable
  behavior changes are deliberate corrections:
  - embedded SVG documents now honor their own `preserveAspectRatio`
    instead of always using the image node's default;
  - same-id definitions in different documents are now scoped per
    document instead of sharing one global namespace.
- Embedded SVG content remains vector-based end to end. The internal
  document is never manually rasterized and the input is never rewritten
  or replaced: only the Cairo context and the surface's definition
  dictionaries are scoped during drawing.
- PDF, PostScript, EPS and SVG output benefit from the same fix because
  the change lives in the shared image-drawing code.

## Tests and demo

- `cairosvg/test_nested_svg.py` renders real PNG output and checks the
  actual pixels for: same-id gradient/clipPath isolation, duplicate
  embedding, per-document relative resource resolution,
  `meet`/`slice`/`none` aspect-ratio mapping, viewBox-only and
  no-intrinsic-size documents, transforms, image opacity, gzip
  (`svgz`) embedding, broken/missing resource sibling rendering, and
  the unchanged `safe_fetch` policy.
- `demo_nested_svg/` is a fully offline case: a main SVG embeds two
  icons whose gradients and clip paths share the same ids with
  different colors. `python -I demo_nested_svg/run_demo.py` renders
  `main.png` and verifies its colors, transparent areas and alignment.
