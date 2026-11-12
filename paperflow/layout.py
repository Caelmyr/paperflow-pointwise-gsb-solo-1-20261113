from __future__ import annotations

import re
from collections import defaultdict

import cv2
import numpy as np

from .core import number, require
from .imaging import gray


def union_rect(rectangles):
    require(bool(rectangles), '没有可合并的区域')
    left = min(rect[0] for rect in rectangles)
    top = min(rect[1] for rect in rectangles)
    right = max(rect[0] + rect[2] for rect in rectangles)
    bottom = max(rect[1] + rect[3] for rect in rectangles)
    return [left, top, right - left, bottom - top]


def intersection(first, second):
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    if right <= left or bottom <= top:
        return 0
    return (right - left) * (bottom - top)


def iou(first, second):
    overlap = intersection(first, second)
    total = first[2] * first[3] + second[2] * second[3] - overlap
    return overlap / total if total else 0


def ocr_lines(result):
    if not result:
        return []
    groups = defaultdict(list)
    for word in result['boxes']:
        groups[word['line']].append(word)
    lines = []
    for line_id, words in groups.items():
        words.sort(key=lambda word: word['x'])
        rectangles = [[word['x'], word['y'], word['width'], word['height']] for word in words]
        line = {
            'id': line_id,
            'text': ' '.join(word['text'] for word in words),
            'rect': union_rect(rectangles),
            'word_ids': [word['id'] for word in words],
            'confidence': round(sum(word['confidence'] for word in words) / len(words), 2),
        }
        lines.append(line)
    return sorted(lines, key=lambda line: (line['rect'][1], line['rect'][0]))


def paragraphs(result, gap_factor=1.6):
    gap_factor = number(gap_factor, '段落间距系数', 0.5, 5)
    lines = ocr_lines(result)
    groups = []
    for line in lines:
        rect = line['rect']
        candidate = None
        distance = float('inf')
        for group in groups:
            last = group['lines'][-1]
            previous = last['rect']
            gap = rect[1] - previous[1] - previous[3]
            indent = abs(rect[0] - previous[0])
            max_height = max(rect[3], previous[3])
            horizontal_overlap = min(rect[0] + rect[2], previous[0] + previous[2]) - max(rect[0], previous[0])
            if -max_height * 0.3 <= gap <= max_height * gap_factor and horizontal_overlap > 0:
                cost = abs(gap) + indent * 0.1
                if cost < distance:
                    candidate = group
                    distance = cost
        if candidate is None:
            groups.append({'lines': [line]})
        else:
            candidate['lines'].append(line)
    results = []
    for index, group in enumerate(groups):
        group_lines = group['lines']
        results.append({
            'index': index,
            'rect': union_rect([line['rect'] for line in group_lines]),
            'text': '\n'.join(line['text'] for line in group_lines),
            'line_ids': [line['id'] for line in group_lines],
            'confidence': round(sum(line['confidence'] for line in group_lines) / len(group_lines), 2),
        })
    return results


def detect_columns(result, width):
    lines = ocr_lines(result)
    if not lines:
        return []
    projection = np.zeros(width, np.float32)
    for line in lines:
        x, y, w, h = line['rect']
        start = max(0, min(width, x))
        end = max(start, min(width, x + w))
        projection[start:end] += h
    smooth_width = max(3, width // 100)
    smooth = np.convolve(projection, np.ones(smooth_width) / smooth_width, mode='same')
    active = smooth > max(1, float(smooth.max()) * 0.05)
    runs = []
    start = None
    for index, value in enumerate(active):
        if value and start is None:
            start = index
        if start is not None and (not value or index == width - 1):
            end = index if not value else index + 1
            if end - start > width * 0.08:
                runs.append([start, end])
            start = None
    merged = []
    for run in runs:
        if merged and run[0] - merged[-1][1] < width * 0.035:
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    return [{'left': start, 'right': end, 'width': end - start} for start, end in merged]


def detect_tables(image):
    luminance = gray(image)
    scale = min(1, 1800 / max(luminance.shape))
    small = cv2.resize(luminance, None, fx=scale, fy=scale)
    mask = cv2.adaptiveThreshold(small, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 12)
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(30, small.shape[1] // 25), 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(30, small.shape[0] // 25)))
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN, horizontal_kernel)
    vertical = cv2.morphologyEx(mask, cv2.MORPH_OPEN, vertical_kernel)
    grid = cv2.bitwise_or(horizontal, vertical)
    joints = cv2.bitwise_and(horizontal, vertical)
    contours, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    tables = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < small.shape[1] * 0.15 or h < 40:
            continue
        joint_count = cv2.connectedComponents(joints[y:y + h, x:x + w])[0] - 1
        if joint_count < 4:
            continue
        rect = [round(x / scale), round(y / scale), round(w / scale), round(h / scale)]
        tables.append({'rect': rect, 'intersections': joint_count})
    return sorted(tables, key=lambda table: table['rect'][1])


def detect_illustrations(image, result=None):
    scale = min(1, 1600 / max(image.shape[:2]))
    small = cv2.resize(image, None, fx=scale, fy=scale)
    luminance = gray(small)
    edges = cv2.Canny(luminance, 40, 120)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    connected = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(connected, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    text_rects = [[box['x'], box['y'], box['width'], box['height']] for box in (result or {}).get('boxes', [])]
    candidates = []
    total_area = image.shape[0] * image.shape[1]
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        rect = [round(x / scale), round(y / scale), round(w / scale), round(h / scale)]
        area = rect[2] * rect[3]
        if area < total_area * 0.015 or area > total_area * 0.8:
            continue
        overlap = sum(intersection(rect, text) for text in text_rects)
        ratio = min(1, overlap / max(1, area))
        if ratio > 0.3:
            continue
        density = float(np.mean(edges[y:y + h, x:x + w] > 0))
        if density < 0.02:
            continue
        candidates.append({'rect': rect, 'text_overlap': round(ratio, 3), 'edge_density': round(density, 3)})
    return sorted(candidates, key=lambda item: (item['rect'][1], item['rect'][0]))


def reading_order(result):
    if not result:
        return []
    lines = ocr_lines(result)
    columns = detect_columns(result, result['width'])
    if len(columns) <= 1:
        return [word_id for line in lines for word_id in line['word_ids']]
    assigned = defaultdict(list)
    for line in lines:
        center = line['rect'][0] + line['rect'][2] / 2
        closest = min(range(len(columns)), key=lambda i: abs(center - (columns[i]['left'] + columns[i]['right']) / 2))
        assigned[closest].append(line)
    order = []
    for index in sorted(assigned):
        column_lines = sorted(assigned[index], key=lambda line: line['rect'][1])
        for line in column_lines:
            order.extend(line['word_ids'])
    return order


def extract_fields(result):
    text = (result or {}).get('text', '')
    patterns = {
        'dates': r'(?:19|20)\d{2}[年/.-]\s*\d{1,2}[月/.-]\s*\d{1,2}日?',
        'emails': r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',
        'urls': r'https?://[^\s<>]+',
        'amounts': r'[¥￥$]\s*\d[\d,]*(?:\.\d{1,2})?',
        'identifiers': r'[A-Z]{2,8}[-_]\d{2,}(?:[-_]\d+)*',
    }
    fields = {}
    for key, pattern in patterns.items():
        found = []
        seen = set()
        for match in re.finditer(pattern, text):
            if match.group() in seen:
                continue
            seen.add(match.group())
            found.append({'value': match.group(), 'start': match.start(), 'end': match.end()})
        fields[key] = found
    return fields


def analyze_layout(image, result):
    return {
        'lines': ocr_lines(result),
        'paragraphs': paragraphs(result),
        'columns': detect_columns(result, image.shape[1]),
        'tables': detect_tables(image),
        'illustrations': detect_illustrations(image, result),
        'reading_order': reading_order(result),
        'fields': extract_fields(result),
    }
