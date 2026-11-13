"""笔迹粗细调整：只改写用户指定正文区域内的中性色手写字/印刷字笔画。

设计约束（对应修复需求）：
- 不做整页二值化，不输出黑白图；原笔迹颜色与抗锯齿轮廓保留。
- 彩图、红印章、细线表格与未选纸面依靠颜色门限、连通域形态与区域遮罩排除。
- 以连通域为单位处理：加粗不会越过相邻字的间距把两个字粘成一个，细化不会
  把细笔画擦断；间距小于设定阈值的相邻笔画会触发保护并给出告警。
- 分区参数以带羽化的像素场融合，同一笔画跨越区域边界时粗细连续，不被切成两截。
- 每次输出都直接从基线图像计算，重复调整不会叠加变字。
"""
from __future__ import annotations

import cv2
import numpy as np

from .core import choice, number, require
from .imaging import gray

MODES = ('thicken', 'thin')
DIRECTIONS = ('both', 'horizontal', 'vertical')
COMPONENT_LIMIT = 12000


def _component_rect(rect, height, width):
    require(isinstance(rect, (list, tuple)) and len(rect) == 4, '分区需要[x,y,宽,高]')
    x = number(rect[0], '分区x', 0, width - 1, True)
    y = number(rect[1], '分区y', 0, height - 1, True)
    w = number(rect[2], '分区宽', 1, width, True)
    h = number(rect[3], '分区高', 1, height, True)
    require(x + w <= width and y + h <= height, '分区越出图片')
    return x, y, w, h


def validate_settings(values, image):
    """校验并归一化一次调整请求；返回可供 adjust() 直接使用的设置字典。"""
    require(isinstance(values, dict), '调整参数必须为对象')
    height, width = image.shape[:2]
    mode = choice(values.get('mode', 'thicken'), '调整方向', MODES)
    delta = number(values.get('delta', 1.0), '粗细变化量', 0.0, 6.0)
    sensitivity = number(values.get('sensitivity', 0.5), '笔迹灵敏度', 0.0, 1.0)
    saturation = number(values.get('saturation', 60), '彩色排除阈值', 0, 255, True)
    min_gap = number(values.get('min_gap', 1.5), '最小笔画间距', 0.0, 20.0)
    max_component = number(values.get('max_component', 40000), '最大笔画部件', 64, 2_000_000, True)
    min_component = number(values.get('min_component', 4), '最小笔画部件', 1, 10000, True)
    feather = number(values.get('feather', 6.0), '边界羽化', 0.0, 100.0)
    direction = choice(values.get('direction', 'both'), '笔画方向', DIRECTIONS)

    zones_raw = values.get('zones', [])
    if zones_raw is None:
        zones_raw = []
    require(isinstance(zones_raw, list) and len(zones_raw) <= 50, '分区数量不能超过50个')
    zones = []
    for index, zone in enumerate(zones_raw):
        require(isinstance(zone, dict), f'分区{index + 1}参数必须为对象')
        entry = {
            'rect': _component_rect(zone['rect'], height, width),
            'name': str(zone.get('name') or f'分区{index + 1}')[:40],
            'action': choice(zone.get('action', 'adjust'), '分区动作', ('adjust', 'protect', 'keep')),
        }
        if entry['action'] == 'adjust':
            entry['delta'] = number(zone.get('delta', delta), '分区粗细变化量', 0.0, 6.0)
            entry['direction'] = choice(zone.get('direction', direction), '分区笔画方向', DIRECTIONS)
        zones.append(entry)

    baseline = choice(values.get('baseline', 'current'), '调整基线', ('current', 'original'))
    return {
        'mode': mode,
        'delta': delta,
        'sensitivity': sensitivity,
        'saturation': saturation,
        'min_gap': min_gap,
        'max_component': max_component,
        'min_component': min_component,
        'feather': feather,
        'direction': direction,
        'zones': zones,
        'baseline': baseline,
    }


def _ink_mask(image, settings):
    """生成候选笔迹遮罩：暗于局部纸面的像素；颜色在部件阶段再判定。"""
    luminance = gray(image)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1]

    # 局部纸面亮度估计：大核中值滤波得到背景，再和像素自身比较。
    scale = min(1.0, 1400 / max(luminance.shape))
    small = cv2.resize(luminance, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    radius = max(15, min(small.shape) // 12) | 1
    background_small = cv2.medianBlur(small, radius)
    background = cv2.resize(background_small, (luminance.shape[1], luminance.shape[0]),
                            interpolation=cv2.INTER_CUBIC)
    # 灵敏度越高，允许越浅的笔迹入选；差值门限随灵敏度线性放宽。
    contrast_floor = 34 - 26 * settings['sensitivity']  # 8 .. 34
    darker_than_paper = cv2.compare(background.astype(np.int16) - luminance.astype(np.int16),
                                    contrast_floor, cv2.CMP_GT)
    mask = darker_than_paper

    # 闭运算先把同一笔画内的抗锯齿缺口合起来，避免单字被拆成碎块。
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    return mask, luminance, saturation


def _line_art_mask(mask):
    """识别表格/坐标系等细线图：长直线开运算得到骨架，再收回与之相连的网格。"""
    line_length = max(40, min(mask.shape) // 12)
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (line_length, 1)))
    vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (1, line_length)))
    skeleton = cv2.bitwise_or(horizontal, vertical)
    full_count, full_labels = cv2.connectedComponents(mask, connectivity=8)[0:2]
    line_art = np.zeros_like(mask, dtype=bool)
    for label in range(1, full_count):
        component = full_labels == label
        # 部件中落在长线骨架上的像素占比高，则判定为线图而非文字。
        if np.logical_and(component, skeleton > 0).sum() > 0.4 * component.sum():
            line_art |= component
    return line_art


def _filter_components(mask, saturation, settings):
    """按饱和度、面积、长宽比过滤连通域，排除彩图、印章、细线图与大色块。

    返回基于“过滤后遮罩”重新编号的标签图，保证标签与后续距离变换一致。
    """
    line_art = _line_art_mask(mask)
    count, raw_labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    valid = set()
    rejected = {'too_small': 0, 'too_large': 0, 'line_like': 0, 'colored': 0}
    for label in range(1, min(count, COMPONENT_LIMIT + 1)):
        area = int(stats[label, cv2.CC_STAT_AREA])
        if area < settings['min_component']:
            rejected['too_small'] += 1
            continue
        if area > settings['max_component']:
            rejected['too_large'] += 1
            continue
        component = raw_labels == label
        w = int(stats[label, cv2.CC_STAT_WIDTH])
        h = int(stats[label, cv2.CC_STAT_HEIGHT])
        longest = max(w, h)
        fill_ratio = area / max(1, w * h)
        # 细线图：与长直线骨架高度重叠，或本身是细长直线。
        if np.logical_and(component, line_art).sum() > 0.4 * area:
            rejected['line_like'] += 1
            continue
        if longest >= 50 and fill_ratio < 0.15 and longest / max(1, min(w, h)) > 8:
            rejected['line_like'] += 1
            continue
        # 部件级颜色判定：中位饱和度过高视为彩图/印章，中性色笔迹才保留。
        component_saturation = float(np.median(saturation[component]))
        if component_saturation > settings['saturation']:
            rejected['colored'] += 1
            continue
        valid.add(label)
    keep_raw = np.isin(raw_labels, list(valid)) if valid else np.zeros_like(mask, dtype=bool)
    clean = np.where(keep_raw, mask, 0).astype(np.uint8)
    # 重新连通域分析，得到只含有效部件、编号连续的标签图。
    count, labels = cv2.connectedComponents(clean, connectivity=8)
    relabeled = set(range(1, count))
    return clean, labels, relabeled, rejected


def _zone_fields(shape, settings):
    """把分区渲染成像素场：调整量场、方向场、动作权重与羽化处理场。"""
    height, width = shape
    delta_field = np.full((height, width), settings['delta'], np.float32)
    weight_field = np.zeros((height, width), np.float32)  # 1=调整, 0=未选区
    protect_field = np.zeros((height, width), np.uint8)
    horizontal = np.zeros((height, width), np.float32)  # 方向混合权重，0=各向同性
    if not settings['zones']:
        weight_field[:] = 1.0
        if settings['direction'] == 'horizontal':
            horizontal[:] = 1.0
        elif settings['direction'] == 'vertical':
            horizontal[:] = -1.0
        return delta_field, weight_field, protect_field, horizontal

    feather = max(0.0, settings['feather'])
    for zone in settings['zones']:
        x, y, w, h = zone['rect']
        patch = np.zeros((h, w), np.float32)
        patch[:] = 1.0
        if feather > 0:
            blur = int(max(1, round(feather)) * 2 + 1)
            patch = cv2.GaussianBlur(patch, (blur, blur), feather)
        if zone['action'] == 'protect':
            protect_field[y:y + h, x:x + w] = np.maximum(
                protect_field[y:y + h, x:x + w], (patch > 0.5).astype(np.uint8) * 255)
            continue
        if zone['action'] == 'keep':
            # keep：明确保留原状，既不调整也不告警，权重置零。
            continue
        current = weight_field[y:y + h, x:x + w]
        overlap = np.minimum(current + patch, 1.0)
        # 在重叠区域按覆盖强度做加权平均，避免两个分区交界处参数跳变。
        total = current + patch
        safe_total = np.maximum(total, 1e-6)
        delta_field[y:y + h, x:x + w] = (
            delta_field[y:y + h, x:x + w] * current + zone['delta'] * patch) / safe_total
        direction_value = {'both': 0.0, 'horizontal': 1.0, 'vertical': -1.0}[zone['direction']]
        horizontal[y:y + h, x:x + w] = (
            horizontal[y:y + h, x:x + w] * current + direction_value * patch) / safe_total
        weight_field[y:y + h, x:x + w] = overlap
    return delta_field, np.clip(weight_field, 0, 1), protect_field, horizontal


def _stroke_widths(labels, valid, distance_inside):
    """每个连通域的代表笔画宽度（部件内部距离变换最大值，即最粗笔画的可容纳半径×2）。"""
    widths = {}
    for label in valid:
        values = distance_inside[labels == label]
        if values.size:
            # 中位数容易被抗锯齿边缘的 1px 值拉低，取 75 分位更接近真实笔宽。
            widths[label] = float(np.percentile(values, 75)) * 2.0
    return widths


def _nearest_component_distances(bg_distance, bg_nearest, labels, valid):
    """利用全图带标签距离变换，求每个部件到“其他”笔迹的最近距离。

    部件自身像素的距离为 0，必须排除本部件标签，只统计最近邻属于其他部件的位置。
    """
    result = {}
    for label in valid:
        component = labels == label
        if not component.any():
            continue
        other_here = np.logical_and(component, bg_nearest != label)
        if other_here.any():
            result[label] = float(bg_distance[other_here].min())
        else:
            result[label] = 999.0
    return result


def _structural_checks(labels, valid, mask, settings, bg_distance, bg_nearest):
    """检查封口、连字风险；返回每个部件的告警集合。"""
    nearest_distance = _nearest_component_distances(bg_distance, bg_nearest, labels, valid)
    warnings_by_component = {}
    for label in valid:
        issues = []
        component = labels == label
        # 与“其他”笔迹的最近间距是否小于保护阈值（连字风险）。
        if nearest_distance.get(label, 999.0) < settings['min_gap']:
            issues.append('adjacent_gap')
        # 内部空腔：被该部件包围的背景小块，加粗可能封口。
        flooded = component.astype(np.uint8) * 255
        contours, hierarchy = cv2.findContours(flooded, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        holes = 0
        total_area = float(component.sum())
        if hierarchy is not None:
            # hierarchy[0][i][3] >= 0 表示该轮廓是内孔。
            for index, contour in enumerate(contours):
                if hierarchy[0][index][3] >= 0:
                    area = cv2.contourArea(contour)
                    if 2 <= area <= 0.5 * total_area:
                        holes += 1
        if holes:
            issues.append('enclosed_cavity')
        if issues:
            warnings_by_component[label] = issues
    return warnings_by_component


def adjust(image, settings):
    """执行调整，返回 (结果图, 差异遮罩, 统计信息)。

    差异遮罩单通道 uint8：新增像素=1，移除像素=2，受保护未改=3。
    统计信息包含像素计数、部件笔画宽度前后值与告警，供预览与确认共用。
    """
    settings = validate_settings(settings, image)
    result = image.copy()
    height, width = image.shape[:2]

    raw_mask, luminance, saturation = _ink_mask(image, settings)
    mask, labels, valid, rejected = _filter_components(raw_mask, saturation, settings)
    delta_field, weight_field, protect_field, direction_field = _zone_fields(
        (height, width), settings)

    # 受保护部件：与保护区域相交即整体保护，保证同一笔画不会一半改一半不改。
    protected_labels = set()
    if protect_field.any() and valid:
        protected_labels = {int(label) for label in valid
                            if bool(np.logical_and(labels == label, protect_field > 0).any())}
    active_labels = valid - protected_labels

    inside = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    widths_before = _stroke_widths(labels, valid, inside)

    # 全图一次带标签距离变换：背景每个像素到最近笔迹部件的距离与部件标签，
    # 连字检查与加粗间距保护共用这一份结果。
    bg_distance, bg_nearest = cv2.distanceTransformWithLabels(
        255 - mask, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_CCOMP)
    warnings_by_component = _structural_checks(
        labels, active_labels, mask, settings, bg_distance, bg_nearest)

    diff = np.zeros((height, width), np.uint8)
    if active_labels:
        # 每个部件只取一个代表调整量：部件质心处的像素场值，保证部件内部粗细一致。
        component_delta = {}
        component_direction = {}
        for label in active_labels:
            ys, xs = np.where(labels == label)
            cy, cx = int(np.median(ys)), int(np.median(xs))
            component_delta[label] = float(delta_field[cy, cx]) * float(weight_field[cy, cx])
            component_direction[label] = float(direction_field[cy, cx])

        if settings['mode'] == 'thicken':
            added_total = np.zeros((height, width), bool)
            for label in active_labels:
                amount = component_delta[label]
                if amount <= 0.01:
                    continue
                # 方向偏置：horizontal 偏置放大横向笔画的纵向（y向）半径，vertical 反之。
                bias = component_direction[label]
                radius_x = amount * (1.0 + 0.35 * max(0.0, bias) - 0.35 * max(0.0, -bias))
                radius_y = amount * (1.0 + 0.35 * max(0.0, -bias) - 0.35 * max(0.0, bias))
                grown = self_distance_grow(mask, label, labels, radius_x, radius_y)
                # 只允许占用无主背景：不能吞掉其他部件，避免连字。
                grown = np.logical_and(grown, mask == 0)
                # 间距保护：新增像素与其他笔迹之间至少保留 min_gap 的纸面。
                if 'adjacent_gap' in warnings_by_component.get(label, []):
                    belongs_to_other = bg_nearest != label
                    clear_of_others = np.logical_or(~belongs_to_other,
                                                     bg_distance >= settings['min_gap'])
                    grown = np.logical_and(grown, clear_of_others)
                added_total = np.logical_or(added_total, grown)
            # 颜色：取最近原笔迹像素的颜色，保持原笔迹色彩与浓淡。
            colored = nearest_ink_color(image, mask, added_total)
            result[added_total] = colored[added_total]
            diff[added_total] = 1
            new_ink = np.where(added_total, 255, mask).astype(np.uint8)
        else:
            removed_total = np.zeros((height, width), bool)
            for label in active_labels:
                amount = component_delta[label]
                if amount <= 0.01:
                    continue
                stroke_width = widths_before.get(label, amount * 2)
                # 细化量不得超过部件笔画宽度的 40%，防止断笔。
                amount = min(amount, stroke_width * 0.4)
                bias = component_direction[label]
                radius_x = amount * (1.0 + 0.35 * max(0.0, bias) - 0.35 * max(0.0, -bias))
                radius_y = amount * (1.0 + 0.35 * max(0.0, -bias) - 0.35 * max(0.0, bias))
                eroded = anisotropic_inside(labels == label, radius_x, radius_y)
                removed = np.logical_and(labels == label, ~eroded)
                removed_total = np.logical_or(removed_total, removed)
            # 移除处恢复为局部纸面颜色，而不是纯白。
            paper = local_paper_color(image, mask)
            result[removed_total] = paper[removed_total]
            diff[removed_total] = 2
            new_ink = np.where(removed_total, 0, mask).astype(np.uint8)

    for label in protected_labels:
        diff[labels == label] = 3

    # 调整后重新计算笔画宽度；标签可能已变化，按调整后笔迹整体的 75 分位统计。
    widths_after = {}
    if active_labels:
        inside_after = cv2.distanceTransform(new_ink, cv2.DIST_L2, 5)
        after_values = inside_after[new_ink > 0]
        after_median = float(np.percentile(after_values, 75)) * 2.0 if after_values.size else 0.0
        before_values = np.asarray([widths_before[label] for label in active_labels])
        widths_after = {'median': round(after_median, 2),
                        'before_median_of_active': round(float(np.median(before_values)), 2)}

    stats = {
        'mode': settings['mode'],
        'baseline': settings['baseline'],
        'components_total': len(valid),
        'components_adjusted': len(active_labels),
        'components_protected': len(protected_labels),
        'pixels_added': int((diff == 1).sum()),
        'pixels_removed': int((diff == 2).sum()),
        'pixels_protected': int((diff == 3).sum()),
        'rejected_components': rejected,
        'warnings': summarize_warnings(warnings_by_component, labels),
        'stroke_width': {
            'before_median': round(float(np.median(list(widths_before.values()))), 2) if widths_before else 0,
            'after_median': widths_after.get('median', 0),
            'active_before_median': widths_after.get('before_median_of_active', 0),
        },
        'settings': settings,
    }
    return result, diff, stats


def self_distance_grow(mask, label, labels, radius_x, radius_y):
    """单部件各向异性膨胀：只在该部件外部、按椭圆半径生长。"""
    component = (labels == label).astype(np.uint8) * 255
    # 缩放坐标把椭圆半径换算成各向同性半径。
    if radius_x <= 0 or radius_y <= 0:
        return np.zeros_like(mask, dtype=bool)
    height, width = mask.shape
    sx = 1.0 / max(radius_x, 1e-3)
    sy = 1.0 / max(radius_y, 1e-3)
    resized = cv2.resize(component, None, fx=sx, fy=sy, interpolation=cv2.INTER_NEAREST)
    distance = cv2.distanceTransform(255 - resized, cv2.DIST_L2, 5)
    grown_small = distance <= 1.0
    grown = cv2.resize(grown_small.astype(np.uint8), (width, height), interpolation=cv2.INTER_NEAREST)
    return grown.astype(bool)


def anisotropic_inside(component_mask, radius_x, radius_y):
    """各向异性腐蚀后仍保留的部件内部像素（距离边界足够远）。"""
    height, width = component_mask.shape
    sx = 1.0 / max(radius_x, 1e-3)
    sy = 1.0 / max(radius_y, 1e-3)
    resized = cv2.resize(component_mask.astype(np.uint8) * 255, None, fx=sx, fy=sy,
                         interpolation=cv2.INTER_NEAREST)
    distance = cv2.distanceTransform(resized, cv2.DIST_L2, 5)
    keep_small = distance > 1.0
    keep = cv2.resize(keep_small.astype(np.uint8), (width, height), interpolation=cv2.INTER_NEAREST)
    # 缩放可能丢掉孤立细点：与原部件相交，保证不产生新部件。
    return np.logical_and(keep.astype(bool), component_mask)


def nearest_ink_color(image, mask, target_pixels):
    """用最近邻笔迹像素的颜色给新增像素上色，保留原墨色与轮廓浓淡。"""
    if not target_pixels.any():
        return image
    _, nearest = cv2.distanceTransformWithLabels(
        255 - mask, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    zeros_y, zeros_x = np.where(mask > 0)
    # DIST_LABEL_PIXEL：标签 i 对应第 i-1 个零像素（按行优先扫描顺序）。
    lookup_y = np.concatenate((zeros_y[:1], zeros_y))
    lookup_x = np.concatenate((zeros_x[:1], zeros_x))
    return image[lookup_y[nearest], lookup_x[nearest]]


def local_paper_color(image, mask):
    """估计每个像素处的局部纸面颜色，供细化后的露底使用。"""
    scale = min(1.0, 1000 / max(image.shape[:2]))
    small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    radius = max(15, min(small.shape[:2]) // 10) | 1
    paper_small = cv2.medianBlur(small, radius)
    paper = cv2.resize(paper_small, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_CUBIC)
    return paper


def summarize_warnings(warnings_by_component, labels):
    counts = {}
    examples = {}
    for label, issues in warnings_by_component.items():
        for issue in issues:
            counts[issue] = counts.get(issue, 0) + 1
            examples.setdefault(issue, int(label))
    return {'counts': counts, 'examples': examples}


def overlay_preview(image, diff, stats):
    """生成红绿差异叠加预览：绿色=拟增加，红色=拟减去，蓝色=受保护。"""
    preview = image.copy()
    colors = {1: (90, 200, 90), 2: (80, 80, 230), 3: (210, 150, 60)}
    for value, color in colors.items():
        pixels = diff == value
        if not pixels.any():
            continue
        blend = preview.astype(np.float32)
        blend[pixels] = blend[pixels] * 0.35 + np.array(color, np.float32) * 0.65
        preview = np.clip(blend, 0, 255).astype(np.uint8)
    return preview
