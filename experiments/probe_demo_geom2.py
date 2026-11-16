import io, cairosvg
from PIL import Image
def stats(body, vb=None):
    vba = f' viewBox="{vb}"' if vb else ''
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"{vba}>{body}</svg>')
    img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
    px = img.load()
    pts = [(x,y) for y in range(200) for x in range(200) if px[x,y][3] and px[x,y][0]<100 and px[x,y][1]>100]
    if not pts: return "no green"
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
    return f"bbox=({min(xs)},{min(ys)})-({max(xs)},{max(ys)}) center=({sum(xs)/len(xs):.1f},{sum(ys)/len(ys):.1f})"

print("p6:", stats('<g transform="translate(110,110)">'
  '<rect x="20" y="0" width="30" height="14" fill="#2e7d32" transform="rotate(90)"/></g>'))

# p8 red uses
svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
  '<defs><g id="b"><rect x="0" y="0" width="40" height="16" fill="#d64545" transform="rotate(90)" transform-origin="20 8"/></g></defs>'
  '<use href="#b" x="50" y="60"/><use href="#b" x="130" y="110"/></svg>')
img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
px = img.load()
pts = [(x,y) for y in range(200) for x in range(200) if px[x,y][3] and px[x,y][0]>150 and px[x,y][1]<90]
c1 = [p for p in pts if p[0] < 110]; c2 = [p for p in pts if p[0] >= 110]
for name, c in (("use#1", c1), ("use#2", c2)):
    xs=[p[0] for p in c]; ys=[p[1] for p in c]
    print(name, f"bbox=({min(xs)},{min(ys)})-({max(xs)},{max(ys)}) center=({sum(xs)/len(xs):.1f},{sum(ys)/len(ys):.1f})")
