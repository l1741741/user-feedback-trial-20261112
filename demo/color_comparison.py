#!/usr/bin/env python3
"""
Generate a reproducible CSS color comparison chart with CairoSVG.

Every swatch in the output image is rendered by CairoSVG from an SVG source
written by this script; no pre-made bitmap is used for the tested colors.
The script measures the real rendered pixels, checks equivalence groups, and
writes:

- ``color_comparison.png``: the chart itself (larger than 240x160);
- ``color_comparison.json``: measured pixels and check results;
- ``samples/*.svg``: the exact SVG documents used for each swatch.

Run it from the repository root::

    python demo/color_comparison.py

The script exits with status 1 when a check fails, so it can be used as a
regression check as well as a local demonstration.

"""

import argparse
import io
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import cairosvg  # noqa: E402

SWATCH_WIDTH = 96
SWATCH_HEIGHT = 64
ROW_HEIGHT = 52
SWATCH_DISPLAY_WIDTH = 64
SWATCH_DISPLAY_HEIGHT = 43
CHART_WIDTH = 1024
HEADER_HEIGHT = 72
GROUP_HEADER_HEIGHT = 30
FOOTER_HEIGHT = 56


def document(body, width=SWATCH_WIDTH, height=SWATCH_HEIGHT):
    """Return a standalone SVG document containing ``body``."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}">{body}</svg>')


def render_rgba(svg):
    """Render ``svg`` with CairoSVG and return a Pillow RGBA image."""
    png = cairosvg.svg2png(bytestring=svg.encode('utf-8'))
    return Image.open(io.BytesIO(png)).convert('RGBA')


def sample_pixels(svg, points):
    """Render ``svg`` once and return the RGBA value at each ``points``."""
    image = render_rgba(svg)
    return [image.getpixel(point) for point in points]


def fill_case(color_value, attribute='fill'):
    """A swatch with a single filled rectangle."""
    return document(
        f'<rect width="{SWATCH_WIDTH}" height="{SWATCH_HEIGHT}" '
        f'{attribute}="{color_value}"/>')


def fill_style_case(declaration):
    """A swatch using an inline ``style`` declaration."""
    return document(
        f'<rect width="{SWATCH_WIDTH}" height="{SWATCH_HEIGHT}" '
        f'style="{declaration}"/>')


def stroke_case(color_value, opacity=None):
    """A swatch with a thick horizontal stroke."""
    opacity = '' if opacity is None else f' stroke-opacity="{opacity}"'
    return document(
        f'<line x1="4" y1="{SWATCH_HEIGHT // 2}" x2="{SWATCH_WIDTH - 4}" '
        f'y2="{SWATCH_HEIGHT // 2}" stroke="{color_value}" '
        f'stroke-width="24"{opacity}/>')


def gradient_case(stop_color, stop_opacity='1', style=False):
    """A swatch filled by a uniform gradient between two equal stops."""
    if style:
        stop = (
            f'<stop offset="{{offset}}" '
            f'style="stop-color:{stop_color};'
            f'stop-opacity:{stop_opacity}"/>')
    else:
        stop = (
            f'<stop offset="{{offset}}" stop-color="{stop_color}" '
            f'stop-opacity="{stop_opacity}"/>')
    return document(
        '<defs><linearGradient id="g" gradientUnits="userSpaceOnUse" '
        f'x1="0" y1="0" x2="{SWATCH_WIDTH}" y2="0">'
        + stop.format(offset='0') + stop.format(offset='1') +
        '</linearGradient></defs>'
        f'<rect width="{SWATCH_WIDTH}" height="{SWATCH_HEIGHT}" '
        'fill="url(#g)"/>')


def overlap_case():
    """Three translucent layers, with single, double and triple overlap."""
    return document(
        '<rect width="60" height="64" fill="rgba(255,0,0,0.5)"/>'
        '<rect x="20" width="50" height="64" fill="rgb(255 0 0 / 50%)"/>'
        '<rect x="40" width="50" height="64" fill="#ff000080"/>')


def composition_case(fill, fill_opacity=None, opacity=None):
    """A swatch combining color alpha, fill opacity and group opacity."""
    attributes = ''
    if fill_opacity is not None:
        attributes += f' fill-opacity="{fill_opacity}"'
    rect = (
        f'<rect width="{SWATCH_WIDTH}" height="{SWATCH_HEIGHT}" '
        f'fill="{fill}"{attributes}/>')
    if opacity is not None:
        rect = f'<g opacity="{opacity}">{rect}</g>'
    return document(rect)


def masked_case(opacity):
    """A swatch with a fully opaque mask and the given element opacity."""
    return document(
        '<defs><mask id="m">'
        f'<rect width="{SWATCH_WIDTH}" height="{SWATCH_HEIGHT}" '
        'fill="#fff"/></mask></defs>'
        f'<rect width="{SWATCH_WIDTH}" height="{SWATCH_HEIGHT}" fill="#f00" '
        f'opacity="{opacity}" mask="url(#m)"/>')


def filtered_case(opacity):
    """A swatch with a neutral filter and the given element opacity."""
    return document(
        '<defs><filter id="f"><feOffset dx="0" dy="0"/></filter></defs>'
        f'<rect width="{SWATCH_WIDTH}" height="{SWATCH_HEIGHT}" fill="#f00" '
        f'opacity="{opacity}" filter="url(#f)"/>')


def use_case(opacity):
    """A swatch drawn through a ``<use>`` reference with an opacity."""
    return document(
        '<defs><rect id="r" width="96" height="64" fill="#f00"/></defs>'
        f'<use href="#r" opacity="{opacity}"/>')


def pattern_case():
    """A pattern referenced twice, each with ``fill-opacity`` 0.5."""
    return document(
        '<defs><pattern id="p" width="8" height="8" '
        'patternUnits="userSpaceOnUse">'
        '<rect width="8" height="8" fill="#0000ff"/></pattern></defs>'
        '<rect width="40" height="64" fill="url(#p)" fill-opacity="0.5"/>'
        '<rect x="56" width="40" height="64" fill="url(#p)" '
        'fill-opacity="0.5"/>')


def invalid_color_case():
    """An invalid color in the middle of a document with valid siblings."""
    return document(
        '<rect width="32" height="64" fill="#00ff00"/>'
        '<rect x="32" width="32" height="64" style="fill:rgb(1,2)"/>'
        '<rect x="64" width="32" height="64" fill="#00ff00"/>')


def invalid_opacity_case():
    """Invalid opacity values next to a valid sibling."""
    return document(
        '<g opacity="not-a-number">'
        '<rect width="48" height="64" fill="#ff0000" '
        'fill-opacity="nope"/></g>'
        '<rect x="48" width="48" height="64" fill="#00ff00"/>')


CENTER = (SWATCH_WIDTH // 2, SWATCH_HEIGHT // 2)

# Each group is a list of cases. A case is ``(label_zh, label_en, svg)`` or
# ``(label_zh, label_en, svg, expected)``; ``expected`` overrides the group
# expectation, either as one pixel or as one pixel per sample point.
# ``samples`` gives the points measured on the rendered swatch (the center by
# default).
GROUPS = (
    {
        'title': '填充 · 等价写法（表现属性）',
        'title_en': 'Fill: equivalent spellings',
        'expected': (255, 0, 0, 255),
        'cases': (
            ('#f00（短十六进制）', '#f00 (short hex)',
             fill_case('#f00')),
            ('#ff0000（长十六进制）', '#ff0000 (long hex)',
             fill_case('#ff0000')),
            ('red（命名颜色）', 'red (named color)', fill_case('red')),
            ('rgb(255,0,0)（合法逗号写法）', 'rgb(255,0,0) (commas)',
             fill_case('rgb(255,0,0)')),
            ('rgb(255 0 0)（空格写法）', 'rgb(255 0 0) (spaces)',
             fill_case('rgb(255 0 0)')),
            ('rgb(100%, 0%, 0%)（百分比）', 'rgb(100%, 0%, 0%) (percent)',
             fill_case('rgb(100%, 0%, 0%)')),
            ('rgb(100% 0% 0%)（百分比+空格）',
             'rgb(100% 0% 0%) (percent, spaces)',
             fill_case('rgb(100% 0% 0%)')),
            ('style="fill:rgb(255 0 0)"（内联样式）',
             'style="fill:rgb(255 0 0)" (inline style)',
             fill_style_case('fill:rgb(255 0 0)')),
        ),
    },
    {
        'title': '填充 · 颜色自带 alpha 的等价写法',
        'title_en': 'Fill: equivalent spellings with color alpha',
        'expected': (255, 0, 0, 128),
        'cases': (
            ('#ff000080（长十六进制 + alpha）',
             '#ff000080 (long hex + alpha)', fill_case('#ff000080')),
            ('rgba(255,0,0,0.5)（逗号写法）',
             'rgba(255,0,0,0.5) (commas)',
             fill_case('rgba(255,0,0,0.5)')),
            ('rgb(255 0 0 / 0.5)（斜线 alpha）',
             'rgb(255 0 0 / 0.5) (slash alpha)',
             fill_case('rgb(255 0 0 / 0.5)')),
            ('rgb(255 0 0 / 50%)（斜线百分比）',
             'rgb(255 0 0 / 50%) (slash percent)',
             fill_case('rgb(255 0 0 / 50%)')),
            ('rgb(100% 0% 0% / 50%)（百分比 + 斜线）',
             'rgb(100% 0% 0% / 50%) (percent + slash)',
             fill_case('rgb(100% 0% 0% / 50%)')),
            ('style="fill:#ff000080"（内联样式）',
             'style="fill:#ff000080" (inline style)',
             fill_style_case('fill:#ff000080')),
        ),
    },
    {
        'title': '填充 · 短十六进制中的 alpha（#f008）',
        'title_en': 'Fill: alpha in short hex (#f008)',
        'expected': (255, 0, 0, 136),
        'cases': (
            ('#f008（短十六进制 + alpha）',
             '#f008 (short hex + alpha)', fill_case('#f008')),
            ('#ff000088（等价长写法）',
             '#ff000088 (equivalent long hex)', fill_case('#ff000088')),
            ('rgb(255 0 0 / 53.333%)（等价百分比）',
             'rgb(255 0 0 / 53.333%) (equivalent percent)',
             fill_case('rgb(255 0 0 / 53.333%)')),
        ),
    },
    {
        'title': '描边 · 等价写法',
        'title_en': 'Stroke: equivalent spellings',
        'expected': (0, 128, 0, 255),
        'cases': (
            ('#008000', '#008000', stroke_case('#008000')),
            ('rgb(0,128,0)（逗号写法）', 'rgb(0,128,0) (commas)',
             stroke_case('rgb(0,128,0)')),
            ('rgb(0 128 0)（空格写法）', 'rgb(0 128 0) (spaces)',
             stroke_case('rgb(0 128 0)')),
            ('rgb(0% 50.1961% 0%)（百分比）',
             'rgb(0% 50.1961% 0%) (percent)',
             stroke_case('rgb(0% 50.1961% 0%)')),
        ),
    },
    {
        'title': '描边 · 颜色 alpha 与 stroke-opacity 组合（各乘一次）',
        'title_en': 'Stroke: color alpha x stroke-opacity, each applied once',
        'expected': (0, 128, 0, 128),
        'cases': (
            ('stroke="#00800080"', 'stroke="#00800080"',
             stroke_case('#00800080')),
            ('rgba(0,128,0,0.5)', 'rgba(0,128,0,0.5)',
             stroke_case('rgba(0,128,0,0.5)')),
            ('rgb(0 128 0 / 50%)', 'rgb(0 128 0 / 50%)',
             stroke_case('rgb(0 128 0 / 50%)')),
            ('stroke-opacity="0.5"（无颜色 alpha）',
             'stroke-opacity="0.5" (no color alpha)',
             stroke_case('#008000', '0.5')),
            ('颜色 alpha × stroke-opacity = 25%',
             'color alpha x stroke-opacity = 25%',
             stroke_case('#00800080', '0.5'), (0, 128, 0, 64)),
        ),
    },
    {
        'title': '渐变 · 色标等价写法（色标 alpha 50%）',
        'title_en': 'Gradient: equivalent stop spellings (stop alpha 50%)',
        'expected': (255, 0, 0, 128),
        'cases': (
            ('stop-color="#ff000080"', 'stop-color="#ff000080"',
             gradient_case('#ff000080')),
            ('stop-color="rgba(255,0,0,0.5)"',
             'stop-color="rgba(255,0,0,0.5)"',
             gradient_case('rgba(255,0,0,0.5)')),
            ('stop-color="rgb(255 0 0 / 50%)"',
             'stop-color="rgb(255 0 0 / 50%)"',
             gradient_case('rgb(255 0 0 / 50%)')),
            ('style="stop-color:#ff000080"（内联样式）',
             'style="stop-color:#ff000080" (inline style)',
             gradient_case('#ff000080', style=True)),
        ),
    },
    {
        'title': '渐变 · stop-opacity 与色标 alpha 叠加',
        'title_en': 'Gradient: stop-opacity combined with stop alpha',
        'expected': (255, 0, 0, 64),
        'cases': (
            ('色标 alpha 50% × stop-opacity 0.5',
             'stop alpha 50% x stop-opacity 0.5',
             gradient_case('rgb(255 0 0 / 50%)', '0.5')),
            ('不透明色标 × stop-opacity 25%',
             'opaque stop x stop-opacity 25%',
             gradient_case('red', '25%')),
        ),
    },
    {
        'title': '半透明重叠 · 单层/两层/三层',
        'title_en': 'Translucent overlap: single, double and triple layers',
        'expected': (255, 0, 0, 128),
        'samples': ((10, 32), (30, 32), (50, 32), (80, 32)),
        'cases': (
            ('单层 50% / 两层 75% / 三层 87.5% / 单层 50%',
             'single 50% / double 75% / triple 87.5% / single 50%',
             overlap_case(),
             ((255, 0, 0, 128), (255, 0, 0, 192),
              (255, 0, 0, 224), (255, 0, 0, 128))),
        ),
    },
    {
        'title': '透明度组合 · color alpha × fill-opacity × 组透明度',
        'title_en': 'Opacity: color alpha x fill-opacity x group opacity',
        'expected': (255, 0, 0, 64),
        'cases': (
            ('color alpha 50% × fill-opacity 50%',
             'color alpha 50% x fill-opacity 50%',
             composition_case('rgba(255,0,0,0.5)', '0.5')),
            ('color alpha 50% × 组 opacity 50%',
             'color alpha 50% x group opacity 50%',
             composition_case('rgb(255 0 0 / 50%)', opacity='0.5')),
            ('fill-opacity 50% × 组 opacity 50%',
             'fill-opacity 50% x group opacity 50%',
             composition_case('#ff0000', '0.5', '0.5')),
            ('三者相乘 50% × 50% × 50% = 12.5%',
             'all three: 50% x 50% x 50% = 12.5%',
             composition_case('#ff000080', '0.5', '0.5'),
             (255, 0, 0, 32)),
        ),
    },
    {
        'title': 'opacity 只应用一次（组/蒙版/滤镜/use）',
        'title_en': 'opacity applied exactly once (group/mask/filter/use)',
        'expected': (255, 0, 0, 128),
        'cases': (
            ('普通元素 opacity 50%', 'plain element, opacity 50%',
             fill_style_case('fill:#f00;opacity:0.5')),
            ('带 mask 的元素 opacity 50%', 'element with mask, opacity 50%',
             masked_case('0.5')),
            ('带 filter 的元素 opacity 50%',
             'element with filter, opacity 50%', filtered_case('0.5')),
            ('<use> 引用 opacity 50%', '<use> reference, opacity 50%',
             use_case('0.5')),
        ),
    },
    {
        'title': '无效输入隔离 · 同文档其他图形不受影响',
        'title_en': 'Invalid input isolation: siblings keep rendering',
        'expected': (0, 255, 0, 255),
        'samples': ((16, 32), (48, 32), (80, 32)),
        'cases': (
            ('无效 fill 回退黑色，两侧兄弟图形正常',
             'invalid fill falls back to black; siblings render',
             invalid_color_case(),
             ((0, 255, 0, 255), (0, 0, 0, 255), (0, 255, 0, 255))),
            ('无效 opacity 回退为 1，右侧兄弟图形正常',
             'invalid opacity falls back to 1; sibling renders',
             invalid_opacity_case(),
             ((255, 0, 0, 255), (0, 255, 0, 255), (0, 255, 0, 255))),
        ),
    },
    {
        'title': '重复导出与重复引用',
        'title_en': 'Repeated export and repeated reference',
        'expected': (0, 0, 255, 128),
        'samples': ((20, 32), (76, 32)),
        'cases': (
            ('同一 pattern 两次引用，fill-opacity 均为 0.5',
             'same pattern referenced twice, fill-opacity 0.5',
             pattern_case()),
        ),
    },
)


def normalize_expected(expected, count):
    """Return one expected pixel per sample point for ``expected``."""
    if (len(expected) == 4 and
            all(isinstance(value, int) for value in expected)):
        return (tuple(expected),) * count
    expected = tuple(tuple(pixel) for pixel in expected)
    if len(expected) == 1:
        return expected * count
    return expected


def build_cases():
    """Build the case list, rendering and measuring every swatch."""
    cases = []
    for group_index, group in enumerate(GROUPS):
        points = group.get('samples', (CENTER,))
        measured_cases = []
        for entry in group['cases']:
            label, label_en, svg = entry[:3]
            expected = entry[3] if len(entry) == 4 else group['expected']
            expected = normalize_expected(expected, len(points))
            pixels = sample_pixels(svg, points)
            measured_cases.append({
                'label': label,
                'label_en': label_en,
                'svg': svg,
                'pixels': pixels,
                'expected': expected,
                'matches_expected': pixels == list(expected),
            })
        cases.append({
            'index': group_index,
            'title': group['title'],
            'title_en': group['title_en'],
            'expected': group['expected'],
            'points': list(points),
            'cases': measured_cases,
            'matches_expected': all(
                case['matches_expected'] for case in measured_cases),
        })
    return cases


def check_equivalence(cases):
    """Check that cases in a group render the same pixels.

    Groups whose cases have different documented expectations (for example a
    transparency-composition series) are only checked against their expected
    pixels, not for mutual equivalence.

    """
    results = []
    for group in cases:
        reference = group['cases'][0]['pixels']
        same_expected = all(
            case['expected'] == group['cases'][0]['expected']
            for case in group['cases'])
        equal = (
            all(case['pixels'] == reference for case in group['cases'])
            if same_expected else None)
        results.append({
            'title': group['title'],
            'title_en': group['title_en'],
            'equivalent': equal,
            'matches_expected': group['matches_expected'],
        })
    return results


def check_repeated_export():
    """Render the same document twice and compare bytes and pixels."""
    source = document(
        '<rect width="40" height="64" fill="rgba(255,0,0,0.5)"/>'
        '<rect x="40" width="40" height="64" fill="rgb(0 0 255 / 50%)"/>'
        '<rect x="80" width="16" height="64" fill="rgb(100% 0% 0% / 50%)"/>')
    first = cairosvg.svg2png(bytestring=source.encode('utf-8'))
    second = cairosvg.svg2png(bytestring=source.encode('utf-8'))
    first_image = Image.open(io.BytesIO(first)).convert('RGBA').tobytes()
    second_image = Image.open(io.BytesIO(second)).convert('RGBA').tobytes()
    return {
        'identical_png': first == second,
        'identical_pixels': first_image == second_image,
        'pixels': list(sample_pixels(source, [(20, 32), (60, 32), (88, 32)])),
    }


def slugify(label, index):
    """Return a filesystem-safe slug for a case label."""
    ascii_label = re.sub(r'[^a-zA-Z0-9]+', '-', label).strip('-').lower()
    return f'{index:02d}-{ascii_label or "case"}'


def checkerboard(size, cell=8):
    """Return a checkerboard image used behind translucent swatches."""
    image = Image.new('RGBA', size, (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)
    for y in range(0, size[1], cell):
        for x in range(0, size[0], cell):
            if (x // cell + y // cell) % 2:
                draw.rectangle(
                    (x, y, x + cell - 1, y + cell - 1),
                    fill=(222, 222, 222, 255))
    return image


CJK_FONT_CANDIDATES = (
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
    '/System/Library/Fonts/STHeiti Light.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
    'C:/Windows/Fonts/msyh.ttc',
    'C:/Windows/Fonts/simhei.ttf',
)


def find_cjk_font():
    """Return a font path able to display Chinese labels, or ``None``."""
    for candidate in CJK_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def load_fonts():
    """Load the fonts used by the chart.

    When no CJK font is available the chart falls back to its English labels
    so that every label stays readable instead of showing empty boxes.

    """
    cjk_path = find_cjk_font()
    if cjk_path:
        try:
            return (
                ImageFont.truetype(cjk_path, 15),
                ImageFont.truetype(cjk_path, 13),
                ImageFont.truetype(cjk_path, 21),
                True)
        except OSError:  # pragma: no cover - unreadable font file
            pass
    try:
        regular = ImageFont.load_default(size=14)
        small = ImageFont.load_default(size=13)
        title = ImageFont.load_default(size=20)
    except TypeError:  # pragma: no cover - very old Pillow
        regular = small = title = ImageFont.load_default()
    return regular, small, title, False


def build_chart(cases, export_check, output_path):
    """Draw the chart image and save it to ``output_path``."""
    regular, small, title_font, has_cjk = load_fonts()

    def text(position, value, *, english, fill, font):
        """Draw the Chinese label when a CJK font is available."""
        draw.text(position, value if has_cjk else english, fill=fill,
                  font=font)

    rows = sum(len(group['cases']) for group in cases)
    height = (
        HEADER_HEIGHT + FOOTER_HEIGHT + rows * ROW_HEIGHT +
        len(cases) * GROUP_HEADER_HEIGHT + 40)
    chart = Image.new('RGB', (CHART_WIDTH, height), (255, 255, 255))
    draw = ImageDraw.Draw(chart)

    text(
        (24, 18), 'CairoSVG CSS 颜色解析与透明度对照图',
        english='CairoSVG CSS color parsing and alpha comparison chart',
        fill=(0, 0, 0), font=title_font)
    text(
        (24, 46),
        '每个色块都由 CairoSVG 从 demo/samples/ 中的 SVG 源码实时渲染；'
        '数值为渲染结果中心像素的 RGBA。',
        english='Every swatch is rendered live by CairoSVG from the SVG '
                'source in demo/samples/; values are rendered RGBA pixels.',
        fill=(70, 70, 70), font=small)

    y = HEADER_HEIGHT
    for group in cases:
        same_expected = all(
            case['expected'] == group['cases'][0]['expected']
            for case in group['cases'])
        equivalent = (
            all(case['pixels'] == group['cases'][0]['pixels']
                for case in group['cases'])
            if same_expected else None)
        status = (
            'OK' if group['matches_expected'] and equivalent is not False
            else 'FAIL')
        hint = '等价写法一致' if same_expected else '逐项组合值'
        hint_en = 'all cases identical' if same_expected else 'per-case values'
        draw.rectangle((16, y, CHART_WIDTH - 16, y + GROUP_HEADER_HEIGHT - 4),
                       fill=(238, 240, 245))
        text(
            (24, y + 3),
            f"{group['index'] + 1}. {group['title']}   "
            f"{hint}：{group['expected']}   [{status}]",
            english=(
                f"{group['index'] + 1}. {group['title_en']}   "
                f"{hint_en}: {group['expected']}   [{status}]"),
            fill=(20, 30, 60), font=regular)
        y += GROUP_HEADER_HEIGHT

        for case in group['cases']:
            swatch = render_rgba(case['svg'])
            assert swatch.size == (SWATCH_WIDTH, SWATCH_HEIGHT)
            background = checkerboard(swatch.size)
            background.alpha_composite(swatch)
            thumbnail = background.convert('RGB').resize(
                (SWATCH_DISPLAY_WIDTH, SWATCH_DISPLAY_HEIGHT),
                Image.Resampling.NEAREST)
            chart.paste(thumbnail, (28, y + 5))

            text((112, y + 8), case['label'], english=case['label_en'],
                 fill=(0, 0, 0), font=regular)
            pixels = case['pixels']
            values = ' / '.join(str(pixel) for pixel in pixels)
            text((112, y + 28), f'像素 {values}', english=f'pixels {values}',
                 fill=(60, 60, 60), font=small)
            verdict = 'OK' if case['matches_expected'] else 'FAIL'
            draw.text((CHART_WIDTH - 70, y + 17), verdict,
                      fill=(0, 120, 0) if verdict == 'OK' else (180, 0, 0),
                      font=regular)
            y += ROW_HEIGHT

    y += 12
    draw.line((24, y, CHART_WIDTH - 24, y), fill=(180, 180, 180))
    y += 8
    export_status = (
        'OK' if export_check['identical_png'] and
        export_check['identical_pixels'] else 'FAIL')
    text(
        (24, y),
        '重复导出：同一文档连续渲染两次，PNG 字节与像素均一致 '
        f'[{export_status}]   像素 {export_check["pixels"]}',
        english=(
            'Repeated export: the same document rendered twice gives '
            f'identical PNG bytes and pixels [{export_status}]   '
            f'pixels {export_check["pixels"]}'),
        fill=(0, 100, 0) if export_status == 'OK' else (180, 0, 0),
        font=regular)
    y += 24
    text(
        (24, y),
        '无效颜色回退为不透明黑色（项目既有行为）；百分比通道按 0–100% 解析，'
        '数值通道按 0–255 解析并截断到合法范围。',
        english=(
            'Invalid colors fall back to opaque black (existing project '
            'behavior); percent channels use 0-100% and number channels use '
            '0-255, clamped to the valid range.'),
        fill=(60, 60, 60), font=small)

    chart.save(output_path)


def write_json(cases, equivalence, export_check, output_path):
    """Write the measured pixels and check results as JSON."""
    report = {
        'generator': 'demo/color_comparison.py',
        'chart': {
            'path': 'color_comparison.png',
            'swatch_size': [SWATCH_WIDTH, SWATCH_HEIGHT],
        },
        'groups': [],
        'repeated_export': export_check,
    }
    for group, check in zip(cases, equivalence, strict=True):
        report['groups'].append({
            'title': group['title'],
            'title_en': group['title_en'],
            'expected': list(group['expected']),
            'equivalent': check['equivalent'],
            'matches_expected': check['matches_expected'],
            'cases': [
                {
                    'label': case['label'],
                    'label_en': case['label_en'],
                    'expected': [list(pixel) for pixel in case['expected']],
                    'pixels': [list(pixel) for pixel in case['pixels']],
                    'matches_expected': case['matches_expected'],
                    'svg': case['svg'],
                }
                for case in group['cases']],
        })
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8')


def write_samples(cases, samples_dir):
    """Write the SVG source of every swatch for inspection and reuse."""
    samples_dir.mkdir(parents=True, exist_ok=True)
    for group in cases:
        for case_index, case in enumerate(group['cases']):
            filename = (
                f"{slugify(group['title_en'], group['index'])}-"
                f"{slugify(case['label_en'], case_index)}.svg")
            (samples_dir / filename).write_text(
                case['svg'] + '\n', encoding='utf-8')


def main(argv=None):
    """Generate the chart, the JSON report and the SVG samples."""
    parser = argparse.ArgumentParser(
        description='Generate the reproducible CairoSVG color chart.')
    parser.add_argument(
        '--output-dir', type=Path,
        default=Path(__file__).resolve().parent,
        help='directory receiving the chart, report and samples')
    options = parser.parse_args(argv)
    output_dir = options.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    cases = build_cases()
    equivalence = check_equivalence(cases)
    export_check = check_repeated_export()

    chart_path = output_dir / 'color_comparison.png'
    build_chart(cases, export_check, chart_path)
    write_json(cases, equivalence, export_check,
               output_dir / 'color_comparison.json')
    write_samples(cases, output_dir / 'samples')

    with Image.open(chart_path) as chart:
        chart_size = chart.size
        chart_colors = len(chart.convert('RGB').getcolors(1 << 20) or ())

    success = (
        all(check['equivalent'] is not False for check in equivalence) and
        all(check['matches_expected'] for check in equivalence) and
        export_check['identical_png'] and
        export_check['identical_pixels'] and
        chart_size[0] > 240 and chart_size[1] > 160 and chart_colors > 1)
    print(f'chart: {chart_path} {chart_size[0]}x{chart_size[1]}, '
          f'{chart_colors} distinct colors')
    print(f'report: {output_dir / "color_comparison.json"}')
    print(f'samples: {output_dir / "samples"}')
    print('all checks passed' if success else 'FAILED checks')
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
