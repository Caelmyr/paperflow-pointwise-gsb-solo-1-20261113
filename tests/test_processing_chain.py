"""非破坏性处理链验收。

覆盖用户报告的核心问题：
1. 灰度/黑白之后切回彩色仍是黑白——链上改参从原图重算，颜色真实回来；
2. 后续去污叠在试出来的中间稿上——所有节点从原图按同一链重算；
3. 恢复原图丢掉已确认校正——历史条目保存完整链，可回到任一链版本；
4. 不适用步骤先指出差异，保留或重算由用户选择，不假装恢复丢失信息；
5. 预览、最终图像、提取区域与 OCR 有效状态对应同一 chain_revision；
6. 旧版本数据保持原样可读；重复确认相同方案不叠加、不产生新版本。
"""
import io
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperflow.imaging import read_image
from paperflow.web import create_app

HEADERS = {'X-PaperFlow': '1'}


@pytest.fixture(scope='module')
def work(tmp_path_factory):
    directory = tmp_path_factory.mktemp('paperflow-chain')
    app = create_app(directory, demo=False)
    client = app.test_client()

    def call(method, path, values=None):
        response = client.open('/api' + path, method=method, json=values, headers=HEADERS)
        assert response.status_code < 400, (path, response.status_code, response.json)
        return response.json

    collection = call('POST', '/collections', {'name': '处理链集合', 'category': '测试'})
    document = call('POST', '/documents', {'collection_id': collection['id'], 'name': '处理链文档'})
    with (ROOT / 'demo' / 'demo_1.png').open('rb') as handle:
        response = client.post(
            '/api/documents/' + document['id'] + '/import',
            data={'files': (io.BytesIO(handle.read()), 'demo_1.png')}, headers=HEADERS)
    assert response.status_code == 201, response.json
    page_id = response.json['page_ids'][0]
    yield app, client, call, page_id, directory
    app.extensions['scheduler'].shutdown()
    app.extensions['pipelines'].executor.shutdown(wait=True)


def test_01_chain_records_every_confirmed_step(work):
    app, client, call, page_id, directory = work
    call('POST', f'/pages/{page_id}/process', {'operation': 'deskew', 'params': {}})
    call('POST', f'/pages/{page_id}/process', {'operation': 'illumination', 'params': {'strength': 0.7}})
    call('POST', f'/pages/{page_id}/process', {'operation': 'color', 'params': {'mode': 'gray'}})
    call('POST', f'/pages/{page_id}/process', {'operation': 'denoise', 'params': {'area': 5, 'median': 1}})
    chain = call('GET', f'/pages/{page_id}/chain')
    assert [node['operation'] for node in chain['nodes']] == ['deskew', 'illumination', 'color', 'denoise']
    assert chain['color_available'] is False
    assert chain['rendered'] is True
    # 链上每一步都可回看参数与阶段。
    assert all(node['stage'] for node in chain['nodes'])
    color_node = next(node for node in chain['nodes'] if node['operation'] == 'color')
    assert color_node['loses_color'] is True


def test_02_switch_back_to_color_recovers_real_color(work):
    app, client, call, page_id, directory = work
    page = call('GET', f'/pages/{page_id}')
    gray = read_image(app.extensions['store'].resolve(page['current']))
    assert int(np.max(np.abs(gray[:, :, 0].astype(int) - gray[:, :, 1].astype(int)))) == 0
    chain = call('GET', f'/pages/{page_id}/chain')
    color_id = next(node['id'] for node in chain['nodes'] if node['operation'] == 'color')
    result = client.patch(f'/api/pages/{page_id}/chain/nodes/{color_id}',
                          json={'params': {'mode': 'color'}}, headers=HEADERS)
    assert result.status_code == 200, result.json
    page = call('GET', f'/pages/{page_id}')
    color = read_image(app.extensions['store'].resolve(page['current']))
    # 几何校正（deskew）、光照和去污节点仍然保留，颜色从原图真实恢复。
    chain = call('GET', f'/pages/{page_id}/chain')
    assert [node['operation'] for node in chain['nodes']] == ['deskew', 'illumination', 'color', 'denoise']
    assert chain['color_available'] is True
    diff = np.max(np.maximum(
        np.abs(color[:, :, 0].astype(int) - color[:, :, 1].astype(int)),
        np.abs(color[:, :, 1].astype(int) - color[:, :, 2].astype(int))))
    assert diff > 20


def test_03_upsert_color_does_not_stack_destructive_steps(work):
    app, client, call, page_id, directory = work
    # 连续切换色彩模式：color 节点始终只有一个，不会堆出一串丢色节点。
    for mode in ('gray', 'binary', 'adaptive', 'color'):
        response = client.post(f'/api/pages/{page_id}/chain/color',
                               json={'params': {'mode': mode}}, headers=HEADERS)
        assert response.status_code == 200, response.json
    chain = call('GET', f'/pages/{page_id}/chain')
    assert len([n for n in chain['nodes'] if n['operation'] == 'color']) == 1
    page_after = call('GET', f'/pages/{page_id}')
    # 最终停在 color，彩色信息可用。
    assert chain['color_available'] is True
    assert page_after['rendered_revision'] == page_after['chain_revision']


def test_04_duplicate_confirmation_creates_no_new_revision(work):
    app, client, call, page_id, directory = work
    page = call('GET', f'/pages/{page_id}')
    response = client.post(f'/api/pages/{page_id}/chain/color',
                           json={'params': {'mode': 'color'}}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json['changed'] is False
    again = call('GET', f'/pages/{page_id}')
    assert again['revision'] == page['revision']
    # 去污相同参数重复确认同样不叠加。
    response = client.post(f'/api/pages/{page_id}/chain/step',
                           json={'operation': 'denoise', 'params': {'area': 5, 'median': 1}},
                           headers=HEADERS)
    assert response.json['changed'] is False


def test_05_inapplicable_downstream_is_flagged_not_silently_dropped(work):
    app, client, call, page_id, directory = work
    # 在彩色链上追加需要彩色输入的白平衡节点。
    call('POST', f'/pages/{page_id}/process', {'operation': 'white_balance', 'params': {'strength': 0.5}})
    chain = call('GET', f'/pages/{page_id}/chain')
    wb = next(node for node in chain['nodes'] if node['operation'] == 'white_balance')
    assert wb['needs_color'] is True and wb['status'] == 'active'
    color_id = next(node['id'] for node in chain['nodes'] if node['operation'] == 'color')
    result = client.patch(f'/api/pages/{page_id}/chain/nodes/{color_id}',
                          json={'params': {'mode': 'binary', 'threshold': 150}}, headers=HEADERS)
    assert result.status_code == 200
    codes = {item['code'] for item in result.json['applicability']}
    assert 'color_unavailable' in codes
    chain = call('GET', f'/pages/{page_id}/chain')
    wb = next(node for node in chain['nodes'] if node['operation'] == 'white_balance')
    assert wb['status'] == 'inapplicable'
    # 节点仍然保留在链上，没有被静默删除。
    assert len(chain['nodes']) == 5
    # 依据已丢失时显式重算必须被拒绝，不能假装恢复。
    rejected = client.post(f'/api/pages/{page_id}/chain/nodes/{wb["id"]}/recompute',
                           json={}, headers=HEADERS)
    assert rejected.status_code == 409
    assert rejected.json['code'] == 'basis_lost'


def test_06_remove_loss_node_restores_applicability(work):
    app, client, call, page_id, directory = work
    chain = call('GET', f'/pages/{page_id}/chain')
    color_id = next(node['id'] for node in chain['nodes'] if node['operation'] == 'color')
    call('DELETE', f'/pages/{page_id}/chain/nodes/{color_id}')
    chain = call('GET', f'/pages/{page_id}/chain')
    assert chain['color_available'] is True
    assert all(node['status'] == 'active' for node in chain['nodes'])


def test_07_preview_is_distinct_from_committed_result(work):
    app, client, call, page_id, directory = work
    chain = call('GET', f'/pages/{page_id}/chain')
    candidate = [{'operation': node['operation'], 'params': node['params']} for node in chain['nodes']]
    candidate.append({'operation': 'gamma', 'params': {'gamma': 1.5}})
    preview = call('POST', f'/pages/{page_id}/chain/preview', {'nodes': candidate})
    assert preview['preview_url'].startswith('/api/previews/')
    preview_image = client.get(preview['preview_url'])
    assert preview_image.status_code == 200
    # 预览不改变已确认的链。
    unchanged = call('GET', f'/pages/{page_id}/chain')
    assert unchanged['chain_revision'] == chain['chain_revision']
    assert all(node['operation'] != 'gamma' for node in unchanged['nodes'])
    # 提交后链才变化，且最终图像与预览尺寸一致。
    committed = call('POST', f'/pages/{page_id}/chain/commit',
                     {'nodes': candidate, 'policy': 'recompute'})
    assert committed['page']['chain_revision'] > chain['chain_revision']
    page = call('GET', f'/pages/{page_id}')
    assert (page['width'], page['height']) == (preview['width'], preview['height'])


def test_08_regions_and_ocr_validity_track_chain_revision(work):
    app, client, call, page_id, directory = work
    region = call('POST', f'/pages/{page_id}/regions',
                  {'name': '插图', 'kind': 'illustration', 'region': [100, 100, 300, 300]})
    assert region['chain_revision'] == call('GET', f'/pages/{page_id}/chain')['chain_revision']
    # 修改链后，旧区域被标记为 stale，但记录保留。
    call('POST', f'/pages/{page_id}/chain/step',
         {'operation': 'illumination', 'params': {'strength': 0.3}})
    page = call('GET', f'/pages/{page_id}')
    assert page['regions'][0]['stale'] is True
    # 未确认的链提交后才改变 rendered；这里验证区域与 OCR 守卫对应同一链版本：
    # 先提交一条新链，再在该版本提取的区域必须绑定新 chain_revision。
    chain = call('GET', f'/pages/{page_id}/chain')
    candidate = [{'operation': n['operation'], 'params': n['params']} for n in chain['nodes']]
    candidate.append({'operation': 'contrast', 'params': {'contrast': 1.2, 'brightness': 0}})
    # 预览不落盘，不改变已确认链版本。
    preview = call('POST', f'/pages/{page_id}/chain/preview', {'nodes': candidate})
    assert preview['chain_revision'] == chain['chain_revision']
    mid = call('GET', f'/pages/{page_id}/chain')
    assert mid['chain_revision'] == chain['chain_revision']
    # 确认后新链生效，旧区域仍标记 stale，新区域绑定新版本。
    call('POST', f'/pages/{page_id}/chain/commit', {'nodes': candidate, 'policy': 'recompute'})
    page = call('GET', f'/pages/{page_id}')
    region_two = call('POST', f'/pages/{page_id}/regions',
                      {'name': '正文', 'kind': 'text', 'region': [50, 50, 200, 200]})
    page = call('GET', f'/pages/{page_id}')
    assert region_two['chain_revision'] == page['chain_revision']
    assert page['regions'][1]['stale'] is False
    assert page['regions'][0]['stale'] is True


def test_09_restore_original_keeps_history_and_history_restore_rebuilds_chain(work):
    app, client, call, page_id, directory = work
    page = call('GET', f'/pages/{page_id}')
    snapshot = next(entry for entry in reversed(page['history'])
                    if entry.get('metadata', {}).get('recipe_snapshot'))
    restored = call('POST', f'/pages/{page_id}/restore', {})
    assert restored['current'] == restored['original']
    assert restored['recipe'] == [] and restored['chain_revision'] == 0
    # 历史仍保留，且可回到某条完整链，而不是孤立像素稿。
    back = call('POST', f'/pages/{page_id}/restore', {'history_id': snapshot['id']})
    assert len(back['recipe']) > 0
    assert back['rendered_revision'] == back['chain_revision']
    rendered = read_image(app.extensions['store'].resolve(back['current']))
    historical = read_image(app.extensions['store'].resolve(snapshot['after']))
    assert rendered.shape == historical.shape
    assert np.array_equal(rendered, historical)


def test_10_legacy_page_without_recipe_remains_readable(work):
    app, client, call, page_id, directory = work
    # 模拟旧版本写入的页面：没有 recipe 字段。
    with app.extensions['store'].transaction() as state:
        state['pages'][page_id].pop('recipe', None)
        state['pages'][page_id].pop('chain_revision', None)
        state['pages'][page_id].pop('rendered_revision', None)
    chain = call('GET', f'/pages/{page_id}/chain')
    assert chain['nodes'] == [] and chain['chain_revision'] == 0
    # 旧页面仍可正常进入新链处理。
    call('POST', f'/pages/{page_id}/process', {'operation': 'illumination', 'params': {'strength': 0.5}})
    chain = call('GET', f'/pages/{page_id}/chain')
    assert [node['operation'] for node in chain['nodes']] == ['illumination']


def test_12_runtime_inapplicable_node_is_marked_not_fatal(work):
    app, client, call, page_id, directory = work
    # 先裁切到 300×300，再放一个引用 500×500 选区的去污节点：新输入下该节点失效。
    candidate = [
        {'operation': 'crop', 'params': {'region': [0, 0, 300, 300]}},
        {'operation': 'denoise', 'params': {'area': 5, 'median': 1, 'region': [100, 100, 500, 500]}},
    ]
    strict = client.post(f'/api/pages/{page_id}/chain/preview', json={'nodes': candidate}, headers=HEADERS)
    assert strict.status_code == 400
    committed = call('POST', f'/pages/{page_id}/chain/commit',
                     {'nodes': candidate, 'policy': 'recompute'})
    statuses = [node['status'] for node in committed['page']['recipe']]
    assert statuses == ['active', 'inapplicable']
    page = call('GET', f'/pages/{page_id}')
    assert (page['width'], page['height']) == (300, 300)
    assert page['rendered_revision'] == page['chain_revision']
    chain = call('GET', f'/pages/{page_id}/chain')
    assert any(item['code'] in ('runtime_inapplicable', 'kept_inapplicable') for item in chain['applicability'])


def test_11_ocr_validity_is_bound_to_chain_revision(work):
    app, client, call, page_id, directory = work
    health = call('GET', '/health')
    if not health['ocr']['available']:
        pytest.skip('本机未安装 Tesseract，跳过 OCR 链绑定验证')
    call('POST', f'/pages/{page_id}/ocr', {'language': 'chi_sim+eng'})
    page = call('GET', f'/pages/{page_id}')
    assert page['ocr']['stale'] is False
    assert page['ocr']['chain_revision'] == page['chain_revision']
    # 链变化后 OCR 失效；重新识别后绑定新链并恢复有效。
    call('POST', f'/pages/{page_id}/chain/step',
         {'operation': 'illumination', 'params': {'strength': 0.4}})
    page = call('GET', f'/pages/{page_id}')
    assert page['ocr']['stale'] is True
    call('POST', f'/pages/{page_id}/ocr', {'language': 'chi_sim+eng'})
    page = call('GET', f'/pages/{page_id}')
    assert page['ocr']['stale'] is False
    assert page['ocr']['chain_revision'] == page['chain_revision']
