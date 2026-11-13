"""基于文字证据的倾斜分析。

设计原则（对应自动纠偏的已知失效模式）：

1. 只把连通文字组件（字符/笔画块）作为倾斜证据，表格长线、插图边框、
   粗下划线等长直线单独识别并标记，绝不参与正文角度估计；
2. 把页面切成网格，逐块判断横排/竖排并给出局部角度，页面结论由文字块
   投票聚合，局部方向冲突时显式暴露而不是用最长的线一锤定音；
3. 文字证据不足时状态为 ``undetermined``、置信度为 0，不输出看似确定的
   角度，调用方必须能选择“保留原状”；
4. 所有角度统一为“视觉度”：正角表示内容相对水平/垂直方向顺时针倾斜；
   实测 OpenCV ``getRotationMatrix2D`` 的正角对应视觉逆时针。横排拟合角
   相对水平轴、竖排拟合角相对竖直轴，二者几何方向相反，因此横排校正角与
   检测角同号、竖排校正角与检测角反号，分析与实际变换共用这一套约定，
   避免“检测对、应用反”。
"""
from __future__ import annotations

import math

import cv2
import numpy as np

from .core import number, require
from .imaging import gray, rectangle

GRID = 4
MIN_TILE_GLYPHS = 20
MIN_TILE_LINES = 2
MAX_TILT = 20.0
# 图块角度离散度/覆盖率门槛：超标的块证据不稳定（书页弯曲、跨栏边缘等），
# 只能作为弱参考，不参与页面方向判定与冲突统计
MAX_TILE_MAD = 6.0
MIN_TILE_COVERAGE = 0.55
# 次方向至少有多少个稳定图块才算“局部方向冲突”，单块噪声不报警
MIN_CONFLICT_TILES = 2
# 小于该残差（度）视为已经水平，避免在噪声里反复微转
STRAIGHT_LIMIT = 0.05
# 可信文字块的判定阈值
MIN_CREDIBLE_TILES = 2
# 长直线相对画面长边的最小长度占比
RULED_LENGTH_RATIO = 0.32
RULED_FILL_RATIO = 0.7
# 长直线的厚度窗口（缩小图坐标）：小于 2px 的多为文字行偶然连通，
# 超过 7px 的更可能是照片背景纹理、插图色块而不是纸面细线
MIN_RULED_THICKNESS = 2.0
MAX_RULED_THICKNESS = 7.0
# 长度/厚度比低于该值的更像插图边框粗块而不是细线
MIN_RULED_ASPECT = 35.0


def _text_mask(luminance):
    """自适应阈值得到文字候选，并剥离极细噪点。"""
    mask = cv2.adaptiveThreshold(
        luminance, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 35, 15,
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)


def _strong_mask(luminance):
    """Otsu 与自适应结果合并，用于长直线等强结构检测。"""
    adaptive = cv2.adaptiveThreshold(
        luminance, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, 35, 15,
    )
    _, otsu = cv2.threshold(luminance, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    return cv2.bitwise_or(adaptive, otsu)


def _components(mask):
    """返回文字连通组件（x, y, w, h, area, cx, cy），过滤大块与噪点。"""
    height, width = mask.shape
    count, _, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    result = []
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if area < 6 or area > 0.02 * height * width:
            continue
        if w < 2 or h < 2 or w > width * 0.5 or h > height * 0.5:
            continue
        result.append((
            int(x), int(y), int(w), int(h), int(area),
            float(centroids[index][0]), float(centroids[index][1]),
        ))
    return result


def _group_bands(coordinates, gap):
    """把一维坐标按间隙聚类成若干条带（文字行/文字列）。"""
    order = np.argsort(coordinates)
    groups = [[int(order[0])]]
    for previous, current in zip(order[:-1], order[1:]):
        if coordinates[current] - coordinates[previous] > gap:
            groups.append([int(current)])
        else:
            groups[-1].append(int(current))
    return groups


def _fit_line(points, vertical):
    """对组件质心拟合直线，返回视觉倾斜角与中位残差（像素）。"""
    points = np.asarray(points, dtype=np.float64)
    if len(points) < 3:
        return None, 1e9
    if not vertical:
        slope, intercept = np.polyfit(points[:, 0], points[:, 1], 1)
        angle = math.degrees(math.atan(slope))
        residual = np.polyval([slope, intercept], points[:, 0]) - points[:, 1]
    else:
        slope, intercept = np.polyfit(points[:, 1], points[:, 0], 1)
        angle = math.degrees(math.atan(slope))
        residual = np.polyval([slope, intercept], points[:, 1]) - points[:, 0]
    return angle, float(np.median(np.abs(residual)))


def _tile_estimate(components_list, rect, tile_index):
    """估计单个网格块的文字方向与倾斜角。

    横排、竖排各自尝试按质心投影聚成文字行/列，拟合行（列）中心线，
    用覆盖率、行数、角度离散度和残差给两种假设打分，取更可信的一种。
    """
    x0, y0, width, height = rect
    selected = [
        component for component in components_list
        if x0 <= component[5] < x0 + width and y0 <= component[6] < y0 + height
    ]
    if len(selected) < MIN_TILE_GLYPHS:
        return None
    median_h = float(np.median([component[3] for component in selected]))
    median_w = float(np.median([component[2] for component in selected]))
    best = None
    for vertical in (False, True):
        coordinates = np.array([
            component[6] if not vertical else component[5]
            for component in selected
        ])
        gap = 0.9 * (median_h if not vertical else median_w)
        bands = _group_bands(coordinates, gap)
        bands = [band for band in bands if len(band) >= 3]
        if len(bands) < MIN_TILE_LINES:
            continue
        angles = []
        residuals = []
        used = set()
        for band in bands:
            points = [(selected[i][5], selected[i][6]) for i in band]
            angle, residual = _fit_line(points, vertical)
            if angle is None or abs(angle) > MAX_TILT:
                continue
            angles.append(angle)
            residuals.append(residual)
            used.update(band)
        if len(angles) < MIN_TILE_LINES:
            continue
        angles = np.asarray(angles)
        median_angle = float(np.median(angles))
        mad = float(np.median(np.abs(angles - median_angle)))
        coverage = len(used) / len(selected)
        score = coverage * len(angles) / (1.0 + mad + max(0.5, float(np.mean(residuals))))
        if best is None or score > best['score']:
            best = {
                'tile': tile_index,
                'rect': [round(x0, 1), round(y0, 1), round(width, 1), round(height, 1)],
                'orientation': 'vertical' if vertical else 'horizontal',
                'angle': round(median_angle, 3),
                'mad': round(mad, 3),
                'glyphs': len(selected),
                'text_lines': len(angles),
                'coverage': round(coverage, 3),
                'score': round(float(score), 4),
                'reliable': mad <= MAX_TILE_MAD and coverage >= MIN_TILE_COVERAGE,
            }
    return best


def _overlaps_text_components(x1, y1, x2, y2, components_list, threshold=0.08):
    """判断候选长线是否穿过密集文字组件带（即它其实是文字行而非表格线）。

    统计质心落在直线附近（约一个字高的条带内）的组件数量，按线段长度
    归一化；真实表格线沿线只有极少量组件，文字行沿线则布满字符。
    """
    if not components_list:
        return False
    dx = float(x2 - x1)
    dy = float(y2 - y1)
    norm = max(1.0, math.hypot(dx, dy))
    nx, ny = -dy / norm, dx / norm
    near = 0
    for component in components_list:
        cx, cy = component[5], component[6]
        along = ((cx - x1) * dx + (cy - y1) * dy) / norm
        if along < -8 or along > norm + 8:
            continue
        distance = abs((cx - x1) * nx + (cy - y1) * ny)
        if distance <= max(6.0, 1.6 * float(np.median([c[3] for c in components_list]))):
            near += 1
    return near / (norm / 40.0) > threshold


def _detect_ruled_lines(mask, scale_x, scale_y, components_list=None):
    """提取贯穿画面的长直线（表格线、插图边框、粗下划线）。

    形态学长核对斜线无效，因此改用 Hough 线段候选，再沿线段法线采样
    验证支撑率与厚度；普通文字行由断续字符组成，会在“组件密度”剪枝
    中被剔除（真实细线沿线组件很少，文字行沿线布满字符组件）。每条线
    附带长度、厚度与填充情况，界面可说明它为何没有被当作正文证据。
    """
    height, width = mask.shape
    span = max(height, width)
    minimum_length = max(40, int(span * RULED_LENGTH_RATIO))
    lines = cv2.HoughLinesP(
        mask, 1, np.pi / 1800, threshold=60,
        minLineLength=minimum_length, maxLineGap=18,
    )
    candidates = []
    if lines is not None:
        for x1, y1, x2, y2 in lines[:, 0]:
            dx = float(x2 - x1)
            dy = float(y2 - y1)
            length = math.hypot(dx, dy)
            if length < minimum_length:
                continue
            angle = math.degrees(math.atan2(dy, dx))
            vertical = abs(angle) > 45
            if vertical:
                # 换算成相对竖直轴的倾斜角，落在 (-45, 45]
                angle = angle - 90 if angle > 45 else angle + 90
            if abs(angle) > MAX_TILT:
                continue
            samples = 40
            sample_x = np.linspace(x1, x2, samples)
            sample_y = np.linspace(y1, y2, samples)
            normal_x = -dy / length
            normal_y = dx / length
            hits = 0
            thicknesses = []
            for px, py in zip(sample_x, sample_y):
                offsets = []
                for offset in range(-9, 10):
                    qx = int(round(px + normal_x * offset))
                    qy = int(round(py + normal_y * offset))
                    if 0 <= qx < width and 0 <= qy < height and mask[qy, qx]:
                        offsets.append(abs(offset))
                if offsets:
                    hits += 1
                    thicknesses.append(max(offsets) - min(offsets) + 1)
            support = hits / samples
            thickness = float(np.median(thicknesses)) if thicknesses else 99.0
            if support < RULED_FILL_RATIO or thickness < MIN_RULED_THICKNESS or thickness > MAX_RULED_THICKNESS:
                continue
            if length / max(thickness, 1.0) < MIN_RULED_ASPECT:
                continue
            if _overlaps_text_components(x1, y1, x2, y2, components_list):
                continue
            candidates.append({
                'orientation': 'vertical' if vertical else 'horizontal',
                'angle': round(float(angle), 3),
                'length': round(length / (scale_y if vertical else scale_x), 1),
                'thickness': round(thickness / (scale_x if vertical else scale_y), 1),
                'fill_ratio': round(support, 3),
                'endpoints': [int(x1), int(y1), int(x2), int(y2)],
            })
    # 同一物理直线会被多次检出，按方向、角度与中点距离去重，保留最长者
    ruled = []
    for item in sorted(candidates, key=lambda entry: -entry['length']):
        duplicate = None
        mid_x = (item['endpoints'][0] + item['endpoints'][2]) / 2
        mid_y = (item['endpoints'][1] + item['endpoints'][3]) / 2
        for kept in ruled:
            if kept['orientation'] != item['orientation'] or abs(kept['angle'] - item['angle']) > 1.0:
                continue
            kept_x = (kept['endpoints'][0] + kept['endpoints'][2]) / 2
            kept_y = (kept['endpoints'][1] + kept['endpoints'][3]) / 2
            if math.hypot(mid_x - kept_x, mid_y - kept_y) < 0.03 * span:
                duplicate = kept
                break
        if duplicate is None:
            ruled.append(item)
    result = []
    for item in ruled:
        x1, y1, x2, y2 = item.pop('endpoints')
        thickness = item['thickness']
        item['rect'] = [
            round(min(x1, x2) / scale_x, 1),
            round(min(y1, y2) / scale_y, 1),
            round(max(abs(x2 - x1) / scale_x, thickness), 1),
            round(max(abs(y2 - y1) / scale_y, thickness), 1),
        ]
        result.append(item)
    return result


def _weighted_angle(tiles):
    """按文字块证据量加权聚合局部角度，返回角度、离散度和总证据。"""
    angles = np.asarray([tile['angle'] for tile in tiles], dtype=np.float64)
    weights = np.asarray([
        tile['glyphs'] * max(0.1, tile['coverage']) / (1.0 + tile['mad'])
        for tile in tiles
    ], dtype=np.float64)
    weights = np.maximum(weights, 1e-6)
    angle = float(np.average(angles, weights=weights))
    spread = float(np.average(np.abs(angles - angle), weights=weights))
    return angle, spread, float(weights.sum())


def _confidence(tile_count, spread, glyphs, directed=False):
    """置信度必须随文字证据量增长；证据不足时严格为 0。

    ``directed`` 表示用户已显式指定可信区域或文字方向，单块证据也可
    采信，但置信度仍低于多块互相印证的自动结论，不把弱证据装成确定。
    """
    if tile_count < 1:
        return 0.0
    if not directed and tile_count < MIN_CREDIBLE_TILES:
        return 0.0
    agreement = max(0.0, 1.0 - spread / 4.0)
    amount = min(1.0, glyphs / (120.0 if directed else 240.0))
    breadth = min(1.0, tile_count / (2.0 if directed else 4.0))
    ceiling = 0.85 if directed else 0.99
    return round(max(0.0, min(ceiling, 0.9 * agreement * amount * breadth + 0.1 * breadth * amount)), 3)


def analyze_skew(image, region=None, mode='auto'):
    """分析页面倾斜，返回可解释的文字证据与建议校正角。

    :param region: 可选 ``[x, y, w, h]``，只采信与该可信正文区域相交的
        文字块与组件；全页统一分块，区域只做证据过滤，因此小区域也不会
        因重新分块而丢失文字行。区域按原图坐标返回，框选与预览可直接复用。
    :param mode: ``auto`` 自动判定横/竖排；``horizontal`` / ``vertical``
        只接受对应方向的文字块，用于用户指定可信正文方向。
    """
    require(mode in ('auto', 'horizontal', 'vertical'), '倾斜分析模式无效')
    source_height, source_width = image.shape[:2]
    if region is not None:
        rx, ry, rw, rh = rectangle(region, image)
        region_rect = [rx, ry, rw, rh]
    else:
        rx, ry, rw, rh = 0, 0, source_width, source_height
        region_rect = [0, 0, source_width, source_height]

    luminance = gray(image)
    scale = min(1.0, 1600 / max(luminance.shape))
    if scale < 1.0:
        luminance = cv2.resize(luminance, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    small_height, small_width = luminance.shape
    scale_x = small_width / source_width
    scale_y = small_height / source_height

    text_mask = _text_mask(luminance)
    components_list = _components(text_mask)
    # 长直线在全图检测（表格线常横跨可信区域之外），再按区域裁剪展示，
    # 避免区域分析时漏掉页面上真实存在、但被排除在角度判断之外的线
    ruled_small = _detect_ruled_lines(_strong_mask(luminance), scale_x, scale_y, components_list)
    # 区域过滤在缩小后的坐标中进行，保证全页与局部分析共用同一套分块
    sx0, sy0 = rx * scale_x, ry * scale_y
    sx1, sy1 = (rx + rw) * scale_x, (ry + rh) * scale_y
    region_components = [
        component for component in components_list
        if sx0 <= component[5] <= sx1 and sy0 <= component[6] <= sy1
    ]
    # 只把穿过可信区域的长直线计入本次结论的“已排除”数量
    def intersects_region(line):
        lx, ly, lw, lh = line['rect']
        return not (lx + lw < rx or lx > rx + rw or ly + lh < ry or ly > ry + rh)

    ruled_small = [line for line in ruled_small if intersects_region(line)]

    tile_w = small_width / GRID
    tile_h = small_height / GRID
    tiles = []
    for row in range(GRID):
        for column in range(GRID):
            rect = (column * tile_w, row * tile_h, tile_w, tile_h)
            estimate = _tile_estimate(region_components, rect, row * GRID + column)
            if estimate is None:
                continue
            x, y, w, h = estimate['rect']
            estimate['rect'] = [
                round(x / scale_x, 1), round(y / scale_y, 1),
                round(w / scale_x, 1), round(h / scale_y, 1),
            ]
            tiles.append(estimate)
    # _detect_ruled_lines 内部已把 rect 换算为原图坐标，直接使用
    ruled = ruled_small

    reliable_tiles = [tile for tile in tiles if tile['reliable']]
    weak_tile_count = len(tiles) - len(reliable_tiles)
    horizontal_tiles = [tile for tile in reliable_tiles if tile['orientation'] == 'horizontal']
    vertical_tiles = [tile for tile in reliable_tiles if tile['orientation'] == 'vertical']
    if mode == 'horizontal':
        vertical_tiles = []
    elif mode == 'vertical':
        horizontal_tiles = []

    # 用户显式框选可信区域或指定方向时，单块证据也允许采信（用户已背书）
    directed = region is not None or mode != 'auto'
    groups = []
    for orientation, bucket in (('horizontal', horizontal_tiles), ('vertical', vertical_tiles)):
        if not bucket:
            continue
        angle, spread, _evidence_weight = _weighted_angle(bucket)
        groups.append({
            'orientation': orientation,
            'tile_count': len(bucket),
            'glyphs': sum(tile['glyphs'] for tile in bucket),
            'angle': round(angle, 3),
            'spread': round(spread, 3),
            'confidence': _confidence(len(bucket), spread, sum(tile['glyphs'] for tile in bucket), directed),
            'tile_ids': [tile['tile'] for tile in bucket],
        })

    notes = []
    if not groups:
        status = 'undetermined'
        orientation = None
        detected_angle = 0.0
        correction_angle = 0.0
        confidence = 0.0
        conflicting = False
        if len(region_components) < MIN_TILE_GLYPHS:
            notes.append('所选范围内没有足够的文字组件，长直线/边框不能代表正文方向，已保留原状。')
        else:
            notes.append('文字组件无法聚成稳定的文字行或文字列，证据不足，已保留原状。')
        if weak_tile_count:
            notes.append('另有 {n} 个图块因弯曲或跨栏证据不稳定，未参与方向判定。'.format(n=weak_tile_count))
    else:
        groups.sort(key=lambda group: (group['confidence'], group['tile_count'], group['glyphs']), reverse=True)
        dominant = groups[0]
        secondary = groups[1] if len(groups) > 1 else None
        orientation = dominant['orientation']
        detected_angle = dominant['angle']
        spread = dominant['spread']
        confidence = dominant['confidence']
        # 次方向必须有足够数量的稳定文字块才算局部方向冲突，
        # 单个跨栏/弯曲产生的噪声块不报警；证据强弱在置信度里单独体现。
        conflicting = secondary is not None and secondary['tile_count'] >= MIN_CONFLICT_TILES
        if conflicting:
            notes.append(
                '页面同时存在横排与竖排文字证据：横排 {h} 块、竖排 {v} 块，'
                '请确认可信正文区域后再校正。'.format(
                    h=len(horizontal_tiles), v=len(vertical_tiles),
                )
            )
            status = 'conflict'
        elif confidence == 0.0:
            status = 'undetermined'
            notes.append('文字证据过少或局部角度分歧过大，无法给出可信角度，已保留原状。')
        elif spread > 2.5:
            status = 'weak'
            notes.append('局部文字角度分歧较大，结论仅供参考，可框选可信正文区域后重新分析。')
        else:
            status = 'ok'
        if weak_tile_count:
            notes.append('另有 {n} 个图块因弯曲或跨栏证据不稳定，未参与方向判定。'.format(n=weak_tile_count))
        # 横排拟合角相对水平轴、竖排拟合角相对竖直轴，二者几何方向相反；
        # 实测 OpenCV 正角为视觉逆时针，因此横排校正角与检测角同号，
        # 竖排校正角与检测角反号。两套角度都在下方 convention 中声明。
        if orientation == 'vertical':
            correction_angle = round(-detected_angle, 3)
        else:
            correction_angle = round(detected_angle, 3)
        if status == 'undetermined' or abs(correction_angle) < STRAIGHT_LIMIT:
            correction_angle = 0.0
        if ruled:
            notes.append('检测到 {n} 条长直线（表格线/边框/下划线），已从正文角度估计中排除。'.format(n=len(ruled)))

    if directed and groups:
        if mode != 'auto':
            notes.append('已按用户指定的{s}方向评估，其他方向的文字块不参与结论。'.format(
                s='横排' if mode == 'horizontal' else '竖排',
            ))
        if region is not None:
            notes.append('仅采信用户框选可信正文区域内的文字证据。')

    return {
        'status': status,
        'orientation': orientation,
        'mode': mode,
        'angle': round(correction_angle, 3),
        'detected_angle': round(detected_angle, 3),
        'correction_angle': round(correction_angle, 3),
        'confidence': confidence,
        'conflicting': conflicting,
        'region': region_rect,
        'grid': GRID,
        'evidence': {
            'component_count': len(region_components),
            'text_tiles': len(tiles),
            'credible_tiles': sum(group['tile_count'] for group in groups),
            'ruled_line_count': len(ruled),
        },
        'groups': groups,
        'tiles': tiles,
        'ruled_lines': ruled,
        'notes': notes,
        'convention': '角度单位为视觉度：正角表示内容顺时针倾斜；correction_angle 为 rotate 入参（OpenCV 正角为视觉逆时针）。横排校正角与检测角同号，竖排校正角与检测角反号',
    }


def correction_params(image, params):
    """供 ``deskew`` 操作复用，返回 (旋转角, 分析结果)。

    证据不足、页面已经水平或用户显式要求保留原状（``apply=False``）时
    旋转角为 0，调用方据此跳过重写图像，连续纠偏因此不会在残差噪声里
    越转越歪。
    """
    region = params.get('region')
    mode = params.get('mode', 'auto')
    if region is not None:
        number(region[0], '区域x', 0, image.shape[1] - 1, True)
    analysis = analyze_skew(image, region=region, mode=mode)
    if params.get('apply', True) is False:
        analysis['correction_angle'] = 0.0
        analysis['angle'] = 0.0
        analysis['applied'] = False
        analysis['user_kept_original'] = True
        analysis.setdefault('notes', []).append('用户选择保留原状，未施加任何旋转。')
    return analysis['correction_angle'], analysis
