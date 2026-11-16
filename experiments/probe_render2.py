"""Probes where the current origin reference frame must diverge from browsers."""
import io
import cairosvg
from PIL import Image

W = H = 200

def render_full(svg):
    return Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')

def red_stats(img, w=W, h=H):
    px = img.load()
    pts = [(x, y) for y in range(h) for x in range(w)
           if px[x, y][3] and px[x, y][0] > 150 and px[x, y][1] < 80 and px[x, y][2] < 80]
    if not pts:
        return "NO RED PIXELS (rotated/clipped off surface)"
    cx = sum(p[0] for p in pts)/len(pts); cy = sum(p[1] for p in pts)/len(pts)
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return f"center=({cx:.1f},{cy:.1f}) bbox=({min(xs)},{min(ys)})-({max(xs)},{max(ys)}) n={len(pts)}"

svgA = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 100 100">'
        '<rect x="35" y="45" width="30" height="10" fill="red" '
        'transform="rotate(90)" transform-origin="center"/></svg>')
print("A keyword center, viewBox x2, expect center(100,100):", red_stats(render_full(svgA)))

svgB = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 100 100">'
        '<rect x="35" y="45" width="30" height="10" fill="red" '
        'transform="rotate(90)" transform-origin="50% 50%"/></svg>')
print("B 50% pct, viewBox x2, expect center(100,100):", red_stats(render_full(svgB)))

svgC = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="-50 -50 100 100">'
        '<rect x="-15" y="-5" width="30" height="10" fill="red" '
        'transform="rotate(90)" transform-origin="center"/></svg>')
print("C keyword center, shifted viewBox, expect center(100,100):", red_stats(render_full(svgC)))

svgD = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 100 100">'
        '<g transform="translate(25,25)">'
        '<rect x="10" y="20" width="30" height="10" fill="red" '
        'transform="rotate(90)" transform-origin="center"/></g></svg>')
print("D nested center, expect device center(200,100):", red_stats(render_full(svgD)))

svgE = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<rect x="10" y="10" width="20" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="garbage"/>'
        '<rect x="50" y="50" width="20" height="20" fill="red"/></svg>')
imgE = render_full(svgE)
px = imgE.load()
pts = [(x, y) for y in range(H) for x in range(W)
       if px[x, y][3] and px[x, y][0] > 150 and px[x, y][1] < 80 and px[x, y][2] < 80]
print("E garbage origin: red pixel count =", len(pts), "(second rect alone = 21x21=441)")

svgF = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<defs><rect id="r" x="0" y="0" width="20" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="10 10"/></defs>'
        '<use href="#r" x="40" y="40"/><use href="#r" x="100" y="100"/></svg>')
imgF = render_full(svgF)
px = imgF.load()
pts = [(x, y) for y in range(H) for x in range(W)
       if px[x, y][3] and px[x, y][0] > 150 and px[x, y][1] < 80 and px[x, y][2] < 80]
clusters = {}
for x, y in pts:
    key = (x // 60, y // 60)
    clusters.setdefault(key, []).append((x, y))
for k, v in sorted(clusters.items()):
    cx = sum(p[0] for p in v)/len(v); cy = sum(p[1] for p in v)/len(v)
    print(f"F cluster {k}: center=({cx:.1f},{cy:.1f}) n={len(v)}")
