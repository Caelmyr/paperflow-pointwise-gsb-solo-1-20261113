from __future__ import annotations

import copy
import re
from collections import Counter, defaultdict

from .core import choice, identity, ids, now, number, require, string


class Catalog:
    def __init__(self, store):
        self.store = store

    def tags(self, values):
        require(isinstance(values, list) and len(values) <= 30, '最多30个标签')
        result = []
        seen = set()
        for value in values:
            tag = string(value, '标签', 40)
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                result.append(tag)
        return result

    def update_metadata(self, document_id, values):
        with self.store.transaction() as state:
            document = state['documents'].get(document_id)
            require(document is not None, '文档不存在', 404)
            metadata = document.setdefault('metadata', {})
            for field in ('author', 'source', 'reference', 'description'):
                if field in values:
                    metadata[field] = string(values[field], field, 5000 if field == 'description' else 300, True)
            if 'tags' in values:
                metadata['tags'] = self.tags(values['tags'])
            if 'date' in values:
                date = string(values['date'], '文档日期', 10, True)
                if date:
                    from datetime import date as date_type
                    try:
                        date_type.fromisoformat(date)
                    except ValueError:
                        require(False, '日期必须为 YYYY-MM-DD')
                metadata['date'] = date
            if 'starred' in values:
                require(isinstance(values['starred'], bool), '收藏状态必须为布尔值')
                metadata['starred'] = values['starred']
            document['updated_at'] = now()
            self.store.event(state, '更新文档元数据', document_id)
        return self.store.get('documents', document_id)

    def facets(self):
        state = self.store.snapshot()
        categories = Counter()
        tags = Counter()
        authors = Counter()
        years = Counter()
        for document in state['documents'].values():
            categories[document['category']] += 1
            metadata = document.get('metadata', {})
            tags.update(metadata.get('tags', []))
            if metadata.get('author'):
                authors[metadata['author']] += 1
            if metadata.get('date'):
                years[metadata['date'][:4]] += 1
        return {
            'categories': dict(categories.most_common()),
            'tags': dict(tags.most_common()),
            'authors': dict(authors.most_common()),
            'years': dict(sorted(years.items(), reverse=True)),
        }

    def query(self, values):
        state = self.store.snapshot()
        results = list(state['documents'].values())
        text = values.get('q', '')
        require(isinstance(text, str) and len(text) <= 200, '文档查询词无效')
        text = text.casefold().strip()
        if text:
            results = [document for document in results if text in self.searchable_metadata(document).casefold()]
        collection_id = values.get('collection_id')
        if collection_id:
            results = [document for document in results if document['collection_id'] == collection_id]
        category = values.get('category')
        if category:
            results = [document for document in results if document['category'] == category]
        tag = values.get('tag')
        if tag:
            results = [document for document in results if tag in document.get('metadata', {}).get('tags', [])]
        author = values.get('author')
        if author:
            results = [document for document in results if document.get('metadata', {}).get('author') == author]
        if values.get('starred'):
            results = [document for document in results if document.get('metadata', {}).get('starred')]
        if values.get('has_ocr') is not None:
            want_ocr = values['has_ocr']
            require(isinstance(want_ocr, bool), '识别筛选值无效')
            results = [document for document in results if any(state['pages'][page_id].get('ocr') for page_id in document['page_ids']) == want_ocr]
        sort = choice(values.get('sort', 'updated_at'), '排序字段', ('updated_at', 'created_at', 'name', 'page_count'))
        descending = values.get('descending', True)
        require(isinstance(descending, bool), '排序方向无效')
        if sort == 'page_count':
            results.sort(key=lambda document: len(document['page_ids']), reverse=descending)
        else:
            results.sort(key=lambda document: document[sort], reverse=descending)
        total = len(results)
        offset = number(values.get('offset', 0), '查询偏移', 0, 100000, True)
        limit = number(values.get('limit', 50), '查询数量', 1, 500, True)
        results = results[offset:offset + limit]
        for document in results:
            document['page_count'] = len(document['page_ids'])
            document['ocr_count'] = sum(bool(state['pages'][page_id].get('ocr')) for page_id in document['page_ids'])
        return {'total': total, 'offset': offset, 'limit': limit, 'documents': results}

    def searchable_metadata(self, document):
        values = [document['name'], document['category']]
        metadata = document.get('metadata', {})
        for field in ('author', 'source', 'reference', 'description', 'date'):
            values.append(metadata.get(field, ''))
        values.extend(metadata.get('tags', []))
        return '\n'.join(values)

    def batch_update(self, values):
        document_ids = ids(values.get('document_ids'), '文档')
        changes = values.get('changes')
        require(isinstance(changes, dict), '批量修改内容无效')
        permitted = {'category', 'collection_id', 'add_tags', 'remove_tags', 'starred'}
        require(set(changes).issubset(permitted) and bool(changes), '批量修改字段无效')
        with self.store.transaction() as state:
            require(all(identifier in state['documents'] for identifier in document_ids), '批量文档不存在', 404)
            if 'collection_id' in changes:
                require(changes['collection_id'] in state['collections'], '目标集合不存在', 404)
            if 'category' in changes:
                category = string(changes['category'], '分类', 80)
            add_tags = self.tags(changes.get('add_tags', []))
            remove_tags = self.tags(changes.get('remove_tags', []))
            for identifier in document_ids:
                document = state['documents'][identifier]
                if 'category' in changes:
                    document['category'] = category
                if 'collection_id' in changes:
                    document['collection_id'] = changes['collection_id']
                metadata = document.setdefault('metadata', {})
                existing_tags = metadata.get('tags', [])
                metadata['tags'] = self.tags([tag for tag in existing_tags + add_tags if tag not in remove_tags])
                if 'starred' in changes:
                    require(isinstance(changes['starred'], bool), '收藏值无效')
                    metadata['starred'] = changes['starred']
                document['updated_at'] = now()
            self.store.event(state, '批量修改文档信息', 'workspace', {'documents': document_ids, 'fields': list(changes)})
        return {'updated': document_ids}

    def bookmarks(self, document_id):
        document = self.store.get('documents', document_id)
        return document.get('bookmarks', [])

    def add_bookmark(self, document_id, values):
        page_id = values.get('page_id')
        title = string(values.get('title'), '书签标题', 160)
        record = {
            'id': identity('bookmark'),
            'page_id': page_id,
            'title': title,
            'created_at': now(),
        }
        with self.store.transaction() as state:
            document = state['documents'].get(document_id)
            require(document is not None, '文档不存在', 404)
            require(page_id in document['page_ids'], '书签页面不属于此文档')
            document.setdefault('bookmarks', []).append(record)
            self.store.event(state, '添加文档书签', document_id, {'bookmark_id': record['id']})
        return record

    def delete_bookmark(self, document_id, bookmark_id):
        with self.store.transaction() as state:
            document = state['documents'].get(document_id)
            require(document is not None, '文档不存在', 404)
            bookmarks = document.get('bookmarks', [])
            require(any(bookmark['id'] == bookmark_id for bookmark in bookmarks), '书签不存在', 404)
            document['bookmarks'] = [bookmark for bookmark in bookmarks if bookmark['id'] != bookmark_id]
            self.store.event(state, '删除文档书签', document_id)
        return {'deleted': bookmark_id}

    def suggest_name(self, document_id):
        state = self.store.snapshot()
        document = state['documents'].get(document_id)
        require(document is not None, '文档不存在', 404)
        candidates = []
        for page_id in document['page_ids'][:3]:
            result = state['pages'][page_id].get('ocr')
            if not result:
                continue
            for line in result['text'].splitlines()[:6]:
                clean = re.sub(r'\s+', ' ', line).strip()
                if 4 <= len(clean) <= 80:
                    candidates.append(clean)
        if not candidates:
            return {'suggestions': [], 'reason': '请先识别文档标题'}
        return {'suggestions': list(dict.fromkeys(candidates))[:5], 'reason': '来自前几页的识别文字，仅提供建议，不自动改名'}
