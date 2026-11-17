"""Run unmodified model demo with output capture; measure black glyphs, excluding coloured guides."""
from pathlib import Path
from io import BytesIO
import json,sys,runpy,traceback
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True)
source=Path(cairosvg.__file__).resolve().parent.parent/'demo/text_whitespace_sign.py'
ns=runpy.run_path(str(source),run_name='evaluated_demo');main=ns['main'];g=main.__globals__;g['OUTPUT']=str(OUT/'model-demo.png');original=g['svg2png']
def capture(*args,**kw):
    text=kw.get('bytestring',args[0] if args else '')
    (OUT/'model-demo.svg').write_bytes(text.encode() if isinstance(text,str) else text)
    return original(*args,**kw)
g['svg2png']=capture
main()
font=g['FONT_FAMILY'];reference=f'<svg xmlns="http://www.w3.org/2000/svg" width="520" height="360"><rect width="520" height="360" fill="white"/><text x="30" y="130" font-family="{font}" font-size="28">SIGN SHOP No 319</text><text x="30" y="300" font-family="{font}" font-size="28" xml:space="preserve">SIGN  SHOP    No   319  </text></svg>'
(OUT/'explicit-reference.svg').write_text(reference);data=cairosvg.svg2png(bytestring=reference.encode(),background_color='white');(OUT/'explicit-reference.png').write_bytes(data)
model=Image.open(OUT/'model-demo.png').convert('RGBA');ref=Image.open(BytesIO(data)).convert('RGBA')
def black_bbox(im,ymin,ymax):
    pts=[(x,y) for y in range(ymin,ymax) for x in range(im.width) if max(im.getpixel((x,y))[:3])<60 and im.getpixel((x,y))[3]>200]
    assert pts,'No black glyphs';return [min(x for x,y in pts),min(y for x,y in pts),max(x for x,y in pts)+1,max(y for x,y in pts)+1]
md=black_bbox(model,100,133);mp=black_bbox(model,270,303);rd=black_bbox(ref,100,133);rp=black_bbox(ref,270,303)
checks=[{'name':'default-panel-glyphs-match-reference','actual':md,'expected':rd,'passed':max(abs(a-b) for a,b in zip(md,rd))<=2},
        {'name':'preserve-panel-glyphs-match-reference','actual':mp,'expected':rp,'passed':max(abs(a-b) for a,b in zip(mp,rp))<=2},
        {'name':'preserve-panel-is-wider-than-default','default_width':md[2]-md[0],'preserve_width':mp[2]-mp[0],'expected_preserve_width':rp[2]-rp[0],'passed':mp[2]-mp[0]>md[2]-md[0]+20}]
r={'source':cairosvg.__file__,'evaluated_demo':str(source),'tests':len(checks),'passed':sum(x['passed'] for x in checks),'failed':sum(not x['passed'] for x in checks),'checks':checks,'font':font}
(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>文本空白演示实测</title><style>body{font:19px system-ui;margin:24px;background:#eee}section{display:inline-block;background:white;margin:10px;padding:15px;vertical-align:top}img{width:650px;max-width:100%;border:1px solid #aaa;image-rendering:pixelated}pre{white-space:pre-wrap}</style><h1>preserve面板的实际字距</h1><p>左侧为原演示本次运行结果，右侧为等价显式文本参考。独立测量只统计黑色字形，排除红色基准线和蓝色边界框。</p><section><h2>原演示实际PNG</h2><img src="model-demo.png"><p>原演示实际传入的SVG</p><img src="model-demo.svg"></section><section><h2>显式文本参考PNG</h2><img src="explicit-reference.png"><p>参考SVG</p><img src="explicit-reference.svg"></section><pre>'+json.dumps(r,ensure_ascii=False,indent=2)+'</pre>');sys.exit(bool(r['failed']))
