#!/usr/bin/env python3
"""嵌套视口对照例子 / Nested viewport comparison example.

构建一个"可缩放面板"：同一个图标（多层方框 + 圆 + 百分比标尺）分别用三种
嵌套视口方式组合布局：

1. 直接嵌套 ``svg`` 标签；
2. ``symbol`` 定义 + ``use`` 引用；
3. ``image`` 标签嵌入独立 SVG 文档（data URI）。

三种方式应当渲染出完全一致的结果：百分比宽高、圆半径和线条位置都必须相对
各自嵌套视口解析，而不是相对外层画布。脚本渲染整张面板为 PNG，逐像素对比
三个单元格，并检查中心、边界和比例探针。

运行 / Run:

    python examples/nested_viewport_comparison.py [输出目录]

退出码：0 表示三种组合方式结果一致；1 表示发现错位。
"""

import base64
import io
import sys
from pathlib import Path

from PIL import Image

import cairosvg

# 图标内容：全部使用百分比长度，viewBox 原点非零（10, 10）。
ICON_BODY = '''
    <rect x="14%" y="14%" width="72%" height="72%"
          fill="none" stroke="black" stroke-width="2"/>
    <rect x="15%" y="15%" width="25%" height="25%" fill="red"/>
    <circle cx="62.5%" cy="62.5%" r="12.5%" fill="blue"/>
    <line x1="0%" y1="50%" x2="100%" y2="50%"
          stroke="green" stroke-width="1"/>
    <line x1="25%" y1="45%" x2="25%" y2="55%"
          stroke="green" stroke-width="1"/>
    <line x1="50%" y1="45%" x2="50%" y2="55%"
          stroke="green" stroke-width="1"/>
    <line x1="75%" y1="45%" x2="75%" y2="55%"
          stroke="green" stroke-width="1"/>'''

VIEWBOX = '10 10 80 80'
CELL = 300  # 每个单元格的视口边长（设备像素）
CELL_Y = 100
CELL_XS = (40, 380, 720)
WIDTH, HEIGHT = 1060, 460

EMBEDDED = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="300" '
    f'viewBox="{VIEWBOX}">{ICON_BODY}</svg>')
EMBEDDED_B64 = base64.b64encode(EMBEDDED.encode('utf-8')).decode('ascii')

PANEL = f'''<svg width="{WIDTH}" height="{HEIGHT}"
  xmlns="http://www.w3.org/2000/svg"
  xmlns:xlink="http://www.w3.org/1999/xlink">
  <rect width="{WIDTH}" height="{HEIGHT}" fill="white"/>
  <defs>
    <symbol id="icon" viewBox="{VIEWBOX}">{ICON_BODY}
    </symbol>
  </defs>
  <!-- 单元格 1：直接嵌套 svg -->
  <svg x="{CELL_XS[0]}" y="{CELL_Y}" width="{CELL}" height="{CELL}"
       viewBox="{VIEWBOX}">{ICON_BODY}
  </svg>
  <!-- 单元格 2：symbol + use -->
  <use xlink:href="#icon" x="{CELL_XS[1]}" y="{CELL_Y}"
       width="{CELL}" height="{CELL}"/>
  <!-- 单元格 3：image 嵌入独立 SVG 文档 -->
  <image x="{CELL_XS[2]}" y="{CELL_Y}" width="{CELL}" height="{CELL}"
         xlink:href="data:image/svg+xml;base64,{EMBEDDED_B64}"/>
</svg>'''

NAMES = ('直接嵌套 svg', 'symbol + use', 'image 嵌入 SVG')


def check_probes(image, cell_x, name):
    """检查一个单元格的中心、边界和比例探针，返回错误列表。"""
    errors = []

    def probe(dx, dy, expected, label):
        pixel = image.getpixel((cell_x + dx, CELL_Y + dy))
        if pixel != expected:
            errors.append(
                f'{name}: 探针 {label} ({cell_x + dx}, {CELL_Y + dy}) '
                f'为 {pixel}，期望 {expected}')

    red = (255, 0, 0, 255)
    blue = (0, 0, 255, 255)
    white = (255, 255, 255, 255)
    # 视口 300px 映射 viewBox 80 单位（原点 10,10），比例 3.75，
    # 百分比相对 80x80 的有效视口解析。
    # 红框：15%..40% -> 用户 12..32 -> 设备 7.5..82.5（相对单元格）。
    probe(20, 20, red, '红框内部')
    probe(78, 78, red, '红框右下角内侧')
    probe(90, 90, white, '红框外（比例边界）')
    # 蓝圆：中心 62.5% -> 设备 150，半径 12.5% -> 37.5px。
    probe(150, 150, blue, '蓝圆中心')
    probe(150, 120, blue, '蓝圆上半部')
    probe(150, 105, white, '蓝圆外（半径边界）')
    # 标尺：50% 水平线 -> 设备 y≈112.5，贯穿 0%..100% -> x≤262.5。
    green = (0, 128, 0, 255)
    probe(20, 112, green, '标尺线左段')
    probe(250, 112, green, '标尺线右段')
    probe(270, 112, white, '标尺线终点外（100% 边界）')
    # 标尺刻度：25% -> 设备 x=37.5，45%..55% -> y 97.5..127.5。
    probe(37, 100, green, '标尺刻度 25%')
    probe(75, 100, white, '刻度间隙')
    return errors


def main():
    output_dir = Path(sys.argv[1] if len(sys.argv) > 1 else
                      Path(__file__).parent / 'out')
    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / 'panel.svg').write_text(PANEL, encoding='utf-8')
    (output_dir / 'embedded-icon.svg').write_text(
        EMBEDDED, encoding='utf-8')
    png = cairosvg.svg2png(bytestring=PANEL.encode('utf-8'))
    (output_dir / 'panel.png').write_bytes(png)
    image = Image.open(io.BytesIO(png)).convert('RGBA')
    print(f'面板已渲染到 {output_dir}/panel.png ({image.size[0]}x'
          f'{image.size[1]})')

    errors = []

    # 探针检查：中心、边界、比例
    for cell_x, name in zip(CELL_XS, NAMES):
        errors.extend(check_probes(image, cell_x, name))

    # 三种组合方式逐像素对比
    cells = [
        image.crop((x, CELL_Y, x + CELL, CELL_Y + CELL)).tobytes()
        for x in CELL_XS]
    for i in (1, 2):
        if cells[i] != cells[0]:
            diff = sum(
                1 for a, b in zip(cells[0], cells[i]) if a != b)
            errors.append(
                f'{NAMES[i]} 与 {NAMES[0]} 渲染结果不一致 '
                f'({diff} 个字节不同)')
        else:
            print(f'{NAMES[i]} 与 {NAMES[0]} 渲染结果一致 ✓')

    if errors:
        print('\n发现错位：')
        for error in errors:
            print(' -', error)
        return 1
    print('全部探针通过：中心、边界和比例在三种组合方式下一致 ✓')
    return 0


if __name__ == '__main__':
    sys.exit(main())
