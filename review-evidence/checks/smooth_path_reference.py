from pathlib import Path
from io import BytesIO
import sys,json,html
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
cases=[('cubic-reflect','M10 90C30 10 50 10 70 90S110 170 140 90','M10 90C30 10 50 10 70 90C90 170 110 170 140 90'),('line-reset','M10 90C30 10 50 10 70 90L90 90S130 10 160 90','M10 90C30 10 50 10 70 90L90 90C90 90 130 10 160 90'),('quadratic-reflect','M10 90Q50 10 90 90T170 90','M10 90Q50 10 90 90Q130 170 170 90'),('family-switch','M10 90C30 10 50 10 70 90T140 90','M10 90C30 10 50 10 70 90Q70 90 140 90'),('subpath-reset','M10 90Q30 10 60 90M90 90T160 90','M10 90Q30 10 60 90M90 90Q90 90 160 90'),('relative-reflect','M10 90q40 -80 80 0t80 0','M10 90Q50 10 90 90Q130 170 170 90')]
def render(n,path):
 names.append(n);s='<svg xmlns="http://www.w3.org/2000/svg" width="240" height="190"><path d="'+path+'" fill="none" stroke="red" stroke-width="4"/><rect x="210" y="10" width="20" height="40" fill="blue"/></svg>';(OUT/(n+'.svg')).write_text(s);p=cairosvg.svg2png(bytestring=s.encode());(OUT/(n+'.png')).write_bytes(p);return Image.open(BytesIO(p)).convert('RGBA')
for n,actual,explicit in cases:
 a=render(n,actual);b=render(n+'-reference',explicit);diff=sum(x!=y for x,y in zip(a.getdata(),b.getdata()));checks.append({'name':n+'-explicit-control-equivalence','different_pixels':diff,'passed':diff==0});checks.append({'name':n+'-sibling','passed':a.getpixel((220,30))==(0,0,255,255)})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>平滑路径控制点核验</title><style>body{font:20px system-ui;margin:24px}img{width:480px;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>平滑命令与显式控制点</h1><p>同族反射、直线重置、跨族重置、子路径与相对命令，逐像素对照。</p>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG</p><img src="'+n+'.png">' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
