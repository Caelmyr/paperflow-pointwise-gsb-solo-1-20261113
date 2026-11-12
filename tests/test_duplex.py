import io
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperflow.web import create_app

HEADERS = {'X-PaperFlow': '1'}


@pytest.fixture(scope='module')
def work(tmp_path_factory):
    directory = tmp_path_factory.mktemp('paperflow-duplex')
    app = create_app(directory, demo=False)
    client = app.test_client()

    def call(method, path, values=None):
        response = client.open('/api' + path, method=method, json=values, headers=HEADERS)
        assert response.status_code < 400, (path, response.status_code, response.json)
        return response.json

    def import_images(document_id, images, name):
        files = []
        for index, image in enumerate(images):
            buffer = io.BytesIO()
            Image.fromarray(image).save(buffer, format='PNG')
            buffer.seek(0)
            files.append((buffer, f'{name}_{index}.png'))
        response = client.post(
            '/api/documents/' + document_id + '/import',
            data={'files': files},
            headers=HEADERS,
            content_type='multipart/form-data',
        )
        assert response.status_code in (201, 207), response.json
        return response.json

    collection = call('POST', '/collections', {'name': '双面验收集合'})
    yield app, client, call, import_images, collection, directory
    app.extensions['scheduler'].shutdown()


def make_sheet(seed, width=620, height=880):
    """内容由种子决定的合成页：模拟带横向文字行的扫描页，不同种子差异明显。"""
    rng = np.random.default_rng(seed)
    canvas = Image.new('RGB', (width, height), (250, 250, 248))
    draw = ImageDraw.Draw(canvas)
    # 顶部标题带与贯穿大部分宽度的横向文字行，提供明确的版面朝向特征。
    draw.rectangle((50, 46, width - 50, 96), fill=(38, 58, 48))
    y = 140
    for _ in range(16):
        line_width = int(rng.integers(int(width * 0.45), width - 90))
        draw.rectangle((60, y, 60 + line_width, y + int(rng.integers(9, 16))), fill=(48, 66, 56))
        y += int(rng.integers(34, 46))
    for _ in range(4):
        x0 = int(rng.integers(60, width - 160))
        size = int(rng.integers(40, 90))
        draw.ellipse((x0, y, x0 + size, y + size), outline=(70, 90, 80), width=3)
    return np.asarray(canvas)


def rescan(image, seed):
    rng = np.random.default_rng(seed)
    shifted = np.roll(image, (6, -8), axis=(0, 1))
    noise = rng.normal(0, 2, shifted.shape).astype(np.int16)
    return np.clip(shifted.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def build_document(client, call, collection):
    document = call('POST', '/documents', {'collection_id': collection['id'], 'name': '双面验收文档'})
    fronts = [make_sheet(100 + i) for i in range(4)]
    # 第 4 张正面（索引3）重扫一次形成重复页；正面共 4 个来源页。
    front_upload = fronts[:3] + [rescan(fronts[2], 7)]
    # 背面 180° 倒置进纸，另加一张真正的空白页。
    backs = [np.rot90(np.rot90(make_sheet(200 + i))) for i in range(4)]
    back_upload = backs + [np.full((880, 620, 3), 255, np.uint8)]
    for images, name in ((front_upload, 'front_batch'), (back_upload, 'back_batch')):
        client_post_images(client, document['id'], images, name)
    # 补拍页：独立来源。
    client_post_images(client, document['id'], [rescan(make_sheet(103), 9)], 'phone_supplement')
    return call('GET', '/documents/' + document['id'])


def client_post_images(client, document_id, images, name):
    # 同一批次的页面使用相同来源名，模拟两次扫描各自导入的一批文件。
    files = []
    for image in images:
        buffer = io.BytesIO()
        Image.fromarray(image).save(buffer, format='PNG')
        buffer.seek(0)
        files.append((buffer, name + '.png'))
    response = client.post(
        '/api/documents/' + document_id + '/import',
        data={'files': files},
        headers=HEADERS,
        content_type='multipart/form-data',
    )
    assert response.status_code in (201, 207), response.json
    return response.json


def test_01_session_analysis_batches_duplicates_blanks(work):
    app, client, call, import_images, collection, directory = work
    document = build_document(client, call, collection)
    assert len(document['page_ids']) == 10
    session = call('POST', '/documents/' + document['id'] + '/pairing', {'name': '配对会话'})
    assert session['status'] == 'open'
    analysis = session['analysis']
    # 三个来源批次，角色自动判定为正面/背面/补拍。
    roles = set(session['role_map'].values())
    assert {'front', 'back', 'supplement'} <= roles
    assert analysis['front_count'] == 4 and analysis['back_count'] == 5
    # 重扫页被识别为重复组，且系统没有删除任何页面。
    assert len(analysis['duplicate_groups']) == 1
    assert len(analysis['duplicate_groups'][0]['pages']) == 2
    # 空白页被标出，绝不自动丢弃。
    assert len(analysis['blank_pages']) == 1
    assert analysis['blank_pages'][0]['blank'] is True
    # 初始无锚点时给出明确警示，不假装高置信。
    assert any(warning['code'] == 'no_anchor' for warning in analysis['warnings'])
    # 倒置进纸的背面建议旋转次数为 2（180°）。
    assert all(suggestion['turn'] == 2 for suggestion in analysis['suggestions'])


def test_02_anchor_realigns_remaining_suggestions(work):
    app, client, call, import_images, collection, directory = work
    document = build_document(client, call, collection)
    session = call('POST', '/documents/' + document['id'] + '/pairing', {'name': '锚点会话'})
    first = session['analysis']['suggestions'][0]
    preview = call('POST', '/pairing/' + session['id'] + '/confirm-preview', {
        'front_id': first['front_id'], 'back_id': first['back_id'], 'turn': 2,
    })
    assert 'suggestions_changed' in preview and 'unpaired_before' in preview
    call('POST', '/pairing/' + session['id'] + '/confirm', {
        'front_id': first['front_id'], 'back_id': first['back_id'], 'turn': 2, 'anchor': True,
    })
    updated = call('GET', '/pairing/' + session['id'])
    assert any(pair['anchor'] for pair in updated['pairs'])
    assert all(item['method'] in ('anchor', 'ordinal') for item in updated['analysis']['suggestions'])


def test_03_blank_requires_acknowledgement_and_never_auto_deleted(work):
    app, client, call, import_images, collection, directory = work
    document = build_document(client, call, collection)
    session = call('POST', '/documents/' + document['id'] + '/pairing', {'name': '空白会话'})
    blank_id = session['analysis']['blank_pages'][0]['id']
    rejected = client.post('/api/pairing/' + session['id'] + '/ignore',
                           json={'page_id': blank_id, 'reason': '空白'}, headers=HEADERS)
    assert rejected.status_code == 409
    call('POST', '/pairing/' + session['id'] + '/ignore', {
        'page_id': blank_id, 'reason': '人工核对确认为空白', 'acknowledge_blank': True,
    })
    refreshed = call('GET', '/pairing/' + session['id'])
    assert refreshed['ignored'][0]['blank_acknowledged'] is True
    assert len(call('GET', '/documents/' + document['id'])['page_ids']) == 10


def test_04_duplicate_ignore_keep_and_candidates(work):
    app, client, call, import_images, collection, directory = work
    document = build_document(client, call, collection)
    session = call('POST', '/documents/' + document['id'] + '/pairing', {'name': '重复会话'})
    group = session['analysis']['duplicate_groups'][0]
    duplicate_id = group['pages'][1]['id']
    call('POST', '/pairing/' + session['id'] + '/ignore', {'page_id': duplicate_id, 'reason': '重扫忽略'})
    refreshed = call('GET', '/pairing/' + session['id'])
    assert any(item['page_id'] == duplicate_id for item in refreshed['ignored'])
    call('POST', '/pairing/' + session['id'] + '/keep', {'page_id': duplicate_id})
    restored = call('GET', '/pairing/' + session['id'])
    assert all(item['page_id'] != duplicate_id for item in restored['ignored'])
    candidates = call('GET', '/pairing/' + session['id'] + '/candidates/' + group['pages'][0]['id'])
    assert candidates and all('score' in item and 'turn' in item for item in candidates)


def test_05_readiness_blocks_until_all_pages_accounted(work):
    app, client, call, import_images, collection, directory = work
    document = build_document(client, call, collection)
    session = call('POST', '/documents/' + document['id'] + '/pairing', {'name': '完成度会话'})
    sid = session['id']
    assert call('GET', '/pairing/' + sid + '/readiness')['ready'] is False
    suggestion = session['analysis']['suggestions'][0]
    call('POST', '/pairing/' + sid + '/confirm', {
        'front_id': suggestion['front_id'], 'back_id': suggestion['back_id'],
        'turn': suggestion['turn'], 'anchor': True,
    })
    blocked = client.post('/api/pairing/' + sid + '/finalize', json={'name': '新文档'}, headers=HEADERS)
    assert blocked.status_code == 409


def test_06_finalize_creates_new_document_and_preserves_originals(work):
    app, client, call, import_images, collection, directory = work
    document = build_document(client, call, collection)
    session = call('POST', '/documents/' + document['id'] + '/pairing', {'name': '最终会话'})
    sid = session['id']
    analysis = session['analysis']
    call('POST', '/pairing/' + sid + '/ignore', {
        'page_id': analysis['duplicate_groups'][0]['pages'][1]['id'], 'reason': '重扫',
    })
    call('POST', '/pairing/' + sid + '/ignore', {
        'page_id': analysis['blank_pages'][0]['id'], 'reason': '空白', 'acknowledge_blank': True,
    })
    for suggestion in call('GET', '/pairing/' + sid)['analysis']['suggestions']:
        call('POST', '/pairing/' + sid + '/confirm', {
            'front_id': suggestion['front_id'], 'back_id': suggestion['back_id'],
            'turn': suggestion['turn'], 'anchor': False,
        })
    current = call('GET', '/pairing/' + sid)
    for page in current['analysis']['unpaired_pages']:
        call('POST', '/pairing/' + sid + '/ignore', {'page_id': page['id'], 'reason': '人工暂不采用'})
    readiness = call('GET', '/pairing/' + sid + '/readiness')
    assert readiness['ready'] is True
    result = call('POST', '/pairing/' + sid + '/finalize', {'name': '阅读顺序正确的新文档'})
    new_document = result['document']
    roles = [item['role'] for item in result['order']]
    assert roles[:2] == ['front', 'back'] and all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))
    assert len(new_document['page_ids']) == 2 * readiness['confirmed_pairs']
    assert len(call('GET', '/documents/' + document['id'])['page_ids']) == 10
    rotated = [page for page in new_document['pages'] if any(h['operation'] == 'rotate' for h in page['history'])]
    assert rotated, '倒置背面应在新文档副本上旋转'
    original = call('GET', '/documents/' + document['id'])
    assert all(not any(h['operation'] == 'rotate' for h in page['history']) for page in original['pages'])


def test_07_reopen_keeps_confirmed_pairs_and_persistence(work):
    app, client, call, import_images, collection, directory = work
    document = build_document(client, call, collection)
    session = call('POST', '/documents/' + document['id'] + '/pairing', {'name': '持久化会话'})
    sid = session['id']
    suggestion = session['analysis']['suggestions'][0]
    call('POST', '/pairing/' + sid + '/confirm', {
        'front_id': suggestion['front_id'], 'back_id': suggestion['back_id'],
        'turn': suggestion['turn'], 'anchor': True,
    })
    from paperflow.core import Store
    from paperflow.duplex import Duplex
    reopened_store = Store(directory)
    loaded = Duplex(reopened_store).get(sid)
    assert len(loaded['pairs']) == 1 and loaded['pairs'][0]['anchor'] is True
    child = call('POST', '/pairing/' + sid + '/reopen', {'name': '继续调整'})
    assert child['reopened_from'] == sid and len(child['pairs']) == 1
    assert child['id'] != sid and child['status'] == 'open'


def test_08_revision_history_records_every_decision(work):
    app, client, call, import_images, collection, directory = work
    document = build_document(client, call, collection)
    session = call('POST', '/documents/' + document['id'] + '/pairing', {'name': '日志会话'})
    sid = session['id']
    suggestion = session['analysis']['suggestions'][0]
    call('POST', '/pairing/' + sid + '/confirm', {
        'front_id': suggestion['front_id'], 'back_id': suggestion['back_id'],
        'turn': suggestion['turn'], 'anchor': True,
    })
    pair_id = call('GET', '/pairing/' + sid)['pairs'][0]['id']
    call('POST', '/pairing/' + sid + '/unconfirm', {'pair_id': pair_id})
    actions = [event['action'] for event in call('GET', '/events')]
    assert '创建双面配对会话' in actions
    assert '确认正反配对' in actions and '撤销配对' in actions
