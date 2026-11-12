from __future__ import annotations

import copy
import queue
import threading
import traceback

from .core import DomainError, choice, identity, ids, now, number, require

TERMINAL = {'completed', 'partial', 'failed', 'cancelled', 'interrupted'}


class Scheduler:
    def __init__(self, store, documents):
        self.store = store
        self.documents = documents
        self.queue = queue.Queue(maxsize=100)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.worker, name='paperflow-worker', daemon=True)
        self.thread.start()

    def submit(self, page_ids, operation, params):
        ids(page_ids)
        choice(operation, '批量操作', ('ocr', 'rotate', 'deskew', 'illumination', 'color', 'denoise', 'enhance', 'perspective', 'restore'))
        from .parameters import validate_operation
        params = validate_operation(operation, params)
        task = {
            'id': identity('task'),
            'operation': operation,
            'params': copy.deepcopy(params),
            'page_ids': page_ids,
            'status': 'queued',
            'created_at': now(),
            'started_at': None,
            'finished_at': None,
            'total': len(page_ids),
            'done': 0,
            'succeeded': 0,
            'failed': 0,
            'progress': 0,
            'current_page': None,
            'cancel_requested': False,
            'results': [],
            'error': None,
        }
        with self.store.transaction() as state:
            require(sum(t['status'] not in TERMINAL for t in state['tasks'].values()) < 100, '任务队列已满', 429)
            state['tasks'][task['id']] = task
            self.store.event(state, '提交批量任务', task['id'], {'operation': operation, 'total': len(page_ids)})
        self.queue.put_nowait(task['id'])
        return task

    def cancelled(self, task_id):
        task = self.store.get('tasks', task_id)
        return self.stop.is_set() or task['cancel_requested']

    def cancel(self, task_id):
        with self.store.transaction() as state:
            task = state['tasks'].get(task_id)
            require(task is not None, '任务不存在', 404)
            require(task['status'] not in TERMINAL, '任务已经结束', 409)
            task['cancel_requested'] = True
            if task['status'] == 'queued':
                task['status'] = 'cancelled'
                task['finished_at'] = now()
            else:
                task['status'] = 'cancelling'
            self.store.event(state, '请求取消任务', task_id)
        return self.store.get('tasks', task_id)

    def retry(self, task_id):
        task = self.store.get('tasks', task_id)
        require(task['status'] in TERMINAL, '任务尚未结束', 409)
        successful = {result['page_id'] for result in task['results'] if result['status'] == 'success'}
        remaining = [page_id for page_id in task['page_ids'] if page_id not in successful]
        require(bool(remaining), '没有待重试页面')
        return self.submit(remaining, task['operation'], task['params'])

    def worker(self):
        while not self.stop.is_set():
            try:
                task_id = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self.run(task_id)
            except Exception as error:
                traceback.print_exc()
                with self.store.transaction() as state:
                    task = state['tasks'].get(task_id)
                    if task:
                        task['status'] = 'failed'
                        task['error'] = str(error)
                        task['finished_at'] = now()
            finally:
                self.queue.task_done()

    def run(self, task_id):
        task = self.store.get('tasks', task_id)
        if task['status'] in TERMINAL:
            return
        with self.store.transaction() as state:
            record = state['tasks'][task_id]
            if record['cancel_requested']:
                record['status'] = 'cancelled'
                record['finished_at'] = now()
                return
            record['status'] = 'running'
            record['started_at'] = now()
        for page_id in task['page_ids']:
            if self.cancelled(task_id):
                break
            with self.store.transaction() as state:
                state['tasks'][task_id]['current_page'] = page_id
            started_at = now()
            try:
                if task['operation'] == 'ocr':
                    self.documents.recognize(page_id, task['params'])
                elif task['operation'] == 'restore':
                    self.documents.restore(page_id)
                else:
                    self.documents.apply(page_id, task['operation'], task['params'])
                result = {'page_id': page_id, 'status': 'success', 'started_at': started_at, 'finished_at': now()}
            except Exception as error:
                result = {
                    'page_id': page_id,
                    'status': 'failed',
                    'message': str(error),
                    'code': error.code if isinstance(error, DomainError) else 'processing_failed',
                    'started_at': started_at,
                    'finished_at': now(),
                }
            with self.store.transaction() as state:
                record = state['tasks'][task_id]
                record['results'].append(result)
                record['done'] += 1
                record['succeeded' if result['status'] == 'success' else 'failed'] += 1
                record['progress'] = round(record['done'] / record['total'] * 100, 1)
        with self.store.transaction() as state:
            record = state['tasks'][task_id]
            if record['cancel_requested'] or self.stop.is_set():
                record['status'] = 'cancelled'
            elif record['failed'] == record['total']:
                record['status'] = 'failed'
            elif record['failed']:
                record['status'] = 'partial'
            else:
                record['status'] = 'completed'
            record['current_page'] = None
            record['finished_at'] = now()
            self.store.event(state, '批量任务结束', task_id, {'status': record['status'], 'succeeded': record['succeeded'], 'failed': record['failed']})

    def list(self):
        tasks = list(self.store.snapshot()['tasks'].values())
        return sorted(tasks, key=lambda task: task['created_at'], reverse=True)

    def shutdown(self):
        self.stop.set()
        self.thread.join(timeout=3)
