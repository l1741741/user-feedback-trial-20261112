from pathlib import Path
from io import BytesIO
import sys,json,html,base64
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
def inner(color):return '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="10 20 40 40"><style>.paint{fill:url(#g)}</style><defs><linearGradient id="g"><stop stop-color="'+color+'"/><stop offset="1" stop-color="'+color+'"/></linearGradient></defs><rect class="paint" x="10" y="20" width="20" height="40"/></svg>'
def uri(s):return 'data:image/svg+xml;base64,'+base64.b64encode(s.encode()).decode()
head='<svg xmlns="http://www.w3.org/2000/svg" width="260" height="120"><defs><linearGradient id="g"><stop stop-color="lime"/><stop offset="1" stop-color="lime"/></linearGradient></defs>';first='<image x="10" y="10" width="80" height="80" href="'+uri(inner('red'))+'"/>';second='<image x="110" y="10" width="80" height="80" href="'+uri(inner('blue'))+'"/>';sibling='<rect x="220" y="20" width="20" height="60" fill="url(#g)"/>'
def render(n,s):
 names.append(n);(OUT/(n+'.svg')).write_text(s);d=cairosvg.svg2png(bytestring=s.encode());(OUT/(n+'.png')).write_bytes(d);return Image.open(BytesIO(d)).convert('RGBA')
a=render('two-icons',head+first+second+sibling+'</svg>')
for label,pt,want in [('red-icon',(30,50),(255,0,0,255)),('red-transparent-half',(70,50),(0,0,0,0)),('blue-icon',(130,50),(0,0,255,255)),('blue-transparent-half',(170,50),(0,0,0,0)),('outer-same-id-remains-green',(230,50),(0,255,0,255))]:
 actual=a.getpixel(pt);checks.append({'name':label,'point':pt,'actual':actual,'expected':want,'passed':actual==want})
b=render('reverse-icons',head+second+first+sibling+'</svg>');checks.append({'name':'same-ids-independent-of-reference-order','passed':a.tobytes()==b.tobytes()})
bad='<image width="40" height="40" href="'+uri('<svg broken')+'"/>';b=render('broken-before-icons',head+bad+first+second+sibling+'</svg>');checks.append({'name':'broken-embed-does-not-block-siblings','passed':a.tobytes()==b.tobytes()})
# Use actual relative files, no external fetch or policy changes.
icons=OUT/'icons';icons.mkdir();(icons/'paint.svg').write_text(inner('red'));outer=OUT/'relative.svg';outer.write_text(head+'<image x="10" y="10" width="80" height="80" href="icons/paint.svg"/>'+sibling+'</svg>');data=cairosvg.svg2png(url=str(outer));(OUT/'relative.png').write_bytes(data);im=Image.open(BytesIO(data)).convert('RGBA');checks.append({'name':'relative-local-resource-from-source','passed':im.getpixel((30,50))==(255,0,0,255) and im.getpixel((230,50))==(0,255,0,255)});names.append('relative')
from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface
tree=Tree(bytestring=(OUT/'two-icons.svg').read_bytes());original=[(e.tag,dict(e.attrib)) for e in tree.xml_tree.iter()];data=[]
for i in range(2):
 sink=BytesIO();surf=PNGSurface(tree,sink,96);surf.finish();data.append(sink.getvalue())
checks.append({'name':'repeated-same-tree-export-identical','passed':data[0]==data[1]});checks.append({'name':'outer-input-tree-unchanged','passed':original==[(e.tag,dict(e.attrib)) for e in tree.xml_tree.iter()]})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>嵌入文档隔离</title><style>body{font:20px system-ui;margin:24px}img{width:650px;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>两个同id图标、外层同id定义与透明区</h1><p>红蓝图标各保留一半透明，外层绿色不变，反转顺序或加入损坏图标不影响正常内容。</p>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG</p><img src="'+n+'.png">' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
