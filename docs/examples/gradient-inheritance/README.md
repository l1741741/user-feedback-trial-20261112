# 渐变 href 继承示例（离线）

这些例子演示多个品牌图标通过 `href`（SVG2 `href` 与旧版 `xlink:href` 均可）
共享公共线性、径向渐变定义时的正确继承行为。所有素材都是本地文件，
渲染不需要网络。

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `icons-order-a.svg` | 三个并排图标，图形顺序为 A、B、C |
| `icons-order-b.svg` | 与 A 的定义、位置、样式完全相同，仅把图形绘制顺序交换为 C、B、A |
| `icons-broken-refs.svg` | 循环引用与缺失引用的安全结束示例，且不污染同文档中的正常渐变 |

三个图标的继承关系：

```
brandLinear (公共色标, 水平向量)
└── iconLinear  覆盖 x2/y2（改为垂直向量），继承全部色标
    └── iconChain  无自身色标、无几何属性，两级 href 继承色标与向量，
                    仅覆盖 spreadMethod="repeat"
brandRadial (公共径向色标)
└── iconRadial  覆盖 r，继承圆心与色标
```

继承规则（SVG 2 §15.3.2，与本实现一致）：

- 引用者没有给出的属性，从被引用渐变的**有效定义**继承；
- 引用者自己给出的属性（几何、`gradientUnits`、`gradientTransform`、
  `spreadMethod` 等）优先；
- 引用者没有任何子元素时，继承被引用渐变的有效色标（`<stop>`）；
  引用者带有自身子元素时，使用自身色标；
- 多级继承沿 `href` 链逐级求解；循环引用和指向缺失 id 的引用安全结束，
  使用当前已知的节点，不抛异常、不修改其他图形。

## 渲染方法

需要 Cairo 动态库。仓库自带虚拟环境（`.venv`），在 macOS + Homebrew 上：

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib"
cd docs/examples/gradient-inheritance

# 命令行
../../.venv/bin/python -m cairosvg icons-order-a.svg -o icons-order-a.png
../../.venv/bin/python -m cairosvg icons-order-b.svg -o icons-order-b.png
../../.venv/bin/python -m cairosvg icons-broken-refs.svg -o icons-broken-refs.png
```

或 Python API：

```python
import cairosvg
cairosvg.svg2png(url='icons-order-a.svg', write_to='icons-order-a.png')
```

## 期望结果

`icons-order-a.png` 与 `icons-order-b.png` 必须**逐像素相同**：交换节点
顺序不会改变任何一个图标的颜色分布。

- 图标 A：垂直方向红 → 蓝线性渐变；
- 图标 B：继承垂直向量，色标重复平铺（repeat），整体 85% 不透明度；
- 图标 C：径向渐变，中心浅黄、边缘棕色，半径覆盖 75%。

`icons-broken-refs.svg` 正常出图：循环引用的矩形使用自身紫色色标，
缺失引用的矩形使用自身黑色色标，最右侧健康渐变仍为完整的红 → 蓝渐变。

这些断言由 `cairosvg/test_gradient_href.py` 通过实际 PNG 像素采样自动验证，
并与无 `href` 的等价基线渐变做回归对比。

目录中的 `*.png` 是用上面的命令从对应 `*.svg` 生成的参考输出（可随时删除后
重新生成）：`icons-order-a.png` 与 `icons-order-b.png` 字节完全一致。
