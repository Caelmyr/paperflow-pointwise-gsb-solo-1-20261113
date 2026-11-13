from __future__ import annotations

import math
import os
import warnings
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from .core import DomainError, choice, number, require

MAX_PIXELS = 36_000_000
MIN_SIDE = 32
MAX_SIDE = 16000
# 明确的审阅纸底：需要不透明输入的识别、缩略图与铺底输出统一使用它。
REVIEW_PAPER_HEX = 'ffffff'
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def validate_image(image):
    require(image.ndim in (2, 3), '图像数据结构无效')
    height, width = image.shape[:2]
    require(MIN_SIDE <= width <= MAX_SIDE and MIN_SIDE <= height <= MAX_SIDE, '图片宽高必须在32–16000像素之间')
    require(width * height <= MAX_PIXELS, '图片最多3600万像素')
    if image.ndim == 3:
        require(image.shape[2] in (3, 4), '图像通道数无效')
    return image


def has_alpha(image):
    """只有真正存在半透明或透明像素的图像才携带 alpha 通道。"""
    return image.ndim == 3 and image.shape[2] == 4


def parse_paper_color(value, field='审阅纸底颜色'):
    """把 #RRGGBB / RRGGBB 解析为不透明 BGR 三元组（cv2 约定）。"""
    if value is None:
        value = REVIEW_PAPER_HEX
    require(isinstance(value, str), field + '必须为十六进制颜色文本')
    text = value.strip().lstrip('#')
    require(len(text) == 6, field + '必须为 #RRGGBB 六位十六进制颜色')
    try:
        red = int(text[0:2], 16)
        green = int(text[2:4], 16)
        blue = int(text[4:6], 16)
    except ValueError:
        require(False, field + '必须为 #RRGGBB 六位十六进制颜色')
    return (blue, green, red)


def sanitize_transparent(image):
    """把完全透明像素里的隐藏颜色清为黑色，避免污染可见边缘。

    透明原件中 alpha=0 的 RGB 没有物理含义；统一清零后，任何直接插值
    （例如未走预乘路径的运算）都不会把隐藏颜色带到半透明边缘上。
    """
    if not has_alpha(image):
        return image
    result = image.copy()
    transparent = result[:, :, 3] == 0
    if transparent.any():
        result[:, :, :3][transparent] = 0
    return result


def premultiplied(image):
    """返回 (预乘BGRA float32, 原alpha uint8)，用于几何重采样。"""
    alpha = image[:, :, 3].astype(np.float32) / 255.0
    color = image[:, :, :3].astype(np.float32)
    pre = color * alpha[:, :, None]
    return np.dstack([pre, alpha * 255.0]), image[:, :, 3].copy()


def unpremultiplied(pre):
    """预乘 float32 BGRA -> 非预乘 uint8 BGRA；零 alpha 处 RGB 清零。"""
    pre = np.clip(pre, 0, 255)
    alpha = pre[:, :, 3:4]
    coverage = np.divide(alpha, 255.0, out=np.zeros_like(alpha), where=alpha > 0)
    color = np.divide(pre[:, :, :3], coverage, out=np.zeros_like(pre[:, :, :3]), where=coverage > 1e-6)
    color = np.clip(np.rint(color), 0, 255).astype(np.uint8)
    alpha_u8 = np.clip(np.rint(alpha[:, :, 0]), 0, 255).astype(np.uint8)
    return sanitize_transparent(np.dstack([color, alpha_u8]))


def warp_alpha(image, warp, size, flags, border_value=(255, 255, 255)):
    """以预乘 alpha 执行任意透视/仿射变换，半透明边缘不混入透明区隐藏颜色。

    warp 为接受 (BGRA float32, size, flags, borderValue) 的 cv2 变换函数。
    不透明图像仍按原三通道路径处理，保持既有行为。
    """
    if not has_alpha(image):
        return warp(image, size, flags, border_value)
    pre, _ = premultiplied(image)
    warped = warp(pre, size, flags, (0, 0, 0, 0))
    return unpremultiplied(warped)


def resize_alpha(image, size=None, fx=None, fy=None, interpolation=cv2.INTER_AREA):
    """缩放时对颜色与覆盖程度使用同一套预乘含义。"""
    if not has_alpha(image):
        return cv2.resize(image, size, fx=fx, fy=fy, interpolation=interpolation)
    pre, _ = premultiplied(image)
    resized = cv2.resize(pre, size, fx=fx, fy=fy, interpolation=interpolation)
    return unpremultiplied(resized)


def flatten(image, paper=REVIEW_PAPER_HEX):
    """按明确审阅纸底合成不透明 BGR；只用于审阅/铺底，绝不改写透明原件。"""
    if isinstance(paper, str):
        paper = parse_paper_color(paper)
    if not has_alpha(image):
        return bgr(image).copy()
    alpha = image[:, :, 3].astype(np.float32) / 255.0
    color = image[:, :, :3].astype(np.float32)
    background = np.zeros_like(color)
    background[:, :, :] = paper
    composited = color * alpha[:, :, None] + background * (1 - alpha[:, :, None])
    return np.clip(np.rint(composited), 0, 255).astype(np.uint8)


def drop_opaque_alpha(image):
    """完全不透明的四通道图像回落为三通道，减少不必要的 alpha 负担。"""
    if has_alpha(image) and bool((image[:, :, 3] == 255).all()):
        return image[:, :, :3].copy()
    return image


def pil_to_opencv(pil_image):
    """PIL 图像 -> cv2 BGRA/BGR，保留真实 alpha，并清理透明像素的隐藏颜色。"""
    normalized = ImageOps.exif_transpose(pil_image)
    bands = normalized.getbands()
    if 'A' in bands:
        rgba = np.asarray(normalized.convert('RGBA'))
        bgra = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)
        return drop_opaque_alpha(sanitize_transparent(bgra))
    rgb = np.asarray(normalized.convert('RGB'))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def read_image(path):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as original:
                require(original.format in ('PNG', 'JPEG', 'TIFF', 'BMP', 'WEBP'), '不支持此图片格式')
                require(original.width * original.height <= MAX_PIXELS, '图片像素过大')
                result = pil_to_opencv(original)
                return validate_image(result)
    except DomainError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise DomainError('图片损坏或格式不支持：' + str(error)) from error


def save_image(path, image):
    validate_image(image)
    success, encoded = cv2.imencode('.png', image)
    require(success, '图像编码失败', 500)
    temporary = Path(str(path) + '.tmp')
    try:
        with temporary.open('wb') as handle:
            handle.write(encoded.tobytes())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def thumbnail(path, image, size=240, paper=REVIEW_PAPER_HEX):
    """缩略图是不透明审阅视图：按明确审阅纸底铺底，与铺底导出边缘一致。"""
    height, width = image.shape[:2]
    ratio = min(size / width, size / height, 1)
    small = resize_alpha(image, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
    small = flatten(small, paper)
    success, encoded = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 86])
    require(success, '缩略图生成失败', 500)
    Path(path).write_bytes(encoded.tobytes())


def gray(image):
    if image.ndim == 2:
        return image
    if has_alpha(image):
        return cv2.cvtColor(flatten(image), cv2.COLOR_BGR2GRAY)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def bgr(image):
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    return image


def ordered_quad(points):
    points = np.asarray(points, dtype=np.float32)
    require(points.shape == (4, 2) and bool(np.isfinite(points).all()), '需要四个有效角点')
    center = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
    ordered = points[np.argsort(angles)]
    start = np.argmin(ordered.sum(axis=1))
    ordered = np.roll(ordered, -start, axis=0)
    first_edge = ordered[1] - ordered[0]
    second_edge = ordered[2] - ordered[1]
    if first_edge[0] * second_edge[1] - first_edge[1] * second_edge[0] < 0:
        ordered = ordered[[0, 3, 2, 1]]
    require(cv2.isContourConvex(ordered.astype(np.int32)), '角点必须构成凸四边形')
    require(abs(cv2.contourArea(ordered)) >= 1024, '选定纸张区域过小')
    return ordered


def detect_edges(image):
    height, width = image.shape[:2]
    ratio = min(1, 1200 / max(height, width))
    small = resize_alpha(image, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
    luminance = gray(small)
    blurred = cv2.GaussianBlur(luminance, (5, 5), 0)
    edges = cv2.Canny(blurred, 40, 120)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    candidates = []
    area = small.shape[0] * small.shape[1]
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:40]:
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, 0.025 * perimeter, True)
        covered = cv2.contourArea(polygon) / area
        if len(polygon) == 4 and covered > 0.12 and cv2.isContourConvex(polygon):
            points = ordered_quad(polygon.reshape(4, 2) / ratio)
            candidates.append({'points': points.round(2).tolist(), 'confidence': round(min(0.99, covered), 3)})
    if candidates:
        return {'detected': True, **candidates[0], 'alternatives': candidates[1:4]}
    return {
        'detected': False,
        'points': [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        'confidence': 0,
        'alternatives': [],
    }


def perspective(image, points):
    points = ordered_quad(points)
    height, width = image.shape[:2]
    require(bool((points[:, 0] >= 0).all() and (points[:, 0] < width).all()), '角点横坐标越界')
    require(bool((points[:, 1] >= 0).all() and (points[:, 1] < height).all()), '角点纵坐标越界')
    tl, tr, br, bl = points
    target_width = round(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    target_height = round(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    require(target_width * target_height <= MAX_PIXELS, '校正结果过大')
    require(min(target_width, target_height) >= MIN_SIDE, '校正区域过窄')
    target = np.array([[0, 0], [target_width - 1, 0], [target_width - 1, target_height - 1], [0, target_height - 1]], np.float32)
    matrix = cv2.getPerspectiveTransform(points, target)

    def warp(source, size, flags, border_value):
        return cv2.warpPerspective(source, matrix, size, flags=flags, borderValue=border_value)

    result = warp_alpha(image, warp, (target_width, target_height), cv2.INTER_LINEAR, (255, 255, 255))
    return result, {'points': points.tolist(), 'matrix': matrix.tolist()}


def rotate(image, angle, expand=True):
    angle = number(angle, '旋转角度', -360, 360)
    if angle % 360 == 90:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    if angle % 360 == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    if angle % 360 == 270:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1)
    new_width, new_height = width, height
    if expand:
        cosine = abs(matrix[0, 0])
        sine = abs(matrix[0, 1])
        new_width = math.ceil(height * sine + width * cosine)
        new_height = math.ceil(height * cosine + width * sine)
        matrix[0, 2] += (new_width - width) / 2
        matrix[1, 2] += (new_height - height) / 2
    require(new_width * new_height <= MAX_PIXELS, '旋转结果超过像素限制')

    def warp(source, size, flags, border_value):
        return cv2.warpAffine(source, matrix, size, flags=flags, borderValue=border_value)

    return warp_alpha(image, warp, (new_width, new_height), cv2.INTER_CUBIC, (255, 255, 255))


def skew_angle(image):
    luminance = gray(image)
    scale = min(1, 1800 / max(luminance.shape))
    luminance = cv2.resize(luminance, None, fx=scale, fy=scale)
    binary = cv2.threshold(luminance, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    edges = cv2.Canny(binary, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 1800, 80, minLineLength=max(40, luminance.shape[1] // 10), maxLineGap=20)
    angles = []
    weights = []
    if lines is not None:
        for x1, y1, x2, y2 in lines[:, 0]:
            angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
            if -15 <= angle <= 15:
                angles.append(angle)
                weights.append(math.hypot(x2 - x1, y2 - y1))
    if not angles:
        return {'angle': 0.0, 'confidence': 0.0, 'lines': 0}
    ordered = np.argsort(angles)
    cumulative = np.cumsum(np.asarray(weights)[ordered])
    index = np.searchsorted(cumulative, cumulative[-1] / 2)
    angle = float(np.asarray(angles)[ordered][index])
    deviation = float(np.median(np.abs(np.asarray(angles) - angle)))
    return {'angle': round(angle, 3), 'confidence': round(max(0, 1 - deviation / 8), 3), 'lines': len(angles)}


def illumination(image, strength):
    strength = number(strength, '光照强度', 0, 1)
    if strength == 0:
        return image.copy()
    if not has_alpha(image):
        work = bgr(image)
        alpha = None
    else:
        work = image[:, :, :3].copy()
        alpha = image[:, :, 3].copy()
    lab = cv2.cvtColor(work, cv2.COLOR_BGR2LAB)
    light, channel_a, channel_b = cv2.split(lab)
    scale = min(1, 1200 / max(light.shape))
    reduced = cv2.resize(light, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    radius = max(15, min(reduced.shape) // 20) | 1
    background = cv2.morphologyEx(reduced, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius, radius)))
    background = cv2.GaussianBlur(background, (0, 0), 15)
    background = cv2.resize(background, (light.shape[1], light.shape[0]), interpolation=cv2.INTER_CUBIC)
    corrected = cv2.divide(light, np.maximum(background, 1), scale=245)
    light = cv2.addWeighted(light, 1 - strength, corrected, strength, 0)
    result = cv2.cvtColor(cv2.merge((light, channel_a, channel_b)), cv2.COLOR_LAB2BGR)
    if alpha is not None:
        return sanitize_transparent(np.dstack([result, alpha]))
    return result


def color_mode(image, mode, threshold=160, block_size=35, constant=12):
    choice(mode, '色彩模式', ('color', 'gray', 'binary', 'adaptive'))
    if mode == 'color':
        return bgr(image)
    if has_alpha(image):
        alpha = image[:, :, 3].copy()
        luminance = gray(image)
    else:
        alpha = None
        luminance = gray(bgr(image))
    if mode == 'gray':
        result = cv2.cvtColor(luminance, cv2.COLOR_GRAY2BGR)
        if alpha is not None:
            return sanitize_transparent(np.dstack([result, alpha]))
        return result
    if mode == 'binary':
        threshold = number(threshold, '全局阈值', 0, 255, True)
        binary = cv2.threshold(luminance, threshold, 255, cv2.THRESH_BINARY)[1]
    else:
        block_size = number(block_size, '自适应窗口', 3, 201, True)
        require(block_size % 2 == 1, '自适应窗口必须为奇数')
        constant = number(constant, '自适应常量', -50, 50)
        binary = cv2.adaptiveThreshold(luminance, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block_size, constant)
    result = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
    if alpha is not None:
        # 二值化只决定可见颜色，覆盖程度（alpha）原样保留。
        return sanitize_transparent(np.dstack([result, alpha]))
    return result


def rectangle(value, image):
    require(isinstance(value, (list, tuple)) and len(value) == 4, '区域需要[x,y,宽,高]')
    height, width = image.shape[:2]
    x = number(value[0], '区域x', 0, width - 1, True)
    y = number(value[1], '区域y', 0, height - 1, True)
    w = number(value[2], '区域宽', MIN_SIDE, width, True)
    h = number(value[3], '区域高', MIN_SIDE, height, True)
    require(x + w <= width and y + h <= height, '区域越出图片')
    return x, y, w, h


def denoise(image, area=8, median=3, region=None):
    area = number(area, '污点面积', 0, 100, True)
    median = number(median, '滤波窗口', 1, 9, True)
    require(median % 2 == 1, '滤波窗口必须为奇数')
    result = image.copy()
    if region is None:
        x, y, w, h = 0, 0, image.shape[1], image.shape[0]
    else:
        x, y, w, h = rectangle(region, image)
    source = result[y:y + h, x:x + w]
    work = cv2.medianBlur(source, median) if median > 1 else source.copy()
    mask = cv2.threshold(gray(work), 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    remove = np.zeros(mask.shape, np.uint8)
    removed = 0
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] <= area:
            remove[labels == label] = 255
            removed += 1
    if removed:
        if has_alpha(work):
            # 修复只作用于颜色通道；透明像素没有可修复的纸面颜色，alpha 保持不变。
            repaired = cv2.inpaint(work[:, :, :3], remove, 3, cv2.INPAINT_TELEA)
            work = np.dstack([repaired, work[:, :, 3]])
        else:
            work = cv2.inpaint(work, remove, 3, cv2.INPAINT_TELEA)
    result[y:y + h, x:x + w] = work
    return result, {'removed_components': removed, 'region': [x, y, w, h]}


def quality(image):
    luminance = gray(flatten(image) if has_alpha(image) else image)
    histogram = cv2.calcHist([luminance], [0], None, [256], [0, 256]).ravel()
    histogram /= max(1, histogram.sum())
    nonzero = histogram[histogram > 0]
    entropy = float(-(nonzero * np.log2(nonzero)).sum())
    return {
        'width': image.shape[1],
        'height': image.shape[0],
        'brightness': round(float(luminance.mean()), 2),
        'contrast': round(float(luminance.std()), 2),
        'sharpness': round(float(cv2.Laplacian(luminance, cv2.CV_64F).var()), 2),
        'entropy': round(entropy, 3),
        'dark_ratio': round(float((luminance < 80).mean()), 4),
        'light_ratio': round(float((luminance > 230).mean()), 4),
        'has_alpha': has_alpha(image),
        'transparent_ratio': round(float((image[:, :, 3] == 0).mean()), 4) if has_alpha(image) else 0.0,
    }


def process(image, operation, params):
    require(isinstance(params, dict), '参数必须为对象')
    metadata = {}
    if operation == 'perspective':
        points = params.get('points')
        if points is None:
            detection = detect_edges(image)
            require(detection['detected'], '未检测到可靠纸张边缘，请手动调整四角')
            points = detection['points']
        result, metadata = perspective(image, points)
    elif operation == 'rotate':
        result = rotate(image, params.get('angle', 90))
    elif operation == 'deskew':
        metadata = skew_angle(image)
        result = rotate(image, metadata['angle'])
    elif operation == 'illumination':
        result = illumination(image, params.get('strength', 0.7))
    elif operation == 'color':
        result = color_mode(image, params.get('mode', 'gray'), params.get('threshold', 160), params.get('block_size', 35), params.get('constant', 12))
    elif operation == 'denoise':
        result, metadata = denoise(image, params.get('area', 8), params.get('median', 3), params.get('region'))
    elif operation == 'crop':
        x, y, w, h = rectangle(params.get('region'), image)
        result = image[y:y + h, x:x + w].copy()
        metadata = {'region': [x, y, w, h]}
    elif operation == 'enhance':
        result = illumination(image, params.get('strength', 0.6))
        result = color_mode(result, params.get('mode', 'adaptive'), params.get('threshold', 160), params.get('block_size', 35), params.get('constant', 12))
        result, metadata = denoise(result, params.get('area', 3), params.get('median', 1))
    elif operation == 'flatten':
        paper = parse_paper_color(params.get('paper', REVIEW_PAPER_HEX), '铺底纸色')
        result = flatten(image, paper)
        metadata = {'paper': params.get('paper', REVIEW_PAPER_HEX)}
    else:
        from .filters import apply_filter
        result, metadata = apply_filter(image, operation, params)
    validate_image(result)
    return result, metadata
