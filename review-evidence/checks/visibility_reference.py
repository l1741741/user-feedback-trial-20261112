from pathlib import Path
import sys,json,io,html
import cairosvg
from PIL import Image
from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface
OUT=Path(sys.argv[-1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
def svg(body):return '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="240" height="120">'+body+'</svg>'
def render(name,body):
 t=svg(body);(OUT/(name+'.svg')).write_text(t);png=cairosvg.svg2png(bytestring=t.encode());(OUT/(name+'.png')).write_bytes(png);names.append(name);return Image.open(io.BytesIO(png)).convert('RGBA')
blue='<rect x="190" y="20" width="30" height="60" fill="blue"/>';r='<rect x="20" y="20" width="60" height="60" fill="red"/>';green='<rect x="100" y="20" width="60" height="60" fill="lime" visibility="visible"/>'
for name,body,points in [('visible-child','<g visibility="hidden">'+r+green+'</g>'+blue,[(50,50,(0,0,0,0)),(130,50,(0,255,0,255)),(200,50,(0,0,255,255))]),('display-none','<g display="none">'+r+green+'</g>'+blue,[(50,50,(0,0,0,0)),(130,50,(0,0,0,0)),(200,50,(0,0,255,255))]),('css-priority','<style>.hidden {visibility:hidden} .shown {visibility:visible !important}</style><g class="hidden"><rect x="20" y="20" width="60" height="60" fill="red" class="shown" style="visibility:hidden"/></g>'+blue,[(50,50,(255,0,0,255)),(200,50,(0,0,255,255))])]:
 im=render(name,body)
 for x,y,want in points:checks.append({'name':name+'-'+str(x),'actual':im.getpixel((x,y)),'expected':want,'passed':im.getpixel((x,y))==want})
for name,attr in [('hidden','visibility="hidden"'),('collapse','visibility="collapse"'),('display','display="none"')]:
 im=render('clip-'+name,'<defs><clipPath id="c"><rect width="100" height="100" '+attr+'/></clipPath></defs><rect width="100" height="100" fill="red" clip-path="url(#c)"/>'+blue);checks.append({'name':'clip-'+name,'passed':im.getpixel((50,50))==(0,0,0,0) and im.getpixel((200,50))==(0,0,255,255)})
body='<defs><rect id="r" width="40" height="40" fill="red"/></defs><use xlink:href="#r" x="20" y="20" visibility="hidden"/><use xlink:href="#r" x="100" y="20"/>'+blue
im=render('use-instances',body);checks.append({'name':'use-hidden-isolated','passed':im.getpixel((30,30))==(0,0,0,0) and im.getpixel((110,30))==(255,0,0,255)})
tree=Tree(bytestring=svg(body).encode());values=[]
for i in range(2):
 stream=io.BytesIO();s=PNGSurface(tree,stream,96);s.finish();values.append(stream.getvalue())
checks.append({'name':'same-tree-repeat','passed':values[0]==values[1]})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(x['passed'] for x in checks),'failed':sum(not x['passed'] for x in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>可见性与裁剪引用</title><style>body{font:20px system-ui;margin:24px}img{width:720px;max-width:100%;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>可见子项、整支隐藏及引用隔离</h1>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG</p><img src="'+n+'.png">' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
