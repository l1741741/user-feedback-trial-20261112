from pathlib import Path
import io,json,sys,html,copy,math
import cairosvg
from PIL import Image,ImageChops,ImageFilter
from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface
OUT=Path(sys.argv[-1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
def svg(body):return '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="360" height="260" viewBox="0 0 360 260">'+body+'</svg>'
def render(name,body):
 text=svg(body);(OUT/(name+'.svg')).write_text(text);data=cairosvg.svg2png(bytestring=text.encode());(OUT/(name+'.png')).write_bytes(data);names.append(name);return Image.open(io.BytesIO(data)).convert('RGBA')
def check(name,body,reference):
 a=render(name,body);b=render(name+'-reference',reference);different=sum(x!=y for x,y in zip(a.getdata(),b.getdata()));checks.append({'name':name,'different_pixels':different,'passed':different==0})
rect='<rect x="100" y="80" width="100" height="40" fill="red" {}/>';blue='<rect x="300" y="20" width="20" height="20" fill="blue"/>'
check('fill-box-centre',rect.format('transform="rotate(90)" transform-box="fill-box" transform-origin="50% 50%"')+blue,'<rect x="130" y="50" width="40" height="100" fill="red"/>'+blue)
check('fill-box-corner',rect.format('transform="rotate(90)" transform-box="fill-box" transform-origin="0% 0%"')+blue,'<rect x="60" y="80" width="40" height="100" fill="red"/>'+blue)
check('explicit-length-centre',rect.format('transform="rotate(90)" transform-origin="150px 100px"')+blue,'<rect x="130" y="50" width="40" height="100" fill="red"/>'+blue)
check('view-box-centre',rect.format('transform="rotate(90)" transform-origin="50% 50%"')+blue,'<rect x="190" y="50" width="40" height="100" fill="red"/>'+blue)
check('list-order', '<rect x="10" y="20" width="30" height="10" fill="red" transform="translate(60 40) scale(2)"/>'+blue,'<rect x="80" y="80" width="60" height="20" fill="red"/>'+blue)
check('nested-order','<g transform="translate(60 40)"><rect x="10" y="20" width="30" height="10" fill="red" transform="scale(2)"/></g>'+blue,'<rect x="80" y="80" width="60" height="20" fill="red"/>'+blue)
check('group-box-centre','<g transform="rotate(90)" transform-box="fill-box" transform-origin="50% 50%"><rect x="100" y="80" width="100" height="40" fill="red"/></g>'+blue,'<rect x="130" y="50" width="40" height="100" fill="red"/>'+blue)
check('invalid-origin-fallback', '<rect x="10" y="20" width="30" height="10" fill="red" transform="translate(60 40)" transform-origin="left right"/>'+blue,'<rect x="70" y="60" width="30" height="10" fill="red"/>'+blue)
check('singular-no-sibling-leak','<rect x="10" y="20" width="30" height="10" fill="red" transform="scale(0)"/>'+blue,blue)
body='<defs><rect id="r" x="10" y="20" width="30" height="10" fill="red"/></defs><use xlink:href="#r" x="50" y="20"/>'+blue
tree=Tree(bytestring=svg(body).encode());before=copy.deepcopy(dict(tree));outputs=[]
for n in range(2):
 stream=io.BytesIO();surface=PNGSurface(tree,stream,96);surface.finish();outputs.append(stream.getvalue());(OUT/('reuse-'+str(n)+'.png')).write_bytes(outputs[-1])
checks.append({'name':'same-tree-repeat','passed':outputs[0]==outputs[1] and dict(tree)==before})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(x['passed'] for x in checks),'failed':sum(not x['passed'] for x in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>变换原点与独立几何参考</title><style>body{font:20px system-ui;margin:24px}img{width:720px;max-width:100%;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>变换原点、顺序与兄弟状态</h1><p>红色对象按解析几何定位；蓝色兄弟保持原位。输入SVG和当次PNG逐对对照。</p>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG</p><img src="'+n+'.png">' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
