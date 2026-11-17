"""Compare transform-origin cases to explicit translate/rotate/translate geometry."""
from pathlib import Path
from io import BytesIO
import html,json,sys
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];pairs=[]
def render(n,svg):
 (OUT/(n+'.svg')).write_text(svg);data=cairosvg.svg2png(bytestring=svg.encode());(OUT/(n+'.png')).write_bytes(data);return Image.open(BytesIO(data)).convert('RGBA')
# Reference uses ordinary transform lists; no origin helper or formula from implementation.
for name,w,h,vb,x,y,rw,rh,angle,attrs,ox,oy in [
 ('center-bottom',240,200,'0 0 240 200',110,160,20,20,90,'transform-origin="center bottom"',120,200),
 ('nonzero-viewbox-percent',240,160,'10 20 120 80',30,40,20,10,180,'transform-origin="50% 50%"',70,60),
 ('fillbox-center',240,200,'0 0 240 200',40,40,60,20,90,'transform-origin="50% 50%" transform-box="fill-box"',70,50),
 ('explicit-length',240,200,'0 0 240 200',40,40,60,20,90,'transform-origin="70 50"',70,50),
 ('zero-viewbox-percent',240,160,'0 0 120 80',30,20,20,10,180,'transform-origin="50% 50%"',60,40)]:
  head=f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="{vb}">';body=f'<rect x="{x}" y="{y}" width="{rw}" height="{rh}" fill="red"';tail='<rect x="5" y="5" width="5" height="5" fill="blue"/></svg>'
  actual=render(name+'-actual',head+body+f' {attrs} transform="rotate({angle})"/>'+tail)
  reference=render(name+'-reference',head+f'<g transform="translate({ox} {oy}) rotate({angle}) translate({-ox} {-oy})">'+body+'/></g>'+tail)
  changed=sum(a!=b for a,b in zip(actual.getdata(),reference.getdata()));checks.append({'name':name,'expected_origin':[ox,oy],'changed_pixels':changed,'passed':changed==0});pairs.append(name)
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks}
(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2))
(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>变换原点参考</title><style>body{font:19px system-ui;margin:24px}section{display:inline-block;margin:10px}img{width:480px;max-width:100%;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>原点声明与显式平移旋转对照</h1><p>左为transform-origin实际PNG，右为等价translate/rotate/translate参考。</p>'+''.join('<h2>'+n+'</h2><section><h3>实际PNG</h3><img src="'+n+'-actual.png"></section><section><h3>显式参考PNG</h3><img src="'+n+'-reference.png"></section><details><summary>SVG输入</summary><pre>'+html.escape((OUT/(n+'-actual.svg')).read_text())+'</pre></details>' for n in pairs)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>')
sys.exit(bool(r['failed']))
