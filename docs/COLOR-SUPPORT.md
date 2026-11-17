# CSS 颜色 Alpha 支持范围说明

本文件说明本次对 `cairosvg/colors.py` 的颜色解析改造所支持的语法、
通道与透明度的语义处理，以及明确**不支持**的内容。实现目标是覆盖网页
设计工具导出 SVG 中实际出现的颜色写法，而不是完整实现 CSS Color Level 4
新标准。

## 1. 支持的颜色写法

### 1.1 命名颜色与关键字

- 全部既有 SVG/CSS 命名颜色（`red`、`teal`、`gray` 等，含历史系统颜色名）。
- `none` 与 `transparent`：解析为 `(0, 0, 0, 0)`。
- 大小写不敏感，首尾空白会被忽略。

### 1.2 十六进制（含 alpha）

| 写法 | 含义 |
| --- | --- |
| `#RGB` | 短写三色，如 `#f00` |
| `#RGBA` | 短写三色 + alpha，末位按 CSS 规则复制成两位，如 `#f008` → alpha `0x88` |
| `#RRGGBB` | 长写三色，如 `#ff0000` |
| `#RRGGBBAA` | 长写三色 + alpha，如 `#ff000080` |

要求整串完全匹配（不再用子串搜索），非法位数（如 `#12345`）按无效颜色处理。

### 1.3 `rgb()` / `rgba()` 函数写法

兼容两类语法，可以混用在同一文档的不同元素上：

**传统逗号语法（保持原有合法写法）**

- `rgb(255, 0, 0)`
- `rgb(100%, 0%, 0%)`
- `rgba(255, 0, 0, 0.5)`
- `rgba(100%, 0%, 0%, 50%)` —— alpha 百分比以前会被错误地乘以 255，
  现在正确按 `% / 100` 解析（本次修复的回归点之一）。

**CSS Color 4 空格与斜线语法（新增）**

- `rgb(255 0 0)`
- `rgb(100% 0% 0%)`
- `rgb(255 0 0 / 0.5)`、`rgb(255 0 0 / 50%)`
- `rgba(255 0 0 / 0.5)`（`rgba` 同样接受空格语法）
- 数字可带符号、小数和科学计数法，如 `rgb(1e2 0 0)`。

语法约束（与 CSS Color 4 第 4.5 节一致）：

- 三个通道必须同为数字或同为百分比，混用（`rgb(255 0% 0)`）判为无效。
- 逗号语法中不允许斜线；组件数必须是 3 或 4。
- 空格语法中 alpha 只能由一个 `/` 引导。

## 2. 通道范围与透明度语义

- **RGB 通道**：数字按 `/255`、百分比按 `/100` 归一化，并钳制到 `[0, 1]`，
  例如 `rgb(300, -10, 0)` 等价于 `rgb(255, 0, 0)`。
- **alpha 通道**：裸数字即倍率（钳制到 `[0, 1]`），百分比按 `/100` 归一化。
- **只乘一次原则**：颜色自身的 alpha 与外部不透明度是乘法关系。
  调用方（`surface.py`、`defs.py`）把 `fill-opacity`、`stroke-opacity`、
  `stop-opacity`、`flood-opacity` 或组 `opacity` 折算出的系数作为
  `opacity` 参数传入，`color()` 只做一次 `alpha * opacity`：
  - 填充：颜色 alpha × `fill-opacity`（叶子节点上再含组 `opacity`）。
  - 描边：颜色 alpha × `stroke-opacity`。
  - 渐变停点：停点颜色 alpha × `stop-opacity` × 渐变引用处的绘制不透明度。
  - 含子节点的组：先在独立 group 上绘制，再 `paint_with_alpha(组 opacity)`，
    组内颜色 alpha 不会被重复乘。
- 半透明图形重叠时由 Cairo 按标准 source-over 公式合成，
  两层 alpha 0.5 的重叠区结果为 `0.5 + 0.5×(1-0.5) = 0.75`。

## 3. 无效颜色的兼容行为

沿用项目既有约定：无法识别的颜色**不抛异常、不中断文档**，回退为
不透明黑色 `(0, 0, 0, 1)`；空字符串仍返回全透明 `(0, 0, 0, 0)`。
同一文档中相邻的合法图形不受影响（见
`cairosvg/test_colors.py::test_invalid_color_does_not_break_the_rest_of_the_document`）。

## 4. 明确不支持的范围

以下属于 CSS Color 4/5 新特性，本次**不实现**，遇到时按第 3 节回退：

- 其他颜色函数：`hsl()`、`hwb()`、`lab()`、`lch()`、`oklab()`、`oklch()`、
  `color()`、`color-mix()`、`light-dark()` 等。
- `none` 关键字与缺失通道（`rgb(none none none)`）。
- `currentColor` 仍由 SVG 层既有逻辑在调用 `color()` 前替换，不在本模块处理。
- 相对颜色语法（`rgb(from red ...)`）、`device-cmyk()`、ICC 命名色彩。
- 8 位十六进制之外的整数精度（如 `#rrggbbaa` 的 16 位通道）。
- 颜色空间转换、色域映射、`color-scheme` / 系统颜色语义调整。
- `system-color` 旧名保留在命名表里仅为兼容，不随操作系统主题变化。

## 5. 验证入口

- 单元与像素回归：`pytest cairosvg/test_colors.py`（无预制位图，全部由
  CairoSVG 现场渲染后读回真实像素断言）。
- 可重新生成的颜色对照图：`python demo/color-alpha/generate_color_chart.py`，
  产物在 `demo/color-alpha/output/`，退出码非 0 表示等价写法像素不一致。
- 批量图标转换演示：`python demo/color-alpha/batch_convert_icons.py`。
