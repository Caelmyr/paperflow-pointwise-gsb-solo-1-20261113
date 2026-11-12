import io
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperflow.web import create_app

HEADERS = {'X-PaperFlow': '1'}
FONT_PATH = ROOT / 'resources' / 'NotoSansCJK-Regular.ttc'


@pytest.fixture(scope='module')
def work(tmp_path_factory):
    directory = tmp_path_factory.mktemp('paperflow-cross')
    app = create_app(directory, demo=False)
    client = app.test_client()

    def call(method, path, values=None):
        response = client.open('/api' + path, method=method, json=values, headers=HEADERS)
        assert response.status_code < 400, (path, response.status_code, response.json)
        return response.json

    font = ImageFont.truetype(str(FONT_PATH), 30)

    def make_page(lines, size=(900, 1200)):
        image = Image.new('RGB', size, (255, 255, 255))
        draw = ImageDraw.Draw(image)
        for index, line in enumerate(lines):
            draw.text((80, 90 + index * 60), line, font=font, fill=(25, 25, 25))
        return cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)

    def shift(image, factor=0.8):
        return np.clip(image.astype(np.float32) * factor, 0, 255).astype(np.uint8)

    def translate(image, dx=14, dy=10):
        matrix = np.float32([[1, 0, dx], [0, 1, dy]])
        return cv2.warpAffine(image, matrix, (image.shape[1], image.shape[0]), borderValue=(255, 255, 255))

    collection = call('POST', '/collections', {'name': '合同对比', 'category': '测试'})
    left_doc = call('POST', '/documents', {'collection_id': collection['id'], 'name': '合同首扫版'})
    right_doc = call('POST', '/documents', {'collection_id': collection['id'], 'name': '合同再扫版'})
    documents = app.extensions['documents']

    clause_1 = ['合同编号 HT-2026-0007', '甲方：示例科技有限公司', '乙方：测试服务有限公司',
                '第二条 合同金额', '总金额人民币壹拾万元整', '第三条 交付时间', '2026年12月31日前交付']
    clause_2 = ['第四条 保密义务', '双方对合作内容保密', '保密期限为两年', '违者赔偿全部损失']
    clause_3 = ['第五条 争议解决', '争议提交上海仲裁', '裁决为终局裁决']
    changed_1 = clause_1.copy()
    changed_1[4] = '总金额人民币贰拾万元整'
    attachment_a = ['附件A 报价清单', '扫描模块 2万 1套', 'OCR模块 3万 1套', '存储模块 5万 1套', '合计 10万']
    attachment_b = ['附件B 验收报告', '导入功能 通过', '校正功能 通过', '识别功能 通过', '复核结论 合格']

    left_pages = [
        documents.add_image(left_doc['id'], make_page(clause_1), '首扫-条款一'),
        documents.add_image(left_doc['id'], make_page(clause_2), '首扫-条款二'),
        documents.add_image(left_doc['id'], make_page(clause_3), '首扫-条款三'),
        documents.add_image(left_doc['id'], make_page(attachment_a), '首扫-附件A'),
    ]
    right_pages = [
        documents.add_image(right_doc['id'], make_page(changed_1), '再扫-条款一改金额'),
        documents.add_image(right_doc['id'], shift(translate(make_page(clause_2)), 0.82), '再扫-条款二位移变暗'),
        documents.add_image(right_doc['id'], shift(translate(make_page(clause_3), 10, 8), 0.9), '再扫-条款三位移变暗'),
        documents.add_image(right_doc['id'], make_page(attachment_b), '再扫-附件B'),
    ]
    yield {
        'app': app, 'client': client, 'call': call, 'directory': directory,
        'left_doc': left_doc, 'right_doc': right_doc,
        'left_pages': left_pages, 'right_pages': right_pages,
        'make_page': make_page, 'attachment_a': attachment_a, 'attachment_b': attachment_b,
    }
    app.extensions['scheduler'].shutdown()
    app.extensions['pipelines'].executor.shutdown(wait=True)


def test_01_auto_match_ignores_capture_shift_and_flags_real_change(work):
    call = work['call']
    project = call('POST', '/cross-comparisons', {
        'name': '合同两版对比',
        'left_document_id': work['left_doc']['id'],
        'right_document_id': work['right_doc']['id'],
    })
    detail = call('POST', '/cross-comparisons/' + project['id'] + '/auto-match', {})
    pairs = detail['pairs']
    # 三页条款应自动配上；两个不同附件不得自动配对
    assert len(pairs) == 3
    by_left = {pair['left_page_id']: pair for pair in pairs}
    moved_pair = by_left[work['left_pages'][1]['id']]
    assert moved_pair['visual']['classification'] == 'equivalent'
    assert moved_pair['visual']['ink_change_ratio'] == 0
    changed_pair = by_left[work['left_pages'][0]['id']]
    assert changed_pair['visual']['classification'] == 'content_changed'
    assert changed_pair['visual']['region_count'] >= 1
    assert changed_pair['visual']['ink_change_ratio'] > 0
    # 不同附件保持未配对，不能仅因版面相近自动结合
    assert [item['name'] for item in detail['left_unmatched']] == ['首扫-附件A']
    assert [item['name'] for item in detail['right_unmatched']] == ['再扫-附件B']
    work['project_id'] = project['id']


def test_02_manual_pairing_and_attachment_evidence(work):
    call = work['call']
    project_id = work['project_id']
    detail = call('GET', '/cross-comparisons/' + project_id)
    left_attachment = detail['left_unmatched'][0]['page_id']
    right_attachment = detail['right_unmatched'][0]['page_id']
    manual = call('POST', '/cross-comparisons/' + project_id + '/pairs', {
        'left_page_id': left_attachment,
        'right_page_id': right_attachment,
    })
    attachment_pair = manual['pairs'][-1]
    assert attachment_pair['match_origin'] == 'manual'
    assert attachment_pair['match_status'] == 'confirmed'
    # 人工可以强行配对，但证据必须显示两页内容不同，防止误当作同一附件
    assert attachment_pair['visual']['ink_change_ratio'] > 0.1
    # 同一配对不能重复使用页面
    conflict = work['client'].post(
        '/api/cross-comparisons/' + project_id + '/pairs',
        json={'left_page_id': left_attachment, 'right_page_id': right_attachment},
        headers=HEADERS,
    )
    assert conflict.status_code == 409


def test_03_decisions_for_unmatched_pages(work):
    call = work['call']
    project_id = work['project_id']
    detail = call('GET', '/cross-comparisons/' + project_id)
    # 解除附件配对后，分别标记重复与插入
    pair_id = detail['pairs'][-1]['id']
    detail = call('DELETE', '/cross-comparisons/' + project_id + '/pairs/' + pair_id)
    left_missing = detail['left_unmatched'][0]
    right_inserted = detail['right_unmatched'][0]
    detail = call('POST', '/cross-comparisons/' + project_id + '/decisions', {
        'page_id': left_missing['page_id'], 'side': 'left',
        'decision': 'missing', 'note': '再扫版缺少附件A',
    })
    detail = call('POST', '/cross-comparisons/' + project_id + '/decisions', {
        'page_id': right_inserted['page_id'], 'side': 'right',
        'decision': 'inserted', 'note': '再扫版新插入附件B',
    })
    decisions = {item['page_id']: item['decision'] for item in detail['decisions']}
    assert decisions[left_missing['page_id']] == 'missing'
    assert decisions[right_inserted['page_id']] == 'inserted'
    # 非法决定被拒绝
    bad = work['client'].post(
        '/api/cross-comparisons/' + project_id + '/decisions',
        json={'page_id': right_inserted['page_id'], 'side': 'right', 'decision': 'exploded'},
        headers=HEADERS,
    )
    assert bad.status_code == 400


def test_04_review_conclusion_bound_to_versions(work):
    call = work['call']
    project_id = work['project_id']
    detail = call('GET', '/cross-comparisons/' + project_id)
    changed_pair = next(pair for pair in detail['pairs']
                        if pair['left_page_id'] == work['left_pages'][0]['id'])
    result = call('POST', '/cross-comparisons/' + project_id + '/pairs/' + changed_pair['id'] + '/review', {
        'verdict': 'content_changed', 'note': '金额由十万改为二十万', 'reviewer': '法务审阅人',
    })
    pair = next(item for item in result['pairs'] if item['id'] == changed_pair['id'])
    assert pair['review']['verdict'] == 'content_changed'
    assert pair['review']['left_ref']['page_id'] == work['left_pages'][0]['id']
    assert pair['review']['right_ref']['revision'] == 0
    assert len(pair['review_history']) == 1

    # 差异图实时生成，不写入工作区文件
    image_response = work['client'].get(
        '/api/cross-comparisons/' + project_id + '/pairs/' + changed_pair['id'] + '/diff-image'
    )
    assert image_response.status_code == 200
    assert image_response.mimetype == 'image/png'
    assert len(image_response.data) > 1000


def test_05_revision_change_marks_conclusion_stale_and_keeps_evidence(work):
    call = work['call']
    project_id = work['project_id']
    documents = work['app'].extensions['documents']
    detail = call('GET', '/cross-comparisons/' + project_id)
    changed_pair = next(pair for pair in detail['pairs']
                        if pair['left_page_id'] == work['left_pages'][0]['id'])
    original_note = changed_pair['review']['note']

    # 左侧页面重新处理（模拟重扫），既有结论应被标记失效
    documents.apply(work['left_pages'][0]['id'], 'illumination', {'strength': 0.5})
    detail = call('GET', '/cross-comparisons/' + project_id)
    pair = next(item for item in detail['pairs'] if item['id'] == changed_pair['id'])
    assert pair['stale']['stale'] is True
    assert any(reason['reason'] == 'image_revised' for reason in pair['stale']['reasons'])
    # 原结论仍保留供核对
    assert pair['review']['note'] == original_note

    refreshed = call('POST', '/cross-comparisons/' + project_id + '/pairs/' + changed_pair['id'] + '/refresh', {})
    pair = next(item for item in refreshed['pairs'] if item['id'] == changed_pair['id'])
    assert pair['review'] is None
    assert len(pair['stale_reviews']) == 1
    assert pair['stale_reviews'][0]['note'] == original_note
    assert pair['baseline_refs']['left']['revision'] == 1

    # OCR 校订同样触发失效
    documents.recognize(work['left_pages'][0]['id'], {'language': 'chi_sim+eng'})
    documents.edit_ocr(work['left_pages'][0]['id'], {'text': '人工校订后的合同金额条款'})
    detail = call('GET', '/cross-comparisons/' + project_id)
    pair = next(item for item in detail['pairs'] if item['id'] == changed_pair['id'])
    assert any(reason['reason'] == 'ocr_revised' for reason in pair['stale']['reasons'])


def test_06_text_diff_distinguishes_similar_attachments(work):
    from paperflow.compare_text import compare_text
    result = compare_text('\n'.join(work['attachment_a']), '\n'.join(work['attachment_b']))
    assert result['available'] is True
    assert result['similarity'] < 0.4
    assert result['added_tokens'] >= 3 and result['removed_tokens'] >= 3
    # 相同文本两次识别仅有空白差异时应判高度相似
    same = compare_text('第一条 服务内容\n第二条 金额', '第一条 服务内容\n第二条 金额')
    assert same['similarity'] == 1.0 and same['change_ratio'] == 0.0


def test_07_project_persistence_and_listing(work):
    call = work['call']
    projects = call('GET', '/cross-comparisons')
    assert len(projects) == 1
    assert projects[0]['pair_count'] == 3
    from paperflow.core import Store
    reopened = Store(work['directory']).snapshot()
    stored = reopened['cross_comparisons'][work['project_id']]
    assert len(stored['pairs']) == 3
    assert stored['baselines']['left'][0]['page_id'] == work['left_pages'][0]['id']

    # 校验与审计仍通过
    integrity = call('GET', '/audit/integrity?deep=1')
    assert integrity['valid'], integrity['issues']
    deleted = call('DELETE', '/cross-comparisons/' + work['project_id'])
    assert deleted['deleted'] == work['project_id']
    assert call('GET', '/cross-comparisons') == []


def test_08_same_document_and_missing_page_rejected(work):
    client = work['client']
    response = client.post(
        '/api/cross-comparisons',
        json={'left_document_id': work['left_doc']['id'],
              'right_document_id': work['left_doc']['id']},
        headers=HEADERS,
    )
    assert response.status_code == 400
    missing = 'document_' + 'f' * 32
    response = client.post(
        '/api/cross-comparisons',
        json={'left_document_id': work['left_doc']['id'], 'right_document_id': missing},
        headers=HEADERS,
    )
    assert response.status_code == 404


def test_09_deleted_page_surfaces_as_missing_not_silent(work):
    call = work['call']
    documents = work['app'].extensions['documents']
    # 新建一个仅两页的小文档对，删除其中一页
    collection = call('POST', '/collections', {'name': '删除场景'})
    doc_a = call('POST', '/documents', {'collection_id': collection['id'], 'name': '删除甲'})
    doc_b = call('POST', '/documents', {'collection_id': collection['id'], 'name': '删除乙'})
    page_a1 = documents.add_image(doc_a['id'], work['make_page'](['删除场景第一页', '合同内容甲']), '删-甲1')
    page_a2 = documents.add_image(doc_a['id'], work['make_page'](['删除场景第二页']), '删-甲2')
    page_b1 = documents.add_image(doc_b['id'], work['make_page'](['删除场景第一页', '合同内容甲']), '删-乙1')
    project = call('POST', '/cross-comparisons', {
        'name': '删除场景对比', 'left_document_id': doc_a['id'], 'right_document_id': doc_b['id'],
    })
    detail = call('POST', '/cross-comparisons/' + project['id'] + '/auto-match', {})
    assert len(detail['pairs']) == 1
    # 左文档第二页未配对，先记决定再删除页面
    unmatched_id = detail['left_unmatched'][0]['page_id']
    call('POST', '/cross-comparisons/' + project['id'] + '/decisions', {
        'page_id': unmatched_id, 'side': 'left', 'decision': 'missing',
    })
    call('POST', '/pages/delete', {'page_ids': [page_a2['id']]})
    detail = call('GET', '/cross-comparisons/' + project['id'])
    deleted_rows = [row for row in detail['left_unmatched'] if not row['exists']]
    assert deleted_rows and deleted_rows[0]['page_id'] == page_a2['id']
    assert any(item['reason'] == 'page_deleted' for item in detail['stale']['decisions'])
    # 删除配对中的页面应在配对行明确标红，而不是报错
    call('POST', '/pages/delete', {'page_ids': [page_b1['id']]})
    detail = call('GET', '/cross-comparisons/' + project['id'])
    broken_pair = detail['pairs'][0]
    assert broken_pair['stale']['stale']
    assert any(reason['reason'] == 'page_deleted' for reason in broken_pair['stale']['reasons'])
    assert broken_pair['right_exists'] is False


def test_10_text_gate_blocks_similar_layout_attachments_with_ocr(work):
    call = work['call']
    documents = work['app'].extensions['documents']
    collection = call('POST', '/collections', {'name': '文字门控'})
    doc_a = call('POST', '/documents', {'collection_id': collection['id'], 'name': '表单甲'})
    doc_b = call('POST', '/documents', {'collection_id': collection['id'], 'name': '表单乙'})
    template = ['报表标题：{title}', '编号 FORM-2026-{n}', '第一项 {a}', '第二项 {b}', '第三项 {c}']
    text_a = [line.format(title='报价审批单', n='11', a='扫描仪两台', b='服务器一台', c='软件三套') for line in template]
    text_b = [line.format(title='入库验收单', n='22', a='打印机一台', b='交换机四台', c='线缆二十根') for line in template]
    page_a = documents.add_image(doc_a['id'], work['make_page'](text_a), '表单-A')
    page_b = documents.add_image(doc_b['id'], work['make_page'](text_b), '表单-B')
    # 直接写入 OCR 结果，模拟已识别页面
    documents.recognize(page_a['id'], {'language': 'chi_sim+eng'})
    documents.recognize(page_b['id'], {'language': 'chi_sim+eng'})
    project = call('POST', '/cross-comparisons', {
        'name': '同模板异单据', 'left_document_id': doc_a['id'], 'right_document_id': doc_b['id'],
    })
    detail = call('POST', '/cross-comparisons/' + project['id'] + '/auto-match', {})
    # 版面相近但文字不同，文字门控应阻止自动配对
    assert detail['pairs'] == []
    assert detail['left_unmatched_count'] == 1 and detail['right_unmatched_count'] == 1
    # 人工仍可配对，且文字证据显示低相似
    manual = call('POST', '/cross-comparisons/' + project['id'] + '/pairs', {
        'left_page_id': page_a['id'], 'right_page_id': page_b['id'],
    })
    pair = manual['pairs'][0]
    assert pair['match_status'] == 'confirmed'
    assert pair['scores']['text_similarity'] < 0.6
    assert pair['text']['added_tokens'] >= 3
