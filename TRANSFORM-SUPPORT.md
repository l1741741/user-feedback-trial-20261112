# 变换原点与组合次序支持说明

本文件说明本次对 CairoSVG 变换管线（`cairosvg/helpers.py` 的
`transform()` / `resolve_transform_origin()`，以及 `surface.py`、
`defs.py`、`image.py` 的配套修改）的行为约定和支持范围。

## 1. 修复的问题

1. **`transform-origin` 关键词/百分比参照系错误。** 旧实现把
   `center`、`right`、`bottom` 以及裸百分比解析为**设备表面尺寸**
   （`surface.width/height`，单位为像素点）。当文档带有非恒等
   `viewBox`（缩放、平移）或节点位于已变换的父组内时，设备像素与
   局部用户单位并不重合，旋转/缩放中心因此偏移；在 2 倍缩放的
   viewBox 下，`center` 会把几何转到画布之外。
   现在原点一律在**节点自身的局部用户空间**中解析，即应用节点自身
   `transform` 之前、但已包含所有祖先组变换的坐标系。

2. **不支持 `transform-box`。** 现在识别 `view-box`（默认）与
   `fill-box` 两个取值。

3. **非法值的错误隔离不完整。** 旧实现遇到无法解析的原点 token 时
   依赖 `float()` 抛出的异常向外冒泡，或静默落到 0。现在原点解析的
   任何失败都收敛为“忽略该原点声明”，节点按无原点方式渲染，
   Cairo 上下文不被污染，后续兄弟节点不受影响。同样，`transform`
   属性列表中单个无法解析的函数会被跳过，其余函数继续生效。

4. **`<use>` / 嵌套 SVG 图像的就地状态变异。** `defs.use()` 与
   `image.image()` 过去会 `del node['x']`、`del node['y']`、
   `del node['mask']`、`del node['viewBox']`，使同一棵 `Tree`
   被重复渲染（或同一节点被多次 `<use>`）时结果依赖上一次渲染。
   这些删除已移除；重复渲染同一 `Tree` 现在产生逐字节相同的 PNG。

## 2. `transform-origin` 支持范围

### 2.1 值语法

接受一或两个 token，token 之间由空白或逗号分隔：

| 类别 | 示例 | 说明 |
| --- | --- | --- |
| 长度 | `10`、`10.5px`、`1in`、`2cm`、`12pt`、`1.5em` | 经 `helpers.size()` 按当前 DPI、字体尺寸换算为局部用户单位 |
| 百分比 | `50%`、`100%` | 相对当前参考盒的宽（x 轴）/高（y 轴） |
| x 关键词 | `left`、`center`、`right` | 参考盒宽度的 0%、50%、100% |
| y 关键词 | `top`、`center`、`bottom` | 参考盒高度的 0%、50%、100% |

单 token 规则遵循 CSS Transforms 1：

* `top` / `bottom`：x 轴取参考盒中心，y 轴取 0% / 100%；
* `left` / `right`：y 轴取参考盒中心，x 轴取 0% / 100%；
* `center`：两轴均取中心；
* 单个长度：设置 x 轴，y 轴取中心。

两 token 时允许交换次序（`top left` 与 `left top` 等价）。

### 2.2 默认行为

* **未设置 `transform-origin` 时不引入任何隐式原点**，旋转仍绕局部
  坐标原点 (0, 0)，保持 SVG 1.1 语义和既有渲染结果不变。
* 设置了但无法解析时，等价于未设置（而不是取 0,0 或 50%,50%）。
* 空字符串、超过两个 token、含无法识别单位的 token（如 `garbage`、
  `10pixels`）都属于无法解析的声明。

### 2.3 `transform-box`

| 取值 | 参考盒 | 支持状态 |
| --- | --- | --- |
| `view-box`（默认） | 最近的 SVG 视口，原点取活动 viewBox 的 min-x/min-y，尺寸取视口宽高（局部用户单位） | 支持 |
| `fill-box` | 节点自身几何的对象边界盒（object bounding box），不含节点自身变换 | 支持 |
| `border-box`、`content-box` 及其他任意值 | — | 不支持，回退到 `view-box` 并继续渲染 |

`fill-box` 的边界盒由 `cairosvg/bounding_box.py` 计算，覆盖
`rect`、`circle`、`ellipse`、`line`、`polyline`、`polygon`、
`path`、`text`/`tspan`、`g`、`use`、`marker`。

**退化边界处理。** 当 `fill-box` 请求的几何不存在或边界盒宽/高为
零（例如 `width="0"` 的矩形、无有效尺寸的路径）时，原点声明被
忽略，节点按无原点方式渲染；不会抛异常，也不会影响后续节点。

### 2.4 已知限制

* 百分比/关键词的参考盒不随 CSS 盒模型（padding/border）调整——
  SVG 元素无 CSS 边框盒概念，`border-box`/`content-box` 不实现。
* `transform-origin` 对 `gradientTransform`、`patternTransform`
  不生效（渐变/图案没有对象边界盒语义）；这两个属性仍按原方式
  解析变换函数列表。
* 3D 变换函数（`rotate3d`、`translate3d`、`matrix3d` 等）属于
  未知函数，会被逐个跳过，不影响同属性中的其他函数。

## 3. 变换组合次序

`transform()` 现在直接按 **SVG 属性中的书写顺序构造正向矩阵**：
属性中先列出的函数先作用于几何，等价于把矩阵左乘到当前矩阵上。
矩阵在交给 Cairo 前整体求逆一次（Cairo 的 CTM 把用户空间映射到
设备空间，方向相反）。

因此下列两种写法产生逐字节相同的输出（有测试覆盖）：

```xml
<g transform="translate(100,100)">
  <g transform="rotate(90)">
    <rect x="10" y="0" width="5" height="5"/>
  </g>
</g>

<rect x="10" y="0" width="5" height="5"
      transform="translate(100,100) rotate(90)"/>
```

节点自身坐标（`x`/`y`/路径数据）在最内层的局部空间中定义，先经过
节点自身 `transform`，再由内向外依次经过各祖先组变换，与浏览器
预览的嵌套图层行为一致。

支持的变换函数：`matrix`、`translate`、`scale`、`rotate`
（含 `rotate(angle cx cy)` 三参数形式）、`skewX`、`skewY`。
奇异矩阵（如 `scale(0)`）不可求逆时，沿用既有行为把当前裁剪区
置空，且不会中断文档其余部分的渲染。

## 4. 错误隔离与状态独立性约定

* 任何单点的解析失败（非法原点、非法变换函数、非法数值）都只影响
  当前声明本身，不向调用方抛异常、不修改 Cairo 上下文的既有状态。
* 渲染过程不修改节点字典中的输入属性（`x`、`y`、`mask`、
  `viewBox` 等保持不变），因此：
  * 同一 `Tree` 对象可被重复渲染，输出逐字节一致；
  * 同一被引用节点在多个 `<use>` 中各自独立定位，不依赖上一次
    实例化留下的状态；
  * 边界盒等派生信息通过节点上的缓存键存储，不改变输入属性。

## 5. 验证方式

* 单元/像素级测试：

  ```bash
  export DYLD_FALLBACK_LIBRARY_PATH="<本机 cairo 库路径>"
  .venv/bin/python -m pytest -q cairosvg/test_transform.py \
      cairosvg/test_api.py
  ```

  `test_transform.py` 中的断言基于实际 PNG 的像素包围盒与中心，
  覆盖：显式长度/关键词/百分比/物理单位原点、viewBox 缩放与平移、
  嵌套组、`fill-box` 与退化边界、非法值隔离、平铺变换列表与嵌套
  组等价、三层嵌套、`<use>` 复用幂等、默认行为回归。

* 本地 PNG 演示（含修复前/后对照面板与期望中心十字线）：

  ```bash
  .venv/bin/python -I demo/generate_demo.py
  # 产物：demo/transform_demo.png、demo/index.html
  ```
