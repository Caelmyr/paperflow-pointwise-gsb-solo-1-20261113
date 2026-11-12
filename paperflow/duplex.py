from __future__ import annotations

import copy

import numpy as np

from . import signature as sig
from .core import identity, now, require, string
from .imaging import read_image


class Duplex:
    """双面扫描配对工作台。

    会话状态独立保存于 state['pairing_sessions']，不改动文档页序与原图；
    只有在用户显式确认后，finalize 才会创建新文档。
    """

    def __init__(self, store):
        self.store = store
        # 页面特征在会话创建后不可变，按页对缓存特征匹配结果，避免重复 SIFT 计算。
        self._rotation_cache = {}

    def _best_rotation(self, left_id, right_id, signatures):
        key = tuple(sorted((left_id, right_id)))
        cached = self._rotation_cache.get(key)
        if cached is None:
            cached = sig.best_rotation(signatures[left_id], signatures[right_id])
            if len(self._rotation_cache) > 8192:
                self._rotation_cache.clear()
            self._rotation_cache[key] = cached
        return cached

    def _duplicate_groups(self, signatures):
        page_ids = list(signatures)
        unions = {page_id: page_id for page_id in page_ids}

        def find(identifier):
            while unions[identifier] != identifier:
                unions[identifier] = unions[unions[identifier]]
                identifier = unions[identifier]
            return identifier

        for index, left_id in enumerate(page_ids):
            for right_id in page_ids[index + 1:]:
                _, score, _ = self._best_rotation(left_id, right_id, signatures)
                if score >= sig.SCORE_HIGH:
                    root_left, root_right = find(left_id), find(right_id)
                    if root_left != root_right:
                        unions[root_right] = root_left
        grouped = {}
        for page_id in page_ids:
            grouped.setdefault(find(page_id), []).append(page_id)
        return [sorted(members) for members in grouped.values() if len(members) > 1]

    # ---------- 基础查询 ----------

    def _load(self, session_id):
        session = self.store.snapshot().get('pairing_sessions', {}).get(session_id)
        require(session is not None, '配对会话不存在，可能已被删除', 404, 'not_found')
        return session

    def _save(self, session, action, detail=None):
        session['updated_at'] = now()
        with self.store.transaction() as state:
            require(session['id'] in state.setdefault('pairing_sessions', {}), '会话已被删除', 409)
            state['pairing_sessions'][session['id']] = session
            self.store.event(state, action, session['id'], detail or {})

    def get(self, session_id):
        session = self._load(session_id)
        return self._public(session)

    def list_sessions(self):
        sessions = self.store.snapshot().get('pairing_sessions', {}).values()
        return [self._summary(session) for session in sorted(sessions, key=lambda item: item['created_at'], reverse=True)]

    def _summary(self, session):
        return {
            'id': session['id'],
            'name': session['name'],
            'document_id': session['document_id'],
            'status': session['status'],
            'created_at': session['created_at'],
            'updated_at': session['updated_at'],
            'page_total': len(session['pages']),
            'pair_total': len(session['pairs']),
            'confirmed_total': sum(1 for pair in session['pairs'] if pair['status'] == 'confirmed'),
        }

    # ---------- 会话创建 ----------

    def create(self, document_id, values):
        document = self.store.get('documents', document_id)
        name = string(values.get('name', document['name'] + ' · 双面配对'), '会话名称', 160)
        state = self.store.snapshot()
        require(document['page_ids'], '文档没有页面，无法建立配对会话', 409)
        pages = []
        signatures = {}
        descriptor_paths = {}
        for index, page_id in enumerate(document['page_ids']):
            page = state['pages'][page_id]
            feature = sig.page_signature(read_image(self.store.resolve(page['current'])))
            descriptor_paths[page_id] = sig.build_cache(self.store, page_id, feature)
            signatures[page_id] = feature
            pages.append(self._page_entry(page, index, feature, descriptor_paths[page_id]))
        batches = self._build_batches(document['page_ids'], state)
        duplicate_groups = self._duplicate_groups(signatures)
        session = {
            'id': identity('pairing'),
            'document_id': document_id,
            'name': name,
            'status': 'open',
            'created_at': now(),
            'updated_at': now(),
            'revision': 0,
            'pages': pages,
            'batches': batches,
            'pairs': [],
            'ignored': [],
            'role_map': self._default_roles(batches),
            'orientation_map': {},
            'finalized_document_id': None,
            'analysis': None,
        }
        analysis = self._analyze(session, signatures)
        session['analysis'] = analysis
        with self.store.transaction() as candidate:
            candidate.setdefault('pairing_sessions', {})[session['id']] = session
            self.store.event(candidate, '创建双面配对会话', session['id'], {'document_id': document_id, 'pages': len(pages)})
        return self._public(session)

    def _page_entry(self, page, index, feature, descriptor_path):
        source = page.get('source') or {}
        return {
            'id': page['id'],
            'name': page['name'],
            'index': index,
            'width': page['width'],
            'height': page['height'],
            'source_name': source.get('name'),
            'source_type': source.get('type'),
            'source_page': source.get('page'),
            'source_file_id': page.get('source_file_id'),
            'batch_key': self._batch_key(page),
            'blank': feature['blank'],
            'blank_score': feature['blank_score'],
            'light_ratio': feature['light_ratio'],
            'dark_ratio': feature['dark_ratio'],
            'signature': sig.light_view(feature),
            'descriptor_path': descriptor_path,
            'created_at': page['created_at'],
        }

    def _batch_key(self, page):
        source = page.get('source') or {}
        # 同一次扫描批次通常保留相同来源名（多页 PDF/TIFF），逐张导入的图片
        # 只要文件名一致也归入同一批次；source_file_id 只用于来源留存不参与分组。
        if source.get('name'):
            return 'name:' + source['name']
        if page.get('source_file_id'):
            return 'file:' + page['source_file_id']
        return 'single'

    def _build_batches(self, page_ids, state):
        order = []
        counts = {}
        first_index = {}
        kinds = {}
        for index, page_id in enumerate(page_ids):
            page = state['pages'][page_id]
            key = self._batch_key(page)
            if key not in counts:
                order.append(key)
                first_index[key] = index
                kinds[key] = self._batch_kind(page)
            counts[key] = counts.get(key, 0) + 1
        return [
            {
                'key': key,
                'count': counts[key],
                'first_index': first_index[key],
                'kind': kinds[key],
                'label': state['pages'][page_ids[first_index[key]]].get('source', {}).get('name') or key,
            }
            for key in order
        ]

    def _batch_kind(self, page):
        source = page.get('source') or {}
        source_type = source.get('type')
        if source.get('kind') == 'blank' or source_type == 'generated':
            return 'generated'
        if source_type in ('pdf', 'tiff', 'image'):
            return 'scan'
        return 'other'

    def _default_roles(self, batches):
        roles = {}
        scan_batches = [batch for batch in batches if batch['kind'] == 'scan']
        ordered = sorted(scan_batches, key=lambda batch: batch['first_index'])
        if len(ordered) >= 2:
            roles[ordered[0]['key']] = 'front'
            roles[ordered[1]['key']] = 'back'
            for batch in ordered[2:]:
                roles[batch['key']] = 'supplement'
        else:
            for batch in ordered:
                roles[batch['key']] = 'front'
        for batch in batches:
            roles.setdefault(batch['key'], 'supplement')
        return roles

    # ---------- 分析与建议 ----------

    def _signatures(self, session):
        """从持久化的轻量特征恢复完整签名，并从二进制缓存载入 SIFT 描述子。"""
        result = {}
        for page in session['pages']:
            signature = copy.deepcopy(page['signature'])
            signature['descriptors'] = sig.load_cache(self.store, page['descriptor_path'])
            result[page['id']] = signature
        return result

    def analyze(self, session_id):
        session = self._load(session_id)
        analysis = self._analyze(session, self._signatures(session))
        session['analysis'] = analysis
        self._save(session, '重新分析配对建议')
        return self._public(session)

    def _analyze(self, session, signatures):
        pages = {page['id']: page for page in session['pages']}
        front_batches = {key for key, role in session['role_map'].items() if role == 'front'}
        back_batches = {key for key, role in session['role_map'].items() if role == 'back'}
        front_ids = [page['id'] for page in session['pages'] if page['batch_key'] in front_batches]
        back_ids = [page['id'] for page in session['pages'] if page['batch_key'] in back_batches]
        supplement_ids = [page['id'] for page in session['pages'] if session['role_map'].get(page['batch_key']) == 'supplement']
        confirmed_keys = {(pair['front_id'], pair['back_id']) for pair in session['pairs'] if pair['status'] == 'confirmed'}
        anchors = [pair for pair in session['pairs'] if pair['status'] == 'confirmed' and pair.get('anchor')]
        suggestions = self._ordinal_suggestions(front_ids, back_ids, signatures, anchors, session, pages)
        duplicates = self._duplicate_summary(self._duplicate_groups(signatures), pages, session)
        blanks = [self._page_card(pages[page_id]) for page_id in signatures if pages[page_id]['blank']]
        orientation = self._orientation_summary(session, front_ids, back_ids, signatures)
        unpaired = self._unpaired(session, pages, suggestions)
        warnings = self._analysis_warnings(session, suggestions, duplicates, blanks, orientation, front_ids, back_ids)
        return {
            'generated_at': now(),
            'front_batch_keys': sorted(front_batches),
            'back_batch_keys': sorted(back_batches),
            'front_count': len(front_ids),
            'back_count': len(back_ids),
            'supplement_count': len(supplement_ids),
            'suggestions': suggestions,
            'duplicate_groups': duplicates,
            'blank_pages': blanks,
            'unpaired_pages': unpaired,
            'orientation': orientation,
            'warnings': warnings,
            'anchors': len(anchors),
            'confident_pairs': sum(1 for item in suggestions if item['confidence'] == 'high'),
        }

    def _ordinal_suggestions(self, front_ids, back_ids, signatures, anchors, session, pages):
        occupied_front = {pair['front_id'] for pair in session['pairs'] if pair['status'] == 'confirmed'}
        occupied_back = {pair['back_id'] for pair in session['pairs'] if pair['status'] == 'confirmed'}
        ignored = {item['page_id'] for item in session['ignored']}
        free_front = [page_id for page_id in front_ids if page_id not in occupied_front and page_id not in ignored]
        free_back = [page_id for page_id in back_ids if page_id not in occupied_back and page_id not in ignored]
        mapping = {}
        method = 'ordinal'
        if anchors and free_front and free_back:
            anchor_points = sorted(
                (front_ids.index(pair['front_id']), back_ids.index(pair['back_id']))
                for pair in anchors if pair['front_id'] in front_ids and pair['back_id'] in back_ids
            )
            for front_id in free_front:
                target = self._interpolate(front_ids.index(front_id), anchor_points, len(back_ids))
                if target is not None:
                    mapping[front_id] = back_ids[target]
            method = 'anchor'
        if not mapping:
            for index, front_id in enumerate(free_front):
                if index < len(free_back):
                    mapping[front_id] = free_back[index]
        suggestions = []
        for front_id in free_front:
            back_id = mapping.get(front_id)
            if back_id is None or back_id not in free_back:
                continue
            turn, image_score, candidates = self._best_rotation(front_id, back_id, signatures)
            ordinal_score = self._ordinal_confidence(front_ids, back_ids, front_id, back_id, method)
            score = round(0.45 * image_score + 0.55 * ordinal_score, 4)
            candidate_cards = [
                {'page_id': candidate_id, 'score': candidate_score, 'turn': candidate_turn}
                for candidate_id, candidate_turn, candidate_score in self._top_candidates(front_id, free_back, signatures)
            ]
            suggestions.append({
                'front_id': front_id,
                'back_id': back_id,
                'score': score,
                'image_score': image_score,
                'ordinal_score': ordinal_score,
                'confidence': sig.score_band(score),
                'method': method,
                'turn': turn,
                'candidates': candidate_cards,
                'front': self._page_card(pages[front_id]),
                'back': self._page_card(pages[back_id]),
            })
        return sorted(suggestions, key=lambda item: item['front']['index'])

    def _interpolate(self, front_index, anchor_points, back_length):
        if not anchor_points:
            return None
        for left, right in zip(anchor_points, anchor_points[1:]):
            left_front, left_back = left
            right_front, right_back = right
            if left_front < front_index < right_front and right_front != left_front:
                ratio = (front_index - left_front) / (right_front - left_front)
                target = round(left_back + ratio * (right_back - left_back))
                return min(back_length - 1, max(0, target))
        first_front, first_back = anchor_points[0]
        last_front, last_back = anchor_points[-1]
        if front_index <= first_front:
            target = first_back + (front_index - first_front)
        else:
            target = last_back + (front_index - last_front)
        return min(back_length - 1, max(0, target))

    def _ordinal_confidence(self, front_ids, back_ids, front_id, back_id, method):
        front_index = front_ids.index(front_id)
        back_index = back_ids.index(back_id)
        if method == 'anchor':
            span = max(len(front_ids), len(back_ids), 1)
            drift = abs(front_index / max(1, len(front_ids)) - back_index / max(1, len(back_ids)))
            return round(max(0.55, 1 - drift / span - 0.05), 4)
        if len(front_ids) == len(back_ids):
            return 0.9
        drift = abs(front_index - back_index) / max(len(front_ids), len(back_ids), 1)
        return round(max(0.4, 0.8 - drift), 4)

    def _top_candidates(self, front_id, back_pool, signatures, limit=3):
        ranked = []
        for back_id in back_pool:
            turn, score, _ = self._best_rotation(front_id, back_id, signatures)
            ranked.append((back_id, turn, score))
        return sorted(ranked, key=lambda item: item[2], reverse=True)[:limit]

    def _duplicate_summary(self, groups, pages, session):
        ignored = {item['page_id'] for item in session['ignored']}
        result = []
        for members in groups:
            cards = []
            for page_id in members:
                card = self._page_card(pages[page_id])
                card['ignored'] = page_id in ignored
                cards.append(card)
            result.append({'page_ids': members, 'pages': cards, 'size': len(members)})
        return sorted(result, key=lambda group: group['pages'][0]['index'])

    def _unpaired(self, session, pages, suggestions):
        paired = {pair['front_id'] for pair in session['pairs'] if pair['status'] == 'confirmed'}
        paired.update(pair['back_id'] for pair in session['pairs'] if pair['status'] == 'confirmed')
        suggested = set()
        for item in suggestions:
            suggested.add(item['front_id'])
            suggested.add(item['back_id'])
        ignored = {item['page_id'] for item in session['ignored']}
        cards = []
        for page in session['pages']:
            page_id = page['id']
            if page_id in paired or page_id in ignored:
                continue
            card = self._page_card(page)
            card['has_suggestion'] = page_id in suggested
            cards.append(card)
        return cards

    def _orientation_summary(self, session, front_ids, back_ids, signatures):
        per_batch = {}
        for page_id in back_ids[:12]:
            page = next(item for item in session['pages'] if item['id'] == page_id)
            vote = self._layout_orientation(page['signature'])
            per_batch.setdefault(page['batch_key'], []).append(vote)
        batch_turns = {}
        for key, votes in per_batch.items():
            counts = {}
            for vote in votes:
                counts[vote] = counts.get(vote, 0) + 1
            batch_turns[key] = max(counts, key=counts.get) if counts else None
        overrides = session['orientation_map']
        return {
            'batch_turns': batch_turns,
            'overrides': overrides,
            'note': '未启用 OCR 时依据版面投影推断朝向，只能区分横竖，180° 倒置请在确认配对时人工指定。',
        }

    def _layout_orientation(self, feature):
        grid = np.asarray(feature['orientation'], dtype=np.uint8)
        binary = (grid < 200).astype(np.uint8)
        horizontal = float(np.diff(binary.astype(np.int8), axis=1).clip(0).sum())
        vertical = float(np.diff(binary.astype(np.int8), axis=0).clip(0).sum())
        if max(horizontal, vertical) == 0:
            return 0
        return 0 if horizontal >= vertical else 1

    def _analysis_warnings(self, session, suggestions, duplicates, blanks, orientation, front_ids, back_ids):
        warnings = []
        if not front_ids or not back_ids:
            warnings.append({'code': 'missing_role_batch', 'message': '需要先在批次设置中指定正面批次与背面批次，才能给出正反配对建议。'})
        if len(front_ids) != len(back_ids):
            warnings.append({'code': 'batch_size_mismatch', 'message': f'正面 {len(front_ids)} 页、背面 {len(back_ids)} 页，数量不一致，存在缺扫或补拍页，请核对未配对清单。'})
        if not any(pair.get('anchor') for pair in session['pairs'] if pair['status'] == 'confirmed'):
            warnings.append({'code': 'no_anchor', 'message': '尚无人工锚点，初始建议仅按批次内顺序给出；请先确认几个有把握的配对，系统会据此重新对齐。'})
        weak = [item for item in suggestions if item['confidence'] in ('low', 'none', 'middle')]
        if weak:
            warnings.append({'code': 'weak_suggestions', 'message': f'{len(weak)} 个建议相似度不足或顺序漂移较大，已单独标出，请人工核对。', 'count': len(weak)})
        if duplicates:
            warnings.append({'code': 'duplicates', 'message': f'发现 {len(duplicates)} 组重复扫描，重复页不会自动删除，请在重复清单中选择保留或忽略。', 'count': len(duplicates)})
        if blanks:
            warnings.append({'code': 'blank_pages', 'message': f'发现 {len(blanks)} 个疑似空白页，系统不会自动丢弃；空白页可能是有效页背，请显式保留或忽略。', 'count': len(blanks)})
        return warnings

    def _page_card(self, page):
        return {
            'id': page['id'],
            'name': page['name'],
            'index': page['index'],
            'batch_key': page['batch_key'],
            'source_name': page['source_name'],
            'source_page': page['source_page'],
            'blank': page['blank'],
            'blank_score': page['blank_score'],
            'width': page['width'],
            'height': page['height'],
        }

    # ---------- 配对确认与撤销（含影响分析） ----------

    def candidates(self, session_id, page_id):
        session = self._load(session_id)
        require(any(page['id'] == page_id for page in session['pages']), '页面不属于该配对会话', 404)
        signatures = self._signatures(session)
        pages = {page['id']: page for page in session['pages']}
        role_map = session['role_map']
        current_page = pages[page_id]
        wanted_role = 'back' if role_map.get(current_page['batch_key']) == 'front' else 'front'
        # 优先列出对侧角色批次；补拍等无角色页也纳入候选，便于人工填补缺扫位置。
        role_pool = [page['id'] for page in session['pages'] if role_map.get(page['batch_key']) == wanted_role]
        pool = role_pool or [page['id'] for page in session['pages'] if page['id'] != page_id]
        occupied = self._occupied(session)
        ignored = {item['page_id'] for item in session['ignored']}
        ranked = []
        for candidate_id in pool:
            if candidate_id == page_id or candidate_id in ignored:
                continue
            turn, score, _ = self._best_rotation(page_id, candidate_id, signatures)
            card = self._page_card(pages[candidate_id])
            card.update({'score': score, 'turn': turn, 'occupied': candidate_id in occupied, 'confidence': sig.score_band(score)})
            ranked.append(card)
        return sorted(ranked, key=lambda item: item['score'], reverse=True)[:8]

    def _occupied(self, session):
        occupied = set()
        for pair in session['pairs']:
            if pair['status'] == 'confirmed':
                occupied.add(pair['front_id'])
                occupied.add(pair['back_id'])
        return occupied

    def confirm_preview(self, session_id, values):
        session = self._load(session_id)
        front_id, back_id, turn = self._validate_pair_values(session, values)
        impact = self._confirm_impact(session, front_id, back_id)
        return {'front_id': front_id, 'back_id': back_id, 'turn': turn, **impact}

    def _validate_pair_values(self, session, values):
        require(isinstance(values, dict), '参数必须是对象')
        front_id = string(values.get('front_id'), '正面页面', 80)
        back_id = string(values.get('back_id'), '背面页面', 80)
        page_ids = {page['id'] for page in session['pages']}
        require(front_id in page_ids and back_id in page_ids, '配对页面不属于该会话', 404)
        require(front_id != back_id, '正反页不能是同一页面', 409)
        turn = sig.validate_turn(values.get('turn', 0))
        ignored = {item['page_id'] for item in session['ignored']}
        require(front_id not in ignored and back_id not in ignored, '页面已被忽略，请先取消忽略再配对', 409)
        return front_id, back_id, turn

    def _confirm_impact(self, session, front_id, back_id):
        existing = next((pair for pair in session['pairs'] if pair['status'] == 'confirmed' and (pair['front_id'] == front_id or pair['back_id'] == back_id or pair['front_id'] == back_id or pair['back_id'] == front_id)), None)
        signatures = self._signatures(session)
        turn, image_score, _ = self._best_rotation(front_id, back_id, signatures)
        baseline = copy.deepcopy(session)
        trial = copy.deepcopy(session)
        self._apply_confirmation(trial, front_id, back_id, turn, anchor=True)
        before = self._analyze(baseline, signatures)
        after = self._analyze(trial, signatures)
        affected = self._diff_suggestions(before['suggestions'], after['suggestions'])
        displaced = []
        if existing and (existing['front_id'] != front_id or existing['back_id'] != back_id):
            pages = {page['id']: page for page in session['pages']}
            displaced.append({
                'front': self._page_card(pages[existing['front_id']]),
                'back': self._page_card(pages[existing['back_id']]),
            })
        return {
            'replaces_existing': existing is not None,
            'displaced_pairs': displaced,
            'suggestions_added': affected['added'],
            'suggestions_removed': affected['removed'],
            'suggestions_changed': affected['changed'],
            'unpaired_before': len(before['unpaired_pages']),
            'unpaired_after': len(after['unpaired_pages']),
            'image_score': image_score,
            'image_turn': turn,
        }

    def _diff_suggestions(self, before, after):
        before_map = {item['front_id']: item for item in before}
        after_map = {item['front_id']: item for item in after}
        added = sorted(set(after_map) - set(before_map))
        removed = sorted(set(before_map) - set(after_map))
        changed = []
        for front_id in set(before_map) & set(after_map):
            if before_map[front_id]['back_id'] != after_map[front_id]['back_id'] or before_map[front_id]['method'] != after_map[front_id]['method']:
                changed.append({
                    'front_id': front_id,
                    'old_back_id': before_map[front_id]['back_id'],
                    'new_back_id': after_map[front_id]['back_id'],
                    'old_method': before_map[front_id]['method'],
                    'new_method': after_map[front_id]['method'],
                })
        return {'added': added, 'removed': removed, 'changed': changed}

    def confirm(self, session_id, values):
        session = self._load(session_id)
        front_id, back_id, turn = self._validate_pair_values(session, values)
        anchor = bool(values.get('anchor', True))
        self._apply_confirmation(session, front_id, back_id, turn, anchor=anchor)
        session['analysis'] = self._analyze(session, self._signatures(session))
        self._save(session, '确认正反配对', {'front_id': front_id, 'back_id': back_id, 'anchor': anchor})
        return self._public(session)

    def _apply_confirmation(self, session, front_id, back_id, turn, anchor):
        for pair in session['pairs']:
            if pair['status'] != 'confirmed':
                continue
            if pair['front_id'] == front_id or pair['back_id'] == back_id or pair['front_id'] == back_id or pair['back_id'] == front_id:
                pair['status'] = 'superseded'
                pair['superseded_at'] = now()
        record = {
            'id': identity('pair'),
            'front_id': front_id,
            'back_id': back_id,
            'turn': turn,
            'status': 'confirmed',
            'anchor': anchor,
            'created_at': now(),
            'updated_at': now(),
            'history': [],
        }
        session['pairs'].append(record)

    def unconfirm(self, session_id, values):
        session = self._load(session_id)
        pair_id = string(values.get('pair_id'), '配对标识', 80)
        pair = next((item for item in session['pairs'] if item['id'] == pair_id), None)
        require(pair is not None, '配对不存在', 404)
        require(pair['status'] == 'confirmed', '配对已不是确认状态', 409)
        impact = self._unconfirm_impact(session, pair)
        pair['status'] = 'revoked'
        pair['revoked_at'] = now()
        session['analysis'] = self._analyze(session, self._signatures(session))
        self._save(session, '撤销配对', {'pair_id': pair_id, **impact})
        return {'impact': impact, 'session': self._public(session)}

    def _unconfirm_impact(self, session, pair):
        signatures = self._signatures(session)
        before = session['analysis'] or self._analyze(session, signatures)
        trial = copy.deepcopy(session)
        target = next(item for item in trial['pairs'] if item['id'] == pair['id'])
        target['status'] = 'revoked'
        after = self._analyze(trial, signatures)
        return self._diff_suggestions(before['suggestions'], after['suggestions'])

    def set_turn(self, session_id, values):
        session = self._load(session_id)
        pair_id = string(values.get('pair_id'), '配对标识', 80)
        turn = sig.validate_turn(values.get('turn'))
        pair = next((item for item in session['pairs'] if item['id'] == pair_id and item['status'] == 'confirmed'), None)
        require(pair is not None, '确认配对不存在', 404)
        pair['turn'] = turn
        pair['updated_at'] = now()
        self._save(session, '调整背面旋转', {'pair_id': pair_id, 'turn': turn})
        return self._public(session)

    # ---------- 忽略 / 保留（重复、空白、补拍） ----------

    def ignore(self, session_id, values):
        session = self._load(session_id)
        page_id = string(values.get('page_id'), '页面标识', 80)
        reason = string(values.get('reason', '人工忽略'), '忽略原因', 200)
        page = next((item for item in session['pages'] if item['id'] == page_id), None)
        require(page is not None, '页面不属于该会话', 404)
        paired = self._occupied(session)
        require(page_id not in paired, '页面已在确认配对中，请先撤销配对再忽略', 409)
        # 疑似空白页绝不自动删除：忽略时必须显式确认，且该确认会被记录到会话历史。
        acknowledge_blank = bool(values.get('acknowledge_blank'))
        if page['blank']:
            require(acknowledge_blank, '该页疑似空白，忽略前请勾选“确认空白页可忽略”，系统不会自动删除任何页面', 409)
        if not any(item['page_id'] == page_id for item in session['ignored']):
            session['ignored'].append({
                'page_id': page_id,
                'reason': reason,
                'blank_acknowledged': page['blank'],
                'ignored_at': now(),
            })
        session['analysis'] = self._analyze(session, self._signatures(session))
        self._save(session, '忽略页面', {'page_id': page_id, 'reason': reason, 'blank_acknowledged': page['blank']})
        return self._public(session)

    def keep(self, session_id, values):
        session = self._load(session_id)
        page_id = string(values.get('page_id'), '页面标识', 80)
        before = len(session['ignored'])
        session['ignored'] = [item for item in session['ignored'] if item['page_id'] != page_id]
        require(len(session['ignored']) != before, '页面未被忽略', 409)
        session['analysis'] = self._analyze(session, self._signatures(session))
        self._save(session, '保留页面', {'page_id': page_id})
        return self._public(session)

    def set_roles(self, session_id, values):
        session = self._load(session_id)
        roles = values.get('roles')
        require(isinstance(roles, dict), '批次角色必须是对象')
        valid_roles = {'front', 'back', 'supplement'}
        keys = {batch['key'] for batch in session['batches']}
        require(set(roles).issubset(keys), '包含未知批次', 404)
        for role in roles.values():
            require(role in valid_roles, '批次角色无效')
        trial = dict(session['role_map'])
        trial.update(roles)
        front_keys = [key for key, role in trial.items() if role == 'front']
        back_keys = [key for key, role in trial.items() if role == 'back']
        require(len(front_keys) <= 1, '只能指定一个正面批次；请先把原正面批次改为补拍或背面', 409)
        require(len(back_keys) <= 1, '只能指定一个背面批次；请先把原背面批次改为补拍或正面', 409)
        session['role_map'] = trial
        session['analysis'] = self._analyze(session, self._signatures(session))
        self._save(session, '调整扫描批次角色', {'roles': roles})
        return self._public(session)

    # ---------- 确认后生成新文档 ----------

    def finalize_check(self, session_id):
        session = self._load(session_id)
        return self._finalize_issues(session)

    def _finalize_issues(self, session):
        confirmed = [pair for pair in session['pairs'] if pair['status'] == 'confirmed']
        used = set()
        issues = []
        for pair in confirmed:
            if pair['front_id'] in used or pair['back_id'] in used:
                issues.append({'code': 'page_reused', 'message': '存在页面被多个确认配对引用，请撤销重复配对。'})
            used.add(pair['front_id'])
            used.add(pair['back_id'])
        ignored = {item['page_id'] for item in session['ignored']}
        stray = []
        for page in session['pages']:
            page_id = page['id']
            if page_id in used or page_id in ignored:
                continue
            card = self._page_card(page)
            stray.append(card)
        if stray:
            issues.append({'code': 'unresolved_pages', 'message': f'还有 {len(stray)} 页未配对也未忽略，确认前必须逐项处理。', 'pages': stray})
        unacknowledged_blanks = [
            item['page_id'] for item in session['ignored']
            if next(page for page in session['pages'] if page['id'] == item['page_id'])['blank'] and not item.get('blank_acknowledged')
        ]
        if unacknowledged_blanks:
            issues.append({'code': 'blank_unacknowledged', 'message': '存在未显式确认即忽略的疑似空白页，请取消忽略并重新确认。', 'page_ids': unacknowledged_blanks})
        if not confirmed:
            issues.append({'code': 'no_confirmed_pairs', 'message': '还没有任何确认配对，无法生成阅读文档。'})
        return {
            'ready': not issues,
            'issues': issues,
            'confirmed_pairs': len(confirmed),
            'ignored_pages': len(ignored),
            'ignored_blank_pages': sum(1 for item in session['ignored'] if next(page for page in session['pages'] if page['id'] == item['page_id'])['blank']),
            'total_pages': len(session['pages']),
            'accounted_pages': len(used) + len(ignored),
        }

    def finalize(self, session_id, documents, values):
        session = self._load(session_id)
        require(session['status'] == 'open', '会话已结束，不能重复生成；可重新打开为新会话', 409)
        check = self._finalize_issues(session)
        require(check['ready'], '仍有未处理页面或配对冲突，无法生成文档：' + '；'.join(issue['message'] for issue in check['issues']), 409)
        state = self.store.snapshot()
        source_document = state['documents'][session['document_id']]
        name = string(values.get('name', session['name'] + ' · 阅读顺序'), '新文档名称', 160)
        collection_id = values.get('collection_id') or source_document['collection_id']
        self.store.get('collections', collection_id)
        ordered = self._reading_order(session)
        new_document_id = identity('document')
        with self.store.transaction() as candidate:
            require(new_document_id not in candidate['documents'], '文档标识冲突', 500)
            candidate['documents'][new_document_id] = {
                'id': new_document_id,
                'collection_id': collection_id,
                'name': name,
                'category': source_document.get('category', '未分类'),
                'page_ids': [],
                'created_at': now(),
                'updated_at': now(),
                'pairing_session_id': session['id'],
            }
        # 复制（而非移动）原始页面：原扫描批次、原图与各页校订始终独立保留。
        created_page_ids = documents.copy_pages([item['page_id'] for item in ordered], new_document_id, 0)
        finalized_order = [
            {'page_id': page_id, 'role': item['role'], 'turn': item['turn'], 'source_pair_id': item['pair_id']}
            for page_id, item in zip(created_page_ids, ordered)
        ]
        # 背面倒着进纸时，在新文档的副本上旋转，原始扫描页与其处理历史保持不变。
        for page_id, item in zip(created_page_ids, ordered):
            if item['role'] == 'back' and item['turn']:
                documents.apply(page_id, 'rotate', {'angle': 90 * item['turn']})
        with self.store.transaction() as candidate:
            target = candidate['documents'][new_document_id]
            target['page_ids'] = created_page_ids
            target['updated_at'] = now()
            pairing = candidate['pairing_sessions'][session['id']]
            require(pairing['status'] == 'open', '会话状态已变化，请刷新后重试', 409)
            pairing['status'] = 'finalized'
            pairing['finalized_document_id'] = new_document_id
            pairing['finalized_at'] = now()
            pairing['finalized_order'] = finalized_order
            self.store.event(candidate, '生成阅读顺序文档', new_document_id, {'session_id': session['id'], 'pages': len(ordered)})
        session['status'] = 'finalized'
        session['finalized_document_id'] = new_document_id
        session['finalized_order'] = finalized_order
        return {'document': documents.document(new_document_id), 'session': self._summary(session), 'order': finalized_order}

    def _reading_order(self, session):
        confirmed = [pair for pair in session['pairs'] if pair['status'] == 'confirmed']
        pages = {page['id']: page for page in session['pages']}
        ordered_pairs = sorted(confirmed, key=lambda pair: pages[pair['front_id']]['index'])
        sequence = []
        for pair in ordered_pairs:
            sequence.append({'page_id': pair['front_id'], 'role': 'front', 'turn': 0, 'pair_id': pair['id']})
            sequence.append({'page_id': pair['back_id'], 'role': 'back', 'turn': pair['turn'], 'pair_id': pair['id']})
        return sequence

    def reopen(self, session_id, values):
        """复制出一个新会话继续调整；已确认配对完整继承，原会话保留。"""
        session = self._load(session_id)
        name = string(values.get('name', session['name'] + ' · 继续调整'), '新会话名称', 160)
        new_session = copy.deepcopy(session)
        new_session['id'] = identity('pairing')
        new_session['name'] = name
        new_session['status'] = 'open'
        new_session['created_at'] = now()
        new_session['updated_at'] = now()
        new_session['finalized_document_id'] = None
        new_session.pop('finalized_at', None)
        new_session['reopened_from'] = session['id']
        for pair in new_session['pairs']:
            if pair['status'] == 'confirmed':
                pair['anchor'] = True
        with self.store.transaction() as state:
            state.setdefault('pairing_sessions', {})[new_session['id']] = new_session
            self.store.event(state, '重新打开配对会话', new_session['id'], {'from': session['id']})
        return self._public(new_session)

    def delete(self, session_id):
        session = self._load(session_id)
        with self.store.transaction() as state:
            require(session_id in state.get('pairing_sessions', {}), '会话不存在', 404)
            state['pairing_sessions'].pop(session_id)
            self.store.event(state, '删除配对会话', session_id)
        self._cleanup_orphan_caches()
        return {'deleted': session_id}

    def _cleanup_orphan_caches(self):
        """回收不再被任何会话引用的描述子缓存；仍被引用的保留共享。"""
        state = self.store.snapshot()
        referenced = set()
        for other in state.get('pairing_sessions', {}).values():
            for page in other.get('pages', []):
                if page.get('descriptor_path'):
                    referenced.add(page['descriptor_path'])
        cache_dir = self.store.root / 'paircache'
        if not cache_dir.is_dir():
            return
        for path in cache_dir.iterdir():
            if not path.is_file():
                continue
            relative = self.store.relative(path)
            if relative not in referenced:
                path.unlink(missing_ok=True)

    # ---------- 对外视图 ----------

    def _public(self, session):
        pages = {page['id']: page for page in session['pages']}
        pairs = []
        for pair in session['pairs']:
            if pair['status'] != 'confirmed':
                continue
            pairs.append({
                'id': pair['id'],
                'front_id': pair['front_id'],
                'back_id': pair['back_id'],
                'turn': pair['turn'],
                'anchor': pair.get('anchor', False),
                'created_at': pair['created_at'],
                'front': self._page_card(pages[pair['front_id']]),
                'back': self._page_card(pages[pair['back_id']]),
            })
        ignored = []
        for item in session['ignored']:
            page = pages.get(item['page_id'])
            card = self._page_card(page) if page else {'id': item['page_id']}
            ignored.append({**item, 'page': card})
        return {
            'id': session['id'],
            'name': session['name'],
            'document_id': session['document_id'],
            'status': session['status'],
            'created_at': session['created_at'],
            'updated_at': session['updated_at'],
            'revision': session['revision'],
            'role_map': session['role_map'],
            'orientation_map': session.get('orientation_map', {}),
            'batches': session['batches'],
            'pages': [self._page_card(page) for page in session['pages']],
            'pairs': sorted(pairs, key=lambda pair: pair['front']['index']),
            'ignored': ignored,
            'analysis': session.get('analysis'),
            'finalized_document_id': session.get('finalized_document_id'),
            'finalized_order': session.get('finalized_order'),
            'reopened_from': session.get('reopened_from'),
            'readiness': self._finalize_issues(session),
        }
