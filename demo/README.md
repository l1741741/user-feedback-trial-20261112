# negate_colors / invert_images 演示

同一份 SVG 版式（480×340，含矢量色块、渐变、文字、半透明 RGBA PNG、
带透明索引的调色板 PNG、RGB JPEG、带 EXIF 方向的 JPEG）导出四种组合：

| 文件 | negate_colors | invert_images |
| --- | --- | --- |
| `1_normal.png` | 关 | 关 |
| `2_negate_colors.png` | 开 | 关 |
| `3_invert_images.png` | 关 | 开 |
| `4_both.png` | 开 | 开 |

运行（在仓库根目录，需要可用的 Cairo 动态库，见 ../NATIVE-RUNTIME.md）：

```bash
.venv/bin/python demo/demo_color_flags.py
```

输出写入 `demo/output/`：

- `assets/` — 当次生成的本地图像素材（输入，可查看）；
- `1_normal.png` … `4_both.png` — 当次导出结果；
- `index.html` — 输入素材与四种结果的对照页，附像素检查结果。

## 两个开关的含义

- `negate_colors=True`：矢量颜色（填充、描边、渐变停止色、滤镜泛色、
  文字）取补色；不透明度与 alpha 不变，透明区域保持透明；栅格图片不受影响。
- `invert_images=True`：栅格图片每个像素取补色；alpha 通道、调色板透明
  索引、图像几何与 EXIF 方向保持不变，透明像素不会被铺成黑底；
  `<image>` 引用的矢量图不受影响（其颜色随 `negate_colors`）。
- 两者可组合；每张图每次渲染至多反色一次，输入图片不会被原地修改，
  重复导出结果一致；两个开关都关闭时输出与未传参完全相同。
- 无 8 位 RGB 语义的色彩模式（16 位灰度、浮点等）经 RGBA 近似转换后
  反色，结果仅为近似。

本地图片通过只允许素材目录的 `url_fetcher` 加载，演示不需要
`unsafe=True`；颜色开关不会扩大资源访问范围（默认取址仍只允许
data: URL）。
