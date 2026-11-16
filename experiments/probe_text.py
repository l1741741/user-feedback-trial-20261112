import cairosvg
from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface
from cairosvg.helpers import resolve_transform_origin
from cairosvg.bounding_box import calculate_bounding_box

# text without transform-box — does it render at all?
svg1 = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<text x="50" y="100" font-size="30" fill="red">Hi</text></svg>')
open('/tmp/t1.png','wb').write(cairosvg.svg2png(svg1.encode()))

# text with rotate but no origin
svg2 = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<text x="50" y="100" font-size="30" fill="red" transform="rotate(180)">Hi</text></svg>')
open('/tmp/t2.png','wb').write(cairosvg.svg2png(svg2.encode()))

# check bounding box resolution
svg3 = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<text id="t" x="50" y="100" font-size="30" fill="red" transform="rotate(180)" '
        'transform-origin="center" transform-box="fill-box">Hi</text></svg>')
tree = Tree(bytestring=svg3.encode())
surf = PNGSurface(tree, None, 96)
text = tree.children[0]
bb = calculate_bounding_box(surf, text)
print("text bounding box:", bb)
print("resolved origin:", resolve_transform_origin(surf, text))
