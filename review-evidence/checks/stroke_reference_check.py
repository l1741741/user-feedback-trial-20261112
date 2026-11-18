from pathlib import Path
from io import BytesIO
from math import atan2,degrees
import sys,json,html
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
def render(n,body):
 names.append(n);s='<svg xmlns="http://www.w3.org/2000/svg" width="240" height="150">'+body+'<rect x="210" y="20" width="20" height="60" fill="blue"/></svg>';(OUT/(n+'.svg')).write_text(s);png=cairosvg.svg2png(bytestring=s.encode());(OUT/(n+'.png')).write_bytes(png);return Image.open(BytesIO(png)).convert('RGBA')
path='<path d="M20 100L80 20L140 100" fill="none" stroke="red" stroke-width="16" stroke-linecap="butt" stroke-linejoin="miter" stroke-miterlimit="4"/>'
a=render('explicit-reset','<g stroke-linecap="round" stroke-linejoin="round">'+path+'</g>');b=render('standalone-miter-butt',path);checks.append({'name':'explicit-butt-miter-resets-inherited-round','passed':a.tobytes()==b.tobytes()})
for value in ['garbage','nan','inf','-1','0.5']:
 a=render('invalid-'+value.replace('.','_'),path.replace('stroke-miterlimit="4"','stroke-miterlimit="'+value+'"'));checks.append({'name':'miterlimit-'+value+'-fallback','passed':a.tobytes()==b.tobytes()});checks.append({'name':'miterlimit-'+value+'-blue-sibling','passed':a.getpixel((220,50))==(0,0,255,255)})
curve='<path d="M20 100Q80 10 140 100" fill="none" stroke="black" stroke-width="2"';tri='<path d="M0 0L12 4L0 8Z" fill="red"/>'
a=render('quadratic-marker','<defs><marker id="m" markerUnits="userSpaceOnUse" markerWidth="20" markerHeight="20" refX="0" refY="4" orient="auto" overflow="visible">'+tri+'</marker></defs>'+curve+' marker-end="url(#m)"/>');angle=degrees(atan2(90,60));b=render('quadratic-marker-reference',curve+'/><g transform="translate(140 100) rotate('+str(angle)+') translate(0 -4)">'+tri+'</g>');diff=sum(x!=y for x,y in zip(a.getdata(),b.getdata()));checks.append({'name':'quadratic-marker-follows-end-tangent','passed':diff<=2,'different_pixels':diff,'end_tangent_degrees':angle})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>描边状态与曲线切线</title><style>body{font:20px system-ui;margin:24px}img{width:600px;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>描边复位、非法斜接值及二次曲线marker</h1>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG</p><img src="'+n+'.png">' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
