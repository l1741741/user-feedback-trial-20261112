颜色对照图演示
==============

``color_comparison.py`` 用 CairoSVG 实时渲染每个色块，生成可重新生成的
颜色对照图，并核对“等价写法是否得到相同像素”。

运行：

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/Users/lichao/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib:/opt/homebrew/lib"
.venv/bin/python demo/color_comparison.py
```

输出：

- ``color_comparison.png``：1024×2816 的对照图（要求 ≥ 240×160），每行
  一个写法，左侧为真实渲染色块（半透明处叠在棋盘格上），右侧为该次渲染
  采样的 RGBA 像素与是否通过；
- ``color_comparison.json``：每个色块的 SVG 源码、预期像素、实际像素、
  分组等价性与重复导出结果；
- ``samples/*.svg``：每个色块对应的独立 SVG 源文件，可直接用
  ``cairosvg samples/xxx.svg -o out.png`` 复现。

所有被测颜色都由 CairoSVG 从脚本生成的 SVG 渲染，没有使用预制位图作为
对照像素；Pillow 只负责把渲染结果排版进对照图。

覆盖内容：

1. 填充的等价写法：短/长十六进制、命名颜色、逗号/空格/百分比
   ``rgb()``、内联 ``style``；
2. 颜色自带 alpha 的等价写法：``#RRGGBBAA``、``#RGBA``、``rgba()``、
   空格+斜线 alpha、百分比 alpha；
3. 描边：等价写法，以及颜色 alpha 与 ``stroke-opacity`` 各乘一次；
4. 渐变：``stop-color`` 等价写法、``stop-opacity`` 与色标 alpha 叠加、
   内联 ``style`` 中的 ``stop-color``；
5. 半透明重叠：单层 / 两层 / 三层，alpha 依次为 50% / 75% / 87.5%；
6. 透明度组合：颜色 alpha × ``fill-opacity`` × 组 ``opacity``；
7. ``opacity`` 只应用一次：普通元素、带 ``mask``、带 ``filter``、
   ``<use>``；
8. 无效输入隔离：无效颜色/透明度只影响自身，兄弟图形正常；
9. 重复导出与重复引用：同一文档两次渲染字节一致；同一 pattern 两次引用
   透明度不累乘。

脚本在任一检查失败时以状态码 1 退出，因此也可以直接当作回归检查使用。
``cairosvg/test_color_chart.py`` 在 pytest 中调用同一套检查。

字体：图表优先使用系统中文字体（macOS/Windows/Linux 常见路径）；找不到
中文字体时自动改用英文标签，保证在任何机器上都能重新生成可读的 PNG。
