from pathlib import Path
from io import BytesIO
import sys,json,html
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
def render(n,body):
 names.append(n);s='<svg xmlns="http://www.w3.org/2000/svg" width="260" height="120">'+body+'</svg>';(OUT/(n+'.svg')).write_text(s);d=cairosvg.svg2png(bytestring=s.encode());(OUT/(n+'.png')).write_bytes(d);return Image.open(BytesIO(d)).convert('RGBA')
defs='<defs><symbol id="s" viewBox="0 0 20 20"><rect width="20" height="20"/><rect x="15" width="5" height="5" fill="white"/></symbol><g id="loop"><use href="#loop"/></g></defs>'
red='<use href="#s" x="10" y="10" width="60" height="60" fill="red"/>';blue='<use href="#s" x="120" y="20" width="80" height="80" fill="blue"/>';sib='<rect x="230" y="20" width="20" height="60" fill="lime"/>'
a=render('instances',defs+red+blue+sib);b=render('reverse-order',defs+blue+red+sib);checks.append({'name':'reverse-order-independent','passed':a.tobytes()==b.tobytes()})
for n,body,point,wanted in [('red',red,(30,30),(255,0,0,255)),('blue',blue,(150,50),(0,0,255,255))]:
 im=render(n+'-alone',defs+body+sib);crop=(10,10,70,70) if n=='red' else (120,20,200,100);checks.append({'name':n+'-instance-matches-alone','passed':a.crop(crop).tobytes()==im.crop(crop).tobytes()});checks.append({'name':n+'-pixel','passed':a.getpixel(point)==wanted})
a=render('loop-before-valid',defs+'<use href="#loop"/>'+red+blue+sib);checks.append({'name':'cyclic-reference-does-not-block-valid-instances','passed':a.tobytes()==b.tobytes()})
a=render('missing-before-valid',defs+'<use href="#missing"/>'+red+blue+sib);checks.append({'name':'missing-reference-does-not-block-valid','passed':a.tobytes()==b.tobytes()})
# Render the same parsed tree twice; inspect all original XML attributes.
from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface
tree=Tree(bytestring=(OUT/'instances.svg').read_bytes());before=[(e.tag,dict(e.attrib)) for e in tree.xml_tree.iter()];arr=[]
for k in range(2):
 sink=BytesIO();surface=PNGSurface(tree,sink,96);surface.finish();arr.append(sink.getvalue());(OUT/('tree-repeat-'+str(k)+'.png')).write_bytes(arr[-1])
checks.append({'name':'same-tree-repeat-identical','passed':arr[0]==arr[1]});checks.append({'name':'input-xml-attributes-unchanged','passed':before==[(e.tag,dict(e.attrib)) for e in tree.xml_tree.iter()]})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>复用实例隔离</title><style>body{font:20px system-ui;margin:24px}img{width:650px;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>独立实例、引用顺序与错误引用</h1>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG</p><img src="'+n+'.png">' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
