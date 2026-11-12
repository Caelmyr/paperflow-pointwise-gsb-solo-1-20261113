"""对折小册子拼版（imposition）。

本模块是**纯函数式**的拼版规划与渲染：

- ``plan_booklet`` 只依据传入的页面快照和本次选项重新计算整份拼版，
  不读取、不修改工作区中的任何文档或页面；
- 每次调用都从零构建逻辑页序列、分册、帖内排序与纸张坐标，
  上一次方案的补白页和坐标不会残留到新方案；
- ``render_booklet`` 按规划把页面图像等比缩放置入面板（contain，绝不裁切），
  OCR 文字层使用同一个几何矩阵随缩放和旋转保持对应。

约定（坐标单位为 PDF 点，1 英寸 = 72 点）：

- 一张纸（sheet）有正反两面，每面左右两个面板（panel）；
- ``binding='left'`` 为左装订（西式左翻），``binding='right'`` 为右装订；
- ``flip='short'`` 为短边翻面（横向纸张的常见双面方式），背面面板正向；
  ``flip='long'`` 为长边翻面，背面整面旋转 180°；
- 横竖混排页面默认自动旋转 90° 以匹配面板方向，也可选择保持原方向仅缩放。
"""
from __future__ import annotations

import hashlib
import json

import fitz

from .core import choice, number, require

# 常用纸张（点，72dpi）。拼版用纸一律取横向，面板为纵向书页。
PAPER_SIZES = {
    'a4': (841.89, 595.28),
    'a3': (1190.55, 841.89),
    'letter': (792, 612),
    'tabloid': (1224, 792),
}

# 仅保留渲染时实际用到的装订/翻面中文标签；其余选项标签由前端维护。
BINDING_LABELS = {'left': '左装订（左翻页）', 'right': '右装订（右翻页）'}
FLIP_LABELS = {'short': '短边翻面（横向纸推荐）', 'long': '长边翻面（背面整体旋转180°）'}

MAX_SHEETS_PER_SIGNATURE = 40
MAX_LOGICAL_PAGES = 800


# --------------------------------------------------------------------------- #
# 选项校验
# --------------------------------------------------------------------------- #

def validate_options(values):
    require(isinstance(values, dict), '拼版参数必须是对象')
    require(isinstance(values.get('fold_marks', True), bool), '折线标记必须是布尔值')
    require(isinstance(values.get('searchable', True), bool), '文字层开关必须是布尔值')
    options = {
        'binding': choice(values.get('binding', 'left'), '装订方向', ('left', 'right')),
        'flip': choice(values.get('flip', 'short'), '双面翻转方式', ('short', 'long')),
        'signature_mode': choice(values.get('signature_mode', 'none'), '分册方式', ('none', 'split')),
        'paper': choice(values.get('paper', 'a4'), '纸张尺寸', tuple(PAPER_SIZES)),
        'fit': choice(values.get('fit', 'contain'), '适配方式', ('contain',)),
        'orientation': choice(values.get('orientation', 'auto'), '横竖页处理', ('auto', 'keep')),
        'cover': choice(values.get('cover', 'none'), '封面处理', ('none', 'blank')),
        'padding': choice(values.get('padding', 'end'), '补白位置', ('end', 'both')),
        'margin_mm': number(values.get('margin_mm', 8), '安全边距(mm)', 0, 40),
        'gap_mm': number(values.get('gap_mm', 0), '中缝间距(mm)', 0, 30),
        'fold_marks': values.get('fold_marks', True),
        'searchable': values.get('searchable', True),
    }
    if values.get('signature_mode') == 'split':
        options['sheets_per_signature'] = number(
            values.get('sheets_per_signature', 4), '每帖纸张数', 1, MAX_SHEETS_PER_SIGNATURE, True)
    else:
        # 即使未启用分册也记录上限，便于校验超大文档。
        options['sheets_per_signature'] = number(
            values.get('sheets_per_signature', MAX_SHEETS_PER_SIGNATURE),
            '每帖纸张数', 1, MAX_SHEETS_PER_SIGNATURE, True)
    return options


# --------------------------------------------------------------------------- #
# 规划（纯计算，不接触文件系统）
# --------------------------------------------------------------------------- #

def _logical_sequence(pages, options):
    """构建逻辑页序列：真实页 + 可选的空白封面页。"""
    entries = [{'kind': 'content', 'page_id': page['id'], 'index': index,
                'width': page['width'], 'height': page['height'],
                'name': page['name'], 'has_ocr': bool(page.get('ocr')),
                'stale_ocr': bool(page.get('ocr') and page['ocr'].get('stale'))}
               for index, page in enumerate(pages)]
    if options['cover'] == 'blank':
        entries.insert(0, {'kind': 'blank', 'reason': 'cover', 'name': '空白封面'})
    # 每帖必须是 4 的倍数；凑帖所需补白在 _split_signatures 中显式生成并标注。
    return entries


def _split_signatures(entries, options):
    """把逻辑页条目切成若干帖，每帖页数为 4 的倍数，补白只出现在帖尾或指定位置。"""
    if options['signature_mode'] == 'split':
        cap = options['sheets_per_signature'] * 4
    else:
        cap = MAX_LOGICAL_PAGES
    signatures = []
    cursor = 0
    total = len(entries)
    while cursor < total:
        chunk = entries[cursor:cursor + cap]
        cursor += len(chunk)
        signatures.append(chunk)
    # 计算每帖需要的补白。封面补白已在序列里，这里只补“凑4的倍数”的空白。
    details = []
    for sig_index, chunk in enumerate(signatures):
        missing = (-len(chunk)) % 4
        blanks = []
        if missing:
            # padding=both 且为第一帖时，把补白平分到帖首（封面侧），其余在帖尾。
            front = 0
            if options['padding'] == 'both' and sig_index == 0:
                front = missing // 2
                missing -= front
            for _ in range(front):
                blanks.append({'kind': 'blank', 'reason': 'pad_front', 'name': '补白页（帖首）'})
            for _ in range(missing):
                blanks.append({'kind': 'blank', 'reason': 'pad_end', 'name': '补白页（帖尾）'})
            chunk = blanks[:front] + chunk + blanks[front:]
        signatures[sig_index] = chunk
        content_count = sum(1 for item in chunk if item['kind'] == 'content')
        details.append({
            'index': sig_index + 1,
            'leaf_count': len(chunk),
            'sheet_count': len(chunk) // 2,
            'content_count': content_count,
            'blank_count': len(chunk) - content_count,
            'blank_reasons': sorted({item['reason'] for item in chunk if item['kind'] == 'blank'}),
        })
    return signatures, details


def _impose_signature(chunk, binding):
    """单帖骑马钉排序，返回每张纸的两面面板，序号为帖内位置（从 1 开始）。

    n 页（4 的倍数）共 n/4 张纸。第 k 张纸（k 从 1 起）：
      正面（外）两个面板为 n-2k+2 与 2k-1，
      背面（内）两个面板为 2k 与 n-2k+1。
    左装订正面为 [大, 小]；右装订阅读方向相反，正面取 [小, 大]。
    背面顺序两种装订一致（左小右大），因为翻面动作本身改变了左右。
    """
    n = len(chunk)
    quarter = n // 4
    sheets = []
    for k in range(1, quarter + 1):
        # 标准骑马钉帖序（n 为 4 的倍数，共 n/4 张纸）：
        # 第 k 张纸正面 [n-2k+2, 2k-1]，背面 [2k, n-2k+1]。
        outer_left, outer_right = n - 2 * k + 2, 2 * k - 1
        inner_left, inner_right = 2 * k, n - 2 * k + 1
        if binding == 'left':
            front = [outer_left, outer_right]
        else:
            front = [outer_right, outer_left]
        back = [inner_left, inner_right]
        sheets.append({'front': front, 'back': back})
    # 自检：每个帖内位置在整帖中恰好出现一次，且覆盖 1..n。
    used = [position for sheet in sheets for side in ('front', 'back') for position in sheet[side]]
    require(sorted(used) == list(range(1, n + 1)), '帖内拼版排序自检失败', 500)
    return sheets


def _panel_rotation(entry, options):
    """决定内容页在面板中的旋转角度（顺时针，单位度）。"""
    if entry['kind'] == 'blank':
        return 0
    if options['orientation'] == 'keep':
        return 0
    # 面板是纵向书页：横向源页旋转 90° 顺时针后可正常阅读。
    if entry['width'] > entry['height']:
        return 90
    return 0


def plan_booklet(pages, options):
    """根据页面快照与选项，完整重算一份拼版规划。"""
    require(isinstance(pages, list) and pages, '没有可拼版的页面')
    require(len(pages) <= MAX_LOGICAL_PAGES, f'拼版最多支持 {MAX_LOGICAL_PAGES} 页')
    options = validate_options(options)
    entries = _logical_sequence(pages, options)
    signatures, signature_details = _split_signatures(entries, options)

    paper_w, paper_h = PAPER_SIZES[options['paper']]
    margin = options['margin_mm'] * 72 / 25.4
    gap = options['gap_mm'] * 72 / 25.4
    usable_w = paper_w - 2 * margin
    usable_h = paper_h - 2 * margin
    panel_w = (usable_w - gap) / 2
    panel_h = usable_h
    require(panel_w > 20 and panel_h > 20, '安全边距过大，面板空间不足')
    panels = {
        'left': fitz.Rect(margin, margin, margin + panel_w, margin + panel_h),
        'right': fitz.Rect(margin + panel_w + gap, margin, paper_w - margin, margin + panel_h),
    }

    warnings = []
    sheet_records = []
    reading_order = []
    # 逻辑序号由“折好后的阅读顺序”决定：跨帖连续编号 1..N，
    # 与面板落在哪张纸的哪一面无关；补白页不占逻辑序号。
    logical_counter = 0
    for chunk in signatures:
        for entry in chunk:
            if entry['kind'] == 'content':
                logical_counter += 1
                entry['_logical'] = logical_counter
            else:
                entry['_logical'] = None
    for sig_index, chunk in enumerate(signatures):
        imposed = _impose_signature(chunk, options['binding'])
        for local_sheet, sides in enumerate(imposed, start=1):
            paper_index = len(sheet_records) // 2 + 1
            for side in ('front', 'back'):
                side_panels = []
                for slot, logical_seq in enumerate(sides[side]):
                    entry = chunk[logical_seq - 1]
                    rotation = _panel_rotation(entry, options)
                    panel_key = 'left' if slot == 0 else 'right'
                    record = {
                        'signature': sig_index + 1,
                        'sheet_in_signature': local_sheet,
                        'side': side,
                        'slot': panel_key,
                        'logical_number': entry.get('_logical'),
                        'kind': entry['kind'],
                        'reason': entry.get('reason'),
                        'page_id': entry.get('page_id'),
                        'source_index': entry.get('index'),
                        'name': entry['name'],
                        'rotation': rotation,
                        'panel_rect': [round(v, 3) for v in panels[panel_key]],
                    }
                    side_panels.append(record)
                sheet_records.append({
                    'signature': sig_index + 1,
                    'sheet_in_signature': local_sheet,
                    'side': side,
                    'paper_index': paper_index,
                    'panels': side_panels,
                })

    # 阅读顺序按逻辑序号 1..N 从面板记录中重建，保证“折好之后”的顺序可独立校核。
    content_panels = [panel for sheet in sheet_records for panel in sheet['panels']
                      if panel['kind'] == 'content']
    content_panels.sort(key=lambda panel: panel['logical_number'])
    for panel in content_panels:
        reading_order.append({
            'logical_number': panel['logical_number'],
            'page_id': panel['page_id'],
            'source_index': panel['source_index'],
            'name': panel['name'],
            'signature': panel['signature'],
            'sheet_in_signature': panel['sheet_in_signature'],
            'side': panel['side'],
            'slot': panel['slot'],
            'rotation': panel['rotation'],
        })
        source_page = pages[panel['source_index']]
        if source_page.get('ocr') and source_page['ocr'].get('stale'):
            warnings.append(f"第 {panel['logical_number']} 页 OCR 已过期，文字层可能与图像不一致，请重新识别")
        elif not source_page.get('ocr'):
            warnings.append(f"第 {panel['logical_number']} 页尚未 OCR，该页在可检索 PDF 中没有文字层")

    # 阅读顺序自检：逻辑序号必须是 1..N，且每个逻辑页对应的源页严格按传入页序出现。
    actual = [panel['logical_number'] for panel in content_panels]
    expected = list(range(1, len(actual) + 1))
    require(actual == expected, '拼版阅读顺序自检失败', 500)
    source_by_logical = [panel['source_index'] for panel in content_panels]
    require(source_by_logical == list(range(len(pages))), '拼版后源页序被打乱', 500)

    blank_total = sum(detail['blank_count'] for detail in signature_details)
    plan = {
        'options': options,
        'paper': {'id': options['paper'], 'width': round(paper_w, 3), 'height': round(paper_h, 3),
                  'orientation': 'landscape', 'margin_pt': round(margin, 3), 'gap_pt': round(gap, 3)},
        'panel': {'width': round(panel_w, 3), 'height': round(panel_h, 3), 'orientation': 'portrait'},
        'content_page_count': len(pages),
        'logical_page_count': len(reading_order),
        'blank_page_count': blank_total,
        'signature_count': len(signatures),
        'signatures': signature_details,
        'sheet_count': len(sheet_records) // 2,
        'pdf_page_count': len(sheet_records),
        'sheets': sheet_records,
        'reading_order': reading_order,
        'warnings': sorted(set(warnings)),
    }
    plan['checksum'] = checksum(plan)
    return plan


def checksum(plan):
    """对规划的关键几何与页序生成稳定校验值（不含自身字段）。"""
    payload = {
        'options': plan['options'],
        'paper': plan['paper'],
        'sheets': [[p['page_id'], p['kind'], p['reason'], p['rotation'],
                    p['signature'], p['sheet_in_signature'], p['side'], p['slot'],
                    p['logical_number'], p['panel_rect']]
                   for sheet in plan['sheets'] for p in sheet['panels']],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


# --------------------------------------------------------------------------- #
# 几何：源页 -> 面板 的仿射矩阵
# --------------------------------------------------------------------------- #

def fit_matrix(src_w, src_h, rect, rotation):
    """计算把源图像 contain 进面板（含旋转）的仿射矩阵。

    旋转角度只支持 0 / 90 / 270（顺时针）。矩阵直接映射源像素坐标到纸张坐标，
    OCR 文字框用同一矩阵变换，保证缩放、旋转后仍与图像对应。
    """
    rotation = choice(rotation, '旋转角度', (0, 90, 270))
    if rotation == 0:
        rotated_w, rotated_h = src_w, src_h
    else:
        rotated_w, rotated_h = src_h, src_w
    scale = min(rect.width / rotated_w, rect.height / rotated_h)
    w, h = rotated_w * scale, rotated_h * scale
    x0 = rect.x0 + (rect.width - w) / 2
    y0 = rect.y0 + (rect.height - h) / 2
    if rotation == 0:
        return fitz.Matrix(scale, 0, 0, scale, x0, y0)
    if rotation == 90:  # (x,y) -> (x0 + y*s, y0 + (src_w-x)*s)
        return fitz.Matrix(0, -scale, scale, 0, x0, y0 + src_w * scale)
    # 270 顺时针：(x,y) -> (x0 + (src_h-y)*s, y0 + x*s)
    return fitz.Matrix(0, scale, -scale, 0, x0 + src_h * scale, y0)


def sheet_flip_matrix(plan, side):
    """长边翻面时背面整面 180° 旋转矩阵；短边翻面为恒等。"""
    if plan['options']['flip'] == 'long' and side == 'back':
        w, h = plan['paper']['width'], plan['paper']['height']
        return fitz.Matrix(-1, 0, 0, -1, w, h)
    return fitz.Matrix(1, 0, 0, 1, 0, 0)


# --------------------------------------------------------------------------- #
# PDF 渲染
# --------------------------------------------------------------------------- #

class BookletRenderer:
    def __init__(self, store):
        self.store = store

    def render(self, pages, plan):
        page_by_id = {page['id']: page for page in pages}
        doc = fitz.open()
        self.font = fitz.Font('china-s')
        paper_w, paper_h = plan['paper']['width'], plan['paper']['height']
        try:
            for sheet in plan['sheets']:
                pdf_page = doc.new_page(width=paper_w, height=paper_h)
                flip = sheet_flip_matrix(plan, sheet['side'])
                self._draw_guides(pdf_page, plan, sheet, flip)
                for panel in sheet['panels']:
                    rect = fitz.Rect(*panel['panel_rect'])
                    if panel['kind'] == 'blank':
                        self._draw_blank(pdf_page, rect, panel, flip, plan)
                        continue
                    source = page_by_id.get(panel['page_id'])
                    require(source is not None, '拼版引用的页面不存在', 500)
                    matrix = fit_matrix(source['width'], source['height'], rect, panel['rotation'])
                    final = matrix * flip
                    target = fitz.Rect(0, 0, source['width'], source['height']) * final
                    # 安全断言：任何内容面板都必须完整落在纸张内，绝不允许裁切。
                    require(target.x0 >= -0.5 and target.y0 >= -0.5
                            and target.x1 <= paper_w + 0.5 and target.y1 <= paper_h + 0.5,
                            '拼版内容越出纸张，请减小安全边距', 500)
                    pdf_page.insert_image(
                        target,
                        filename=str(self.store.resolve(source['current'])),
                        keep_proportion=False,
                    )
                    if plan['options']['searchable'] and source.get('ocr'):
                        self._draw_text_layer(pdf_page, source, final, self.font, plan, flip)
            doc.set_metadata({
                'title': 'PaperFlow 对折小册子拼版',
                'author': 'PaperFlow',
                'creator': 'PaperFlow booklet imposition',
                'subject': '骑马钉对折拼版打印文件',
                'keywords': 'booklet,imposition,fold',
            })
            return doc.tobytes(garbage=4, deflate=True)
        finally:
            doc.close()

    # ------------------------------------------------------------------ #
    def _draw_guides(self, pdf_page, plan, sheet, flip):
        paper_w, paper_h = plan['paper']['width'], plan['paper']['height']
        gray = (0.55, 0.58, 0.55)
        if plan['options']['fold_marks']:
            center_x = paper_w / 2
            line = fitz.Rect(center_x, 0, center_x, paper_h) * flip
            pdf_page.draw_line(line.tl, line.br, color=gray, width=0.4, dashes='[3 3] 0')
        # 每张纸外缘的装订/裁切对位标记（不进入面板安全区）。
        m = plan['paper']['margin_pt']
        mark = 6
        for x in (m / 2, paper_w - m / 2):
            segment = fitz.Rect(x - 0.3, paper_h / 2 - mark, x + 0.3, paper_h / 2 + mark) * flip
            pdf_page.draw_line(segment.tl, segment.br, color=gray, width=0.5)
        side_label = '正面' if sheet['side'] == 'front' else '背面'
        label = (f"第 {sheet['paper_index']} 张纸 · {side_label} · 第 {sheet['signature']} 册"
                 f" · {BINDING_LABELS[plan['options']['binding']].split('（')[0]}"
                 f" · {FLIP_LABELS[plan['options']['flip']].split('（')[0]}")
        writer = fitz.TextWriter(pdf_page.rect)
        writer.append(fitz.Point(8, paper_h - 6), label, font=self.font, fontsize=6.5)
        writer.write_text(pdf_page, color=gray, render_mode=0, morph=self._flip_morph(plan, flip))

    def _flip_morph(self, plan, flip):
        """长边翻面背面 180° 的 morph 参数；其他情况为 None。"""
        if flip.a < 0:
            center = fitz.Point(plan['paper']['width'] / 2, plan['paper']['height'] / 2)
            return center, fitz.Matrix(-1, 0, 0, -1, 0, 0)
        return None

    def _draw_blank(self, pdf_page, rect, panel, flip, plan):
        # 边框按翻面矩阵定位；文字在未翻转坐标写入，再用 morph 统一旋转，避免双重变换。
        target = rect * flip
        pdf_page.draw_rect(target, color=(0.82, 0.84, 0.8), width=0.6, dashes='[2 2] 0')
        reason_labels = {'cover': '空白封面', 'pad_front': '补白页（帖首）', 'pad_end': '补白页（帖尾）'}
        label = reason_labels.get(panel['reason'], '空白页')
        writer = fitz.TextWriter(pdf_page.rect)
        origin = fitz.Point(rect.x0 + 8, (rect.y0 + rect.y1) / 2)
        writer.append(origin, label, font=self.font, fontsize=9)
        writer.write_text(pdf_page, color=(0.7, 0.72, 0.68), morph=self._flip_morph(plan, flip))

    def _draw_text_layer(self, pdf_page, source, matrix, font, plan, flip):
        result = source['ocr']
        if result.get('text_override'):
            self._draw_text_override(pdf_page, source, matrix, result, font, plan, flip)
            return
        writer = fitz.TextWriter(pdf_page.rect)
        for word in result['boxes']:
            text = word['text']
            if not text:
                continue
            box = fitz.Rect(word['x'], word['y'],
                            word['x'] + word['width'], word['y'] + word['height']) * matrix
            font_size = max(1, abs(box.height) * 0.85)
            # 旋转/翻面后 y 轴可能反向，TextWriter 需要正向高度，取基线点。
            origin = fitz.Point(box.x0, box.y1 if box.y1 > box.y0 else box.y0)
            writer.append(origin, text, font=font, fontsize=font_size)
        writer.write_text(pdf_page, render_mode=3, morph=self._flip_morph(plan, flip))

    def _draw_text_override(self, pdf_page, source, matrix, result, font, plan, flip):
        src_rect = fitz.Rect(0, 0, source['width'], source['height'])
        box = src_rect * matrix
        # 180° 翻转后坐标反转，恢复成正向矩形再写文本框。
        box = fitz.Rect(min(box.x0, box.x1), min(box.y0, box.y1),
                        max(box.x0, box.x1), max(box.y0, box.y1))
        lines = result['text'].splitlines() or ['']
        text = '\n'.join(lines)
        font_size = min(11, max(3, box.height / (len(lines) * 1.5)))
        for _ in range(12):
            used = pdf_page.insert_textbox(
                box, text, fontname='china-s', fontsize=font_size, render_mode=3,
                morph=self._flip_morph(plan, flip))
            if used >= 0:
                return
            font_size *= 0.85
        raise DomainError('人工文本过长，无法完整写入拼版文字层；请减少文本或编辑文字框')
