from __future__ import annotations

import cv2
import numpy as np

from .analysis import hash_distance, perceptual_hash
from .core import number, require
from .imaging import gray

# 工作分辨率：保留笔画细节的同时控制 ECC 配准耗时
WORK_WIDTH = 460
WORK_HEIGHT = 600
# ECC 参数
ECC_ITERATIONS = 40
ECC_PYRAMID = 3
# 距离变换容差（工作分辨率像素）：ECC 配准后残差通常 ≤1px，1.5 足以吸收，又能保留单字笔画改动
DISTANCE_TOLERANCE = 1.5
# 差异连通域最小面积，过滤孤立噪点
MIN_CHANGE_AREA = 4
# 判定阈值
SAME_PAGE_ECC = 0.55
CHANGED_INK_RATIO = 0.0003
CHANGED_REGIONS = 1


def normalize(image):
    """缩放到工作尺寸，CLAHE 压平光照，轻度模糊抑制拍摄噪点。"""
    luminance = gray(image)
    resized = cv2.resize(luminance, (WORK_WIDTH, WORK_HEIGHT), interpolation=cv2.INTER_AREA)
    equalized = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(resized)
    return cv2.GaussianBlur(equalized, (5, 5), 1.1)


def ink_mask(luminance):
    """Otsu 全局二值化，返回文字笔画掩膜（笔画为 255）。"""
    _, binary = cv2.threshold(luminance, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    return binary


def align_pair(first_image, second_image):
    """用 ECC 欧氏变换把第二页对齐到第一页，返回相关系数与对齐后的工作图。"""
    first = normalize(first_image)
    second = normalize(second_image)
    first_f = first.astype(np.float32) / 255
    second_f = second.astype(np.float32) / 255
    warp = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, ECC_ITERATIONS, 1e-6)
    try:
        correlation, warp = cv2.findTransformECC(
            first_f, second_f, warp, cv2.MOTION_EUCLIDEAN, criteria, None, ECC_PYRAMID
        )
    except cv2.error:
        correlation = -1.0
        warp = np.eye(2, 3, dtype=np.float32)
    aligned = cv2.warpAffine(
        second, warp, (WORK_WIDTH, WORK_HEIGHT),
        flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP, borderValue=255,
    )
    return {
        'correlation': round(float(max(correlation, -1.0)), 5),
        'warp': np.round(warp, 5).tolist(),
        'shift_x': round(float(warp[0, 2]), 3),
        'shift_y': round(float(warp[1, 2]), 3),
        'rotation_degrees': round(float(np.degrees(np.arctan2(warp[1, 0], warp[0, 0]))), 3),
        'first_gray': first,
        'second_gray': aligned,
    }


def change_regions(first_gray, second_gray, far=DISTANCE_TOLERANCE, min_area=MIN_CHANGE_AREA):
    """基于距离变换提取真实笔画差异，返回差异区域与失配率。"""
    far = number(far, '距离容差', 1, 12)
    min_area = number(min_area, '最小差异面积', 2, 5000, True)
    first_mask = ink_mask(first_gray)
    second_mask = ink_mask(second_gray)
    # 到对方笔画的距离：对齐残差通常在 1px 内，真实新增/删除内容距离很大
    first_distance = cv2.distanceTransform(255 - first_mask, cv2.DIST_L2, 5)
    second_distance = cv2.distanceTransform(255 - second_mask, cv2.DIST_L2, 5)
    missing_in_second = (first_mask > 0) & (second_distance > far)
    added_in_second = (second_mask > 0) & (first_distance > far)
    changed = cv2.bitwise_or(
        missing_in_second.astype(np.uint8), added_in_second.astype(np.uint8)
    ) * 255
    count, _, stats, centroids = cv2.connectedComponentsWithStats(changed, 8)
    regions = []
    for index in range(1, count):
        area = int(stats[index, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        x = int(stats[index, cv2.CC_STAT_LEFT])
        y = int(stats[index, cv2.CC_STAT_TOP])
        width = int(stats[index, cv2.CC_STAT_WIDTH])
        height = int(stats[index, cv2.CC_STAT_HEIGHT])
        regions.append({
            'rect': [x, y, width, height],
            'area': area,
            'centroid': [round(float(centroids[index][0]), 1), round(float(centroids[index][1]), 1)],
            'kind': 'removed' if missing_in_second[y:y + height, x:x + width].sum()
                    >= added_in_second[y:y + height, x:x + width].sum() else 'added',
        })
    regions.sort(key=lambda item: -item['area'])
    ink_total = max(1, int((first_mask > 0).sum() + (second_mask > 0).sum()) / 2)
    ink_ratio = round(float((changed > 0).sum()) / ink_total, 6)
    return {
        'ink_change_ratio': ink_ratio,
        'region_count': len(regions),
        'regions': regions[:200],
        'changed_mask': changed,
        'first_mask': first_mask,
        'second_mask': second_mask,
    }


def classify(correlation, ink_ratio, region_count, text_ratio=None):
    """综合配准、笔画与文字信号给出页面关系判定。"""
    if correlation < SAME_PAGE_ECC:
        return 'different'
    if ink_ratio >= CHANGED_INK_RATIO or region_count >= CHANGED_REGIONS:
        return 'content_changed'
    if text_ratio is not None and text_ratio >= 0.08:
        return 'text_changed'
    return 'equivalent'


def visual_compare(first_image, second_image):
    """完整视觉比对：配准 + 笔画差异 + 指纹，不含掩膜位图，可直接持久化。"""
    aligned = align_pair(first_image, second_image)
    diff = change_regions(aligned['first_gray'], aligned['second_gray'])
    phash_distance = hash_distance(
        perceptual_hash(first_image), perceptual_hash(second_image)
    )
    classification = classify(
        aligned['correlation'], diff['ink_change_ratio'], diff['region_count']
    )
    return {
        'classification': classification,
        'correlation': aligned['correlation'],
        'warp': aligned['warp'],
        'shift_x': aligned['shift_x'],
        'shift_y': aligned['shift_y'],
        'rotation_degrees': aligned['rotation_degrees'],
        'ink_change_ratio': diff['ink_change_ratio'],
        'region_count': diff['region_count'],
        'regions': diff['regions'],
        'phash_distance': phash_distance,
        'work_size': [WORK_WIDTH, WORK_HEIGHT],
    }


# 快速相似度工作尺寸：相位相关 + 平移补偿 NCC，对亮度/位移稳健、每对约 5ms
QUICK_WIDTH = 300
QUICK_HEIGHT = 400
# 序列对齐参数：合同两次扫描页序基本稳定，仅允许局部带宽内的错位
ALIGN_BAND = 8
# 相似度高于该值视为可配对候选，低于该值宁可留空交人工，避免不同附件误配
PAIR_SIMILARITY_FLOOR = 0.55


def quick_normalize(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    resized = cv2.resize(gray, (QUICK_WIDTH, QUICK_HEIGHT), interpolation=cv2.INTER_AREA)
    return cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(resized).astype(np.float32)


def quick_similarity(first_image, second_image):
    """位移/亮度稳健的廉价相似度（0–1）：相位相关峰值 + 平移补偿后的归一化相关。"""
    first = quick_normalize(first_image)
    second = quick_normalize(second_image)
    # 近空白页相位相关不可靠，退化为直接归一化相关
    if first.std() < 1.0 or second.std() < 1.0:
        x = first - first.mean()
        y = second - second.mean()
        ncc = float((x * y).sum() / (np.linalg.norm(x) * np.linalg.norm(y) + 1e-9))
        return round(max(0.0, ncc), 5)
    (shift_x, shift_y), response = cv2.phaseCorrelate(first, second)
    matrix = np.float32([[1, 0, -shift_x], [0, 1, -shift_y]])
    aligned = cv2.warpAffine(
        second, matrix, (second.shape[1], second.shape[0]),
        flags=cv2.INTER_LINEAR, borderValue=float(second.mean()),
    )
    x = first - first.mean()
    y = aligned - aligned.mean()
    ncc = float((x * y).sum() / (np.linalg.norm(x) * np.linalg.norm(y) + 1e-9))
    return round(0.5 * max(0.0, response) + 0.5 * max(0.0, ncc), 5)


def sequence_align(left_images, right_images, floor=PAIR_SIMILARITY_FLOOR, band=ALIGN_BAND):
    """带顺序约束的序列对齐，返回 (left_index, right_index, similarity) 配对与两侧未配页索引。

    利用两次扫描页序基本稳定的领域事实，用动态规划在带宽内寻找全局最优，
    天然处理页位移、插入附件与缺页；相似度低于 floor 的位置留空交人工决定。
    """
    floor = number(floor, '相似度下限', 0.0, 1.0)
    band = number(band, '对齐带宽', 1, 60, True)
    m, n = len(left_images), len(right_images)
    scores = np.zeros((m, n), dtype=np.float32)
    for i in range(m):
        for j in range(n):
            if abs(i - j) <= band:
                scores[i, j] = quick_similarity(left_images[i], right_images[j])
    # 配对收益为相似度，gap 收益为 0；找最大得分路径
    dp = np.zeros((m + 1, n + 1), dtype=np.float32)
    for i in range(1, m + 1):
        j_lo = max(1, i - band)
        j_hi = min(n, i + band)
        for j in range(j_lo, j_hi + 1):
            dp[i, j] = max(
                dp[i - 1, j - 1] + scores[i - 1, j - 1],
                dp[i - 1, j],
                dp[i, j - 1],
            )
        # 带宽外传播 gap，保证边界可达
        for j in range(1, j_lo):
            dp[i, j] = max(dp[i - 1, j], dp[i, j - 1])
        for j in range(j_hi + 1, n + 1):
            dp[i, j] = max(dp[i - 1, j], dp[i, j - 1])
    pairs = []
    i, j = m, n
    while i > 0 and j > 0:
        if abs(i - j) <= band and abs(dp[i, j] - (dp[i - 1, j - 1] + scores[i - 1, j - 1])) < 1e-6:
            similarity = float(scores[i - 1, j - 1])
            if similarity >= floor:
                pairs.append((i - 1, j - 1, round(similarity, 5)))
            i -= 1
            j -= 1
        elif dp[i - 1, j] >= dp[i, j - 1]:
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    matched_left = {item[0] for item in pairs}
    matched_right = {item[1] for item in pairs}
    return {
        'pairs': pairs,
        'left_unmatched': [index for index in range(m) if index not in matched_left],
        'right_unmatched': [index for index in range(n) if index not in matched_right],
    }


def diff_overlay(first_image, second_image, max_width=1100):
    """生成左右并排的差异可视化：绿色=对齐后仅第二页有的内容，红色=仅第一页有。"""
    aligned = align_pair(first_image, second_image)
    diff = change_regions(aligned['first_gray'], aligned['second_gray'])
    first_rgb = cv2.cvtColor(aligned['first_gray'], cv2.COLOR_GRAY2BGR)
    second_rgb = cv2.cvtColor(aligned['second_gray'], cv2.COLOR_GRAY2BGR)
    first_mask = diff['first_mask']
    second_mask = diff['second_mask']
    far = DISTANCE_TOLERANCE
    first_distance = cv2.distanceTransform(255 - first_mask, cv2.DIST_L2, 5)
    second_distance = cv2.distanceTransform(255 - second_mask, cv2.DIST_L2, 5)
    removed = ((first_mask > 0) & (second_distance > far)).astype(np.uint8) * 255
    added = ((second_mask > 0) & (first_distance > far)).astype(np.uint8) * 255
    # 在第一页图上标红（本页有、对方无），第二页图上标绿（对方新增）
    marked_first = first_rgb.copy()
    marked_first[removed > 0] = (60, 60, 220)
    marked_second = second_rgb.copy()
    marked_second[added > 0] = (90, 170, 90)
    for region in diff['regions']:
        x, y, width, height = region['rect']
        cv2.rectangle(marked_first, (x, y), (x + width, y + height), (60, 60, 220), 2)
        cv2.rectangle(marked_second, (x, y), (x + width, y + height), (90, 170, 90), 2)
    gap = np.full((WORK_HEIGHT, 16, 3), 210, np.uint8)
    composite = np.concatenate((marked_first, gap, marked_second), axis=1)
    scale = min(1.0, max_width / composite.shape[1])
    if scale < 1:
        composite = cv2.resize(composite, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    require(composite.size > 0, '差异图生成失败', 500)
    return composite, {
        'correlation': aligned['correlation'],
        'ink_change_ratio': diff['ink_change_ratio'],
        'region_count': diff['region_count'],
        'classification': classify(
            aligned['correlation'], diff['ink_change_ratio'], diff['region_count']
        ),
    }
