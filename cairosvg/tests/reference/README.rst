# feBlend 颜色与 alpha 处理：测试素材与对照图

本目录存放可手算覆盖关系的色块/渐变输入、真实导出的 PNG，以及修复前后对照。

## 目录结构

- `../svg/`：6 个测试输入 SVG，文件头注释写明每个采样点的手算过程
  （W3C compositing-1 公式，straight RGBA）。
- `*.png`：当前（修复后）代码直接导出的 PNG，透明背景。
- `*_white-bg.png`：同一输入在 `background_color='white'` 下导出的 PNG。
- `comparison/*_before.png` / `comparison/*_after.png`：修复前/后输出，
  4 倍放大并叠在棋盘格上，便于观察透明边缘，二者输入完全相同。

## 手算基准（8 位取整前）

两个 50% 不透明矩形（红在下、蓝在上）的重叠像素，先在组内 source-over：

```
a  = 0.5 + 0.5 * (1 - 0.5) = 0.75        -> alpha 192
R  = (0.5 * 0.5) / 0.75 = 1/3            -> 85
B  = 0.5 / 0.75 = 2/3                    -> 170
```

组再以 multiply/screen 与**全透明**背景合成时，W3C 公式中
`ab = 0`，混合退化为 source-over，颜色和覆盖率都不变——这就是
“完全透明输入不得带入隐藏颜色”的可验证含义。

50% 红与不透明白底的混合：

| 模式 | 结果 (straight RGB, alpha) |
| --- | --- |
| normal / multiply / darken | (255, 128, 128), 255 |
| screen / lighten | (255, 255, 255), 255 |

## 修复点与图中可见的差异

1. **节点/组透明度只作用一次**。旧代码对带 filter 的叶子节点，先把
   `opacity` 乘进 fill/stroke，组弹出时又 `paint_with_alpha(opacity)`，
   覆盖率被平方（0.5 → 0.25）。
   - 见 `feBlend_leaf_opacity_screen_*`：修复前整体发虚、重叠区偏暗；
     修复后单色区 alpha 恰为 128、重叠区 192。
   - 见 `feBlend_modes_on_white_*`：修复前粉色块为 (255,191,191)，
     修复后为手算值 (255,127,127)；screen/lighten 正确得到纯白。
2. **透明边缘无黑框**。Cairo 的扩展混合算子按预乘 alpha 实现 W3C 公式，
   透明像素的隐藏 straight 颜色不参与计算。
   - 见 `feBlend_transparent_gradient_*`：红→透明渐变在任意覆盖率下
     straight 颜色始终是纯红 (255,0,0)，只有 alpha 下降；棋盘格上
     平滑过渡，没有深色描边。
3. **共享 filter 定义不被修改**。`blend_operator()` 只读 filter 节点，
   重复引用输出逐像素相同，见 `feBlend_shared_filter_*`。
4. **不支持的模式如实诊断**。非 SVG 1.1 模式（如 `overlay`）保持旧的
   normal 回退行为，同时发出 `RuntimeWarning`，不再静默。
