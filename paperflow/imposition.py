"""拼版服务：规划预览、打印文件导出与导出校核。

服务层负责选项校验、页面选取、导出记录持久化和 PDF 成品校验。
所有拼版几何都在 :mod:`paperflow.booklet` 中按本次选项从零计算，
本模块不缓存任何上一次方案的页序或坐标，因此调整分册或翻面方式时
得到的永远是与当前选项严格对应的新文件。
"""
from __future__ import annotations

import fitz

from . import booklet
from .core import identity, now, require
from .exports import safe_name


class Imposition:
    def __init__(self, store):
        self.store = store
        self.renderer = booklet.BookletRenderer(store)

    # ------------------------------------------------------------------ #
    def _select(self, document_id, page_ids=None):
        state = self.store.snapshot()
        document = state['documents'].get(document_id)
        require(document is not None, '文档不存在', 404)
        if page_ids is None:
            page_ids = document['page_ids']
        require(isinstance(page_ids, list) and 0 < len(page_ids) <= booklet.MAX_LOGICAL_PAGES,
                '拼版页面数量无效')
        require(len(page_ids) == len(set(page_ids)), '拼版页面重复')
        require(set(page_ids).issubset(document['page_ids']), '拼版页面不属于该文档')
        pages = [state['pages'][page_id] for page_id in page_ids]
        return document, pages

    def preview(self, document_id, values):
        values = values or {}
        document, pages = self._select(document_id, values.get('page_ids'))
        options = booklet.validate_options(values.get('options', {}))
        plan = booklet.plan_booklet(pages, options)
        plan['document_id'] = document_id
        plan['document_name'] = document['name']
        # 面板附带缩略图地址，前端无需再自行拼 URL。
        for sheet in plan['sheets']:
            for panel in sheet['panels']:
                panel['thumbnail_url'] = (
                    f"/api/pages/{panel['page_id']}/image?variant=thumbnail"
                    if panel['page_id'] else None)
        return plan

    def export(self, document_id, values):
        values = values or {}
        document, pages = self._select(document_id, values.get('page_ids'))
        options = booklet.validate_options(values.get('options', {}))
        # 每次导出都重新规划：不接受前端回传的旧规划，杜绝旧空白页/坐标混入。
        plan = booklet.plan_booklet(pages, options)
        data = self.renderer.render(pages, plan)
        path = self.store.new_file('exports', '.pdf')
        path.write_bytes(data)
        record = {
            'id': identity('export'),
            'document_id': document_id,
            'kind': 'booklet_pdf',
            'path': self.store.relative(path),
            'filename': safe_name(document['name']) + '_对折拼版.pdf',
            'mime': 'application/pdf',
            'bytes': len(data),
            'page_ids': [page['id'] for page in pages],
            'created_at': now(),
            'booklet': {
                'checksum': plan['checksum'],
                'options': plan['options'],
                'paper': plan['paper'],
                'content_page_count': plan['content_page_count'],
                'blank_page_count': plan['blank_page_count'],
                'signature_count': plan['signature_count'],
                'signatures': plan['signatures'],
                'sheet_count': plan['sheet_count'],
                'pdf_page_count': plan['pdf_page_count'],
                'reading_order': plan['reading_order'],
                'panels': [
                    {
                        'paper_index': sheet['paper_index'],
                        'side': sheet['side'],
                        'slot': panel['slot'],
                        'logical_number': panel['logical_number'],
                        'page_id': panel['page_id'],
                        'kind': panel['kind'],
                        'reason': panel['reason'],
                        'rotation': panel['rotation'],
                        'panel_rect': panel['panel_rect'],
                    }
                    for sheet in plan['sheets'] for panel in sheet['panels']
                ],
            },
        }
        with self.store.transaction() as state:
            state.setdefault('exports', {})[record['id']] = record
            self.store.event(state, '导出对折拼版', document_id,
                             {'export_id': record['id'], 'checksum': plan['checksum'][:12]})
        return {'record': record, 'plan': plan}

    # ------------------------------------------------------------------ #
    def verify(self, export_id):
        """对已导出的拼版 PDF 做结构与几何校核。"""
        state = self.store.snapshot()
        record = state.get('exports', {}).get(export_id)
        require(record is not None, '导出记录不存在', 404)
        require(record['kind'] == 'booklet_pdf', '该导出不是拼版 PDF', 400)
        path = self.store.resolve(record['path'])
        require(path.is_file(), '导出文件缺失', 404)
        info = record['booklet']
        errors, warnings = [], []
        if path.stat().st_size != record['bytes']:
            errors.append('文件大小与导出记录不一致')
        doc = fitz.open(path)
        try:
            if len(doc) != info['pdf_page_count']:
                errors.append(f"PDF 页数 {len(doc)} 与规划 {info['pdf_page_count']} 不一致")
            paper = info['paper']
            panels = info['panels']
            for page_number, pdf_page in enumerate(doc, start=1):
                # PDF 每一面一页：第 1、2 个 PDF 页对应第 1 张纸的正、背面。
                paper_number = (page_number + 1) // 2
                side = 'front' if page_number % 2 == 1 else 'back'
                if abs(pdf_page.rect.width - paper['width']) > 1 or abs(pdf_page.rect.height - paper['height']) > 1:
                    errors.append(f'第 {paper_number} 张纸{side}面尺寸与规划纸张不符')
                page_panels = [panel for panel in panels
                               if panel['paper_index'] == paper_number and panel['side'] == side]
                images = pdf_page.get_images(full=True)
                # 每面两个面板：内容面板各一张图，空白面板无图。
                expected_images = sum(1 for panel in page_panels if panel['kind'] == 'content')
                if len(images) != expected_images:
                    errors.append(f'第 {paper_number} 张纸{side}面图像数 {len(images)}，应为 {expected_images}')
                for panel in page_panels:
                    rect = fitz.Rect(*panel['panel_rect'])
                    if not fitz.Rect(0, 0, paper['width'], paper['height']).contains(rect):
                        errors.append(f"第 {paper_number} 张纸面板越界：{panel['slot']}")
                    if panel['kind'] == 'content':
                        source = state['pages'].get(panel['page_id'])
                        if source is None:
                            errors.append('拼版引用了已删除的页面')
                            continue
                        # 仅当本次导出选择写入文字层时，才校核 OCR 词是否可检索。
                        if info['options'].get('searchable', True) and source.get('ocr') \
                                and not source['ocr'].get('stale'):
                            word = next((b['text'] for b in source['ocr']['boxes'] if len(b['text']) >= 2), None)
                            if word and not pdf_page.search_for(word):
                                errors.append(f"第 {paper_number} 张纸{side}面文字层检索失败：{word}")
            # 阅读顺序连续性。
            numbers = [item['logical_number'] for item in info['reading_order']]
            if numbers != list(range(1, len(numbers) + 1)):
                errors.append('规划中的折叠阅读顺序不连续')
            # 重新按当前记录的选项核算一次，校验值必须一致（防止记录被篡改）。
            document_pages = [state['pages'][pid] for pid in record['page_ids']]
            recomputed = booklet.plan_booklet(document_pages, info['options'])
            if recomputed['checksum'] != info['checksum']:
                errors.append('重新核算的拼版校验值与导出记录不一致')
        finally:
            doc.close()
        return {
            'export_id': export_id,
            'valid': not errors,
            'errors': errors,
            'warnings': warnings,
            'checksum': info['checksum'],
            'sheet_count': info['sheet_count'],
            'signature_count': info['signature_count'],
            'blank_page_count': info['blank_page_count'],
            'checked_at': now(),
        }
