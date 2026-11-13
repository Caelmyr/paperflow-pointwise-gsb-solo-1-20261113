from __future__ import annotations

import copy

import cv2
import numpy as np

from . import screening
from .core import DomainError, identity, now, number, require, string
from .imaging import read_image, save_image

SCREEN_REVIEW_VERSION = 1
TILE = screening.TILE
STEP = screening.STEP

# 同一 review 预览缓存最多保留多少组，避免反复调参时堆积文件
MAX_PREVIEWS = 12


class Screening:
    """印刷网纹检测、分区复核与确认抑制的持久化服务。"""

    def __init__(self, store, documents):
        self.store = store
        self.documents = documents

    # ---------- 检测 ----------

    def analyze(self, page_id, options=None):
        options = options or {}
        min_confidence = number(options.get('min_confidence', 0.45), '最低置信度', 0.05, 1)
        page = self.store.get('pages', page_id)
        image = read_image(self.store.resolve(page['current']))
        lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
        height, width = image.shape[:2]
        rows, cols, positions = screening._grid_positions(height, width)
        previous_rects = self._previous_suppressed_rects(page)
        grid = []
        confidences = []
        for row, col, y0, x0 in positions:
            tile = screening._extract_tile(image, y0, x0)
            lab_tile = screening._extract_tile(lab, y0, x0)
            confidence, level, evidence, reason = screening.analyze_tile(tile, lab_tile)
            if self._inside_any(x0, y0, previous_rects):
                # 已经确认抑制过的图块：不再当作新网纹细节，防止重复抹除
                confidence, level, reason = 0.0, screening.CONFIDENCE_NONE, '该区域已在之前确认中抑制，跳过重复检测'
                evidence['already_suppressed'] = True
            grid.append({
                'row': row,
                'col': col,
                'rect': [x0, y0, min(TILE, width - x0), min(TILE, height - y0)],
                'confidence': round(float(confidence), 3),
                'level': level,
                'reason': reason,
                'evidence': evidence,
            })
            confidences.append(confidence)
        mask = screening.build_screen_mask(image, confidences)
        regions = self._merge_regions(grid)
        record = {
            'version': SCREEN_REVIEW_VERSION,
            'page_id': page_id,
            'image_revision': page['revision'],
            'source_image': page['current'],
            'created_at': now(),
            'tile_size': TILE,
            'grid_step': STEP,
            'grid': grid,
            'regions': regions,
            'manual_protected': [],
            'mask': self._encode_mask(mask),
            'options': {'min_confidence': min_confidence},
            'previews': [],
            'confirmed': False,
            'applied_history_id': None,
            'suppressed_rects': previous_rects,
        }
        with self.store.transaction() as state:
            state['pages'][page_id]['screen_review'] = record
            self.store.event(state, '检测印刷网纹', page_id, {'regions': len(regions)})
        return self.public(self.store.get('pages', page_id)['screen_review'])

    def _previous_suppressed_rects(self, page):
        """汇总本页历史上所有已确认抑制的矩形，供重复调整时排除。"""
        rects = []
        existing = page.get('screen_review')
        if existing and existing.get('image_revision') == page['revision']:
            rects.extend(existing.get('suppressed_rects', []))
        for entry in page.get('history', []):
            if entry.get('operation') == 'screen_suppress':
                for region in entry.get('params', {}).get('regions', []):
                    if region.get('decision') == 'suppress' and region.get('rect'):
                        rects.append(region['rect'])
        # 去重
        unique = []
        for rect in rects:
            if rect not in unique:
                unique.append(rect)
        return unique

    def _inside_any(self, x0, y0, rects):
        center_x, center_y = x0 + TILE // 2, y0 + TILE // 2
        for x, y, width, height in rects:
            if x <= center_x < x + width and y <= center_y < y + height:
                return True
        return False

    def _merge_regions(self, grid):
        """把同级相邻图块合并成带证据摘要的区域，待定区域也展示但默认保留。"""
        levels = {(cell['row'], cell['col']): cell for cell in grid}
        visited = set()
        regions = []
        for cell in grid:
            key = (cell['row'], cell['col'])
            if cell['level'] == screening.CONFIDENCE_NONE or key in visited:
                continue
            level = cell['level']
            stack = [key]
            members = []
            while stack:
                current_key = stack.pop()
                if current_key in visited or current_key not in levels:
                    continue
                current = levels[current_key]
                if current['level'] != level:
                    continue
                visited.add(current_key)
                members.append(current)
                row, col = current_key
                stack.extend([(row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)])
            x0 = min(item['rect'][0] for item in members)
            y0 = min(item['rect'][1] for item in members)
            x1 = max(item['rect'][0] + item['rect'][2] for item in members)
            y1 = max(item['rect'][1] + item['rect'][3] for item in members)
            spacing = float(np.median([item['evidence']['spacing'] for item in members]))
            angle = float(np.median([item['evidence']['angle'] for item in members]))
            confidence = round(float(np.mean([item['confidence'] for item in members])), 3)
            reasons = sorted({item['reason'] for item in members})
            regions.append({
                'id': identity('screen_region'),
                'confidence_level': level,
                'confidence': confidence,
                'rect': [int(x0), int(y0), int(x1 - x0), int(y1 - y0)],
                'tile_count': len(members),
                'spacing': round(spacing, 2),
                'angle': round(angle, 1),
                'reason': reasons[0],
                'reasons': reasons,
                'decision': 'suppress' if level == screening.CONFIDENCE_HIGH else 'keep',
                'editable': True,
            })
        regions.sort(key=lambda item: (
            item['confidence_level'] != screening.CONFIDENCE_HIGH, -item['confidence']
        ))
        return regions

    # ---------- 复核决策 ----------

    def _require_review(self, page):
        review = page.get('screen_review')
        require(review is not None, '请先执行印刷网纹检测', 409)
        require(review.get('image_revision') == page['revision'], '图像已变化，请重新检测网纹', 409)
        return review

    def review_state(self, page_id):
        page = self.store.get('pages', page_id)
        review = page.get('screen_review')
        if review is None:
            return {'available': False, 'regions': [], 'confirmed': False}
        return self.public(review)

    def set_region_decision(self, page_id, values):
        region_id = string(values.get('region_id'), '区域标识', 80)
        decision = values.get('decision')
        require(decision in ('suppress', 'keep'), '区域决策只能是 suppress 或 keep')
        with self.store.transaction() as state:
            page = state['pages'].get(page_id)
            require(page is not None, '页面不存在', 404)
            review = self._require_review(page)
            region = next((item for item in review['regions'] if item['id'] == region_id), None)
            require(region is not None, '网纹区域不存在', 404)
            region['decision'] = decision
            region['decided_at'] = now()
            self.store.event(state, '确认网纹区域', page_id, {'region': region_id, 'decision': decision})
        return self.public(self.store.get('pages', page_id)['screen_review'])

    def add_protection(self, page_id, values):
        """手动框选保护区域（文字、织物、真实重复图案），抑制时完全跳过。"""
        page = self.store.get('pages', page_id)
        rect = self._validate_rect(values.get('rect'), page)
        label = string(values.get('label', '手动保护区域'), '保护说明', 120, True)
        with self.store.transaction() as state:
            review = self._require_review(state['pages'][page_id])
            review['manual_protected'].append({
                'id': identity('protect'),
                'rect': rect,
                'label': label,
                'created_at': now(),
            })
            self.store.event(state, '添加网纹保护区域', page_id, {'rect': rect})
        return self.public(self.store.get('pages', page_id)['screen_review'])

    def remove_protection(self, page_id, protection_id):
        with self.store.transaction() as state:
            review = self._require_review(state['pages'][page_id])
            before = len(review['manual_protected'])
            review['manual_protected'] = [
                item for item in review['manual_protected'] if item['id'] != protection_id
            ]
            require(len(review['manual_protected']) < before, '保护区域不存在', 404)
            self.store.event(state, '移除网纹保护区域', page_id)
        return self.public(self.store.get('pages', page_id)['screen_review'])

    def _validate_rect(self, rect, page):
        require(isinstance(rect, (list, tuple)) and len(rect) == 4, '保护区域需要 [x,y,宽,高]')
        x, y = int(rect[0]), int(rect[1])
        width, height = int(rect[2]), int(rect[3])
        require(0 <= x < page['width'] and 0 <= y < page['height'], '保护区域起点越界')
        require(width >= 16 and height >= 16, '保护区域至少 16×16 像素')
        require(x + width <= page['width'] and y + height <= page['height'], '保护区域越出图片')
        return [x, y, width, height]

    # ---------- 掩膜 ----------

    def _decode_mask(self, review, page):
        encoded = review.get('mask')
        if not encoded:
            return np.zeros((page['height'], page['width']), np.float32)
        buffer = np.frombuffer(bytes.fromhex(encoded), np.uint8)
        mask = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
        require(mask is not None and mask.shape == (page['height'], page['width']), '网纹掩膜损坏', 500)
        return mask.astype(np.float32) / 255.0

    def _encode_mask(self, mask):
        success, encoded = cv2.imencode('.png', (np.clip(mask, 0, 1) * 255).astype(np.uint8))
        require(success, '网纹掩膜编码失败', 500)
        return encoded.tobytes().hex()

    def _effective_mask(self, page, review):
        """综合自动掩膜、区域决策与手动保护，得到本次实际抑制掩膜。

        - 自动掩膜只在高置信图块处非零；中/待定区域本身置信度为 0；
        - 用户明确把中/待定区域改为 suppress 时，在其矩形内启用满掩膜，尊重人工判断；
        - 用户保留的高置信区域清零；手动保护矩形最后统一清零，优先级最高。
        """
        mask = self._decode_mask(review, page)
        for region in review['regions']:
            x, y, width, height = region['rect']
            if region['decision'] == 'suppress' and region['confidence_level'] != screening.CONFIDENCE_HIGH:
                mask[y:y + height, x:x + width] = np.maximum(
                    mask[y:y + height, x:x + width], 1.0
                )
            elif region['decision'] == 'keep' and region['confidence_level'] == screening.CONFIDENCE_HIGH:
                mask[y:y + height, x:x + width] = 0.0
        for item in review['manual_protected']:
            x, y, width, height = item['rect']
            mask[y:y + height, x:x + width] = 0.0
        return np.clip(mask, 0, 1)

    # ---------- 全分辨率预览（与确认结果同一渲染路径） ----------

    def preview(self, page_id, values):
        strength = number(values.get('strength', 0.8), '抑制强度', 0, 1)
        page = self.store.get('pages', page_id)
        review = self._require_review(page)
        baseline = read_image(self.store.resolve(review['source_image']))
        mask = self._effective_mask(page, review)
        rendered = screening.render(baseline, mask, strength)
        overlay = screening.overlay_image(baseline, mask, review['regions'])
        preview_path = self.store.new_file('screen_previews', '.png')
        overlay_path = self.store.new_file('screen_previews', '.png')
        save_image(preview_path, rendered)
        save_image(overlay_path, overlay)
        before_energy = screening.screen_energy(baseline)
        after_energy = screening.screen_energy(rendered)
        record = {
            'id': identity('screen_preview'),
            'path': self.store.relative(preview_path),
            'overlay_path': self.store.relative(overlay_path),
            'strength': strength,
            'created_at': now(),
            'source_revision': review['image_revision'],
            'metrics': {
                'screen_energy_before': round(before_energy, 2),
                'screen_energy_after': round(after_energy, 2),
                'energy_reduction_pct': round(
                    max(0, (before_energy - after_energy) / max(1e-6, before_energy)) * 100, 1
                ),
                'detail_loss': screening.detail_loss(baseline, rendered, mask),
                'suppressed_pixel_ratio': round(float((mask > 0.2).mean()), 4),
            },
            'region_metrics': self._region_metrics(baseline, rendered, review),
        }
        with self.store.transaction() as state:
            current_review = state['pages'][page_id]['screen_review']
            stale = current_review['previews'][:-MAX_PREVIEWS + 1] if len(current_review['previews']) >= MAX_PREVIEWS else []
            for item in stale:
                self._unlink(item['path'])
                self._unlink(item['overlay_path'])
            current_review['previews'].append(record)
            current_review['previews'] = current_review['previews'][-MAX_PREVIEWS:]
            self.store.event(state, '生成网纹抑制预览', page_id, {'strength': strength})
        return self._public_preview(record)

    def _region_metrics(self, baseline, rendered, review):
        metrics = []
        for region in review['regions']:
            x, y, width, height = region['rect']
            before_crop = baseline[y:y + height, x:x + width]
            after_crop = rendered[y:y + height, x:x + width]
            mask_crop = np.full((height, width), 1.0 if region['decision'] == 'suppress' else 0.0, np.float32)
            metrics.append({
                'region_id': region['id'],
                'energy_before': round(screening.screen_energy(before_crop), 2),
                'energy_after': round(screening.screen_energy(after_crop), 2),
                'mean_change': round(float(np.abs(
                    before_crop.astype(np.int16) - after_crop.astype(np.int16)
                ).mean()), 3),
                'detail_loss': screening.detail_loss(before_crop, after_crop, mask_crop),
                'decision': region['decision'],
            })
        return metrics

    def preview_file(self, page_id, preview_id, kind='result'):
        page = self.store.get('pages', page_id)
        review = self._require_review(page)
        preview = next((item for item in review['previews'] if item['id'] == preview_id), None)
        require(preview is not None, '预览不存在或已被清理，请重新生成', 404)
        key = 'overlay_path' if kind == 'overlay' else 'path'
        return self.store.resolve(preview[key]), preview

    def region_crop(self, page_id, preview_id, region_id):
        """与确认结果同源的局部 1:1 裁切，缩放预览无法掩盖原始分辨率波纹。"""
        page = self.store.get('pages', page_id)
        review = self._require_review(page)
        preview = next((item for item in review['previews'] if item['id'] == preview_id), None)
        require(preview is not None, '预览不存在，请重新生成', 404)
        region = next((item for item in review['regions'] if item['id'] == region_id), None)
        require(region is not None, '区域不存在', 404)
        rendered = read_image(self.store.resolve(preview['path']))
        baseline = read_image(self.store.resolve(review['source_image']))
        x, y, width, height = region['rect']
        return {
            'before': baseline[y:y + height, x:x + width],
            'after': rendered[y:y + height, x:x + width],
            'region': region,
        }

    # ---------- 确认（直接复用预览文件，保存结果与确认所见逐位一致） ----------

    def apply(self, page_id, values):
        preview_id = string(values.get('preview_id'), '预览标识', 80)
        page = self.store.get('pages', page_id)
        review = self._require_review(page)
        preview = next((item for item in review['previews'] if item['id'] == preview_id), None)
        require(preview is not None, '请先生成并核对全分辨率预览', 409)
        require(preview['source_revision'] == review['image_revision'], '预览基于旧图像，请重新生成', 409)
        region_payload = [
            {
                'id': item['id'],
                'decision': item['decision'],
                'rect': item['rect'],
                'spacing': item['spacing'],
                'angle': item['angle'],
                'confidence_level': item['confidence_level'],
            }
            for item in review['regions']
        ]
        preview_source = self.store.resolve(preview['path'])
        result_page = self.documents.apply(
            page_id,
            'screen_suppress',
            {
                'strength': preview['strength'],
                'preview_id': preview_id,
                'regions': region_payload,
                'protected': [item['rect'] for item in review['manual_protected']],
                'metrics': preview['metrics'],
            },
            expected_revision=page['revision'],
            source_path=preview_source,
        )
        with self.store.transaction() as state:
            current_review = state['pages'][page_id]['screen_review']
            current_review['confirmed'] = True
            current_review['applied_history_id'] = result_page['history'][-1]['id']
            current_review['applied_at'] = now()
            current_review['applied_preview'] = preview_id
            current_review['suppressed_rects'] = [
                item['rect'] for item in current_review['regions'] if item['decision'] == 'suppress'
            ]
            # 同步到新图像版本；已抑制矩形继续保留，供下次检测排除
            current_review['image_revision'] = result_page['revision']
            current_review['source_image'] = result_page['current']
            self.store.event(
                state, '确认网纹抑制', page_id, {'history': current_review['applied_history_id']}
            )
        return result_page

    def _unlink(self, relative):
        try:
            self.store.resolve(relative).unlink(missing_ok=True)
        except DomainError:
            pass

    # ---------- 对外结构 ----------

    def public(self, review):
        return {
            'available': True,
            'version': review['version'],
            'image_revision': review['image_revision'],
            'created_at': review['created_at'],
            'tile_size': review['tile_size'],
            'grid_step': review['grid_step'],
            'regions': review['regions'],
            'manual_protected': review['manual_protected'],
            'previews': [self._public_preview(item) for item in review['previews']],
            'confirmed': review.get('confirmed', False),
            'applied_history_id': review.get('applied_history_id'),
            'suppressed_rects': review.get('suppressed_rects', []),
            'options': review.get('options', {}),
            'grid_summary': self._grid_summary(review),
        }

    def _public_preview(self, preview):
        return {key: value for key, value in preview.items() if key not in ('path', 'overlay_path')}

    def _grid_summary(self, review):
        counts = {screening.CONFIDENCE_HIGH: 0, screening.CONFIDENCE_MEDIUM: 0,
                  screening.CONFIDENCE_UNCERTAIN: 0, screening.CONFIDENCE_NONE: 0}
        for cell in review['grid']:
            counts[cell['level']] = counts.get(cell['level'], 0) + 1
        return counts
