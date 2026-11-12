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
