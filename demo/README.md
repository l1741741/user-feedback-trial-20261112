# transform-origin 几何演示

本目录包含一个可在本地直接运行的 PNG 演示生成器，用偏心矩形、旋转轴和
嵌套图层展示 `transform-origin` / `transform-box` 的前后关系。

## 运行

```bash
# 如 Cairo 动态库不在默认搜索路径，先设置库路径（按需调整）：
export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib"

# 使用项目虚拟环境；-I 隔离用户站点包
.venv/bin/python -I demo/build_demo.py
```

脚本会在 `demo/output/` 下生成 7 张 PNG，并对每张图实际渲染的像素
包围盒与手算坐标逐一核对；任何一张几何不匹配都会以 `AssertionError`
失败并标记 `[BAD]`。

## 场景清单

| 文件 | 展示内容 |
| --- | --- |
| `01_explicit_length_origin.png` | 偏心矩形绕显式长度原点 `20 20` 旋转 90°，蓝十字为旋转中心，绿点跟踪角点 (60,20)→(20,60) |
| `02_fill_box_center.png` | `transform-box: fill-box` 下原点取矩形自身中心 (40,30) |
| `03_nested_layers.png` | 外层 `translate(50,0)` + 内层 `rotate(90)`，标注局部轴 (30,30) 与全局轴 (80,30) |
| `04_percentage_view_box.png` | 百分比原点 `25% 75%` 在默认 `view-box` 上解析为 (40,120) |
| `05_degenerate_box_fallback.png` | 零尺寸矩形的退化包围盒回退到视口框，兄弟节点正常渲染 |
| `06_illegal_value_isolated.png` | 非法原点回退到参考框中心，绿色兄弟节点不受影响 |
| `07_reused_node_independent.png` | 同一节点经 `<use>` 复用三次，各自独立落位，不依赖上一次位置 |

图中虚线灰框为变换前的几何，红色实体为变换后的结果，浅灰网格每格
10 个用户单位，便于直接核对中心与角点坐标。
