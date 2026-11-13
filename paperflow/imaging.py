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
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def validate_image(image):
    height, width = image.shape[:2]
    require(MIN_SIDE <= width <= MAX_SIDE and MIN_SIDE <= height <= MAX_SIDE, '图片宽高必须在32–16000像素之间')
    require(width * height <= MAX_PIXELS, '图片最多3600万像素')
    return image


def read_image(path):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as original:
                require(original.format in ('PNG', 'JPEG', 'TIFF', 'BMP', 'WEBP'), '不支持此图片格式')
                require(original.width * original.height <= MAX_PIXELS, '图片像素过大')
                normalized = ImageOps.exif_transpose(original)
                if 'A' in normalized.getbands():
                    background = Image.new('RGB', normalized.size, 'white')
                    background.paste(normalized, mask=normalized.getchannel('A'))
                    normalized = background
                rgb = np.asarray(normalized.convert('RGB'))
                result = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
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


def thumbnail(path, image, size=240):
    height, width = image.shape[:2]
    ratio = min(size / width, size / height, 1)
    small = cv2.resize(image, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
    success, encoded = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 86])
    require(success, '缩略图生成失败', 500)
    Path(path).write_bytes(encoded.tobytes())


def gray(image):
    if image.ndim == 2:
        return image
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
    small = cv2.resize(image, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
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
    result = cv2.warpPerspective(image, matrix, (target_width, target_height), borderValue=(255, 255, 255))
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
    return cv2.warpAffine(image, matrix, (new_width, new_height), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))


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
    lab = cv2.cvtColor(bgr(image), cv2.COLOR_BGR2LAB)
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
    return result


def color_mode(image, mode, threshold=160, block_size=35, constant=12):
    choice(mode, '色彩模式', ('color', 'gray', 'binary', 'adaptive'))
    if mode == 'color':
        return bgr(image)
    luminance = gray(image)
    if mode == 'gray':
        return bgr(luminance)
    if mode == 'binary':
        threshold = number(threshold, '全局阈值', 0, 255, True)
        result = cv2.threshold(luminance, threshold, 255, cv2.THRESH_BINARY)[1]
    else:
        block_size = number(block_size, '自适应窗口', 3, 201, True)
        require(block_size % 2 == 1, '自适应窗口必须为奇数')
        constant = number(constant, '自适应常量', -50, 50)
        result = cv2.adaptiveThreshold(luminance, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block_size, constant)
    return bgr(result)


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
        work = cv2.inpaint(work, remove, 3, cv2.INPAINT_TELEA)
    result[y:y + h, x:x + w] = work
    return result, {'removed_components': removed, 'region': [x, y, w, h]}


def quality(image):
    luminance = gray(image)
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
    elif operation == 'line_work':
        from .linework import apply_plan, paper_background
        result, metadata = apply_plan(image, params.get('plan'), paper_background(image))
    else:
        from .filters import apply_filter
        result, metadata = apply_filter(image, operation, params)
    validate_image(result)
    return result, metadata
