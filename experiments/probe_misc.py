import io, cairosvg
from PIL import Image

def render(svg):
    return Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')

# gradient with gradientTransform still renders
svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
       '<defs><linearGradient id="g" gradientTransform="rotate(45)">'
       '<stop offset="0" stop-color="red"/><stop offset="1" stop-color="blue"/>'
       '</linearGradient></defs>'
       '<rect width="100" height="100" fill="url(#g)"/></svg>')
img = render(svg)
px = img.load()
print("gradient renders:", px[10,10] != px[90,90], px[10,10], px[90,90])

# pattern with patternTransform
svg2 = ('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<defs><pattern id="p" width="10" height="10" patternTransform="rotate(45)">'
        '<rect width="10" height="10" fill="white"/><rect width="5" height="5" fill="red"/>'
        '</pattern></defs><rect width="100" height="100" fill="url(#p)"/></svg>')
img2 = render(svg2)
reds = sum(1 for y in range(100) for x in range(100) if img2.load()[x,y][0]>150 and img2.load()[x,y][1]<80)
print("pattern red pixels:", reds)

# skewX with origin
svg3 = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<rect x="60" y="80" width="40" height="20" fill="red" '
        'transform="skewX(45)" transform-origin="80 90"/></svg>')
img3 = render(svg3)
pts = [(x,y) for y in range(200) for x in range(200) if img3.load()[x,y][3] and img3.load()[x,y][0]>150 and img3.load()[x,y][1]<80]
xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
print("skewX bbox:", (min(xs),min(ys),max(xs),max(ys)))

# matrix() function still works
svg4 = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<rect x="0" y="0" width="20" height="20" fill="red" '
        'transform="matrix(1 0 0 1 50 60)"/></svg>')
img4 = render(svg4)
pts = [(x,y) for y in range(200) for x in range(200) if img4.load()[x,y][3] and img4.load()[x,y][0]>150 and img4.load()[x,y][1]<80]
xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
print("matrix translate bbox:", (min(xs),min(ys),max(xs),max(ys)), "expect (50,60)-(69,79)")

# singular matrix scale(0) must not crash and must not hide next node
svg5 = ('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<rect width="20" height="20" fill="red" transform="scale(0)"/>'
        '<rect x="50" y="50" width="10" height="10" fill="red"/></svg>')
img5 = render(svg5)
pts = [(x,y) for y in range(100) for x in range(100) if img5.load()[x,y][3] and img5.load()[x,y][1]<80]
print("singular matrix, remaining rect bbox:", (min(x for x,_ in pts),min(y for _,y in pts),max(x for x,_ in pts),max(y for _,y in pts)))

# rotate with center args: rotate(90 50 50)
svg6 = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<rect x="30" y="40" width="40" height="20" fill="red" transform="rotate(90 50 50)"/></svg>')
img6 = render(svg6)
pts = [(x,y) for y in range(200) for x in range(200) if img6.load()[x,y][3] and img6.load()[x,y][0]>150 and img6.load()[x,y][1]<80]
xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
# rect center (50,50) stays
print("rotate(90 50 50) bbox:", (min(xs),min(ys),max(xs),max(ys)), "center ~", (sum(xs)/len(xs), sum(ys)/len(ys)))
