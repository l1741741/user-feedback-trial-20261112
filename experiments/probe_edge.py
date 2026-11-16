import io, cairosvg
from PIL import Image
def stats(svg, size=200):
    img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
    px = img.load()
    pts = [(x,y) for y in range(size) for x in range(size) if px[x,y][3] and px[x,y][0]>150 and px[x,y][1]<80]
    if not pts: return "NO PIXELS"
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
    return f"bbox=({min(xs)},{min(ys)})-({max(xs)},{max(ys)}) center=({sum(xs)/len(xs):.1f},{sum(ys)/len(ys):.1f})"

# comma-separated origin
print("comma:", stats('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
      '<rect x="70" y="90" width="60" height="20" fill="red" transform="rotate(90)" transform-origin="100, 100"/></svg>'))

# em units (default font-size 12pt = 16px at 96dpi); 5em = 80px
print("em:", stats('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
      '<rect x="50" y="70" width="60" height="20" fill="red" transform="rotate(90)" transform-origin="5em 5em"/></svg>'))

# fill-box with percentage 25% 25%: rect (40,60) 80x40 -> origin (60,70)
# rect center (80,80), delta (20,10) R90 -> (-10,20) -> (50,90)
print("fill-box pct:", stats('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
      '<rect x="40" y="60" width="80" height="40" fill="red" transform="rotate(90)" transform-origin="25% 25%" transform-box="fill-box"/></svg>'))

# nested svg viewport origin
print("nested svg:", stats('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
      '<svg x="50" y="50" width="100" height="100" viewBox="0 0 50 50">'
      '<rect x="15" y="20" width="20" height="10" fill="red" transform="rotate(90)" transform-origin="center"/></svg></svg>'))

# text element with fill-box
print("text fill-box:", stats('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
      '<text x="50" y="100" font-size="30" fill="red" transform="rotate(180)" transform-origin="center" transform-box="fill-box">Hi</text></svg>'))

# circle with fill-box
print("circle fill-box:", stats('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
      '<circle cx="100" cy="100" r="30" fill="red" transform="rotate(90)" transform-origin="center" transform-box="fill-box"/></svg>'))
