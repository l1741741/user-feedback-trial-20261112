"""Translate both geometry and viewBox origin; rendered image must be unchanged."""
from pathlib import Path
from io import BytesIO
import html,json,sys
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];pairs=[]
icon='<rect width="100" height="100" fill="white"/><rect width="20" height="20" fill="red"/><rect x="80" width="20" height="20" fill="lime"/><rect y="80" width="20" height="20" fill="blue"/><rect x="80" y="80" width="20" height="20" fill="yellow"/><path d="M50 0V100M0 50H100" stroke="black" stroke-width="4"/>'
def doc(w,h,x,y,par):
 return f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="{x} {y} 100 100" preserveAspectRatio="{par}"><g transform="translate({x} {y})">'+icon+'</g></svg>'
def render(n,text,**kw):
 (OUT/(n+'.svg')).write_text(text);data=cairosvg.svg2png(bytestring=text.encode(),**kw);(OUT/(n+'.png')).write_bytes(data);return Image.open(BytesIO(data)).convert('RGBA')
for w,h in [(200,100),(100,200)]:
 for par in ['xMidYMid meet','xMidYMid slice','none']:
  tag=f'{w}x{h}-'+par.replace(' ','-');ref=render(tag+'-reference',doc(w,h,0,0,par))
  for x,y in [(10,20),(-15,-25)]:
   n=tag+f'-origin-{x}-{y}';actual=render(n,doc(w,h,x,y,par));changed=sum(a!=b for a,b in zip(actual.getdata(),ref.getdata()));checks.append({'name':n,'passed':changed==0,'changed_pixels':changed});pairs.append((n,tag+'-reference'))
root=doc(200,100,0,0,'xMidYMid slice');a=render('scale-two',root,scale=2);b=render('output-size',root,output_width=400,output_height=200);checks.append({'name':'scale-vs-output-dimensions','passed':a.tobytes()==b.tobytes()})
first=render('fresh-root-before',doc(200,100,0,0,'none'));render('intervening-slice',doc(100,200,0,0,'xMidYMid slice'));last=render('fresh-root-after',doc(200,100,0,0,'none'));checks.append({'name':'fresh-call-isolation','passed':first.tobytes()==last.tobytes()})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(x['passed'] for x in checks),'failed':sum(not x['passed'] for x in checks),'checks':checks,'oracle':'Adding the same translation to viewBox origin and all geometry preserves the device image'}
(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2))
(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>非零viewBox等价变换</title><style>body{font:19px system-ui;margin:24px}img{width:400px;max-width:100%;border:1px solid #aaa;background:#eee}section{display:inline-block;margin:12px}pre{white-space:pre-wrap}</style><h1>图形与viewBox同时平移，PNG应完全相同</h1><p>左为非零起点的当次PNG，右为等价零起点参考。两侧各有四色角标和黑色中心十字。</p>'+''.join('<h2>'+a+'</h2><section><h3>非零起点实际图</h3><img src="'+a+'.png"></section><section><h3>等价零起点参考</h3><img src="'+b+'.png"></section><details><summary>实际SVG输入</summary><pre>'+html.escape((OUT/(a+'.svg')).read_text())+'</pre></details>' for a,b in pairs[:6])+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>')
sys.exit(bool(r['failed']))
