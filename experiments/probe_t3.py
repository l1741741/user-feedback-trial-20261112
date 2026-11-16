import io, cairosvg
from PIL import Image
def stats(svg, size=200):
    img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
    px = img.load()
    pts = [(x,y) for y in range(size) for x in range(size) if px[x,y][3] and px[x,y][0]>150 and px[x,y][1]<80]
    if not pts: return "NO PIXELS"
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
    return f"center=({sum(xs)/len(xs):.1f},{sum(ys)/len(ys):.1f}) bbox=({min(xs)},{min(ys)})-({max(xs)},{max(ys)})"

# "top": origin (100,0); rect center (100,10) rotated -> (90,0)
print("top keyword:", stats('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
      '<rect x="70" y="0" width="60" height="20" fill="red" transform="rotate(90)" transform-origin="top"/></svg>'))

# "top left": origin (0,0); rect (0,0)40x20 rotated -> (-20,0)-(0,40): off canvas
print("top left:", stats('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
      '<rect x="0" y="0" width="40" height="20" fill="red" transform="rotate(90)" transform-origin="top left"/></svg>'))

# Better swapped-keyword test: rect placed so rotation stays visible.
# origin (0,0); rect (40,40) 20x10 -> rotated: (-10,40)-(0,60) still partly off
# Use origin "bottom right" = (200,200), rect centered (170,180) 60x20 -> rotation about corner
print("bottom right:", stats('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
      '<rect x="140" y="170" width="60" height="20" fill="red" transform="rotate(90)" transform-origin="bottom right"/></svg>'))
# center (170,180) about (200,200): delta(-30,-20) -> (20, -30) -> (220,170)
