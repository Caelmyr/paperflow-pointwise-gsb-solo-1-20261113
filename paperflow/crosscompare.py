from __future__ import annotations

import copy

from . import compare_text, compare_vision
from .core import identity, now, require, string
from .imaging import read_image

# 文字门控：两侧都有 OCR 时，相似度低于该值视为不同附件/单据，不自动配对
AUTO_TEXT_SIMILARITY = 0.55
MAX_PAGES = 200


class CrossComparisons:
    """两份独立文档之间的页面对应与差异审阅，结论绑定双方当时版本。"""

    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------ 基础读取
    def _load(self, project_id):
        record = self.store.snapshot().get('cross_comparisons', {}).get(project_id)
        require(record is not None, '对比项目不存在', 404)
        return record

    def _page(self, state, page_id):
        page = state['pages'].get(page_id)
        require(page is not None, '页面已被删除，无法参与对比', 404, 'page_missing')
        return page

    def _document_pages(self, state, document_id):
        document = state['documents'].get(document_id)
        require(document is not None, '对比文档不存在或已删除', 404, 'document_missing')
        require(0 < len(document['page_ids']) <= MAX_PAGES, '文档页数须在1–200之间')
        return document, [state['pages'][pid] for pid in document['page_ids']]

    def _page_ref(self, page):
        """版本指纹：任一方重扫或校订后都会变化。"""
        ocr = page.get('ocr')
        return {
            'page_id': page['id'],
            'name': page.get('name'),
            'revision': page['revision'],
            'current': page['current'],
            'sha256': page.get('sha256'),
            'ocr_edited_at': ocr.get('edited_at') if ocr else None,
            'ocr_recognized_at': ocr.get('recognized_at') if ocr else None,
            'ocr_text_digest': _digest_text(ocr.get('text') if ocr else None),
        }

    def _visual_features(self, page):
        image = read_image(self.store.resolve(page['current']))
        return {
            'image': image,
            'phash': compare_vision.perceptual_hash(image),
        }

    # ------------------------------------------------------------------ 项目管理
    def create(self, values):
        left_id = values.get('left_document_id')
        right_id = values.get('right_document_id')
        require(isinstance(left_id, str) and isinstance(right_id, str), '必须指定两份文档')
        require(left_id != right_id, '请选择两份不同的文档进行对比')
        name = string(values.get('name', '双文档对比'), '对比名称', 160)
        state = self.store.snapshot()
        left_doc, left_pages = self._document_pages(state, left_id)
        right_doc, right_pages = self._document_pages(state, right_id)
        record = {
            'id': identity('compare'),
            'name': name,
            'left_document_id': left_id,
            'right_document_id': right_id,
            'left_document_name': left_doc['name'],
            'right_document_name': right_doc['name'],
            'created_at': now(),
            'updated_at': now(),
            'workspace_revision': state['revision'],
            'pairs': [],
            'left_unmatched': list(left_doc['page_ids']),
            'right_unmatched': list(right_doc['page_ids']),
            'baselines': {
                'left': [self._page_ref(page) for page in left_pages],
                'right': [self._page_ref(page) for page in right_pages],
            },
        }
        with self.store.transaction() as candidate:
            candidate.setdefault('cross_comparisons', {})[record['id']] = record
            self.store.event(candidate, '创建双文档对比', record['id'], {
                'left': left_doc['name'], 'right': right_doc['name'],
            })
        return self.detail(record['id'])

    def list_projects(self):
        records = self.store.snapshot().get('cross_comparisons', {}).values()
        return [self._summary(record) for record in sorted(records, key=lambda item: item['created_at'], reverse=True)]

    def _project_status(self, record):
        if not record['pairs']:
            return 'draft'
        reviewed = sum(1 for pair in record['pairs'] if pair.get('review'))
        if reviewed == 0:
            return 'matching'
        if reviewed == len(record['pairs']):
            return 'reviewed'
        return 'in_review'

    def _summary(self, record):
        stale = self._stale_summary(record)
        return {
            'id': record['id'],
            'name': record['name'],
            'left_document_id': record['left_document_id'],
            'right_document_id': record['right_document_id'],
            'left_document_name': record['left_document_name'],
            'right_document_name': record['right_document_name'],
            'created_at': record['created_at'],
            'updated_at': record['updated_at'],
            'status': self._project_status(record),
            'pair_count': len(record['pairs']),
            'left_unmatched_count': len(record['left_unmatched']),
            'right_unmatched_count': len(record['right_unmatched']),
            'stale_pair_count': stale['stale_pair_count'],
            'missing_page_count': stale['missing_page_count'],
            'stale': stale['stale_pair_count'] > 0 or stale['missing_page_count'] > 0,
        }

    def detail(self, project_id):
        record = self._load(project_id)
        state = self.store.snapshot()
        result = self._summary(record)
        result['pairs'] = [self._pair_view(state, pair) for pair in record['pairs']]
        result['left_unmatched'] = self._unmatched_view(state, record['left_unmatched'], record['left_document_id'], record)
        result['right_unmatched'] = self._unmatched_view(state, record['right_unmatched'], record['right_document_id'], record)
        result['decisions'] = list(record.get('decisions', {}).values())
        result['stale'] = self._stale_detail(state, record)
        return result

    def _unmatched_view(self, state, page_ids, document_id, record):
        document = state['documents'].get(document_id)
        ordered = document['page_ids'] if document else []
        baseline_names = {}
        for side in ('left', 'right'):
            for ref in record.get('baselines', {}).get(side, []):
                baseline_names[ref['page_id']] = ref.get('name')
        rows = []
        # 文档中仍存在的未配对页，按当前页序展示
        for index, page_id in enumerate(ordered):
            if page_id not in page_ids:
                continue
            page = state['pages'].get(page_id)
            rows.append({
                'page_id': page_id,
                'name': page['name'] if page else '（页面已删除）',
                'page_number': index + 1,
                'exists': page is not None,
                'thumbnail': page['thumbnail'] if page else None,
            })
        existing = {row['page_id'] for row in rows}
        # 已从文档删除但仍被项目记录的页面，追加到末尾并明确标记，避免“静默消失”
        for page_id in page_ids:
            if page_id in existing:
                continue
            rows.append({
                'page_id': page_id,
                'name': baseline_names.get(page_id) or '（页面已删除）',
                'page_number': None,
                'exists': False,
                'thumbnail': None,
            })
        return rows

    def _pair_view(self, state, pair):
        left = state['pages'].get(pair['left_page_id'])
        right = state['pages'].get(pair['right_page_id'])
        view = {
            'id': pair['id'],
            'left_page_id': pair['left_page_id'],
            'right_page_id': pair['right_page_id'],
            'left_name': left['name'] if left else '（页面已删除）',
            'right_name': right['name'] if right else '（页面已删除）',
            'left_exists': left is not None,
            'right_exists': right is not None,
            'match_status': pair['match_status'],
            'match_origin': pair['match_origin'],
            'scores': pair.get('scores'),
            'visual': pair.get('visual'),
            'visual_regions': pair.get('visual_regions', []),
            'text': pair.get('text'),
            'decision': pair.get('decision'),
            'review': pair.get('review'),
            'review_history': pair.get('review_history', []),
            'stale_reviews': pair.get('stale_reviews', []),
            'stale': self._pair_stale(state, pair),
            'baseline_refs': pair.get('baseline_refs'),
            'created_at': pair['created_at'],
            'updated_at': pair['updated_at'],
        }
        return view

    # ------------------------------------------------------------------ 自动配对
    def auto_match(self, project_id, values=None):
        values = values or {}
        record = self._load(project_id)
        reset = bool(values.get('reset'))
        state = self.store.snapshot()
        _, left_pages = self._document_pages(state, record['left_document_id'])
        _, right_pages = self._document_pages(state, record['right_document_id'])
        left_map = {page['id']: page for page in left_pages}
        right_map = {page['id']: page for page in right_pages}

        # 重算匹配池（reset 时回到项目基线），图像 IO 与全部计算在事务外完成
        if reset:
            left_pool = [ref['page_id'] for ref in record['baselines']['left']
                         if ref['page_id'] in left_map]
            right_pool = [ref['page_id'] for ref in record['baselines']['right']
                          if ref['page_id'] in right_map]
        else:
            left_pool = [pid for pid in record['left_unmatched'] if pid in left_map]
            right_pool = [pid for pid in record['right_unmatched'] if pid in right_map]
        left_images = [read_image(self.store.resolve(left_map[pid]['current'])) for pid in left_pool]
        right_images = [read_image(self.store.resolve(right_map[pid]['current'])) for pid in right_pool]
        # 序列对齐：利用页序稳定性做全局最优对应，处理位移、插入附件与缺页，复杂度 O(页×带宽)
        alignment = compare_vision.sequence_align(left_images, right_images)
        built_pairs = []
        used_left, used_right = set(), set()
        for left_index, right_index, quick_score in alignment['pairs']:
            left_id = left_pool[left_index]
            right_id = right_pool[right_index]
            # 文字否决：两侧都有 OCR 但文本相似度过低，说明是同模板的不同附件/单据，
            # 不能仅因版面相近就自动结合，留给人工决定
            left_ocr = left_map[left_id].get('ocr')
            right_ocr = right_map[right_id].get('ocr')
            if left_ocr and right_ocr:
                text_check = compare_text.text_similarity(left_ocr.get('text'), right_ocr.get('text'))
                if text_check is not None and text_check < AUTO_TEXT_SIMILARITY:
                    continue
            # 仅对最终候选执行昂贵的 ECC 精算，得到笔画级差异与配准参数
            score = self._candidate_score(
                left_map[left_id], right_map[right_id],
                self._visual_features(left_map[left_id]),
                self._visual_features(right_map[right_id]),
            )
            score['quick_similarity'] = quick_score
            built_pairs.append((left_id, right_id, score))
            used_left.add(left_id)
            used_right.add(right_id)

        with self.store.transaction() as candidate:
            project = candidate['cross_comparisons'][project_id]
            if reset:
                project['pairs'] = []
                project['decisions'] = {}
                project['left_unmatched'] = list(left_pool)
                project['right_unmatched'] = list(right_pool)
            for left_id, right_id, score in built_pairs:
                pair = self._build_pair(
                    candidate, left_map[left_id], right_map[right_id], score, 'auto'
                )
                project['pairs'].append(pair)
            project['left_unmatched'] = [pid for pid in project['left_unmatched'] if pid not in used_left]
            project['right_unmatched'] = [pid for pid in project['right_unmatched'] if pid not in used_right]
            project['updated_at'] = now()
            self.store.event(candidate, '自动匹配页面', project_id, {
                'pairs': len(built_pairs),
                'left_unmatched': len(project['left_unmatched']),
                'right_unmatched': len(project['right_unmatched']),
            })
        return self.detail(project_id)

    def _candidate_score(self, left_page, right_page, left_feature, right_feature):
        visual = compare_vision.visual_compare(left_feature['image'], right_feature['image'])
        text = compare_text.compare_text(
            left_page.get('ocr', {}).get('text') if left_page.get('ocr') else None,
            right_page.get('ocr', {}).get('text') if right_page.get('ocr') else None,
        )
        correlation = visual['correlation']
        text_similarity = text['similarity']
        phash = visual['phash_distance']
        # 综合分：视觉为主，文字为辅；无 OCR 时只依赖视觉，且不允许自动确认
        visual_term = max(0.0, correlation)
        text_term = text_similarity if text_similarity is not None else 0.0
        text_weight = 0.35 if text_similarity is not None else 0.0
        combined = round((1 - text_weight) * visual_term + text_weight * text_term, 5)
        return {
            'left_page_id': left_page['id'],
            'right_page_id': right_page['id'],
            'correlation': correlation,
            'ink_change_ratio': visual['ink_change_ratio'],
            'region_count': visual['region_count'],
            'phash_distance': phash,
            'text_similarity': text_similarity,
            'text_change_ratio': text['change_ratio'] if text['available'] else None,
            'combined': combined,
            'visual_detail': visual,
            'text_detail': text,
        }

    def _build_pair(self, state, left_page, right_page, score, origin):
        # 人工配对一律确认；自动配对里，只要视觉发现笔画改动或文字证据不足，就降级为待人工确认
        if origin == 'manual':
            match_status = 'confirmed'
        elif score['ink_change_ratio'] >= compare_vision.CHANGED_INK_RATIO \
                or score['region_count'] >= compare_vision.CHANGED_REGIONS:
            match_status = 'suggested_review'
        elif score['text_similarity'] is not None and score['text_similarity'] < AUTO_TEXT_SIMILARITY:
            match_status = 'suggested_review'
        else:
            match_status = 'auto_confirmed'
        pair = {
            'id': identity('pair'),
            'left_page_id': left_page['id'],
            'right_page_id': right_page['id'],
            'match_status': match_status,
            'match_origin': origin,
            'scores': {
                'correlation': score['correlation'],
                'combined': score['combined'],
                'phash_distance': score['phash_distance'],
                'quick_similarity': score.get('quick_similarity'),
                'text_similarity': score['text_similarity'],
                'text_change_ratio': score['text_change_ratio'],
            },
            'visual': {key: value for key, value in score['visual_detail'].items()
                       if key not in ('regions',)},
            'visual_regions': score['visual_detail']['regions'],
            'text': score['text_detail'],
            'decision': None,
            'review': None,
            'baseline_refs': {
                'left': self._page_ref(left_page),
                'right': self._page_ref(right_page),
            },
            'created_at': now(),
            'updated_at': now(),
        }
        return pair

    # ------------------------------------------------------------------ 人工配对
    def manual_match(self, project_id, values):
        left_id = values.get('left_page_id')
        right_id = values.get('right_page_id')
        require(isinstance(left_id, str) and isinstance(right_id, str), '必须指定左右页面')
        record = self._load(project_id)
        state = self.store.snapshot()
        left_page = self._page(state, left_id)
        right_page = self._page(state, right_id)
        require(left_page['document_id'] == record['left_document_id'], '左页面不属于对比左侧文档', 409)
        require(right_page['document_id'] == record['right_document_id'], '右页面不属于对比右侧文档', 409)
        # 图像 IO 与特征计算在事务外完成，避免长事务阻塞工作区
        left_feature = self._visual_features(left_page)
        right_feature = self._visual_features(right_page)
        score = self._candidate_score(left_page, right_page, left_feature, right_feature)
        with self.store.transaction() as candidate:
            project = candidate['cross_comparisons'][project_id]
            require(left_id in project['left_unmatched'], '左页面已在其他配对中，请先解除', 409)
            require(right_id in project['right_unmatched'], '右页面已在其他配对中，请先解除', 409)
            pair = self._build_pair(candidate, left_page, right_page, score, 'manual')
            project['pairs'].append(pair)
            project['left_unmatched'].remove(left_id)
            project['right_unmatched'].remove(right_id)
            project['updated_at'] = now()
            self.store.event(candidate, '人工确认页面配对', project_id, {'pair': pair['id']})
        return self.detail(project_id)

    def confirm_pair(self, project_id, pair_id):
        with self.store.transaction() as candidate:
            project = candidate['cross_comparisons'][project_id]
            pair = self._find_pair(project, pair_id)
            pair['match_status'] = 'confirmed'
            pair['updated_at'] = now()
            self.store.event(candidate, '确认配对建议', pair_id)
        return self.detail(project_id)

    def unmatch(self, project_id, pair_id):
        with self.store.transaction() as candidate:
            project = candidate['cross_comparisons'][project_id]
            pair = self._find_pair(project, pair_id)
            project['pairs'] = [item for item in project['pairs'] if item['id'] != pair_id]
            if pair['left_page_id'] not in project['left_unmatched']:
                project['left_unmatched'].append(pair['left_page_id'])
            if pair['right_page_id'] not in project['right_unmatched']:
                project['right_unmatched'].append(pair['right_page_id'])
            project['updated_at'] = now()
            self.store.event(candidate, '解除页面配对', pair_id)
        return self.detail(project_id)

    def _find_pair(self, project, pair_id):
        pair = next((item for item in project['pairs'] if item['id'] == pair_id), None)
        require(pair is not None, '配对不存在', 404)
        return pair

    # ------------------------------------------------------------------ 重复/缺失决定
    def set_decision(self, project_id, values):
        page_id = values.get('page_id')
        side = values.get('side')
        decision = values.get('decision')
        note = string(values.get('note', ''), '决定备注', 2000, True)
        require(side in ('left', 'right'), '页面侧别无效')
        require(decision in ('duplicate', 'missing', 'inserted', 'ignored', 'pending'), '页面决定无效')
        self._load(project_id)
        with self.store.transaction() as candidate:
            project = candidate['cross_comparisons'][project_id]
            unmatched = project['left_unmatched'] if side == 'left' else project['right_unmatched']
            require(page_id in unmatched, '只能对未配对页面作重复/缺失决定', 409)
            decisions = project.setdefault('decisions', {})
            if decision == 'pending':
                decisions.pop(page_id, None)
            else:
                page = self._page(candidate, page_id)
                decisions[page_id] = {
                    'page_id': page_id,
                    'side': side,
                    'decision': decision,
                    'note': note,
                    'reviewer': string(values.get('reviewer', '本机用户'), '审阅人', 80),
                    'page_ref': self._page_ref(page),
                    'created_at': now(),
                }
            project['updated_at'] = now()
            self.store.event(candidate, '记录页面决定', page_id, {'decision': decision})
        return self.detail(project_id)

    # ------------------------------------------------------------------ 审阅结论
    def review_pair(self, project_id, pair_id, values):
        verdict = values.get('verdict')
        require(verdict in ('equivalent', 'content_changed', 'text_changed', 'different', 'uncertain'),
                '审阅结论无效')
        note = string(values.get('note', ''), '审阅备注', 5000, True)
        reviewer = string(values.get('reviewer', '本机用户'), '审阅人', 80)
        self._load(project_id)
        with self.store.transaction() as candidate:
            project = candidate['cross_comparisons'][project_id]
            pair = self._find_pair(project, pair_id)
            left = self._page(candidate, pair['left_page_id'])
            right = self._page(candidate, pair['right_page_id'])
            entry = {
                'id': identity('review'),
                'verdict': verdict,
                'note': note,
                'reviewer': reviewer,
                'created_at': now(),
                'left_ref': self._page_ref(left),
                'right_ref': self._page_ref(right),
                'scores_snapshot': copy.deepcopy(pair.get('scores')),
            }
            pair.setdefault('review_history', []).append(copy.deepcopy(entry))
            pair['review'] = entry
            pair['match_status'] = 'confirmed'
            pair['updated_at'] = now()
            project['updated_at'] = now()
            self.store.event(candidate, '保存对比审阅结论', pair_id, {'verdict': verdict})
        return self.detail(project_id)

    def refresh_pair(self, project_id, pair_id):
        """任一方重扫/校订后，按当前版本重新计算视觉与文字证据；历史结论保留并标记失效。"""
        self._load(project_id)
        state = self.store.snapshot()
        project = state['cross_comparisons'][project_id]
        pair = next((item for item in project['pairs'] if item['id'] == pair_id), None)
        require(pair is not None, '配对不存在', 404)
        left = self._page(state, pair['left_page_id'])
        right = self._page(state, pair['right_page_id'])
        score = self._candidate_score(
            left, right, self._visual_features(left), self._visual_features(right)
        )
        with self.store.transaction() as candidate:
            project = candidate['cross_comparisons'][project_id]
            pair = self._find_pair(project, pair_id)
            if pair.get('review'):
                pair.setdefault('stale_reviews', []).append(copy.deepcopy(pair['review']))
                pair['review'] = None
            pair['scores'] = {
                'correlation': score['correlation'],
                'combined': score['combined'],
                'phash_distance': score['phash_distance'],
                'text_similarity': score['text_similarity'],
                'text_change_ratio': score['text_change_ratio'],
            }
            pair['visual'] = {key: value for key, value in score['visual_detail'].items()
                              if key != 'regions'}
            pair['visual_regions'] = score['visual_detail']['regions']
            pair['text'] = score['text_detail']
            pair['baseline_refs'] = {
                'left': self._page_ref(left),
                'right': self._page_ref(right),
            }
            pair['match_status'] = 'suggested_review' if pair['match_origin'] == 'auto' else pair['match_status']
            pair['updated_at'] = now()
            project['updated_at'] = now()
            self.store.event(candidate, '刷新配对证据', pair_id)
        return self.detail(project_id)

    def refresh_project(self, project_id):
        record = self._load(project_id)
        for pair in list(record['pairs']):
            if self._pair_stale(self.store.snapshot(), pair):
                self.refresh_pair(project_id, pair['id'])
        return self.detail(project_id)

    def delete(self, project_id):
        self._load(project_id)
        with self.store.transaction() as candidate:
            candidate['cross_comparisons'].pop(project_id)
            self.store.event(candidate, '删除双文档对比', project_id)
        return {'deleted': project_id}

    # ------------------------------------------------------------------ 版本绑定/失效
    def _pair_stale(self, state, pair):
        reasons = []
        for side, page_id in (('left', pair['left_page_id']), ('right', pair['right_page_id'])):
            page = state['pages'].get(page_id)
            if page is None:
                reasons.append({'side': side, 'reason': 'page_deleted'})
                continue
            ref = pair.get('baseline_refs', {}).get(side)
            if ref is None:
                continue
            if page['revision'] != ref['revision'] or page['current'] != ref['current']:
                reasons.append({'side': side, 'reason': 'image_revised',
                                'baseline_revision': ref['revision'], 'current_revision': page['revision']})
            ocr = page.get('ocr')
            if _digest_text(ocr.get('text') if ocr else None) != ref.get('ocr_text_digest'):
                reasons.append({'side': side, 'reason': 'ocr_revised'})
        return {'stale': bool(reasons), 'reasons': reasons}

    def _stale_summary(self, record):
        state = self.store.snapshot()
        stale_pairs = 0
        missing = 0
        for pair in record['pairs']:
            status = self._pair_stale(state, pair)
            if status['stale']:
                stale_pairs += 1
            if any(item['reason'] == 'page_deleted' for item in status['reasons']):
                missing += 1
        return {'stale_pair_count': stale_pairs, 'missing_page_count': missing}

    def _stale_detail(self, state, record):
        items = []
        for pair in record['pairs']:
            status = self._pair_stale(state, pair)
            if status['stale']:
                items.append({'pair_id': pair['id'], 'reasons': status['reasons'],
                              'review_preserved': pair.get('review') is not None or bool(pair.get('stale_reviews'))})
        decision_stale = []
        for page_id, decision in record.get('decisions', {}).items():
            page = state['pages'].get(page_id)
            if page is None:
                decision_stale.append({'page_id': page_id, 'reason': 'page_deleted'})
                continue
            ref = decision['page_ref']
            if page['revision'] != ref['revision'] or page['current'] != ref['current']:
                decision_stale.append({'page_id': page_id, 'reason': 'image_revised'})
            elif _digest_text(page.get('ocr', {}).get('text') if page.get('ocr') else None) != ref.get('ocr_text_digest'):
                decision_stale.append({'page_id': page_id, 'reason': 'ocr_revised'})
        return {'pairs': items, 'decisions': decision_stale}

    # ------------------------------------------------------------------ 差异可视化
    def pair_visual(self, project_id, pair_id):
        self._load(project_id)
        state = self.store.snapshot()
        project = state['cross_comparisons'][project_id]
        pair = next((item for item in project['pairs'] if item['id'] == pair_id), None)
        require(pair is not None, '配对不存在', 404)
        left = self._page(state, pair['left_page_id'])
        right = self._page(state, pair['right_page_id'])
        image, meta = compare_vision.diff_overlay(
            read_image(self.store.resolve(left['current'])),
            read_image(self.store.resolve(right['current'])),
        )
        return image, meta


def _digest_text(text):
    import hashlib
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()
