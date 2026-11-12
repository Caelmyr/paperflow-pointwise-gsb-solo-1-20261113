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
    outputs = build_demo_pages(directory, font_path)
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
    board_photo = directory / 'demo_receipt_board.png'
    if not board_photo.exists():
        build_receipt_board(directory, board_photo, font_path)
    pdf_path = directory / 'demo_document.pdf'
    if not pdf_path.exists():
        pdf = fitz.open()
        for output in outputs:
            page = pdf.new_page(width=672, height=912)
            page.insert_image(page.rect, filename=str(output))
        pdf.save(pdf_path)
        pdf.close()
    return outputs


def build_demo_pages(directory, font_path):
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
    return outputs


def build_receipt_board(directory, target, font_path):
    """生成桌面上多张方向不同、局部重叠的采购票据照片。"""
    receipts = [
        ('采购票据 A', [('日期', '2026-11-12'), ('金额', '120.00 元'), ('经办', '档案管理')], (238, 246, 236)),
        ('采购票据 B', [('日期', '2026-11-12'), ('金额', '195.00 元'), ('经办', '后勤采购')], (255, 248, 232)),
        ('采购票据 C', [('日期', '2026-11-12'), ('金额', '270.00 元'), ('经办', '项目报销')], (237, 241, 248)),
        ('采购票据 D（部分被遮挡）', [('日期', '2026-11-12'), ('金额', '86.50 元'), ('经办', '现场登记')], (248, 238, 240)),
    ]
    width, height = 760, 980
    sheets = []
    for index, (title, rows, color) in enumerate(receipts):
        canvas = Image.new('RGB', (width, height), color)
        draw = ImageDraw.Draw(canvas)
        title_font = ImageFont.truetype(str(font_path), 40) if font_path else ImageFont.load_default(size=32)
        body_font = ImageFont.truetype(str(font_path), 32) if font_path else ImageFont.load_default(size=26)
        small_font = ImageFont.truetype(str(font_path), 24) if font_path else ImageFont.load_default(size=20)
        draw.rectangle((36, 36, width - 36, height - 36), outline=(120, 138, 126), width=4)
        draw.text((70, 70), title, font=title_font, fill=(35, 58, 50))
        draw.line((70, 150, width - 70, 150), fill=(180, 196, 184), width=3)
        for row_index, (label, value) in enumerate(rows):
            draw.text((70, 220 + row_index * 130), f'{label}：{value}', font=body_font, fill=(45, 55, 50))
        draw.text((70, height - 130), f'NO. PF-RCPT-2026{index + 1:02d}', font=small_font, fill=(110, 125, 115))
        draw.text((70, height - 85), '凭票核对，遮挡区域不作推断', font=small_font, fill=(150, 120, 110))
        sheets.append(np.asarray(canvas.convert('RGB'))[:, :, ::-1].copy())
    board = np.full((1700, 2000, 3), (176, 181, 178), np.uint8)
    placements = [
        (np.float32([[110, 80], [810, 50], [870, 800], [160, 860]]), -2),
        (np.float32([[1090, 110], [1830, 200], [1750, 950], [1050, 860]]), 3),
        (np.float32([[210, 900], [900, 940], [860, 1480], [180, 1420]]), -4),
        (np.float32([[830, 900], [1510, 960], [1450, 1540], [790, 1480]]), 5),
    ]
    source_quad = np.float32([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]])
    for sheet, (target_quad, tilt) in zip(sheets, placements):
        matrix = cv2.getPerspectiveTransform(source_quad, target_quad)
        warped = cv2.warpPerspective(sheet, matrix, (2000, 1700), borderValue=(176, 181, 178))
        mask = cv2.warpPerspective(np.full((height, width), 255, np.uint8), matrix, (2000, 1700))
        board[mask > 0] = warped[mask > 0]
    save_image(target, board)


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
    board_doc = store.create_document(collection['id'], '桌面多票据 · 票据板练习', '演示资料')
    page_ids = documents.import_path(board_doc['id'], ROOT / 'demo' / 'demo_receipt_board.png')
    from .boards import Boards
    boards = Boards(store, documents)
    board = boards.create(page_ids[0], {'name': '桌面采购票据板'})
    boards.detect(board['id'], {'replace': True})
