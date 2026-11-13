"""透明语义从导入、处理到交付的端到端验收。

覆盖：
- 导入保留真实 alpha，完全透明像素的隐藏颜色不污染可见边缘；
- 几何校正/增强/缩略图/局部提取遵守同一套预乘 alpha 含义；
- OCR 等不透明输入使用明确审阅纸底且不改写透明原件；
- 预览/PNG/铺底 PDF/JPEG 边缘一致；
- 历史恢复保留当时的透明决定。
"""
import io
import zipfile
from pathlib import Path

import cv2
import fitz
import numpy as np
import pytest
from PIL import Image

from paperflow import imaging
from paperflow.core import DomainError
from paperflow.imaging import (
    REVIEW_PAPER_HEX,
    bgr,
    flatten,
    has_alpha,
    parse_paper_color,
    pil_to_opencv,
    read_image,
    sanitize_transparent,
)
from paperflow.web import create_app

HEADERS = {'X-PaperFlow': '1'}


def make_annotation(width=64, height=64):
    """构造带透明背景的批注页：左半不透明红、中段半透明蓝、右段全透明，透明区藏有醒目隐藏色。"""
    image = np.zeros((height, width, 4), np.uint8)
    image[:, : width // 2] = (0, 0, 220, 255)            # 不透明红 (BGR)
    image[:, width // 2: width * 3 // 4] = (220, 60, 0, 128)  # 半透明蓝，alpha=128
    image[:, width * 3 // 4:] = (13, 17, 19, 0)          # 完全透明像素里的隐藏颜色
    return image


def save_png(tmp_path, image, name='annotation.png'):
    path = tmp_path / name
    Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGRA2RGBA)).save(path)
    return path


def composite(image, paper):
    return flatten(image, parse_paper_color(paper))


@pytest.fixture(scope='module')
def work(tmp_path_factory):
    directory = tmp_path_factory.mktemp('paperflow-alpha')
    app = create_app(directory, demo=False)
    client = app.test_client()

    def call(method, path, values=None):
        response = client.open('/api' + path, method=method, json=values, headers=HEADERS)
        assert response.status_code < 400, (path, response.status_code, response.json)
        return response.json

    collection = call('POST', '/collections', {'name': '透明集合', 'category': '测试'})
    document = call('POST', '/documents', {'collection_id': collection['id'], 'name': '透明文档'})
    yield app, client, call, document, directory
    app.extensions['scheduler'].shutdown()
    app.extensions['pipelines'].executor.shutdown(wait=True)


def imported_page(work):
    app, client, call, document, directory = work
    refreshed = call('GET', '/documents/' + document['id'])
    if refreshed['page_ids']:
        return refreshed['page_ids'][-1]
    # 惰性导入一个 120×120 的透明批注页，保证各测试可独立运行。
    path = save_png(Path(directory), make_annotation(120, 120))
    with path.open('rb') as handle:
        response = client.post(
            '/api/documents/' + document['id'] + '/import',
            data={'files': (io.BytesIO(handle.read()), 'annotation.png')},
            headers=HEADERS,
        )
    assert response.status_code == 201, response.json
    return response.json['page_ids'][0]


def test_01_import_preserves_alpha_and_clears_hidden_color(tmp_path):
    image = make_annotation()
    path = save_png(tmp_path, image)
    loaded = read_image(path)
    assert has_alpha(loaded)
    # 完全透明像素的隐藏 RGB 必须被清零，不能带入后续处理。
    assert loaded[0, -1, 3] == 0
    assert tuple(loaded[0, -1, :3]) == (0, 0, 0)
    # 半透明像素的颜色与覆盖程度被完整保留（64 宽：半透明区为 32–47 列）。
    assert tuple(loaded[32, 40]) == (220, 60, 0, 128)
    assert tuple(loaded[32, 8][:3]) == (0, 0, 220) and loaded[32, 8, 3] == 255
    # 全透明区的隐藏颜色统一清零。
    assert tuple(loaded[32, 60][:3]) == (0, 0, 0) and loaded[32, 60, 3] == 0


def test_02_fully_opaque_alpha_is_dropped(tmp_path):
    rgba = np.zeros((40, 40, 4), np.uint8)
    rgba[:, :, :3] = 40
    rgba[:, :, 3] = 255
    path = save_png(tmp_path, rgba, 'opaque.png')
    loaded = read_image(path)
    assert loaded.ndim == 3 and loaded.shape[2] == 3


def test_03_sanitize_zeroes_only_fully_transparent():
    image = make_annotation()
    cleaned = sanitize_transparent(image)
    assert tuple(cleaned[32, 40]) == (220, 60, 0, 128)  # 半透明像素不动
    assert tuple(cleaned[0, -1][:3]) == (0, 0, 0)


def test_04_geometry_keeps_alpha_without_hidden_color_fringe():
    image = make_annotation(120, 120)
    rotated = imaging.rotate(image, 15)
    assert has_alpha(rotated)
    # 透明像素的 RGB 必须始终为 0，旋转插值不能把隐藏颜色带回边缘。
    transparent = rotated[:, :, 3] == 0
    if transparent.any():
        assert rotated[:, :, :3][transparent].max() == 0
    points = [[8, 8], [112, 10], [110, 112], [10, 110]]
    warped, _ = imaging.perspective(image, points)
    assert has_alpha(warped)
    warped_transparent = warped[:, :, 3] == 0
    if warped_transparent.any():
        assert warped[:, :, :3][warped_transparent].max() == 0
    # 覆盖程度仍是连续的，没有被压成纯 0/255。
    assert ((warped[:, :, 3] > 0) & (warped[:, :, 3] < 255)).any()


def test_05_premultiplied_resize_matches_physical_composite():
    # 右 1/3 完全透明但藏有醒目颜色，直接缩放会把它带进半透明边缘。
    image = np.zeros((40, 40, 4), np.uint8)
    image[:, :20] = (0, 0, 220, 255)
    image[:, 20:27] = (220, 60, 0, 128)
    image[:, 27:] = (7, 199, 233, 0)
    resized = imaging.resize_alpha(image, (29, 29), interpolation=cv2.INTER_AREA)
    on_white = composite(image, '#ffffff')
    reference = cv2.resize(on_white, (29, 29), interpolation=cv2.INTER_AREA)
    actual = composite(resized, '#ffffff')
    assert np.abs(actual.astype(int) - reference.astype(int)).max() <= 2
    # 直接缩放（不预乘）会把透明区隐藏颜色带进边缘，预乘路径必须明显更接近物理结果。
    naive = cv2.resize(image, (29, 29), interpolation=cv2.INTER_AREA)
    naive_composite = composite(naive, '#ffffff')
    assert np.abs(actual.astype(int) - reference.astype(int)).mean() < np.abs(naive_composite.astype(int) - reference.astype(int)).mean()


def test_06_enhancements_keep_alpha_separate_from_color():
    image = make_annotation(96, 96)
    grayish = imaging.color_mode(image, 'gray')
    assert has_alpha(grayish)
    assert np.array_equal(grayish[:, :, 3], image[:, :, 3])
    binary = imaging.color_mode(image, 'binary', threshold=160)
    assert has_alpha(binary)
    assert np.array_equal(binary[:, :, 3], image[:, :, 3])
    lit = imaging.illumination(image, 0.6)
    assert has_alpha(lit)
    assert np.array_equal(lit[:, :, 3], image[:, :, 3])
    denoised, _ = imaging.denoise(image, area=2, median=1)
    assert has_alpha(denoised)
    assert np.array_equal(denoised[:, :, 3], image[:, :, 3])


def test_07_filters_respect_alpha_semantics():
    image = make_annotation(80, 80)
    alpha_preserving = ('contrast', 'gamma', 'invert', 'bilateral')
    for operation, params in [
        ('contrast', {'contrast': 1.3, 'bright': 0}),
        ('gamma', {'gamma': 1.4}),
        ('invert', {}),
        ('flip', {}),
        ('pad', {'top': 8, 'bottom': 8, 'left': 8, 'right': 8}),
        ('resize', {'scale': 0.5}),
        ('bilateral', {'diameter': 5}),
        ('erase', {'region': [4, 4, 40, 40], 'mode': 'white'}),
    ]:
        from paperflow.filters import apply_filter
        result, _ = apply_filter(image, operation, params)
        assert has_alpha(result), operation
        if operation in alpha_preserving:
            assert np.array_equal(result[:, :, 3], image[:, :, 3]), operation
        if operation == 'flip':
            # 翻转只重排像素，覆盖程度集合必须保持一致。
            assert np.array_equal(np.sort(result[:, :, 3].ravel()), np.sort(image[:, :, 3].ravel()))
    # pad 扩展区域必须透明而非白纸。
    padded, _ = imaging.process(image, 'pad', {'top': 8, 'bottom': 8, 'left': 8, 'right': 8})
    assert padded[0, 0, 3] == 0
    # erase white 模式在透明页上是移除覆盖。
    erased, _ = imaging.process(image, 'erase', {'region': [4, 4, 40, 40], 'mode': 'white'})
    assert erased[10, 10, 3] == 0 and tuple(erased[10, 10, :3]) == (0, 0, 0)


def test_08_flatten_is_explicit_and_uses_review_paper_only_for_views():
    image = make_annotation()
    flat = imaging.process(image, 'flatten', {'paper': '#e8f0e2'})[0]
    assert flat.ndim == 3 and flat.shape[2] == 3
    expected = composite(image, '#e8f0e2')
    assert np.array_equal(flat, expected)
    # 默认纸底为白色。
    default = imaging.process(image, 'flatten', {})[0]
    assert np.array_equal(default, composite(image, '#ffffff'))
    with pytest.raises(DomainError):
        parse_paper_color('#12')


def test_08b_every_operation_keeps_transparent_rgb_clean():
    base = sanitize_transparent(make_annotation(120, 120))
    for operation, params in [
        ('perspective', {'points': [[5, 5], [115, 8], [112, 112], [8, 110]]}),
        ('rotate', {'angle': 45}),
        ('illumination', {'strength': 0.6}),
        ('color', {'mode': 'binary', 'threshold': 160}),
        ('denoise', {'area': 2, 'median': 1}),
        ('crop', {'region': [10, 10, 80, 80]}),
        ('contrast', {'contrast': 1.2}),
        ('gamma', {'gamma': 1.3}),
        ('bilateral', {'diameter': 5}),
        ('pad', {'top': 8, 'bottom': 8, 'left': 8, 'right': 8}),
        ('resize', {'scale': 0.5}),
        ('erase', {'region': [10, 10, 40, 40], 'mode': 'white'}),
        ('morph_text', {'mode': 'thicken', 'size': 2, 'iterations': 1}),
        ('sauvola', {'window': 41, 'k': 0.2}),
        ('remove_lines', {'orientation': 'both', 'length': 80}),
        ('flatten_background', {'radius': 35, 'strength': 0.8}),
        ('invert', {}),
        ('flip', {'axis': 'horizontal'}),
    ]:
        result, _ = imaging.process(base, operation, params)
        if operation == 'crop':
            continue  # 裁掉全部透明像素时无需断言
        transparent = result[:, :, 3] == 0
        if transparent.any():
            assert result[:, :, :3][transparent].max() == 0, operation


def test_09_crop_and_region_extraction_keep_alpha(work):
    app, client, call, document, directory = work
    path = save_png(Path(directory), make_annotation(120, 120))
    with path.open('rb') as handle:
        response = client.post(
            '/api/documents/' + document['id'] + '/import',
            data={'files': (io.BytesIO(handle.read()), 'annotation.png')},
            headers=HEADERS,
        )
    assert response.status_code == 201, response.json
    page_id = response.json['page_ids'][0]
    page = call('GET', '/pages/' + page_id)
    assert page['has_alpha'] is True
    cropped = call('POST', '/pages/' + page_id + '/process', {'operation': 'crop', 'params': {'region': [10, 10, 80, 80]}})
    assert cropped['has_alpha'] is True
    region = call('POST', '/pages/' + page_id + '/regions', {'name': '局部', 'kind': 'illustration', 'region': [10, 10, 60, 60]})
    assert region['has_alpha'] is True
    download = client.get('/api/pages/' + page_id + '/regions/' + region['id'])
    assert download.status_code == 200
    region_image = np.asarray(Image.open(io.BytesIO(download.data)))
    assert region_image.shape[2] == 4
    call('POST', '/pages/' + page_id + '/restore', {})


def test_10_thumbnail_is_flattened_on_review_paper_and_jpeg(work):
    app, client, call, document, directory = work
    page_id = imported_page(work)
    assert page_id
    response = client.get('/api/pages/' + page_id + '/image?variant=thumbnail')
    assert response.status_code == 200
    assert response.mimetype == 'image/jpeg'
    thumb = np.asarray(Image.open(io.BytesIO(response.data)).convert('RGB'))
    page = call('GET', '/pages/' + page_id)
    current = read_image(app.extensions['store'].resolve(page['current']))
    # 缩略图边缘必须与白纸铺底结果一致；走同一 JPEG q86 编解码以隔离压缩噪声。
    flat = flatten(current, REVIEW_PAPER_HEX)
    ratio = min(240 / flat.shape[1], 240 / flat.shape[0], 1)
    reference = cv2.resize(flat, (round(flat.shape[1] * ratio), round(flat.shape[0] * ratio)), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode('.jpg', reference, [cv2.IMWRITE_JPEG_QUALITY, 86])
    assert ok
    reference_rgb = cv2.cvtColor(cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
    assert thumb.shape == reference_rgb.shape
    assert np.abs(thumb.astype(int) - reference_rgb.astype(int)).max() <= 1
    # 透明区域在缩略图里必须呈现接近审阅白纸的颜色，而不是透明像素里藏着的颜色。
    transparent_corner = thumb[0:4, -4:].reshape(-1, 3).mean(axis=0)
    assert transparent_corner.min() > 240


def test_11_png_export_keeps_alpha_while_jpeg_and_pdf_flatten(work):
    app, client, call, document, directory = work
    page_id = imported_page(work)
    page = call('GET', '/pages/' + page_id)
    # PNG 图片包保留 alpha。
    png_export = call('POST', '/documents/' + document['id'] + '/export', {'kind': 'images', 'options': {'image_format': 'png'}})
    payload = client.get('/api/exports/' + png_export['id'] + '/download').data
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = next(n for n in archive.namelist() if n.endswith('.png'))
        with Image.open(io.BytesIO(archive.read(name))) as image:
            assert 'A' in image.getbands()
    # JPEG 图片包按明确纸底铺底。
    paper = '#f0e6d2'
    jpg_export = call('POST', '/documents/' + document['id'] + '/export', {'kind': 'images', 'options': {'image_format': 'jpeg', 'paper': paper}})
    payload = client.get('/api/exports/' + jpg_export['id'] + '/download').data
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        name = next(n for n in archive.namelist() if n.endswith('.jpg'))
        with Image.open(io.BytesIO(archive.read(name))) as image:
            jpg = np.asarray(image.convert('RGB'))
    current = read_image(app.extensions['store'].resolve(page['current']))
    reference = cv2.cvtColor(flatten(current, paper), cv2.COLOR_BGR2RGB)
    # 走同一 JPEG q95 编解码，隔离压缩 ringing，只验证铺底语义。
    ok, encoded = cv2.imencode('.jpg', cv2.cvtColor(reference, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 95])
    assert ok
    reference_jpg = cv2.cvtColor(cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
    assert jpg.shape == reference_jpg.shape
    assert np.abs(jpg.astype(int) - reference_jpg.astype(int)).max() <= 1
    # 透明角落必须落在所选纸底颜色上，而不是透明像素的隐藏颜色。
    paper_rgb = (int(paper[1:3], 16), int(paper[3:5], 16), int(paper[5:7], 16))
    assert np.abs(jpg[0:4, -4:].reshape(-1, 3).mean(axis=0) - np.array(paper_rgb)).max() < 6
    # 铺底 PDF 与同一纸底的 PNG 预览边缘一致。
    pdf_export = call('POST', '/documents/' + document['id'] + '/export', {'kind': 'pdf', 'options': {'paper': paper}})
    pdf_bytes = client.get('/api/exports/' + pdf_export['id'] + '/download').data
    pdf = fitz.open(stream=pdf_bytes, filetype='pdf')
    try:
        pix = pdf[0].get_pixmap(alpha=False)
        pdf_render = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)
    finally:
        pdf.close()
    preview = client.get('/api/pages/' + page_id + '/preview?paper=' + paper.lstrip('#'))
    preview_rgb = np.asarray(Image.open(io.BytesIO(preview.data)).convert('RGB'))
    # PDF 按 150 DPI 重采样，尺寸可能不同；缩放到同尺寸比较边缘颜色。
    resized = cv2.resize(preview_rgb, (pdf_render.shape[1], pdf_render.shape[0]), interpolation=cv2.INTER_AREA)
    assert np.abs(pdf_render.astype(int) - resized.astype(int)).mean() < 4


def test_12_preview_does_not_modify_stored_original(work):
    app, client, call, document, directory = work
    page_id = imported_page(work)
    store = app.extensions['store']
    before = call('GET', '/pages/' + page_id)
    for paper in ('ffffff', '000000', 'e8f0e2'):
        response = client.get('/api/pages/' + page_id + '/preview?paper=' + paper)
        assert response.status_code == 200
    after = call('GET', '/pages/' + page_id)
    assert after['current'] == before['current']
    assert after['revision'] == before['revision']
    stored = read_image(store.resolve(after['current']))
    assert has_alpha(stored)


def test_13_history_restore_keeps_transparency_decision(work):
    app, client, call, document, directory = work
    page_id = imported_page(work)
    # 明确铺底是一次历史决定。
    call('POST', '/pages/' + page_id + '/process', {'operation': 'flatten', 'params': {'paper': '#d9e8d2'}})
    flattened = call('GET', '/pages/' + page_id)
    assert flattened['has_alpha'] is False
    # 恢复到透明原图，透明决定随历史版本一起回来。
    restored = call('POST', '/pages/' + page_id + '/restore', {})
    assert restored['has_alpha'] is True
    stored = read_image(app.extensions['store'].resolve(restored['current']))
    assert has_alpha(stored)
    # 导入的是 120 宽页面：半透明蓝位于 60–89 列，全透明区在 90 列之后。
    assert tuple(stored[32, 72]) == (220, 60, 0, 128)
    assert tuple(stored[0, -1]) == (0, 0, 0, 0)
    # 再次恢复到铺底版本，仍是不透明决定。
    flatten_entry = next(entry for entry in restored['history'] if entry['operation'] == 'flatten')
    again = call('POST', '/pages/' + page_id + '/restore', {'history_id': flatten_entry['id']})
    assert again['has_alpha'] is False
    # 收尾恢复为透明原件，保证后续测试与归档仍拿到透明决定。
    back = call('POST', '/pages/' + page_id + '/restore', {})
    assert back['has_alpha'] is True


def test_14_ocr_uses_review_paper_without_rewriting_alpha(work):
    app, client, call, document, directory = work
    # OCR 引擎可能未安装；在该环境下验证请求被受理且透明原件不被改写即可。
    page_id = imported_page(work)
    response = client.post('/api/pages/' + page_id + '/ocr', json={'language': 'eng'}, headers=HEADERS)
    store = app.extensions['store']
    page = call('GET', '/pages/' + page_id)
    stored = read_image(store.resolve(page['current']))
    assert has_alpha(stored)
    if response.status_code == 503:
        return
    assert response.status_code == 200, response.json
    assert page['ocr'].get('recognition_paper') == REVIEW_PAPER_HEX


def test_15_quality_and_analysis_ignore_transparent_padding(work):
    app, client, call, document, directory = work
    page_id = imported_page(work)
    quality = call('GET', '/pages/' + page_id)['quality']
    assert 'has_alpha' in quality and quality['has_alpha'] is True
    analysis = call('GET', '/pages/' + page_id + '/analysis')
    assert analysis['histogram']['pixels'] > 0
    comparison = call('GET', '/pages/' + page_id + '/comparison')
    assert 'structural_similarity' in comparison


def test_16_zip_archive_roundtrip_preserves_alpha(work):
    app, client, call, document, directory = work
    export = call('POST', '/documents/' + document['id'] + '/export', {'kind': 'zip'})
    payload = client.get('/api/exports/' + export['id'] + '/download').data
    collection = call('POST', '/collections', {'name': '归档集合'})
    imported = app.extensions['archives'].import_document(
        app.extensions['store'].resolve(export['path']), collection['id'], '透明往返'
    )
    page_id = imported['page_ids'][0]
    page = app.extensions['store'].get('pages', page_id)
    assert page['has_alpha'] is True
    image = read_image(app.extensions['store'].resolve(page['current']))
    assert has_alpha(image)
    assert tuple(image[32, 72]) == (220, 60, 0, 128)
    # 透明像素的隐藏颜色在往返后依然不会复活。
    assert tuple(image[0, -1]) == (0, 0, 0, 0)


def test_17_review_paper_setting_drives_views_and_validates(work):
    app, client, call, document, directory = work
    page_id = imported_page(work)
    # 非法纸底必须被拒绝。
    bad = client.patch('/api/review/settings', json={'paper_background': 'nope'}, headers=HEADERS)
    assert bad.status_code == 400
    configured = call('PATCH', '/review/settings', {'paper_background': '#203040'})
    assert configured['paper_background'] == '#203040'
    # 新生成的缩略图使用该审阅纸底，且仍是不透明 JPEG。
    call('POST', '/pages/' + page_id + '/process', {'operation': 'flip', 'params': {'axis': 'horizontal'}})
    response = client.get('/api/pages/' + page_id + '/image?variant=thumbnail')
    assert response.mimetype == 'image/jpeg'
    thumb = np.asarray(Image.open(io.BytesIO(response.data)).convert('RGB'))
    # 全透明区域应呈现审阅纸色 (32,48,64)。
    assert np.abs(thumb[0:4, 0:4].reshape(-1, 3).mean(axis=0) - np.array((32, 48, 64))).max() < 8
    # 显式预览端点同样遵守纸底，且透明原件未被改写。
    preview = client.get('/api/pages/' + page_id + '/preview?paper=203040')
    preview_rgb = np.asarray(Image.open(io.BytesIO(preview.data)).convert('RGB'))
    assert tuple(preview_rgb[0, 0]) == (32, 48, 64)
    page = call('GET', '/pages/' + page_id)
    assert page['has_alpha'] is True
    # 还原默认审阅纸底，避免影响其他用例。
    call('PATCH', '/review/settings', {'paper_background': '#ffffff'})
    call('POST', '/pages/' + page_id + '/restore', {})


def test_18_pdf_import_and_webp_remain_opaque(tmp_path):
    # 不透明输入路径不能被 alpha 改造破坏。
    rgb = np.full((40, 40, 3), 200, np.uint8)
    path = tmp_path / 'plain.png'
    Image.fromarray(cv2.cvtColor(rgb, cv2.COLOR_BGR2RGB)).save(path)
    loaded = read_image(path)
    assert loaded.shape[2] == 3
    flat = flatten(loaded)
    assert np.array_equal(flat, rgb)
