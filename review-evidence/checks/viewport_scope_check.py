from pathlib import Path
from io import BytesIO
import html,json,sys
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
def render(n,s):
 names.append(n);(OUT/(n+'.svg')).write_text(s);p=cairosvg.svg2png(bytestring=s.encode());(OUT/(n+'.png')).write_bytes(p);return Image.open(BytesIO(p)).convert('RGBA')
head='<svg xmlns="http://www.w3.org/2000/svg" width="320" height="150">';sibling='<rect x="270" y="20" width="30" height="30" fill="blue"/>'
for mode,x,y,w,h in [('meet',70,20,50,50),('slice',20,20,100,50),('none',20,20,100,50)]:
 par='none' if mode=='none' else 'xMidYMid '+mode
 body='<rect x="10" y="10" width="50%" height="50%" fill="red"/>'
 a=render(mode,head+'<svg x="20" y="20" width="200" height="100" viewBox="10 10 100 100" preserveAspectRatio="'+par+'">'+body+'</svg>'+sibling+'</svg>')
 b=render(mode+'-reference',head+f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="red"/>'+sibling+'</svg>')
 checks.append({'name':mode+'-explicit-geometry','passed':a.tobytes()==b.tobytes()})
 s=render(mode+'-symbol',head+'<defs><symbol id="s" viewBox="10 10 100 100">'+body+'</symbol></defs><use href="#s" x="20" y="20" width="200" height="100" preserveAspectRatio="'+par+'"/>'+sibling+'</svg>')
 checks.append({'name':mode+'-symbol-equivalence','passed':a.tobytes()==s.tobytes()})
 checks.append({'name':mode+'-outer-sibling','passed':a.getpixel((280,30))==(0,0,255,255)})
for label,vb in [('zero-width','0 0 0 100'),('zero-height','0 0 100 0'),('zero-both','0 0 0 0')]:
 a=render(label,head+'<svg x="20" y="20" width="200" height="100" viewBox="'+vb+'"><rect x="5" y="5" width="30" height="30" fill="red"/></svg>'+sibling+'</svg>')
 checks.append({'name':label+'-empty','passed':a.getpixel((30,30))==(0,0,0,0)});checks.append({'name':label+'-outer-sibling','passed':a.getpixel((280,30))==(0,0,255,255)})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2))
(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>嵌套视口与独立几何</title><style>body{font:20px system-ui;margin:24px}img{width:640px;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>meet/slice/none、symbol与零面积视口</h1><p>前九组依次为嵌套、显式几何参考和symbol；三种映射均应与各自参考一致。零面积视口无红块，蓝色外层兄弟保持不变。</p>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG</p><img src="'+n+'.png">' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
