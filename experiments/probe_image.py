import io, cairosvg
from PIL import Image

# nested SVG image placed at x,y — needs a data URL
inner = ('<svg xmlns="http://www.w3.org/2000/svg" width="50" height="50">'
         '<rect width="50" height="50" fill="red"/></svg>')
import urllib.parse
data = 'data:image/svg+xml,' + urllib.parse.quote(inner)
svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" '
       f'xmlns:xlink="http://www.w3.org/1999/xlink">'
       f'<image href="{data}" x="60" y="70" width="50" height="50"/></svg>')
img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
px = img.load()
pts = [(x,y) for y in range(200) for x in range(200) if px[x,y][3] and px[x,y][0]>150 and px[x,y][1]<80]
xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
print("nested svg image bbox:", (min(xs),min(ys),max(xs),max(ys)), "expect (60,70)-(109,119)")

# twice-render stability
o1 = cairosvg.svg2png(svg.encode()); o2 = cairosvg.svg2png(svg.encode())
print("idempotent:", o1 == o2)

# raster png image via data url
import base64, io as _io
buf = _io.BytesIO()
Image.new('RGBA',(10,10),(255,0,0,255)).save(buf, 'PNG')
png_data = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
svg2 = (f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        f'<image href="{png_data}" x="30" y="40" width="20" height="20"/></svg>')
img2 = Image.open(io.BytesIO(cairosvg.svg2png(svg2.encode()))).convert('RGBA')
px = img2.load()
pts = [(x,y) for y in range(200) for x in range(200) if px[x,y][3] and px[x,y][0]>150 and px[x,y][1]<80]
xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
print("raster image bbox:", (min(xs),min(ys),max(xs),max(ys)), "expect (30,40)-(49,59)")
