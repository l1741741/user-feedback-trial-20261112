本机已验证的运行命令：

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/Users/lichao/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib:/opt/homebrew/lib"
.venv/bin/python -m pytest -q cairosvg/test_api.py
```

需要可用的Cairo共享库，其他机器应设置对应本机库路径。

椭圆弧（路径 A/a 命令）的几何/像素回归测试与离线视觉验证：

```bash
.venv/bin/python -m pytest -q cairosvg/test_path_arcs.py   # 弧线几何与像素测试
.venv/bin/python demo/arc_visual_check.py                  # 离线演示，PNG 输出到 demo/output/
```

