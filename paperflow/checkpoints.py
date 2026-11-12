from __future__ import annotations

import copy

from .core import identity, now, require, string
from .imaging import read_image, thumbnail


class Checkpoints:
    def __init__(self, store):
        self.store = store

    def create(self, document_id, values):
        document = self.store.get('documents', document_id)
        name = string(values.get('name', '处理前快照'), '快照名称', 100)
        state = self.store.snapshot()
        pages = {page_id: copy.deepcopy(state['pages'][page_id]) for page_id in document['page_ids']}
        record = {
            'id': identity('checkpoint'),
            'document_id': document_id,
            'name': name,
            'created_at': now(),
            'workspace_revision': state['revision'],
            'document': document,
            'pages': pages,
        }
        with self.store.transaction() as state:
            state.setdefault('checkpoints', {})[record['id']] = record
            self.store.event(state, '创建文档快照', document_id, {'checkpoint_id': record['id']})
        return self.summary(record)

    def summary(self, record):
        return {
            'id': record['id'],
            'document_id': record['document_id'],
            'name': record['name'],
            'created_at': record['created_at'],
            'workspace_revision': record['workspace_revision'],
            'page_count': len(record['pages']),
            'ocr_pages': sum(bool(page.get('ocr')) for page in record['pages'].values()),
        }

    def get(self, identifier):
        record = self.store.snapshot().get('checkpoints', {}).get(identifier)
        require(record is not None, '快照不存在', 404)
        return record

    def list(self, document_id):
        self.store.get('documents', document_id)
        records = self.store.snapshot().get('checkpoints', {}).values()
        return [self.summary(record) for record in records if record['document_id'] == document_id]

    def diff(self, identifier):
        record = self.get(identifier)
        state = self.store.snapshot()
        document = state['documents'].get(record['document_id'])
        if not document:
            return {'document_missing': True, 'added': [], 'removed': list(record['pages']), 'changed': []}
        before_ids = record['document']['page_ids']
        after_ids = document['page_ids']
        before = set(before_ids)
        after = set(after_ids)
        changed = []
        for page_id in before & after:
            old = record['pages'][page_id]
            new = state['pages'][page_id]
            fields = []
            for field in ('current', 'name', 'ocr', 'regions', 'review'):
                if old.get(field) != new.get(field):
                    fields.append(field)
            if fields:
                changed.append({'page_id': page_id, 'fields': fields})
        return {
            'document_missing': False,
            'added': [page_id for page_id in after_ids if page_id not in before],
            'removed': [page_id for page_id in before_ids if page_id not in after],
            'changed': changed,
            'order_changed': [page_id for page_id in before_ids if page_id in after] != [page_id for page_id in after_ids if page_id in before],
        }

    def restore_copy(self, identifier, values):
        checkpoint = self.get(identifier)
        state = self.store.snapshot()
        collection_id = values.get('collection_id', checkpoint['document']['collection_id'])
        require(collection_id in state['collections'], '目标集合不存在', 404)
        name = string(values.get('name', checkpoint['document']['name'] + ' · 快照恢复'), '恢复文档名称', 160)
        document_id = identity('document')
        document = copy.deepcopy(checkpoint['document'])
        document['id'] = document_id
        document['collection_id'] = collection_id
        document['name'] = name
        document['created_at'] = now()
        document['updated_at'] = now()
        document['page_ids'] = []
        document['bookmarks'] = []
        pages = {}
        for old_id in checkpoint['document']['page_ids']:
            page = copy.deepcopy(checkpoint['pages'][old_id])
            for field in ('original', 'current', 'thumbnail'):
                require(self.store.resolve(page[field]).is_file(), '快照图像丢失', 409)
            page['id'] = identity('page')
            page['document_id'] = document_id
            page['created_at'] = now()
            page['updated_at'] = now()
            pages[page['id']] = page
            document['page_ids'].append(page['id'])
        with self.store.transaction() as state:
            require(collection_id in state['collections'], '目标集合已删除', 409)
            state['documents'][document_id] = document
            state['pages'].update(pages)
            self.store.event(state, '从快照恢复为新文档', document_id, {'checkpoint_id': identifier})
        return document

    def delete(self, identifier):
        self.get(identifier)
        with self.store.transaction() as state:
            state['checkpoints'].pop(identifier)
            self.store.event(state, '删除文档快照', identifier)
        return {'deleted': identifier}
