from __future__ import annotations

import cv2
import numpy as np

from .core import number
from .imaging import gray, read_image

# 建议阈值依据实测分布标定：重扫同页约 0.72+，不同页通常低于 0.64。
SCORE_HIGH = 0.70
SCORE_MIDDLE = 0.60
SCORE_LOW = 0.45
# 空白页判定：亮像素占比高于该值且暗部像素极少。
BLANK_LIGHT_RATIO = 0.992
BLANK_DARK_RATIO = 0.004
HASH_SIZE = 16
ORIENT_SIZE = 32
FEATURE_SIZE = 480
GRID = 6
LOWE_RATIO = 0.72
RANSAC_THRESHOLD = 5.0
_MATCHER = cv2.BFMatcher(cv2.NORM_L2)


def _sift():
    return cv2.SIFT_create(nfeatures=800, contrastThreshold=0.03)


def page_signature(image):
    """生成页面的感知特征：均值/差异哈希、方向网格、SIFT 关键点与空白判定。

    返回的 descriptors 为 float32 数组，不直接写入工作区 JSON；由调用方
    落盘为二进制缓存（见 build_cache / load_cache），避免大对象反复序列化。
    """
    luminance = gray(image)
    resized = cv2.resize(luminance, (HASH_SIZE, HASH_SIZE), interpolation=cv2.INTER_AREA)
    average_hash = (resized > resized.mean()).astype(np.uint8)
    difference_hash = (resized[:, 1:] > resized[:, :-1]).astype(np.uint8)
    orientation = cv2.resize(luminance, (ORIENT_SIZE, ORIENT_SIZE), interpolation=cv2.INTER_AREA)
    feature_frame = cv2.resize(luminance, (FEATURE_SIZE, FEATURE_SIZE), interpolation=cv2.INTER_AREA)
    keypoints, descriptors = _sift().detectAndCompute(feature_frame, None)
    if descriptors is None:
        descriptors = np.empty((0, 128), np.float32)
    dark_ratio = float((luminance < 90).mean())
    light_ratio = float((luminance > 228).mean())
    blank_score = round(min(1.0, max(0.0, (light_ratio - 0.90) / 0.095)), 3)
    is_blank = bool(light_ratio >= BLANK_LIGHT_RATIO and dark_ratio <= BLANK_DARK_RATIO)
    return {
        'average_hash': average_hash.flatten().tolist(),
        'difference_hash': difference_hash.flatten().tolist(),
        'orientation': orientation.astype(np.uint8).tolist(),
        'keypoints': [
            [round(point.pt[0], 2), round(point.pt[1], 2), round(point.size, 2), round(point.angle, 2)]
            for point in keypoints
        ],
        'descriptors': descriptors,
        'blank': is_blank,
        'blank_score': blank_score,
        'dark_ratio': round(dark_ratio, 5),
        'light_ratio': round(light_ratio, 5),
        'mean': round(float(luminance.mean()), 2),
    }


def light_view(signature):
    """剔除 numpy 描述子，只保留可直接 JSON 持久化的轻量字段。"""
    result = {key: value for key, value in signature.items() if key != 'descriptors'}
    result['descriptor_count'] = 0 if signature.get('descriptors') is None else len(signature['descriptors'])
    return result


def build_cache(store, page_id, signature):
    """把 SIFT 描述子以二进制形式存到 paircache 目录，内容寻址、跨会话共享。"""
    import hashlib
    descriptors = signature.get('descriptors')
    if descriptors is None:
        descriptors = np.empty((0, 128), np.float32)
    descriptors = np.asarray(descriptors, dtype=np.float32)
    checksum = hashlib.sha256(descriptors.tobytes()).hexdigest()
    relative = 'paircache/' + checksum + '.npy'
    path = store.resolve(relative)
    if not path.is_file():
        with open(path, 'wb') as handle:
            np.save(handle, descriptors)
    return relative


def load_cache(store, relative):
    path = store.resolve(relative)
    if not path.is_file():
        return np.empty((0, 128), np.float32)
    return np.load(path)


def _descriptors(signature):
    descriptors = np.asarray(signature.get('descriptors', []), dtype=np.float32)
    return descriptors if descriptors.size else None


def spatial_verification(left, right):
    """同页判定的核心：RANSAC 单应内点 + 内点空间落格分散度。

    重扫/补拍保持整体版面，内点会铺满多个网格；不同页面只在局部文字笔画上
    偶然匹配，内点集中在少数格子，分散度明显偏低。
    """
    left_desc = _descriptors(left)
    right_desc = _descriptors(right)
    if left_desc is None or right_desc is None or len(left_desc) < 10 or len(right_desc) < 10:
        return {'inlier_ratio': 0.0, 'good_matches': 0, 'spread': 0.0}
    left_points = {index: (item[0], item[1]) for index, item in enumerate(left['keypoints'])}
    right_points = {index: (item[0], item[1]) for index, item in enumerate(right['keypoints'])}
    raw = _MATCHER.knnMatch(left_desc, right_desc, k=2)
    source, destination = [], []
    for pair in raw:
        if len(pair) < 2:
            continue
        first, second = pair
        if first.distance < LOWE_RATIO * second.distance and first.queryIdx in left_points and first.trainIdx in right_points:
            source.append(left_points[first.queryIdx])
            destination.append(right_points[first.trainIdx])
    good = len(source)
    if good < 12:
        return {'inlier_ratio': 0.0, 'good_matches': good, 'spread': 0.0}
    _, mask = cv2.findHomography(np.float32(source), np.float32(destination), cv2.RANSAC, RANSAC_THRESHOLD)
    if mask is None:
        return {'inlier_ratio': 0.0, 'good_matches': good, 'spread': 0.0}
    inlier_flags = mask.ravel().astype(bool)
    inlier_ratio = float(inlier_flags.sum()) / good
    cell_size = FEATURE_SIZE / GRID
    cells = set()
    for (x, y), is_inlier in zip(source, inlier_flags):
        if is_inlier:
            cells.add((min(GRID - 1, int(x / cell_size)), min(GRID - 1, int(y / cell_size))))
    spread = len(cells) / (GRID * GRID)
    return {'inlier_ratio': round(inlier_ratio, 4), 'good_matches': good, 'spread': round(spread, 4)}


def hamming(left, right):
    left_bits = np.asarray(left, dtype=np.uint8)
    right_bits = np.asarray(right, dtype=np.uint8)
    if left_bits.shape != right_bits.shape or left_bits.size == 0:
        return 1.0
    return float(np.count_nonzero(left_bits != right_bits)) / left_bits.size


def rotated(matrix, times):
    return np.rot90(np.asarray(matrix, dtype=np.uint8), times).tolist()


def orientation_vote(left_grid, right_grid):
    """右侧图像相对左侧的进纸朝向：0 正向，1/3 侧向，2 倒置（180°）。"""
    left = np.asarray(left_grid, dtype=np.uint8)
    right = np.asarray(right_grid, dtype=np.uint8)
    best_turn = 0
    best_difference = None
    for turn in range(4):
        difference = float(np.mean(cv2.absdiff(left, np.rot90(right, turn))))
        if best_difference is None or difference < best_difference:
            best_difference = difference
            best_turn = turn
    confidence = round(max(0.0, 1 - best_difference / 64.0), 3)
    return {'turn': best_turn, 'confidence': confidence, 'difference': round(best_difference, 2)}


def hash_similarity(left, right):
    average_distance = hamming(left['average_hash'], right['average_hash'])
    difference_distance = hamming(left['difference_hash'], right['difference_hash'])
    grid_difference = float(np.mean(cv2.absdiff(
        np.asarray(left['orientation'], dtype=np.uint8),
        np.asarray(right['orientation'], dtype=np.uint8),
    ))) / 255.0
    return max(0.0, 1 - (0.4 * average_distance + 0.35 * difference_distance + 0.25 * grid_difference))


def feature_score(left, right):
    verification = spatial_verification(left, right)
    # 分散度权重最高：它对“同一张纸的重扫”与“不同纸张”区分最稳定。
    return round(min(1.0, 0.55 * verification['spread'] + 0.45 * verification['inlier_ratio']), 4), verification


def similarity(left, right):
    """综合空间特征分与感知哈希，输出 0–1 的同页相似度。"""
    feature, _ = feature_score(left, right)
    score = 0.65 * feature + 0.35 * hash_similarity(left, right)
    return round(max(0.0, min(1.0, score)), 4)


def best_rotation(left, right):
    """把右侧按四种角度旋转后与左侧比较，返回最相似的旋转次数与分数。

    空间特征本身对小幅旋转稳健，因此特征分只算一次；哈希分按四种朝向计算，
    既可以识别倒置进纸（turn=2），也避免重复昂贵的特征匹配。
    """
    feature, verification = feature_score(left, right)
    right_grid = right['orientation']
    right_average = np.asarray(right['average_hash'], dtype=np.uint8).reshape(HASH_SIZE, HASH_SIZE)
    right_difference = np.asarray(right['difference_hash'], dtype=np.uint8).reshape(HASH_SIZE, HASH_SIZE - 1)
    candidates = []
    for turn in range(4):
        rotated_right = {
            **right,
            'orientation': rotated(right_grid, turn),
            'average_hash': np.rot90(right_average, turn).flatten().tolist(),
            'difference_hash': np.rot90(right_difference, turn).flatten().tolist(),
        }
        hash_score = max(0.0, hash_similarity(left, rotated_right))
        score = round(min(1.0, 0.65 * feature + 0.35 * hash_score), 4)
        candidates.append({'turn': turn, 'score': score, 'hash_score': round(hash_score, 4), 'feature_score': feature})
    best = max(candidates, key=lambda item: item['score'])
    return best['turn'], best['score'], candidates


def score_band(score):
    if score >= SCORE_HIGH:
        return 'high'
    if score >= SCORE_MIDDLE:
        return 'middle'
    if score >= SCORE_LOW:
        return 'low'
    return 'none'


def duplicate_groups(signatures):
    """按高相似度把页面聚成重复组，返回组列表（每组至少两页）。"""
    page_ids = list(signatures)
    unions = {page_id: page_id for page_id in page_ids}

    def find(identifier):
        while unions[identifier] != identifier:
            unions[identifier] = unions[unions[identifier]]
            identifier = unions[identifier]
        return identifier

    for index, left_id in enumerate(page_ids):
        for right_id in page_ids[index + 1:]:
            _, score, _ = best_rotation(signatures[left_id], signatures[right_id])
            if score >= SCORE_HIGH:
                root_left, root_right = find(left_id), find(right_id)
                if root_left != root_right:
                    unions[root_right] = root_left
    grouped = {}
    for page_id in page_ids:
        grouped.setdefault(find(page_id), []).append(page_id)
    return [sorted(members) for members in grouped.values() if len(members) > 1]


def signature_from_path(store, path):
    return page_signature(read_image(store.resolve(path)))


def validate_turn(value):
    return number(value if value is not None else 0, '旋转次数', 0, 3, True)
