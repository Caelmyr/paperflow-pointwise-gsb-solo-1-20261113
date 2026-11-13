from __future__ import annotations

import copy
import io
import shutil
import threading
from pathlib import Path

import cv2
import fitz
import numpy as np
from PIL import Image, ImageOps

from . import imaging, ocr, recipes
from .core import DomainError, digest, identity, ids, now, number, require, string
from .parameters import validate_operation

MAX_FILE_BYTES = 60 * 1024 * 1024
MAX_IMPORT_PAGES = 200
ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp', '.webp', '.pdf'}


class Documents:
    def __init__(self, store):
        self.store = store
        self.locks = {}
        self.guard = threading.Lock()

    def page_lock(self, page_id):
        with self.guard:
            return self.locks.setdefault(page_id, threading.RLock())

    def document(self, document_id):
        document = self.store.get('documents', document_id)
        state = self.store.snapshot()
        document['pages'] = [state['pages'][page_id] for page_id in document['page_ids']]
        return document

    def add_image(self, document_id, image, name, source=None, position=None):
        imaging.validate_image(image)
        self.store.get('documents', document_id)
        original_path = self.store.new_file('originals')
        thumb_path = self.store.new_file('thumbs', '.jpg')
        imaging.save_image(original_path, image)
        imaging.thumbnail(thumb_path, image)
        page_id = identity('page')
        original = self.store.relative(original_path)
        record = {
            'id': page_id,
            'document_id': document_id,
            'name': string(name, '页面名称', 200),
            'original': original,
            'current': original,
            'thumbnail': self.store.relative(thumb_path),
            'source': source,
            'sha256': digest(original_path),
            'width': image.shape[1],
            'height': image.shape[0],
            'revision': 0,
            'created_at': now(),
            'updated_at': now(),
            'ocr': None,
            'ocr_versions': [],
            'history': [],
            'regions': [],
            'quality': imaging.quality(image),
            # 非破坏性处理链：所有处理都记录为节点，最终图从原图按链重算。
            'recipe': [],
            'chain_revision': 0,
            'rendered_revision': 0,
            'chain_basis_revision': 0,
        }
        try:
            with self.store.transaction() as state:
                document = state['documents'].get(document_id)
                require(document is not None, '导入期间目标文档被删除', 409)
                state['pages'][page_id] = record
                if position is None:
                    document['page_ids'].append(page_id)
                else:
                    position = number(position, '插入位置', 0, len(document['page_ids']), True)
                    document['page_ids'].insert(position, page_id)
                document['updated_at'] = now()
                self.store.event(state, '导入页面', page_id, {'name': name})
        except Exception:
            original_path.unlink(missing_ok=True)
            thumb_path.unlink(missing_ok=True)
            raise
        return self.store.get('pages', page_id)

    def save_upload(self, uploaded):
        filename = Path(uploaded.filename or '').name
        require(filename and len(filename) <= 240, '上传文件名无效')
        extension = Path(filename).suffix.lower()
        require(extension in ALLOWED_EXTENSIONS, '仅支持 PNG/JPEG/TIFF/BMP/WebP/PDF')
        destination = self.store.new_file('incoming', extension)
        size = 0
        try:
            with destination.open('xb') as output:
                while True:
                    block = uploaded.stream.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    require(size <= MAX_FILE_BYTES, '单个文件不得超过60MB', 413)
                    output.write(block)
            require(size > 0, '上传文件为空')
            return destination, filename
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def import_path(self, document_id, path, name=None, position=None, cancelled=None):
        path = Path(path)
        name = name or path.name
        require(path.stat().st_size <= MAX_FILE_BYTES, '单个文件不得超过60MB', 413)
        from .sources import Sources
        sources = Sources(self.store)
        retained = sources.retain(path, name)
        imported = []
        if path.suffix.lower() == '.pdf':
            try:
                pdf = fitz.open(path)
            except Exception as error:
                raise DomainError('PDF 无法打开：' + str(error)) from error
            try:
                require(not pdf.is_encrypted, '不支持加密 PDF')
                require(0 < len(pdf) <= MAX_IMPORT_PAGES, 'PDF 必须为1–200页')
                for index, pdf_page in enumerate(pdf):
                    if cancelled and cancelled():
                        break
                    require(pdf_page.rect.width > 0 and pdf_page.rect.height > 0, 'PDF 页面尺寸无效')
                    scale = min(2, (imaging.MAX_PIXELS / (pdf_page.rect.width * pdf_page.rect.height)) ** 0.5)
                    pixmap = pdf_page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
                    image = np.frombuffer(pixmap.samples, np.uint8).reshape(pixmap.height, pixmap.width, 3)
                    image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
                    source = {'name': name, 'type': 'pdf', 'page': index + 1}
                    insertion = None if position is None else position + len(imported)
                    record = self.add_image(document_id, image, f'{Path(name).stem} · {index + 1}', source, insertion)
                    imported.append(record['id'])
                    sources.attach(retained['id'], [record['id']])
            finally:
                pdf.close()
        elif path.suffix.lower() in ('.tif', '.tiff'):
            try:
                with Image.open(path) as tiff:
                    require(tiff.n_frames <= MAX_IMPORT_PAGES, 'TIFF 最多200帧')
                    for frame in range(tiff.n_frames):
                        if cancelled and cancelled():
                            break
                        tiff.seek(frame)
                        require(tiff.width * tiff.height <= imaging.MAX_PIXELS, 'TIFF 页面过大')
                        rgb = np.asarray(ImageOps.exif_transpose(tiff).convert('RGB'))
                        image = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                        insertion = None if position is None else position + len(imported)
                        record = self.add_image(document_id, image, f'{Path(name).stem} · {frame + 1}', {'name': name, 'type': 'tiff', 'page': frame + 1}, insertion)
                        imported.append(record['id'])
                        sources.attach(retained['id'], [record['id']])
            except (OSError, ValueError) as error:
                raise DomainError('TIFF 读取失败：' + str(error)) from error
        else:
            image = imaging.read_image(path)
            record = self.add_image(document_id, image, Path(name).stem, {'name': name, 'type': 'image'}, position)
            imported.append(record['id'])
            sources.attach(retained['id'], [record['id']])
        return imported

    def _ensure_recipe(self, page):
        """旧版本页面没有处理链字段时补齐空链，保持旧文件原样可读。"""
        page.setdefault('recipe', [])
        page.setdefault('chain_revision', 0)
        page.setdefault('rendered_revision', 0)
        page.setdefault('chain_basis_revision', 0)

    def _mark_inapplicable(self, recipe):
        """依据链上信息丢失情况标记不适用节点，返回适用性差异说明。"""
        _, notes = recipes.evaluate_applicability(recipe)
        for item in notes:
            if item['code'] == 'color_unavailable':
                recipe[item['index']]['status'] = 'inapplicable'
        return notes

    def _history_entry(self, current, operation, params, metadata, before_path):
        """构造可回看的历史条目：保存当时的完整处理链快照。"""
        return {
            'id': identity('history'),
            'time': now(),
            'operation': operation,
            'params': copy.deepcopy(params),
            'metadata': {**metadata, 'chain_revision': current['chain_revision'], 'non_destructive': True,
                         'recipe_snapshot': copy.deepcopy(current['recipe'])},
            'before': before_path,
            'after': current['current'],
            'before_width': current['width'],
            'before_height': current['height'],
        }

    def _render_and_persist(self, state, page_id, reason):
        """按当前处理链从原图重算最终图，并原子替换 current/缩略图。

        预览、最终图像、提取区域与 OCR 有效状态都通过 chain_revision 与
        rendered_revision 对应到同一条链；链未确认（rendered 落后）时不得
        把重算结果当作已确认结果。
        """
        page = state['pages'][page_id]
        original = imaging.read_image(self.store.resolve(page['original']))
        result, outputs, metadatas, active_count, errors = recipes.render_chain(
            original, page['recipe'], collect=True, safe=True)
        path = self.store.new_file('processed')
        imaging.save_image(path, result)
        thumb_path = self.store.new_file('thumbs', '.jpg')
        imaging.thumbnail(thumb_path, result)
        page['current'] = self.store.relative(path)
        page['thumbnail'] = self.store.relative(thumb_path)
        page['width'] = result.shape[1]
        page['height'] = result.shape[0]
        page['quality'] = imaging.quality(result)
        page['rendered_revision'] = page['chain_revision']
        page['render'] = {
            'reason': reason,
            'time': now(),
            'active_nodes': active_count,
            'outputs': len(outputs) - 1,
            'runtime_errors': errors,
        }
        # OCR 与提取区域只在其绑定的链版本与当前链不一致时才失效：
        # 同一链上的重复重算不会把仍然对应的识别结果误判为过期。
        if page.get('ocr'):
            bound = page['ocr'].get('chain_revision')
            page['ocr']['stale'] = bound is not None and bound != page['chain_revision']
        for region in page.get('regions', []):
            bound = region.get('chain_revision', region.get('image_revision'))
            if bound != page['chain_revision']:
                region['stale'] = True
            else:
                region['stale'] = False
        self.store.event(state, '重算处理链', page_id, {'reason': reason, 'chain_revision': page['chain_revision']})
        return result

    def _chain_preview(self, page, nodes, index=None):
        """对候选链做一次不落盘的重算，返回预览图与适用性差异。"""
        original = imaging.read_image(self.store.resolve(page['original']))
        result = recipes.render_chain(original, nodes, upto_index=index)
        return result

    def apply(self, page_id, operation, params, expected_revision=None):
        """向后兼容的单步处理入口：把操作作为新节点追加到处理链。

        重复确认完全相同的方案不会再次叠加损伤——同一操作、同一参数且
        位于链尾时只返回当前链，不产生新版本。
        """
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            if expected_revision is not None:
                require(expected_revision == page['revision'], '页面已变化，请刷新后重试', 409, 'revision_conflict')
            params = validate_operation(operation, params)
            require(operation in recipes.OPERATION_INFO, '该操作不支持非破坏性处理链')
            with self.store.transaction() as state:
                current = state['pages'].get(page_id)
                require(current is not None, '页面已被删除', 409)
                require(current['revision'] == page['revision'], '页面版本冲突', 409)
                self._ensure_recipe(current)
                active = [node for node in current['recipe'] if recipes.node_status(node) == 'active']
                duplicated = bool(active and active[-1]['operation'] == operation and active[-1]['params'] == params)
                if not duplicated:
                    node = {
                        'id': identity('node'),
                        'operation': operation,
                        'params': copy.deepcopy(params),
                        'created_at': now(),
                        'status': 'active',
                        'note': '',
                    }
                    current['recipe'].append(node)
                    current['chain_revision'] += 1
                    notes = self._mark_inapplicable(current['recipe'])
                    before_path = current['current']
                    self._render_and_persist(state, page_id, 'append:' + operation)
                    current['history'].append(self._history_entry(
                        current, operation, params,
                        {'chain_node_id': node['id'], 'applicability': notes}, before_path))
                    current['revision'] += 1
                current['updated_at'] = now()
                self.store.event(state, '图像处理', page_id, {'operation': operation, 'duplicated': duplicated})
            return self.store.get('pages', page_id)

    def chain(self, page_id):
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            self._ensure_recipe(page)
            return recipes.public_chain(page)

    def chain_preview(self, page_id, values):
        """对候选修改做不落盘预览，并返回适用性差异供用户选择。"""
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            self._ensure_recipe(page)
            candidate = recipes.validate_chain(values.get('nodes'))
            color_available, notes = recipes.evaluate_applicability(candidate)
            result = self._chain_preview(page, candidate)
            path = self.store.new_file('previews')
            imaging.save_image(path, result)
            return {
                'preview_id': path.name,
                'preview_url': '/api/previews/' + path.name,
                'width': result.shape[1],
                'height': result.shape[0],
                'color_available': color_available,
                'applicability': notes,
                'chain_revision': page['chain_revision'],
                'nodes': [
                    {
                        'id': node['id'], 'index': index, 'operation': node['operation'],
                        'label': recipes.operation_label(node['operation']),
                        'stage': recipes.OPERATION_INFO[node['operation']]['stage'],
                        'params': node['params'], 'status': recipes.node_status(node),
                        'loses_color': recipes.loses_color(node['operation'], node['params']),
                        'needs_color': bool(recipes.OPERATION_INFO[node['operation']]['color']),
                    }
                    for index, node in enumerate(candidate)
                ],
            }

    def chain_commit(self, page_id, values, expected_revision=None):
        """确认一条候选链：从原图整体重算，保留仍有适用依据的下游节点。

        policy='recompute'（默认）：不适用节点标记 inapplicable 并保留，
        同时在返回中列出差异；policy='keep'：同样保留并标记，二者都不会
        静默删除用户确认过的处理。被标记节点可随后显式重算或移除。
        """
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            if expected_revision is not None:
                require(expected_revision == page['revision'], '页面已变化，请刷新后重试', 409, 'revision_conflict')
            candidate = recipes.validate_chain(values.get('nodes'))
            policy = values.get('policy', 'recompute')
            require(policy in ('recompute', 'keep'), '适用性策略无效')
            color_available, notes = recipes.evaluate_applicability(candidate)
            # 依据不足的节点不参与重算，但原样保留为 inapplicable，由用户选择。
            inapplicable_indexes = {item['index'] for item in notes if item['code'] == 'color_unavailable'}
            for index in inapplicable_indexes:
                candidate[index]['status'] = 'inapplicable'
            with self.store.transaction() as state:
                current = state['pages'].get(page_id)
                require(current is not None, '页面已被删除', 409)
                require(current['revision'] == page['revision'], '页面版本冲突', 409)
                self._ensure_recipe(current)
                before_path = current['current']
                current['recipe'] = candidate
                current['chain_revision'] += 1
                self._render_and_persist(state, page_id, 'commit')
                current['history'].append(self._history_entry(
                    current, 'chain_commit',
                    {'policy': policy, 'inapplicable': sorted(inapplicable_indexes)},
                    {'inapplicable': sorted(inapplicable_indexes)}, before_path))
                current['revision'] += 1
                current['updated_at'] = now()
                self.store.event(state, '确认处理链', page_id, {'inapplicable': sorted(inapplicable_indexes)})
            record = self.store.get('pages', page_id)
            return {'page': record, 'applicability': notes, 'color_available': color_available}

    def chain_upsert_step(self, page_id, operation, params, expected_revision=None):
        """光照、去污等可调步骤的就地改参入口：改参不叠加，始终从原图重算。

        只对允许重复且参数化的单例步骤开放；找不到同类型节点时追加为新节点。
        """
        require(operation in ('illumination', 'denoise'), '该操作不支持就地调参')
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            if expected_revision is not None:
                require(expected_revision == page['revision'], '页面已变化，请刷新后重试', 409, 'revision_conflict')
            params = validate_operation(operation, params)
            with self.store.transaction() as state:
                current = state['pages'].get(page_id)
                require(current is not None and current['revision'] == page['revision'], '页面版本冲突', 409)
                self._ensure_recipe(current)
                target = next((node for node in current['recipe'] if node['operation'] == operation), None)
                before_path = current['current']
                if target is None:
                    target = {
                        'id': identity('node'), 'operation': operation, 'params': copy.deepcopy(params),
                        'created_at': now(), 'status': 'active', 'note': '',
                    }
                    current['recipe'].append(target)
                    changed = True
                else:
                    changed = target['params'] != params
                    target['params'] = copy.deepcopy(params)
                    target['status'] = 'active'
                if changed:
                    current['chain_revision'] += 1
                    color_available, notes = recipes.evaluate_applicability(current['recipe'])
                    inapplicable_indexes = {item['index'] for item in notes if item['code'] == 'color_unavailable'}
                    for index in inapplicable_indexes:
                        current['recipe'][index]['status'] = 'inapplicable'
                    self._render_and_persist(state, page_id, 'upsert:' + operation)
                    current['history'].append(self._history_entry(
                        current, operation, params, {'upsert': True, 'applicability': notes}, before_path))
                    current['revision'] += 1
                else:
                    color_available, notes = recipes.evaluate_applicability(current['recipe'])
                current['updated_at'] = now()
                self.store.event(state, '调整处理步骤', page_id, {'operation': operation, 'changed': changed})
            record = self.store.get('pages', page_id)
            return {'page': record, 'applicability': notes, 'color_available': color_available, 'changed': changed}

    def chain_upsert_color(self, page_id, params, expected_revision=None):
        """色彩模式专用入口：已确认的色彩节点就地改参并从原图重算。

        灰度/黑白之后再切回彩色不再产生“仍是黑白”的结果——节点永远作用在
        原图重算出的彩色上游上；若链上已存在二值化等其他丢色节点，会通过
        适用性差异明确指出，而不是假装颜色可以恢复。
        """
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            if expected_revision is not None:
                require(expected_revision == page['revision'], '页面已变化，请刷新后重试', 409, 'revision_conflict')
            params = validate_operation('color', params)
            with self.store.transaction() as state:
                current = state['pages'].get(page_id)
                require(current is not None and current['revision'] == page['revision'], '页面版本冲突', 409)
                self._ensure_recipe(current)
                target = next((node for node in current['recipe'] if node['operation'] == 'color'), None)
                before_path = current['current']
                if target is None:
                    target = {
                        'id': identity('node'), 'operation': 'color', 'params': copy.deepcopy(params),
                        'created_at': now(), 'status': 'active', 'note': '',
                    }
                    current['recipe'].append(target)
                    changed = True
                else:
                    changed = target['params'] != params
                    target['params'] = copy.deepcopy(params)
                    target['status'] = 'active'
                if changed:
                    current['chain_revision'] += 1
                    color_available, notes = recipes.evaluate_applicability(current['recipe'])
                    inapplicable_indexes = {item['index'] for item in notes if item['code'] == 'color_unavailable'}
                    for index in inapplicable_indexes:
                        current['recipe'][index]['status'] = 'inapplicable'
                    self._render_and_persist(state, page_id, 'upsert_color')
                    current['history'].append(self._history_entry(
                        current, 'color', params, {'upsert': True, 'applicability': notes}, before_path))
                    current['revision'] += 1
                else:
                    color_available, notes = recipes.evaluate_applicability(current['recipe'])
                current['updated_at'] = now()
                self.store.event(state, '扫描色彩调整', page_id, {'mode': params.get('mode'), 'changed': changed})
            record = self.store.get('pages', page_id)
            return {'page': record, 'applicability': notes, 'color_available': color_available, 'changed': changed}

    def chain_update_node(self, page_id, node_id, values, expected_revision=None):
        """只修改链上一个节点的参数，其余已确认节点原样保留并重算。"""
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            if expected_revision is not None:
                require(expected_revision == page['revision'], '页面已变化，请刷新后重试', 409, 'revision_conflict')
            with self.store.transaction() as state:
                current = state['pages'].get(page_id)
                require(current is not None, '页面已被删除', 409)
                require(current['revision'] == page['revision'], '页面版本冲突', 409)
                self._ensure_recipe(current)
                target = next((node for node in current['recipe'] if node['id'] == node_id), None)
                require(target is not None, '处理链节点不存在', 404)
                if 'operation' in values:
                    require(values['operation'] in recipes.OPERATION_INFO, '操作不能进入处理链')
                    target['operation'] = values['operation']
                params = values.get('params', target['params'])
                target['params'] = validate_operation(target['operation'], params)
                target['status'] = 'active'
                before_path = current['current']
                current['chain_revision'] += 1
                color_available, notes = recipes.evaluate_applicability(current['recipe'])
                inapplicable_indexes = {item['index'] for item in notes if item['code'] == 'color_unavailable'}
                for index in inapplicable_indexes:
                    current['recipe'][index]['status'] = 'inapplicable'
                self._render_and_persist(state, page_id, 'update_node:' + node_id)
                current['history'].append(self._history_entry(
                    current, 'chain_update',
                    {'node_id': node_id, 'operation': target['operation'], 'params': copy.deepcopy(target['params'])},
                    {'node_id': node_id, 'applicability': notes}, before_path))
                current['revision'] += 1
                current['updated_at'] = now()
                self.store.event(state, '修改处理链节点', page_id, {'node_id': node_id})
            record = self.store.get('pages', page_id)
            return {'page': record, 'applicability': notes, 'color_available': color_available}

    def chain_remove_node(self, page_id, node_id, expected_revision=None):
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            if expected_revision is not None:
                require(expected_revision == page['revision'], '页面已变化，请刷新后重试', 409, 'revision_conflict')
            with self.store.transaction() as state:
                current = state['pages'].get(page_id)
                require(current is not None, '页面已被删除', 409)
                require(current['revision'] == page['revision'], '页面版本冲突', 409)
                self._ensure_recipe(current)
                before_len = len(current['recipe'])
                current['recipe'] = [node for node in current['recipe'] if node['id'] != node_id]
                require(len(current['recipe']) == before_len - 1, '处理链节点不存在', 404)
                for node in current['recipe']:
                    node['status'] = 'active'
                before_path = current['current']
                current['chain_revision'] += 1
                _, notes = recipes.evaluate_applicability(current['recipe'])
                self._render_and_persist(state, page_id, 'remove_node:' + node_id)
                current['history'].append(self._history_entry(
                    current, 'chain_remove', {'node_id': node_id}, {'node_id': node_id}, before_path))
                current['revision'] += 1
                current['updated_at'] = now()
                self.store.event(state, '移除处理链节点', page_id, {'node_id': node_id})
            return self.store.get('pages', page_id)

    def chain_recompute_node(self, page_id, node_id, params=None, expected_revision=None):
        """显式重算一个被标记为不适用的节点：必须由用户确认新参数。

        若上游仍不具备该节点所需信息（例如颜色已丢失），直接拒绝并说明，
        不能在已经丢失信息的结果上假装恢复。
        """
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            if expected_revision is not None:
                require(expected_revision == page['revision'], '页面已变化，请刷新后重试', 409, 'revision_conflict')
            with self.store.transaction() as state:
                current = state['pages'].get(page_id)
                require(current is not None and current['revision'] == page['revision'], '页面版本冲突', 409)
                target = next((node for node in current['recipe'] if node['id'] == node_id), None)
                require(target is not None, '处理链节点不存在', 404)
                info = recipes.OPERATION_INFO[target['operation']]
                upstream = current['recipe'][:current['recipe'].index(target)]
                color_available, _ = recipes.evaluate_applicability(upstream)
                if info['color'] and not color_available:
                    raise DomainError('上游已丢失彩色信息，%s 无法在不恢复原图颜色的情况下重算；请先调整或移除上游的灰度/黑白节点。' % info['label'], 409, 'basis_lost')
                if params is not None:
                    target['params'] = validate_operation(target['operation'], params)
                target['status'] = 'active'
                current['chain_revision'] += 1
                _, notes = recipes.evaluate_applicability(current['recipe'])
                self._render_and_persist(state, page_id, 'recompute_node:' + node_id)
                current['revision'] += 1
                current['updated_at'] = now()
                self.store.event(state, '重算处理链节点', page_id, {'node_id': node_id})
            return self.store.get('pages', page_id)

    def restore(self, page_id, history_id=None):
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            self._ensure_recipe(page)
            restored_recipe = None
            if history_id:
                entries = [entry for entry in page['history'] if entry['id'] == history_id]
                require(bool(entries), '历史版本不存在', 404)
                entry = entries[0]
                target = entry['after']
                # 历史条目保存了当时的完整链：恢复的是一条可回看、可重算的链，
                # 而不是把孤立像素稿当作新起点继续叠加。
                restored_recipe = entry.get('metadata', {}).get('recipe_snapshot')
            else:
                target = page['original']
            image = imaging.read_image(self.store.resolve(target))
            thumb_path = self.store.new_file('thumbs', '.jpg')
            imaging.thumbnail(thumb_path, image)
            with self.store.transaction() as state:
                current = state['pages'][page_id]
                current['history'].append({
                    'id': identity('history'),
                    'time': now(),
                    'operation': 'restore',
                    'params': {'history_id': history_id},
                    'metadata': {'chain_revision': current['chain_revision']},
                    'before': current['current'],
                    'after': target,
                    'before_width': current['width'],
                    'before_height': current['height'],
                })
                if restored_recipe is not None:
                    current['recipe'] = copy.deepcopy(restored_recipe)
                    current['chain_revision'] += 1
                    current['rendered_revision'] = current['chain_revision']
                else:
                    # 恢复原图：清空处理链但保留历史，几何校正不再静默丢失。
                    current['recipe'] = []
                    current['chain_revision'] = 0
                    current['rendered_revision'] = 0
                current['current'] = target
                current['thumbnail'] = self.store.relative(thumb_path)
                current['width'] = image.shape[1]
                current['height'] = image.shape[0]
                current['quality'] = imaging.quality(image)
                current['revision'] += 1
                current['updated_at'] = now()
                if current['ocr']:
                    current['ocr']['stale'] = True
                self.store.event(state, '恢复图像', page_id)
            return self.store.get('pages', page_id)

    def recognize(self, page_id, params):
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            self._ensure_recipe(page)
            require(page.get('rendered_revision') == page.get('chain_revision'), '处理链尚有未确认的修改，请先确认预览后再识别', 409, 'chain_unconfirmed')
            image = imaging.read_image(self.store.resolve(page['current']))
            result = ocr.recognize(image, params.get('language', 'chi_sim+eng'), params.get('psm', 3), params.get('minimum', 0))
            result['image_revision'] = page['revision']
            result['chain_revision'] = page['chain_revision']
            with self.store.transaction() as state:
                current = state['pages'].get(page_id)
                require(current is not None and current['revision'] == page['revision'], 'OCR 期间页面发生变化', 409)
                if current['ocr']:
                    current['ocr_versions'].append(current['ocr'])
                    current['ocr_versions'] = current['ocr_versions'][-20:]
                current['ocr'] = result
                current['updated_at'] = now()
                self.store.event(state, '文字识别', page_id, {'words': len(result['boxes']), 'confidence': result['confidence'], 'chain_revision': page['chain_revision']})
            return self.store.get('pages', page_id)

    def edit_ocr(self, page_id, values):
        with self.page_lock(page_id):
            with self.store.transaction() as state:
                current = state['pages'].get(page_id)
                require(current is not None, '页面不存在', 404)
                if current['ocr']:
                    current['ocr_versions'].append(copy.deepcopy(current['ocr']))
                    current['ocr_versions'] = current['ocr_versions'][-20:]
                current['ocr'] = ocr.edit_result(current['ocr'], values)
                current['updated_at'] = now()
                self.store.event(state, '校订文字', page_id)
            return self.store.get('pages', page_id)

    def reorder(self, document_id, page_ids):
        ids(page_ids)
        with self.store.transaction() as state:
            document = state['documents'].get(document_id)
            require(document is not None, '文档不存在', 404)
            require(set(page_ids) == set(document['page_ids']), '页序必须完整包含文档当前页面', 409)
            document['page_ids'] = page_ids
            document['updated_at'] = now()
            self.store.event(state, '调整页序', document_id)
        return self.document(document_id)

    def copy_pages(self, page_ids, target_id, position=None):
        ids(page_ids)
        target = self.store.get('documents', target_id)
        if position is None:
            position = len(target['page_ids'])
        position = number(position, '插入位置', 0, len(target['page_ids']), True)
        with self.store.transaction() as state:
            require(all(page_id in state['pages'] for page_id in page_ids), '待复制页面不存在', 404)
            created = []
            for page_id in page_ids:
                page = copy.deepcopy(state['pages'][page_id])
                page['id'] = identity('page')
                page['document_id'] = target_id
                page['name'] += ' · 副本'
                page['created_at'] = now()
                page['updated_at'] = now()
                state['pages'][page['id']] = page
                created.append(page['id'])
            state['documents'][target_id]['page_ids'][position:position] = created
            state['documents'][target_id]['updated_at'] = now()
            self.store.event(state, '复制页面', target_id, {'source': page_ids, 'created': created})
        return created

    def delete_pages(self, page_ids):
        ids(page_ids)
        with self.store.transaction() as state:
            require(all(page_id in state['pages'] for page_id in page_ids), '待删除页面不存在', 404)
            for page_id in page_ids:
                page = state['pages'].pop(page_id)
                document = state['documents'][page['document_id']]
                document['page_ids'].remove(page_id)
                document['updated_at'] = now()
            self.store.event(state, '删除页面', ','.join(page_ids))

    def move_pages(self, page_ids, target_id, position=None):
        ids(page_ids)
        self.store.get('documents', target_id)
        with self.store.transaction() as state:
            require(all(page_id in state['pages'] for page_id in page_ids), '页面不存在', 404)
            target = state['documents'][target_id]
            for page_id in page_ids:
                page = state['pages'][page_id]
                source = state['documents'][page['document_id']]
                source['page_ids'].remove(page_id)
                source['updated_at'] = now()
                page['document_id'] = target_id
            if position is None:
                position = len(target['page_ids'])
            position = number(position, '插入位置', 0, len(target['page_ids']), True)
            target['page_ids'][position:position] = page_ids
            target['updated_at'] = now()
            self.store.event(state, '移动页面', target_id, {'pages': page_ids})
        return self.document(target_id)

    def split(self, document_id, page_ids, name):
        ids(page_ids)
        source = self.store.get('documents', document_id)
        require(set(page_ids).issubset(source['page_ids']), '拆分页面不属于该文档')
        name = string(name, '新文档名称', 160)
        new_id = identity('document')
        with self.store.transaction() as state:
            source = state['documents'][document_id]
            require(set(page_ids).issubset(source['page_ids']), '文档在拆分期间发生变化', 409)
            ordered = [page_id for page_id in source['page_ids'] if page_id in page_ids]
            state['documents'][new_id] = {
                'id': new_id,
                'collection_id': source['collection_id'],
                'name': name,
                'category': source['category'],
                'page_ids': ordered,
                'created_at': now(),
                'updated_at': now(),
            }
            source['page_ids'] = [page_id for page_id in source['page_ids'] if page_id not in page_ids]
            source['updated_at'] = now()
            for page_id in ordered:
                state['pages'][page_id]['document_id'] = new_id
            self.store.event(state, '拆分文档', document_id, {'created': new_id, 'pages': ordered})
        return self.document(new_id)

    def merge(self, document_ids, name, collection_id=None, consume=False):
        ids(document_ids, '文档')
        state = self.store.snapshot()
        require(all(identifier in state['documents'] for identifier in document_ids), '合并文档不存在', 404)
        first = state['documents'][document_ids[0]]
        collection_id = collection_id or first['collection_id']
        self.store.get('collections', collection_id)
        new_id = identity('document')
        name = string(name, '合并名称', 160)
        with self.store.transaction() as state:
            new_document = {
                'id': new_id,
                'collection_id': collection_id,
                'name': name,
                'category': first['category'],
                'page_ids': [],
                'created_at': now(),
                'updated_at': now(),
            }
            state['documents'][new_id] = new_document
            for document_id in document_ids:
                document = state['documents'][document_id]
                for page_id in document['page_ids']:
                    if consume:
                        page = state['pages'][page_id]
                    else:
                        page = copy.deepcopy(state['pages'][page_id])
                        page['id'] = identity('page')
                        page['created_at'] = now()
                        state['pages'][page['id']] = page
                    page['document_id'] = new_id
                    new_document['page_ids'].append(page['id'])
                if consume:
                    state['documents'].pop(document_id)
            self.store.event(state, '合并文档', new_id, {'source': document_ids, 'consume': consume})
        return self.document(new_id)

    def extract_region(self, page_id, values):
        with self.page_lock(page_id):
            page = self.store.get('pages', page_id)
            self._ensure_recipe(page)
            require(page.get('rendered_revision') == page.get('chain_revision'), '处理链尚有未确认的修改，请先确认预览后再提取区域', 409, 'chain_unconfirmed')
            image = imaging.read_image(self.store.resolve(page['current']))
            x, y, w, h = imaging.rectangle(values.get('region'), image)
            path = self.store.new_file('regions')
            imaging.save_image(path, image[y:y + h, x:x + w])
            record = {
                'id': identity('region'),
                'name': string(values.get('name', '提取区域'), '区域名称', 100),
                'kind': values.get('kind', 'illustration'),
                'rect': [x, y, w, h],
                'path': self.store.relative(path),
                'image_revision': page['revision'],
                'chain_revision': page['chain_revision'],
                'stale': False,
                'created_at': now(),
            }
            require(record['kind'] in ('illustration', 'text', 'other'), '区域类型无效')
            with self.store.transaction() as state:
                state['pages'][page_id]['regions'].append(record)
                self.store.event(state, '提取区域', page_id, {'region_id': record['id'], 'chain_revision': page['chain_revision']})
            return record

    def rename_page(self, page_id, name):
        name = string(name, '页面名称', 200)
        with self.store.transaction() as state:
            require(page_id in state['pages'], '页面不存在', 404)
            state['pages'][page_id]['name'] = name
            self.store.event(state, '重命名页面', page_id)
        return self.store.get('pages', page_id)
