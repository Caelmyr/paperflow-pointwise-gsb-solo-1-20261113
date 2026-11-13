"""印章 / 批注分离工作区的业务服务。

边界与承诺：
- 只读页面的 current 图像，绝不修改正文页面、原稿或其他已提取素材；
- 工作区数据独立存放在 state['seal_projects'] 与 seals/ 文件目录；
- state 中只保存文件相对路径与标量，任何像素数组都不进入 JSON；
- 自动提取产生“版本”，人工在版本上做决定；换参数重算时保留人工决定；
- 重叠无法判定处保持“待确认”，系统不补内容；
- 导出的透明 PNG 与预览共用同一条合成链路，并记录来源页与版本依据。
"""
from __future__ import annotations

import copy

import cv2
import numpy as np

from . import seal_imaging as si
from .core import identity, now, require, string
from .imaging import read_image


class SealWorkbench:
    def __init__(self, store):
        self.store = store
        (store.root / 'seals').mkdir(exist_ok=True)

    # ------------------------------------------------------------------ 内部工具
    def _all_projects(self, state):
        return state.setdefault('seal_projects', {})

    def get_project(self, project_id):
        project = self.store.snapshot().get('seal_projects', {}).get(project_id)
        require(project is not None, '印章工作区不存在', 404)
        return copy.deepcopy(project)

    def _get_live_project(self, state, project_id):
        project = state.get('seal_projects', {}).get(project_id)
        require(project is not None, '印章工作区不存在', 404)
        return project

    def _active_version_id(self, project):
        version_id = project.get('active_version_id')
        require(version_id and version_id in project['versions'], '工作区尚无提取版本', 409)
        return version_id

    def _load_page_image(self, state, page_id):
        page = state['pages'].get(page_id)
        require(page is not None, '来源页面不存在', 404)
        path = self.store.resolve(page['current'])
        require(path.is_file(), '来源页面图像缺失', 409)
        return read_image(path), page

    def _write(self, filename, data):
        path = self.store.root / 'seals' / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.store.relative(path)

    def _read_mask(self, relative):
        path = self.store.resolve(relative)
        require(path.is_file(), 'mask 文件缺失：' + relative, 409)
        mask = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        require(mask is not None, 'mask 无法读取', 500)
        return si.mask_png_to_status(mask)

    def _stats(self, status):
        return {
            'keep': int(np.count_nonzero(status == si.MASK_VALUE[si.KEEP])),
            'review': int(np.count_nonzero(status == si.MASK_VALUE[si.REVIEW])),
            'exclude': int(np.count_nonzero(status == si.MASK_VALUE[si.EXCLUDE])),
            'width': int(status.shape[1]),
            'height': int(status.shape[0]),
        }

    def _compute_alpha(self, image, status):
        """统一的 alpha 计算，预览与导出共用，保证边缘一致。

        委托给 seal_imaging.build_alpha，确保前端预览、资源图与最终导出
        使用完全相同的软透明度规则（浅印泥低透明度、待确认保守半透明）。
        """
        return si.build_alpha(image, status)

    def _render_assets(self, project_id, version_id, image, status, touched):
        """在事务外把 mask / touched / rgba / 预览图全部落盘，返回相对路径字典。"""
        alpha = self._compute_alpha(image, status)
        rgba = si.composite_rgba(image, alpha, status)
        preview_bgr = si.overlay_preview(image, status)
        ok, preview_buf = cv2.imencode('.png', preview_bgr)
        require(ok, '预览编码失败', 500)
        return {
            'mask_path': self._write(f'{project_id}_{version_id}_mask.png', si.encode_png(status)),
            'touched_path': self._write(f'{project_id}_{version_id}_touched.png', si.encode_png(touched)),
            'rgba_path': self._write(f'{project_id}_{version_id}_rgba.png', si.encode_png(rgba)),
            'preview_path': self._write(f'{project_id}_{version_id}_preview.png', preview_buf.tobytes()),
        }

    # ------------------------------------------------------------------ 创建 / 查询
    def create_project(self, page_id, values):
        name = string(values.get('name', '印章分离'), '工作区名称', 160)
        state = self.store.snapshot()
        image, page = self._load_page_image(state, page_id)
        document = state['documents'][page['document_id']]
        page_index = document['page_ids'].index(page_id)
        project_id = identity('sealproj')
        record = {
            'id': project_id,
            'name': name,
            'page_id': page_id,
            'document_id': page['document_id'],
            'page_number': page_index + 1,
            'page_name': page['name'],
            'source_image': page['current'],
            'source_revision': page['revision'],
            'source_sha256': page.get('sha256'),
            'width': int(image.shape[1]),
            'height': int(image.shape[0]),
            'background': 'checker',
            'active_version_id': None,
            'versions': {},
            'version_order': [],
            'exports': [],
            'created_at': now(),
            'updated_at': now(),
        }
        with self.store.transaction() as candidate:
            self._all_projects(candidate)[project_id] = record
            self.store.event(candidate, '创建印章工作区', project_id, {'page_id': page_id})
        return self.get_project(project_id)

    def list_projects(self, document_id=None, page_id=None):
        projects = list(self.store.snapshot().get('seal_projects', {}).values())
        if document_id:
            projects = [p for p in projects if p['document_id'] == document_id]
        if page_id:
            projects = [p for p in projects if p['page_id'] == page_id]
        return [self._summary(p) for p in sorted(projects, key=lambda p: p['created_at'])]

    def _summary(self, project):
        active_id = project.get('active_version_id')
        active = project['versions'].get(active_id) if active_id else None
        return {
            'id': project['id'],
            'name': project['name'],
            'page_id': project['page_id'],
            'document_id': project['document_id'],
            'page_number': project['page_number'],
            'page_name': project['page_name'],
            'width': project['width'],
            'height': project['height'],
            'background': project['background'],
            'active_version_id': active_id,
            'version_count': len(project['version_order']),
            'export_count': len(project['exports']),
            'active_stats': copy.deepcopy(active['stats']) if active else None,
            'has_pending_review': bool(active and active['stats']['review'] > 0),
            'updated_at': project['updated_at'],
        }

    def detail(self, project_id):
        project = self.get_project(project_id)
        versions = []
        for vid in project['version_order']:
            version = project['versions'][vid]
            versions.append({
                'id': version['id'],
                'created_at': version['created_at'],
                'origin': version['origin'],
                'based_on_version': version.get('based_on_version'),
                'params': copy.deepcopy(version['params']),
                'stats': copy.deepcopy(version['stats']),
                'manual_edit_count': version.get('manual_edit_count', 0),
                'mask_path': version['mask_path'],
                'preview_path': version['preview_path'],
                'rgba_path': version['rgba_path'],
            })
        result = self._summary(project)
        result['versions'] = versions
        result['exports'] = copy.deepcopy(project['exports'])
        result['param_schema'] = si.describe_params()
        return result

    # ------------------------------------------------------------------ 自动提取 / 重算
    def extract(self, project_id, values, origin='auto'):
        params = si._validate_params(values or {})
        require(origin in ('auto', 'reextract'), '提取来源无效')
        project = self.get_project(project_id)
        state = self.store.snapshot()
        image, _ = self._load_page_image(state, project['page_id'])

        auto_status = si.separate(image, params)['status']
        version_id = identity('sealver')

        # 换参数重算：把上一版人工改动过的像素原样保留，人工决定优先于自动提取
        based_on = None
        touched = np.zeros(auto_status.shape, np.uint8)
        if origin == 'reextract' and project.get('active_version_id'):
            current = project['versions'][project['active_version_id']]
            based_on = current['id']
            prior_manual = self._read_mask(current['mask_path'])
            touched = self._read_mask(current['touched_path'])
            effective_status = si.reconcile_manual_edits(auto_status, prior_manual, touched)
            manual_count = current.get('manual_edit_count', 0)
        else:
            effective_status = auto_status
            manual_count = 0

        assets = self._render_assets(project_id, version_id, image, effective_status, touched)
        with self.store.transaction() as candidate:
            live = self._get_live_project(candidate, project_id)
            version = {
                'id': version_id,
                'origin': 'reextract' if based_on else 'auto',
                'based_on_version': based_on,
                'params': params,
                'stats': self._stats(effective_status),
                'manual_edit_count': manual_count,
                'last_manual_at': None,
                'created_at': now(),
                **assets,
            }
            live['versions'][version_id] = version
            live['version_order'].append(version_id)
            live['active_version_id'] = version_id
            live['updated_at'] = now()
            self.store.event(candidate, '印章自动提取', project_id,
                             {'version_id': version_id, 'origin': version['origin']})
        return self.detail(project_id)

    # ------------------------------------------------------------------ 人工决定
    def edit(self, project_id, values):
        target = values.get('target')
        require(target in ('keep', 'exclude', 'review'), '人工标记目标无效')
        geometry = values.get('geometry')
        require(isinstance(geometry, dict), '需要标记形状 geometry')
        project = self.get_project(project_id)
        active_id = self._active_version_id(project)
        state = self.store.snapshot()
        image, _ = self._load_page_image(state, project['page_id'])
        version = project['versions'][active_id]

        base = self._read_mask(version['mask_path'])
        touched = self._read_mask(version['touched_path'])
        before = base.copy()
        new_status = si.apply_manual_edits(base, [{'target': target, 'geometry': geometry}])
        changed = new_status != before
        touched = touched.copy()
        touched[changed] = 255
        changed_count = int(np.count_nonzero(changed))
        assets = self._render_assets(project_id, active_id, image, new_status, touched)

        with self.store.transaction() as candidate:
            live = self._get_live_project(candidate, project_id)
            live_version = live['versions'][active_id]
            live_version.update(assets)
            live_version['manual_edit_count'] = live_version.get('manual_edit_count', 0) + changed_count
            live_version['stats'] = self._stats(new_status)
            live_version['last_manual_at'] = now()
            live['updated_at'] = now()
            self.store.event(candidate, '人工印章标记', project_id,
                             {'version_id': active_id, 'target': target, 'changed': changed_count})
        return self.detail(project_id)

    def activate_version(self, project_id, version_id):
        project = self.get_project(project_id)
        require(version_id in project['versions'], '版本不存在', 404)
        with self.store.transaction() as candidate:
            live = self._get_live_project(candidate, project_id)
            live['active_version_id'] = version_id
            live['updated_at'] = now()
            self.store.event(candidate, '切换印章版本', project_id, {'version_id': version_id})
        return self.detail(project_id)

    def set_background(self, project_id, background):
        require(background in ('white', 'dark', 'checker', 'paper'), '纸底无效')
        with self.store.transaction() as candidate:
            live = self._get_live_project(candidate, project_id)
            live['background'] = background
            live['updated_at'] = now()
            self.store.event(candidate, '切换印章预览纸底', project_id, {'background': background})
        return self.get_project(project_id)

    # ------------------------------------------------------------------ 预览图 / 导出
    def _resolve_asset(self, project_id, version_id, kind):
        project = self.get_project(project_id)
        if version_id is None:
            version_id = self._active_version_id(project)
        version = project['versions'].get(version_id)
        require(version is not None, '版本不存在', 404)
        field = {'mask': 'mask_path', 'preview': 'preview_path', 'rgba': 'rgba_path'}[kind]
        return self.store.resolve(version[field]), version

    def asset_response_path(self, project_id, version_id, kind):
        path, _ = self._resolve_asset(project_id, version_id, kind)
        require(path.is_file(), '资源文件缺失', 404)
        return path

    def composed_preview(self, project_id, background=None):
        """按当前纸底合成的 BGR 预览字节（与导出同一条合成链路）。"""
        project = self.get_project(project_id)
        active_id = self._active_version_id(project)
        state = self.store.snapshot()
        image, _ = self._load_page_image(state, project['page_id'])
        version = project['versions'][active_id]
        status = self._read_mask(version['mask_path'])
        alpha = self._compute_alpha(image, status)
        rgba = si.composite_rgba(image, alpha, status)
        background = background or project['background']
        composed = si.composite_on_background(rgba, background)
        ok, buf = cv2.imencode('.png', composed)
        require(ok, '预览合成失败', 500)
        return buf.tobytes(), background

    def export_png(self, project_id, values):
        values = values or {}
        include_review = values.get('include_review', False)
        crop = values.get('crop', True)
        require(isinstance(include_review, bool), '待确认处理选项无效')
        require(isinstance(crop, bool), '裁剪选项无效')
        project = self.get_project(project_id)
        active_id = self._active_version_id(project)
        state = self.store.snapshot()
        image, _ = self._load_page_image(state, project['page_id'])
        version = project['versions'][active_id]
        status = self._read_mask(version['mask_path']).copy()

        # 未解决的待确认像素默认不纳入导出：保持待确认，不替用户做归属决定
        unresolved_review = int(np.count_nonzero(status == si.MASK_VALUE[si.REVIEW]))
        if not include_review:
            status[status == si.MASK_VALUE[si.REVIEW]] = si.MASK_VALUE[si.EXCLUDE]

        alpha = self._compute_alpha(image, status)
        if not include_review:
            alpha[status == si.MASK_VALUE[si.EXCLUDE]] = 0
        rgba = si.composite_rgba(image, alpha, status)
        bbox = None
        if crop:
            rgba, bbox = si.crop_to_content(rgba)
        data = si.encode_png(rgba)
        export_id = identity('sealexp')
        filename = f"{string(project['name'], '名称', 120)}_{export_id[-8:]}.png"
        path_rel = self._write(f'{project_id}_{export_id}_export.png', data)
        record = {
            'id': export_id,
            'filename': filename,
            'path': path_rel,
            'mime': 'image/png',
            'bytes': len(data),
            'page_id': project['page_id'],
            'document_id': project['document_id'],
            'page_number': project['page_number'],
            'page_name': project['page_name'],
            'source_image': project['source_image'],
            'source_revision': project['source_revision'],
            'source_sha256': project.get('source_sha256'),
            'version_id': active_id,
            'version_params': copy.deepcopy(version['params']),
            'background': project['background'],
            'include_review': include_review,
            'unresolved_review_at_export': unresolved_review,
            'crop': crop,
            'bbox': bbox,
            'width': int(rgba.shape[1]),
            'height': int(rgba.shape[0]),
            'created_at': now(),
        }
        with self.store.transaction() as candidate:
            live = self._get_live_project(candidate, project_id)
            live['exports'].append(record)
            live['updated_at'] = now()
            self.store.event(candidate, '导出印章透明图', project_id,
                             {'export_id': export_id, 'version_id': active_id})
        return record

    def delete_project(self, project_id):
        project = self.get_project(project_id)
        with self.store.transaction() as candidate:
            self._all_projects(candidate).pop(project_id)
            self.store.event(candidate, '删除印章工作区', project_id)
        # 文件清理在事务外，失败不影响 JSON 一致性
        for path in (self.store.root / 'seals').glob(project_id + '_*'):
            try:
                path.unlink()
            except OSError:
                pass
        return {'deleted': project_id}
