from __future__ import annotations

import math
from collections import Counter

import cv2
import numpy as np

from .core import choice, number, require
from .imaging import flatten, gray, has_alpha, resize_alpha


def opaque(image):
    """分析需要不透明输入时，按审阅白纸临时铺底（不改写原件）。"""
    return flatten(image) if has_alpha(image) else image


def reduced(image, longest=1800):
    height, width = image.shape[:2]
    ratio = min(1, longest / max(height, width))
    result = resize_alpha(image, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
    return result, ratio


def histogram(image):
    image = opaque(image)
    result = {}
    for index, name in enumerate(('blue', 'green', 'red')):
        values = cv2.calcHist([image], [index], None, [256], [0, 256])
        result[name] = values.ravel().astype(int).tolist()
    luminance = gray(image)
    values = cv2.calcHist([luminance], [0], None, [256], [0, 256])
    result['luminance'] = values.ravel().astype(int).tolist()
    result['pixels'] = int(luminance.size)
    return result


def percentiles(image):
    luminance = gray(image)
    levels = [1, 5, 10, 25, 50, 75, 90, 95, 99]
    values = np.percentile(luminance, levels)
    result = {}
    for level, value in zip(levels, values):
        result[str(level)] = round(float(value), 2)
    return result


def tile_statistics(image, rows=4, columns=4):
    rows = number(rows, '网格行数', 2, 16, True)
    columns = number(columns, '网格列数', 2, 16, True)
    luminance = gray(image)
    height, width = luminance.shape
    results = []
    for row in range(rows):
        for column in range(columns):
            x1 = round(column * width / columns)
            x2 = round((column + 1) * width / columns)
            y1 = round(row * height / rows)
            y2 = round((row + 1) * height / rows)
            tile = luminance[y1:y2, x1:x2]
            if not tile.size:
                continue
            result = {
                'row': row,
                'column': column,
                'rect': [x1, y1, x2 - x1, y2 - y1],
                'mean': round(float(tile.mean()), 2),
                'std': round(float(tile.std()), 2),
                'background': round(float(np.percentile(tile, 90)), 2),
                'dark_ratio': round(float((tile < 80).mean()), 4),
            }
            results.append(result)
    return results


def illumination_score(image):
    tiles = tile_statistics(image)
    backgrounds = [tile['background'] for tile in tiles]
    difference = max(backgrounds) - min(backgrounds)
    deviation = float(np.std(backgrounds))
    score = max(0, 100 - difference * 0.5 - deviation)
    return {
        'score': round(score, 2),
        'range': round(difference, 2),
        'standard_deviation': round(deviation, 2),
        'tiles': tiles,
        'needs_repair': difference > 35,
    }


def blur_score(image):
    small, ratio = reduced(image)
    luminance = gray(small)
    laplacian = cv2.Laplacian(luminance, cv2.CV_64F)
    sharpness = float(laplacian.var())
    sobel_x = cv2.Sobel(luminance, cv2.CV_32F, 1, 0)
    sobel_y = cv2.Sobel(luminance, cv2.CV_32F, 0, 1)
    magnitude = cv2.magnitude(sobel_x, sobel_y)
    energy = float(np.mean(magnitude))
    maximum = float(np.percentile(magnitude, 95))
    score = min(100, math.log1p(sharpness) / math.log(1001) * 100)
    return {
        'laplacian_variance': round(sharpness, 3),
        'gradient_energy': round(energy, 3),
        'gradient_p95': round(maximum, 3),
        'score': round(score, 2),
        'scale': ratio,
        'possibly_blurred': sharpness < 40,
    }


def margin_analysis(image):
    small, scale = reduced(image)
    luminance = gray(small)
    mask = cv2.threshold(luminance, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    horizontal = np.mean(mask > 0, axis=1)
    vertical = np.mean(mask > 0, axis=0)
    active_rows = np.where(horizontal > 0.005)[0]
    active_columns = np.where(vertical > 0.005)[0]
    if not len(active_rows) or not len(active_columns):
        return {'blank': True, 'content_rect': None, 'margins': None}
    top = int(active_rows[0] / scale)
    bottom = int(active_rows[-1] / scale)
    left = int(active_columns[0] / scale)
    right = int(active_columns[-1] / scale)
    height, width = image.shape[:2]
    return {
        'blank': False,
        'content_rect': [left, top, right - left + 1, bottom - top + 1],
        'margins': {
            'top': top,
            'bottom': max(0, height - bottom - 1),
            'left': left,
            'right': max(0, width - right - 1),
        },
        'row_projection': horizontal.round(4).tolist(),
        'column_projection': vertical.round(4).tolist(),
    }


def text_components(image):
    small, scale = reduced(image)
    luminance = gray(small)
    mask = cv2.adaptiveThreshold(luminance, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 35, 15)
    count, labels, stats, centers = cv2.connectedComponentsWithStats(mask, 8)
    components = []
    specks = 0
    large = 0
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        if area <= 4:
            specks += 1
            continue
        if area > small.shape[0] * small.shape[1] * 0.05:
            large += 1
            continue
        if w <= 1 or h <= 2:
            continue
        component = {
            'x': round(x / scale),
            'y': round(y / scale),
            'width': round(w / scale),
            'height': round(h / scale),
            'area': round(area / scale ** 2),
            'density': round(float(area / (w * h)), 3),
        }
        components.append(component)
    heights = [component['height'] for component in components]
    widths = [component['width'] for component in components]
    return {
        'components': components[:20000],
        'total': len(components),
        'specks': specks,
        'large_regions': large,
        'median_height': round(float(np.median(heights)), 2) if heights else 0,
        'median_width': round(float(np.median(widths)), 2) if widths else 0,
    }


def perceptual_hash(image):
    luminance = cv2.resize(gray(image), (32, 32), interpolation=cv2.INTER_AREA)
    transformed = cv2.dct(luminance.astype(np.float32))
    low = transformed[:8, :8]
    threshold = np.median(low.ravel()[1:])
    bits = (low > threshold).ravel()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f'{value:016x}'


def difference_hash(image):
    luminance = cv2.resize(gray(image), (9, 8), interpolation=cv2.INTER_AREA)
    bits = (luminance[:, 1:] > luminance[:, :-1]).ravel()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f'{value:016x}'


def hash_distance(first, second):
    require(isinstance(first, str) and isinstance(second, str), '图像指纹无效')
    try:
        return (int(first, 16) ^ int(second, 16)).bit_count()
    except ValueError:
        raise ValueError('图像指纹必须为十六进制字符串')


def compare_images(first, second):
    first = opaque(first)
    second = opaque(second)
    target_width = min(first.shape[1], second.shape[1], 1200)
    target_height = min(first.shape[0], second.shape[0], 1600)
    first_gray = cv2.resize(gray(first), (target_width, target_height)).astype(np.float32)
    second_gray = cv2.resize(gray(second), (target_width, target_height)).astype(np.float32)
    error = first_gray - second_gray
    mean_squared = float(np.mean(error ** 2))
    absolute = float(np.mean(np.abs(error)))
    first_mean = cv2.GaussianBlur(first_gray, (11, 11), 1.5)
    second_mean = cv2.GaussianBlur(second_gray, (11, 11), 1.5)
    first_variance = cv2.GaussianBlur(first_gray ** 2, (11, 11), 1.5) - first_mean ** 2
    second_variance = cv2.GaussianBlur(second_gray ** 2, (11, 11), 1.5) - second_mean ** 2
    covariance = cv2.GaussianBlur(first_gray * second_gray, (11, 11), 1.5) - first_mean * second_mean
    numerator = (2 * first_mean * second_mean + 6.5025) * (2 * covariance + 58.5225)
    denominator = (first_mean ** 2 + second_mean ** 2 + 6.5025) * (first_variance + second_variance + 58.5225)
    similarity = float(np.mean(numerator / np.maximum(denominator, 1e-6)))
    return {
        'mean_squared_error': round(mean_squared, 4),
        'mean_absolute_error': round(absolute, 4),
        'structural_similarity': round(similarity, 5),
        'hash_distance': hash_distance(perceptual_hash(first), perceptual_hash(second)),
        'same_dimensions': first.shape == second.shape,
        'compared_dimensions': [target_width, target_height],
    }


def color_statistics(image):
    image = opaque(image)
    pixels = image.reshape(-1, 3)
    means = np.mean(pixels, axis=0)
    deviations = np.std(pixels, axis=0)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    saturation = hsv[:, :, 1]
    colored = saturation > 45
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    result = {
        'mean_rgb': [round(float(value), 2) for value in means[::-1]],
        'std_rgb': [round(float(value), 2) for value in deviations[::-1]],
        'saturation_mean': round(float(saturation.mean()), 2),
        'colored_ratio': round(float(colored.mean()), 4),
        'lightness_mean': round(float(lab[:, :, 0].mean()), 2),
        'likely_grayscale': float(saturation.mean()) < 8,
    }
    return result


def recommend(image):
    light = illumination_score(image)
    blur = blur_score(image)
    colors = color_statistics(image)
    components = text_components(image)
    margins = margin_analysis(image)
    steps = []
    warnings = []
    if margins['blank']:
        warnings.append('此页可能为空白，建议检查后删除或保留。')
    if light['needs_repair']:
        strength = min(1, max(0.3, light['range'] / 120))
        steps.append({'operation': 'illumination', 'params': {'strength': round(strength, 2)}, 'reason': '检测到背景光照不均'})
    if blur['possibly_blurred']:
        warnings.append('图像可能模糊，软件增强无法恢复丢失的文字细节，建议重新拍摄。')
    if components['specks'] > 100:
        steps.append({'operation': 'denoise', 'params': {'area': 3, 'median': 1}, 'reason': '检测到较多孤立小点'})
    if not colors['likely_grayscale'] and colors['colored_ratio'] < 0.15:
        steps.append({'operation': 'color', 'params': {'mode': 'gray'}, 'reason': '以文字为主，可转为灰度减少色彩干扰'})
    return {
        'steps': steps,
        'warnings': warnings,
        'illumination': light,
        'sharpness': blur,
        'color': colors,
        'components': {key: value for key, value in components.items() if key != 'components'},
        'blank': margins['blank'],
    }


def full_analysis(image):
    return {
        'histogram': histogram(image),
        'percentiles': percentiles(image),
        'illumination': illumination_score(image),
        'blur': blur_score(image),
        'margins': margin_analysis(image),
        'color': color_statistics(image),
        'phash': perceptual_hash(image),
        'dhash': difference_hash(image),
        'recommendations': recommend(image),
    }
