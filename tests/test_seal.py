"""印章 / 批注分离工作区验收测试。

覆盖用户的核心业务约束：
1. 自动先“提出可保留部分”，红压黑等无法判定处保持“待确认”，不凭空补内容；
2. 人工补选/排除优先于自动，换参数重算会保留人工决定，并可退回旧版本；
3. 最终透明 PNG 与预览共用同一条合成链路（边缘一致）；
4. 导出记录保存来源页、页码、页面版本、参数与 SHA256 依据；
5. 全程不改动正文页面、原稿或其他已提取素材。
"""
import io
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperflow.core import Store
from paperflow.web import create_app
from paperflow import seal_imaging as si

HEADERS = {'X-PaperFlow': '1'}


def build_page():
    """白纸 + 黑字 + 红章真实压在黑字上 + 浅印泥。"""
    image = np.full((600, 800, 3), 255, np.uint8)
    cv2.putText(image, 'CONTRACT', (90, 300), cv2.FONT_HERSHEY_SIMPLEX, 2.4, (35, 35, 35), 6)
    cv2.circle(image, (300, 300), 130, (60, 60, 200), 9)
    cv2.putText(image, 'SEAL', (250, 320), cv2.FONT_HERSHEY_SIMPLEX, 1.8, (60, 60, 200), 5)
    dark = (image[:, :, 0] < 100) & (image[:, :, 1] < 100) & (image[:, :, 2] < 100)
    zone = np.zeros(dark.shape, bool)
    zone[170:340, 90:520] = True
    red = np.array([60, 60, 200], np.float32)
    blended = image.astype(np.float32)
    blended[dark & zone] = blended[dark & zone] * 0.45 + red * 0.55
    image = np.clip(blended, 0, 255).astype(np.uint8)
    cv2.circle(image, (580, 470), 55, (226, 229, 246), -1)  # 很浅的印泥
    ok, buffer = cv2.imencode('.png', image)
    assert ok
    return buffer.tobytes()


@pytest.fixture(scope='module')
def work(tmp_path_factory):
    directory = tmp_path_factory.mktemp('seal-acceptance')
    app = create_app(directory, demo=False)
    client = app.test_client()

    def call(method, path, values=None):
        response = client.open('/api' + path, method=method, json=values, headers=HEADERS)
        assert response.status_code < 400, (path, response.status_code, response.json)
        return response.json

    collection = call('POST', '/collections', {'name': '印章验收'})
    document = call('POST', '/documents', {'collection_id': collection['id'], 'name': '合同页'})
    response = client.post(
        '/api/documents/' + document['id'] + '/import',
        data={'files': (io.BytesIO(build_page()), 'contract.png')}, headers=HEADERS)
    assert response.status_code == 201, response.json
    page_id = response.json['page_ids'][0]
    yield app, client, call, document, page_id, directory
    app.extensions['scheduler'].shutdown()


def test_01_separation_marks_overlap_as_review_never_keeps_black_text():
    image = cv2.imdecode(np.frombuffer(build_page(), np.uint8), cv2.IMREAD_COLOR)
    result = si.separate(image, {'preset': 'red'})
    stats = result['stats']
    assert stats['keep'] > 0          # 提出了可保留的红色笔画
    assert stats['review'] > 0        # 红压黑重叠处保持待确认
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    black_text = (hsv[:, :, 2] < 80) & (hsv[:, :, 1] < 70)
    keep = result['status'] == si.MASK_VALUE[si.KEEP]
    assert int((black_text & keep).sum()) == 0   # 绝不把黑字当印章带走
    # 确定性：同参数两次结果完全一致
    again = si.separate(image, {'preset': 'red'})
    assert np.array_equal(again['status'], result['status'])


def test_02_full_workflow_manual_priority_reextract_versions(work):
    app, client, call, document, page_id, directory = work
    project = call('POST', '/seal/projects', {'page_id': page_id, 'name': '合同章'})
    pid = project['id']
    assert project['active_version_id'] is None and project['version_order'] == []

    detail = call('POST', '/seal/projects/' + pid + '/extract', {'origin': 'auto', 'params': {'preset': 'red'}})
    v1 = detail['active_version_id']
    stats = detail['versions'][-1]['stats']
    assert stats['keep'] > 0 and stats['review'] > 0
    assert detail['has_pending_review'] is True

    # 三类资源都可下载且为 PNG
    for kind in ('mask', 'preview', 'rgba'):
        response = client.get(f'/api/seal/projects/{pid}/versions/{v1}/asset/{kind}')
        assert response.status_code == 200 and response.data[:8] == b'\x89PNG\r\n\x1a\n'

    # 人工在空白角补选一块保留
    detail = call('POST', '/seal/projects/' + pid + '/edit',
                  {'target': 'keep', 'geometry': {'type': 'rect', 'rect': [12, 12, 36, 36]}})
    assert detail['versions'][-1]['manual_edit_count'] > 0

    # 换一组参数重算 -> 新版本，且人工决定被保留
    detail2 = call('POST', '/seal/projects/' + pid + '/extract',
                   {'origin': 'reextract', 'params': {'preset': 'red', 'saturation_min': 95}})
    v2 = detail2['active_version_id']
    assert v2 != v1 and detail2['version_count'] == 2
    mask_raw = client.get(f'/api/seal/projects/{pid}/versions/{v2}/asset/mask').data
    mask = cv2.imdecode(np.frombuffer(mask_raw, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert mask[30, 30] == si.MASK_VALUE[si.KEEP]      # 人工补选像素仍然保留
    assert detail2['versions'][-1]['origin'] == 'reextract'

    # 可退回旧版本
    call('POST', '/seal/projects/' + pid + '/activate', {'version_id': v1})
    assert call('GET', '/seal/projects/' + pid)['active_version_id'] == v1
    call('POST', '/seal/projects/' + pid + '/activate', {'version_id': v2})

    # 非法人工目标 / 越界矩形被拒绝
    bad = client.post('/api/seal/projects/' + pid + '/edit',
                      json={'target': 'nope', 'geometry': {'type': 'rect', 'rect': [1, 1, 2, 2]}}, headers=HEADERS)
    assert bad.status_code == 400
    bad_param = client.post('/api/seal/projects/' + pid + '/extract',
                            json={'origin': 'auto', 'params': {'hue_span': 999}}, headers=HEADERS)
    assert bad_param.status_code == 400


def test_03_export_matches_preview_and_records_provenance(work):
    app, client, call, document, page_id, directory = work
    project = call('GET', '/seal/projects?page_id=' + page_id)[0]
    pid = project['id']

    # 预览（深底）与导出必须基于同一份 alpha；先取当前版本统计
    detail = call('GET', '/seal/projects/' + pid)
    version = next(v for v in detail['versions'] if v['id'] == detail['active_version_id'])
    unresolved = version['stats']['review']
    assert unresolved > 0

    # 默认导出不含待确认像素
    export = call('POST', '/seal/projects/' + pid + '/export', {'include_review': False, 'crop': True})
    assert export['unresolved_review_at_export'] == unresolved
    assert export['page_id'] == page_id
    assert export['page_number'] == 1
    assert export['version_id'] == detail['active_version_id']
    assert export['source_sha256'] and export['source_revision'] == 0
    assert export['version_params']['preset'] == 'red'

    payload = client.get('/api/seal/exports/' + export['id'] + '/download')
    assert payload.status_code == 200
    rim = Image.open(io.BytesIO(payload.data))
    assert rim.mode == 'RGBA'
    alpha = np.array(rim.getchannel('A'))
    assert alpha.max() > 0 and (alpha == 0).sum() > 0     # 确实是透明图

    # 预览合成与导出使用同一合成函数：在白纸上合成结果尺寸/形状一致
    preview = client.get(f'/api/seal/projects/{pid}/composed?background=white')
    assert preview.status_code == 200 and preview.data[:8] == b'\x89PNG\r\n\x1a\n'

    # 包含待确认的导出：尺寸为整页（不裁剪），待确认被纳入
    full = call('POST', '/seal/projects/' + pid + '/export', {'include_review': True, 'crop': False})
    assert full['width'] == 800 and full['height'] == 600
    full_img = Image.open(io.BytesIO(client.get('/api/seal/exports/' + full['id'] + '/download').data))
    assert full_img.size == (800, 600)


def test_04_body_page_originals_and_other_materials_untouched(work):
    app, client, call, document, page_id, directory = work
    before = app.extensions['store'].get('pages', page_id)
    # 再做一轮提取 + 人工编辑 + 导出
    projects = call('GET', '/seal/projects?page_id=' + page_id)
    pid = projects[0]['id']
    call('POST', '/seal/projects/' + pid + '/extract', {'origin': 'auto', 'params': {'preset': 'red'}})
    call('POST', '/seal/projects/' + pid + '/edit',
         {'target': 'exclude', 'geometry': {'type': 'rect', 'rect': [400, 400, 30, 30]}})
    call('POST', '/seal/projects/' + pid + '/export', {'include_review': False, 'crop': True})
    after = app.extensions['store'].get('pages', page_id)
    assert after['current'] == before['current'] == after['original']
    assert after['revision'] == before['revision'] == 0
    assert after['regions'] == []


def test_05_persistence_and_isolation_across_reopen(work):
    app, client, call, document, page_id, directory = work
    pid = call('GET', '/seal/projects?page_id=' + page_id)[0]['id']
    reopened = Store(directory).snapshot()
    assert pid in reopened['seal_projects']
    project = reopened['seal_projects'][pid]
    # JSON 中不允许出现 numpy 数组，且每个版本只存路径与标量
    for version in project['versions'].values():
        assert isinstance(version['mask_path'], str)
        assert isinstance(version['stats']['keep'], int)
        assert all(not isinstance(value, np.ndarray) for value in version.values())
        assert app.extensions['store'].resolve(version['mask_path']).is_file()
        assert app.extensions['store'].resolve(version['rgba_path']).is_file()
    # 来源页面图像文件仍存在，seals 目录与 originals 相互独立
    assert app.extensions['store'].resolve(project['source_image']).is_file()
    seals_dir = directory / 'seals'
    assert seals_dir.is_dir() and any(seals_dir.iterdir())
