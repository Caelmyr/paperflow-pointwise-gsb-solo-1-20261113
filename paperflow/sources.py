from __future__ import annotations

import shutil
from pathlib import Path

from .core import digest, identity, now, require, string
from .documents import ALLOWED_EXTENSIONS, MAX_FILE_BYTES


class Sources:
    def __init__(self, store):
        self.store = store
        (store.root / 'sources').mkdir(exist_ok=True)

    def retain(self, path, name):
        path = Path(path)
        require(path.is_file() and not path.is_symlink(), '来源文件不存在')
        require(path.stat().st_size <= MAX_FILE_BYTES, '来源文件超过60MB')
        suffix = path.suffix.lower()
        require(suffix in ALLOWED_EXTENSIONS, '来源文件格式不支持')
        checksum = digest(path)
        state = self.store.snapshot()
        for source in state.get('sources', {}).values():
            if source['sha256'] == checksum:
                require(self.store.resolve(source['path']).is_file(), '相同来源记录的文件丢失，请检查工作区', 409)
                return source
        destination = self.store.new_file('sources', suffix)
        temporary = destination.with_suffix(destination.suffix + '.tmp')
        try:
            shutil.copyfile(path, temporary)
            temporary.replace(destination)
            require(digest(destination) == checksum, '来源文件复制校验失败', 500)
        finally:
            temporary.unlink(missing_ok=True)
        record = {
            'id': identity('source'),
            'name': string(Path(name).name, '来源文件名称', 240),
            'path': self.store.relative(destination),
            'sha256': checksum,
            'bytes': destination.stat().st_size,
            'extension': suffix,
            'created_at': now(),
        }
        with self.store.transaction() as state:
            state.setdefault('sources', {})[record['id']] = record
            self.store.event(state, '保留上传原始文件', record['id'], {'name': record['name']})
        return record

    def attach(self, source_id, page_ids):
        with self.store.transaction() as state:
            require(source_id in state.get('sources', {}), '来源记录不存在', 404)
            require(all(page_id in state['pages'] for page_id in page_ids), '页面记录不存在', 404)
            for page_id in page_ids:
                page = state['pages'][page_id]
                page['source_file_id'] = source_id
                if page.get('source') is None:
                    page['source'] = {}
                page['source']['retained'] = True
            self.store.event(state, '关联原始文件', source_id, {'pages': page_ids})
        return self.get(source_id)

    def get(self, source_id):
        state = self.store.snapshot()
        record = state.get('sources', {}).get(source_id)
        require(record is not None, '来源记录不存在', 404)
        record['page_ids'] = [page['id'] for page in state['pages'].values() if page.get('source_file_id') == source_id]
        return record

    def list(self, document_id=None):
        state = self.store.snapshot()
        sources = state.get('sources', {})
        if document_id:
            document = state['documents'].get(document_id)
            require(document is not None, '文档不存在', 404)
            source_ids = {state['pages'][page_id].get('source_file_id') for page_id in document['page_ids']}
            sources = {key: value for key, value in sources.items() if key in source_ids}
        results = []
        for source_id in sources:
            results.append(self.get(source_id))
        return results

    def verify(self, source_id):
        source = self.get(source_id)
        path = self.store.resolve(source['path'])
        exists = path.is_file()
        checksum = digest(path) if exists else None
        return {
            'id': source_id,
            'exists': exists,
            'valid': exists and checksum == source['sha256'],
            'expected_sha256': source['sha256'],
            'actual_sha256': checksum,
            'bytes': path.stat().st_size if exists else 0,
        }

    def provenance(self, page_id):
        page = self.store.get('pages', page_id)
        source_id = page.get('source_file_id')
        source = self.get(source_id) if source_id else None
        original = self.store.resolve(page['original'])
        result = self.store.resolve(page['current'])
        steps = []
        previous = page['original']
        for entry in page['history']:
            steps.append({
                'id': entry['id'],
                'time': entry['time'],
                'operation': entry['operation'],
                'params': entry['params'],
                'input': entry['before'],
                'output': entry['after'],
                'branches_from_previous': previous != entry['before'],
            })
            previous = entry['after']
        return {
            'page_id': page_id,
            'source': source,
            'original': {
                'path': page['original'],
                'sha256': page['sha256'],
                'exists': original.is_file(),
            },
            'current': {
                'path': page['current'],
                'sha256': digest(result) if result.is_file() else None,
                'revision': page['revision'],
            },
            'steps': steps,
            'ocr_engine': page['ocr'].get('engine') if page.get('ocr') else None,
            'ocr_edited': bool(page.get('ocr') and page['ocr'].get('edited')),
        }
