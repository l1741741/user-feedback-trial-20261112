# 文本 chunk 布局与 text-anchor 修复说明

## 修复的问题

旧的 `cairosvg/text.py` 为每个 `text`/`tspan` 节点分别绘制，并依赖
`surface.cursor_position`、`surface.cursor_d_position` 等跨节点可变状态，
且把 `text-anchor` 的对齐量按整段文本一次性计算。由此产生：

1. **多段文本中途改变 `x` 后整行仍被一起居中。** SVG 1.1 中，每次出现绝对
   `x`/`y`（节点上的 `x="x1 x2 …"` 列表或子 `tspan` 的 `x`/`y` 覆盖）都应
   开始一个新的 *text chunk*，`text-anchor` 只作用于当前 chunk，而不是整行。
2. **嵌套 `tspan` 的字符偏移泄漏到下一行。** 内层 `tspan` 的 `dx`/`dy`
   累积在共享的 `cursor_d_position` 上，后续设置了绝对 `x` 的兄弟行没有被
   正确复位。
3. **多个独立 `text` 节点、不同锚点顺序绘制时结果依赖之前的文字状态。**
   对齐量与游标在节点间残留，先画的 `end` 文本会移动后画的 `middle` 文本。

## 实现方式（不插入空格、不使用预制位图）

`cairosvg/text.py` 为普通文本（不含 `textPath`）引入 chunk 布局引擎：

- `_layout_text()` 按文档顺序递归遍历整个文本子树，为每个字形记录绝对位置、
  相对偏移、逐字符旋转和所属字体节点。
- **chunk 边界**：任一字形拿到绝对 `x` 或 `y` 即开启新 chunk。
- **`dx`/`dy` 语义**：相对偏移在子树内累积；绝对 `x` 只清零同轴向的 `dx`，
  绝对 `y` 只清零 `dy`，两个轴向相互独立，父子坐标不会重复叠加。
- **逐字符 `rotate`**：沿用 `rotate="a b …"` 列表，最后一个值对后续字形
  持续生效。
- **`letter-spacing`**：仅在整段文本除第一个字形外的字形前插入额外前进量。
- **锚点**：每个 chunk 依据其首字形的 `text-anchor` 独立计算 `x_align`
  （`middle` 为负的半宽、`end` 为负的整宽、`start` 为 0）。
- 字形仍通过 cairo 的 `text_path()`/`show_text()` 按真实字体轮廓逐字符绘制，
  不插入空格凑位，也不使用任何预制位图。
- 整棵文本子树在根节点一次性排版，结果按**来源节点分组**保存在 surface 上
  （`text_layout`）。随后 `surface.draw` 遍历到每个 `text`/`tspan`/`a` 节点
  时，只绘制属于该节点的字形路径，因此每个节点仍用**自己的 fill / stroke /
  fill-opacity** 上色——嵌套 span 的局部颜色、透明度、字号/字重不会丢失。
  共享布局在根节点 `draw` 结束时释放，独立 `text` 节点之间不共享文字游标
  状态，同一 Tree 也可被多个 surface 重复渲染。
- `textPath` 仍走原先的路径引擎（`_draw_text_on_path_node`），行为保持不变；
  其根节点通过 `surface.text_path_root` 阻止子节点被二次遍历。

## 字体条件

- 测试与演示使用 cairo 的 toy font API，字体族为 **Helvetica**
  （macOS 自带 `/System/Library/Fonts/Helvetica.ttc`，cairo 经 fontconfig /
  CoreText 解析）。
- 运行需要可用的 Cairo 动态库。本机命令（见 `NATIVE-RUNTIME.md`）：

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/Users/lichao/.cache/codex-runtime/codex-primary-runtime/dependencies/native/poppler/poppler/lib:/opt/homebrew/lib"
.venv/bin/python -m pytest -q cairosvg/
```

- 像素断言以字形墨迹的边缘/中心为基准，留有 3px 的抗锯齿容差
  （`test_text_layout.py` 中的 `TOL`），不依赖具体的亚像素渲染结果。
- 若机器缺少 Helvetica，可把测试与演示里的 `FONT` 换成本机已安装的任一
  等宽度量稳定的 sans-serif（如 Arial）；布局断言基于 cairo 实时测量的
  `text_extents`，不硬编码字形宽度。

## 本地图片演示

`work/make_demo.py` 生成带左右基线与中心参考线的四格演示图：

```bash
.venv/bin/python -I work/make_demo.py
# 输出 work/demo/text_chunks_demo.png
```

四个面板分别展示：

1. `x="160 560"` 配合 `text-anchor="middle"`，两个 chunk 各自居中于参考线；
2. 嵌套 `tspan dx="15"` 只移动本行的 B，下一行 `DEF` 仍从绝对 `x=80` 开始；
3. 三个独立 `text` 节点以 end/middle/start 锚点绘制，均对齐到同一条
   垂直参考线，互不影响；
4. 逐字符 `rotate` 与 `letter-spacing` 的语义。

辅助脚本（均以 `-I` 隔离模式运行，只读取当前工程）：

- `work/repro.py`：四个缺陷场景的像素级回归检查；
- `work/semantics.py`：旋转、字间距、描边、空文本；
- `work/state_tests.py`：重复导出确定性、绘制顺序无关性、跨节点偏移隔离、
  textPath。

## 回归测试

新增 `cairosvg/test_text_layout.py`（16 个用例），覆盖嵌套 span、空文本、
重复导出、逐字符旋转、字间距、锚点继承、Tree 复用、描边文本、根节点坐标
下沉到子 tspan、`<a>` 包裹、逐 tspan 字号/字重以及逐 tspan 填充色：

```bash
.venv/bin/python -m pytest -q cairosvg/
# 23 passed
```

对 `test_non_regression/fail` 下全部 147 个含文本的 SVG 夹具做了改前/改后
渲染对比：所有夹具的字形墨迹质心与包围盒位移均不超过 2px（差异仅来自字形
路径化后的抗锯齿），无布局回归。`fill="none"` 描边文本与嵌套 `textPath`
的渲染像素与改前完全一致。

## 实现过程中额外修正的相关缺陷

- **根节点 `x`/`y`/`dx`/`dy` 对仅含子 tspan 的文本失效。** 这些属性不被
  继承，旧引擎靠共享游标兜底；新引擎在根节点自身无文本时，用其首个坐标
  为布局状态播种，避免首字形落到 (0, 0)。
- **子 tspan 的 `font-size`/`font-weight` 被忽略。** 内部子树遍历期间
  surface 级字号仍属于祖先节点，`_select_font` 改为按当前节点自身的
  `font-size` 解析，保证逐字形字号、字重生效。
- **子 tspan 的局部 fill / fill-opacity / stroke 丢失。** 早期版本把所有
  字形画进同一条路径、只用根节点颜色填充；改为按来源节点分组绘制，使每个
  节点保留自己的上色与描边，且字形不会被重复绘制。
- **重复渲染同一 Tree 时文字消失。** 共享布局 `text_layout` /
  `text_layout_root` 在根节点 `draw` 结束时统一清空，跨 surface 不复用
  残留状态，多次渲染像素完全一致。

