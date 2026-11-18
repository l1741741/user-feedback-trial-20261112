# textLength 与字距处理说明

本文档说明 CairoSVG 工作副本中 `textLength` / `lengthAdjust` 及
`letter-spacing` / `word-spacing` 的实现范围、边界情况与误差来源。
对应实现见 `cairosvg/text.py`，测试见 `cairosvg/test_text_length.py`。

## 支持范围

- `textLength` 可写在 `text` / `tspan` 元素上，作用于该元素**自身的文本块**
  （不含子 tspan 的文本）。嵌套 tspan 各自声明各自的 `textLength`，结果稳定。
- 支持带单位的长度（`px`、`pt`、`em` 等）及百分比（相对视口宽度）。
- `lengthAdjust="spacing"`（默认）：只调整字符间距，把多余/不足的宽度
  均摊到每个字符间隙；字形不变。
- `lengthAdjust="spacingAndGlyphs"`：字形、字进距与声明的间距按同一比例
  水平缩放，使整行精确达到目标宽度；垂直方向不缩放。
- `text-anchor="start|middle|end"` 与 `textLength` 组合时，按**目标宽度**
  对齐（start 从 x 起铺满目标宽，middle 居中，end 右对齐到 x）。
- `letter-spacing` / `word-spacing` 在同一 `text` 元素的子节点
  （`tspan`、`a`）之间连续生效：跨 tspan 边界的首字符同样与前一个字符
  保持声明间距，不再出现间断。`word-spacing` 在每个空格字符后追加进距。
- `textLength` 与 `letter-spacing`/`word-spacing` 同时声明时，先按声明
  间距计算自然宽度，再调整至目标宽度（目标宽度优先）。

## 边界情况的定义（稳定结果）

- **单字符**：`spacing` 模式没有字符间隙可调，保持自然宽度（渲染与未声明
  `textLength` 完全一致）；`spacingAndGlyphs` 模式缩放该唯一字形。
- **空内容**：不绘制任何内容，忽略 `textLength`，不报错。
- **非法长度**：无法解析、负数或零的长度被忽略，保持自然布局。
- **显式位置列表**（`x`/`dx` 等列表）：位置列表照常生效，间距调整叠加在
  字符间隙上；首字符保持声明位置。重复渲染结果逐字节一致。
- **后续文本不受影响**：缩放只包在逐字符的 save/restore 内，间距是局部
  变量，连续性标志在每个 `text` 元素开始时重置；同一文档中后面的普通
  文本不继承前一标签的缩放或间距。
- **未声明 `textLength` 的文本**：布局与改动前逐字节一致（已用一批不含
  新特性的样张对比验证）。

## 已知限制

- `textPath` 上的 `textLength` 只应用间距调整，不缩放字形。
- 外层 `text` 上的 `textLength` 不会汇总子 tspan 的文本（各文本块独立）。
- 逐字符绘制，不含字距微调（kerning）与连字；这与 CairoSVG 既有的玩具
  字体（toy font）绘制方式一致。

## 误差来源（测试容差的依据）

- **墨迹范围 ≠ 进距范围**：目标宽度按进距（advance）对齐，而墨迹
  （实际像素）首尾各内缩一个字形的侧轴承（side bearing）。
  `spacingAndGlyphs` 模式下侧轴承同样被缩放，窄字形（如 `i`）在大倍率
  下偏移明显——测试用同一字体后端实测度量来预测墨迹范围，而非硬编码。
- **提示（hinting）取整**：位置按设备像素取整，误差 ≤1px。
- **抗锯齿**：边缘像素向外扩散约 1px。
- **字体替换**：`sans-serif` 等通用族名由 Cairo 玩具字体后端映射到具体
  字体，不同机器字形宽度不同；测试全部基于实测度量，不假设具体字体。

运行测试：

```bash
export DYLD_FALLBACK_LIBRARY_PATH="<本机 Cairo 库路径>"
.venv/bin/python -m pytest -q cairosvg/test_text_length.py
```
