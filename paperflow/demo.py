from __future__ import annotations

import io
from pathlib import Path

import cv2
import fitz
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .core import ROOT
from .imaging import save_image

def find_font():
    local = ROOT / 'resources' / 'NotoSansCJK-Regular.ttc'
    if local.is_file():
        return local
    import subprocess
    import shutil
    if shutil.which('fc-match'):
        result = subprocess.run(['fc-match', '-f', '%{file}', 'sans:lang=zh-cn'], capture_output=True, text=True, timeout=10)
        candidate = Path(result.stdout.strip())
        if candidate.is_file():
            local.parent.mkdir(exist_ok=True)
            shutil.copyfile(candidate, local)
            return local
    return None


def build_demo():
    directory = ROOT / 'demo'
    directory.mkdir(exist_ok=True)
    font_path = find_font()
    outputs = []
    themes = [
        ('项目档案 / 数字化计划', 'PaperFlow Document Studio', [
            '纸质资料数字化工作台',
            '文档编号：PF-2026-001',
            '扫描、校正、识别与归档',
            '本地处理，原始文件完整保留。',
            '支持中文和英文文字识别。',
            'Hello PaperFlow 2026',
            'Searchable documents, organized ideas.',
            '会议日期：2026年11月12日',
            '负责人：林晓    状态：待归档',
        ]),
        ('资料整理 / 工作清单', 'From paper to possibility', [
            '文档整理验收记录',
            '第一步：导入文档图片或PDF',
            '第二步：校正纸张边缘和倾斜',
            '第三步：文字识别与人工校订',
            '第四步：导出可搜索PDF文档',
            'Archive checklist 2026',
            'Quality checked. Ready for review.',
            '关键词：数字化、档案、知识管理',
        ]),
    ]
    for index, (title, subtitle, lines) in enumerate(themes):
        output = directory / f'demo_{index + 1}.png'
        if not output.exists():
            canvas = Image.new('RGB', (1400, 1900), '#fffef9')
            draw = ImageDraw.Draw(canvas)
            if font_path:
                heading = ImageFont.truetype(str(font_path), 54)
                body = ImageFont.truetype(str(font_path), 38)
                small = ImageFont.truetype(str(font_path), 28)
            else:
                heading = body = small = ImageFont.load_default(size=32)
            draw.rectangle((90, 90, 110, 205), fill='#3d705c')
            draw.text((150, 100), title, font=heading, fill='#203e34')
            draw.text((150, 185), subtitle, font=small, fill='#6b756f')
            draw.line((100, 285, 1300, 285), fill='#cad5cd', width=3)
            for line_index, line in enumerate(lines):
                draw.text((120, 365 + line_index * 112), line, font=body, fill='#262d2a')
            draw.rectangle((120, 1480, 1280, 1660), outline='#b9ccc0', width=3)
            draw.text((160, 1510), 'PAPERFLOW / LOCAL ARCHIVE', font=small, fill='#4d6a5d')
            draw.text((160, 1570), f'Page {index + 1}  |  Digital collection', font=small, fill='#4d6a5d')
            draw.text((120, 1770), '演示文档 · 可自由导入、处理和导出', font=small, fill='#7a847e')
            canvas.save(output)
        outputs.append(output)
    photo = directory / 'demo_photo.png'
    if not photo.exists():
        source = cv2.imread(str(outputs[0]))
        background = np.full((2100, 1700, 3), (177, 185, 192), np.uint8)
        corners = np.float32([[150, 100], [1520, 185], [1450, 1980], [80, 1870]])
        matrix = cv2.getPerspectiveTransform(np.float32([[0, 0], [1399, 0], [1399, 1899], [0, 1899]]), corners)
        warped = cv2.warpPerspective(source, matrix, (1700, 2100), borderValue=(177, 185, 192))
        light = np.linspace(0.67, 1, 1700)[None, :, None]
        warped = np.clip(warped * light, 0, 255).astype(np.uint8)
        save_image(photo, warped)
    pdf_path = directory / 'demo_document.pdf'
    if not pdf_path.exists():
        pdf = fitz.open()
        for output in outputs:
            page = pdf.new_page(width=672, height=912)
            page.insert_image(page.rect, filename=str(output))
        pdf.save(pdf_path)
        pdf.close()
    return outputs


def _sheet_image(font_path, sheet_number, side, width=1240, height=1754):
    """生成一张内容互不相同的模拟扫描页；同一张纸的重扫/补拍才保留高相似度。"""
    rng = np.random.default_rng(9000 + sheet_number * 131 + (7 if side == 'back' else 0))
    canvas = Image.new('RGB', (width, height), '#fffef8')
    draw = ImageDraw.Draw(canvas)
    if font_path:
        heading = ImageFont.truetype(str(font_path), 52)
        body = ImageFont.truetype(str(font_path), 34)
        small = ImageFont.truetype(str(font_path), 26)
        code_font = ImageFont.truetype(str(font_path), 44)
    else:
        heading = body = small = code_font = ImageFont.load_default(size=30)
    title = f'双面资料 · 第 {sheet_number} 张 · {"正面" if side == "front" else "背面"}'
    draw.text((90, 90), title, font=heading, fill='#27433a')
    draw.text((90, 180), f'Duplex sheet {sheet_number} / {side} / PaperFlow practice', font=small, fill='#6d7a70')
    # 每张纸独有的角落编码块：随机点阵，是区分不同物理页的关键高频特征。
    grid_x, grid_y = 940, 80
    cell = 16
    for row in range(8):
        for column in range(8):
            if rng.integers(0, 2):
                x0 = grid_x + column * cell
                y0 = grid_y + row * cell
                draw.rectangle((x0, y0, x0 + cell - 3, y0 + cell - 3), fill='#2f4a3e')
    # 每行宽度、纵向位置都由该页种子决定，不同页面的笔画布局完全不同。
    y_cursor = 330
    palette = ('#39473f', '#466055', '#5d7568', '#33423b')
    for line_index in range(12):
        line_width = int(rng.integers(260, 1020))
        x0 = int(rng.integers(110, 170))
        draw.rectangle((x0, y_cursor, x0 + line_width, y_cursor + int(rng.integers(10, 18))), fill=palette[int(rng.integers(0, 4))])
        y_cursor += int(rng.integers(78, 118))
    # 该页独有的几何图形组合。
    for _ in range(5):
        x0 = int(rng.integers(120, 900))
        y0 = int(rng.integers(1250, 1600))
        size = int(rng.integers(40, 130))
        kind = int(rng.integers(0, 3))
        color = palette[int(rng.integers(0, 4))]
        if kind == 0:
            draw.ellipse((x0, y0, x0 + size, y0 + size), outline=color, width=5)
        elif kind == 1:
            draw.rectangle((x0, y0, x0 + size, y0 + int(size * 0.7)), outline=color, width=5)
        else:
            draw.polygon([(x0, y0 + size), (x0 + size // 2, y0), (x0 + size, y0 + size)], outline=color)
    draw.text((90, 1660), f'PF-DUPLEX-{sheet_number:02d}-{side[0].upper()}', font=code_font, fill='#3c5a4d')
    return cv2.cvtColor(np.asarray(canvas), cv2.COLOR_RGB2BGR)


def _rescan_variant(image, sheet_number):
    """模拟重扫：轻微平移、亮度差异与噪声，内容仍是同一张纸。"""
    shifted = cv2.warpAffine(image, np.float32([[1, 0, 14], [0, 1, -10]]), (image.shape[1], image.shape[0]), borderValue=(255, 254, 248))
    bright = np.clip(shifted.astype(np.float32) * 1.06 + 4, 0, 255).astype(np.uint8)
    noise = np.random.default_rng(sheet_number).normal(0, 3, bright.shape).astype(np.int16)
    return np.clip(bright.astype(np.int16) + noise, 0, 255).astype(np.uint8)


def _phone_photo(image):
    """模拟后来补拍的单页：透视倾斜、暖色光照与暗角。"""
    height, width = image.shape[:2]
    background = np.full((height + 240, width + 200, 3), (182, 188, 193), np.uint8)
    corners = np.float32([[120, 70], [width + 150, 110], [width + 90, height + 190], [70, height + 150]])
    matrix = cv2.getPerspectiveTransform(np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]]), corners)
    warped = cv2.warpPerspective(image, matrix, (background.shape[1], background.shape[0]), borderValue=(182, 188, 193))
    mask = cv2.warpPerspective(np.full_like(image, 255), matrix, (background.shape[1], background.shape[0]))
    composed = np.where(mask > 0, warped, background)
    warm = composed.astype(np.float32)
    warm[:, :, 2] *= 1.05
    return np.clip(warm, 0, 255).astype(np.uint8)


def build_duplex_demo(store, documents, collection_id):
    """构造双面扫描练习文档：两次扫描、背面倒置进纸、缺扫、重扫、空白页与补拍页。"""
    import fitz
    font_path = find_font()
    state = store.snapshot()
    name = '双面扫描配对练习 · 两批次混扫'
    existing = next((doc for doc in state['documents'].values() if doc['name'] == name), None)
    if existing:
        return existing['id']
    document = store.create_document(collection_id, name, '演示资料')
    document_id = document['id']
    staging = ROOT / 'demo' / 'duplex_case'
    staging.mkdir(parents=True, exist_ok=True)

    def write_pdf(path, images):
        if path.exists():
            return
        pdf = fitz.open()
        try:
            for image in images:
                height, width = image.shape[:2]
                page = pdf.new_page(width=width * 72 / 150, height=height * 72 / 150)
                success, encoded = cv2.imencode('.png', image)
                page.insert_image(page.rect, stream=encoded.tobytes())
            pdf.save(str(path), garbage=4, deflate=True)
        finally:
            pdf.close()

    # 第一批：正面 1、2、3、5、6（第 4 张正面缺扫），末尾混入第 2 张正面的重扫页。
    front_images = [_sheet_image(font_path, sheet, 'front') for sheet in (1, 2, 3, 5, 6)]
    front_images.append(_rescan_variant(_sheet_image(font_path, 2, 'front'), 2))
    front_pdf = staging / '第一批扫描_正面.pdf'
    write_pdf(front_pdf, front_images)
    # 第二批：背面倒着进纸（180°），缺第 5 张背面，并含一张真正的空白页。
    back_images = [cv2.rotate(_sheet_image(font_path, sheet, 'back'), cv2.ROTATE_180) for sheet in (1, 2, 3, 4, 6)]
    blank = Image.new('RGB', (1240, 1754), '#fffefd')
    back_images.append(cv2.cvtColor(np.asarray(blank), cv2.COLOR_RGB2BGR))
    back_pdf = staging / '第二批扫描_背面倒置.pdf'
    write_pdf(back_pdf, back_images)
    # 后来补拍的单页：第 4 张正面手机照片，来源独立。
    supplement = staging / '后来补拍_手机照片.jpg'
    if not supplement.exists():
        cv2.imwrite(str(supplement), _phone_photo(_sheet_image(font_path, 4, 'front')))

    documents.import_path(document_id, front_pdf)
    documents.import_path(document_id, back_pdf)
    documents.import_path(document_id, supplement)
    return document_id


def seed(store, documents):
    build_demo()
    state = store.snapshot()
    if state['collections']:
        return
    collection = store.create_collection('我的数字档案', '工作资料')
    document = store.create_document(collection['id'], '从纸张到数字 · 体验文档', '演示资料')
    documents.import_path(document['id'], ROOT / 'demo' / 'demo_1.png')
    documents.import_path(document['id'], ROOT / 'demo' / 'demo_2.png')
    photo = store.create_document(collection['id'], '手机拍摄 · 边缘校正练习', '演示资料')
    documents.import_path(photo['id'], ROOT / 'demo' / 'demo_photo.png')
