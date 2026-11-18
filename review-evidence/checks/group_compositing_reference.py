from pathlib import Path
from io import BytesIO
import base64,json,sys,html
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
W,H=320,180
rects='<rect x="20" y="20" width="100" height="100" fill="red"/><rect x="70" y="20" width="100" height="100" fill="red"/>'
mask='<defs><mask id="white" maskUnits="userSpaceOnUse" x="0" y="0" width="320" height="180"><rect width="320" height="180" fill="white"/></mask></defs>'
def svg(s):return f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}">'+s+'</svg>'
def render(n,s):
 names.append(n);text=svg(s);(OUT/(n+'.svg')).write_text(text);data=cairosvg.svg2png(bytestring=text.encode());(OUT/(n+'.png')).write_bytes(data);return Image.open(BytesIO(data)).convert('RGBA')
def sample(n,im,p,expected,tol=1):
 actual=im.getpixel(p);checks.append({'name':n,'point':p,'expected':expected,'actual':actual,'passed':max(abs(a-b) for a,b in zip(actual,expected))<=tol})
a=render('group-half','<g opacity=".5">'+rects+'</g>');sample('group-nonoverlap',a,(40,60),(255,0,0,128));sample('group-overlap-once',a,(90,60),(255,0,0,128))
b=render('children-half',rects.replace('fill="red"','fill="red" opacity=".5"'));sample('children-overlap',b,(90,60),(255,0,0,192));sample('children-nonoverlap',b,(40,60),(255,0,0,128))
c=render('nested','<g opacity=".5"><g opacity=".5">'+rects+'</g></g>');sample('nested-quarter',c,(90,60),(255,0,0,64))
d=render('mask-leaf',mask+'<rect x="20" y="20" width="150" height="100" fill="red" mask="url(#white)" opacity=".5"/>');sample('mask-leaf-once',d,(90,60),(255,0,0,128))
e=render('mask-nested',mask+'<g opacity=".5"><g mask="url(#white)" opacity=".5">'+rects+'</g></g>');sample('mask-nested-quarter',e,(90,60),(255,0,0,64))
u=render('use-half','<defs><g id="icon">'+rects+'</g></defs><use href="#icon" opacity=".5"/>');sample('use-overlap-once',u,(90,60),(255,0,0,128))
empty=render('empty-transparent-sibling','<g opacity="0"><g opacity=".5">'+rects+'</g></g><g opacity=".5"/><rect x="210" y="20" width="70" height="70" fill="blue"/>');sample('transparent-content-absent',empty,(90,60),(0,0,0,0));sample('sibling-after-empty',empty,(240,50),(0,0,255,255))
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>组透明度独立像素核验</title><style>body{font:19px system-ui;margin:24px}img{width:640px;max-width:100%;border:1px solid #999;background:#eee}pre{white-space:pre-wrap}</style><h1>组整体透明与逐元素透明的区别</h1><p>组重叠区alpha应128，逐元素重叠区应192；嵌套0.5×0.5为64，白色mask不得再重复乘透明度。</p>'+''.join('<h2>'+n+'</h2><h3>SVG输入</h3><img src="'+n+'.svg"><h3>本次PNG</h3><img src="'+n+'.png"><details><summary>实际输入源码</summary><pre>'+html.escape((OUT/(n+'.svg')).read_text())+'</pre></details>' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
