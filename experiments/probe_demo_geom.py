import io, cairosvg
from PIL import Image
def center(body, vb=None):
    vba = f' viewBox="{vb}"' if vb else ''
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"{vba}>{body}</svg>')
    img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
    px = img.load()
    pts = [(x,y) for y in range(200) for x in range(200) if px[x,y][3] and px[x,y][0]>150 and px[x,y][1]<90]
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
    return (min(xs),min(ys),max(xs),max(ys)), (sum(xs)/len(xs), sum(ys)/len(ys))

# panel 3
print("p3:", center('<g transform="translate(40,50)">'
  '<rect x="20" y="30" width="40" height="20" fill="red" transform="rotate(90)" transform-origin="40 40"/></g>'))
# rect center local (40,40); rotated about itself stays (40,40); +parent (80,90)
# panel 4 fill-box
print("p4:", center('<rect x="30" y="110" width="80" height="30" fill="red" transform="rotate(90)" transform-origin="center" transform-box="fill-box"/>'))
# fill-box center (70,125)
# panel 5
print("p5:", center('<g transform="translate(20,20)">'
  '<rect x="30" y="50" width="60" height="20" fill="red" transform="rotate(90)" transform-origin="50% 50%"/></g>'))
# origin local viewport (100,100); rect center (60,60) rotate about (100,100):
# delta(-40,-40)->(40,-40)->(140,60); +parent (160,80)
# panel 6
print("p6:", center('<g transform="translate(110,110)">'
  '<rect x="20" y="0" width="30" height="14" fill="green" transform="rotate(90)"/></g>'))
# rect (20,0)30x14 -> rotate: (0,20)-(-14,50); +T -> (110,130)-(96,160)
# panel 8 use centers
print("p8:", center('<defs><g id="b"><rect x="0" y="0" width="40" height="16" fill="red" transform="rotate(90)" transform-origin="20 8"/></g></defs>'
  '<use href="#b" x="50" y="60"/><use href="#b" x="130" y="110"/>'))
