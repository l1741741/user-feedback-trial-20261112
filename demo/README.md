# paint-order 演示

本目录用本地 SVG 演示新增的 SVG2 `paint-order` 支持。

## 输入与脚本

- `assets/paint_order_demo.svg`：演示输入。三行图形使用**完全相同的路径几何、
  填充、描边宽度和起止标记**，只有 `paint-order` 属性不同，因此重叠处的任何
  可见差异都只来自绘制顺序，几何尺寸未为演示做任何改动。
- `render_demo.py`：渲染脚本（与输入数据分目录存放，仅通过路径读取 SVG）。

运行（库路径按本机环境设置，见仓库根目录 `NATIVE-RUNTIME.md`）：

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/Users/lichao/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib:/opt/homebrew/lib"
.venv/bin/python demo/render_demo.py
```

## 输出（`output/`）

- `paint_order_demo.png`：完整 PNG（640×560）。
- `<行>_marker_x4.png`：起点标记区域的 4 倍放大图。
- `<行>_overlap_x4.png`：描边与填充重叠区域的 4 倍放大图。

三行分别对应：

1. `default`（默认，fill → stroke → markers）：蓝色描边完整压在黄色填充上，
   红色标记方块完整可见。
2. `paint-order="stroke"`（stroke → fill → markers）：描边先画，重叠处被
   后画的黄色填充电覆盖，描边只在形状外侧保留一圈细边；标记仍在最上层。
3. `paint-order="markers"`（markers → fill → stroke）：标记最先画，被后画的
   描边盖住，起点处只剩描边颜色。
