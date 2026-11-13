from __future__ import annotations

import cv2
import numpy as np

from .core import choice, number, require
from .imaging import bgr, gray, rectangle, rotate, validate_image


def contrast(image, params):
    alpha = number(params.get('contrast', 1.15), '对比度', 0.1, 3)
    beta = number(params.get('brightness', 0), '亮度', -100, 100)
    result = image.astype(np.float32) * alpha + beta
    return np.clip(result, 0, 255).astype(np.uint8)


def clahe(image, params):
    clip = number(params.get('clip', 2), '局部对比度', 0.5, 8)
    grid = number(params.get('grid', 8), '局部网格', 2, 32, True)
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    channels = list(cv2.split(lab))
    engine = cv2.createCLAHE(clipLimit=clip, tileGridSize=(grid, grid))
    channels[0] = engine.apply(channels[0])
    return cv2.cvtColor(cv2.merge(channels), cv2.COLOR_LAB2BGR)


def sharpen(image, params):
    amount = number(params.get('amount', 0.8), '锐化强度', 0, 3)
    radius = number(params.get('radius', 1.2), '锐化半径', 0.1, 5)
    threshold = number(params.get('threshold', 3), '锐化阈值', 0, 50)
    smooth = cv2.GaussianBlur(image, (0, 0), radius)
    delta = image.astype(np.float32) - smooth.astype(np.float32)
    mask = np.max(np.abs(delta), axis=2) >= threshold
    enhanced = np.clip(image.astype(np.float32) + amount * delta, 0, 255).astype(np.uint8)
    result = image.copy()
    result[mask] = enhanced[mask]
    return result


def white_balance(image, params):
    strength = number(params.get('strength', 0.7), '白平衡强度', 0, 1)
    method = choice(params.get('method', 'white_patch'), '白平衡方式', ('white_patch', 'gray_world'))
    source = image.astype(np.float32)
    if method == 'gray_world':
        means = source.reshape(-1, 3).mean(axis=0)
        target = float(means.mean())
        gains = target / np.maximum(means, 1)
    else:
        white = np.percentile(source.reshape(-1, 3), 97, axis=0)
        gains = 250 / np.maximum(white, 1)
    gains = 1 + (np.clip(gains, 0.5, 2) - 1) * strength
    return np.clip(source * gains, 0, 255).astype(np.uint8)


def gamma(image, params):
    value = number(params.get('gamma', 1.2), '伽马值', 0.2, 4)
    table = np.array([min(255, round((index / 255) ** (1 / value) * 255)) for index in range(256)], np.uint8)
    return cv2.LUT(image, table)


def percentile_stretch(image, params):
    lower = number(params.get('lower', 1), '暗部百分位', 0, 20)
    upper = number(params.get('upper', 99), '亮部百分位', 80, 100)
    require(upper > lower, '亮部百分位必须大于暗部百分位')
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    light = lab[:, :, 0].astype(np.float32)
    low, high = np.percentile(light, [lower, upper])
    if high - low < 1:
        return image.copy()
    lab[:, :, 0] = np.clip((light - low) * 255 / (high - low), 0, 255).astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def bilateral(image, params):
    diameter = number(params.get('diameter', 5), '双边滤波直径', 3, 15, True)
    sigma_color = number(params.get('sigma_color', 35), '色彩平滑', 1, 150)
    sigma_space = number(params.get('sigma_space', 35), '空间平滑', 1, 150)
    return cv2.bilateralFilter(image, diameter, sigma_color, sigma_space)


def remove_border(image, params):
    size = number(params.get('size', 12), '边框宽度', 1, 300, True)
    height, width = image.shape[:2]
    require(size * 2 < min(width, height), '边框宽度过大')
    result = image.copy()
    result[:size] = 255
    result[-size:] = 255
    result[:, :size] = 255
    result[:, -size:] = 255
    return result


def pad(image, params):
    top = number(params.get('top', 20), '上边距', 0, 1000, True)
    bottom = number(params.get('bottom', 20), '下边距', 0, 1000, True)
    left = number(params.get('left', 20), '左边距', 0, 1000, True)
    right = number(params.get('right', 20), '右边距', 0, 1000, True)
    height, width = image.shape[:2]
    require((height + top + bottom) * (width + left + right) <= 36_000_000, '加边后图片过大')
    return cv2.copyMakeBorder(image, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(255, 255, 255))


def resize(image, params):
    height, width = image.shape[:2]
    scale = number(params.get('scale', 1), '缩放倍率', 0.1, 4)
    target_width = round(width * scale)
    target_height = round(height * scale)
    require(min(target_width, target_height) >= 32, '缩放后图片过小')
    require(max(target_width, target_height) <= 16000, '缩放后边长过大')
    require(target_width * target_height <= 36_000_000, '缩放后像素过多')
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    return cv2.resize(image, (target_width, target_height), interpolation=interpolation)


def erase(image, params):
    x, y, width, height = rectangle(params.get('region'), image)
    mode = choice(params.get('mode', 'white'), '擦除方式', ('white', 'inpaint'))
    result = image.copy()
    if mode == 'white':
        result[y:y + height, x:x + width] = 255
        return result
    mask = np.zeros(image.shape[:2], np.uint8)
    mask[y:y + height, x:x + width] = 255
    radius = number(params.get('radius', 5), '修复半径', 1, 20)
    return cv2.inpaint(image, mask, radius, cv2.INPAINT_TELEA)


def morph_text(image, params):
    """兼容旧入口的文字形态调整：委托给彩色笔迹调整管线，不再整页二值化。

    新功能请使用 /pages/{id}/stroke/preview 与 /stroke/confirm，
    支持选区、分区保护、差异预览与确认后落盘。此处保留操作名以兼容
    既有工作流与历史记录，输出保持原笔迹颜色和纸面。
    """
    from . import stroke
    legacy_mode = params.get('mode', 'thicken')
    mode = 'thin' if legacy_mode in ('thin', 'open') else 'thicken'
    size = number(params.get('size', 2), '形态窗口', 1, 7, True)
    iterations = number(params.get('iterations', 1), '形态次数', 1, 3, True)
    settings = stroke.validate_settings({
        'mode': mode,
        'delta': float(size) * 0.75 * iterations,
        'sensitivity': 0.5,
    }, image)
    result, _, _ = stroke.adjust(image, settings)
    return result


def sauvola(image, params):
    window = number(params.get('window', 41), 'Sauvola 窗口', 3, 201, True)
    require(window % 2 == 1, '窗口必须为奇数')
    k = number(params.get('k', 0.2), 'Sauvola 系数', 0.01, 0.8)
    source = gray(image).astype(np.float32)
    mean = cv2.boxFilter(source, -1, (window, window), borderType=cv2.BORDER_REFLECT)
    squared = cv2.boxFilter(source ** 2, -1, (window, window), borderType=cv2.BORDER_REFLECT)
    deviation = np.sqrt(np.maximum(squared - mean ** 2, 0))
    threshold = mean * (1 + k * (deviation / 128 - 1))
    result = np.where(source > threshold, 255, 0).astype(np.uint8)
    return bgr(result)


def remove_lines(image, params):
    orientation = choice(params.get('orientation', 'both'), '线条方向', ('horizontal', 'vertical', 'both'))
    length = number(params.get('length', 80), '最短线长', 20, 1000, True)
    mask = cv2.threshold(gray(image), 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    combined = np.zeros(mask.shape, np.uint8)
    if orientation in ('horizontal', 'both'):
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (length, 1))
        horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        combined = cv2.bitwise_or(combined, horizontal)
    if orientation in ('vertical', 'both'):
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, length))
        vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        combined = cv2.bitwise_or(combined, vertical)
    combined = cv2.dilate(combined, np.ones((3, 3), np.uint8))
    return cv2.inpaint(image, combined, 3, cv2.INPAINT_TELEA)


def flatten_background(image, params):
    radius = number(params.get('radius', 35), '背景估计半径', 7, 151, True)
    require(radius % 2 == 1, '背景估计半径必须为奇数')
    strength = number(params.get('strength', 0.8), '背景归一化强度', 0, 1)
    luminance = gray(image)
    scale = min(1, 1400 / max(luminance.shape))
    small = cv2.resize(luminance, None, fx=scale, fy=scale)
    background = cv2.medianBlur(small, radius)
    background = cv2.resize(background, (luminance.shape[1], luminance.shape[0]))
    corrected = cv2.divide(luminance, np.maximum(background, 1), scale=250)
    blended = cv2.addWeighted(luminance, 1 - strength, corrected, strength, 0)
    return bgr(blended)


def invert(image, params):
    return 255 - image


def flip(image, params):
    axis = choice(params.get('axis', 'horizontal'), '翻转方向', ('horizontal', 'vertical'))
    return cv2.flip(image, 1 if axis == 'horizontal' else 0)


FILTERS = {
    'contrast': contrast,
    'clahe': clahe,
    'sharpen': sharpen,
    'white_balance': white_balance,
    'gamma': gamma,
    'stretch': percentile_stretch,
    'bilateral': bilateral,
    'remove_border': remove_border,
    'pad': pad,
    'resize': resize,
    'erase': erase,
    'morph_text': morph_text,
    'sauvola': sauvola,
    'remove_lines': remove_lines,
    'flatten_background': flatten_background,
    'invert': invert,
    'flip': flip,
}


def apply_filter(image, operation, params):
    require(operation in FILTERS, '不支持的高级滤镜')
    result = FILTERS[operation](image, params)
    validate_image(result)
    return result, {'filter': operation}
