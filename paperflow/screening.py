"""印刷网纹（halftone screen / moiré）检测与分区域抑制。

设计原则：
- 检测与抑制分离，检测只给证据与置信度，不直接修改图像；
- 高/中/待定三级结论，依据不足时置信度为 0 并给出保留原因，默认不处理；
- 文字、细线由像素级保护掩膜排除；织物等大间距正交纹理落入“待定”；
- 抑制只在用户确认区域的软掩膜内生效，不同方向/间距/颜色的网点按各自频谱峰做陷波；
- 同一 review 始终以同一基线图渲染，重复调整不会把已抑制网点当成新细节继续抹除。
"""
from __future__ import annotations

import cv2
import numpy as np

from .core import number, require

TILE = 128
STEP = 64
MIN_SPACING = 2.5
MAX_SPACING = 16.0
HANNING = np.hanning(TILE)[:, None] * np.hanning(TILE)[None, :]

CONFIDENCE_HIGH = 'high'
CONFIDENCE_MEDIUM = 'medium'
CONFIDENCE_UNCERTAIN = 'uncertain'
CONFIDENCE_NONE = 'none'


def _ring(spec):
    cy = cx = TILE // 2
    y_grid, x_grid = np.mgrid[0:TILE, 0:TILE]
    radius = np.hypot(x_grid - cx, y_grid - cy)
    ring = (radius > 4) & (radius < TILE * 0.45)
    return ring, radius, (cx, cy), (x_grid, y_grid)


def _chroma_prominence(lab_tile, ring, window):
    """彩色油墨网点在 a/b 色度通道同样具有周期峰，文字与织物通常没有。"""
    best = 0.0
    for channel in (1, 2):
        values = lab_tile[:, :, channel].astype(np.float32)
        values -= values.mean()
        spectrum = np.abs(np.fft.fftshift(np.fft.fft2(values * window)))
        baseline = np.median(spectrum[ring]) + 1e-6
        best = max(best, float((spectrum / baseline)[ring].max()))
    return best


def _stroke_count(gray):
    """自适应阈值下的笔画状连通组件数量，用于区分文字行。"""
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 10
    )
    count, _, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    strokes = 0
    for index in range(1, count):
        area = stats[index, cv2.CC_STAT_AREA]
        width = stats[index, cv2.CC_STAT_WIDTH]
        height = stats[index, cv2.CC_STAT_HEIGHT]
        if 6 <= area <= 400 and width <= 40 and height <= 40 and area / max(1, width * height) > 0.25:
            strokes += 1
    return strokes


def analyze_tile(tile_bgr, lab_tile):
    """返回单个 128px 图块的频谱证据与分级结论。"""
    gray = cv2.cvtColor(tile_bgr, cv2.COLOR_BGR2GRAY)
    values = gray.astype(np.float32)
    values -= values.mean()
    spectrum = np.abs(np.fft.fftshift(np.fft.fft2(values * HANNING)))
    ring, radius, (cx, cy), _ = _ring(spectrum)
    baseline = np.median(spectrum[ring]) + 1e-6
    prominence = spectrum / baseline
    ring_prominence = np.where(ring, prominence, 0)
    sharpness = float(ring_prominence.max())
    index = np.argmax(ring_prominence)
    peak_y, peak_x = np.unravel_index(index, ring_prominence.shape)
    fx, fy = peak_x - cx, peak_y - cy
    peak_radius = float(np.hypot(fx, fy))
    spacing = TILE / peak_radius if peak_radius > 0 else 999.0
    angle = float(np.degrees(np.arctan2(fy, fx))) % 180.0

    strong = ring & (prominence > 4)
    family = strong & (np.abs(radius - peak_radius) <= max(2, 0.22 * peak_radius))
    family_y, family_x = np.where(family)
    points = set(zip(family_x.tolist(), family_y.tolist()))
    symmetry = (
        sum(1 for x, y in points if (2 * cx - x, 2 * cy - y) in points) / max(1, len(points))
    )

    autocorr = np.fft.fftshift(np.abs(np.fft.ifft2(spectrum ** 2)))
    autocorr /= autocorr[cy, cx] + 1e-9
    autocorr_search = autocorr.copy()
    autocorr_search[radius <= 4] = 0
    with np.errstate(divide='ignore', invalid='ignore'):
        lag = TILE / np.where(radius == 0, np.nan, radius)
    periodicity = float(np.nanmax(np.where((lag >= MIN_SPACING) & (lag <= MAX_SPACING), autocorr_search, 0)))

    chroma = _chroma_prominence(lab_tile, ring, HANNING)
    strokes = _stroke_count(gray)

    evidence = {
        'sharpness': round(sharpness, 2),
        'spacing': round(float(spacing), 2),
        'angle': round(angle, 1),
        'symmetry': round(float(symmetry), 3),
        'family_peaks': len(points),
        'autocorrelation': round(periodicity, 3),
        'chroma_prominence': round(chroma, 2),
        'stroke_components': strokes,
    }

    periodic = sharpness > 12 and symmetry >= 0.6 and len(points) >= 4 and periodicity > 0.3
    if not periodic:
        return 0.0, CONFIDENCE_NONE, evidence, '无稳定周期峰，判定为普通内容，保留'
    if not (MIN_SPACING <= spacing <= MAX_SPACING):
        evidence['spacing_out_of_range'] = True
        return 0.0, CONFIDENCE_UNCERTAIN, evidence, (
            f'周期间距 {spacing:.1f}px 超出常见网点 {MIN_SPACING:g}–{MAX_SPACING:g}px 范围，'
            '疑似织物或大尺寸重复纹理，依据不足，保留待确认'
        )
    if strokes >= 12 and chroma < 10:
        evidence['text_like'] = True
        return 0.0, CONFIDENCE_UNCERTAIN, evidence, (
            '检测到密集笔画状组件且无彩色油墨调制，疑似文字或细线，默认保护'
        )
    confidence = min(1.0, np.tanh(sharpness / 40.0) * 0.6 + min(periodicity, 1.0) * 0.4)
    if chroma >= 15 and sharpness > 25:
        evidence['colored_ink'] = True
        return (
            min(1.0, confidence + 0.2),
            CONFIDENCE_HIGH,
            evidence,
            f'彩色油墨周期峰 {sharpness:.0f}× 中位数，间距 {spacing:.1f}px，角度 {angle:.0f}°，依据充分',
        )
    if sharpness > 40 and periodicity > 0.5 and strokes < 12:
        evidence['grayscale_screen'] = True
        return (
            min(0.85, confidence * 0.8),
            CONFIDENCE_MEDIUM,
            evidence,
            f'灰度周期峰 {sharpness:.0f}× 但缺少彩色油墨佐证，建议人工确认后再抑制',
        )
    return 0.0, CONFIDENCE_UNCERTAIN, evidence, '存在周期信号但强度与结构依据不足，保留并标注'


def text_protection_mask(image):
    """像素级文字/细线保护掩膜，取值 0..1，1 表示完全保护。"""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 10
    )
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    protected = np.zeros(gray.shape, np.uint8)
    for index in range(1, count):
        width = stats[index, cv2.CC_STAT_WIDTH]
        height = stats[index, cv2.CC_STAT_HEIGHT]
        area = stats[index, cv2.CC_STAT_AREA]
        if area < 6:
            continue
        aspect = max(width, height) / max(1, min(width, height))
        if (area <= 400 and width <= 42 and height <= 42) or aspect > 3.2:
            protected[labels == index] = 255
    protected = cv2.dilate(protected, np.ones((3, 3), np.uint8), iterations=1)
    return cv2.GaussianBlur(protected, (0, 0), 2.0) / 255.0


def _grid_positions(height, width):
    rows = max(1, (height - TILE) // STEP + 1)
    cols = max(1, (width - TILE) // STEP + 1)
    positions = []
    for row in range(rows):
        y0 = min(row * STEP, max(0, height - TILE))
        for col in range(cols):
            x0 = min(col * STEP, max(0, width - TILE))
            positions.append((row, col, y0, x0))
    return rows, cols, positions


def _extract_tile(image, y0, x0, lab=False):
    """裁剪图块；图像不足 128px 时反射填充，保证 FFT 窗口尺寸恒定。"""
    source = image
    height, width = source.shape[:2]
    y1, x1 = min(y0 + TILE, height), min(x0 + TILE, width)
    tile = source[y0:y1, x0:x1]
    if tile.shape[0] < TILE or tile.shape[1] < TILE:
        pad_bottom = TILE - tile.shape[0]
        pad_right = TILE - tile.shape[1]
        tile = cv2.copyMakeBorder(tile, 0, pad_bottom, 0, pad_right, cv2.BORDER_REFLECT)
    return tile


def build_screen_mask(image, tile_confidences):
    """把图块置信度网格放大为全分辨率软掩膜，并用文字保护掩膜扣除。"""
    height, width = image.shape[:2]
    rows = max(1, (height - TILE) // STEP + 1)
    cols = max(1, (width - TILE) // STEP + 1)
    grid = np.asarray(tile_confidences, dtype=np.float32).reshape(rows, cols)
    dense = cv2.resize(grid, (width, height), interpolation=cv2.INTER_CUBIC)
    dense = cv2.GaussianBlur(np.clip(dense, 0, 1), (0, 0), STEP / 2)
    protection = text_protection_mask(image)
    return np.clip(np.clip(dense, 0, 1) * (1 - protection), 0, 1)


def _notch_peaks(gray_tile):
    """每个图块独立找陷波峰，兼容同一页面内不同方向/间距/颜色的网点。"""
    values = gray_tile.astype(np.float32)
    values -= values.mean()
    spectrum = np.abs(np.fft.fftshift(np.fft.fft2(values * HANNING)))
    ring, _, (cx, cy), _ = _ring(spectrum)
    baseline = np.median(spectrum[ring]) + 1e-6
    prominence = spectrum / baseline
    dilated = cv2.dilate(spectrum, np.ones((5, 5), np.uint8))
    local_max = (spectrum == dilated) & ring & (prominence > 5)
    peak_y, peak_x = np.where(local_max)
    order = np.argsort(prominence[peak_y, peak_x])[::-1]
    peaks, seen = [], set()
    for order_index in order:
        x, y = int(peak_x[order_index]), int(peak_y[order_index])
        if any(abs(x - px) + abs(y - py) < 4 for px, py in seen):
            continue
        seen.add((x, y))
        peaks.append((x - cx, y - cy))
        if len(peaks) >= 6:
            break
    return peaks


def suppress_tile(tile_bgr, strength, notch_sigma=2.2):
    gray = cv2.cvtColor(tile_bgr, cv2.COLOR_BGR2GRAY)
    peaks = _notch_peaks(gray)
    cy = cx = TILE // 2
    _, _, _, (x_grid, y_grid) = _ring(np.zeros((TILE, TILE), np.float32))
    notch = np.ones((TILE, TILE), np.float32)
    for fx, fy in peaks:
        for multiple in (1, 2):
            for sign in (1, -1):
                distance2 = (x_grid - (cx + sign * fx * multiple)) ** 2
                distance2 += (y_grid - (cy + sign * fy * multiple)) ** 2
                notch *= 1 - strength * np.exp(-distance2 / (2 * notch_sigma ** 2))
    result = np.empty_like(tile_bgr, np.float32)
    for channel in range(3):
        transformed = np.fft.fftshift(np.fft.fft2(tile_bgr[:, :, channel].astype(np.float32)))
        result[:, :, channel] = np.real(np.fft.ifft2(np.fft.ifftshift(transformed * notch)))
    result = np.clip(result, 0, 255)
    # 轻度双边滤波清理残余颗粒，强度由用户控制，边缘不被抹平
    smoothed = cv2.bilateralFilter(
        result.astype(np.uint8), 5, 25 + 35 * strength, 3 + 3 * strength
    )
    blend = 0.3 * strength
    return np.clip(result * (1 - blend) + smoothed.astype(np.float32) * blend, 0, 255)


def render(image, mask, strength):
    """在软掩膜内合成抑制结果；掩膜外像素与输入逐位一致。"""
    strength = number(strength, '抑制强度', 0, 1)
    require(mask.shape == image.shape[:2], '网纹掩膜尺寸与图像不一致')
    height, width = image.shape[:2]
    window = HANNING / HANNING.max()
    accumulated = np.zeros((height, width, 3), np.float32)
    weights = np.zeros((height, width), np.float32)
    _, _, positions = _grid_positions(height, width)
    for _, _, y0, x0 in positions:
        tile = _extract_tile(image, y0, x0)
        suppressed = suppress_tile(tile, strength)
        valid_h = min(TILE, height - y0)
        valid_w = min(TILE, width - x0)
        accumulated[y0:y0 + valid_h, x0:x0 + valid_w] += (
            suppressed[:valid_h, :valid_w] * window[:valid_h, :valid_w, None]
        )
        weights[y0:y0 + valid_h, x0:x0 + valid_w] += window[:valid_h, :valid_w]
    suppressed_full = accumulated / np.maximum(weights[:, :, None], 1e-6)
    soft = np.clip(mask, 0, 1)[:, :, None]
    output = image.astype(np.float32) * (1 - soft) + suppressed_full * soft
    return np.clip(output, 0, 255).astype(np.uint8)


def screen_energy(image):
    """整图或区域的周期峰锐度，用于量化抑制前后的波纹能量。"""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32)
    height, width = gray.shape
    side = min(height, width, 256)
    if side < 32:
        return 0.0
    resized = cv2.resize(gray, (side, side), interpolation=cv2.INTER_AREA)
    resized -= resized.mean()
    window = np.hanning(side)[:, None] * np.hanning(side)[None, :]
    spectrum = np.abs(np.fft.fftshift(np.fft.fft2(resized * window)))
    cy = cx = side // 2
    y_grid, x_grid = np.mgrid[0:side, 0:side]
    radius = np.hypot(x_grid - cx, y_grid - cy)
    ring = (radius > 4) & (radius < side * 0.45)
    if not ring.any():
        return 0.0
    return float(spectrum[ring].max() / (np.median(spectrum[ring]) + 1e-6))


def detail_loss(before, after, mask):
    """抑制区域内高频细节变化（0..1），供用户比较波纹抑制与真实细节损失。"""
    if mask.max() <= 1e-6:
        return 0.0
    before_gray = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY).astype(np.float32)
    after_gray = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY).astype(np.float32)
    high_before = before_gray - cv2.GaussianBlur(before_gray, (0, 0), 1.5)
    high_after = after_gray - cv2.GaussianBlur(after_gray, (0, 0), 1.5)
    lost = np.abs(high_before - high_after)
    selected = mask > 0.2
    return round(float(lost[selected].mean() / 255.0), 4)


def overlay_image(image, mask, regions):
    """生成网纹区域叠加图，高置信偏橙、中置信偏黄、待定偏蓝，保护区域不叠加。"""
    canvas = image.copy()
    color = {
        CONFIDENCE_HIGH: (40, 145, 240),
        CONFIDENCE_MEDIUM: (40, 210, 235),
        CONFIDENCE_UNCERTAIN: (220, 120, 60),
    }
    for region in regions:
        x, y, width, height = region['rect']
        layer = canvas[y:y + height, x:x + width]
        tint = np.full_like(layer, color.get(region['confidence_level'], (200, 200, 200)))
        alpha = 0.28
        canvas[y:y + height, x:x + width] = cv2.addWeighted(layer, 1 - alpha, tint, alpha, 0)
        cv2.rectangle(canvas, (x, y), (x + width - 1, y + height - 1),
                      color.get(region['confidence_level'], (200, 200, 200)), 3)
    if mask is not None and mask.size:
        soft = np.clip(mask, 0, 1)
        # 热力图只在实际掩膜（>0.05）处叠加，零掩膜区域保持原图可读
        active = (soft > 0.05).astype(np.float32)
        colored = cv2.applyColorMap((soft * 255).astype(np.uint8), cv2.COLORMAP_PLASMA)
        weight = (active * 0.35)[:, :, None]
        canvas = np.clip(
            canvas.astype(np.float32) * (1 - weight) + colored.astype(np.float32) * weight,
            0, 255,
        ).astype(np.uint8)
        edge = cv2.Canny((soft * 255).astype(np.uint8), 30, 90)
        canvas[edge > 0] = (255, 255, 255)
    return canvas
