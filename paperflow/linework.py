"""智能表格线处理引擎。

设计原则（对应人工校订场景的硬约束）：

1. 先检测、预览、人工确认，再改图；检测阶段不写入任何像素。
2. 区分三类线条：表格线（横/纵成组并形成网格）、下划线（贴近文字基线的单条横线）、
   斜线（仅检测不默认处理），外加颜色/断续等不确定性标注。
3. 交叉处单独评估：能从两侧笔画延续判断为笔画穿越的才允许修补；无法判断时保留原像素，
   标记 uncertain，绝不凭猜测补造数字或笔画。
4. 修补只使用纸张底色（局部估计）与小半径 inpaint，不做统一刷白；局部应用严格限制在
   区域内、区域外像素逐像素不变，局部去线后再整页应用与直接整页应用的结果一致，不会断接。
5. 每条线与每个交叉处都有基于几何的稳定标识；人工保护决策随页面持久化，调整线宽或方向
   参数后按几何重叠重新匹配，决策继续成立。
"""
from __future__ import annotations

import hashlib
import math

import cv2
import numpy as np

from .core import choice, number, require
from .imaging import gray

# 单次分析允许返回的最大线条/交叉处数量，避免异常页面拖垮前端。
MAX_SEGMENTS = 400
MAX_CROSSINGS = 800

# 交叉处判定：与线条垂直方向上，超出线宽两侧多少像素的墨迹视为笔画穿越。
CROSS_PADDING = 3


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------

def _number_input(value, field, low, high):
    """兼容 JSON 数值与查询字符串中的数字文本。"""
    if isinstance(value, str):
        require(value.strip() and not value.startswith(' '), field + '必须是数值')
        try:
            value = float(value)
        except ValueError:
            require(False, field + '必须是数值')
    return number(value, field, low, high, True)


def analyze_params(values):
    values = values or {}
    require(isinstance(values, dict), '去线参数必须为对象')
    orientation = choice(values.get('orientation', 'both'), '线条方向',
                         ('horizontal', 'vertical', 'both', 'diagonal'))
    min_length = _number_input(values.get('min_length', 80), '最短线长', 20, 4000)
    max_gap = _number_input(values.get('max_gap', 6), '断线桥接间隔', 0, 80)
    dash_gap = _number_input(values.get('dash_gap', 24), '断续线桥接间隔', 0, 120)
    include_diagonal = _as_bool(values.get('include_diagonal', False))
    region = values.get('region')
    if region is not None:
        if isinstance(region, str):
            require(region.strip(), '区域不能为空')
            parts = [part.strip() for part in region.split(',')]
            require(len(parts) == 4, '区域需要 x,y,宽,高 四个数值')
            region = parts
        require(isinstance(region, (list, tuple)) and len(region) == 4, '区域需要[x,y,宽,高]')
        region = [int(_number_input(region[i], '区域坐标', 0, 16000)) for i in range(4)]
        require(region[2] >= 32 and region[3] >= 32, '区域宽高至少32像素')
    return {
        'orientation': orientation,
        'min_length': int(min_length),
        'max_gap': int(max_gap),
        'dash_gap': int(dash_gap),
        'include_diagonal': include_diagonal,
        'region': region,
    }


def _as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return False


def _clamp_region(region, height, width):
    if region is None:
        return 0, 0, width, height
    x, y, w, h = region
    x = max(0, min(x, width - 1))
    y = max(0, min(y, height - 1))
    w = max(1, min(w, width - x))
    h = max(1, min(h, height - y))
    return x, y, w, h


# ---------------------------------------------------------------------------
# 墨迹与底色
# ---------------------------------------------------------------------------

def ink_masks(image):
    """返回 (墨迹掩码, 彩色墨迹掩码)。

    Otsu 处理深色墨迹；对亮度高于 Otsu 阈值的彩色细线，用 HSV 饱和度补一层，
    保证有颜色的细线不会因为“不够黑”而漏检，也不会被当成普通灰度统一刷白。
    """
    luminance = gray(image)
    dark = cv2.threshold(luminance, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1]
    value = hsv[:, :, 2]
    # 明显带颜色且不是纯白背景的像素（浅金框、红/蓝细线等）。
    colored = cv2.bitwise_and(
        cv2.threshold(saturation, 40, 255, cv2.THRESH_BINARY)[1],
        cv2.threshold(value, 60, 255, cv2.THRESH_BINARY)[1],
    )
    colored = cv2.morphologyEx(colored, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    ink = cv2.bitwise_or(dark, colored)
    # color_only 用于交叉处判断；完整彩色掩码用于线段着色判定（深色彩线同时落在 dark 中）。
    color_only = cv2.bitwise_and(colored, cv2.bitwise_not(dark))
    return ink, color_only, colored, luminance


def paper_background(image):
    """估计整页纸张底色（含彩色/渐变背景），用于修补与羽化合成。"""
    height, width = image.shape[:2]
    scale = min(1.0, 900 / max(height, width))
    small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    radius = max(15, min(small.shape[:2]) // 20) | 1
    background = cv2.medianBlur(small, radius)
    background = cv2.GaussianBlur(background, (0, 0), max(3, radius / 3))
    return cv2.resize(background, (width, height), interpolation=cv2.INTER_CUBIC)


# ---------------------------------------------------------------------------
# 线条检测
# ---------------------------------------------------------------------------

def _bridge(mask, max_gap):
    """沿线条走向做小额闭运算，把断续扫描线连成同一条，同时不改变线宽。"""
    if max_gap <= 0:
        return mask
    span = max(3, min(max_gap * 2 + 1, 21))
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (span, 1)))


def _line_components(mask, orientation, min_length, max_gap, dash_gap):
    """提取横/纵长条线段。

    关键：先做短核开运算（先腐蚀）取“线芯”，只有连续长度达标的纯直线才能通过，
    汉字/数字的横向笔画在腐蚀阶段被滤掉；随后再沿走向用较大闭运算把断续扫描线
    （虚线）的各段桥成同一根。最后按覆盖率、厚度一致性和长宽比二次校验：
    实线要求高覆盖率，断续线允许低覆盖率但要求厚度极稳定，排除文字笔画偶然连成的假线。
    """
    # 短核保证能发现较短的虚线笔画；文字笔画经短核腐蚀后仍较厚且厚度抖动大。
    # 不随 min_length 放大：min_length 是桥接后整根线的长度，单段虚线可能很短。
    core_span = 13
    if orientation == 'horizontal':
        long_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (core_span, 1))
    else:
        long_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, core_span))
    cores = cv2.morphologyEx(mask, cv2.MORPH_OPEN, long_kernel)
    # 此时 cores 只含通过长核腐蚀的纯线芯，文字笔画已被排除；可以安全地用较大的
    # 沿走向闭运算把虚线/断续扫描线的各段桥成一根（不会把文字接进来）。
    # max_gap 处理实线的小断裂，dash_gap 处理虚线各段之间的较大间隔。
    bridge_span = max(3, min(dash_gap * 2 + 1, max(21, min_length // 2)))
    if orientation == 'horizontal':
        gap_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (bridge_span, 1))
    else:
        gap_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, bridge_span))
    if dash_gap > 0 or max_gap > 0:
        cores = cv2.morphologyEx(cores, cv2.MORPH_CLOSE, gap_kernel)
    count, _, stats, _ = cv2.connectedComponentsWithStats(cores, connectivity=8)
    results = []
    for label in range(1, count):
        x, y, w, h, area = stats[label]
        length = w if orientation == 'horizontal' else h
        thickness = h if orientation == 'horizontal' else w
        if length < min_length or thickness == 0:
            continue
        # 回到原始墨迹上测量该带内的真实形状，避免形态学产物掩盖断线/抖动。
        profile = _thickness_profile(mask, (x, y, w, h), orientation)
        coverage = float(np.count_nonzero(profile)) / max(1, len(profile))
        nonzero = profile[profile > 0]
        mean_thick = float(nonzero.mean()) if nonzero.size else 0.0
        thick_std = float(nonzero.std()) if nonzero.size else 0.0
        aspect = length / max(1.0, mean_thick)
        if aspect < 8 or mean_thick > 16:
            continue
        thickness_stability = thick_std / max(0.8, mean_thick)
        if coverage >= 0.92:
            # 实线：轻微厚度抖动可以接受。
            if thickness_stability > 0.75:
                continue
        else:
            # 断续线/虚线：覆盖率低时必须满足“细线 + 厚度恒定 + 多段等距”，
            # 三者共同把它与汉字笔画的偶然串联区分开（见 _periodic_gaps）。
            if coverage < 0.45:
                continue
            if mean_thick > 2.6 or thickness_stability > 0.18:
                continue
            gap_stats = _gap_stats(profile)
            if gap_stats is None or gap_stats['count'] < 4 or gap_stats['cv'] > 0.35:
                continue
        results.append((x, y, w, h, int(area), max(1, int(round(mean_thick))),
                        round(coverage, 3)))
    return _merge_parallel(results, orientation)


def _thickness_profile(mask, rect, orientation):
    """沿线条走向统计每个位置的墨迹厚度。"""
    x, y, w, h = rect
    band = mask[y:y + h, x:x + w]
    if orientation == 'horizontal':
        return np.count_nonzero(band, axis=0).astype(np.float32)
    return np.count_nonzero(band, axis=1).astype(np.float32)


def _gap_stats(profile):
    """统计走向轮廓上的空白段：数量、均值、变异系数（虚线等距性判据）。"""
    flags = (profile > 0).astype(np.uint8)
    gaps = []
    start = None
    for index, value in enumerate(flags):
        if not value and start is None:
            start = index
        elif value and start is not None:
            gaps.append(index - start)
            start = None
    gaps = [gap for gap in gaps if gap >= 2]
    if len(gaps) < 2:
        return None
    gaps = np.asarray(gaps, dtype=np.float32)
    mean = float(gaps.mean())
    return {'count': len(gaps), 'mean': mean, 'cv': float(gaps.std() / max(1.0, mean))}


def _merge_parallel(components, orientation):
    """合并同一根线被切成的多条平行重叠检测结果。"""
    merged = []
    for component in sorted(components, key=lambda item: -item[2] * item[3]):
        x, y, w, h, area, thickness, coverage = component
        for index, other in enumerate(merged):
            ox, oy, ow, oh, oarea, oth, ocov = other
            if orientation == 'horizontal':
                overlap = max(0, min(x + w, ox + ow) - max(x, ox))
                aligned = overlap >= 0.6 * min(w, ow) and abs((y + h / 2) - (oy + oh / 2)) <= max(h, oh)
            else:
                overlap = max(0, min(y + h, oy + oh) - max(y, oy))
                aligned = overlap >= 0.6 * min(h, oh) and abs((x + w / 2) - (ox + ow / 2)) <= max(w, ow)
            if aligned:
                if orientation == 'horizontal':
                    nx1, nx2 = min(x, ox), max(x + w, ox + ow)
                    ny1, ny2 = min(y, oy), max(y + h, oy + oh)
                else:
                    nx1, nx2 = min(x, ox), max(x + w, ox + ow)
                    ny1, ny2 = min(y, oy), max(y + h, oy + oh)
                merged[index] = (nx1, ny1, nx2 - nx1, ny2 - ny1, area + oarea,
                                 max(thickness, oth), max(coverage, ocov))
                break
        else:
            merged.append(component)
    return merged


def _band_mask(shape, rect, orientation, endpoints=None):
    """线条带状区域掩码（含垂直方向少量外扩，覆盖抗锯齿边缘）。"""
    x, y, w, h = rect
    if orientation == 'diagonal' and endpoints is not None:
        mask = np.zeros(shape, np.uint8)
        # 斜线用端点多边形表示，避免矩形框把无关内容圈入。
        (x1, y1), (x2, y2) = endpoints
        dx, dy = x2 - x1, y2 - y1
        norm = max(1.0, math.hypot(dx, dy))
        nx, ny = -dy / norm, dx / norm
        pad = 2
        polygon = np.array([
            [x1 + nx * pad, y1 + ny * pad],
            [x2 + nx * pad, y2 + ny * pad],
            [x2 - nx * pad, y2 - ny * pad],
            [x1 - nx * pad, y1 - ny * pad],
        ], np.int32)
        cv2.fillPoly(mask, [polygon], 255)
        return mask
    mask = np.zeros(shape, np.uint8)
    if orientation == 'horizontal':
        mask[max(0, y - 1):y + h + 1, x:x + w] = 255
    else:
        mask[y:y + h, max(0, x - 1):x + w + 1] = 255
    return mask


def _signature(rect, orientation):
    """基于几何的稳定标识：参数微调后只要位置基本不变，标识即可复现。"""
    x, y, w, h = rect
    # 量化到 8 像素网格，容忍 1–2 像素的检测抖动。
    basis = (orientation, round(x / 8), round(y / 8), round(w / 16), round(h / 4))
    return 'ln_' + hashlib.sha1('|'.join(map(str, basis)).encode()).hexdigest()[:16]


def detect_segments(image, params):
    """检测横/纵/斜线段落，返回段落字典列表。"""
    height, width = image.shape[:2]
    x0, y0, rw, rh = _clamp_region(params['region'], height, width)
    ink, color_only, colored, luminance = ink_masks(image)
    scope = np.zeros((height, width), np.uint8)
    scope[y0:y0 + rh, x0:x0 + rw] = 255
    ink = cv2.bitwise_and(ink, scope)
    color_only = cv2.bitwise_and(color_only, scope)
    colored = cv2.bitwise_and(colored, scope)

    segments = []
    orientations = []
    if params['orientation'] in ('horizontal', 'both'):
        orientations.append('horizontal')
    if params['orientation'] in ('vertical', 'both'):
        orientations.append('vertical')

    raw = []
    for orientation in orientations:
        for x, y, w, h, area, thickness, coverage in _line_components(
                ink, orientation, params['min_length'], params['max_gap'],
                params['dash_gap']):
            raw.append({
                'orientation': orientation,
                'rect': [int(x), int(y), int(w), int(h)],
                'thickness': max(1, thickness),
                'length': int(w if orientation == 'horizontal' else h),
                'core_coverage': coverage,
            })

    # Hough 补充斜线（含倾斜表格线）。默认只报告不动手，交由人工确认。
    diagonals = []
    if params['include_diagonal'] or params['orientation'] == 'diagonal':
        diagonals = _detect_diagonals(ink, params['min_length'])

    if not raw and not diagonals:
        return [], ink, color_only, colored

    # 用段落带状掩码与彩色掩码、网格关系做分类。
    h_bands = [s for s in raw if s['orientation'] == 'horizontal']
    v_bands = [s for s in raw if s['orientation'] == 'vertical']
    h_mask = _union_bands((height, width), h_bands, 'horizontal')
    v_mask = _union_bands((height, width), v_bands, 'vertical')

    for segment in raw:
        rect = segment['rect']
        orientation = segment['orientation']
        band = _band_mask((height, width), rect, orientation)
        # 用完整彩色掩码统计：深色但带颜色的线（深红/深蓝）也要识别为彩色细线。
        colored_ratio = float(np.count_nonzero(cv2.bitwise_and(band, colored))
                              / max(1, np.count_nonzero(band)))
        # 断续度：线芯覆盖率不足说明存在扫描断线/虚线（已按 max_gap 桥接后仍不连续）。
        covered = segment.pop('core_coverage')
        broken = covered < 0.92
        crosses_grid = bool(np.count_nonzero(cv2.bitwise_and(band, v_mask if orientation == 'horizontal' else h_mask)))
        uncertain_line = colored_ratio > 0.25 or broken
        kind, kind_reason = _classify(segment, h_mask, v_mask, luminance, ink)
        segment.update({
            'id': _signature(rect, orientation),
            'kind': kind,
            'kind_reason': kind_reason,
            'colored': colored_ratio > 0.25,
            'colored_ratio': round(colored_ratio, 3),
            'broken': broken,
            'coverage': round(covered, 3),
            'crosses_grid': crosses_grid,
            # 彩色/断续线可能是批注或扫描噪声，默认保留并标记不确定，由人工确认。
            'decision': 'keep' if uncertain_line else 'remove',
            'uncertain': uncertain_line,
        })
        segments.append(segment)

    for diagonal in diagonals:
        diagonal['id'] = _signature(diagonal['rect'], 'diagonal')
        diagonal.update({
            'kind': 'diagonal',
            'kind_reason': '斜向线段，工具不默认处理，需要人工确认',
            'colored': False,
            'broken': False,
            'coverage': 1.0,
            'crosses_grid': False,
            'decision': 'keep',
            'uncertain': True,
        })
        segments.append(diagonal)

    # 同标识去重（不同参数下可能重复命中），保留信息更丰富的一条。
    deduped = {}
    for segment in segments:
        if segment['id'] not in deduped:
            deduped[segment['id']] = segment
    segments = list(deduped.values())[:MAX_SEGMENTS]
    segments.sort(key=lambda item: (item['rect'][1], item['rect'][0]))
    return segments, ink, color_only, colored


def _union_bands(shape, segments, orientation):
    mask = np.zeros(shape, np.uint8)
    for segment in segments:
        mask = cv2.bitwise_or(mask, _band_mask(shape, segment['rect'], orientation))
    return mask


def _classify(segment, h_mask, v_mask, luminance, ink):
    """表格线 vs 下划线。

    表格线：与垂直方向线段相交成网格，或长度跨越页面较大比例且为孤立直线；
    下划线：横线、不构成网格、带状区域上沿附近存在贴近的文字墨迹（压在线上的字）。
    """
    rect = segment['rect']
    x, y, w, h = rect
    orientation = segment['orientation']
    if orientation == 'vertical':
        return 'table', '纵向直线，属于表格结构'
    band = _band_mask(luminance.shape, rect, 'horizontal')
    if np.count_nonzero(cv2.bitwise_and(band, v_mask)) > 0:
        return 'table', '与纵向线相交形成表格网格'
    # 检查线上方 1.2 倍字高范围内是否有文字墨迹（下划线贴着文字基线）。
    height, width = luminance.shape
    probe = np.zeros_like(ink)
    top = max(0, y - max(24, h * 6))
    probe[top:y, x:min(width, x + w)] = 255
    nearby = np.count_nonzero(cv2.bitwise_and(ink, probe))
    if nearby > max(20, w * 0.4):
        return 'underline', '横线贴近上方文字基线，判定为下划线'
    if w >= width * 0.45:
        return 'rule', '跨越页面较大比例的长直线，按表格结构候选处理'
    return 'rule', '独立横线，未发现网格或下划线特征'


def _detect_diagonals(ink, min_length):
    edges = cv2.Canny(ink, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180,
                            threshold=max(50, min_length // 2),
                            minLineLength=min_length, maxLineGap=8)
    results = []
    if lines is None:
        return results
    for x1, y1, x2, y2 in lines[:, 0]:
        # 倾斜角归一化到 [0,180)：0=水平，90=垂直；只保留真正斜的（偏离横纵 ≥12°）。
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180
        deviation = min(angle, abs(angle - 180), abs(angle - 90))
        if deviation < 12:
            continue
        x, y = min(x1, x2), min(y1, y2)
        w, h = abs(x2 - x1) + 1, abs(y2 - y1) + 1
        length = int(math.hypot(x2 - x1, y2 - y1))
        if length < min_length:
            continue
        results.append({
            'orientation': 'diagonal',
            'rect': [int(x), int(y), int(w), int(h)],
            'endpoints': [[int(x1), int(y1)], [int(x2), int(y2)]],
            'thickness': 2,
            'length': length,
            'angle': round(float(angle if angle <= 90 else angle - 180), 2),
        })
    # 合并同一根斜线的多条 Hough 响应：角度接近且端点距离小即视为同一根。
    merged = []
    for item in sorted(results, key=lambda r: -r['length']):
        duplicate = False
        for other in merged:
            if abs(item['angle'] - other['angle']) > 6:
                continue
            p1, p2 = item['endpoints']
            q1, q2 = other['endpoints']
            d1 = min(math.dist(p1, q1), math.dist(p1, q2))
            d2 = min(math.dist(p2, q1), math.dist(p2, q2))
            if d1 < 40 and d2 < 40:
                duplicate = True
                break
        if not duplicate:
            merged.append(item)
    return merged[:40]


def _rect_iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / max(1, union)


# ---------------------------------------------------------------------------
# 交叉处风险评估
# ---------------------------------------------------------------------------

def detect_crossings(image, segments, ink, color_only):
    """评估线段与文字笔画的交叉处。

    思路：取每条线的带状区域，向线宽两侧外扩若干像素，找带外仍有墨迹的连通域，
    这些就是“压在线上”的笔画/字符。两侧都有同方向延续且线较细时可安全修补；
    只有一侧有墨迹、或线宽接近笔画宽度时无法推断被遮住的内容，标记 uncertain。
    网格自身的横纵线交点不算交叉处（那是表格结构，随线一起去除）。
    """
    height, width = ink.shape
    crossings = []
    straight = [s for s in segments if s['orientation'] in ('horizontal', 'vertical')]
    h_lines = [s for s in straight if s['orientation'] == 'horizontal']
    v_lines = [s for s in straight if s['orientation'] == 'vertical']
    for segment in straight:
        rect = segment['rect']
        x, y, w, h = rect
        orientation = segment['orientation']
        thickness = segment['thickness']
        if orientation == 'horizontal':
            # 外扩到线宽两侧，包含压在线上的笔画。
            x1, x2 = x, x + w
            y1, y2 = max(0, y - CROSS_PADDING), min(height, y + h + CROSS_PADDING)
            above = ink[max(0, y - 9):y, x1:x2]
            below = ink[y + h:min(height, y + h + 9), x1:x2]
            col_has = np.zeros(w, np.uint8)
            if above.size:
                col_has |= (np.count_nonzero(above, axis=0) > 0).astype(np.uint8)
            if below.size:
                col_has |= (np.count_nonzero(below, axis=0) > 0).astype(np.uint8)
            # 网格纵线穿过的位置：纵线自身的带状墨迹，不是文字笔画。
            junction = np.zeros(w, np.uint8)
            for vline in v_lines:
                vx, vy, vw, vh = vline['rect']
                if vy <= y + h and vy + vh >= y:
                    lo = max(0, vx - x - 2)
                    hi = min(w, vx - x + vw + 2)
                    junction[lo:hi] = 1
        else:
            x1, x2 = max(0, x - CROSS_PADDING), min(width, x + w + CROSS_PADDING)
            y1, y2 = y, y + h
            left = ink[y1:y2, max(0, x - 9):x]
            right = ink[y1:y2, x + w:min(width, x + w + 9)]
            row_has = np.zeros(h, np.uint8)
            if left.size:
                row_has |= (np.count_nonzero(left, axis=1) > 0).astype(np.uint8)
            if right.size:
                row_has |= (np.count_nonzero(right, axis=1) > 0).astype(np.uint8)
            col_has = row_has  # 统一变量名，下面按走向解释
            junction = np.zeros(h, np.uint8)
            for hline in h_lines:
                hx, hy, hw, hh = hline['rect']
                if hx <= x + w and hx + hw >= x:
                    lo = max(0, hy - y - 2)
                    hi = min(h, hy - y + hh + 2)
                    junction[lo:hi] = 1
        if not np.any(col_has):
            continue
        # 把连续的穿越列聚成交叉块，同时剔除纯网格交点区间。
        spans = _runs(cv2.bitwise_and(col_has, cv2.bitwise_not(junction)))
        for start, span in spans:
            if span < 2:
                continue
            if orientation == 'horizontal':
                cx, cy = x1 + start, y1
                cw, ch = span, y2 - y1
                above_pixels = int(np.count_nonzero(above[:, start:start + span])) if above.size else 0
                below_pixels = int(np.count_nonzero(below[:, start:start + span])) if below.size else 0
            else:
                cx, cy = x1, y1 + start
                cw, ch = x2 - x1, span
                above_pixels = int(np.count_nonzero(left[start:start + span, :])) if left.size else 0
                below_pixels = int(np.count_nonzero(right[start:start + span, :])) if right.size else 0
            two_sided = above_pixels > 0 and below_pixels > 0
            # 线宽越接近笔画（约 2–4px），越无法判断覆盖内容。
            thick_risk = thickness >= 4
            colored = bool(np.count_nonzero(color_only[cy:cy + ch, cx:cx + cw]))
            repairable = two_sided and not thick_risk
            risk = 'low' if repairable else ('high' if thick_risk or not two_sided else 'medium')
            crossings.append({
                'id': _cross_id(segment['id'], cx, cy, cw, ch),
                'segment_id': segment['id'],
                'rect': [int(cx), int(cy), int(cw), int(ch)],
                'orientation': orientation,
                'risk': risk,
                'repairable': bool(repairable),
                'two_sided': two_sided,
                'colored': colored,
                'reason': _cross_reason(two_sided, thick_risk, colored),
                'decision': 'remove' if repairable else 'keep',
            })
    # 合并不同线段在网格交点附近报出的重复交叉。
    return _merge_crossings(crossings)[:MAX_CROSSINGS]


def _runs(flags):
    spans = []
    start = None
    for index, value in enumerate(flags):
        if value and start is None:
            start = index
        elif not value and start is not None:
            spans.append((start, index - start))
            start = None
    if start is not None:
        spans.append((start, len(flags) - start))
    return spans


def _cross_reason(two_sided, thick_risk, colored):
    if thick_risk:
        return '线条较粗，被遮住的笔画无法从图像推断，保留原像素'
    if not two_sided:
        return '仅检测到单侧笔画，无法确认被线覆盖的内容，保留原像素'
    if colored:
        return '交叉处含彩色墨迹，按不确定处理'
    return '两侧笔画延续清晰，可按纸张底色安全修补'


def _cross_id(segment_id, x, y, w, h):
    basis = (segment_id, round(x / 6), round(y / 6), round(w / 4), round(h / 4))
    return 'cr_' + hashlib.sha1('|'.join(map(str, basis)).encode()).hexdigest()[:16]


def _merge_crossings(crossings):
    kept = []
    for crossing in crossings:
        duplicate = None
        for other in kept:
            if other['orientation'] != crossing['orientation']:
                continue
            if _rect_iou(crossing['rect'], other['rect']) > 0.25:
                duplicate = other
                break
        if duplicate is None:
            kept.append(crossing)
        else:
            # 保留风险更高的一条，避免低风险条目掩盖人工需要关注的位置。
            order = {'low': 0, 'medium': 1, 'high': 2}
            if order[crossing['risk']] > order[duplicate['risk']]:
                kept.remove(duplicate)
                kept.append(crossing)
    kept.sort(key=lambda item: (item['rect'][1], item['rect'][0]))
    return kept


# ---------------------------------------------------------------------------
# 人工决策匹配（参数调整后仍成立）
# ---------------------------------------------------------------------------

def reapply_decisions(segments, crossings, saved):
    """把历史人工决策按几何重叠重新匹配到本次检测结果。"""
    if not saved:
        return segments, crossings
    saved_segments = {item['id']: item for item in saved.get('segments', [])}
    saved_crossings = {item['id']: item for item in saved.get('crossings', [])}

    for segment in segments:
        match = _match_saved(segment, saved_segments)
        if match is not None and 'decision' in match:
            segment['decision'] = match['decision']
            segment['decision_source'] = 'saved'
    for crossing in crossings:
        match = _match_saved(crossing, saved_crossings)
        if match is not None and 'decision' in match:
            crossing['decision'] = match['decision']
            crossing['decision_source'] = 'saved'
    return segments, crossings


def _match_saved(item, saved_map):
    if item['id'] in saved_map:
        return saved_map[item['id']]
    rect = item['rect']
    best, best_iou = None, 0.35
    for saved in saved_map.values():
        if saved.get('orientation') != item['orientation']:
            continue
        score = _rect_iou(rect, saved['rect'])
        if score > best_iou:
            best, best_iou = saved, score
    return best


# ---------------------------------------------------------------------------
# 修补执行
# ---------------------------------------------------------------------------

def build_removal_mask(image, segments, crossings, region, include_uncertain=False):
    """构造最终删除掩码。

    - 仅包含 decision == 'remove' 的线段；
    - 交叉处若被保护（keep），从掩码中挖掉，原像素（含被线遮住的笔画）原样保留；
    - 掩码只覆盖真实墨迹及其抗锯齿边缘，不做整片刷白。
    """
    height, width = image.shape[:2]
    ink, _, _, _ = ink_masks(image)
    mask = np.zeros((height, width), np.uint8)
    removed_segments = []
    for segment in segments:
        if segment.get('decision') != 'remove':
            continue
        if segment['orientation'] == 'diagonal' and not include_uncertain:
            # 斜线默认永不自动删除。
            continue
        band = _band_mask((height, width), segment['rect'], segment['orientation'],
                          segment.get('endpoints'))
        mask = cv2.bitwise_or(mask, band)
        removed_segments.append(segment['id'])

    kept_crossings = []
    for crossing in crossings:
        x, y, w, h = crossing['rect']
        if crossing.get('decision') == 'keep':
            # 只保留交叉框内的真实墨迹（被线遮住的笔画），框内的线条像素照常删除；
            # 这样人工保护的是“可能的笔画”，而不是把整段线留下来形成残桩。
            pad = 1
            x1, y1 = max(0, x - pad), max(0, y - pad)
            x2, y2 = min(width, x + w + pad), min(height, y + h + pad)
            local = np.zeros((height, width), np.uint8)
            local[y1:y2, x1:x2] = 255
            stroke = cv2.bitwise_and(local, ink)
            mask = cv2.bitwise_and(mask, cv2.bitwise_not(stroke))
            kept_crossings.append(crossing['id'])

    # 只删除真实墨迹像素：掩码 AND 墨迹，避免把背景纹理/底色抹成白斑。
    mask = cv2.bitwise_and(mask, ink)
    if region is not None:
        x0, y0, rw, rh = _clamp_region(region, height, width)
        scope = np.zeros((height, width), np.uint8)
        # 与整页应用使用同一张纸底色估计；区域边界在 render_result 中按硬边界处理。
        scope[y0:y0 + rh, x0:x0 + rw] = 255
        mask = cv2.bitwise_and(mask, scope)
    return mask, removed_segments, kept_crossings


def render_result(image, mask, background=None, region=None, inpaint_radius=2):
    """按掩码生成修补结果。

    非交叉处直接替换为局部纸底色；低风险交叉处用小半径 TELEA inpaint 连通两侧笔画。
    区域模式严格限制在区域内替换，区域外像素逐像素不变；区域内修补块用距离羽化
    与纸面融合，避免白斑，区域边界硬截断、不残留灰尾。
    """
    result = image.copy()
    if not np.any(mask):
        return result
    if background is None:
        background = paper_background(image)

    # 小半径 inpaint 只用于笔画桥接；半径受限于线宽，避免粗线附近把细字糊成一团。
    inpainted = cv2.inpaint(image, mask, max(1, min(3, inpaint_radius)), cv2.INPAINT_TELEA)
    paper_fill = background.copy()

    if region is None:
        # 整页：掩码内像素 = inpaint 与纸底色的加权，越靠近掩码边缘越信任纸底色，
        # 越靠近中心（笔画桥接处）越信任 inpaint。
        distance = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
        confidence = np.clip(distance / 4.0, 0, 1)[..., None]
        blended = (inpainted.astype(np.float32) * confidence
                   + paper_fill.astype(np.float32) * (1 - confidence))
        selected = mask > 0
        result[selected] = np.clip(blended[selected], 0, 255).astype(np.uint8)
        return result

    # 区域模式：只在区域内部替换像素，区域外完全不动，保证局部应用与未处理区域
    # 不会互相污染。区域边界处按硬边界处理（线段在边界被明确截断，不生成灰尾）；
    # 修补块与纸面之间用距离羽化，避免出现生硬白斑。
    height, width = image.shape[:2]
    x0, y0, rw, rh = _clamp_region(region, height, width)
    local_mask = np.zeros((height, width), np.uint8)
    local_mask[y0:y0 + rh, x0:x0 + rw] = 255
    local_mask = cv2.bitwise_and(local_mask, mask)
    # alpha 在区域边界强制为 1：跨区域边界的线段在边界处干净截断，不残留灰线。
    alpha = np.clip(cv2.distanceTransform(local_mask, cv2.DIST_L2, 3) / 2.0, 0, 1)
    alpha[local_mask > 0] = np.maximum(alpha[local_mask > 0], 1.0)

    distance = cv2.distanceTransform(local_mask, cv2.DIST_L2, 3)
    confidence = np.clip(distance / 4.0, 0, 1)
    blended = (inpainted.astype(np.float32) * confidence[..., None]
               + paper_fill.astype(np.float32) * (1 - confidence[..., None]))
    alpha3 = alpha[..., None]
    result = (result.astype(np.float32) * (1 - alpha3)
              + np.clip(blended, 0, 255) * alpha3)
    return np.clip(result, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# 汇总分析
# ---------------------------------------------------------------------------

def analyze(image, params, saved_decisions=None):
    params = analyze_params(params)
    segments, ink, color_only, colored = detect_segments(image, params)
    crossings = detect_crossings(image, segments, ink, color_only)
    segments, crossings = reapply_decisions(segments, crossings, saved_decisions)
    summary = {
        'segments_total': len(segments),
        'segments_remove': sum(1 for s in segments if s.get('decision') == 'remove'),
        'segments_keep': sum(1 for s in segments if s.get('decision') == 'keep'),
        'crossings_total': len(crossings),
        'crossings_kept': sum(1 for c in crossings if c.get('decision') == 'keep'),
        'crossings_uncertain': sum(1 for c in crossings if c['risk'] != 'low'),
        'colored_segments': sum(1 for s in segments if s.get('colored')),
        'broken_segments': sum(1 for s in segments if s.get('broken')),
        'diagonal_segments': sum(1 for s in segments if s['orientation'] == 'diagonal'),
    }
    return {
        'params': params,
        'segments': segments,
        'crossings': crossings,
        'summary': summary,
    }


def apply_plan(image, plan, background=None):
    """按确认过的方案执行，返回 (结果图, 执行元数据)。"""
    require(isinstance(plan, dict), '去线方案必须为对象')
    params = analyze_params(plan.get('params'))
    segments = _validate_decisions(plan.get('segments'), 'segment')
    crossings = _validate_decisions(plan.get('crossings'), 'crossing')
    for segment in segments:
        require(segment['orientation'] in ('horizontal', 'vertical', 'diagonal'), '线条方向无效')
    for crossing in crossings:
        require(crossing['orientation'] in ('horizontal', 'vertical'), '交叉处方向无效')
    mask, removed, kept = build_removal_mask(image, segments, crossings, params['region'])
    if background is None:
        background = paper_background(image)
    result = render_result(image, mask, background, params['region'])
    uncertain_removed = [
        item['id'] for item in segments
        if item.get('decision') == 'remove' and item.get('uncertain')
    ]
    metadata = {
        'tool': 'line_work',
        'params': params,
        'removed_segments': removed,
        'kept_crossings': kept,
        'uncertain_segments_removed': uncertain_removed,
        'pixels_changed': int(np.count_nonzero(mask)),
    }
    return result, metadata


def _validate_decisions(items, label):
    if items is None:
        return []
    require(isinstance(items, list), label + '决策必须为列表')
    require(len(items) <= MAX_SEGMENTS + MAX_CROSSINGS, label + '数量超出限制')
    normalized = []
    for item in items:
        require(isinstance(item, dict), label + '决策必须为对象')
        require(isinstance(item.get('id'), str) and len(item['id']) <= 64, label + '标识无效')
        require(item.get('decision') in ('remove', 'keep'), label + '决策无效')
        rect = item.get('rect')
        require(isinstance(rect, (list, tuple)) and len(rect) == 4, label + '坐标无效')
        rect = [int(number(v, '坐标', 0, 16000, True)) for v in rect]
        normalized.append({**item, 'rect': rect})
    return normalized


def overlay_preview(image, report, width=1200):
    """生成带标注的预览图：线段按类别着色，交叉处按风险标红/黄/绿。"""
    height, width_full = image.shape[:2]
    canvas = image.copy()
    colors = {
        'table': (198, 137, 56),      # 橙蓝(BGR)：表格线
        'underline': (72, 176, 96),   # 绿：下划线
        'rule': (90, 150, 200),       # 蓝：独立直线
        'diagonal': (60, 60, 210),    # 红：斜线
    }
    for segment in report['segments']:
        x, y, w, h = segment['rect']
        if segment.get('decision') == 'keep':
            # 保留项（彩色/断续/斜线/人工保留）用灰色虚线提示“不会动”，仍可见可切换。
            color = (150, 150, 150)
        else:
            color = colors.get(segment['kind'], (90, 150, 200))
        cv2.rectangle(canvas, (x, y), (x + w, y + h), color, thickness=2)
    risk_colors = {'low': (72, 176, 96), 'medium': (40, 200, 230), 'high': (60, 60, 210)}
    for crossing in report['crossings']:
        x, y, w, h = crossing['rect']
        if crossing.get('decision') == 'keep':
            # 保护的交叉处：醒目的品红描边。
            cv2.rectangle(canvas, (x - 1, y - 1), (x + w + 1, y + h + 1), (200, 40, 200), 2)
        else:
            cv2.rectangle(canvas, (x, y), (x + w, y + h), risk_colors[crossing['risk']], 2)
    return canvas
