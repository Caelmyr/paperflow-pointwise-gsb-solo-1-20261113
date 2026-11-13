from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor

from .core import choice, identity, ids, now, require, string

OPERATIONS = {
    'perspective', 'rotate', 'deskew', 'illumination', 'color', 'denoise', 'crop', 'enhance',
    'contrast', 'clahe', 'sharpen', 'white_balance', 'gamma', 'stretch', 'bilateral',
    'remove_border', 'pad', 'resize', 'erase', 'morph_text', 'sauvola', 'remove_lines',
    'flatten_background', 'invert', 'flip', 'ocr', 'screen_suppress',
}


def validate_steps(steps):
    require(isinstance(steps, list) and 0 < len(steps) <= 20, '工作流包含1–20个步骤')
    normalized = []
    for step in steps:
        require(isinstance(step, dict), '工作流步骤必须为对象')
        operation = choice(step.get('operation'), '工作流操作', OPERATIONS)
        params = step.get('params', {})
        require(isinstance(params, dict), '步骤参数必须为对象')
        from .parameters import validate_operation
        params = validate_operation(operation, params)
        on_error = choice(step.get('on_error', 'stop'), '失败策略', ('stop', 'continue'))
        normalized.append({'operation': operation, 'params': copy.deepcopy(params), 'on_error': on_error})
    return normalized


class Pipelines:
    def __init__(self, store, documents):
        self.store = store
        self.documents = documents
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='paperflow-pipeline')
        self.seed()
        self.recover()

    def seed(self):
        defaults = [
            {
                'name': '文字扫描 · 清晰归档',
                'description': '修复背景、倾斜纠正、自适应黑白与中英文识别。',
                'steps': [
                    {'operation': 'illumination', 'params': {'strength': 0.7}},
                    {'operation': 'deskew', 'params': {}},
                    {'operation': 'color', 'params': {'mode': 'adaptive'}},
                    {'operation': 'denoise', 'params': {'area': 2, 'median': 1}},
                    {'operation': 'ocr', 'params': {'language': 'chi_sim+eng'}},
                ],
            },
            {
                'name': '彩色资料 · 温和增强',
                'description': '保留插图色彩，修复背景并轻度锐化。',
                'steps': [
                    {'operation': 'white_balance', 'params': {'strength': 0.4}},
                    {'operation': 'illumination', 'params': {'strength': 0.4}},
                    {'operation': 'sharpen', 'params': {'amount': 0.4}},
                    {'operation': 'ocr', 'params': {'language': 'chi_sim+eng'}},
                ],
            },
        ]
        with self.store.transaction() as state:
            if 'pipelines' in state:
                return
            state['pipelines'] = {}
            state['pipeline_runs'] = {}
            for template in defaults:
                identifier = identity('pipeline')
                state['pipelines'][identifier] = {
                    'id': identifier,
                    'name': template['name'],
                    'description': template['description'],
                    'steps': validate_steps(template['steps']),
                    'created_at': now(),
                    'updated_at': now(),
                }

    def recover(self):
        with self.store.transaction() as state:
            for run in state.get('pipeline_runs', {}).values():
                if run['status'] in ('running', 'queued', 'cancelling'):
                    run['status'] = 'interrupted'
                    run['finished_at'] = now()
                    run['error'] = '服务重启，已完成的步骤已持久化。'

    def list(self):
        return list(self.store.snapshot().get('pipelines', {}).values())

    def get(self, identifier):
        result = self.store.snapshot().get('pipelines', {}).get(identifier)
        require(result is not None, '工作流不存在', 404)
        return result

    def create(self, values):
        record = {
            'id': identity('pipeline'),
            'name': string(values.get('name'), '工作流名称', 100),
            'description': string(values.get('description', ''), '工作流说明', 1000, True),
            'steps': validate_steps(values.get('steps')),
            'created_at': now(),
            'updated_at': now(),
        }
        with self.store.transaction() as state:
            state['pipelines'][record['id']] = record
            self.store.event(state, '创建工作流', record['id'])
        return record

    def update(self, identifier, values):
        self.get(identifier)
        with self.store.transaction() as state:
            record = state['pipelines'][identifier]
            if 'name' in values:
                record['name'] = string(values['name'], '工作流名称', 100)
            if 'description' in values:
                record['description'] = string(values['description'], '工作流说明', 1000, True)
            if 'steps' in values:
                record['steps'] = validate_steps(values['steps'])
            record['updated_at'] = now()
            self.store.event(state, '修改工作流', identifier)
        return self.get(identifier)

    def delete(self, identifier):
        self.get(identifier)
        with self.store.transaction() as state:
            state['pipelines'].pop(identifier)
            self.store.event(state, '删除工作流', identifier)
        return {'deleted': identifier}

    def submit(self, identifier, page_ids):
        ids(page_ids)
        pipeline = self.get(identifier)
        snapshot = self.store.snapshot()
        require(all(page_id in snapshot['pages'] for page_id in page_ids), '工作流页面不存在', 404)
        require(sum(run['status'] in ('queued', 'running', 'cancelling') for run in snapshot.get('pipeline_runs', {}).values()) < 20, '工作流队列已满', 429)
        record = {
            'id': identity('run'),
            'pipeline_id': identifier,
            'name': pipeline['name'],
            'steps': pipeline['steps'],
            'page_ids': page_ids,
            'total': len(page_ids) * len(pipeline['steps']),
            'done': 0,
            'status': 'queued',
            'cancel_requested': False,
            'created_at': now(),
            'started_at': None,
            'finished_at': None,
            'results': [],
            'error': None,
        }
        with self.store.transaction() as state:
            state['pipeline_runs'][record['id']] = record
            self.store.event(state, '提交工作流', record['id'])
        self.executor.submit(self.execute, record['id'])
        return record

    def get_run(self, identifier):
        run = self.store.snapshot().get('pipeline_runs', {}).get(identifier)
        require(run is not None, '工作流任务不存在', 404)
        run['progress'] = round(run['done'] / max(1, run['total']) * 100, 1)
        return run

    def cancel(self, identifier):
        with self.store.transaction() as state:
            run = state.get('pipeline_runs', {}).get(identifier)
            require(run is not None, '工作流任务不存在', 404)
            require(run['status'] in ('queued', 'running', 'cancelling'), '工作流已结束', 409)
            run['cancel_requested'] = True
            run['status'] = 'cancelling'
            self.store.event(state, '取消工作流', identifier)
        return self.get_run(identifier)

    def execute(self, identifier):
        try:
            self.run_steps(identifier)
        except Exception as error:
            with self.store.transaction() as state:
                run = state['pipeline_runs'][identifier]
                run['status'] = 'failed'
                run['error'] = str(error)
                run['finished_at'] = now()

    def run_steps(self, identifier):
        run = self.get_run(identifier)
        with self.store.transaction() as state:
            record = state['pipeline_runs'][identifier]
            record['started_at'] = now()
            record['status'] = 'running'
        failed = 0
        for page_id in run['page_ids']:
            if self.get_run(identifier)['cancel_requested']:
                break
            for index, step in enumerate(run['steps']):
                if self.get_run(identifier)['cancel_requested']:
                    break
                started_at = now()
                result = {
                    'page_id': page_id,
                    'step': index,
                    'operation': step['operation'],
                    'started_at': started_at,
                    'status': 'success',
                }
                try:
                    if step['operation'] == 'ocr':
                        self.documents.recognize(page_id, step['params'])
                    else:
                        self.documents.apply(page_id, step['operation'], step['params'])
                except Exception as error:
                    failed += 1
                    result['status'] = 'failed'
                    result['error'] = str(error)
                result['finished_at'] = now()
                with self.store.transaction() as state:
                    record = state['pipeline_runs'][identifier]
                    record['results'].append(result)
                    record['done'] += 1
                if result['status'] == 'failed' and step['on_error'] == 'stop':
                    break
        with self.store.transaction() as state:
            record = state['pipeline_runs'][identifier]
            if record['cancel_requested']:
                record['status'] = 'cancelled'
            elif failed:
                record['status'] = 'partial'
            else:
                record['status'] = 'completed'
            record['finished_at'] = now()
            self.store.event(state, '工作流结束', identifier, {'status': record['status']})
