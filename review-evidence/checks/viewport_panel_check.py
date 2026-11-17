from pathlib import Path
from io import BytesIO
import json,sys,subprocess,base64
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True)
source=Path(cairosvg.__file__).resolve().parent.parent/'examples/nested_viewport_comparison.py'
run=subprocess.run([sys.executable,str(source),str(OUT)],capture_output=True,text=True,timeout=40)
(OUT/'model-example.log').write_text(run.stdout+run.stderr);print(run.stdout);assert run.returncode==0,run.stderr
im=Image.open(OUT/'panel.png').convert('RGBA');checks=[]
for left in [40,380,720]:
 for label,x,y,wanted in [('red',20,20,(255,0,0,255)),('blue',150,150,(0,0,255,255)),('clear',90,90,(255,255,255,255))]:
  actual=im.getpixel((left+x,100+y));checks.append({'name':f'panel-{left}-{label}','actual':actual,'expected':wanted,'passed':actual==wanted})
a=im.crop((40,100,340,400)).tobytes()
for left in [380,720]:checks.append({'name':f'equivalent-cell-{left}','passed':im.crop((left,100,left+300,400)).tobytes()==a})
inner='<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="10 20 100 50"><rect x="10" y="20" width="50%" height="100%" fill="red"/></svg>'
u='data:image/svg+xml;base64,'+base64.b64encode(inner.encode()).decode();text=f'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="180"><image x="10" y="20" width="200" height="100" href="{u}"/><rect x="240" y="20" width="40" height="80" fill="blue"/></svg>'
(OUT/'independent-image.svg').write_text(text);data=cairosvg.svg2png(bytestring=text.encode());(OUT/'independent-image.png').write_bytes(data);im=Image.open(BytesIO(data)).convert('RGBA')
for label,pt,wanted in [('inner-left',(30,50),(255,0,0,255)),('inner-right',(170,50),(0,0,0,0)),('outer-sibling',(260,50),(0,0,255,255)),('outside-image',(5,50),(0,0,0,0))]:
 actual=im.getpixel(pt);checks.append({'name':label,'actual':actual,'expected':wanted,'passed':actual==wanted})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(x['passed'] for x in checks),'failed':sum(not x['passed'] for x in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>视口百分比核验</title><style>body{font:18px system-ui;margin:25px}img{width:900px;max-width:100%;border:1px solid #aaa;image-rendering:pixelated}pre{white-space:pre-wrap}</style><h1>同一图标的三种嵌套入口</h1><p>实际SVG输入</p><img src="panel.svg"><p>本次PNG</p><img src="panel.png"><h2>独立非零原点与外层兄弟</h2><p>输入SVG</p><img src="independent-image.svg"><p>本次PNG</p><img src="independent-image.png"><pre>'+json.dumps(r,ensure_ascii=False,indent=2)+'</pre>');sys.exit(bool(r['failed']))
