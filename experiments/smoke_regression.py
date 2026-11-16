import pathlib, traceback, cairosvg

root = pathlib.Path('test_non_regression/fail')
svgs = sorted(root.rglob('*.svg'))
ok, failed = 0, []
for svg in svgs:
    try:
        cairosvg.svg2png(url=str(svg))
        ok += 1
    except Exception as exc:
        failed.append((str(svg), repr(exc)))
print(f"rendered {ok}/{len(svgs)} without exception")
for name, exc in failed[:15]:
    print("FAIL:", name, exc)
