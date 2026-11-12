from __future__ import annotations

import copy
import re
from collections import Counter

from .core import choice, identity, now, number, require, string


class Review:
    def __init__(self, store):
        self.store = store

    def configuration(self):
        values = self.store.snapshot().get('review_settings', {})
        return {
            'minimum_confidence': values.get('minimum_confidence', 65),
            'minimum_words': values.get('minimum_words', 3),
            'require_ocr': values.get('require_ocr', True),
            'require_review': values.get('require_review', False),
            'flag_stale': values.get('flag_stale', True),
        }

    def configure(self, values):
        current = self.configuration()
        if 'minimum_confidence' in values:
            current['minimum_confidence'] = number(values['minimum_confidence'], '最低置信度', 0, 100)
        if 'minimum_words' in values:
            current['minimum_words'] = number(values['minimum_words'], '最低词数', 0, 1000, True)
        for field in ('require_ocr', 'require_review', 'flag_stale'):
            if field in values:
                require(isinstance(values[field], bool), field + '必须为布尔值')
                current[field] = values[field]
        with self.store.transaction() as state:
            state['review_settings'] = current
            self.store.event(state, '更新审核规则', 'workspace')
        return current

    def issues(self, page, settings=None):
        settings = settings or self.configuration()
        issues = []
        result = page.get('ocr')
        if not result:
            if settings['require_ocr']:
                issues.append({'code': 'missing_ocr', 'severity': 'error', 'message': '尚未进行文字识别'})
            return issues
        if result.get('stale') and settings['flag_stale']:
            issues.append({'code': 'stale_ocr', 'severity': 'error', 'message': '图像已变化，识别文字框需要更新'})
        if len(result['boxes']) < settings['minimum_words']:
            issues.append({'code': 'few_words', 'severity': 'warning', 'message': '识别词数低于最低要求，可能为空白或识别失败'})
        if result['confidence'] < settings['minimum_confidence']:
            issues.append({'code': 'low_confidence', 'severity': 'warning', 'message': f'平均置信度仅 {result["confidence"]}%'})
        low_words = [box for box in result['boxes'] if box['confidence'] < settings['minimum_confidence'] and not box.get('edited')]
        if low_words:
            issues.append({
                'code': 'low_words',
                'severity': 'warning',
                'message': f'{len(low_words)} 个低置信度词待校订',
                'word_ids': [box['id'] for box in low_words],
            })
        if '\ufffd' in result['text']:
            issues.append({'code': 'replacement_character', 'severity': 'warning', 'message': '文本含替代字符'})
        if settings['require_review'] and page.get('review', {}).get('status') != 'approved':
            issues.append({'code': 'not_reviewed', 'severity': 'warning', 'message': '页面尚未人工审核通过'})
        return issues

    def queue(self, document_id=None):
        state = self.store.snapshot()
        settings = self.configuration()
        results = []
        for document in state['documents'].values():
            if document_id and document['id'] != document_id:
                continue
            for index, page_id in enumerate(document['page_ids']):
                page = state['pages'][page_id]
                issues = self.issues(page, settings)
                if issues:
                    results.append({
                        'page_id': page_id,
                        'document_id': document['id'],
                        'document_name': document['name'],
                        'page_number': index + 1,
                        'page_name': page['name'],
                        'issues': issues,
                        'errors': sum(issue['severity'] == 'error' for issue in issues),
                        'warnings': sum(issue['severity'] == 'warning' for issue in issues),
                        'review': page.get('review'),
                    })
        return sorted(results, key=lambda item: (-item['errors'], -item['warnings'], item['document_name'], item['page_number']))

    def low_confidence(self, page_id, threshold=None):
        page = self.store.get('pages', page_id)
        result = page.get('ocr')
        require(result is not None, '页面尚未 OCR', 409)
        if threshold is None:
            threshold = self.configuration()['minimum_confidence']
        threshold = number(threshold, '置信度阈值', 0, 100)
        words = [copy.deepcopy(box) for box in result['boxes'] if box['confidence'] < threshold]
        return sorted(words, key=lambda box: box['confidence'])

    def set_status(self, page_id, values):
        status = choice(values.get('status'), '审核状态', ('pending', 'approved', 'rejected'))
        reviewer = string(values.get('reviewer', '本机用户'), '审核人', 80)
        note = string(values.get('note', ''), '审核备注', 5000, True)
        with self.store.transaction() as state:
            page = state['pages'].get(page_id)
            require(page is not None, '页面不存在', 404)
            result = page.get('ocr')
            if status == 'approved':
                require(result is not None and not result.get('stale'), '请先完成最新图像的 OCR', 409)
            record = {
                'status': status,
                'reviewer': reviewer,
                'note': note,
                'image_revision': page['revision'],
                'ocr_edited_at': result.get('edited_at') if result else None,
                'updated_at': now(),
            }
            page['review'] = record
            page.setdefault('review_history', []).append(copy.deepcopy(record))
            self.store.event(state, '人工审核', page_id, {'status': status})
        return self.store.get('pages', page_id)['review']

    def add_note(self, page_id, values):
        text = string(values.get('text'), '批注', 5000)
        rect = values.get('rect')
        if rect is not None:
            require(isinstance(rect, list) and len(rect) == 4, '批注区域无效')
            page = self.store.get('pages', page_id)
            x = number(rect[0], '批注x', 0, page['width'])
            y = number(rect[1], '批注y', 0, page['height'])
            width = number(rect[2], '批注宽', 1, page['width'])
            height = number(rect[3], '批注高', 1, page['height'])
            require(x + width <= page['width'] and y + height <= page['height'], '批注区域越界')
            rect = [x, y, width, height]
        note = {
            'id': identity('note'),
            'text': text,
            'rect': rect,
            'author': string(values.get('author', '本机用户'), '批注人', 80),
            'resolved': False,
            'created_at': now(),
            'updated_at': now(),
        }
        with self.store.transaction() as state:
            page = state['pages'].get(page_id)
            require(page is not None, '页面不存在', 404)
            page.setdefault('notes', []).append(note)
            self.store.event(state, '添加批注', page_id, {'note_id': note['id']})
        return note

    def update_note(self, page_id, note_id, values):
        with self.store.transaction() as state:
            page = state['pages'].get(page_id)
            require(page is not None, '页面不存在', 404)
            note = next((note for note in page.get('notes', []) if note['id'] == note_id), None)
            require(note is not None, '批注不存在', 404)
            if 'text' in values:
                note['text'] = string(values['text'], '批注', 5000)
            if 'resolved' in values:
                require(isinstance(values['resolved'], bool), '解决状态必须为布尔值')
                note['resolved'] = values['resolved']
            note['updated_at'] = now()
            self.store.event(state, '修改批注', page_id, {'note_id': note_id})
            result = copy.deepcopy(note)
        return result

    def delete_note(self, page_id, note_id):
        with self.store.transaction() as state:
            page = state['pages'].get(page_id)
            require(page is not None, '页面不存在', 404)
            notes = page.get('notes', [])
            require(any(note['id'] == note_id for note in notes), '批注不存在', 404)
            page['notes'] = [note for note in notes if note['id'] != note_id]
            self.store.event(state, '删除批注', page_id, {'note_id': note_id})
        return {'deleted': note_id}

    def text_statistics(self, document_id):
        state = self.store.snapshot()
        document = state['documents'].get(document_id)
        require(document is not None, '文档不存在', 404)
        text_parts = []
        confidences = []
        pages_with_ocr = 0
        edited_pages = 0
        stale_pages = 0
        for page_id in document['page_ids']:
            result = state['pages'][page_id].get('ocr')
            if not result:
                continue
            pages_with_ocr += 1
            edited_pages += bool(result.get('edited'))
            stale_pages += bool(result.get('stale'))
            text_parts.append(result['text'])
            confidences.extend(box['confidence'] for box in result['boxes'])
        text = '\n'.join(text_parts)
        words = re.findall(r'[A-Za-z]+(?:\x27[A-Za-z]+)?', text)
        characters = re.findall(r'[\u3400-\u9fff]', text)
        frequencies = Counter(word.casefold() for word in words)
        distribution = {str(index): 0 for index in range(0, 100, 10)}
        for value in confidences:
            bucket = min(90, int(max(0, value) // 10) * 10)
            distribution[str(bucket)] += 1
        return {
            'pages': len(document['page_ids']),
            'pages_with_ocr': pages_with_ocr,
            'edited_pages': edited_pages,
            'stale_pages': stale_pages,
            'characters': len(text),
            'chinese_characters': len(characters),
            'english_words': len(words),
            'mean_confidence': round(sum(confidences) / len(confidences), 2) if confidences else 0,
            'confidence_distribution': distribution,
            'frequent_words': frequencies.most_common(50),
        }

    def replace_preview(self, document_id, values):
        search = string(values.get('search'), '查找内容', 200)
        replacement = string(values.get('replacement', ''), '替换内容', 1000, True)
        case_sensitive = values.get('case_sensitive', True)
        require(isinstance(case_sensitive, bool), '大小写选项无效')
        pattern = re.compile(re.escape(search), 0 if case_sensitive else re.IGNORECASE)
        state = self.store.snapshot()
        document = state['documents'].get(document_id)
        require(document is not None, '文档不存在', 404)
        changes = []
        for page_id in document['page_ids']:
            result = state['pages'][page_id].get('ocr')
            if not result:
                continue
            after, count = pattern.subn(lambda match: replacement, result['text'])
            if count:
                changes.append({'page_id': page_id, 'count': count, 'before': result['text'], 'after': after})
        return changes

    def replace_apply(self, document_id, values):
        changes = self.replace_preview(document_id, values)
        with self.store.transaction() as state:
            for change in changes:
                page = state['pages'][change['page_id']]
                result = page['ocr']
                require(result['text'] == change['before'], '预览后文本已变化，请重试', 409)
                page['ocr_versions'].append(copy.deepcopy(result))
                page['ocr_versions'] = page['ocr_versions'][-20:]
                result['text'] = change['after']
                result['text_override'] = True
                result['edited'] = True
                result['edited_at'] = now()
            self.store.event(state, '批量文字替换', document_id, {'pages': len(changes), 'count': sum(change['count'] for change in changes)})
        return {'pages': len(changes), 'replacements': sum(change['count'] for change in changes)}
