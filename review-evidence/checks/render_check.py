"""Independent main-agent render probes. Run against a frozen source via PYTHONPATH.
Analytic pixel expectations/equivalence tests; never edit the evaluated renderer.
"""
import base64,io,json,sys,traceback,html
from pathlib import Path
import cairosvg
from PIL import Image,ImageChops
SLUG=sys.argv[1];OUT=Path(sys.argv[2]);OUT.mkdir(parents=True,exist_ok=True)
CASES=[];ART=[]
def svg(body,w=200,h=120,attrs=''):
 return f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{w}" height="{h}" {attrs}>{body}</svg>'
def render(name,text,**kw):
 p=OUT/(name+'.svg');p.write_text(text);png=cairosvg.svg2png(bytestring=text.encode(),**kw);(OUT/(name+'.png')).write_bytes(png);im=Image.open(io.BytesIO(png)).convert('RGBA');ART.append(name);return im

def px(im,x,y,expected,tol=3):
 value=im.getpixel((x,y));assert max(abs(a-b) for a,b in zip(value,expected))<=tol,(x,y,value,expected)
def same(a,b,tol=0):
 assert a.size==b.size,(a.size,b.size)
 diff=ImageChops.difference(a,b);mx=max(v[1] for v in diff.getextrema());assert mx<=tol,mx

def case(name,fn):
 try:fn();CASES.append({'name':name,'passed':True})
 except Exception as e:CASES.append({'name':name,'passed':False,'error':str(e),'traceback':traceback.format_exc()})
def bounds(im):return im.getchannel('A').getbbox()
red=(255,0,0,255);blue=(0,0,255,255);clear=(0,0,0,0)
def generic():
 a=render('ordinary',svg('<rect x="10" y="10" width="50" height="40" fill="red"/><circle cx="120" cy="50" r="20" fill="blue"/>'));px(a,20,20,red);px(a,120,50,blue);px(a,190,100,clear)
case('ordinary-render-preserved',generic)
if SLUG=='marker-direction':
 def marker():
  defs='<defs><marker id="m" viewBox="0 -5 10 10" refX="0" refY="0" markerWidth="10" markerHeight="10" markerUnits="userSpaceOnUse" orient="auto-start-reverse"><path d="M0,-5 L10,0 L0,5 Z" fill="red"/></marker></defs>'
  a=render('start-reverse',svg(defs+'<path d="M40,60 L140,60" stroke="black" marker-start="url(#m)" marker-end="url(#m)"/>'));px(a,34,60,red);px(a,146,60,red);px(a,47,56,clear)
 case('auto-start-reverse-pixel-direction',marker)
 def mid():
  a=render('zero-mid',svg('<defs><marker id="m" markerWidth="6" markerHeight="6" refX="3" refY="3" markerUnits="userSpaceOnUse"><circle cx="3" cy="3" r="3" fill="red"/></marker></defs><path d="M20 60 L70 60 L70 60 L120 60" stroke="blue" marker-mid="url(#m)"/>'));px(a,70,60,red)
 case('zero-length-mid-marker',mid)
elif SLUG=='nested-clipping':
 def clips():
  a=render('bbox-clip',svg('<defs><clipPath id="c" clipPathUnits="objectBoundingBox"><rect x=".25" y="0" width=".5" height="1"/></clipPath></defs><g transform="translate(20 10) scale(2 1)"><rect x="10" y="20" width="60" height="60" fill="red" clip-path="url(#c)"/></g><rect x="170" y="20" width="20" height="60" fill="blue"/>'));px(a,80,50,red);px(a,45,50,clear);px(a,145,50,clear);px(a,180,50,blue)
 case('transformed-bbox-and-sibling',clips)
 def evenodd():
  a=render('evenodd',svg('<defs><clipPath id="c"><path clip-rule="evenodd" d="M10 10H110V110H10Z M40 40H80V80H40Z"/></clipPath></defs><rect width="120" height="120" fill="red" clip-path="url(#c)"/>'));px(a,20,20,red);px(a,60,60,clear)
 case('clip-evenodd-hole',evenodd)
elif SLUG=='mask-luminance':
 def mask(kind):
  a=render('mask-'+kind,svg('<defs><mask id="m" maskUnits="userSpaceOnUse" x="0" y="0" width="200" height="120" mask-type="'+kind+'"><rect width="60" height="120" fill="black"/><rect x="60" width="60" height="120" fill="white"/><rect x="120" width="80" height="120" fill="#808080"/></mask></defs><rect width="200" height="120" fill="red" mask="url(#m)"/>'))
  if kind=='luminance':px(a,20,60,clear);px(a,80,60,red);assert 120<=a.getpixel((160,60))[3]<=140
  else:px(a,20,60,red);px(a,160,60,red)
 case('black-white-grey-luminance',lambda:mask('luminance'));case('alpha-mask-colour-independent',lambda:mask('alpha'))
elif SLUG=='gradient-inheritance':
 defs='<defs><linearGradient id="base"><stop stop-color="red"/><stop offset="1" stop-color="blue"/></linearGradient><linearGradient id="a" xlink:href="#base" x2="0" y2="1"/><linearGradient id="b" xlink:href="#base"/></defs>'
 def inherit():
  a=render('gradient-refs',svg(defs+'<rect width="90" height="100" fill="url(#a)"/><rect x="100" width="90" height="100" fill="url(#b)"/>'));assert a.getpixel((20,5))[0]>220;assert a.getpixel((20,95))[2]>220;assert a.getpixel((105,50))[0]>220;assert a.getpixel((185,50))[2]>220
 case('href-attribute-overrides-isolated',inherit)
 def order():
  r1='<rect width="90" height="100" fill="url(#a)"/>';r2='<rect x="100" width="90" height="100" fill="url(#b)"/>';same(render('order-a',svg(defs+r1+r2)),render('order-b',svg(defs+r2+r1)))
 case('reference-order-independent',order)
elif SLUG=='radial-focal':
 def focal():
  a=render('radial-focal',svg('<defs><radialGradient id="g" cx=".5" cy=".5" r=".5" fx=".25" fy=".5"><stop stop-color="white"/><stop offset="1" stop-color="black"/></radialGradient></defs><rect x="10" y="10" width="180" height="100" fill="url(#g)"/>'));assert a.getpixel((55,60))[0]>240;assert a.getpixel((187,60))[0]<20
 case('elliptic-offcentre-light',focal)
 def radiuszero():
  a=render('zero-radius',svg('<defs><radialGradient id="g" r="0"><stop stop-color="white"/><stop offset="1" stop-color="blue"/></radialGradient></defs><rect width="100" height="100" fill="url(#g)"/><rect x="120" width="50" height="50" fill="red"/>'));px(a,50,50,blue);px(a,140,20,red)
 case('zero-radius-last-stop-and-sibling',radiuszero)
elif SLUG=='pattern-tile':
 defs='<defs><pattern id="p" width="20" height="20" patternUnits="userSpaceOnUse"><rect width="10" height="20" fill="red"/></pattern></defs>'
 def tiles():
  a=render('tiles',svg(defs+'<rect x="0" y="0" width="80" height="40" fill="url(#p)"/><rect x="0" y="60" width="180" height="40" fill="url(#p)"/>'))
  for y in (20,80):px(a,5,y,red);px(a,15,y,clear);px(a,25,y,red)
 case('user-space-period-across-host-sizes',tiles)
 def alpha():
  a=render('pattern-opacity',svg(defs+'<rect width="80" height="40" fill="url(#p)" fill-opacity=".5"/><rect y="60" width="80" height="40" fill="url(#p)"/>'));assert 120<=a.getpixel((5,20))[3]<=135;px(a,5,80,red)
 case('pattern-opacity-not-accumulated',alpha)
elif SLUG=='group-compositing':
 def group():
  a=render('group-alpha',svg('<g opacity=".5"><rect x="10" y="10" width="80" height="80" fill="red"/><rect x="50" y="10" width="80" height="80" fill="red"/></g>'));assert 125<=a.getpixel((20,50))[3]<=130;assert 125<=a.getpixel((70,50))[3]<=130
 case('overlap-alpha-applied-once',group)
 def nested():
  a=render('nested-alpha',svg('<g opacity=".5"><g opacity=".5"><rect width="100" height="100" fill="red"/></g></g><rect x="130" width="60" height="60" fill="blue"/>'));assert 61<=a.getpixel((40,40))[3]<=66;px(a,150,40,blue)
 case('nested-alpha-and-sibling-isolation',nested)
elif SLUG=='paint-order':
 def paint():
  a=render('paint-order',svg('<rect x="20" y="20" width="50" height="60" fill="red" stroke="blue" stroke-width="20"/><rect x="120" y="20" width="50" height="60" fill="red" stroke="blue" stroke-width="20" paint-order="stroke fill"/>'));px(a,25,50,blue);px(a,125,50,red)
 case('stroke-first-retains-fill-path',paint)
 def inherited():
  a=render('inherited-paint',svg('<g paint-order="stroke"><rect x="20" y="20" width="50" height="60" fill="red" stroke="blue" stroke-width="20"/></g><rect x="120" y="20" width="50" height="60" fill="red" stroke="blue" stroke-width="20"/>'));px(a,25,50,red);px(a,125,50,blue)
 case('paint-order-inherited-not-leaked',inherited)
elif SLUG=='vector-effect':
 def vector():
  a=render('non-scaling',svg('<g transform="scale(3 2)"><path d="M10 20H50" stroke="red" stroke-width="4" vector-effect="non-scaling-stroke"/></g>'));px(a,90,40,red);px(a,90,43,clear)
 case('nonuniform-transform-keeps-stroke-thickness',vector)
 def regular():
  a=render('regular-stroke',svg('<g transform="scale(3 2)"><path d="M10 20H50" stroke="blue" stroke-width="4"/></g>'));px(a,90,43,blue)
 case('regular-stroke-still-scales',regular)
elif SLUG=='dash-stroke':
 def odd():
  a=render('odd-dash',svg('<path d="M10 30H190" stroke="red" stroke-width="6" stroke-dasharray="10 5 5"/>'));b=render('even-equivalent',svg('<path d="M10 30H190" stroke="red" stroke-width="6" stroke-dasharray="10 5 5 10 5 5"/>'));same(a,b)
 case('odd-array-doubled-equivalence',odd)
 def negative():
  a=render('negative-offset',svg('<path d="M10 30H190" stroke="blue" stroke-width="6" stroke-dasharray="10 10" stroke-dashoffset="-5"/><path d="M10 80H190" stroke="red" stroke-width="6"/>'));px(a,12,30,clear);px(a,17,30,blue);px(a,12,80,red);px(a,27,80,red)
 case('negative-offset-and-solid-sibling',negative)
elif SLUG=='stroke-joins':
 def join():
  a=render('bevel',svg('<path d="M30 100L80 20L130 100" fill="none" stroke="red" stroke-width="20" stroke-linejoin="bevel"/>'));b=render('round',svg('<path d="M30 100L80 20L130 100" fill="none" stroke="red" stroke-width="20" stroke-linejoin="round"/>'));assert a.tobytes()!=b.tobytes();assert bounds(b)[1]<bounds(a)[1]
 case('round-and-bevel-distinct-extents',join)
 def cap():
  a=render('round-cap',svg('<path d="M40 60H140" stroke="blue" stroke-width="20" stroke-linecap="round"/>'));px(a,33,60,blue);px(a,147,60,blue)
 case('round-caps-extend-endpoints',cap)
elif SLUG=='viewport-percent':
 def percent():
  a=render('nested-percent',svg('<svg x="20" y="20" width="100" height="60" viewBox="0 0 200 120"><rect width="50%" height="100%" fill="red"/></svg>'));px(a,30,40,red);px(a,80,40,clear);px(a,150,40,clear)
 case('nested-viewbox-percent',percent)
 def sibling():
  a=render('nested-sibling',svg('<svg width="40" height="40" viewBox="0 0 10 10"><rect width="50%" height="100%" fill="blue"/></svg><rect x="100" width="50%" height="100%" fill="red"/>'));px(a,190,60,red)
 case('viewport-size-restored-for-sibling',sibling)
elif SLUG=='viewbox-slice':
 def align():
  a=render('meet',svg('<rect width="100" height="50" fill="red"/>',100,100,'viewBox="0 0 100 50" preserveAspectRatio="xMidYMid meet"'));px(a,50,10,clear);px(a,50,50,red);px(a,50,90,clear)
  b=render('slice',svg('<rect width="100" height="50" fill="blue"/>',100,100,'viewBox="0 0 100 50" preserveAspectRatio="xMidYMid slice"'));px(b,5,5,blue);px(b,95,95,blue)
 case('meet-letterbox-slice-fills',align)
 def origin():
  a=render('viewbox-origin',svg('<rect x="10" y="20" width="100" height="50" fill="red"/>',100,50,'viewBox="10 20 100 50"'));px(a,0,0,red);px(a,99,49,red)
 case('nonzero-viewbox-origin',origin)
elif SLUG=='transform-origin':
 def transform():
  a=render('origin-rotation',svg('<rect x="30" y="20" width="20" height="40" fill="red" transform="rotate(90)" transform-origin="40 40"/>'));px(a,25,40,red);px(a,40,65,clear)
 case('explicit-transform-origin',transform)
 def order():
  a=render('transform-composition',svg('<g transform="translate(50 20) scale(2)"><rect x="10" y="10" width="10" height="10" fill="blue"/></g>'));px(a,75,45,blue);px(a,125,65,clear)
 case('nested-transform-order',order)
elif SLUG=='currentcolor-inherit':
 def use():
  a=render('theme-use',svg('<defs><symbol id="s"><rect width="50" height="50" fill="currentColor"/></symbol></defs><use xlink:href="#s" color="red"/><use xlink:href="#s" x="100" color="blue"/>'));px(a,20,20,red);px(a,120,20,blue)
 case('same-symbol-two-currentcolor-themes',use)
 def stop():
  a=render('stop-color',svg('<defs><linearGradient id="g" color="red"><stop stop-color="currentColor"/><stop offset="1" stop-color="currentColor"/></linearGradient></defs><rect width="100" height="100" fill="url(#g)"/>'));px(a,50,50,red)
 case('currentcolor-gradient-stops',stop)
elif SLUG=='css-visibility':
 def visible():
  a=render('visible-descendant',svg('<g visibility="hidden"><rect width="60" height="60" fill="red"/><rect x="100" width="60" height="60" fill="blue" visibility="visible"/></g>'));px(a,20,20,clear);px(a,120,20,blue)
 case('visibility-visible-descendant',visible)
 def display():
  a=render('display-none',svg('<g style="display:none"><rect width="60" height="60" fill="red" visibility="visible"/></g><rect x="100" width="60" height="60" fill="blue"/>'));px(a,20,20,clear);px(a,120,20,blue)
 case('display-none-subtree-and-sibling',display)
elif SLUG.startswith('text-'):
 # Use the same local font for paired renders; compare layout, not glyph rasterizer identity.
 def text_im(name,body):return render(name,svg('<g font-family="DejaVu Sans" font-size="20" fill="red">'+body+'</g>'))
 if SLUG=='text-chunk-anchor':
  def chunks():
   a=text_im('chunks','<text y="60" text-anchor="middle"><tspan x="50">AB</tspan><tspan x="150">CD</tspan></text>');b=text_im('separate','<text x="50" y="60" text-anchor="middle">AB</text><text x="150" y="60" text-anchor="middle">CD</text>');same(a,b,1)
  case('new-x-creates-independent-chunk',chunks)
 elif SLUG=='text-spacing-length':
  def length():
   a=text_im('textlength','<text x="20" y="60" textLength="140" lengthAdjust="spacingAndGlyphs">MMMM</text>');b=bounds(a);assert 132<=b[2]-b[0]<=142,b
  case('textlength-adjusts-visible-width',length)
  def length_sibling():
   a=text_im('length-sibling','<text x="20" y="30" textLength="140" lengthAdjust="spacingAndGlyphs">MMMM</text><text x="20" y="90">MM</text>');b=text_im('plain-sibling','<text x="20" y="90">MM</text>');same(a.crop((0,60,200,120)),b.crop((0,60,200,120)))
  case('length-does-not-scale-following-text',length_sibling)
 elif SLUG=='text-baseline':
  def shift():
   a=text_im('shift','<text x="20" y="60" baseline-shift="10">MMMM</text>');b=text_im('shift-explicit','<text x="20" y="50">MMMM</text>');same(a,b,1)
  case('length-baseline-shift',shift)
  def nested():
   a=text_im('super','<text x="20" y="60">M<tspan baseline-shift="super">M</tspan>M</text>');b=text_im('normal','<text x="20" y="60">MMM</text>');assert bounds(a)[1]<bounds(b)[1],(bounds(a),bounds(b))
  case('superscript-raises-glyph',nested)
 elif SLUG=='text-path-layout':
  def pathtext():
   d='<defs><path id="p" d="M20 60H180"/></defs>'
   a=text_im('path-offset',d+'<text><textPath xlink:href="#p" startOffset="50%">MM</textPath></text>');b=text_im('path-plain','<text x="100" y="60">MM</text>');assert max(abs(x-y) for x,y in zip(bounds(a),bounds(b)))<=1,(bounds(a),bounds(b));assert abs(sum(a.getchannel('A').getdata())-sum(b.getchannel('A').getdata()))<=sum(b.getchannel('A').getdata())*.04
  case('textpath-percent-offset-horizontal',pathtext)
  def zero():
   a=text_im('zero-textpath','<defs><path id="p" d="M10 10"/></defs><text><textPath xlink:href="#p">MM</textPath></text><text x="100" y="60">MM</text>');b=text_im('zero-reference','<text x="100" y="60">MM</text>');same(a,b,1)
  case('zero-path-does-not-pollute-sibling',zero)
 def repeat_text():
  t=svg('<text font-family="DejaVu Sans" font-size="20" x="10" y="60">Text</text>');same(render('text-repeat-a',t),render('text-repeat-b',t))
 case('text-repeat-stable',repeat_text)
elif SLUG in ('image-aspect','nested-svg-image','resource-base'):
 def image_uri(im):
  b=io.BytesIO();im.save(b,format='PNG');return 'data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()
 if SLUG=='image-aspect':
  def image():
   im=Image.new('RGBA',(40,20),red);u=image_uri(im)
   a=render('image-slice',svg(f'<image x="20" y="20" width="80" height="80" preserveAspectRatio="xMidYMid slice" xlink:href="{u}"/>'));px(a,30,30,red);px(a,90,90,red);px(a,15,50,clear);px(a,105,50,clear)
   b=render('image-meet',svg(f'<image x="20" y="20" width="80" height="80" preserveAspectRatio="xMidYMid meet" xlink:href="{u}"/>'));px(b,30,30,clear);px(b,30,50,red)
  case('image-slice-clips-and-meet-letterboxes',image)
 elif SLUG=='nested-svg-image':
  def nested():
   def u(c):return 'data:image/svg+xml;base64,'+base64.b64encode(svg('<defs><linearGradient id="g"><stop stop-color="'+c+'"/><stop offset="1" stop-color="'+c+'"/></linearGradient></defs><rect width="40" height="40" fill="url(#g)"/>',40,40).encode()).decode()
   a=render('nested-images',svg('<image x="10" y="10" width="60" height="60" xlink:href="'+u('red')+'"/><image x="100" y="10" width="60" height="60" xlink:href="'+u('blue')+'"/>'));px(a,40,40,red);px(a,130,40,blue);px(a,80,40,clear)
  case('nested-documents-same-id-isolated',nested)
 elif SLUG=='resource-base':
  def local():
   for name,c in [('目录 A',red),('目录 B',blue)]:
    d=OUT/name;d.mkdir(exist_ok=True);Image.new('RGBA',(20,20),c).save(d/'颜色 图.png');text=svg('<image x="10" y="10" width="60" height="60" xlink:href="颜色 图.png"/>');p=d/'图.svg';p.write_text(text)
    im=Image.open(io.BytesIO(cairosvg.svg2png(url=str(p.resolve())))).convert('RGBA');im.save(d/'path.png');px(im,40,40,c)
    with p.open('rb') as f:im2=Image.open(io.BytesIO(cairosvg.svg2png(file_obj=f))).convert('RGBA')
    im2.save(d/'file.png');px(im2,40,40,c);same(im,im2)
  case('relative-unicode-resource-path-and-file-object',local)
elif SLUG=='path-arcs':
 def arc():
  a=render('zero-arc',svg('<path d="M20 60A0 30 0 0 1 160 60" fill="none" stroke="red" stroke-width="6"/>'));b=render('zero-line',svg('<path d="M20 60L160 60" fill="none" stroke="red" stroke-width="6"/>'));same(a,b)
 case('zero-radius-arc-is-line',arc)
 def neg():
  a=render('negative-radius',svg('<path d="M20 60A-50 -30 30 0 1 140 60" fill="none" stroke="blue" stroke-width="4"/>'));b=render('positive-radius',svg('<path d="M20 60A50 30 30 0 1 140 60" fill="none" stroke="blue" stroke-width="4"/>'));same(a,b,1)
 case('negative-radii-use-absolute-values',neg)
elif SLUG=='path-smooth':
 def smooth():
  a=render('smooth-reset',svg('<path d="M10 60C20 20 40 20 50 60L70 60S110 20 140 60" fill="none" stroke="red" stroke-width="4"/>'));b=render('explicit-reset',svg('<path d="M10 60C20 20 40 20 50 60L70 60C70 60 110 20 140 60" fill="none" stroke="red" stroke-width="4"/>'));same(a,b,1)
 case('line-resets-smooth-cubic-control',smooth)
 def quad():
  a=render('smooth-quad',svg('<path d="M10 60Q40 20 70 60T130 60" fill="none" stroke="blue" stroke-width="4"/>'));b=render('explicit-quad',svg('<path d="M10 60Q40 20 70 60Q100 100 130 60" fill="none" stroke="blue" stroke-width="4"/>'));same(a,b,1)
 case('quadratic-reflection-equivalence',quad)
elif SLUG=='fill-rule':
 def fill():
  a=render('evenodd-fill',svg('<path d="M10 10H110V110H10Z M40 40H80V80H40Z" fill="red" fill-rule="evenodd"/>'));px(a,20,20,red);px(a,60,60,clear)
  b=render('nonzero-fill',svg('<path d="M10 10H110V110H10Z M40 40H80V80H40Z" fill="red"/>'));px(b,60,60,red)
 case('compound-path-winding-rules',fill)
elif SLUG=='object-bounds':
 def bbox():
  a=render('curve-bbox',svg('<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop stop-color="red"/><stop offset="1" stop-color="blue"/></linearGradient></defs><path d="M20 100Q100 -60 180 100Z" fill="url(#g)"/>'));assert a.getpixel((100,30))[0]>200,a.getpixel((100,30));assert a.getpixel((100,95))[2]>200,a.getpixel((100,95))
 case('quadratic-extremum-controls-bbox-gradient',bbox)
elif SLUG=='filter-offset':
 def offset():
  a=render('offset',svg('<defs><filter id="f" x="-100%" y="-100%" width="300%" height="300%"><feOffset dx="20" dy="10"/></filter></defs><rect x="20" y="20" width="40" height="40" fill="red" filter="url(#f)"/><rect x="120" y="20" width="40" height="40" fill="blue"/>'));px(a,45,35,red);px(a,25,25,clear);px(a,125,25,blue)
 case('offset-position-and-unfiltered-sibling',offset)
 def unit():
  a=render('offset-bbox',svg('<defs><filter id="f" primitiveUnits="objectBoundingBox" x="-100%" y="-100%" width="300%" height="300%"><feOffset dx=".5" dy=".5"/></filter></defs><rect x="20" y="20" width="40" height="20" fill="red" filter="url(#f)"/>'));px(a,50,35,red);px(a,25,25,clear)
 case('offset-object-bbox-units',unit)
elif SLUG=='filter-blend':
 def blend():
  a=render('transparent-blend',svg('<defs><filter id="f"><feBlend in="SourceGraphic" in2="BackgroundImage" mode="multiply"/></filter></defs><rect x="20" y="20" width="100" height="60" fill="red" fill-opacity=".5" filter="url(#f)"/>'));p=a.getpixel((50,50));assert p[0]>=250 and p[1]<3 and p[2]<3 and 125<=p[3]<=130,p
 case('transparent-background-multiply-keeps-visible-colour',blend)
 def screen():
  a=render('screen',svg('<defs><filter id="f"><feBlend mode="screen" in="SourceGraphic" in2="BackgroundImage"/></filter></defs><rect x="0" y="0" width="120" height="100" fill="blue"/><rect x="20" y="20" width="100" height="60" fill="red" filter="url(#f)"/>'));px(a,50,50,(255,0,255,255))
 case('screen-red-over-blue-is-magenta',screen)
elif SLUG=='render-state-isolation':
 def state():
  d='<defs><clipPath id="c"><rect width="20" height="20"/></clipPath></defs><g opacity=".4" transform="translate(10 10)" clip-path="url(#c)"><rect width="50" height="50" fill="red"/></g>'
  a=render('special-before-sibling',svg(d+'<rect x="120" y="20" width="60" height="60" fill="blue"/>'));px(a,150,50,blue);px(a,100,50,clear)
 case('clip-opacity-transform-restored-for-sibling',state)
 def after():
  try:cairosvg.svg2png(bytestring=b'<svg malformed')
  except Exception:pass
  t=svg('<rect width="100" height="100" fill="blue"/>');same(render('after-error',t),render('separate-call',t))
 case('parse-error-followed-by-real-render',after)
elif SLUG=='output-viewport':
 def width():
  t=svg('<rect x="10" y="20" width="100" height="50" fill="red"/>',100,50,'viewBox="10 20 100 50"');a=render('width-only',t,output_width=200);assert a.size==(200,100),a.size;px(a,0,0,red);px(a,199,99,red)
 case('width-only-preserves-ratio-and-edge-pixels',width)
 def units():
  t=svg('<rect width="100%" height="100%" fill="blue"/>','1in','0.5in');a=render('physical-dpi',t,dpi=120);assert a.size==(120,60),a.size;px(a,119,59,blue)
 case('physical-units-dpi-edge-coverage',units)
 def both():
  a=render('both-size',svg('<rect width="100%" height="100%" fill="red"/>',100,50),output_width=80,output_height=80);assert a.size==(80,80),a.size
 case('explicit-width-height-exact-raster-size',both)
else:raise ValueError(SLUG)
result={'slug':SLUG,'renderer_source':cairosvg.__file__,'tests':len(CASES),'passed':sum(c['passed'] for c in CASES),'failures':sum(not c['passed'] for c in CASES),'cases':CASES,'images':ART}
(OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
sections=''.join('<section><h2>'+html.escape(n)+'</h2><a href="'+n+'.svg">SVG输入</a><br><img src="'+n+'.png" style="image-rendering:pixelated;max-width:100%;width:600px"></section>' for n in ART)
(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>渲染核验 '+SLUG+'</title><style>body{font:18px system-ui;margin:24px;background:#ddd}section{display:inline-block;vertical-align:top;padding:20px;margin:8px;background:white}img{border:1px solid #888;background:repeating-conic-gradient(#eee 0% 25%,#fff 0% 50%) 50%/20px 20px}</style><h1>'+SLUG+'</h1><pre>'+html.escape(json.dumps(result,ensure_ascii=False,indent=2))+'</pre>'+sections)
print(json.dumps(result,ensure_ascii=False,indent=2));sys.exit(bool(result['failures']))
