"""表格线处理的应用服务：检测预览、决策持久化、按确认方案改图。"""
from __future__ import annotations

import copy

from . import linework
from .core import identity, now, require
from .imaging import read_image


class LineTool:
    def __init__(self, store, documents):
        self.store = store
        self.documents = documents

    def _page(self, page_id):
        return self.store.get('pages', page_id)

    def analyze(self, page_id, values):
        """对当前图像做检测，并把页面上保存的人工决策重新匹配到新结果。"""
        page = self._page(page_id)
        params = linework.analyze_params(values)
        image = read_image(self.store.resolve(page['current']))
        saved = page.get('line_work')
        report = linework.analyze(image, params, saved)
        report['page_revision'] = page['revision']
        report['saved_decisions'] = bool(saved)
        return report

    def preview(self, page_id, values):
        """返回标注预览图（PNG 字节）与统计，不写入任何数据。"""
        import cv2
        page = self._page(page_id)
        image = read_image(self.store.resolve(page['current']))
        report = linework.analyze(image, linework.analyze_params(values),
                                  page.get('line_work'))
        annotated = linework.overlay_preview(image, report)
        success, encoded = cv2.imencode('.png', annotated)
        require(success, '预览图编码失败', 500)
        return encoded.tobytes(), report

    def render_plan_preview(self, page_id, plan):
        """按前端回传的临时方案渲染“去线后效果”，仍不落盘。"""
        import cv2
        page = self._page(page_id)
        image = read_image(self.store.resolve(page['current']))
        result, metadata = linework.apply_plan(image, plan)
        success, encoded = cv2.imencode('.png', result)
        require(success, '预览图编码失败', 500)
        return encoded.tobytes(), metadata

    def save_decisions(self, page_id, values):
        """只保存人工决策（保护/去除），不改图像；参数调整后仍按几何匹配。"""
        page = self._page(page_id)
        segments = linework._validate_decisions(values.get('segments'), 'segment')
        crossings = linework._validate_decisions(values.get('crossings'), 'crossing')
        record = {
            'updated_at': now(),
            'image_revision': page['revision'],
            'segments': [
                {'id': s['id'], 'rect': s['rect'], 'orientation': s['orientation'],
                 'decision': s['decision']}
                for s in segments
            ],
            'crossings': [
                {'id': c['id'], 'rect': c['rect'], 'orientation': c['orientation'],
                 'decision': c['decision']}
                for c in crossings
            ],
        }
        with self.store.transaction() as state:
            current = state['pages'].get(page_id)
            require(current is not None, '页面不存在', 404)
            current['line_work'] = record
            self.store.event(state, '保存去线人工决策', page_id,
                             {'segments': len(segments), 'crossings': len(crossings)})
        return self.store.get('pages', page_id)

    def apply(self, page_id, values, expected_revision=None):
        """按确认方案真正改图：走 documents.apply，历史与版本号与其他操作完全一致。"""
        plan = values.get('plan')
        require(isinstance(plan, dict), '缺少确认过的去线方案')
        # 先核对页面版本，过期方案直接 409，避免基于旧图像改图。
        page = self._page(page_id)
        if expected_revision is not None:
            require(expected_revision == page['revision'],
                    '页面已变化，请重新检测后再确认方案', 409, 'revision_conflict')
        # 再完整校验方案，失败时不写任何数据。
        image = read_image(self.store.resolve(page['current']))
        result, metadata = linework.apply_plan(image, plan)
        require(metadata['pixels_changed'] > 0 or values.get('allow_empty'),
                '当前方案没有将去除的线段，请先在预览中确认')

        decisions = {
            'updated_at': now(),
            'image_revision': page['revision'],
            'segments': [
                {'id': s['id'], 'rect': s['rect'], 'orientation': s['orientation'],
                 'decision': s['decision']}
                for s in plan.get('segments', [])
            ],
            'crossings': [
                {'id': c['id'], 'rect': c['rect'], 'orientation': c['orientation'],
                 'decision': c['decision']}
                for c in plan.get('crossings', [])
            ],
        }
        updated = self.documents.apply(page_id, 'line_work', {'plan': copy.deepcopy(plan)},
                                       expected_revision)
        # apply 成功后再固化人工决策，保证图像、OCR 失效状态、历史三者对应同一方案。
        with self.store.transaction() as state:
            current = state['pages'].get(page_id)
            require(current is not None, '页面已被删除', 409)
            current['line_work'] = decisions
            self.store.event(state, '应用智能去线方案', page_id,
                             {'changed_pixels': metadata['pixels_changed'],
                              'kept_crossings': len(metadata['kept_crossings'])})
        return self.store.get('pages', page_id)
