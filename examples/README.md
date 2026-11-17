# 嵌套视口对照例子与变更说明

## 运行

```bash
export DYLD_FALLBACK_LIBRARY_PATH="<本机 Cairo 动态库路径>"  # 见 NATIVE-RUNTIME.md
.venv/bin/python examples/nested_viewport_comparison.py [输出目录]
```

脚本把同一个图标（多层方框 + 圆 + 百分比标尺，viewBox 原点非零）分别用
直接嵌套 `svg`、`symbol`+`use`、`image` 嵌入三种方式组合成一张可缩放面板，
渲染为 PNG 后逐像素对比三个单元格，并检查中心、边界和比例探针。
退出码 0 表示三种组合方式结果一致。

回归测试（真实 PNG 像素级断言）：

```bash
.venv/bin/python -m pytest -q cairosvg/test_nested_viewport.py cairosvg/test_api.py
```

## 变更说明

修复 CairoSVG 嵌套视口的长度解析与状态恢复：

1. **`image` 嵌入的 SVG 文档建立独立视口**（`cairosvg/image.py`）
   修复前，嵌入文档中的百分比宽高、圆半径和线条位置都按外层画布解析，
   导出比例失真；没有尺寸或空视口的文档会沿用外层节点的宽高。现在百分比
   相对嵌入文档自己的视口（有 viewBox 时为 viewBox 用户单位，否则为文档
   自身尺寸）解析，绘制完成后恢复外层视口，外层图形继续原坐标系。

2. **`image` 嵌入 SVG 的 viewBox 映射**（`cairosvg/image.py`）
   非零 viewBox 原点之前没有被平移，preserveAspectRatio 的 letterbox
   对齐偏移用错了单位（树像素而非用户单位），两者都会导致图形错位；现在
   与嵌套 `svg` 标签的映射结果一致。

3. **空 viewBox 不再崩溃**（`cairosvg/image.py`）
   嵌入文档的 viewBox 宽或高为 0 时之前会抛出 `ZeroDivisionError`，
   现在按下述规则处理：空视口禁止渲染该元素（符合规范行为）。

4. **`marker` 内容建立标记视口**（`cairosvg/path.py`）
   修复前标记内容（如标记中 `circle`/`line` 的百分比位置与半径）按外层
   画布解析，常常被裁剪到不可见；现在相对 markerWidth/markerHeight
   建立的视口解析，绘制后恢复外层视口。

未改变的部分（回归保障）：

- 直接嵌套的 `svg` 标签与 `symbol`+`use` 的既有行为不变，二者在同等
  场景下渲染结果逐像素一致（`test_symbol_use_matches_direct_nesting`
  与对照例子）。
- `output_width` / `output_height` / `scale` 输出尺寸接口不变
  （`test_output_size_interface_with_nested_viewports`）。
- 离开嵌套 `svg` 后外层图形继续使用原坐标系
  （`test_outer_coordinate_system_restored_after_nested_svg`）。
