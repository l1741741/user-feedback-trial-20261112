"""Independent PNG equivalence checks for cubic/quadratic control inheritance."""
from pathlib import Path
from io import BytesIO
import html,json,sys
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[]
cases=[
 ('cubic-reflection','M20 80 C40 20 60 20 80 80 S120 140 140 80','M20 80 C40 20 60 20 80 80 C100 140 120 140 140 80'),
 ('relative-cubic','M20 80 c20 -60 40 -60 60 0 s40 60 60 0','M20 80 C40 20 60 20 80 80 C100 140 120 140 140 80'),
 ('mixed-quadratic','M20 80 q30 -40 60 0 t60 0 T200 80','M20 80 Q50 40 80 80 Q110 120 140 80 Q170 40 200 80'),
 ('repeated-cubic-groups','M20 80 C40 20 60 20 80 80 S120 140 140 80 180 20 200 80','M20 80 C40 20 60 20 80 80 C100 140 120 140 140 80 C160 20 180 20 200 80'),
 ('line-reset','M20 80 C40 20 60 20 80 80 L100 100 S150 20 200 80','M20 80 C40 20 60 20 80 80 L100 100 C100 100 150 20 200 80'),
 ('arc-reset','M20 80 C40 20 60 20 80 80 A20 20 0 0 1 120 80 S180 140 200 80','M20 80 C40 20 60 20 80 80 A20 20 0 0 1 120 80 C120 80 180 140 200 80'),
 ('move-reset','M20 80 C40 20 60 20 80 80 M100 100 S150 20 200 80','M20 80 C40 20 60 20 80 80 M100 100 C100 100 150 20 200 80'),
 ('close-reset','M20 80 C40 20 60 20 80 80 Z S140 140 200 80','M20 80 C40 20 60 20 80 80 Z C20 80 140 140 200 80'),
 ('cubic-to-quadratic-default','M20 80 C40 20 60 20 80 80 T140 80','M20 80 C40 20 60 20 80 80 Q80 80 140 80'),
 ('quadratic-to-cubic-default','M20 80 Q50 20 80 80 S140 140 200 80','M20 80 Q50 20 80 80 C80 80 140 140 200 80'),
 ('zero-length-line-reset','M20 80 Q50 20 80 80 l0 0 t60 0','M20 80 Q50 20 80 80 l0 0 q0 0 60 0'),
 ('quadratic-close-reset','M20 80 Q50 20 80 80 Z T140 80','M20 80 Q50 20 80 80 Z Q20 80 140 80')]
def svg(d):return '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="180"><rect width="240" height="180" fill="white"/><path d="'+d+'" fill="none" stroke="#d21f35" stroke-width="3"/></svg>'
def render(n,d):
 t=svg(d);(OUT/(n+'.svg')).write_text(t);png=cairosvg.svg2png(bytestring=t.encode());(OUT/(n+'.png')).write_bytes(png);return Image.open(BytesIO(png)).convert('RGBA')
for n,a,b in cases:
 actual=render(n+'-smooth',a);reference=render(n+'-explicit',b);changed=sum(x!=y for x,y in zip(actual.getdata(),reference.getdata()));checks.append({'name':n,'changed_pixels':changed,'passed':changed==0})
# Independent paths cannot inherit a preceding path's control points.
first='<path d="M20 30 C40 10 60 10 80 30" stroke="blue" fill="none"/>';second='<path d="M100 120 S150 40 200 120" stroke="red" fill="none"/>';head='<svg xmlns="http://www.w3.org/2000/svg" width="240" height="180">'
a=cairosvg.svg2png(bytestring=(head+first+second+'</svg>').encode());b=cairosvg.svg2png(bytestring=(head+first+second.replace('S150 40','C100 120 150 40')+'</svg>').encode());(OUT/'independent-paths.png').write_bytes(a);checks.append({'name':'independent-paths-reset','passed':Image.open(BytesIO(a)).tobytes()==Image.open(BytesIO(b)).tobytes()})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks}
(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2))
(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>平滑曲线控制点对照</title><style>body{font:19px system-ui;margin:24px}img{width:360px;max-width:100%;border:1px solid #aaa}section{display:inline-block;margin:12px}pre{white-space:pre-wrap}</style><h1>平滑命令与显式控制点应画出相同曲线</h1>'+''.join('<h2>'+n+'</h2><section><h3>本次平滑写法PNG</h3><img src="'+n+'-smooth.png"></section><section><h3>本次显式控制点PNG</h3><img src="'+n+'-explicit.png"></section><details><summary>两份实际SVG输入</summary><pre>'+html.escape(svg(a)+'\n'+svg(b))+'</pre></details>' for n,a,b in cases)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>')
sys.exit(bool(r['failed']))
