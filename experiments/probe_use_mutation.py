"""Check whether rendering one <use> mutates shared/cached node state."""
import io
import cairosvg
from cairosvg.parser import Tree

# The use() implementation deletes x/y/viewBox/mask from the node dict.
# With tree_cache the referenced Tree is shared; check attributes survive.
svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="100">'
       '<defs><rect id="r" x="0" y="0" width="20" height="20" fill="red"/></defs>'
       '<use href="#r" x="20" y="40"/>'
       '<use href="#r" x="120" y="40"/>'
       '<use href="#r" x="220" y="40"/></svg>')
tree = Tree(bytestring=svg.encode())
use_nodes = [c for c in tree.children if c.tag == 'use']
print("before render, use x attrs:", [u.get('x') for u in use_nodes])
cairosvg.svg2png(svg.encode())
# Re-parse to check a fresh tree (the old one is consumed by the Surface)
tree2 = Tree(bytestring=svg.encode())
use_nodes2 = [c for c in tree2.children if c.tag == 'use']
print("fresh tree x attrs:", [u.get('x') for u in use_nodes2])

# Mask attr deletion check: mask on <use>
svg2 = ('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<defs><mask id="m"><rect width="100" height="100" fill="white"/></mask>'
        '<rect id="r2" width="20" height="20" fill="red"/></defs>'
        '<use href="#r2" x="10" y="10" mask="url(#m)"/>'
        '<use href="#r2" x="40" y="40" mask="url(#m)"/></svg>')
t = Tree(bytestring=svg2.encode())
uses = [c for c in t.children if c.tag == 'use']
print("mask attrs before:", [u.get('mask') for u in uses])
out1 = cairosvg.svg2png(svg2.encode())
out2 = cairosvg.svg2png(svg2.encode())
print("repeat renders identical:", out1 == out2)

# Same tree object rendered twice (bypassing parse): bounding_box caching
from cairosvg.surface import PNGSurface
buf = io.BytesIO()
s1 = PNGSurface(t, None, 96)
png1 = io.BytesIO(); s1.cairo.write_to_png(png1)
t2 = Tree(bytestring=svg2.encode())
s2 = PNGSurface(t2, None, 96)
png2 = io.BytesIO(); s2.cairo.write_to_png(png2)
print("two surfaces identical:", png1.getvalue() == png2.getvalue())
