import cairosvg, io
from PIL import Image
from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface
from cairosvg.helpers import resolve_transform_origin

svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
       '<text id="t" x="50" y="100" font-size="30" fill="red" transform="rotate(180)" '
       'transform-origin="center" transform-box="fill-box">Hi</text></svg>')
tree = Tree(bytestring=svg.encode())
surf = PNGSurface(tree, None, 96)
text = tree.children[0]
origin = resolve_transform_origin(surf, text)
print("resolved origin:", origin)
print("node keys after resolve:", sorted(text.keys()))
buf = io.BytesIO(); surf.cairo.write_to_png(buf)
img = Image.open(io.BytesIO(buf.getvalue())).convert('RGBA')
n = sum(1 for y in range(200) for x in range(200) if img.load()[x,y][3])
print("pixels when resolved BEFORE draw completes:", n)

# Fresh render without pre-resolving
buf2 = io.BytesIO()
surf2 = PNGSurface(Tree(bytestring=svg.encode()), buf2, 96)
img2 = Image.open(io.BytesIO(buf2.getvalue())).convert('RGBA')
n2 = sum(1 for y in range(200) for x in range(200) if img2.load()[x,y][3])
print("pixels normal render:", n2)

# Same but with view-box instead
svg_vb = svg.replace(' transform-box="fill-box"', '')
buf3 = io.BytesIO()
PNGSurface(Tree(bytestring=svg_vb.encode()), buf3, 96)
img3 = Image.open(io.BytesIO(buf3.getvalue())).convert('RGBA')
n3 = sum(1 for y in range(200) for x in range(200) if img3.load()[x,y][3])
print("view-box pixels:", n3)
