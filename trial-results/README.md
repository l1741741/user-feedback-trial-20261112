# viewBox / preserveAspectRatio 修复验证结果

本目录中的图片均由修复后的 CairoSVG 实际渲染生成（PNG 位图，非公式推导）。
输入图标固定为：viewBox `0 0 100 100`，四角各 20×20 的红/绿/蓝/黄色块，
中心为黑色十字（竖条 `x=45..55`，横条 `y=45..55`）。

复现命令（见 NATIVE-RUNTIME.md 的库路径）：

```bash
.venv/bin/python -m pytest -q cairosvg/test_viewbox.py cairosvg/test_api.py
```

## 文件说明

- `00_before_after_comparison.png` — 同一批输入在修复前（左列）与修复后
  （右列）的对比，直观展示三个缺陷：
  1. 非零 viewBox 起点 + meet：修复前裁切窗口随起点漂移，左下角标被错误裁掉；
  2. 非零 viewBox 起点 + slice：修复前内容偏移且越界绘制到错误边缘；
  3. 嵌套 slice（viewBox 起点为负）：修复前图标越过自身视口，覆盖到两侧
     橙色相邻图层；修复后严格限制在中间有效视口内。
- `01_matrix_zero_origin.png` — 起点 `(0,0)` 时，7 种对齐（xMin/xMid/xMax ×
  meet/slice，以及 none）在横向 200×100、方形 100×100、纵向 100×200 三种
  容器中的完整矩阵。meet 整体可见并留边；slice 填满视口、超出的角被裁切；
  none 两轴独立拉伸、四角均保留。
- `02_matrix_nonzero_origin.png` — 同样矩阵，viewBox 改为 `10 20 100 100`，
  验证非零起点参与映射，裁切窗口仍精确等于容器边缘。
- `03_nested_slice_contained.png` — 嵌套 SVG 用 slice 映射负起点 viewBox，
  两侧橙色相邻图层保持完整，图标内容不越界。
- `04_slice_scale2.png` / `05_slice_output_400x200.png` — `scale=2` 与
  `output_width/output_height=400×200` 产生相同几何，仅像素密度不同。
- `06_after_repeated_calls.png` — 连续 3 次 slice 渲染后再渲染普通 meet
  文档，结果与单独渲染完全一致，证明绘制状态/裁切不跨文档泄漏。
- `07_embedded_image_slice.png` — 通过 `<image>` 内嵌的 SVG 文档按 slice
  映射，左侧橙色条（相邻图层）不被覆盖。

## 退化视框的错误契约

`viewBox` 缺数字（如 `1 2 3`）或宽高不严格为正（`0 0 0 100`、`0 0 100 0`、
负值等）统一抛出 `ValueError`；根级渲染在异常时结束 Cairo surface，嵌套节点
异常时由 `draw()` 的 try/finally 平衡 context 状态，不影响后续文档渲染。
对应测试见 `cairosvg/test_viewbox.py` 中的 `test_degenerate_*`。
