# textPath 沿路径文字布局 — 支持说明

## 背景

CairoSVG 的 `<textPath>` 通过 `textPath` 把曲线标签沿路径轮廓排布。原实现在
路径几何解析、`startOffset` 百分比、文本锚点和状态隔离上存在多处缺陷，导致
导出时起始偏移错误、反向曲线方向错误、短路径后文字出现在错误位置。

本次修复只涉及 `cairosvg/text.py`、`cairosvg/surface.py` 和
`cairosvg/defs.py`，不改变普通（非路径）文本的布局。

## 修复内容

### 1. 路径几何解析（`text.py`）

- **闭合曲线**：cairo 扁平路径中的 `PATH_CLOSE_PATH` 原本被忽略，闭合段
  （回到子路径起点的那段）不计入长度。新增 `subpath_segments()`，把闭合段
  物化为显式线段，并去掉 cairo 在闭合后输出的冗余回到起点的 move-to。
- **多段子路径**：各子路径独立计算长度与切线，子路径之间的间隙不属于路径，
  文字走完一段后从下一段的起点继续，绝不跨过间隙插值。
- **零长度路径**：只有一个 move-to 点的子路径仍有确定位置；总长度为 0 的
  引用路径按 SVG 1.1 §10.13.1 禁用其文字内容。
- **切线方向**：`point_following_path()` 现在返回 `(x, y, angle)`，angle
  取自当前线段方向，因此反向路径上的字形按路径方向旋转（约 180°），文字
  前进方向与路径方向一致。
- **曲线点序**：扁平后的 `CURVE_TO` 按 cairo 的 p1、p2、p3 顺序取点。

### 2. startOffset 与文本锚点（`text.py`）

- **百分比基准**：`startOffset="50%"` 按引用路径长度解析，而不是按视口
  对角线（原来错误地走 `size()` 的视口百分比分支）。新增
  `parse_start_offset()`，数值按用户单位、百分比按路径长度，并把超出
  `[0, 路径长度]` 的值钳制到边界。
- **文本锚点**：`text-anchor` 沿路径方向作用。路径上的锚点用各字符
  advance 之和（实际占用长度）计算，区别于普通文本用墨迹宽度 + x-bearing
  的算法；锚点只在带 href 的 `textPath` 根节点应用一次，外层 `text` 和内层
  `tspan` 不再重复累加。`text-anchor` 通过祖先链继承。
- **字符自身偏移**：`dx` 沿路径方向推进当前位置（反向路径上正值向路径
  方向移动），`dy` 在旋转后的字形坐标系内垂直平移。

### 3. 形状引用与几何稳定性（`defs.py`、`text.py`）

- 原来只有标签名含 "path" 的元素才注册进 `surface.paths`，所以
  `polyline`、`polygon`、`circle`、`ellipse`、`rect`、`line` 即使带 id 也
  无法作为 textPath 引用（文字堆在原点）。现在所有形状标签都注册为可引用
  路径。
- `build_text_path()` 在 `save()`/`new_path()` 的临时上下文里绘制引用节点
  并复制扁平路径，随后恢复。同一路径被多次引用、或同时作为可见形状描边时，
  `surface.paths` 中保存的节点几何不会被修改，多次渲染结果逐字节一致。
- 路径节点自身或祖先 `text` 上的 `transform` 照常作用（构建路径时上下文已
  带该变换）。

### 4. 状态隔离（`surface.py`、`text.py`）

- 路径过短时，超出路径末端的字形被跳过（不再回退到原点绘制）。
- `draw()` 为 `textPath` 子树保存并恢复 `cursor_d_position` 与
  `text_path_width`；零长度/缺失路径禁用整段内容，且不向后续的普通兄弟
  文本泄漏任何偏移。
- 嵌套在 textPath 内的 `tspan` 通过 `surface.active_text_path` 复用同一条
  扁平路径，连续排布而不重置位置。

## 测试

新增 `cairosvg/test_text_path.py`（24 个用例），用 RecordingSurface 拦截
cairo 的 `text_path` 调用，读回每个字形的实际原点、矩阵和包围盒，检查：

- 基本排布与字形顺序；
- 数值/百分比 startOffset、越界钳制；
- middle/end 锚点与 startOffset 的组合；
- 反向路径方向、dx/dy 沿路径方向；
- 多子路径不跨间隙、闭合段计入长度、零长度与缺失 href 禁用内容；
- 短路径/零长度路径后兄弟普通文本位置不变；
- 嵌套 tspan 连续、polyline/circle 形状引用；
- 重复引用几何稳定、可见路径几何不变、transform 生效。

字体栅格化存在 hinting 差异，坐标断言使用 1.5 用户单位的容差（远小于一个
字形 advance，约 10–14 单位）。

运行回归（需要可用的 cairo 动态库）：

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/path/to/cairo/lib:/opt/homebrew/lib"
.venv/bin/python -m pytest -q cairosvg/
```

## 离线演示

`work/demo/` 下提供完全离线的演示，只使用本机 Verdana 字体：

- `text_path_demo.svg`：带刻度圆弧 + 折线路径，覆盖上述全部场景；
- `render_demo.py`：用项目虚拟环境把 SVG 渲染成 PNG（以 `python -I`
  隔离运行，避免工作目录中的同名模块被导入）；
- `text_path_demo.png`：渲染结果。

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/path/to/cairo/lib:/opt/homebrew/lib"
.venv/bin/python -I work/demo/render_demo.py
```
