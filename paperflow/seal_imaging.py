"""印章与批注分离的像素级图像处理。

设计原则：
- 只从照片中“提出”颜色归属，绝不凭空生成或修补任何笔画；
- 自动结果对每个像素给出 保留 / 排除 / 待确认 三种状态，
  颜色与黑字重叠而无法从照片确定归属时，一律标记为待确认；
- 输出软透明度（alpha），浅印泥以较低的不透明度保留，而不是被阈值吃掉；
- 所有结果都是确定性的纯函数，便于换参数复算与测试。
"""
from __future__ import annotations

import cv2
import numpy as np

from .core import DomainError, choice, number, require

# 像素归属状态：与前端、存储中的 mask 取值保持一致
KEEP = 1          # 自动判定属于印章/批注，保留
EXCLUDE = 0       # 自动判定属于纸底或黑字，排除
REVIEW = 2        # 重叠/证据不足，保留待人工确认，禁止自动决定

# 8 位 mask 中各状态的灰度值（便于直接保存为 PNG 并在前端叠加显示）
MASK_VALUE = {EXCLUDE: 0, KEEP: 255, REVIEW: 128}

PRESET_HUES = {
    # 名称: (色相中心 deg, 色相半宽 deg)，OpenCV H 通道为 0-179
    'red': (0.0, 18.0),
    'vermilion': (6.0, 16.0),
    'blue': (110.0, 18.0),
    'black': (0.0, 90.0),
}


def _validate_params(values):
    require(isinstance(values, dict), '提取参数必须为对象')
    preset = choice(values.get('preset', 'red'), '颜色预设', tuple(PRESET_HUES))
    hue_center, hue_span = PRESET_HUES[preset]
    if 'hue_center' in values:
        hue_center = number(values['hue_center'], '色相中心', 0, 179, True)
    if 'hue_span' in values:
        hue_span = number(values['hue_span'], '色相容差', 3, 60, True)
    saturation_min = number(values.get('saturation_min', 45), '最低饱和度', 0, 255, True)
    # 浅到快看不见的印泥：用一个较低的“纳入下限”把它们以软 alpha 留住
    value_min = number(values.get('value_min', 70), '印泥纳入下限', 0, 255, True)
    overlap_gap = number(values.get('overlap_gap', 28), '重叠判定间隔', 0, 120, True)
    min_component = number(values.get('min_component', 12), '最小连通面积', 0, 5000, True)
    return {
        'preset': preset,
        'hue_center': hue_center,
        'hue_span': hue_span,
        'saturation_min': saturation_min,
        'value_min': value_min,
        'overlap_gap': overlap_gap,
        'min_component': min_component,
    }


def describe_params():
    return {
        'preset': {'default': 'red', 'options': list(PRESET_HUES)},
        'hue_center': {'type': 'integer', 'minimum': 0, 'maximum': 179, 'default': 0},
        'hue_span': {'type': 'integer', 'minimum': 3, 'maximum': 60, 'default': 18},
        'saturation_min': {'type': 'integer', 'minimum': 0, 'maximum': 255, 'default': 45},
        'value_min': {'type': 'integer', 'minimum': 0, 'maximum': 255, 'default': 70},
        'overlap_gap': {'type': 'integer', 'minimum': 0, 'maximum': 120, 'default': 28},
        'min_component': {'type': 'integer', 'minimum': 0, 'maximum': 5000, 'default': 12},
    }


def _hue_distance(hue, center):
    """环形色相距离，结果 0-179。"""
    diff = np.abs(hue.astype(np.int16) - int(center))
    return np.minimum(diff, 180 - diff)


def separate(image, params):
    """对一张 BGR 图计算印章/批注的状态 mask 与软 alpha。

    返回 dict：
      status: uint8 单通道，取值 0/255/128（排除/保留/待确认）
      alpha:  uint8 单通道，0-255 的软不透明度（待确认像素给保守低透明度）
      stats:  各状态像素计数
      params: 规范化后的参数
    纯函数，不修改输入。
    """
    params = _validate_params(params)
    height, width = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]

    hue_dist = _hue_distance(hue, params['hue_center'])
    # 颜色证据：越接近目标色相、饱和度越高，越可信是印章/批注色
    hue_score = np.clip(1.0 - hue_dist / max(1.0, params['hue_span']), 0.0, 1.0)
    sat_score = np.clip((sat.astype(np.float32) - params['saturation_min']) /
                        max(1.0, 255 - params['saturation_min']), 0.0, 1.0)
    color_strength = hue_score * sat_score

    # 纯黑字证据：低明度且低饱和（不含印章色相）
    neutral_dark = (val < 110) & (sat < 90)

    in_hue = hue_dist <= params['hue_span']
    colored = in_hue & (sat >= params['saturation_min']) & (val >= params['value_min'])

    # 估计“正常印泥”的参考明度：取色相命中且较亮像素的高位中位数，
    # 这样红压黑导致的明显变暗就能被识别为相对落差，而不依赖绝对阈值。
    bright_hue = in_hue & (sat >= params['saturation_min']) & (val >= 150)
    if np.any(bright_hue):
        reference_v = float(np.median(val[bright_hue]))
    else:
        reference_v = 200.0
    darkened = val < (reference_v - params['overlap_gap'])

    status = np.full((height, width), MASK_VALUE[EXCLUDE], np.uint8)

    # 重叠区：命中印章色相、却被明显压暗（红章压在黑字上，或黑字压在红章上）。
    # 照片无法判断该处笔画归属 -> 待确认，绝不自动吞并黑字或吃掉断笔。
    overlap = colored & darkened
    # 证据较弱的边缘（色相一致但饱和度/明度贴近阈值）也交人工确认，
    # 避免把正文抗锯齿边缘误带走。
    weak = colored & (~overlap) & (color_strength < 0.32)
    strong = colored & (~overlap) & (~weak)

    status[strong] = MASK_VALUE[KEEP]
    status[overlap | weak] = MASK_VALUE[REVIEW]
    # 中性深色黑字明确排除（即便色相噪声让它落入 hue 窗口）
    status[neutral_dark & (~colored)] = MASK_VALUE[EXCLUDE]

    if params['min_component'] > 0:
        status = _drop_tiny_components(status, params['min_component'])

    alpha = build_alpha(image, status)
    stats = {
        'keep': int(np.count_nonzero(status == MASK_VALUE[KEEP])),
        'review': int(np.count_nonzero(status == MASK_VALUE[REVIEW])),
        'exclude': int(np.count_nonzero(status == MASK_VALUE[EXCLUDE])),
        'width': width,
        'height': height,
    }
    return {'status': status, 'alpha': alpha, 'stats': stats, 'params': params}


def _drop_tiny_components(status, min_area):
    """把零散噪点从“保留/待确认”降级为排除，按连通面积判定，不碰大块印章。"""
    candidate = (status != MASK_VALUE[EXCLUDE]).astype(np.uint8)
    count, labels, stats_label, _ = cv2.connectedComponentsWithStats(candidate, connectivity=8)
    result = status.copy()
    for label in range(1, count):
        if stats_label[label, cv2.CC_STAT_AREA] < min_area:
            result[labels == label] = MASK_VALUE[EXCLUDE]
    return result


def build_alpha(image, status):
    """由颜色强度构造软透明度（预览与导出共用的唯一实现）。

    - 排除像素 alpha=0；
    - 保留像素 alpha 随印泥浓度在 60-255 间平滑变化，浅色印泥以低透明度保留；
    - 待确认像素给保守的固定半透明，提示人工，而不是伪装成确定结果。
    """
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1].astype(np.float32)
    val = hsv[:, :, 2].astype(np.float32)
    # 印泥越浓（饱和高、不过曝）不透明度越高
    concentration = np.clip(sat / 255.0, 0, 1) * np.clip((255.0 - val) / 120.0, 0.25, 1.0)
    keep_alpha = (60 + concentration * 195).astype(np.uint8)

    alpha = np.zeros(status.shape, np.uint8)
    keep = status == MASK_VALUE[KEEP]
    review = status == MASK_VALUE[REVIEW]
    alpha[keep] = keep_alpha[keep]
    alpha[review] = 90  # 保守半透明，等待人工
    return alpha


# 旧的内部别名，保持向后兼容
_build_alpha = build_alpha


def mask_png_to_status(mask):
    values = set(np.unique(mask).tolist())
    require(values <= set(MASK_VALUE.values()), '状态 mask 含非法取值', 500)
    return mask.astype(np.uint8)


def composite_rgba(image, alpha, status=None):
    """生成与预览完全一致的 RGBA 透明图。

    颜色取原图像素，仅用 alpha 控制透明；不改变任何颜色，也不补笔画。
    若提供 status，待确认像素仍按其保守 alpha 输出，由导出策略决定是否纳入。
    """
    require(image.shape[:2] == alpha.shape[:2], 'alpha 与原图尺寸不一致', 500)
    rgba = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
    rgba[:, :, 3] = alpha
    return rgba


def composite_on_background(rgba, background):
    """把 RGBA 透明图合成到指定纸底上，供预览与导出共用。

    预览与最终导出调用同一函数，保证“边缘和预览一致”。
    background: white / dark / checker / paper
    """
    choice(background, '纸底', ('white', 'dark', 'checker', 'paper'))
    height, width = rgba.shape[:2]
    alpha = rgba[:, :, 3:4].astype(np.float32) / 255.0
    source = rgba[:, :, :3].astype(np.float32)
    if background == 'white':
        canvas = np.full((height, width, 3), 255, np.float32)
    elif background == 'dark':
        canvas = np.full((height, width, 3), 48, np.float32)
    elif background == 'paper':
        canvas = np.full((height, width, 3), (245, 242, 232), np.float32)  # BGR 暖纸色
    else:
        tile = 16
        checker = np.zeros((height, width, 3), np.float32)
        yy, xx = np.indices((height, width))
        light = ((xx // tile) + (yy // tile)) % 2 == 0
        checker[light] = 235
        checker[~light] = 200
        canvas = checker
    composed = source * alpha + canvas * (1.0 - alpha)
    return np.clip(composed, 0, 255).astype(np.uint8)


def crop_to_content(rgba):
    """按非透明像素裁剪，返回裁剪后的 RGBA 与原图坐标 bbox。"""
    alpha = rgba[:, :, 3]
    coords = cv2.findNonZero(alpha)
    require(coords is not None, '没有可导出的不透明像素', 400)
    x, y, w, h = cv2.boundingRect(coords)
    return rgba[y:y + h, x:x + w].copy(), [int(x), int(y), int(w), int(h)]


def encode_png(image):
    ok, buffer = cv2.imencode('.png', image)
    require(ok, 'PNG 编码失败', 500)
    return buffer.tobytes()


def overlay_preview(image, status):
    """生成给前端的彩色叠加预览（BGR），用于“所见即所得”。

    保留=印章原色描亮，待确认=琥珀色提示，排除=原图淡化。
    这是预览辅助图，不参与最终导出。
    """
    preview = image.copy()
    keep = status == MASK_VALUE[KEEP]
    review = status == MASK_VALUE[REVIEW]
    # 非保留区域整体压暗，突出选区
    non = ~(keep | review)
    if np.any(non):
        preview[non] = (preview[non] * 0.55 + 120).astype(np.uint8)
    # 待确认覆盖琥珀色提示（空集合时跳过，避免 OpenCV 对零像素数组报错）
    if np.any(review):
        amber = np.zeros_like(preview)
        amber[:] = (40, 165, 210)  # BGR 琥珀
        preview[review] = cv2.addWeighted(preview[review], 0.45, amber[review], 0.55, 0)
    # 保留像素轻微提亮描边感
    if np.any(keep):
        preview[keep] = np.clip(preview[keep].astype(np.int16) + 18, 0, 255).astype(np.uint8)
    return preview


def apply_manual_edits(base_status, edits):
    """在自动 mask 上叠加人工决定，人工决定永远优先。

    edits: list of {target: keep|exclude|review, geometry: {type: rect/points, ...}}
    矩形 [x,y,w,h]；points 为像素坐标列表（画笔轨迹覆盖的点）。
    返回新的 status 数组（拷贝，不改输入）。
    """
    height, width = base_status.shape
    result = base_status.copy()
    for edit in edits:
        target = choice(edit.get('target'), '人工标记目标', ('keep', 'exclude', 'review'))
        target_value = MASK_VALUE[{'keep': KEEP, 'exclude': EXCLUDE, 'review': REVIEW}[target]]
        geometry = edit.get('geometry') or {}
        kind = choice(geometry.get('type', 'rect'), '标记形状', ('rect', 'points'))
        if kind == 'rect':
            x, y, w, h = geometry.get('rect', [])
            x = number(x, 'x', 0, width - 1, True)
            y = number(y, 'y', 0, height - 1, True)
            w = number(w, '宽', 1, width, True)
            h = number(h, '高', 1, height, True)
            require(x + w <= width and y + h <= height, '标记区域越界')
            result[y:y + h, x:x + w] = target_value
        else:
            points = geometry.get('points')
            require(isinstance(points, list) and points, '画笔需要坐标点')
            for point in points:
                require(isinstance(point, list) and len(point) == 2, '坐标点需要[x,y]')
                px = number(point[0], '点x', 0, width - 1, True)
                py = number(point[1], '点y', 0, height - 1, True)
                result[py, px] = target_value
    return result


def reconcile_manual_edits(new_auto_status, manual_status, previous_manual):
    """换参数重新自动提取后，保留人工决定。

    manual_status 是上一轮“人工决定后”的完整状态；previous_manual 记录
    哪些像素曾被人工改动（布尔 mask）。重算只覆盖未被人工触碰的像素，
    人工改过的像素原样保留 —— 人工决定优先于再次自动提取。
    """
    require(new_auto_status.shape == manual_status.shape, '重算图像尺寸不一致', 500)
    result = new_auto_status.copy()
    touched = previous_manual.astype(bool)
    result[touched] = manual_status[touched]
    return result
