# transform-origin / transform-box 支持说明

本文件说明 CairoSVG 当前对 CSS Transforms Level 1 中
`transform-origin` 与 `transform-box` 的支持范围、组合次序约定，以及对
非法输入和退化几何的处理。实现位于 `cairosvg/helpers.py`
（`parse_transform_origin`、`get_transform_reference_box`、
`_origin_coordinate`、`transform`），渲染入口在
`cairosvg/surface.py` 的 `Surface.draw`。

## 1. 变换原点（transform-origin）

### 1.1 支持的取值

原点在节点自身的局部坐标系中解析（即应用该节点 `transform` 之前），
接受一或两个分量；只给一个分量时，另一个分量取 `center`：

| 写法 | 含义 |
| --- | --- |
| `left` / `center` / `right` | 水平方向 0% / 50% / 100% |
| `top` / `center` / `bottom` | 垂直方向 0% / 50% / 100% |
| `25% 75%` | 参考框宽、高的百分比 |
| `30 40`、`20px 30px`、`72pt 1in` 等 | 显式长度，支持 `px/pt/pc/in/cm/mm/em/ex/ch` |
| 单独的垂直关键字（如 `top`） | 作用于 y 轴，x 轴补 `center` |

显式长度按当前视口单位换算后，从参考框原点叠加；百分比与关键字按
参考框尺寸解析。

### 1.2 与 transform 列表的组合次序

原点平移会**包裹整段 transform 列表**，而不是只包裹第一个操作。
设原点为 O=(ox, oy)，属性中的变换依次为 T1、T2、…，则前向映射为：

```
F = T(O) · T1 · T2 · … · T(-O)
```

即先把原点移到 (0,0)，依次执行属性中的全部变换，再移回原点。
嵌套组中，父组矩阵通过 Cairo context 先入栈，子节点矩阵在其局部
坐标系中右乘，因此最终效果与浏览器一致：父变换在外层，子变换在内层，
各节点原点均以自身局部坐标表达。

## 2. 参考框（transform-box）

| 取值 | 支持情况 |
| --- | --- |
| `view-box`（默认） | 完整支持。使用最近 SVG 视口，原点位于视口 (0,0)，尺寸取 `context_width/height`（用户单位，不受画布缩放影响） |
| `fill-box` | 完整支持。使用元素自身的对象边界框（rect/circle/ellipse/line/polyline/polygon/path/text/g/use/marker 的几何包围盒） |
| `content-box`、`border-box` | 按 `fill-box` 处理（SVG 几何没有 CSS padding/border，二者等价） |
| `stroke-box` | 按 `fill-box` 处理。CairoSVG 不计算描边外扩范围，这是已知限制 |
| 未知名 / 非法值 | 回退到默认的 `view-box` |

### 2.1 非零边界与退化边界

* `fill-box` 仅在边界框**宽、高均非零**时采用（`is_non_empty_bounding_box`）。
* 零宽或零高图形（如 `width="0"` 的 rect、点重合的 line、空 path）
  以及包围盒计算异常时，自动回退到 `view-box`，不产生 NaN/无穷坐标，
  节点仍可正常渲染。
* 组（`g`）的边界框由全部子节点的边界框合并得到；若所有子节点都是
  退化框，同样回退到视口框。

## 3. 非法值与状态隔离

* `transform-origin` 分量个数非法（0 个或 3 个以上）：整个声明忽略，
  节点按无原点（绕局部 (0,0)）处理。
* 单个分量无法解析为关键字、百分比或长度：该分量回退到参考框中心
  （CSS 对坏坐标的既定行为），不抛出异常。
* 非有限结果（NaN/无穷）：声明整体忽略。
* `transform` 属性中的单个非法操作（如 `rotate(abc)`、参数个数错误的
  `matrix(...)`）：跳过该操作，继续应用列表中的其余合法操作。
* 所有解析均为节点级无状态计算，不修改节点字典、不写入共享缓存；
  一个节点的非法值不会改变后续兄弟节点的渲染。
* 通过 `<use>` 复用同一节点时，每次实例化独立解析原点，重复渲染
  （含同进程内多次 `svg2png` 调用）输出逐字节一致，不依赖上一次位置。

## 4. 与历史行为的差异

旧实现把原点解析到 **Cairo 表面设备像素**（`surface.width/height`），
在带 `viewBox` 缩放或非 1:1 输出比例时，原点会随缩放漂移，旋转/缩放
中心错误，甚至把图形整体转出画布。新实现统一使用用户单位的视口尺寸
（`context_width/context_height`）与对象局部包围盒，并在应用任何变换
前一次性解析原点，因此在缩放视口下中心位置与几何预期一致。

未声明 `transform-origin` 时行为不变：仍绕局部坐标原点 (0,0) 变换，
现有 API 回归测试全部通过。

## 5. 已知限制

* `stroke-box` 不包含描边宽度，按 `fill-box` 处理。
* `transform-box` 不影响没有 `transform-origin` 的节点（符合规范）。
* 3D 变换函数（`rotate3d`、`perspective` 等）不在 SVG2 平面变换范围内，
  仍会被当作未知操作跳过。
* `transform` 列表内的角度/长度单位（如 `rotate(90deg)`、
  `translate(10px)`）沿用 CairoSVG 既有解析约定：函数参数只接受裸数值，
  带单位的参数会使该操作被跳过。单位仅在 `transform-origin` 与普通
  SVG 几何属性中完整支持。这是本次改动之外的既有约束。
* 文本包围盒依赖 Cairo 的字体度量，`fill-box` 作用于 `text` 时与浏览器
  可能存在亚像素级差异。

## 6. 本地验证

```bash
# 单元测试（纯矩阵 + 像素级几何断言，共 25 个用例）
export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib"   # 如 cairo 不在默认路径
.venv/bin/python -m pytest -q cairosvg/test_transform_origin.py cairosvg/test_api.py

# 生成并自检 7 张 PNG 演示（输出到 demo/output/，每张图校验绘制包围盒）
.venv/bin/python -I demo/build_demo.py
```

`demo/output/` 中的演示场景：

1. `01_explicit_length_origin.png` — 偏心矩形绕显式长度原点旋转 90°，
   跟踪角点落位；
2. `02_fill_box_center.png` — `fill-box` 下绕矩形自身中心旋转；
3. `03_nested_layers.png` — 外层平移 + 内层旋转的嵌套图层，标注局部轴
   与全局轴；
4. `04_percentage_view_box.png` — 百分比原点在 `view-box` 上的解析；
5. `05_degenerate_box_fallback.png` — 零尺寸矩形的退化边界回退与
   兄弟节点回归；
6. `06_illegal_value_isolated.png` — 非法原点回退中心、绿色兄弟节点
   不受影响；
7. `07_reused_node_independent.png` — 同一节点经 `<use>` 复用三次，
   各自独立落位。
