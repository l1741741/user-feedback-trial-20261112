from pathlib import Path
from io import BytesIO
import json,sys,html
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[]
head='<svg xmlns="http://www.w3.org/2000/svg" width="400" height="140">';defs='<defs><marker id="m" markerWidth="12" markerHeight="12" viewBox="-6 -6 12 12" refX="0" refY="0" markerUnits="userSpaceOnUse" orient="0"><rect x="-6" y="-6" width="12" height="12" fill="lime"/></marker></defs>';d='M20 20H80V80H20Z';actual=[];reference=[]
for index,(name,order) in enumerate([('normal',['fill','stroke','markers']),('stroke',['stroke','fill','markers']),('markers',['markers','fill','stroke'])]):
 x=index*130;actual.append(f'<g transform="translate({x} 10)"><path d="{d}" fill="blue" stroke="red" stroke-width="16" marker-end="url(#m)" paint-order="{name}"/></g>');parts={'fill':f'<path d="{d}" fill="blue"/>','stroke':f'<path d="{d}" fill="none" stroke="red" stroke-width="16"/>','markers':'<rect x="14" y="14" width="12" height="12" fill="lime"/>'};reference.append(f'<g transform="translate({x} 10)">'+''.join(parts[op] for op in order)+'</g>')
def render(n,text):
 (OUT/(n+'.svg')).write_text(text);png=cairosvg.svg2png(bytestring=text.encode());(OUT/(n+'.png')).write_bytes(png);return Image.open(BytesIO(png)).convert('RGBA')
sibling='<rect x="350" y="100" width="30" height="30" fill="black"/>';a=render('actual',head+defs+''.join(actual)+sibling+'</svg>');b=render('explicit-layers',head+''.join(reference)+sibling+'</svg>');checks.append({'name':'all-three-orders-match-independent-explicit-layers','passed':a.tobytes()==b.tobytes(),'changed_pixels':sum(x!=y for x,y in zip(a.getdata(),b.getdata()))})
for i,(name,edge,corner) in enumerate([('normal',(255,0,0,255),(0,255,0,255)),('stroke',(0,0,255,255),(0,255,0,255)),('markers',(255,0,0,255),(255,0,0,255))]):
 for label,pt,wanted in [('stroke-fill',(i*130+24,60),edge),('marker-corner',(i*130+20,30),corner)]:
  value=a.getpixel(pt);checks.append({'name':name+'-'+label,'passed':value==wanted,'actual':value,'expected':wanted})
checks.append({'name':'black-sibling-unchanged','passed':a.getpixel((360,110))==(0,0,0,255)})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>绘制顺序独立对照</title><style>body{font:19px system-ui;margin:24px}img{width:1000px;max-width:100%;border:1px solid #999;background:#eee}pre{white-space:pre-wrap}</style><h1>填充、描边、marker顺序与显式分层对照</h1><p>每行从左到右：normal、stroke优先、markers优先。应与下方独立分层一致。</p>'+''.join('<h2>'+n+'</h2><h3>SVG输入</h3><img src="'+n+'.svg"><h3>本次PNG</h3><img src="'+n+'.png"><details><summary>源码</summary><pre>'+html.escape((OUT/(n+'.svg')).read_text())+'</pre></details>' for n in ['actual','explicit-layers'])+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
