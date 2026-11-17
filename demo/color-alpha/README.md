# CSS 颜色 Alpha 本地演示

本目录提供两个**可重新生成**的本地演示，所有图像都由仓库内的 CairoSVG
加本机 Cairo 动态库现场渲染，不包含任何预制位图。

## 环境

按仓库根目录 `NATIVE-RUNTIME.md` 设置 Cairo 库路径后使用 `.venv`：

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/Users/lichao/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib:/opt/homebrew/lib"
```

## 1. 颜色对照图（600×880）

```bash
.venv/bin/python -I demo/color-alpha/generate_color_chart.py
```

产物（默认写入 `demo/color-alpha/output/`）：

- `color_chart.svg`：脚本生成的源 SVG；
- `color_chart.png`：CairoSVG 渲染出的对照图；
- `color_chart_report.txt`：逐行像素比对报告。

覆盖内容：不透明与半透明十六进制（`#RGBA` / `#RRGGBBAA`）、传统逗号
`rgb()`/`rgba()`、空格与斜线 alpha、百分比通道、表现属性与内联
`style`、填充、描边、渐变停点、`fill-opacity` / `stop-opacity` /
组透明度与颜色 alpha 的单次组合、半透明重叠（source-over）、无效颜色
回退，以及重复导出字节一致性。

同一组等价写法要求像素完全相同；十六进制 alpha 只有 8 位量化精度，
与浮点函数写法跨组比较时允许 2 个量化级的差异。任何检查失败时脚本以
非零状态码退出。

## 2. 批量 SVG 图标转 PNG

```bash
.venv/bin/python -I demo/color-alpha/batch_convert_icons.py
```

无参数时使用 `icons/` 下自动写入的五个样例（含一个故意使用不受支持的
`hsl()` 的文件），把每个 SVG 单独渲染成 96×96 PNG 输出到
`output/icons/`。单个文件出错只记录失败，不影响其余文件；不受支持的
颜色走不透明黑色回退，图标仍可正常导出。也可显式传入输入/输出目录：

```bash
.venv/bin/python -I demo/color-alpha/batch_convert_icons.py path/to/svg path/to/png
```

## 3. 回归测试

```bash
.venv/bin/python -m pytest -q cairosvg/test_colors.py cairosvg/test_api.py
```

`test_colors.py` 直接读回渲染 PNG 的 RGBA 像素做断言，并包含 hypothesis
参数化等价性检查，不依赖任何参考图片。
