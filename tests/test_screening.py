"""印刷网纹分区治理的验收测试。

不依赖 Tesseract；用合成的彩色/灰度网点、织物纹理与小字页面验证：
检测分级、文字与织物保护、分区域确认、全分辨率预览与保存逐位一致、
重复调整不二次抹除，以及依据不足时默认保留。
"""
import io
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperflow.web import create_app

HEADERS = {'X-PaperFlow': '1'}


def build_halftone_page(path):
    rng = np.random.default_rng(13)

    def halftone(height, width, spacing, angle, ink, shade):
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        rad = np.deg2rad(angle)
        u = (xx * np.cos(rad) + yy * np.sin(rad)) / spacing
        v = (-xx * np.sin(rad) + yy * np.cos(rad)) / spacing
        d2 = ((np.abs(u - np.round(u)) * spacing) ** 2
              + (np.abs(v - np.round(v)) * spacing) ** 2)
        radius = spacing * 0.5 * np.sqrt(shade) * 1.9
        dots = (d2 <= radius ** 2).astype(np.float32)
        block = np.zeros((height, width, 3), np.float32)
        for channel in range(3):
            block[:, :, channel] = 255 * (1 - dots) + ink[channel] * dots
        return block

    def fabric(height, width, thread=14):
        yy, xx = np.mgrid[0:height, 0:width]
        over = ((xx // thread + yy // thread) % 2).astype(np.float32)
        base = np.full((height, width, 3), (196, 178, 150), np.float32)
        return np.clip(base * (1 - 0.18 * over)[:, :, None], 0, 255)

    page = np.full((1280, 1024, 3), (250, 252, 248), np.uint8)
    # 彩色三色叠印网点区
    ax, ay, aw, ah = 80, 200, 400, 360
    gradient = np.linspace(0.15, 0.8, aw)[None, :] * np.ones((ah, 1))
    cyan = halftone(ah, aw, 6.0, 15, (200, 190, 30), gradient)
    magenta = halftone(ah, aw, 6.3, 75, (120, 60, 200), 0.9 - gradient)
    yellow = halftone(ah, aw, 5.8, 0, (60, 210, 230), np.full((ah, aw), 0.35))
    page[ay:ay + ah, ax:ax + aw] = np.clip(255 * (cyan / 255) * (magenta / 255) * (yellow / 255), 0, 255)
    # 单色粗网点区
    bx, by, bw, bh = 560, 200, 400, 360
    shade = np.linspace(0.8, 0.2, bw)[None, :] * np.ones((bh, 1))
    page[by:by + bh, bx:bx + bw] = halftone(bh, bw, 9.5, 45, (40, 70, 160), np.clip(shade, 0.1, 0.9))
    # 织物纹理（保护对象）
    fx, fy, fw, fh = 80, 640, 400, 320
    page[fy:fy + fh, fx:fx + fw] = fabric(fh, fw)
    # 小字与细线（保护对象）
    for index in range(8):
        cv2.putText(page, f'small text line {index} preserve strokes',
                    (560, 680 + index * 36), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (20, 20, 20), 1, cv2.LINE_AA)
    for index in range(5):
        cv2.line(page, (560, 1000 + index * 20), (940, 1000 + index * 20), (60, 60, 60), 1)
    page = np.clip(page.astype(np.float32) + rng.normal(0, 3, page.shape), 0, 255).astype(np.uint8)
    cv2.imwrite(str(path), page)
    return page


@pytest.fixture(scope='module')
def work(tmp_path_factory):
    directory = tmp_path_factory.mktemp('paperflow-screen')
    app = create_app(directory, demo=False)
    client = app.test_client()

    def call(method, path, values=None):
        response = client.open('/api' + path, method=method, json=values, headers=HEADERS)
        assert response.status_code < 400, (path, response.status_code, response.json)
        return response.json

    collection = call('POST', '/collections', {'name': '网纹集合', 'category': '测试'})
    document = call('POST', '/documents', {'collection_id': collection['id'], 'name': '网纹页'})
    image_path = directory / 'halftone.png'
    build_halftone_page(image_path)
    with image_path.open('rb') as handle:
        response = client.post(
            '/api/documents/' + document['id'] + '/import',
            data={'files': (io.BytesIO(handle.read()), 'halftone.png')}, headers=HEADERS,
        )
    assert response.status_code == 201, response.json
    page_id = response.json['page_ids'][0]
    yield app, client, call, page_id, directory
    app.extensions['scheduler'].shutdown()
    app.extensions['pipelines'].executor.shutdown(wait=True)


def test_01_detection_levels_and_reasons(work):
    _, client, call, page_id, _ = work
    review = call('POST', f'/pages/{page_id}/screen/analyze', {})
    summary = review['grid_summary']
    assert summary['high'] > 20, summary
    assert summary['uncertain'] > 0, '织物/弱依据区域应落入待定'
    # 每个区域都带可解释依据与频谱证据
    for region in review['regions']:
        assert region['reason']
        assert 2.5 <= region['spacing'] <= 30
        assert 0 <= region['angle'] < 180
    # 高置信默认抑制，其余默认保留
    assert all(r['decision'] == 'suppress' for r in review['regions'] if r['confidence_level'] == 'high')
    assert all(r['decision'] == 'keep' for r in review['regions'] if r['confidence_level'] != 'high')
    # 网格证据包含关键频谱指标
    stored = client.application.extensions['store'].get('pages', page_id)['screen_review']
    sample = stored['grid'][0]['evidence']
    for key in ('sharpness', 'spacing', 'angle', 'symmetry', 'autocorrelation', 'chroma_prominence'):
        assert key in sample


def test_02_preview_metrics_and_native_resolution_assets(work):
    _, client, call, page_id, _ = work
    call('POST', f'/pages/{page_id}/screen/analyze', {})
    preview = call('POST', f'/pages/{page_id}/screen/preview', {'strength': 0.9})
    # 被抑制区域波纹能量显著下降，细节损失保持低位
    suppressed = [m for m in preview['region_metrics'] if m['decision'] == 'suppress']
    assert suppressed
    for metric in suppressed:
        reduction = (metric['energy_before'] - metric['energy_after']) / max(1e-6, metric['energy_before'])
        assert reduction > 0.3, metric
    assert preview['metrics']['detail_loss'] < 0.05
    # 全分辨率结果图与叠加图可下载
    result = client.get(f"/api/pages/{page_id}/screen/preview/{preview['id']}/image")
    overlay = client.get(f"/api/pages/{page_id}/screen/preview/{preview['id']}/image?kind=overlay")
    assert result.status_code == 200 and overlay.status_code == 200
    # 局部 1:1 裁切，确认缩放预览不掩盖原分辨率
    review = call('GET', f'/pages/{page_id}/screen/review')
    region = review['regions'][0]
    before = client.get(f"/api/pages/{page_id}/screen/preview/{preview['id']}/regions/{region['id']}/crop?kind=before")
    after = client.get(f"/api/pages/{page_id}/screen/preview/{preview['id']}/regions/{region['id']}/crop?kind=after")
    assert before.status_code == 200 and after.status_code == 200
    assert len(before.data) > 1000 and len(after.data) > 1000


def test_03_manual_protection_and_region_decisions(work):
    app, client, call, page_id, _ = work
    call('POST', f'/pages/{page_id}/screen/analyze', {})
    page = call('GET', f'/pages/{page_id}')
    # 覆盖织物区域的手动保护矩形
    fabric_rect = [80, 640, 320, 280]
    protected = call('POST', f'/pages/{page_id}/screen/protection',
                     {'rect': fabric_rect, 'label': '织物/文字保护'})
    assert any(item['rect'] == fabric_rect for item in protected['manual_protected'])
    preview = call('POST', f'/pages/{page_id}/screen/preview', {'strength': 0.95})
    # 保护矩形内像素必须与基线逐位一致（掩膜在该区域被清零）
    store = app.extensions['store']
    review = store.get('pages', page_id)['screen_review']
    baseline = cv2.imread(str(store.resolve(review['source_image'])))
    rendered = cv2.imread(str(store.resolve(
        next(item['path'] for item in review['previews'] if item['id'] == preview['id'])
    )))
    x, y, w, h = fabric_rect
    assert np.array_equal(baseline[y:y + h, x:x + w], rendered[y:y + h, x:x + w])
    # 高置信区域可改为保留
    high = next(r for r in review_regions(call, page_id) if r['confidence_level'] == 'high')
    updated = call('POST', f"/pages/{page_id}/screen/regions/{high['id']}/decision", {'decision': 'keep'})
    assert next(r for r in updated['regions'] if r['id'] == high['id'])['decision'] == 'keep'
    # 中置信区域默认保留（掩膜为 0）；用户强制 suppress 后必须真正生效
    medium = next(r for r in review_regions(call, page_id) if r['confidence_level'] == 'medium')
    idle = call('POST', f'/pages/{page_id}/screen/preview', {'strength': 0.9})
    idle_metric = next(m for m in idle['region_metrics'] if m['region_id'] == medium['id'])
    call('POST', f"/pages/{page_id}/screen/regions/{medium['id']}/decision", {'decision': 'suppress'})
    forced = call('POST', f'/pages/{page_id}/screen/preview', {'strength': 0.9})
    forced_metric = next(m for m in forced['region_metrics'] if m['region_id'] == medium['id'])
    assert forced_metric['mean_change'] > idle_metric['mean_change']
    # 恢复该区域为保留，避免影响后续用例
    call('POST', f"/pages/{page_id}/screen/regions/{medium['id']}/decision", {'decision': 'keep'})
    # 非法决策被拒绝
    bad = client.post(
        f"/api/pages/{page_id}/screen/regions/{high['id']}/decision",
        json={'decision': 'erase'}, headers=HEADERS,
    )
    assert bad.status_code == 400
    # 越界保护区域被拒绝
    bad_protect = client.post(
        f'/api/pages/{page_id}/screen/protection',
        json={'rect': [0, 0, 99999, 99999]}, headers=HEADERS,
    )
    assert bad_protect.status_code == 400


def review_regions(call, page_id):
    return call('GET', f'/pages/{page_id}/screen/review')['regions']


def test_04_apply_is_byte_identical_and_history_recorded(work):
    _, client, call, page_id, _ = work
    call('POST', f'/pages/{page_id}/screen/analyze', {})
    preview = call('POST', f'/pages/{page_id}/screen/preview', {'strength': 0.85})
    page_before = call('GET', f'/pages/{page_id}')
    applied = call('POST', f'/pages/{page_id}/screen/apply', {'preview_id': preview['id']})
    assert applied['revision'] == page_before['revision'] + 1
    assert applied['history'][-1]['operation'] == 'screen_suppress'
    # 保存的当前图与确认预览逐位一致
    saved = client.get(f'/api/pages/{page_id}/image?variant=current').data
    preview_bytes = client.get(f"/api/pages/{page_id}/screen/preview/{preview['id']}/image").data
    assert saved == preview_bytes
    # 不能用不存在的预览确认
    bad = client.post(f'/api/pages/{page_id}/screen/apply', json={'preview_id': 'nope'}, headers=HEADERS)
    assert bad.status_code == 409


def test_05_re_analysis_skips_already_suppressed(work):
    app, client, call, page_id, _ = work
    call('POST', f'/pages/{page_id}/screen/analyze', {})
    preview = call('POST', f'/pages/{page_id}/screen/preview', {'strength': 0.85})
    call('POST', f'/pages/{page_id}/screen/apply', {'preview_id': preview['id']})
    # 再次检测：已抑制图块被标记排除，不会当成新网纹
    call('POST', f'/pages/{page_id}/screen/analyze', {})
    stored = app.extensions['store'].get('pages', page_id)['screen_review']
    excluded = [cell for cell in stored['grid'] if cell['evidence'].get('already_suppressed')]
    assert len(excluded) > 30
    # 再次预览在已抑制区域的增量变化应很小（不二次抹除）
    second = call('POST', f'/pages/{page_id}/screen/preview', {'strength': 0.85})
    assert second['metrics']['detail_loss'] < 0.03


def test_06_restore_keeps_original_and_history(work):
    _, client, call, page_id, _ = work
    call('POST', f'/pages/{page_id}/screen/analyze', {})
    preview = call('POST', f'/pages/{page_id}/screen/preview', {'strength': 0.85})
    call('POST', f'/pages/{page_id}/screen/apply', {'preview_id': preview['id']})
    restored = call('POST', f'/pages/{page_id}/restore', {})
    assert restored['current'] == restored['original']
    # 历史仍保留网纹抑制记录，可回看
    assert any(item['operation'] == 'screen_suppress' for item in restored['history'])


def test_07_stale_review_rejected_after_image_change(work):
    _, client, call, page_id, _ = work
    call('POST', f'/pages/{page_id}/screen/analyze', {})
    # 图像被其他操作改变后，旧复核不能再用于预览/确认
    call('POST', f'/pages/{page_id}/process', {'operation': 'rotate', 'params': {'angle': 90}})
    stale_preview = client.post(
        f'/api/pages/{page_id}/screen/preview', json={'strength': 0.8}, headers=HEADERS,
    )
    assert stale_preview.status_code == 409
