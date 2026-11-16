"""Focused independent probes for referenced viewport and arc degeneracies."""
from pathlib import Path
from io import BytesIO
import json,base64,sys
import cairosvg
from PIL import Image,ImageChops
UID=sys.argv[1];OUT=Path(sys.argv[2]);OUT.mkdir(parents=True,exist_ok=True);C=[];N=[]
def svg(body,w=220,h=120):return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}">{body}</svg>'
def draw(n,s):
 (OUT/(n+'.svg')).write_text(s);data=cairosvg.svg2png(bytestring=s.encode());(OUT/(n+'.png')).write_bytes(data);N.append(n);return Image.open(BytesIO(data)).convert('RGBA')
def test(n,fn):
 try:fn();C.append({'name':n,'passed':True})
 except Exception as e:C.append({'name':n,'passed':False,'error':str(e)})
def px(im,x,y,c):assert im.getpixel((x,y))==c,(x,y,im.getpixel((x,y)),c)
def same(a,b):assert a.tobytes()==b.tobytes(),'images differ'
if UID=='12581':
 def viewbox():
  inner='<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60" viewBox="30 40 60 30"><rect x="30" y="40" width="30" height="30" fill="red"/><rect x="60" y="40" width="30" height="30" fill="blue"/></svg>';uri='data:image/svg+xml;base64,'+base64.b64encode(inner.encode()).decode();a=draw('referenced-nonzero-viewbox',svg(f'<image x="10" y="10" width="180" height="90" href="{uri}"/>'));px(a,25,25,(255,0,0,255));px(a,170,50,(0,0,255,255));px(a,5,50,(0,0,0,0));px(a,200,50,(0,0,0,0))
 test('referenced-viewbox-offset-and-double-scale',viewbox)
 def degenerate():
  inner='<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 0 100"><rect width="100" height="100" fill="red"/></svg>';uri='data:image/svg+xml;base64,'+base64.b64encode(inner.encode()).decode();a=draw('degenerate-viewbox-sibling',svg(f'<image width="100" height="100" href="{uri}"/><rect x="120" y="20" width="60" height="60" fill="blue"/>'));px(a,50,50,(0,0,0,0));px(a,150,50,(0,0,255,255))
 test('zero-viewbox-skipped-and-sibling-drawn',degenerate)
elif UID=='12591':
 for rx,ry,rot in [(-60,30,0),(60,-30,45),(-60,-30,75),(-20,10,30)]:
  def f(rx=rx,ry=ry,rot=rot):
   name=f'arc-{rx}-{ry}-{rot}';a=draw(name,svg(f'<path d="M20 60 A{rx} {ry} {rot} 1 1 170 60" stroke="red" stroke-width="4" fill="none"/>'));b=draw(name+'-positive',svg(f'<path d="M20 60 A{abs(rx)} {abs(ry)} {rot} 1 1 170 60" stroke="red" stroke-width="4" fill="none"/>'));same(a,b)
  test(f'negative-radii-{rx}-{ry}-{rot}',f)
 def endpoint():
  a=draw('omitted-arc',svg('<path d="M20 60A50 30 40 1 1 20 60L170 60" stroke="blue" stroke-width="4" fill="none"/>'));b=draw('omitted-reference',svg('<path d="M20 60L170 60" stroke="blue" stroke-width="4" fill="none"/>'));same(a,b)
 test('same-endpoint-arc-omitted-before-line',endpoint)
elif UID=='12584':
 def hidden_use():
  a=draw('display-none-use',svg('<defs><rect id="r" x="10" y="10" width="80" height="80" fill="red"/></defs><use href="#r" display="none"/><rect x="130" y="20" width="50" height="60" fill="blue"/>'));px(a,50,50,(0,0,0,0));px(a,150,50,(0,0,255,255))
 test('display-none-use-does-not-paint',hidden_use)
 def marker():
  a=draw('hidden-path-marker',svg('<defs><marker id="m" markerWidth="10" markerHeight="10" refX="5" refY="5"><rect width="10" height="10" fill="red"/></marker></defs><path d="M40 60H160" stroke="blue" stroke-width="3" visibility="hidden" marker-start="url(#m)" marker-end="url(#m)"/>'));px(a,40,60,(0,0,0,0));px(a,160,60,(0,0,0,0))
 test('hidden-path-does-not-draw-markers',marker)
 def descendants():
  a=draw('visible-child',svg('<g visibility="hidden"><rect width="60" height="60" fill="red"/><rect x="100" width="60" height="60" fill="blue" style="visibility:visible"/></g>'));px(a,20,20,(0,0,0,0));px(a,120,20,(0,0,255,255))
 test('hidden-parent-visible-descendant',descendants)
else:raise ValueError(UID)
R={'uid':UID,'source':cairosvg.__file__,'tests':len(C),'passed':sum(c['passed'] for c in C),'failed':sum(not c['passed'] for c in C),'checks':C};(OUT/'result.json').write_text(json.dumps(R,ensure_ascii=False,indent=2)+'\n');
(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>SVG渲染核验 '+UID+'</title><style>body{font:18px system-ui;background:#ddd;margin:24px}section{display:inline-block;padding:20px;margin:10px;background:white;vertical-align:top}img{width:440px;border:1px solid #888;background:white;image-rendering:pixelated}pre{white-space:pre-wrap}</style><h1>SVG输入与本次PNG输出 '+UID+'</h1><pre>'+json.dumps(R,ensure_ascii=False,indent=2)+'</pre>'+''.join('<section><h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>PNG输出</p><img src="'+n+'.png"></section>' for n in N))
print(json.dumps(R,ensure_ascii=False,indent=2));sys.exit(bool(R['failed']))
