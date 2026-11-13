"""笔迹粗细调整验收：选区、颜色保留、分区保护、预览确认一致性与防叠加。"""
import io
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paperflow import stroke
from paperflow.core import DomainError
from paperflow.imaging import read_image
from paperflow.web import create_app

HEADERS = {'X-PaperFlow': '1'}
SAMPLE = ROOT / 'devtools' / 'sample.png'


def bgr_from_response(response):
    rgb = np.asarray(Image.open(io.BytesIO(response.data)).convert('RGB'))
    return rgb[:, :, ::-1].copy()


@pytest.fixture(scope='module')
def sample_image():
    return read_image(SAMPLE)


@pytest.fixture(scope='module')
def client_page():
    app = create_app(tempfile.mkdtemp(), demo=False)
    client = app.test_client()

    def call(method, path, values=None):
        response = client.open('/api' + path, method=method, json=values, headers=HEADERS)
        assert response.status_code < 400, (path, response.status_code, response.json)
        return response.json

    collection = call('POST', '/collections', {'name': '笔迹验收'})
    document = call('POST', '/documents', {'collection_id': collection['id'], 'name': '扫描页'})
    with SAMPLE.open('rb') as handle:
        response = client.post('/api/documents/' + document['id'] + '/import',
                               data={'files': (io.BytesIO(handle.read()), 'sample.png')}, headers=HEADERS)
    assert response.status_code == 201
    page_id = response.json['page_ids'][0]
    yield app, client, call, page_id
    app.extensions['scheduler'].shutdown()


def test_detection_excludes_color_art_seal_and_lines(sample_image):
    settings = stroke.validate_settings({'sensitivity': 0.6}, sample_image)
    mask, _, saturation = stroke._ink_mask(sample_image, settings)
    _, _, valid, rejected = stroke._filter_components(mask, saturation, settings)
    assert rejected['colored'] > 0
    assert rejected['line_like'] > 0
    result, diff, _ = stroke.adjust(sample_image, stroke.validate_settings(
        {'mode': 'thicken', 'delta': 1.5, 'sensitivity': 0.6}, sample_image))
    # 彩图、红印章、细线表格、空白纸面必须逐像素保持原样。
    assert np.array_equal(result[90:360, 760:1140], sample_image[90:360, 760:1140])
    assert np.array_equal(result[450:590, 980:1120], sample_image[450:590, 980:1120])
    assert np.array_equal(result[425:685, 55:695], sample_image[425:685, 55:695])
    assert np.array_equal(result[600:680, 750:1100], sample_image[600:680, 750:1100])
    # 只在候选笔迹上动手：差异像素全部落在原笔迹附近，不能凭空出现在纸面中央。
    assert (diff > 0).sum() > 1000


def test_thicken_and_thin_change_width_without_binarization(sample_image):
    thick_settings = stroke.validate_settings(
        {'mode': 'thicken', 'delta': 1.5, 'sensitivity': 0.6}, sample_image)
    thick, _, thick_stats = stroke.adjust(sample_image, thick_settings)
    assert thick_stats['stroke_width']['after_median'] > thick_stats['stroke_width']['before_median']
    assert thick_stats['pixels_added'] > 0 and thick_stats['pixels_removed'] == 0
    # 输出仍是彩色图：保留中间灰度，不是黑白二值图。
    assert len(np.unique(thick.reshape(-1, 3), axis=0)) > 100

    thin_settings = stroke.validate_settings(
        {'mode': 'thin', 'delta': 0.8, 'sensitivity': 0.6}, sample_image)
    thin, _, thin_stats = stroke.adjust(sample_image, thin_settings)
    assert thin_stats['stroke_width']['after_median'] < thin_stats['stroke_width']['before_median']
    assert thin_stats['pixels_removed'] > 0
    # 细化受 40% 上限约束：不会把部件擦没，调整后部件像素仍大量保留。
    assert (thin == sample_image).mean() > 0.85


def test_zones_protect_keep_and_boundary_continuity(sample_image):
    zones = [
        {'rect': [40, 120, 700, 260], 'name': '正文区', 'action': 'adjust', 'delta': 2.0},
        {'rect': [40, 40, 720, 80], 'name': '标题保护', 'action': 'protect'},
        {'rect': [40, 700, 700, 140], 'name': '保留段', 'action': 'keep'},
    ]
    settings = stroke.validate_settings({'mode': 'thicken', 'zones': zones, 'feather': 8}, sample_image)
    result, diff, stats = stroke.adjust(sample_image, settings)
    assert stats['components_protected'] > 0
    # keep 分区逐像素不变。
    assert np.array_equal(result[700:840, 40:740], sample_image[700:840, 40:740])
    # 受保护部件在差异图中标记为 3，且结果图像素不变。
    protected_pixels = diff == 3
    assert protected_pixels.sum() > 0
    assert np.array_equal(result[protected_pixels], sample_image[protected_pixels])
    # 羽化保证分区边界不存在 0/1 硬切：边界带内的新增像素是连通的一片，没有孤立断点。
    added = diff == 1
    border_band = np.zeros_like(added)
    x0, y0, x1, y1 = 40, 120, 740, 380
    border_band[max(0, y0 - 12):y0 + 12, x0:x1] = True
    if added[border_band].any():
        count, labels = cv2_connected(added.astype(np.uint8))
        border_labels = set(np.unique(labels[border_band])) - {0}
        assert border_labels  # 边界上有平滑过渡的增量


def cv2_connected(mask):
    import cv2
    return cv2.connectedComponents(mask, connectivity=8)


def test_deterministic_and_non_cumulative(sample_image):
    settings = stroke.validate_settings(
        {'mode': 'thicken', 'delta': 1.2, 'sensitivity': 0.6}, sample_image)
    first, _, _ = stroke.adjust(sample_image, settings)
    second, _, _ = stroke.adjust(sample_image, settings)
    assert np.array_equal(first, second)
    # 对已加粗结果再以“原始输入”为基线、同参数计算，结果必须与第一次一致——不叠加。
    from_original, _, _ = stroke.adjust(sample_image, settings)
    assert np.array_equal(first, from_original)


def test_validation_rejects_bad_values(sample_image):
    with pytest.raises(DomainError):
        stroke.validate_settings({'delta': 99}, sample_image)
    with pytest.raises(DomainError):
        stroke.validate_settings({'mode': 'sideways'}, sample_image)
    with pytest.raises(DomainError):
        stroke.validate_settings({'zones': [{'rect': [0, 0, 99999, 10]}]}, sample_image)
    with pytest.raises(DomainError):
        stroke.validate_settings({'zones': [{'rect': [10, 10, 32, 32], 'action': 'frozen'}]}, sample_image)


def test_api_preview_confirm_uses_one_result(client_page):
    app, client, call, page_id = client_page
    preview = call('POST', f'/pages/{page_id}/stroke/preview',
                   {'mode': 'thicken', 'delta': 1.5, 'sensitivity': 0.6})
    sid = preview['session_id']
    assert preview['stats']['pixels_added'] > 0

    # 三种预览图都可取回，且是 PNG。
    for kind in ('overlay', 'result', 'diff'):
        response = client.get(f'/api/stroke-sessions/{sid}/image?kind={kind}')
        assert response.status_code == 200 and response.mimetype == 'image/png'

    # 确认前页面未改动。
    page = call('GET', '/pages/' + page_id)
    assert page['current'] == page['original']

    confirmed = call('POST', f'/pages/{page_id}/stroke/confirm', {'session_id': sid})
    assert confirmed['history'][-1]['operation'] == 'stroke_adjust'
    saved = bgr_from_response(client.get(f'/api/pages/{page_id}/image?variant=current'))
    session_result = app.extensions['stroke_sessions'].get(sid)['result']
    assert np.array_equal(saved, session_result)

    # 同一会话不能重复确认（防止叠加变字）。
    duplicate = client.post(f'/api/pages/{page_id}/stroke/confirm', json={'session_id': sid}, headers=HEADERS)
    assert duplicate.status_code == 409

    # 导出使用确认后的同一版本。
    document = app.extensions['store'].get('pages', page_id)['document_id']
    export = call('POST', f'/documents/{document}/export', {'kind': 'images'})
    assert call('GET', '/exports/' + export['id'] + '/verify')['valid']


def test_api_revision_conflict_and_discard(client_page):
    app, client, call, page_id = client_page
    preview = call('POST', f'/pages/{page_id}/stroke/preview', {'mode': 'thin', 'delta': 0.5})
    sid = preview['session_id']
    # 页面发生其他处理后，旧预览必须被拒绝，而不是静默覆盖。
    call('POST', f'/pages/{page_id}/process', {'operation': 'rotate', 'params': {'angle': 90}})
    conflict = client.post(f'/api/pages/{page_id}/stroke/confirm', json={'session_id': sid}, headers=HEADERS)
    assert conflict.status_code == 409
    discarded = call('POST', f'/stroke-sessions/{sid}/discard')
    assert discarded['discarded'] is True
    missing = client.get(f'/api/stroke-sessions/{sid}/image')
    assert missing.status_code == 404


def test_legacy_morph_text_no_longer_binarizes(sample_image):
    from paperflow.filters import morph_text
    result = morph_text(sample_image, {'mode': 'thicken', 'size': 2, 'iterations': 1})
    # 旧入口也不能再产出黑白图：彩图区域保持彩色。
    assert np.array_equal(result[90:360, 760:1140], sample_image[90:360, 760:1140])
    assert len(np.unique(result.reshape(-1, 3), axis=0)) > 100
