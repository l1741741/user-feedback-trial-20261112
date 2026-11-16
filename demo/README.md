# 变换原点 PNG 演示

本目录提供一个完全在本地运行的视觉验证演示，展示
`transform-origin` / `transform-box` 修复后的几何结果，并包含修复前
行为的对照面板。

## 运行

在仓库根目录执行（需要项目虚拟环境和可用的 Cairo 共享库）：

```bash
export DYLD_FALLBACK_LIBRARY_PATH="<本机 cairo 库路径>"
.venv/bin/python -I demo/generate_demo.py
```

macOS 上本机已验证的库路径见 `NATIVE-RUNTIME.md`。

## 产物

* `transform_demo.png`：9 个面板的合成图。面板 1–8 用蓝色十字线/圆点
  标注期望的旋转中心或角点，背景网格间距为 20 设备像素；面板 9 是
  同一 SVG 在修复前（设备尺寸原点，矩形转出画布）与修复后（局部用户
  空间原点，矩形正确居中）的并排放置对比。
* `index.html`：各面板的文字索引与预期几何说明。

演示只依赖仓库内的 `cairosvg` 包、Pillow 和 cairocffi，不访问网络。
