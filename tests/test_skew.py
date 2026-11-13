"""自动纠偏专项测试。

覆盖需求中的关键边界：角度判断与应用的符号一致性、表格长线/粗下划线
不得主导结论、横排/竖排/局部混排的方向冲突、证据不足时置信度必须为 0、
连续纠偏不漂移、旋转不裁切可见内容、区域与方向指定、以及 API/历史可回看。
这些用例只用合成图像与 OpenCV，不依赖本地 Tesseract。
"""
from __future__ import annotations

import io
import math
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paperflow import imaging
from paperflow.skew import analyze_skew
from paperflow.web import create_app

HEADERS = {'X-PaperFlow': '1'}


def cv_rotate(image, angle):
    """与 imaging.rotate 相同的 OpenCV 约定（正角=视觉逆时针），扩展画布。"""
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    cosine, sine = abs(matrix[0, 0]), abs(matrix[0, 1])
    new_width = math.ceil(height * sine + width * cosine)
    new_height = math.ceil(height * cosine + width * sine)
    matrix[0, 2] += (new_width - width) / 2
    matrix[1, 2] += (new_height - height) / 2
    return cv2.warpAffine(image, matrix, (new_width, new_height), flags=cv2.INTER_CUBIC, borderValue=(255, 255, 255))


def horizontal_page(width=1200, height=900, lines=14, seed=1):
    rng = np.random.default_rng(seed)
    canvas = np.full((height, width, 3), 255, np.uint8)
    for index in range(lines):
        y = 90 + index * 46
        x = 80
        while x < width - 120:
            run = int(rng.integers(25, 90))
            thickness = int(rng.integers(2, 4))
            cv2.line(canvas, (x, y), (min(x + run, width - 80), y), (30, 30, 30), thickness)
            x += run + int(rng.integers(8, 22))
    return canvas


def vertical_page(width=1200, height=900, columns=14, seed=2):
    rng = np.random.default_rng(seed)
    canvas = np.full((height, width, 3), 255, np.uint8)
    for index in range(columns):
        x = 100 + index * 46
        y = 70
        while y < height - 80:
            run = int(rng.integers(20, 70))
            cv2.line(canvas, (x, y), (x, min(y + run, height - 70)), (30, 30, 30), 3)
            y += run + int(rng.integers(8, 18))
    return canvas


def hough_median_angle(image):
    gray = imaging.gray(image)
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 1800, 120, minLineLength=gray.shape[1] // 3, maxLineGap=30)
    assert lines is not None
    angles = [math.degrees(math.atan2(y2 - y1, x2 - x1)) for x1, y1, x2, y2 in lines[:, 0]]
    return float(np.median(angles))


def test_horizontal_text_angle_and_correction_share_one_convention():
    # OpenCV 正角=视觉逆时针：施加 +4 后，内容视觉倾斜为 -4，检测角与校正角都约 -4
    tilted = cv_rotate(horizontal_page(), 4)
    analysis = analyze_skew(tilted)
    assert analysis['status'] == 'ok'
    assert analysis['orientation'] == 'horizontal'
    assert abs(analysis['detected_angle'] + 4) < 0.4
    assert abs(analysis['correction_angle'] + 4) < 0.4
    corrected = imaging.rotate(tilted, analysis['correction_angle'])
    assert abs(hough_median_angle(corrected)) < 0.25


def test_vertical_text_is_detected_as_vertical():
    tilted = cv_rotate(vertical_page(), 4)
    analysis = analyze_skew(tilted)
    assert analysis['orientation'] == 'vertical'
    assert analysis['status'] == 'ok'
    # 竖排拟合的是相对竖直轴的角度，校正角与其反号
    assert abs(analysis['correction_angle'] + 4) < 0.5
    corrected = imaging.rotate(tilted, analysis['correction_angle'])
    # 竖直线转回竖直：检测竖直 Hough 线的偏离
    gray = imaging.gray(corrected)
    edges = cv2.Canny(gray, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 1800, 120, minLineLength=gray.shape[0] // 3, maxLineGap=30)
    angles = []
    for x1, y1, x2, y2 in lines[:, 0]:
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
        if abs(angle) > 45:
            angle = angle - 90 if angle > 45 else angle + 90
        angles.append(angle)
    assert abs(np.median(angles)) < 0.5


def test_long_ruled_line_does_not_drive_text_angle():
    page = horizontal_page()
    # 一条明显倾斜的粗表格长线，文字本身水平
    cv2.line(page, (60, 760), (1140, 600), (20, 20, 20), 4)
    analysis = analyze_skew(page)
    assert analysis['evidence']['ruled_line_count'] >= 1
    assert abs(analysis['correction_angle']) < 0.5
    assert all(abs(line['angle'] + 8.4) < 1.2 for line in analysis['ruled_lines'])


def test_underline_only_page_is_undetermined_and_kept():
    page = np.full((900, 1200, 3), 255, np.uint8)
    cv2.line(page, (100, 700), (1100, 650), (20, 20, 20), 6)
    analysis = analyze_skew(page)
    assert analysis['status'] == 'undetermined'
    assert analysis['confidence'] == 0.0
    assert analysis['correction_angle'] == 0.0
    assert analysis['evidence']['ruled_line_count'] >= 1
    result, metadata = imaging.process(page, 'deskew', {})
    assert metadata['applied'] is False
    assert np.array_equal(result, page)


def test_blank_page_is_undetermined():
    page = np.full((900, 1200, 3), 255, np.uint8)
    analysis = analyze_skew(page)
    assert analysis['status'] == 'undetermined'
    assert analysis['confidence'] == 0.0
    assert analysis['correction_angle'] == 0.0


def test_mixed_orientation_reports_conflict_and_both_groups():
    page = np.full((900, 1200, 3), 255, np.uint8)
    page[:, :860] = horizontal_page(860, 900, lines=12, seed=3)
    page[:, 900:] = vertical_page(300, 900, columns=6, seed=4)
    tilted = cv_rotate(page, 3)
    analysis = analyze_skew(tilted)
    assert analysis['conflicting'] is True
    assert analysis['status'] == 'conflict'
    orientations = {group['orientation'] for group in analysis['groups']}
    assert orientations == {'horizontal', 'vertical'}


def test_trusted_region_and_direction_resolve_conflict():
    page = np.full((900, 1200, 3), 255, np.uint8)
    page[:, :860] = horizontal_page(860, 900, lines=12, seed=3)
    page[:, 900:] = vertical_page(300, 900, columns=6, seed=4)
    tilted = cv_rotate(page, 3)
    analysis = analyze_skew(tilted, region=[0, 0, 860, 900], mode='horizontal')
    assert analysis['orientation'] == 'horizontal'
    assert analysis['status'] == 'ok'
    assert abs(analysis['correction_angle'] + 3) < 0.5
    assert analysis['region'] == [0, 0, 860, 900]


def test_repeated_deskew_converges_without_drift():
    image = cv_rotate(horizontal_page(), -3)
    original_shape = image.shape
    for _ in range(5):
        result, metadata = imaging.process(image, 'deskew', {})
        if not metadata['applied']:
            assert np.array_equal(result, image)
        image = result
    analysis = analyze_skew(image)
    assert analysis['correction_angle'] == 0.0
    # 只允许一次扩展，后续无操作不得继续改变尺寸
    assert image.shape[0] >= original_shape[0] and image.shape[1] >= original_shape[1]


def test_rotation_keeps_all_visible_corner_content():
    page = np.full((500, 600, 3), 255, np.uint8)
    marker_value = 0
    for x, y in ((2, 2), (597, 2), (2, 497), (597, 497)):
        cv2.rectangle(page, (x - 2, y - 2), (x + 2, y + 2), (marker_value, marker_value, marker_value), -1)
    rotated = imaging.rotate(page, 7)
    dark_pixels = int((np.min(rotated, axis=2) < 100).sum())
    # 四个 5x5 标记共 100 个暗像素，抗锯齿允许少量损失但不得整块消失
    assert dark_pixels >= 90


def test_zero_angle_rotation_is_lossless():
    page = horizontal_page()
    assert np.array_equal(imaging.rotate(page, 0), page)
    assert np.array_equal(imaging.rotate(page, 360), page)


def test_legacy_skew_angle_contract_remains():
    page = cv_rotate(horizontal_page(), 3)
    result = imaging.skew_angle(page)
    assert set(result) >= {'angle', 'confidence', 'lines', 'status', 'orientation', 'detected_angle'}
    assert abs(result['angle'] + 3) < 0.4
    blank = np.full((400, 400, 3), 255, np.uint8)
    blank_result = imaging.skew_angle(blank)
    assert blank_result['confidence'] == 0.0 and blank_result['angle'] == 0.0


@pytest.fixture(scope='module')
def client(tmp_path_factory):
    data_root = tmp_path_factory.mktemp('skew-data')
    app = create_app(data_root=data_root, demo=False)
    tester = app.test_client()

    def call(method, path, values=None):
        response = tester.open('/api' + path, method=method, json=values, headers=HEADERS)
        assert response.status_code < 400, (path, response.status_code, response.json)
        return response.json

    collection = call('POST', '/collections', {'name': '纠偏测试', 'category': '测试'})
    document = call('POST', '/documents', {'collection_id': collection['id'], 'name': '纠偏文档'})
    yield tester, call, document, app
    app.extensions['scheduler'].shutdown()
    app.extensions['pipelines'].executor.shutdown(wait=True)


def _import_page(client, document, image, name='skew.png'):
    ok, encoded = cv2.imencode('.png', image)
    assert ok
    response = client.post(
        '/api/documents/' + document['id'] + '/import',
        data={'files': (io.BytesIO(encoded.tobytes()), name)},
        headers=HEADERS,
    )
    assert response.status_code == 201, response.json
    return response.json['page_ids'][0]


def test_skew_endpoint_returns_full_evidence(client):
    tester, call, document, _ = client
    page_id = _import_page(tester, document, cv_rotate(horizontal_page(), 3))
    analysis = call('GET', '/pages/' + page_id + '/skew')
    assert analysis['orientation'] == 'horizontal'
    assert abs(analysis['correction_angle'] + 3) < 0.4
    assert {'tiles', 'ruled_lines', 'groups', 'notes', 'evidence', 'status'} <= set(analysis)
    # 区域参数透传
    regional = call('GET', '/pages/' + page_id + '/skew?mode=horizontal&region=0,0,900,900')
    assert regional['mode'] == 'horizontal'
    bad = tester.open('/api/pages/' + page_id + '/skew?region=1,2,3', method='GET', headers=HEADERS)
    assert bad.status_code == 400


def test_deskew_process_records_history_and_preserves_on_undetermined(client):
    tester, call, document, _ = client
    tilted = cv_rotate(horizontal_page(), 3)
    page_id = _import_page(tester, document, tilted)
    before = call('GET', '/pages/' + page_id)
    processed = call('POST', '/pages/' + page_id + '/process', {'operation': 'deskew', 'params': {}})
    entry = processed['history'][-1]
    assert entry['operation'] == 'deskew'
    assert entry['metadata']['applied'] is True
    assert entry['before'] != entry['after']
    assert processed['revision'] == before['revision'] + 1

    blank_id = _import_page(tester, document, np.full((900, 1200, 3), 255, np.uint8), 'blank.png')
    blank_before = call('GET', '/pages/' + blank_id)
    blank_after = call('POST', '/pages/' + blank_id + '/process', {'operation': 'deskew', 'params': {}})
    blank_entry = blank_after['history'][-1]
    assert blank_entry['metadata']['applied'] is False
    assert blank_entry['metadata']['status'] == 'undetermined'
    assert blank_entry['before'] == blank_entry['after'] == blank_before['current']
    assert blank_after['current'] == blank_before['current']
    # 保留原状也留下可回看的分析依据
    assert blank_entry['metadata']['evidence']['component_count'] == 0


def test_deskew_accepts_region_and_mode_params(client):
    tester, call, document, _ = client
    page = np.full((900, 1200, 3), 255, np.uint8)
    page[:, :860] = horizontal_page(860, 900, lines=12, seed=3)
    page[:, 900:] = vertical_page(300, 900, columns=6, seed=4)
    page_id = _import_page(tester, document, cv_rotate(page, 3))
    processed = call('POST', '/pages/' + page_id + '/process', {
        'operation': 'deskew',
        'params': {'region': [0, 0, 860, 900], 'mode': 'horizontal'},
    })
    metadata = processed['history'][-1]['metadata']
    assert metadata['orientation'] == 'horizontal'
    assert metadata['applied'] is True
    # 用户显式保留原状：即使有可用角度也不旋转，图像文件不变
    kept = call('POST', '/pages/' + page_id + '/process', {
        'operation': 'deskew',
        'params': {'apply': False},
    })
    kept_entry = kept['history'][-1]
    assert kept_entry['metadata']['applied'] is False
    assert kept_entry['metadata']['user_kept_original'] is True
    assert kept_entry['before'] == kept_entry['after']
    # 未知参数仍被注册表拒绝
    response = tester.open(
        '/api/pages/' + page_id + '/process', method='POST',
        json={'operation': 'deskew', 'params': {'unexpected': 1}}, headers=HEADERS,
    )
    assert response.status_code == 400
    # 布尔类型校验
    bad_type = tester.open(
        '/api/pages/' + page_id + '/process', method='POST',
        json={'operation': 'deskew', 'params': {'apply': 'yes'}}, headers=HEADERS,
    )
    assert bad_type.status_code == 400


def test_batch_deskew_reports_per_page_basis(client):
    tester, call, document, _ = client
    good = _import_page(tester, document, cv_rotate(horizontal_page(), 3))
    blank = _import_page(tester, document, np.full((900, 1200, 3), 255, np.uint8), 'batch-blank.png')
    task = call('POST', '/tasks', {'page_ids': [good, blank], 'operation': 'deskew', 'params': {}})
    import time
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        record = call('GET', '/tasks/' + task['id'])
        if record['status'] in ('completed', 'partial', 'failed', 'cancelled'):
            break
        time.sleep(0.05)
    assert record['status'] == 'completed'
    by_page = {item['page_id']: item for item in record['results']}
    assert by_page[good]['deskew']['applied'] is True
    assert by_page[blank]['deskew']['applied'] is False
    assert by_page[blank]['deskew']['status'] == 'undetermined'
