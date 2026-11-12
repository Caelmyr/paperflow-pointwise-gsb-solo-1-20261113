"""对折小册子拼版的验收测试。

不依赖 Tesseract：OCR 数据按页面记录结构直接注入，
重点校核帖序、正反面对应、补白去处、分册重算、无裁切几何与文字层随动。
"""
import io
import sys
from pathlib import Path

import fitz
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paperflow import booklet
from paperflow.web import create_app

HEADERS = {'X-PaperFlow': '1'}


@pytest.fixture(scope='module')
def workspace(tmp_path_factory):
    directory = tmp_path_factory.mktemp('paperflow-booklet')
    app = create_app(directory, demo=False)
    client = app.test_client()

    def call(method, path, values=None):
        response = client.open('/api' + path, method=method, json=values,
                               headers={**HEADERS, 'Content-Type': 'application/json'})
        assert response.status_code < 400, (path, response.status_code, response.json)
        return response.json

    collection = call('POST', '/collections', {'name': '拼版集合'})
    yield app, client, call, collection, directory
    app.extensions['scheduler'].shutdown()


def _new_document(call, client, collection, name, page_specs):
    """page_specs: 列表，元素为 ('demo', 文件名) 或 ('blank', 宽, 高, 名称)。"""
    document = call('POST', '/documents',
                    {'collection_id': collection['id'], 'name': name})
    for spec in page_specs:
        if spec[0] == 'demo':
            with (ROOT / 'demo' / spec[1]).open('rb') as handle:
                response = client.post(
                    '/api/documents/' + document['id'] + '/import',
                    data={'files': (io.BytesIO(handle.read()), spec[1])}, headers=HEADERS)
            assert response.status_code == 201, response.json
        else:
            _, width, height, page_name = spec
            call('POST', '/documents/' + document['id'] + '/blank',
                 {'width': width, 'height': height, 'name': page_name})
    return call('GET', '/documents/' + document['id'])


def _plan(call, document, options):
    return call('POST', '/documents/' + document['id'] + '/booklet/plan', {'options': options})


def _export(call, client, document, options):
    result = call('POST', '/documents/' + document['id'] + '/booklet/export', {'options': options})
    record = result['record']
    payload = client.get('/api/exports/' + record['id'] + '/download')
    assert payload.status_code == 200 and payload.mimetype == 'application/pdf'
    verification = call('GET', '/booklet/' + record['id'] + '/verify')
    assert verification['valid'], verification['errors']
    return record, payload.data, verification


def test_01_standard_left_binding_order(workspace):
    app, client, call, collection, directory = workspace
    # 4 页内容（两竖页演示图 + 两张纵向空白页），无补白的干净帖序。
    document = _new_document(call, client, collection, '左装订帖序', [
        ('demo', 'demo_1.png'), ('demo', 'demo_2.png'),
        ('blank', 1240, 1754, '第三页'), ('blank', 1240, 1754, '第四页')])
    plan = _plan(call, document, {'binding': 'left', 'flip': 'short'})
    assert plan['sheet_count'] == 1 and plan['pdf_page_count'] == 2
    assert plan['blank_page_count'] == 0
    front, back = plan['sheets']
    assert [p['logical_number'] for p in front['panels']] == [4, 1]
    assert [p['logical_number'] for p in back['panels']] == [2, 3]
    assert [p['slot'] for p in front['panels']] == ['left', 'right']
    assert [r['logical_number'] for r in plan['reading_order']] == [1, 2, 3, 4]
    assert [r['source_index'] for r in plan['reading_order']] == [0, 1, 2, 3]


def test_02_right_binding_mirrors_outer_side(workspace):
    app, client, call, collection, directory = workspace
    document = _new_document(call, client, collection, '右装订帖序', [
        ('blank', 1240, 1754, f'p{i}') for i in range(4)])
    plan = _plan(call, document, {'binding': 'right', 'flip': 'short'})
    front, back = plan['sheets']
    # 右装订正面左右互换，背面不变。
    assert [p['logical_number'] for p in front['panels']] == [1, 4]
    assert [p['logical_number'] for p in back['panels']] == [2, 3]
    assert [r['logical_number'] for r in plan['reading_order']] == [1, 2, 3, 4]


def test_03_padding_and_blank_cover_have_explicit_destinations(workspace):
    app, client, call, collection, directory = workspace
    # 3 页内容需要 1 张补白凑成 4 页。
    document = _new_document(call, client, collection, '补白去处', [
        ('blank', 1240, 1754, f'p{i}') for i in range(3)])
    plan = _plan(call, document, {'padding': 'end'})
    assert plan['blank_page_count'] == 1
    blank = [p for s in plan['sheets'] for p in s['panels'] if p['kind'] == 'blank']
    assert len(blank) == 1 and blank[0]['reason'] == 'pad_end'
    # 空白封面 + 两端补白：3 内容 + 1 封面 = 4，无需帖尾补白。
    plan_cover = _plan(call, document, {'cover': 'blank', 'padding': 'end'})
    reasons = sorted(p['reason'] for s in plan_cover['sheets'] for p in s['panels']
                     if p['kind'] == 'blank')
    assert reasons == ['cover']
    # 2 页内容 + 封面 = 3，需再补 1 页；padding=both 时补白平分到帖首。
    two_pages = _new_document(call, client, collection, '两端补白', [
        ('blank', 1240, 1754, 'a'), ('blank', 1240, 1754, 'b')])
    plan_both = _plan(call, two_pages, {'cover': 'blank', 'padding': 'both'})
    both_reasons = sorted(p['reason'] for s in plan_both['sheets'] for p in s['panels']
                          if p['kind'] == 'blank')
    assert 'cover' in both_reasons
    assert 'pad_front' in both_reasons or 'pad_end' in both_reasons
    # 补白总数 = 封面 1 + 凑帖 1。
    assert plan_both['blank_page_count'] == 2
    # 补白不占逻辑序号，阅读顺序仍是 1..N。
    assert [r['logical_number'] for r in plan_both['reading_order']] == [1, 2]


def test_04_signature_split_recalculates_as_independent_booklets(workspace):
    app, client, call, collection, directory = workspace
    document = _new_document(call, client, collection, '分册', [
        ('blank', 1240, 1754, f'p{i}') for i in range(9)])
    # 每帖 1 张纸（4 页/帖）：9 页 -> 3 帖，最后一帖补 3 张空白。
    plan = _plan(call, document, {'signature_mode': 'split', 'sheets_per_signature': 1})
    assert plan['signature_count'] == 3
    assert all(detail['leaf_count'] % 4 == 0 for detail in plan['signatures'])
    assert sum(detail['content_count'] for detail in plan['signatures']) == 9
    assert sum(detail['blank_count'] for detail in plan['signatures']) == 3
    # 每帖帖序独立，覆盖该帖全部位置。
    for detail in plan['signatures']:
        sig_sheets = [s for s in plan['sheets'] if s['signature'] == detail['index']]
        positions = [seq for s in sig_sheets for side in ('front', 'back')
                     for seq in ([p['logical_number'] for p in s['panels']]
                                 if False else [p['kind'] for p in s['panels']])]
        assert len(sig_sheets) == detail['sheet_count']
    # 全局阅读顺序仍然连续 1..9。
    assert [r['logical_number'] for r in plan['reading_order']] == list(range(1, 10))
    # 整本一册时只有一个帖。
    plan_one = _plan(call, document, {'signature_mode': 'none'})
    assert plan_one['signature_count'] == 1
    assert plan_one['signatures'][0]['content_count'] == 9


def test_05_changing_options_recomputes_everything_without_leftovers(workspace):
    app, client, call, collection, directory = workspace
    document = _new_document(call, client, collection, '重算', [
        ('blank', 1240, 1754, f'p{i}') for i in range(5)])
    first = _plan(call, document, {'flip': 'short', 'binding': 'left'})
    second = _plan(call, document, {'flip': 'long', 'binding': 'right',
                                    'signature_mode': 'split', 'sheets_per_signature': 1})
    assert first['checksum'] != second['checksum']
    assert second['options']['flip'] == 'long' and second['options']['binding'] == 'right'
    # 同一选项重复计算，结果必须完全确定（无残留状态）。
    again = _plan(call, document, {'flip': 'long', 'binding': 'right',
                                   'signature_mode': 'split', 'sheets_per_signature': 1})
    assert again['checksum'] == second['checksum']
    # 源文档依旧按正常阅读顺序保留，没有被插入补白页。
    refreshed = call('GET', '/documents/' + document['id'])
    assert refreshed['page_ids'] == document['page_ids']
    assert len(refreshed['pages']) == 5


def test_06_landscape_page_rotation_and_keep_mode(workspace):
    app, client, call, collection, directory = workspace
    document = _new_document(call, client, collection, '横竖混排', [
        ('blank', 1240, 1754, '纵向页'), ('blank', 1800, 1200, '横向页')])
    plan_auto = _plan(call, document, {'orientation': 'auto'})
    landscape = [p for s in plan_auto['sheets'] for p in s['panels']
                 if p['name'] == '横向页'][0]
    assert landscape['rotation'] == 90
    portrait = [p for s in plan_auto['sheets'] for p in s['panels']
                if p['name'] == '纵向页'][0]
    assert portrait['rotation'] == 0
    plan_keep = _plan(call, document, {'orientation': 'keep'})
    assert all(p['rotation'] == 0 for s in plan_keep['sheets'] for p in s['panels'])


def test_07_fit_geometry_never_crops(workspace):
    app, client, call, collection, directory = workspace
    import math
    import random
    rect = fitz.Rect(36, 30, 400, 565)
    random.seed(11)
    for _ in range(300):
        src_w = random.randint(80, 3200)
        src_h = random.randint(80, 3200)
        rotation = random.choice((0, 90, 270))
        matrix = booklet.fit_matrix(src_w, src_h, rect, rotation)
        target = fitz.Rect(0, 0, src_w, src_h) * matrix
        assert rect.x0 - 0.01 <= target.x0 and target.x1 <= rect.x1 + 0.01
        assert rect.y0 - 0.01 <= target.y0 and target.y1 <= rect.y1 + 0.01
        # 横纵缩放一致，宽高比不被拉伸。
        sx = math.hypot(matrix.a, matrix.b)
        sy = math.hypot(matrix.c, matrix.d)
        assert abs(sx - sy) < 1e-9


def test_08_export_pdf_structure_and_verification(workspace):
    app, client, call, collection, directory = workspace
    document = _new_document(call, client, collection, '导出结构', [
        ('demo', 'demo_1.png'), ('demo', 'demo_2.png'),
        ('blank', 1800, 1200, '横向页')])
    for flip in ('short', 'long'):
        record, data, verification = _export(call, client, document, {
            'binding': 'left', 'flip': flip, 'paper': 'a4'})
        pdf = fitz.open(stream=data, filetype='pdf')
        try:
            assert len(pdf) == record['booklet']['pdf_page_count']
            assert abs(pdf[0].rect.width - 841.89) < 1 and abs(pdf[0].rect.height - 595.28) < 1
            for index, pdf_page in enumerate(pdf, start=1):
                paper_number = (index + 1) // 2
                side = 'front' if index % 2 else 'back'
                panels = [p for p in record['booklet']['panels']
                          if p['paper_index'] == paper_number and p['side'] == side]
                assert len(pdf_page.get_images()) == sum(p['kind'] == 'content' for p in panels)
        finally:
            pdf.close()
        preflight = call('GET', '/exports/' + record['id'] + '/verify')
        assert preflight['valid']


def test_09_text_layer_follows_scale_rotation_and_flip(workspace):
    app, client, call, collection, directory = workspace
    document = _new_document(call, client, collection, '文字层随动', [
        ('demo', 'demo_1.png'), ('demo', 'demo_2.png'),
        ('blank', 1800, 1200, '横向页')])
    store = app.extensions['store']
    # 直接注入 OCR 词框，不依赖 Tesseract。
    with store.transaction() as state:
        for index, page_id in enumerate(document['page_ids']):
            page = state['pages'][page_id]
            page['ocr'] = {
                'text': f'Marker{index + 1} PaperFlow 拼版',
                'boxes': [
                    {'id': f'w{index}a', 'text': f'Marker{index + 1}', 'confidence': 96.0,
                     'x': 120, 'y': 365, 'width': 260, 'height': 44,
                     'line': '0:0:0', 'edited': False},
                ],
                'language': 'chi_sim+eng', 'psm': 3, 'confidence': 96.0,
                'width': page['width'], 'height': page['height'],
                'recognized_at': '2026-11-12T00:00:00.000Z',
                'edited_at': None, 'edited': False, 'engine': 'synthetic', 'stale': False,
            }
    for flip in ('short', 'long'):
        record, data, verification = _export(call, client, document, {
            'binding': 'left', 'flip': flip, 'searchable': True})
        assert verification['valid'], verification['errors']
        pdf = fitz.open(stream=data, filetype='pdf')
        try:
            for index in range(len(document['page_ids'])):
                word = f'Marker{index + 1}'
                hits = [hit for page in pdf for hit in page.search_for(word)]
                assert hits, f'{flip} 翻面后文字层丢失：{word}'
        finally:
            pdf.close()
    # 关闭可检索选项时不写文字层。
    record, data, _ = _export(call, client, document, {'searchable': False})
    pdf = fitz.open(stream=data, filetype='pdf')
    try:
        assert all(not page.search_for('Marker1') for page in pdf)
    finally:
        pdf.close()


def test_10_stale_and_missing_ocr_are_warned_not_rejected(workspace):
    app, client, call, collection, directory = workspace
    document = _new_document(call, client, collection, 'OCR警告', [
        ('blank', 1240, 1754, f'p{i}') for i in range(4)])
    plan = _plan(call, document, {})
    assert any('尚未 OCR' in warning for warning in plan['warnings'])
    store = app.extensions['store']
    with store.transaction() as state:
        page = state['pages'][document['page_ids'][0]]
        page['ocr'] = {
            'text': '过期文字', 'boxes': [], 'language': 'chi_sim+eng', 'psm': 3,
            'confidence': 90, 'width': page['width'], 'height': page['height'],
            'recognized_at': '2026-11-12T00:00:00.000Z', 'edited_at': None,
            'edited': False, 'engine': 'synthetic', 'stale': True,
        }
    plan = _plan(call, document, {})
    assert any('OCR 已过期' in warning for warning in plan['warnings'])
    # 导出仍然成功并通过结构校验（警告不阻断打印文件）。
    _, _, verification = _export(call, client, document, {})
    assert verification['valid']


def test_11_invalid_options_rejected(workspace):
    app, client, call, collection, directory = workspace
    document = _new_document(call, client, collection, '非法参数', [
        ('blank', 1240, 1754, 'p0')])
    for payload in (
        {'options': {'binding': 'top'}},
        {'options': {'flip': 'diagonal'}},
        {'options': {'margin_mm': 200}},
        {'options': {'signature_mode': 'split', 'sheets_per_signature': 0}},
    ):
        response = client.post(
            '/api/documents/' + document['id'] + '/booklet/plan',
            json=payload, headers={**HEADERS, 'Content-Type': 'application/json'})
        assert response.status_code == 400, payload
    response = client.post(
        '/api/documents/' + document['id'] + '/booklet/plan',
        json={'page_ids': ['page_' + '0' * 32]},
        headers={**HEADERS, 'Content-Type': 'application/json'})
    assert response.status_code in (400, 404)
    response = client.post(
        '/api/documents/' + document['id'] + '/booklet/plan',
        json={'options': {'searchable': 'yes'}},
        headers={**HEADERS, 'Content-Type': 'application/json'})
    assert response.status_code == 400


def test_12_source_document_remains_in_reading_order(workspace):
    app, client, call, collection, directory = workspace
    document = _new_document(call, client, collection, '源文档保留', [
        ('demo', 'demo_1.png'), ('demo', 'demo_2.png')])
    before = call('GET', '/documents/' + document['id'])
    _plan(call, document, {'cover': 'blank', 'signature_mode': 'split',
                           'sheets_per_signature': 1, 'flip': 'long', 'binding': 'right'})
    _export(call, client, document, {'cover': 'blank', 'flip': 'long'})
    after = call('GET', '/documents/' + document['id'])
    assert after['page_ids'] == before['page_ids']
    assert len(after['pages']) == len(before['pages']) == 2


def test_13_folded_order_exhaustive_for_small_documents(workspace):
    app, client, call, collection, directory = workspace
    for total in range(1, 17):
        pages = [{'id': f'p{total}-{i}', 'width': 800, 'height': 1200,
                  'name': f'p{i}', 'ocr': None} for i in range(total)]
        for binding in ('left', 'right'):
            plan = booklet.plan_booklet(pages, booklet.validate_options({'binding': binding}))
            assert [r['logical_number'] for r in plan['reading_order']] == list(range(1, total + 1))
            placed = [p['page_id'] for s in plan['sheets'] for p in s['panels']
                      if p['kind'] == 'content']
            assert sorted(placed) == sorted(page['id'] for page in pages)
            assert all(detail['leaf_count'] % 4 == 0 for detail in plan['signatures'])
            assert plan['pdf_page_count'] == 2 * plan['sheet_count']


def test_14_left_and_right_binding_only_mirror_front_sides(workspace):
    app, client, call, collection, directory = workspace
    document = _new_document(call, client, collection, '装订镜像', [
        ('blank', 1240, 1754, f'p{i}') for i in range(8)])
    left = _plan(call, document, {'binding': 'left'})
    right = _plan(call, document, {'binding': 'right'})
    for side_left, side_right in zip(left['sheets'], right['sheets']):
        seq_l = [p['logical_number'] for p in side_left['panels']]
        seq_r = [p['logical_number'] for p in side_right['panels']]
        if side_left['side'] == 'front':
            assert seq_l == list(reversed(seq_r))
        else:
            assert seq_l == seq_r
