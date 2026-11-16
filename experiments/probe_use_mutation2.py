"""Render one Tree twice and inspect in-place mutation of its nodes."""
import io
import cairosvg
from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface

svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="100">'
       '<defs><rect id="r" x="0" y="0" width="20" height="20" fill="red"/></defs>'
       '<use href="#r" x="20" y="40"/>'
       '<use href="#r" x="120" y="40"/></svg>')
tree = Tree(bytestring=svg.encode())
uses_before = [(c.tag, dict(c)) for c in tree.children if c.tag == 'use']

buf = io.BytesIO()
s1 = PNGSurface(tree, None, 96)
png1 = io.BytesIO(); s1.cairo.write_to_png(png1)
uses_after_1 = [(c.tag, dict(c)) for c in tree.children if c.tag == 'use']
print("mutated after first render:", uses_before != uses_after_1)
for b, a in zip(uses_before, uses_after_1):
    print("  before:", b[1].get('x'), "after:", a[1].get('x'))

# Second render of the SAME tree: positions must be identical
s2 = PNGSurface(tree, None, 96)
png2 = io.BytesIO(); s2.cairo.write_to_png(png2)
print("same-tree re-render identical:", png1.getvalue() == png2.getvalue())

# Also check referenced tree object identity across uses (tree_cache sharing)
tree_cache = s1.tree_cache
print("tree_cache keys:", list(tree_cache))
