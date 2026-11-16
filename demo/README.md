# 嵌套视口百分比演示

本目录演示嵌套 `svg` 与 `symbol`（经 `use` 引用）中百分比长度和视口状态的
解析。输入是 `assets/viewport_percent.svg`，脚本只按路径读取该文件。

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/Users/lichao/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib:/opt/homebrew/lib"
.venv/bin/python demo/render_demo.py
```

可用参数：`--svg 输入路径`、`--out 输出目录`、`--zoom 放大倍数`（默认 4）。

## 输出（`output/`）

- `viewport_percent.png`：完整 720×700 画面。
- `crop_1_nested_vs_use.png`：同一视口的三种写法。
- `crop_2_three_levels.png`：三层视口（300×190、40×40、20×20）。
- `crop_3_symbol_reuse.png`：同一 `symbol` 以两种尺寸复用。
- `crop_4_sibling_percent.png`：嵌套视口之后的百分比兄弟元素。
- `crop_5_ruler.png`：百分比标尺（每 10% 一个刻度）。
- `crop_6_viewbox_origin.png`：非零 `viewBox` 原点。

## 看图要点

蓝色方块是“50% 宽高”的矩形，它下面压着“5 个用户单位宽高”的红色矩形。
百分比解析正确时两者完全重合，只能看到蓝色；若百分比错误地按外层画布计算，
红色会在右侧或下方露出来。

1. **nested svg, meet**：140×70 视口、10×10 viewBox，等比缩放后左右留白，
   50% 仍是 5 个用户单位。
2. **symbol via use**：与 1 完全一致，说明 `symbol` 经 `use` 引用与直接嵌套
   的同等场景结果相同。
3. **nested svg, none**：两轴独立拉伸，50% 仍按 viewBox 的 5 个单位解析，
   只是单位长度不同（方框变宽）。
4. **sibling after svg**：离开嵌套视口后，绿色矩形按 720×700 外层画布解析
   （20%×10% = 144×70），而不是按 140×70 的嵌套视口。
5. **三层视口**：每层子元素的百分比相对其所属层的 viewBox，圆心、十字线、
   刻度都对齐。
6. **一个 symbol 两种尺寸**：两次引用互不影响，后一次不会沿用前一次的尺寸。
7. **百分比标尺**：刻度位于各自 viewBox 的 10%、20%…处，与圆心对齐。
8. **viewBox 原点 20 30**：50% 是从 (20, 30) 起的 5 个单位，而不是从 (0, 0)
   起；原点只做平移。

回归测试见 `cairosvg/test_viewport.py`，说明见仓库根目录
`NESTED-VIEWPORT.md`。
