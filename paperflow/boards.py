from __future__ import annotations

import copy

import cv2
import numpy as np

from . import imaging
from .core import DomainError, identity, now, number, require, string

CANDIDATE_STATES = ('candidate', 'confirmed', 'blocked', 'extracted')
ACTIVE_STATES = ('candidate', 'confirmed', 'blocked')
# 已确认/已校正候选之间的可见内容重叠上限，超过则视为同一块内容被重复认领。
OVERLAP_LIMIT = 0.85


class Boards:
    """一张桌面照片对应一块票据板，管理多张票据候选与各自的校正出处。"""

    def __init__(self, store, documents):
        self.store = store
        self.documents = documents

    # ---------- 基础工具 ----------
    def get_board(self, board_id):
        board = self.store.snapshot().get('boards', {}).get(board_id)
        require(board is not None, '票据板不存在', 404)
        return copy.deepcopy(board)

    def require_board(self, state, board_id):
        board = state.get('boards', {}).get(board_id)
        require(board is not None, '票据板不存在', 404)
        return board

    def require_candidate(self, board, candidate_id):
        candidate = next((item for item in board['candidates'] if item['id'] == candidate_id), None)
        require(candidate is not None, '票据候选不存在', 404)
        return candidate

    def points(self, value):
        require(isinstance(value, list) and len(value) == 4, '候选轮廓需要四个角点')
        result = []
        for point in value:
            require(isinstance(point, list) and len(point) == 2, '角点需要 [x, y]')
            result.append([number(point[0], '角点x', 0, 16000), number(point[1], '角点y', 0, 16000)])
        return result

    def overlap(self, points, others, width, height, ignore_id=None):
        for other in others:
            if ignore_id and other['id'] == ignore_id:
                continue
            if other['status'] not in ('confirmed', 'extracted'):
                continue
            value = imaging.quad_iou(points, other['points'], width, height)
            if value >= OVERLAP_LIMIT:
                return other, value
        return None, 0.0

    def reject_overlap(self, other, value, action):
        if other is not None:
            require(False, f'与已确认候选「{other["name"]}」可见内容重叠 {round(value * 100)}%，{action}', 409, 'overlap')

    def summary(self, board):
        candidates = board['candidates']
        counts = {state: sum(1 for item in candidates if item['status'] == state) for state in CANDIDATE_STATES}
        result = {key: board[key] for key in board if key != 'candidates'}
        result['candidate_counts'] = counts
        result['candidate_count'] = len(candidates)
        return result

    # ---------- 票据板 ----------
    def create(self, page_id, values):
        page = self.store.get('pages', page_id)
        name = string(values.get('name', page['name'] + ' · 票据板'), '票据板名称', 160)
        state = self.store.snapshot()
        duplicate = next((board for board in state.get('boards', {}).values() if board['page_id'] == page_id), None)
        require(duplicate is None, '该照片已建立票据板，请在原板上调整候选', 409)
        image = imaging.read_image(self.store.resolve(page['current']))
        path = self.store.new_file('boards')
        imaging.save_image(path, image)
        record = {
            'id': identity('board'),
            'page_id': page_id,
            'document_id': page['document_id'],
            'name': name,
            'image': self.store.relative(path),
            'width': image.shape[1],
            'height': image.shape[0],
            'revision': 0,
            'created_at': now(),
            'updated_at': now(),
            'candidates': [],
        }
        with self.store.transaction() as state:
            state.setdefault('boards', {})[record['id']] = record
            self.store.event(state, '创建票据板', record['id'], {'page_id': page_id})
        return self.get_board(record['id'])

    def list_boards(self, document_id=None):
        boards = list(self.store.snapshot().get('boards', {}).values())
        if document_id:
            boards = [board for board in boards if board['document_id'] == document_id]
        boards.sort(key=lambda item: item['created_at'], reverse=True)
        return [self.summary(board) for board in boards]

    def delete(self, board_id):
        board = self.get_board(board_id)
        extracted = [item for item in board['candidates'] if item.get('result')]
        require(not extracted, '票据板已有校正结果，请先重置或删除对应页面后再删除板', 409)
        with self.store.transaction() as state:
            state['boards'].pop(board_id)
            self.store.event(state, '删除票据板', board_id)
        return {'deleted': board_id}

    def reset(self, board_id):
        """清空全部候选并断开结果页出处；已生成的页面本身保留。"""
        with self.store.transaction() as state:
            board = self.require_board(state, board_id)
            for candidate in board['candidates']:
                result = candidate.get('result')
                if result and result.get('page_id'):
                    page = state['pages'].get(result['page_id'])
                    if page is not None:
                        page.pop('board_candidate', None)
            board['candidates'] = []
            board['revision'] += 1
            board['updated_at'] = now()
            self.store.event(state, '重置票据板', board_id)
        return self.get_board(board_id)

    def detect(self, board_id, values):
        replace = bool(values.get('replace', False))
        with self.store.transaction() as state:
            board = self.require_board(state, board_id)
            image = imaging.read_image(self.store.resolve(board['image']))
            detection = imaging.detect_sheets(image)
            if replace:
                # 重新检测只替换自动/待确认候选，已确认、无法处理和已校正的人工结论保留
                retained = [item for item in board['candidates'] if item['status'] != 'candidate']
                board['candidates'] = retained
            added = []
            for detected in detection['candidates']:
                if any(imaging.quad_iou(detected['points'], item['points'], board['width'], board['height']) >= 0.9
                       for item in board['candidates']):
                    continue
                candidate = {
                    'id': identity('cand'),
                    'status': 'candidate',
                    'name': '票据候选 ' + str(len(board['candidates']) + 1),
                    'points': detected['points'],
                    'origin': 'auto',
                    'confidence': detected['confidence'],
                    'visible_ratio': detected['visible_ratio'],
                    'created_at': now(),
                    'updated_at': now(),
                    'result': None,
                    'block_reason': None,
                    'note': None,
                    'history': [{'time': now(), 'action': 'auto_detect', 'detail': {'source': detected['source'], 'shape': detected['shape']}}],
                }
                board['candidates'].append(candidate)
                added.append(candidate['id'])
            board['revision'] += 1
            board['updated_at'] = now()
            self.store.event(state, '自动寻找票据轮廓', board_id, {'added': len(added), 'replaced': replace})
        return self.get_board(board_id)

    # ---------- 候选维护 ----------
    def add_candidate(self, board_id, values):
        points = self.points(values.get('points'))
        name = string(values.get('name', '手描票据'), '候选名称', 160)
        with self.store.transaction() as state:
            board = self.require_board(state, board_id)
            points = imaging.ordered_quad(points).round(2).tolist()
            other, value = self.overlap(points, board['candidates'], board['width'], board['height'])
            self.reject_overlap(other, value, '不能重复认领同一块内容')
            candidate = {
                'id': identity('cand'),
                'status': 'candidate',
                'name': name,
                'points': points,
                'origin': 'manual',
                'confidence': None,
                'visible_ratio': None,
                'created_at': now(),
                'updated_at': now(),
                'result': None,
                'block_reason': None,
                'note': None,
                'history': [{'time': now(), 'action': 'manual_add', 'detail': {}}],
            }
            board['candidates'].append(candidate)
            board['revision'] += 1
            board['updated_at'] = now()
            self.store.event(state, '手动添加票据候选', candidate['id'], {'board_id': board_id})
        return self.get_board(board_id)

    def update_candidate(self, board_id, candidate_id, values, expected_revision=None):
        with self.store.transaction() as state:
            board = self.require_board(state, board_id)
            if expected_revision is not None:
                require(expected_revision == board['revision'], '票据板已被其他操作修改，请刷新后重试', 409, 'revision_conflict')
            candidate = self.require_candidate(board, candidate_id)
            require(candidate['status'] != 'extracted' or values.get('points') is None, '已校正票据的轮廓已固定，可在结果页重新校正生成新页面', 409)
            changed = {}
            if 'name' in values:
                candidate['name'] = string(values['name'], '候选名称', 160)
                changed['name'] = candidate['name']
            if 'note' in values:
                candidate['note'] = string(values['note'], '备注', 500, empty=True) or None
                changed['note'] = candidate['note']
            if values.get('points') is not None:
                points = imaging.ordered_quad(self.points(values['points'])).round(2).tolist()
                other, value = self.overlap(points, board['candidates'], board['width'], board['height'], candidate_id)
                self.reject_overlap(other, value, '调整会重复认领同一块内容')
                candidate['points'] = points
                changed['points'] = points
            candidate['updated_at'] = now()
            candidate['history'].append({'time': now(), 'action': 'edit', 'detail': changed})
            board['revision'] += 1
            board['updated_at'] = now()
            self.store.event(state, '调整票据候选', candidate_id, {'fields': list(changed)})
        return self.get_board(board_id)

    def confirm(self, board_id, candidate_id):
        with self.store.transaction() as state:
            board = self.require_board(state, board_id)
            candidate = self.require_candidate(board, candidate_id)
            require(candidate['status'] in ('candidate', 'confirmed'), '仅待确认候选可以确认范围', 409)
            other, value = self.overlap(candidate['points'], board['candidates'], board['width'], board['height'], candidate_id)
            self.reject_overlap(other, value, '请先拆开或合并候选')
            candidate['status'] = 'confirmed'
            candidate['block_reason'] = None
            candidate['updated_at'] = now()
            candidate['history'].append({'time': now(), 'action': 'confirm', 'detail': {}})
            board['revision'] += 1
            board['updated_at'] = now()
            self.store.event(state, '确认票据范围', candidate_id)
        return self.get_board(board_id)

    def mark_blocked(self, board_id, candidate_id, values):
        reason = string(values.get('reason'), '无法处理原因', 300)
        with self.store.transaction() as state:
            board = self.require_board(state, board_id)
            candidate = self.require_candidate(board, candidate_id)
            require(candidate['status'] != 'extracted', '已校正票据不再标记为无法处理', 409)
            candidate['status'] = 'blocked'
            candidate['block_reason'] = reason
            candidate['updated_at'] = now()
            candidate['history'].append({'time': now(), 'action': 'block', 'detail': {'reason': reason}})
            board['revision'] += 1
            board['updated_at'] = now()
            self.store.event(state, '标记无法处理票据', candidate_id, {'reason': reason})
        return self.get_board(board_id)

    def reopen(self, board_id, candidate_id):
        with self.store.transaction() as state:
            board = self.require_board(state, board_id)
            candidate = self.require_candidate(board, candidate_id)
            require(candidate['status'] in ('blocked', 'confirmed'), '当前状态无需重新打开', 409)
            candidate['status'] = 'candidate'
            candidate['block_reason'] = None
            candidate['updated_at'] = now()
            candidate['history'].append({'time': now(), 'action': 'reopen', 'detail': {}})
            board['revision'] += 1
            board['updated_at'] = now()
            self.store.event(state, '重新打开票据候选', candidate_id)
        return self.get_board(board_id)

    def remove_candidate(self, board_id, candidate_id):
        with self.store.transaction() as state:
            board = self.require_board(state, board_id)
            candidate = self.require_candidate(board, candidate_id)
            require(candidate['status'] != 'extracted', '已校正票据不能直接移除，请删除对应页面后再处理', 409)
            board['candidates'] = [item for item in board['candidates'] if item['id'] != candidate_id]
            board['revision'] += 1
            board['updated_at'] = now()
            self.store.event(state, '移除票据候选', candidate_id)
        return self.get_board(board_id)

    def split_candidate(self, board_id, candidate_id, values):
        points_list = values.get('candidates')
        require(isinstance(points_list, list) and 2 <= len(points_list) <= 6, '拆开需要提供 2–6 组角点')
        with self.store.transaction() as state:
            board = self.require_board(state, board_id)
            source = self.require_candidate(board, candidate_id)
            require(source['status'] != 'extracted', '已校正票据不能再拆开', 409)
            created = []
            for index, raw in enumerate(points_list):
                points = imaging.ordered_quad(self.points(raw)).round(2).tolist()
                other, value = self.overlap(points, board['candidates'] + created, board['width'], board['height'])
                self.reject_overlap(other, value, '新轮廓不能重复认领同一块内容')
                candidate = {
                    'id': identity('cand'),
                    'status': 'candidate',
                    'name': source['name'] + f' · 拆开{index + 1}',
                    'points': points,
                    'origin': 'manual',
                    'confidence': None,
                    'visible_ratio': None,
                    'created_at': now(),
                    'updated_at': now(),
                    'result': None,
                    'block_reason': None,
                    'note': None,
                    'history': [{'time': now(), 'action': 'split', 'detail': {'from': candidate_id}}],
                }
                board['candidates'].append(candidate)
                created.append(candidate)
            board['candidates'] = [item for item in board['candidates'] if item['id'] != candidate_id]
            board['revision'] += 1
            board['updated_at'] = now()
            self.store.event(state, '拆开票据候选', candidate_id, {'created': [item['id'] for item in created]})
        return self.get_board(board_id)

    def merge_candidates(self, board_id, values):
        candidate_ids = values.get('candidate_ids')
        require(isinstance(candidate_ids, list) and 2 <= len(candidate_ids) <= 10, '合并需要选择 2–10 个候选')
        require(len(set(candidate_ids)) == len(candidate_ids), '合并候选不能重复')
        with self.store.transaction() as state:
            board = self.require_board(state, board_id)
            targets = [self.require_candidate(board, identifier) for identifier in candidate_ids]
            require(all(item['status'] != 'extracted' for item in targets), '已校正票据不能参与合并', 409)
            merged_points = imaging.ordered_quad(convex_quad([point for item in targets for point in item['points']])).round(2).tolist()
            other, value = self.overlap(merged_points, board['candidates'], board['width'], board['height'])
            require(other is None or other['id'] in candidate_ids, '合并后的轮廓与其他已确认候选重叠', 409, 'overlap')
            merged = {
                'id': identity('cand'),
                'status': 'candidate',
                'name': string(values.get('name', '合并票据 · ' + ' / '.join(item['name'] for item in targets[:3])), '候选名称', 160),
                'points': merged_points,
                'origin': 'manual',
                'confidence': None,
                'visible_ratio': None,
                'created_at': now(),
                'updated_at': now(),
                'result': None,
                'block_reason': None,
                'note': None,
                'history': [{'time': now(), 'action': 'merge', 'detail': {'sources': candidate_ids}}],
            }
            board['candidates'] = [item for item in board['candidates'] if item['id'] not in candidate_ids]
            board['candidates'].append(merged)
            board['revision'] += 1
            board['updated_at'] = now()
            self.store.event(state, '合并票据候选', merged['id'], {'sources': candidate_ids})
        return self.get_board(board_id)

    # ---------- 校正、识别与出处 ----------
    def extract(self, board_id, candidate_id, values, expected_revision=None):
        target_document = values.get('document_id')
        with self.store.transaction() as state:
            board = self.require_board(state, board_id)
            if expected_revision is not None:
                require(expected_revision == board['revision'], '票据板已被其他操作修改，请刷新后重试', 409, 'revision_conflict')
            candidate = self.require_candidate(board, candidate_id)
            require(candidate['status'] in ('confirmed', 'candidate'), '只有已确认或待确认候选可以校正', 409)
            other, value = self.overlap(candidate['points'], board['candidates'], board['width'], board['height'], candidate_id)
            self.reject_overlap(other, value, '已拦截重复校正')
            document_id = target_document or board['document_id']
            require(document_id in state['documents'], '目标文档不存在', 404)
        image = imaging.read_image(self.store.resolve(board['image']))
        try:
            corrected, geometry = imaging.perspective(image, candidate['points'])
        except DomainError as error:
            return self._block(board_id, candidate_id, '轮廓无法校正：' + error.message)
        page = self.documents.add_image(
            document_id, corrected,
            candidate['name'] + ' · 校正',
            {'type': 'board_extract', 'board_id': board_id, 'candidate_id': candidate_id, 'source_page_id': board['page_id']},
        )
        run_ocr = bool(values.get('ocr'))
        ocr_meta = None
        ocr_failure = None
        if run_ocr:
            try:
                ocr_meta = self.documents.recognize(page['id'], {
                    'language': values.get('language', 'chi_sim+eng'),
                    'psm': values.get('psm', 3),
                    'minimum': 0,
                })['ocr']
            except DomainError as error:
                ocr_failure = error.message
        with self.store.transaction() as state:
            board = self.require_board(state, board_id)
            candidate = self.require_candidate(board, candidate_id)
            candidate['status'] = 'extracted'
            candidate['block_reason'] = None
            candidate['result'] = {
                'page_id': page['id'],
                'document_id': document_id,
                'document_name': state['documents'][document_id]['name'],
                'geometry': geometry,
                'points': candidate['points'],
                'ocr': bool(ocr_meta),
                'ocr_failure': ocr_failure,
                'created_at': now(),
            }
            candidate['updated_at'] = now()
            candidate['history'].append({'time': now(), 'action': 'extract', 'detail': {'page_id': page['id'], 'ocr': bool(ocr_meta)}})
            state['pages'][page['id']]['board_candidate'] = {'board_id': board_id, 'candidate_id': candidate_id}
            board['revision'] += 1
            board['updated_at'] = now()
            self.store.event(state, '校正票据并生成页面', page['id'], {'board_id': board_id, 'candidate_id': candidate_id})
        result_board = self.get_board(board_id)
        result_board['last_extract'] = {'candidate_id': candidate_id, 'page_id': page['id'], 'ocr_failure': ocr_failure}
        return result_board

    def reextract(self, board_id, candidate_id, values):
        board = self.get_board(board_id)
        candidate = self.require_candidate(board, candidate_id)
        require(candidate['status'] == 'extracted', '该候选尚未校正，不能重新校正', 409)
        values = dict(values)
        values.setdefault('document_id', candidate['result']['document_id'])
        values.setdefault('ocr', candidate['result'].get('ocr', False))
        old_page_id = candidate['result']['page_id']
        with self.store.transaction() as state:
            board_live = self.require_board(state, board_id)
            candidate_live = self.require_candidate(board_live, candidate_id)
            candidate_live['status'] = 'confirmed'
            result = candidate_live.pop('result')
            state['pages'][old_page_id].pop('board_candidate', None)
            candidate_live['history'].append({'time': now(), 'action': 'reextract_prepare', 'detail': {'previous_page_id': old_page_id}})
            board_live['revision'] += 1
        return self.extract(board_id, candidate_id, values)

    def _block(self, board_id, candidate_id, reason):
        with self.store.transaction() as state:
            board = self.require_board(state, board_id)
            candidate = self.require_candidate(board, candidate_id)
            candidate['status'] = 'blocked'
            candidate['block_reason'] = reason
            candidate['updated_at'] = now()
            candidate['history'].append({'time': now(), 'action': 'block', 'detail': {'reason': reason}})
            board['revision'] += 1
            board['updated_at'] = now()
            self.store.event(state, '票据校正失败并保留位置', candidate_id, {'reason': reason})
        return self.get_board(board_id)

    def recognize_candidate(self, board_id, candidate_id, values):
        board = self.get_board(board_id)
        candidate = self.require_candidate(board, candidate_id)
        require(candidate['status'] == 'extracted' and candidate.get('result'), '请先校正该票据再识别', 409)
        page_id = candidate['result']['page_id']
        page = self.documents.recognize(page_id, {
            'language': values.get('language', 'chi_sim+eng'),
            'psm': values.get('psm', 3),
            'minimum': values.get('minimum', 0),
        })
        with self.store.transaction() as state:
            board_live = self.require_board(state, board_id)
            candidate_live = self.require_candidate(board_live, candidate_id)
            candidate_live['result']['ocr'] = True
            candidate_live['result']['ocr_failure'] = None
            candidate_live['history'].append({'time': now(), 'action': 'ocr', 'detail': {'page_id': page_id}})
            board_live['revision'] += 1
        return self.get_board(board_id)

    def provenance(self, page_id):
        page = self.store.get('pages', page_id)
        link = page.get('board_candidate')
        if not link:
            return {'page_id': page_id, 'from_board': False}
        board = self.get_board(link['board_id'])
        candidate = self.require_candidate(board, link['candidate_id'])
        return {
            'page_id': page_id,
            'from_board': True,
            'board': self.summary(board),
            'candidate': candidate,
            'source_page_id': board['page_id'],
        }


def convex_quad(points):
    """取点集凸包并收敛为四个角点，用于合并候选的外轮廓。"""
    array = np.asarray(points, dtype=np.float32)
    hull = cv2.convexHull(array).reshape(-1, 2).astype(np.float32)
    if len(hull) > 4:
        perimeter = cv2.arcLength(hull, True)
        simplified = cv2.approxPolyDP(hull, 0.03 * perimeter, True).reshape(-1, 2)
        if len(simplified) >= 4:
            hull = simplified.astype(np.float32)
    if len(hull) != 4:
        hull = cv2.boxPoints(cv2.minAreaRect(array)).astype(np.float32)
    return hull[:4]
