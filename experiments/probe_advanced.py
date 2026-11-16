import io, cairosvg
from PIL import Image

def render(svg):
    return Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')

# tspan with fill-box
svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
       '<text x="30" y="100" font-size="24" fill="red" transform="rotate(180)" '
       'transform-origin="center" transform-box="fill-box">Hello<tspan fill="blue"> world</tspan></text></svg>')
img = render(svg)
n = sum(1 for y in range(200) for x in range(200) if img.load()[x,y][3])
print("tspan fill-box opaque pixels:", n)

# marker on a path
svg2 = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<defs><marker id="m" markerWidth="10" markerHeight="10" refX="5" refY="5">'
        '<circle cx="5" cy="5" r="4" fill="blue"/></marker></defs>'
        '<line x1="20" y1="100" x2="180" y2="100" stroke="red" stroke-width="2" '
        'marker-start="url(#m)" marker-end="url(#m)"/></svg>')
img2 = render(svg2)
opaque = [(x,y) for y in range(200) for x in range(200) if img2.load()[x,y][3]]
print("marker opaque pixels:", len(opaque))

# clipPath still works
svg3 = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<defs><clipPath id="c"><rect x="50" y="50" width="50" height="50"/></clipPath></defs>'
        '<rect width="200" height="200" fill="red" clip-path="url(#c)"/></svg>')
img3 = render(svg3)
pts = [(x,y) for y in range(200) for x in range(200) if img3.load()[x,y][3] and img3.load()[x,y][0]>150]
xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
print("clip bbox:", (min(xs),min(ys),max(xs),max(ys)), "expect (50,50)-(99,99)")

# mask
svg4 = ('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<defs><mask id="m"><rect width="50" height="50" fill="white"/></mask></defs>'
        '<rect width="100" height="100" fill="red" mask="url(#m)"/></svg>')
img4 = render(svg4)
pts = [(x,y) for y in range(100) for x in range(100) if img4.load()[x,y][3] and img4.load()[x,y][0]>150]
print("mask red count:", len(pts), "expect ~2500")
