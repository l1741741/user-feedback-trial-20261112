from pathlib import Path
import io,json,sys,html,warnings,copy
import cairosvg
from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface
from PIL import Image
OUT=Path(sys.argv[-1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
def svg(body):return '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160">'+body+'</svg>'
def render(name,body):
 text=svg(body);(OUT/(name+'.svg')).write_text(text);data=cairosvg.svg2png(bytestring=text.encode());(OUT/(name+'.png')).write_bytes(data);names.append(name);return Image.open(io.BytesIO(data)).convert('RGBA')
def expected(mode,back,source,ab,asrc):
 blend={'normal':lambda b,s:s,'multiply':lambda b,s:b*s,'screen':lambda b,s:b+s-b*s,'darken':min,'lighten':max}[mode];alpha=asrc+ab*(1-asrc)
 rgb=[((1-asrc)*ab*b+(1-ab)*asrc*s+ab*asrc*blend(b,s))/alpha if alpha else 0 for b,s in zip(back,source)]
 return tuple(round(255*x) for x in [*rgb,alpha])
def pixel(name,im,point,want,tol=3):
 actual=im.getpixel(point);checks.append({'name':name,'point':point,'actual':actual,'expected':want,'tolerance':tol,'passed':all(abs(a-b)<=tol for a,b in zip(actual,want))})
for mode in ['normal','multiply','screen','darken','lighten']:
 defs='<defs><filter id="f"><feBlend mode="'+mode+'"/></filter></defs>'
 body=defs+'<rect x="10" y="20" width="100" height="100" fill="blue" opacity=".5"/><rect x="60" y="20" width="100" height="100" fill="red" opacity=".5" filter="url(#f)"/>'
 im=render(mode+'-over-half-blue',body)
 for label,point,want in [('overlap',(80,60),expected(mode,(0,0,1),(1,0,0),.5,.5)),('source-only',(140,60),(255,0,0,128)),('back-only',(30,60),(0,0,255,128)),('outside',(220,140),(0,0,0,0))]:pixel(mode+'-'+label,im,point,want)
 ref=render(mode+'-group-reference',defs+'<rect x="10" y="20" width="100" height="100" fill="blue" opacity=".5"/><g opacity=".5" filter="url(#f)"><rect x="60" y="20" width="100" height="100" fill="red"/></g>');checks.append({'name':mode+'-leaf-group-equivalent','passed':im.tobytes()==ref.tobytes()})
for mode in ['multiply','screen']:
 defs='<defs><filter id="f"><feBlend mode="'+mode+'"/></filter><linearGradient id="g"><stop stop-color="red"/><stop offset="1" stop-color="red" stop-opacity="0"/></linearGradient></defs>'
 im=render(mode+'-gradient',defs+'<rect x="20" y="20" width="100" height="80" fill="url(#g)" opacity=".5" filter="url(#f)"/>')
 for x in [20,70,118]:
  p=im.getpixel((x,60));checks.append({'name':mode+'-gradient-'+str(x),'actual':p,'passed':p[0]>250 and p[1]==p[2]==0 and abs(p[3]-(1-(x+.5-20)/100)*.5*255)<=2})
 defs='<defs><filter id="f"><feBlend mode="'+mode+'"/></filter></defs>'
 im=render(mode+'-zero-source',defs+'<rect width="240" height="160" fill="blue" opacity=".5"/><rect width="100" height="100" fill="red" opacity="0" filter="url(#f)"/>');pixel(mode+'-zero-source-no-hidden-colour',im,(30,30),(0,0,255,128))
body='<defs><filter id="f"><feBlend mode="screen"/></filter></defs><rect x="20" y="20" width="50" height="50" fill="red" opacity=".5" filter="url(#f)"/><rect x="100" y="20" width="50" height="50" fill="red" opacity=".5" filter="url(#f)"/>'
im=render('shared-filter',body);pixel('shared-first',im,(40,40),(255,0,0,128));pixel('shared-second',im,(120,40),(255,0,0,128))
tree=Tree(bytestring=svg(body).encode());fnode=tree.children[0].children[0]
def snap(n):return {'attrs':dict(n),'tag':n.tag,'children':[snap(x) for x in n.children]}
before=snap(fnode);outputs=[]
for i in range(2):
 stream=io.BytesIO();s=PNGSurface(tree,stream,96);s.finish();outputs.append(stream.getvalue())
checks.append({'name':'shared-definition-unchanged-repeat','passed':snap(fnode)==before and outputs[0]==outputs[1]})
with warnings.catch_warnings(record=True) as caught:
 warnings.simplefilter('always');a=render('unsupported-normal-fallback','<defs><filter id="f"><feBlend mode="overlay"/></filter></defs><rect width="80" height="80" fill="red" opacity=".5" filter="url(#f)"/>')
b=render('unfiltered-reference','<rect width="80" height="80" fill="red" opacity=".5"/>');checks.append({'name':'unsupported-mode-explicit-warning-normal-output','passed':a.tobytes()==b.tobytes() and any('not supported' in str(w.message) for w in caught)})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(x['passed'] for x in checks),'failed':sum(not x['passed'] for x in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>半透明混合与手算结果</title><style>body{font:20px system-ui;margin:24px}img{width:600px;max-width:100%;border:1px solid #aaa;background:repeating-conic-gradient(#eee 0% 25%,#fff 0% 50%) 50%/20px 20px}pre{white-space:pre-wrap}</style><h1>五种混合模式及透明度对照</h1><p>背景蓝色50%，前景红色50%；重叠区alpha约192。叶子与组参考应一致，透明边缘不发黑。</p>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG</p><img src="'+n+'.png">' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
