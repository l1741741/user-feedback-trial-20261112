import io
import cairosvg
from PIL import Image
from cairosvg.helpers import resolve_transform_origin
from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface
import io as _io

# Build tree and inspect resolved origin without rendering
svgC = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="-50 -50 100 100">'
        '<rect id="r" x="-15" y="-5" width="30" height="10" fill="red" '
        'transform="rotate(90)" transform-origin="center"/></svg>')
tree = Tree(bytestring=svgC.encode())
buf = _io.BytesIO()
surf = PNGSurface(tree, None, 96)
rect = tree.children[0]
print("surface context size:", surf.context_width, surf.context_height)
print("resolved origin (local user space):", resolve_transform_origin(surf, rect))
# Expect (0,0): center of viewport spanning (-50,-50)-(50,50)

# Where do pixels actually land? Scan whole image for any non-white
png = _io.BytesIO()
surf.cairo.write_to_png(png)
img = Image.open(_io.BytesIO(png.getvalue())).convert('RGBA')
px = img.load()
pts = [(x,y) for y in range(200) for x in range(200) if px[x,y][3] and px[x,y][:3] != (0,0,0)]
red = [(x,y) for x,y in pts if px[x,y][0]>150 and px[x,y][1]<80]
print("red n:", len(red), red[:5] if red else "")
