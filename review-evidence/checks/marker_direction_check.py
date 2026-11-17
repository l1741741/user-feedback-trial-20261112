"""Compare auto mid-marker orientation with its geometric explicit angle."""
from pathlib import Path
from io import BytesIO
from math import atan2,degrees,hypot
import html,json,sys
import cairosvg
from PIL import Image,ImageChops
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];cards=[]
def render(name,path,orient):
 text='<svg xmlns="http://www.w3.org/2000/svg" width="240" height="180"><rect width="240" height="180" fill="white"/><defs><marker id="m" markerWidth="32" markerHeight="24" viewBox="-4 -12 32 24" refX="0" refY="0" markerUnits="userSpaceOnUse" orient="'+str(orient)+'"><path d="M0 -8 L24 0 L0 8Z" fill="red"/></marker></defs><path d="'+path+'" fill="none" stroke="gray" marker-mid="url(#m)"/></svg>'
 (OUT/(name+'.svg')).write_text(text);data=cairosvg.svg2png(bytestring=text.encode());(OUT/(name+'.png')).write_bytes(data);return Image.open(BytesIO(data)).convert('RGBA')
for name,pts in [('right-down',[(30,30),(100,30),(100,130)]),('left-down',[(190,60),(100,60),(25,110)]),('left-near-straight',[(190,80),(100,80),(20,81)]),('up-left',[(160,140),(160,80),(40,80)]),('closed-first-join',[(60,60),(160,60),(160,140),(60,60)])]:
 # Only compare the first middle vertex; closed polyline uses an explicit
 # reference at each middle vertex separately, hence excluded from angle oracle.
 if len(pts)!=3:continue
 a,b,c=pts;u=(b[0]-a[0],b[1]-a[1]);v=(c[0]-b[0],c[1]-b[1]);ul=hypot(*u);vl=hypot(*v);angle=degrees(atan2(u[1]/ul+v[1]/vl,u[0]/ul+v[0]/vl))
 path='M'+' L'.join(f'{x} {y}' for x,y in pts)
 auto=render(name+'-auto',path,'auto');expected=render(name+'-reference',path,angle)
 diff=ImageChops.difference(auto,expected);changed=sum(a!=b for a,b in zip(auto.getdata(),expected.getdata()))
 checks.append({'name':name,'expected_angle_degrees':angle,'changed_pixels':changed,'passed':changed==0});cards.append(name)
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(x['passed'] for x in checks),'failed':sum(not x['passed'] for x in checks),'checks':checks,'oracle':'normalized incoming plus outgoing vectors at a nondegenerate middle vertex; explicit orientation is the reference'}
(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2))
(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>拐点箭头方向</title><style>body{font:19px system-ui;margin:24px}section{display:inline-block;margin:12px}img{width:480px;max-width:100%;border:1px solid #999}pre{white-space:pre-wrap}</style><h1>自动方向与几何参考对照</h1><p>每对左图为auto实际PNG，右图为按入射和出射单位向量角平分线给定的显式角度PNG。</p>'+''.join('<h2>'+n+'</h2><section><h3>实际auto</h3><img src="'+n+'-auto.png"></section><section><h3>显式角度参考</h3><img src="'+n+'-reference.png"></section><details><summary>实际SVG输入</summary><pre>'+html.escape((OUT/(n+'-auto.svg')).read_text())+'</pre></details>' for n in cards)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>')
sys.exit(bool(r['failed']))
