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


def _halftone_demo_page(path):
    """生成带彩色印刷网点、灰度网点、织物纹理与小字的网纹治理演示页。"""
    rng = np.random.default_rng(11)

    def halftone_block(height, width, spacing, angle, ink, coverage=None, shade=None):
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        rad = np.deg2rad(angle)
        u = (xx * np.cos(rad) + yy * np.sin(rad)) / spacing
        v = (-xx * np.sin(rad) + yy * np.cos(rad)) / spacing
        distance2 = ((np.abs(u - np.round(u)) * spacing) ** 2
                     + (np.abs(v - np.round(v)) * spacing) ** 2)
        cov = np.clip(shade if shade is not None else coverage, 0.08, 0.92)
        radius = spacing * 0.5 * np.sqrt(cov) * 1.9
        dots = (distance2 <= radius ** 2).astype(np.float32)
        block = np.zeros((height, width, 3), np.float32)
        for channel in range(3):
            block[:, :, channel] = 255 * (1 - dots) + ink[channel] * dots
        return block

    def fabric_block(height, width, thread=14):
        yy, xx = np.mgrid[0:height, 0:width]
        over = ((xx // thread + yy // thread) % 2).astype(np.float32)
        vx = ((xx // thread) % 2).astype(np.float32)
        vy = ((yy // thread) % 2).astype(np.float32)
        base = np.full((height, width, 3), (196, 178, 150), np.float32)
        shade = 1 - 0.18 * over - 0.06 * (vx - vy)
        return np.clip(base * shade[:, :, None], 0, 255)

    height, width = 1600, 1200
    page = np.full((height, width, 3), (250, 252, 248), np.uint8)
    # 彩色三色叠印区（不同角度/间距的 CMY 网点）
    ax, ay, aw, ah = 90, 260, 480, 420
    gradient = np.linspace(0.15, 0.8, aw)[None, :] * np.ones((ah, 1))
    cyan = halftone_block(ah, aw, 6.0, 15, (200, 190, 30), shade=gradient)
    magenta = halftone_block(ah, aw, 6.3, 75, (120, 60, 200), shade=0.9 - gradient)
    yellow = halftone_block(ah, aw, 5.8, 0, (60, 210, 230), coverage=0.35)
    page[ay:ay + ah, ax:ax + aw] = np.clip(255 * (cyan / 255) * (magenta / 255) * (yellow / 255), 0, 255)
    # 单色粗网点区（45°，更大间距）
    bx, by, bw, bh = 630, 260, 480, 420
    shade2 = np.linspace(0.8, 0.2, bw)[None, :] * np.ones((bh, 1))
    page[by:by + bh, bx:bx + bw] = halftone_block(
        bh, bw, 9.5, 45, (40, 70, 160), shade=np.clip(shade2, 0.1, 0.9)
    )
    # 织物纹理（本就重复的图案，应被保护）
    fx, fy, fw, fh = 90, 760, 480, 360
    page[fy:fy + fh, fx:fx + fw] = fabric_block(fh, fw)
    # 小字与细线（必须保护）
    cv2.putText(page, 'Halftone scan test 2026 small print', (630, 800),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (30, 30, 30), 2, cv2.LINE_AA)
    for index in range(8):
        cv2.putText(page, f'line {index} tiny text preserve small characters',
                    (630, 850 + index * 34), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (20, 20, 20), 1, cv2.LINE_AA)
    for index in range(6):
        cv2.line(page, (630, 1150 + index * 22), (1080, 1150 + index * 22), (60, 60, 60), 1)
    cv2.rectangle(page, (630, 760), (1080, 1120), (90, 90, 90), 1)
    noise = rng.normal(0, 3.0, page.shape)
    save_image(path, np.clip(page.astype(np.float32) + noise, 0, 255).astype(np.uint8))


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
    halftone = directory / 'demo_halftone.png'
    if not halftone.exists():
        _halftone_demo_page(halftone)
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
    halftone_doc = store.create_document(collection['id'], '印刷网纹 · 分区抑制练习', '演示资料')
    documents.import_path(halftone_doc['id'], ROOT / 'demo' / 'demo_halftone.png')
