# textPath 布局修复：测试与离线渲染说明

本目录验证 `cairosvg/text.py` 中沿路径文字（SVG `textPath`）的布局修复，
全部内容可离线运行，仅依赖本机 Cairo 动态库与系统字体（Arial/Helvetica）。

## 目录结构

- `cairosvg/test_text_path.py`（位于仓库主包内）：正式回归测试，共 15 个用例。
  通过记录 cairo 上下文变换矩阵来检查**字形布局**（位置、顺序、方向），
  而不是比较像素，从而给字体栅格化留出合理容差：
  - 位置容差 `POSITION_TOLERANCE = 1.0` 用户单位；
  - 角度容差 `ANGLE_TOLERANCE = 3.0` 度。
- `svg/`：演示用 SVG 源文件。
  - `textpath_showcase.svg`：带刻度圆弧、折线路径、反向直线、多段子路径。
  - `textpath_edge_cases.svg`：零长度路径、过短路径、路径变换、
    重复引用同一几何、闭合曲线。
- `png/`：上述 SVG 的离线 PNG 渲染结果（110 DPI）。
- `probe*.py`：开发期手动探针脚本（非正式测试），可直接运行查看每个字形
  实际落点与切向角。

## 修复覆盖的确定性行为

1. `startOffset` 百分比按**被引用路径自身长度**解析，并夹紧到
   `[0, 路径长度]`；负值按 0 处理，超出长度按末端处理。
2. `text-anchor`（start/middle/end）沿**路径方向**平移字形，
   反向路径上方向与位移都跟随切向。
3. 字符 `dx/dy` 与路径方向配合：`dx` 沿切向推进，`dy` 垂直于基线偏移。
4. 多段子路径：文字跨过第一段后在第二段继续排布；中间的零长度子路径被跳过。
5. 闭合曲线：`CLOSE_PATH` 显式补回闭合段，文字沿轮廓连续旋转。
6. 零长度路径（或仅含 `M` 的路径）：不绘制任何字形，也不抛异常。
7. 路径变换：字形位置与角度继承引用路径所在坐标系的变换。
8. 路径过短时，超出部分不绘制，且累计推进量不会泄漏给后续普通 `text`
   兄弟节点；嵌套 `tspan` 仍在同一 textPath 内连续推进。
9. 同一 `path` 被多次引用时只读取其几何、从不修改，重复引用布局完全一致。

## 运行方式

需要可用的 Cairo 共享库。在本机（macOS）上：

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/Users/lichao/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib:/opt/homebrew/lib"

# 正式回归（含原有 API 测试）
.venv/bin/python -m pytest -q cairosvg/test_text_path.py cairosvg/test_api.py

# 手动探针：打印每个字形的 (字符, x, y, 切向角)
.venv/bin/python -I text_path_tests/probe.py
.venv/bin/python -I text_path_tests/probe_more.py
.venv/bin/python -I text_path_tests/probe_nested.py

# 重新生成离线 PNG
.venv/bin/python -m cairosvg text_path_tests/svg/textpath_showcase.svg \
    -o text_path_tests/png/textpath_showcase.png -d 110
.venv/bin/python -m cairosvg text_path_tests/svg/textpath_edge_cases.svg \
    -o text_path_tests/png/textpath_edge_cases.png -d 110
```

其他机器应将 `DYLD_FALLBACK_LIBRARY_PATH`（Linux 上为
`LD_LIBRARY_PATH`）指向本机 Cairo 库所在目录。测试不依赖网络，
使用的是系统自带字体，因此字形位置在不同机器上可能有亚像素差异，
断言已按上述容差放宽。
