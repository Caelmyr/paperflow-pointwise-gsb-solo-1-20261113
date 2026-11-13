"""物理尺寸依据的回归测试。

覆盖：导入密度识别、导出纸幅与输出分辨率解耦、无依据照片拒绝导出、
人工确认、各类几何操作对依据的不同影响、区域图片、混合尺寸文档、
文字层定位、归档往返与历史版本不互相借用依据。
"""
import io
import sys
import zipfile
from pathlib import Path

import fitz
import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from paperflow.web import create_app
from paperflow import measurement

HEADERS = {'X-PaperFlow': '1'}
PRACTICE = ROOT / 'demo' / 'practice' / 'physical_size'


@pytest.fixture(scope='module')
def client(tmp_path_factory):
    app = create_app(tmp_path_factory.mktemp('paperflow-measurement'), demo=False)
    test_client = app.test_client()

    def call(method, path, values=None):
        response = test_client.open('/api' + path, method=method, json=values, headers=HEADERS)
        assert response.status_code < 400, (path, response.status_code, response.json)
        return response.json

    collection = call('POST', '/collections', {'name': '尺寸集合'})
    yield app, test_client, call, collection
    app.extensions['scheduler'].shutdown()
    app.extensions['pipelines'].executor.shutdown(wait=True)


def upload(call, test_client, document_id, path):
    with open(path, 'rb') as handle:
        response = test_client.post(
            '/api/documents/' + document_id + '/import',
            data={'files': (io.BytesIO(handle.read()), Path(path).name)}, headers=HEADERS)
    assert response.status_code == 201, response.json
    return response.json


def new_document(call, collection_id, name):
    return call('POST', '/documents', {'collection_id': collection_id, 'name': name})


def test_source_dpi_imports_produce_distinct_physical_sizes(client):
    app, test_client, call, collection = client
    document = new_document(call, collection['id'], '双密度扫描')
    upload(call, test_client, document['id'], PRACTICE / '300dpi.png')
    upload(call, test_client, document['id'], PRACTICE / '150dpi.png')
    pages = call('GET', '/documents/' + document['id'])['pages']
    dense, sparse = [call('GET', '/pages/' + page['id'] + '/measurement') for page in pages]
    # 同为 600x900 像素，但物理尺寸由各自密度决定，恰好相差一倍。
    assert dense['basis'] == 'source_dpi' and sparse['basis'] == 'source_dpi'
    assert abs(dense['physical_width_mm'] - 50.8) < 0.2
    assert abs(sparse['physical_width_mm'] - 101.6) < 0.2
    assert abs(sparse['physical_width_mm'] - 2 * dense['physical_width_mm']) < 0.2


def test_export_paper_size_is_constant_across_output_dpi(client):
    app, test_client, call, collection = client
    document = new_document(call, collection['id'], '导出分辨率解耦')
    upload(call, test_client, document['id'], PRACTICE / '300dpi.png')
    upload(call, test_client, document['id'], PRACTICE / '150dpi.png')
    rects = {}
    embedded = {}
    for dpi in (72, 150, 300, 600):
        record = call('POST', '/documents/' + document['id'] + '/export',
                      {'kind': 'pdf', 'options': {'dpi': dpi}})
        data = test_client.get('/api/exports/' + record['id'] + '/download').data
        pdf = fitz.open(stream=data, filetype='pdf')
        rects[dpi] = [(round(page.rect.width, 2), round(page.rect.height, 2)) for page in pdf]
        xref = pdf[0].get_images()[0][0]
        embedded[dpi] = (pdf.extract_image(xref)['width'], pdf.extract_image(xref)['height'])
        pdf.close()
    # 换分辨率只改嵌入图像采样，纸张物理尺寸保持不变。
    assert rects[72] == rects[150] == rects[300] == rects[600]
    assert rects[150][0] == (144.0, 216.0)
    assert rects[150][1] == (287.98, 431.96)
    # 嵌入像素确实随输出 DPI 改变。
    assert embedded[300][0] > embedded[150][0]
    record = call('POST', '/documents/' + document['id'] + '/export',
                  {'kind': 'pdf', 'options': {'dpi': 200}})
    verification = call('GET', '/exports/' + record['id'] + '/verify')
    assert verification['valid']
    for detail in verification['details']['pages']:
        assert abs(detail['width_points'] - detail['expected_width_points']) < 0.6


def test_photo_without_density_remains_unconfirmed_and_blocks_pdf(client):
    app, test_client, call, collection = client
    document = new_document(call, collection['id'], '无密度照片')
    upload(call, test_client, document['id'], ROOT / 'demo' / 'demo_photo.png')
    page = call('GET', '/documents/' + document['id'])['pages'][0]
    basis = call('GET', '/pages/' + page['id'] + '/measurement')
    assert basis['status'] == 'unconfirmed' and basis['basis'] is None
    response = test_client.post('/api/documents/' + document['id'] + '/export',
                                json={'kind': 'pdf', 'options': {'dpi': 150}}, headers=HEADERS)
    assert response.status_code == 409 and response.json['code'] == 'measurement_unconfirmed'
    preflight = call('GET', '/documents/' + document['id'] + '/preflight?kind=pdf')
    assert not preflight['ready']
    codes = [issue['code'] for item in preflight['pages'] for issue in item['issues']]
    assert 'measurement_unconfirmed' in codes
    # 非 PDF 导出不依赖物理依据，仍然允许。
    archived = call('POST', '/documents/' + document['id'] + '/export', {'kind': 'zip'})
    assert test_client.get('/api/exports/' + archived['id'] + '/download').status_code == 200


def test_manual_confirmation_by_edge_and_paper(client):
    app, test_client, call, collection = client
    document = new_document(call, collection['id'], '人工确认')
    upload(call, test_client, document['id'], ROOT / 'demo' / 'demo_photo.png')
    page_id = call('GET', '/documents/' + document['id'])['pages'][0]['id']
    page = call('GET', '/pages/' + page_id)
    call('POST', '/pages/' + page_id + '/measurement',
         {'basis': 'manual_edge', 'edge': 'top', 'length_mm': 210.0})
    edge_basis = call('GET', '/pages/' + page_id + '/measurement')
    assert edge_basis['basis'] == 'manual_edge'
    assert abs(edge_basis['physical_width_mm'] - 210.0) < 0.05
    expected_height = page['height'] / page['width'] * 210
    assert abs(edge_basis['physical_height_mm'] - expected_height) < 0.05
    call('POST', '/pages/' + page_id + '/measurement',
         {'basis': 'manual_paper', 'physical_width_mm': 210, 'physical_height_mm': 297, 'paper': 'A4'})
    paper_basis = call('GET', '/pages/' + page_id + '/measurement')
    assert paper_basis['basis'] == 'manual_paper'
    assert (paper_basis['physical_width_mm'], paper_basis['physical_height_mm']) == (210.0, 297.0)
    # 确认后 PDF 可以导出，纸幅即 A4 点尺寸。
    record = call('POST', '/documents/' + document['id'] + '/export',
                  {'kind': 'pdf', 'options': {'dpi': 150}})
    pdf = fitz.open(stream=test_client.get('/api/exports/' + record['id'] + '/download').data,
                    filetype='pdf')
    assert abs(pdf[0].rect.width - 210 * 72 / 25.4) < 0.6
    assert abs(pdf[0].rect.height - 297 * 72 / 25.4) < 0.6
    pdf.close()


def test_geometry_operations_have_distinct_basis_effects(client):
    app, test_client, call, collection = client
    document = new_document(call, collection['id'], '操作影响')
    upload(call, test_client, document['id'], PRACTICE / '300dpi.png')
    page_id = call('GET', '/documents/' + document['id'])['pages'][0]['id']

    def basis():
        return call('GET', '/pages/' + page_id + '/measurement')

    original = basis()
    # 旋转交换宽高，依据保持。
    call('POST', '/pages/' + page_id + '/process', {'operation': 'rotate', 'params': {'angle': 90}})
    rotated = basis()
    assert rotated['status'] == 'confirmed'
    assert abs(rotated['physical_width_mm'] - original['physical_height_mm']) < 0.05
    # 非几何操作完全不改变依据。
    call('POST', '/pages/' + page_id + '/process',
         {'operation': 'illumination', 'params': {'strength': 0.5}})
    assert basis() == rotated
    # 重新采样保持物理尺寸，密度按比例变化。
    call('POST', '/pages/' + page_id + '/process', {'operation': 'resize', 'params': {'scale': 0.5}})
    resampled = basis()
    assert abs(resampled['physical_width_mm'] - rotated['physical_width_mm']) < 0.05
    assert abs(resampled['source_dpi'] - rotated['source_dpi'] * 0.5) < 0.5
    # 裁切保持密度，物理尺寸随像素缩小。
    page = call('GET', '/pages/' + page_id)
    region = [20, 20, max(40, page['width'] // 3), max(40, page['height'] // 3)]
    call('POST', '/pages/' + page_id + '/process', {'operation': 'crop', 'params': {'region': region}})
    cropped = basis()
    assert abs(cropped['source_dpi'] - resampled['source_dpi']) < 0.5
    assert abs(cropped['physical_width_mm'] - region[2] * 25.4 / resampled['source_dpi']) < 0.1
    # 自动纠偏使依据失效，交回用户确认。
    call('POST', '/pages/' + page_id + '/process', {'operation': 'deskew', 'params': {}})
    invalid = basis()
    assert invalid['status'] == 'unconfirmed' and invalid['basis'] is None
    assert any(entry['action'] == 'invalidate'
               for entry in call('GET', '/pages/' + page_id)['measurement']['history'])


def test_perspective_invalidates_basis_until_reconfirmed(client):
    app, test_client, call, collection = client
    document = new_document(call, collection['id'], '透视失效')
    upload(call, test_client, document['id'], ROOT / 'demo' / 'demo_photo.png')
    page_id = call('GET', '/documents/' + document['id'])['pages'][0]['id']
    edges = call('GET', '/pages/' + page_id + '/edges')
    assert edges['detected']
    call('POST', '/pages/' + page_id + '/process',
         {'operation': 'perspective', 'params': {'points': edges['points']}})
    assert basis_status(call, page_id) == 'unconfirmed'
    response = test_client.post('/api/documents/' + document['id'] + '/export',
                                json={'kind': 'pdf'}, headers=HEADERS)
    assert response.status_code == 409
    call('POST', '/pages/' + page_id + '/measurement',
         {'basis': 'manual_paper', 'physical_width_mm': 200, 'physical_height_mm': 280})
    assert basis_status(call, page_id) == 'confirmed'


def test_restore_uses_the_restored_versions_own_basis(client):
    app, test_client, call, collection = client
    document = new_document(call, collection['id'], '版本依据隔离')
    upload(call, test_client, document['id'], PRACTICE / '300dpi.png')
    page_id = call('GET', '/documents/' + document['id'])['pages'][0]['id']
    call('POST', '/pages/' + page_id + '/process', {'operation': 'rotate', 'params': {'angle': 90}})
    call('POST', '/pages/' + page_id + '/process', {'operation': 'deskew', 'params': {}})
    assert basis_status(call, page_id) == 'unconfirmed'
    # 整体恢复原图：使用导入时记录的原始依据。
    call('POST', '/pages/' + page_id + '/restore', {})
    restored = call('GET', '/pages/' + page_id + '/measurement')
    assert restored['basis'] == 'source_dpi'
    assert abs(restored['source_dpi'] - 300) < 1
    # 恢复到某个历史版本：采用该版本快照里的依据，而非当前或其他版本。
    call('POST', '/pages/' + page_id + '/process', {'operation': 'rotate', 'params': {'angle': 90}})
    history = call('GET', '/pages/' + page_id)['history']
    target = next(entry for entry in history if entry['operation'] == 'rotate')
    call('POST', '/pages/' + page_id + '/restore', {'history_id': target['id']})
    version_basis = call('GET', '/pages/' + page_id + '/measurement')
    assert version_basis['status'] == 'confirmed'
    assert abs(version_basis['physical_width_mm'] - 76.2) < 0.2


def test_mixed_size_document_uses_each_pages_own_basis(client):
    app, test_client, call, collection = client
    document = new_document(call, collection['id'], '混合尺寸')
    upload(call, test_client, document['id'], ROOT / 'demo' / 'demo_document.pdf')
    upload(call, test_client, document['id'], PRACTICE / '150dpi.png')
    record = call('POST', '/documents/' + document['id'] + '/export',
                  {'kind': 'pdf', 'options': {'dpi': 200}})
    pdf = fitz.open(stream=test_client.get('/api/exports/' + record['id'] + '/download').data,
                    filetype='pdf')
    assert (round(pdf[0].rect.width, 2), round(pdf[0].rect.height, 2)) == (672.0, 912.0)
    assert abs(pdf[-1].rect.width - 287.98) < 0.7
    pdf.close()


def test_searchable_text_layer_tracks_resampled_image(client):
    app, test_client, call, collection = client
    document = new_document(call, collection['id'], '文字层定位')
    upload(call, test_client, document['id'], PRACTICE / '300dpi.png')
    page_id = call('GET', '/documents/' + document['id'])['pages'][0]['id']
    call('POST', '/pages/' + page_id + '/ocr', {'language': 'eng', 'psm': 6})
    page = call('GET', '/pages/' + page_id)
    word = page['ocr']['boxes'][0]
    call('PATCH', '/pages/' + page_id + '/ocr', {'boxes': [{'id': word['id'], 'text': 'MARKER'}]})
    # 将文字框放到已知像素坐标：x=50/600 宽，应映射到纸幅 144pt 的 12pt 处。
    with app.extensions['store'].transaction() as state:
        for box in state['pages'][page_id]['ocr']['boxes']:
            if box['text'] == 'MARKER':
                box.update({'x': 50, 'y': 60, 'width': 200, 'height': 40})
    positions = {}
    for dpi in (72, 300, 600):
        record = call('POST', '/documents/' + document['id'] + '/export',
                      {'kind': 'searchable_pdf', 'options': {'dpi': dpi}})
        pdf = fitz.open(stream=test_client.get('/api/exports/' + record['id'] + '/download').data,
                        filetype='pdf')
        assert pdf[0].search_for('MARKER')
        # 文字层位置只取决于纸幅与当前图像像素，与输出重采样分辨率完全无关。
        positions[dpi] = pdf[0].get_text('words')[0][:4]
        assert abs(positions[dpi][0] - 12.0) < 0.6
        pdf.close()
    assert positions[72] == positions[300] == positions[600]
    # 重新采样使 OCR 过期后，可搜索 PDF 必须被拒绝，避免文字层与新像素错位。
    call('POST', '/pages/' + page_id + '/process', {'operation': 'resize', 'params': {'scale': 0.5}})
    response = test_client.post('/api/documents/' + document['id'] + '/export',
                                json={'kind': 'searchable_pdf'}, headers=HEADERS)
    assert response.status_code == 409


def test_region_images_inherit_page_basis_and_embed_dpi(client):
    app, test_client, call, collection = client
    document = new_document(call, collection['id'], '区域依据')
    upload(call, test_client, document['id'], PRACTICE / '300dpi.png')
    page_id = call('GET', '/documents/' + document['id'])['pages'][0]['id']
    region = call('POST', '/pages/' + page_id + '/regions',
                  {'name': '插图', 'kind': 'illustration', 'region': [100, 100, 300, 200]})
    assert region['measurement']['status'] == 'confirmed'
    response = test_client.get('/api/pages/' + page_id + '/regions/' + region['id'])
    image = Image.open(io.BytesIO(response.data))
    assert image.size == (300, 200)
    assert image.info['dpi'] and abs(image.info['dpi'][0] - 300) < 1

    photo_doc = new_document(call, collection['id'], '无依据区域')
    upload(call, test_client, photo_doc['id'], ROOT / 'demo' / 'demo_photo.png')
    photo_id = call('GET', '/documents/' + photo_doc['id'])['pages'][0]['id']
    photo_region = call('POST', '/pages/' + photo_id + '/regions',
                        {'name': 'x', 'kind': 'other', 'region': [100, 100, 300, 200]})
    assert photo_region['measurement']['status'] == 'unconfirmed'
    response = test_client.get('/api/pages/' + photo_id + '/regions/' + photo_region['id'])
    assert Image.open(io.BytesIO(response.data)).info.get('dpi') is None


def test_archive_round_trip_preserves_each_measurement(client):
    app, test_client, call, collection = client
    document = new_document(call, collection['id'], '归档往返')
    upload(call, test_client, document['id'], PRACTICE / '300dpi.png')
    upload(call, test_client, document['id'], PRACTICE / '150dpi.png')
    pages = call('GET', '/documents/' + document['id'])['pages']
    original = [call('GET', '/pages/' + page['id'] + '/measurement') for page in pages]
    archive = call('POST', '/documents/' + document['id'] + '/export',
                   {'kind': 'zip', 'options': {'include_originals': True}})
    path = ROOT / 'data' / ('test-archive-' + archive['id'] + '.zip')
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(test_client.get('/api/exports/' + archive['id'] + '/download').data)
    imported = app.extensions['archives'].import_document(path, collection['id'], '归档往返文档')
    for page, basis in zip(imported['pages'], original):
        carried = page['measurement']
        assert carried['status'] == 'confirmed'
        assert carried['basis'] == basis['basis']
        assert carried['physical_width_mm'] == basis['physical_width_mm']
        assert carried['source_dpi'] == basis['source_dpi']
        assert page['original_measurement']['status'] == 'confirmed'
    path.unlink(missing_ok=True)


def test_image_archive_embeds_effective_density(client):
    app, test_client, call, collection = client
    document = new_document(call, collection['id'], '图片包密度')
    upload(call, test_client, document['id'], PRACTICE / '300dpi.png')
    record = call('POST', '/documents/' + document['id'] + '/export',
                  {'kind': 'images', 'options': {'image_format': 'png'}})
    archive = zipfile.ZipFile(io.BytesIO(
        test_client.get('/api/exports/' + record['id'] + '/download').data))
    with archive.open(archive.namelist()[0]) as handle:
        image = Image.open(io.BytesIO(handle.read()))
        assert image.info['dpi'] and abs(image.info['dpi'][0] - 300) < 1


def basis_status(call, page_id):
    return call('GET', '/pages/' + page_id + '/measurement')['status']
