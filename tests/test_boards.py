import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperflow.imaging import detect_sheets, perspective, read_image
from paperflow.web import create_app

HEADERS = {'X-PaperFlow': '1'}
BOARD_IMAGE = ROOT / 'demo' / 'demo_receipt_board.png'


@pytest.fixture(scope='module')
def work(tmp_path_factory):
    directory = tmp_path_factory.mktemp('paperflow-boards')
    app = create_app(directory, demo=False)
    client = app.test_client()

    def call(method, path, values=None):
        response = client.open('/api' + path, method=method, json=values, headers=HEADERS)
        assert response.status_code < 400, (path, response.status_code, response.json)
        return response.json

    collection = call('POST', '/collections', {'name': '票据集合', 'category': '验收'})
    document = call('POST', '/documents', {'collection_id': collection['id'], 'name': '桌面票据照片'})
    with BOARD_IMAGE.open('rb') as handle:
        response = client.post('/api/documents/' + document['id'] + '/import',
                               data={'files': (io.BytesIO(handle.read()), 'receipt_board.png')}, headers=HEADERS)
    assert response.status_code == 201, response.json
    page_id = response.json['page_ids'][0]
    target = call('POST', '/documents', {'collection_id': collection['id'], 'name': '票据结果文档'})
    board = call('POST', '/pages/' + page_id + '/boards', {'name': '验收票据板'})
    yield app, client, call, document, page_id, target, board['id'], directory
    app.extensions['scheduler'].shutdown()


def reset_board(call, client, page_id, board_id):
    """每个用例从一块全新的候选集开始；结果页保留但断开出处。"""
    call('POST', '/boards/' + board_id + '/reset', {})
    return call('POST', '/boards/' + board_id + '/detect', {'replace': True})


def test_01_multi_sheet_detection_separates_overlapping_receipts():
    image = read_image(BOARD_IMAGE)
    detection = detect_sheets(image)
    assert detection['detected']
    assert len(detection['candidates']) == 4
    for candidate in detection['candidates']:
        assert len(candidate['points']) == 4
        corrected, _ = perspective(image, candidate['points'])
        assert min(corrected.shape[:2]) >= 32
    # 单张文档页面不会被误拆成多张
    single = detect_sheets(read_image(ROOT / 'demo' / 'demo_1.png'))
    assert len(single['candidates']) == 1
    practice = detect_sheets(read_image(ROOT / 'demo' / 'practice' / 'receipt_board.png'))
    assert len(practice['candidates']) == 3


def test_02_board_detect_lists_four_candidates_and_serves_anchor(work):
    app, client, call, document, page_id, target, board_id, directory = work
    board = reset_board(call, client, page_id, board_id)
    assert board['page_id'] == page_id
    assert len(board['candidates']) == 4
    assert {candidate['status'] for candidate in board['candidates']} == {'candidate'}
    response = client.post('/api/pages/' + page_id + '/boards', json={'name': '重复板'}, headers=HEADERS)
    assert response.status_code == 409
    image = client.get('/api/boards/' + board_id + '/image')
    assert image.status_code == 200 and len(image.data) > 1000
    listing = call('GET', '/boards?document_id=' + document['id'])
    assert any(item['id'] == board_id and item['candidate_count'] == 4 for item in listing)


def test_03_manual_candidate_validation_and_overlap_guard(work):
    app, client, call, document, page_id, target, board_id, directory = work
    board = reset_board(call, client, page_id, board_id)
    candidates = board['candidates']
    for candidate in candidates:
        call('POST', '/boards/' + board_id + '/candidates/' + candidate['id'] + '/confirm', {})
    # 同一块可见内容不能重复认作两张票据
    duplicate = client.post('/api/boards/' + board_id + '/candidates',
                            json={'name': '重复票据', 'points': candidates[0]['points']}, headers=HEADERS)
    assert duplicate.status_code == 409 and duplicate.json['code'] == 'overlap'
    # 非法角点被拒绝
    bad = client.post('/api/boards/' + board_id + '/candidates',
                      json={'name': '坏轮廓', 'points': [[0, 0], [10, 0], [5, 5]]}, headers=HEADERS)
    assert bad.status_code == 400
    # 在空白桌面区域手描一个新候选可以成功，且不与既有候选冲突
    free = [[60, 60], [board['width'] - 100, 60], [board['width'] - 100, 150], [60, 150]]
    added = call('POST', '/boards/' + board_id + '/candidates', {'name': '手描补漏', 'points': free})
    assert len(added['candidates']) == len(candidates) + 1


def test_04_split_and_merge_candidates(work):
    app, client, call, document, page_id, target, board_id, directory = work
    board = reset_board(call, client, page_id, board_id)
    source = board['candidates'][0]
    points = source['points']
    first = [[p[0] - 80, p[1]] for p in points]
    second = [[p[0] + 80, p[1]] for p in points]
    split = call('POST', '/boards/' + board_id + '/candidates/' + source['id'] + '/split',
                 {'candidates': [first, second]})
    new_ids = [c['id'] for c in split['candidates'] if c['history'][0]['action'] == 'split']
    assert len(new_ids) == 2
    assert all(c['id'] != source['id'] for c in split['candidates'])
    merged = call('POST', '/boards/' + board_id + '/merge',
                  {'candidate_ids': new_ids, 'name': '合并回的票据'})
    assert '合并回的票据' in [c['name'] for c in merged['candidates']]
    assert len(merged['candidates']) == len(board['candidates'])


def test_05_extract_independent_pages_with_provenance_and_ocr(work):
    app, client, call, document, page_id, target, board_id, directory = work
    board = reset_board(call, client, page_id, board_id)
    candidates = sorted(board['candidates'], key=lambda item: item['points'][0][1])
    amounts = ['120.00', '195.00', '270.00', '86.50']
    extracted_pages = []
    for index, candidate in enumerate(candidates):
        call('POST', '/boards/' + board_id + '/candidates/' + candidate['id'] + '/confirm', {})
        result = call('POST', '/boards/' + board_id + '/candidates/' + candidate['id'] + '/extract',
                      {'document_id': target['id'], 'ocr': True})
        updated = next(item for item in result['candidates'] if item['id'] == candidate['id'])
        assert updated['status'] == 'extracted' and updated['result']['ocr']
        page = call('GET', '/pages/' + updated['result']['page_id'])
        assert amounts[index] in page['ocr']['text']
        provenance = call('GET', '/pages/' + page['id'] + '/board-provenance')
        assert provenance['from_board']
        assert provenance['candidate']['id'] == candidate['id']
        assert provenance['source_page_id'] == page_id
        extracted_pages.append(page['id'])
    assert len(set(extracted_pages)) == 4
    result_doc = call('GET', '/documents/' + target['id'])
    assert len(result_doc['page_ids']) == 4 and len(result_doc['pages']) == 4
    # 已校正候选的轮廓被锁定
    locked = client.patch('/api/boards/' + board_id + '/candidates/' + candidates[0]['id'],
                          json={'points': candidates[1]['points']}, headers=HEADERS)
    assert locked.status_code == 409


def test_06_reextract_keeps_other_receipts_and_old_page(work):
    app, client, call, document, page_id, target, board_id, directory = work
    board = reset_board(call, client, page_id, board_id)
    candidate = board['candidates'][0]
    call('POST', '/boards/' + board_id + '/candidates/' + candidate['id'] + '/confirm', {})
    first = call('POST', '/boards/' + board_id + '/candidates/' + candidate['id'] + '/extract',
                 {'document_id': target['id'], 'ocr': False})
    first_page = next(c for c in first['candidates'] if c['id'] == candidate['id'])['result']['page_id']
    before_revisions = {c['id']: c['updated_at'] for c in first['candidates']}
    again = call('POST', '/boards/' + board_id + '/candidates/' + candidate['id'] + '/reextract',
                 {'document_id': target['id'], 'ocr': False})
    new_page = next(c for c in again['candidates'] if c['id'] == candidate['id'])['result']['page_id']
    assert new_page != first_page
    assert call('GET', '/pages/' + first_page)['id'] == first_page
    for other in again['candidates']:
        if other['id'] != candidate['id']:
            assert other['updated_at'] == before_revisions[other['id']]


def test_07_blocked_receipt_keeps_position_and_reason_others_continue(work):
    app, client, call, document, page_id, target, board_id, directory = work
    board = reset_board(call, client, page_id, board_id)
    blocked_candidate, good_candidate = board['candidates'][2], board['candidates'][3]
    blocked = call('POST', '/boards/' + board_id + '/candidates/' + blocked_candidate['id'] + '/block',
                   {'reason': '左下角被遮挡，角点不可靠'})
    record = next(c for c in blocked['candidates'] if c['id'] == blocked_candidate['id'])
    assert record['status'] == 'blocked' and record['block_reason'] == '左下角被遮挡，角点不可靠'
    refused = client.post('/api/boards/' + board_id + '/candidates/' + blocked_candidate['id'] + '/extract',
                          json={}, headers=HEADERS)
    assert refused.status_code == 409
    reopened = call('POST', '/boards/' + board_id + '/candidates/' + blocked_candidate['id'] + '/reopen', {})
    assert next(c for c in reopened['candidates'] if c['id'] == blocked_candidate['id'])['status'] == 'candidate'
    call('POST', '/boards/' + board_id + '/candidates/' + good_candidate['id'] + '/confirm', {})
    done = call('POST', '/boards/' + board_id + '/candidates/' + good_candidate['id'] + '/extract',
                {'document_id': target['id'], 'ocr': False})
    assert next(c for c in done['candidates'] if c['id'] == good_candidate['id'])['status'] == 'extracted'


def test_08_revision_conflict_and_deletion_guards(work):
    app, client, call, document, page_id, target, board_id, directory = work
    board = reset_board(call, client, page_id, board_id)
    candidate = board['candidates'][0]
    conflict = client.patch('/api/boards/' + board_id + '/candidates/' + candidate['id'],
                            json={'revision': 9999, 'name': '过期修改'}, headers=HEADERS)
    assert conflict.status_code == 409 and conflict.json['code'] == 'revision_conflict'
    call('POST', '/boards/' + board_id + '/candidates/' + candidate['id'] + '/confirm', {})
    extracted = call('POST', '/boards/' + board_id + '/candidates/' + candidate['id'] + '/extract',
                     {'document_id': target['id'], 'ocr': False})
    result_page = next(c for c in extracted['candidates'] if c['id'] == candidate['id'])['result']['page_id']
    guarded = client.post('/api/pages/delete', json={'page_ids': [result_page]}, headers=HEADERS)
    assert guarded.status_code == 409
    board_refused = client.delete('/api/boards/' + board_id, headers=HEADERS)
    assert board_refused.status_code == 409


def test_09_persistence_and_integrity_across_restart(work):
    app, client, call, document, page_id, target, board_id, directory = work
    board = reset_board(call, client, page_id, board_id)
    candidate = board['candidates'][1]
    call('POST', '/boards/' + board_id + '/candidates/' + candidate['id'] + '/confirm', {})
    call('POST', '/boards/' + board_id + '/candidates/' + candidate['id'] + '/extract',
         {'document_id': target['id'], 'ocr': False})
    from paperflow.core import Store
    reopened = Store(directory).snapshot()
    stored = reopened['boards'][board_id]
    assert stored['page_id'] == page_id
    assert any(c['status'] == 'extracted' for c in stored['candidates'])
    extracted_page = next(c for c in stored['candidates'] if c['status'] == 'extracted')['result']['page_id']
    assert reopened['pages'][extracted_page]['board_candidate'] == {
        'board_id': board_id, 'candidate_id': candidate['id']}
    integrity = call('GET', '/audit/integrity?deep=1')
    assert integrity['valid']


def test_10_demo_route_creates_ready_board():
    import tempfile
    app = create_app(tempfile.mkdtemp(), demo=False)
    client = app.test_client()

    def call(method, path, values=None):
        response = client.open('/api' + path, method=method, json=values, headers=HEADERS)
        assert response.status_code < 400, (path, response.status_code, response.json)
        return response.json

    collection = call('POST', '/collections', {'name': '演示集合'})
    document = call('POST', '/documents', {'collection_id': collection['id'], 'name': '演示票据'})
    result = call('POST', '/demo', {'document_id': document['id'], 'sample': 'board'})
    assert len(result['board']['candidates']) == 4
    assert result['page_ids']
    app.extensions['scheduler'].shutdown()
