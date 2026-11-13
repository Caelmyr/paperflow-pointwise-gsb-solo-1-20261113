"""笔迹粗细调整的预览会话：预览所见即确认所得。

会话只保存从基线图像算出的调整结果与差异遮罩，确认时原样落盘，
不存在“预览一套参数、保存再算一遍”的偏差。重新调整参数时以原图或当前
确认图为基线重新计算，多次尝试不会叠加变字。
"""
from __future__ import annotations

import threading
import time

from . import stroke
from .core import identity, require
from .imaging import read_image

SESSION_TTL_SECONDS = 24 * 3600
MAX_SESSIONS = 200


class StrokeSessions:
    def __init__(self, store):
        self.store = store
        self.sessions = {}
        self.guard = threading.Lock()

    def _purge(self):
        expired = [key for key, item in self.sessions.items()
                   if time.time() - item['created_at'] > SESSION_TTL_SECONDS]
        for key in expired:
            self.sessions.pop(key, None)
        while len(self.sessions) > MAX_SESSIONS:
            oldest = min(self.sessions, key=lambda key: self.sessions[key]['created_at'])
            self.sessions.pop(oldest, None)

    def _load_baseline(self, page, baseline):
        variant = 'original' if baseline == 'original' else 'current'
        return read_image(self.store.resolve(page[variant]))

    def preview(self, page_id, values):
        """根据参数生成预览；同一 session_id 重复调用覆盖为最新一次尝试。"""
        page = self.store.get('pages', page_id)
        settings = stroke.validate_settings(values or {}, self._load_baseline(
            page, (values or {}).get('baseline', 'current')))
        image = self._load_baseline(page, settings['baseline'])
        result, diff, stats = stroke.adjust(image, settings)
        overlay = stroke.overlay_preview(image, diff, stats)
        session_id = values.get('session_id') if isinstance(values, dict) else None
        with self.guard:
            self._purge()
            if session_id:
                require(isinstance(session_id, str) and len(session_id) <= 80, '会话标识无效')
                old = self.sessions.get(session_id)
                if old is not None:
                    require(old['page_id'] == page_id, '该预览会话属于其他页面，请新建预览', 409)
                    require(old['page_revision'] == page['revision'],
                            '页面已变化，请基于当前版本重新预览', 409)
            else:
                session_id = identity('stroke')
            self.sessions[session_id] = {
                'page_id': page_id,
                'page_revision': page['revision'],
                'baseline': settings['baseline'],
                'settings': settings,
                'result': result,
                'diff': diff,
                'overlay': overlay,
                'stats': stats,
                'created_at': time.time(),
            }
        return {
            'session_id': session_id,
            'page_id': page_id,
            'page_revision': page['revision'],
            'stats': stats,
            'confirmed': False,
        }

    def get(self, session_id):
        with self.guard:
            item = self.sessions.get(session_id)
            require(item is not None, '预览会话不存在或已过期，请重新生成预览', 404)
            return item

    def image(self, session_id, kind):
        item = self.get(session_id)
        require(kind in ('overlay', 'result', 'diff'), '预览类型无效')
        if kind == 'overlay':
            return item['overlay'], 'image/png'
        if kind == 'result':
            return item['result'], 'image/png'
        # 差异遮罩放大到 0/128/255 便于直接查看。
        visual = (item['diff'].astype('uint8') * 85).clip(0, 255)
        return visual, 'image/png'

    def confirm(self, page_id, session_id):
        """确认预览：把会话中已算好的同一份结果交给 Documents 落盘。"""
        item = self.get(session_id)
        require(item['page_id'] == page_id, '预览会话与页面不匹配', 409)
        require(not item.get('confirmed'), '该预览已确认，不能重复提交；请重新生成预览', 409)
        page = self.store.get('pages', page_id)
        require(item['page_revision'] == page['revision'],
                '页面在预览后发生变化，请重新预览再确认', 409)
        from .documents import Documents
        documents = Documents(self.store)
        record = documents.commit_stroke(page_id, item)
        with self.guard:
            item['confirmed'] = True
            item['page_revision'] = record['revision']
        return record

    def discard(self, session_id):
        with self.guard:
            existed = self.sessions.pop(session_id, None) is not None
        return {'discarded': existed}
