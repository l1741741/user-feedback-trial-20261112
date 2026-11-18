# 嵌套视口百分比长度解析与状态恢复 — 变更说明

## 问题现象

多层 `svg`/`symbol` + `use` 组合布局时，嵌套图标里的百分比宽高、圆半径和
线条位置没有相对“属性所属元素的有效视口”解析：

- 带 `viewBox` 且发生 `preserveAspectRatio`（meet/slice）留边时，百分比
  相对留边后的用户视口计算，圆半径、矩形高度等被放大或缩小，导出比例失真；
- `use` 引用在绘制时直接 `del` 掉自身的 `x`/`y`/`viewBox`/`mask` 属性，
  `mask`/`pattern` 定义节点的 `tag` 与尺寸也被原地改写，导致同一棵解析树
  重复渲染结果不一致；
- `use` 只给 `width` 或只给 `height` 时，两个维度必须同时存在才生效，单
  维度尺寸被整体忽略；`use` 上的 `viewBox`（SVG2）被删除后完全不起作用；
- `viewBox` 尺寸作为零宽高兜底时，显式 `width="0"`/`height="0"` 的节点会
  被悄悄撑成 viewBox 大小，而不是保持为空元素。

## 修改的文件

### `cairosvg/surface.py`

- 新增 `percent_width` / `percent_height` 两个表面属性，专门表示“百分比
  长度应当相对的有效视口尺寸”，并在 `draw()` 入口保存、出口恢复，与
  `context_width`/`context_height`、字体大小等状态走同一套保存/恢复流程。
- `set_context_size()`：
  - 无 `viewBox` 时，百分比基准就是用户视口本身（与历史行为一致）；
  - 有 `viewBox` 时，百分比基准改为 viewBox 自身的宽高（viewBox 用户
    单位），这样 meet/slice 留边、非均匀缩放（`none`）和非零原点下，
    `50%` 始终表示 viewBox 的一半，符合 SVG 坐标系统规范；
  - 缩放因子为 0（零尺寸 viewBox）时不再做除零传播，退化为 1，空元素
    不绘制，兄弟节点在 `draw()` 返回后继续使用恢复后的视口。

### `cairosvg/helpers.py`

- `size()` 的百分比分支改读 `percent_width`/`percent_height`（不存在时
  回退到 `context_width`/`context_height`，兼容自定义表面子类），并对
  `None`（根表面尺寸尚未建立）与真实 `0` 做区分，统一按 0 处理而不是
  抛错或沿用上一个节点的尺寸。
- `node_format()`：viewBox 只在节点**完全没有** `width`/`height` 属性时
  提供内在尺寸兜底；显式给出的零尺寸保持为零，节点保持为空。
- `preserve_ratio()`：补充零尺寸 viewBox 的注释说明（行为上原本已用
  `> 0` 判断保护，这里明确语义）。

### `cairosvg/defs.py`

- `use()` 不再删除/改写 use 节点自身的任何属性：
  - `x`/`y` 仍然通过 `context.translate` 生效，但属性保留；
  - `width`、`height` 分别独立覆盖引用元素的对应维度，单维度也生效，
    缺失维度交给 `node_format()` 按 100%/内在尺寸/viewBox 解析；
  - use 自带的 `viewBox` 在引用元素没有 viewBox 时传给生成的嵌套 svg；
  - `mask` 保留在节点上，由 `draw()` 的既有流程处理，重复引用与重复
    渲染结果一致。
- `paint_mask()`：绘制前保存 mask 定义节点的 `tag` 和全部属性，绘制后
  原样恢复（mask 节点是跨引用、跨渲染共享缓存的）。
- `draw_pattern()`：同样用保存/恢复包住整个绘制过程（含提前返回路径），
  pattern 节点的 `tag`、`width`/`height` 等临时改写不再泄漏。

### `cairosvg/image.py`

- 内嵌 SVG 图像不再 `del` image 节点的 `x`/`y`；定位完全通过上下文
  平移完成，节点保持不可变，同一棵树重复渲染像素一致。

## 保持不变的接口与行为

- `svg2png`/`svg2pdf`/`svg2ps` 的全部参数不变：`output_width`、
  `output_height`、`scale`、`parent_width`、`parent_height`、`dpi` 等
  输出尺寸接口的结果逐字节保持（见 `cairosvg/test_api.py`，7 项全过）。
- 无 viewBox 的普通嵌套、`overflow=hidden/visible` 裁剪、`use` 引用
  普通 `g`（width/height 无视口含义）等历史行为不变。
- 上一轮试验中的 14 项像素回归在新实现下全部通过。

## 测试与对照例子

- `cairosvg/test_viewport.py`（25 个测试函数、32 条断言用例）：真实 PNG
  像素级检查中心、边界、留边与比例，探针点全部选在图形内部或外部，避开
  抗锯齿边缘，保证跨 Cairo 版本可重复。覆盖：
  - 多层嵌套下每层百分比相对各自有效视口（宽高、圆半径、stroke-width）；
  - 非零 viewBox 原点、meet/slice/none 的映射与裁剪；
  - 离开嵌套 svg 后兄弟节点恢复外层坐标系，且与兄弟排列顺序无关；
  - 零尺寸/空视口节点不泄漏尺寸；
  - `symbol`+`use` 与等价直接嵌套 svg 像素级一致、单维度 use、use 上的
    viewBox、use 引用普通 g 的历史行为；
  - use/mask/pattern/内嵌 SVG 图像在同一解析树重复渲染下幂等。
- `work/viewport_demo.py`：可直接运行的对照例子
  （`.venv/bin/python -I work/viewport_demo.py`），用多层方框、圆和标尺
  展示错位修复，自动检查真实 PNG 中的中心、边界和比例，并把渲染结果写
  到 `work/demo*.png`。
- `regression_pngs/`：demo 图与 `work/generate_regression_pngs.py` 生成的
  逐测试参考 PNG，可作为后续人工/图像比对基线。

## 运行方式

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/Users/lichao/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib:/opt/homebrew/lib"
.venv/bin/python -m pytest -q cairosvg/test_api.py cairosvg/test_viewport.py
.venv/bin/python -I work/viewport_demo.py
```
