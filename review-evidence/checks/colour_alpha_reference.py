from pathlib import Path
from io import BytesIO
import sys,json,html
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
def render(n,body,**kw):
 names.append(n);s='<svg xmlns="http://www.w3.org/2000/svg" width="240" height="120">'+body+'<rect x="210" y="20" width="20" height="60" fill="blue"/></svg>';(OUT/(n+'.svg')).write_text(s);data=cairosvg.svg2png(bytestring=s.encode(),**kw);(OUT/(n+'.png')).write_bytes(data);return Image.open(BytesIO(data)).convert('RGBA')
for n,color,alpha in [('short-hex','#0f08',136),('long-hex','#00ff0080',128),('modern','rgb(0 255 0 / 50%)',128),('legacy','rgba(0,255,0,.5)',128),('percent','rgb(0% 100% 0% / 50%)',128)]:
 im=render(n,'<rect x="10" y="10" width="80" height="80" fill="'+color+'"/>');actual=im.getpixel((30,30));checks.append({'name':n+'-rgba','actual':actual,'expected':[0,255,0,alpha],'passed':actual==(0,255,0,alpha)})
im=render('composite','<g opacity="0.5"><rect x="10" y="10" width="80" height="80" fill="#00ff0080" fill-opacity="0.5"/></g>');a=im.getpixel((30,30));checks.append({'name':'color-fill-group-alpha-once','actual':a,'passed':a[:3]==(0,255,0) and a[3] in [32,33]})
for n,col in [('bad-count','rgb(1,2)'),('bad-token','rgb(foo)'),('mixed','rgb(100%,0,0)')]:
 im=render(n,'<rect x="10" y="10" width="80" height="80" fill="'+col+'"/>');checks.append({'name':n+'-fallback-and-sibling','passed':im.getpixel((30,30))==(0,0,0,255) and im.getpixel((220,50))==(0,0,255,255)})
im=render('negate','<rect x="10" y="10" width="80" height="80" fill="#0f08"/>',negate_colors=True);a=im.getpixel((30,30));checks.append({'name':'negate-keeps-alpha','actual':a,'passed':a==(255,0,255,136)})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>颜色与透明度</title><style>body{font:20px system-ui;margin:24px}img{width:600px;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>颜色语法、透明度合成及非法值</h1>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG</p><img src="'+n+'.png">' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
