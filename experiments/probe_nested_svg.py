import io, cairosvg
from PIL import Image
def stats(svg, w=200, h=200):
    img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
    px = img.load()
    pts = [(x,y) for y in range(h) for x in range(w) if px[x,y][3] and px[x,y][0]>150 and px[x,y][1]<80]
    if not pts: return "NO PIXELS"
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
    return f"bbox=({min(xs)},{min(ys)})-({max(xs)},{max(ys)}) center=({sum(xs)/len(xs):.1f},{sum(ys)/len(ys):.1f})"

# non-uniform scale: viewBox 0 0 100 50 in 200x200 -> scale x2, y4 (meet -> uniform actually min=2)
# use preserveAspectRatio=none to force non-uniform
svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 100 50" preserveAspectRatio="none">'
       '<rect x="35" y="20" width="30" height="10" fill="red" transform="rotate(90)" transform-origin="center"/></svg>')
print("non-uniform viewBox, center keyword:", stats(svg))
# local center (50,25) -> device (100,100)

# nested svg with its own viewBox
svg2 = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<svg x="50" y="50" width="100" height="100" viewBox="0 0 50 50">'
        '<rect x="15" y="20" width="20" height="10" fill="red" transform="rotate(90)" transform-origin="center"/></svg></svg>')
print("nested svg center keyword:", stats(svg2))
# inner viewport local center (25,25) -> in inner device space (50,50) -> outer (100,100)
