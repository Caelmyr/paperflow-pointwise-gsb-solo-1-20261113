"""交付清单的扫描、构建与版本差异比对。

清单只记录 *文件内容哈希* 与 *页面顺序*，不记录 ZIP 的压缩参数、
时间戳或条目排列，因此重新压缩只要内容不变就不会误报；
替换图像、调整页序、修改文字都会落到具体页面/文件上。
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path, PurePosixPath

from .core import (
    MANIFEST_FORMAT,
    MANIFEST_SCHEMA,
    SealError,
    file_sha256,
    now_iso,
)

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp', '.bmp'}
TEXT_EXTENSIONS = {'.txt', '.md', '.csv'}
MAX_FILES = 5000
MAX_TOTAL_BYTES = 512 * 1024 * 1024
NOTES_PREFIX = 'delivery-notes/'


def safe_member(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 400:
        raise SealError('压缩包文件名无效：' + repr(name), 'unsafe_path')
    if '\\' in name or '\x00' in name or ':' in name:
        raise SealError('压缩包包含危险文件名：' + name, 'unsafe_path')
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise SealError('压缩包包含越界路径：' + name, 'unsafe_path')
    if any(part in ('', '.') for part in path.parts):
        raise SealError('压缩包包含空路径：' + name, 'unsafe_path')
    return path.as_posix()


def natural_key(value: str):
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r'(\d+)', value)]


def _kind_for(relative: str) -> str:
    suffix = PurePosixPath(relative).suffix.lower()
    if relative == 'metadata.json':
        return 'document_metadata'
    if relative in ('recognized.txt', 'README.txt') or relative.startswith(NOTES_PREFIX):
        return 'note'
    if relative.startswith('sources/'):
        return 'source'
    if relative.startswith('regions/'):
        return 'region_image'
    if relative.startswith('thumbs/'):
        return 'thumbnail'
    if relative.startswith('processed/'):
        return 'page_image'
    if relative.startswith('originals/'):
        return 'original_image'
    if suffix in TEXT_EXTENSIONS:
        return 'note'
    if suffix in IMAGE_EXTENSIONS:
        return 'page_image'
    return 'other'


def _hash_stream(stream) -> tuple[str, int]:
    import hashlib
    hasher = hashlib.sha256()
    total = 0
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        hasher.update(block)
        total += len(block)
    return hasher.hexdigest(), total


def scan_paperflow_zip(zip_path: Path) -> dict:
    """解析 PaperFlow 导出 ZIP（含 metadata.json/order/sources/ocr 文本）。"""
    try:
        archive = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as error:
        raise SealError('ZIP 文件无法打开：' + str(error), 'bad_zip') from error
    files: dict[str, dict] = {}
    total_bytes = 0
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise SealError('交付文件数超过上限 %d' % MAX_FILES, 'too_many_files')
        names = set()
        for info in infos:
            name = safe_member(info.filename)
            if name in names:
                raise SealError('归档包含重名文件：' + name, 'duplicate_member')
            names.add(name)
            mode = info.external_attr >> 16
            if mode & 0o170000 == 0o120000:
                raise SealError('归档不得包含符号链接：' + name, 'symlink_member')
            if info.flag_bits & 1:
                raise SealError('不支持加密归档：' + name, 'encrypted_member')
        if 'metadata.json' not in names:
            raise SealError('不是 PaperFlow 归档：缺少 metadata.json', 'not_paperflow')
        for info in infos:
            name = safe_member(info.filename)
            with archive.open(info) as stream:
                checksum, size = _hash_stream(stream)
            total_bytes += size
            if total_bytes > MAX_TOTAL_BYTES:
                raise SealError('交付总大小超过 512MB 上限', 'too_large')
            files[name] = {
                'path': name,
                'sha256': checksum,
                'bytes': size,
                'kind': _kind_for(name),
            }
        try:
            metadata = json.loads(archive.read('metadata.json').decode('utf-8'))
        except (ValueError, UnicodeError) as error:
            raise SealError('metadata.json 不是有效 JSON', 'bad_metadata') from error
    if metadata.get('format') != 'paperflow-archive' or metadata.get('schema') != 1:
        raise SealError('不是受支持的 PaperFlow 归档格式', 'not_paperflow')
    pages_by_id = {page['id']: page for page in metadata.get('pages', [])}
    order = metadata.get('order', [])
    if not isinstance(order, list) or not order or set(order) != set(pages_by_id):
        raise SealError('归档页序 order 与页面记录不一致', 'bad_order')
    pages = []
    for seq, page_id in enumerate(order, start=1):
        page = pages_by_id[page_id]
        image_path = safe_member(page['current'])
        if image_path not in files:
            raise SealError('页面图像缺失：' + image_path, 'missing_payload')
        ocr = page.get('ocr') or {}
        text = ocr.get('text')
        pages.append({
            'seq': seq,
            'page_id': page_id,
            'name': str(page.get('name') or page_id),
            'image_path': image_path,
            'image_sha256': files[image_path]['sha256'],
            'text_sha256': _text_digest(text) if text is not None else None,
            'ocr_edited': bool(ocr.get('edited')),
            'source_file_id': page.get('source_file_id'),
        })
    sources = []
    for source_id, source in sorted(metadata.get('sources', {}).items()):
        source_path = safe_member(source['path'])
        if source_path not in files:
            raise SealError('来源文件缺失：' + source_path, 'missing_payload')
        sources.append({
            'source_id': source_id,
            'name': source.get('name'),
            'path': source_path,
            'sha256': files[source_path]['sha256'],
            'bytes': files[source_path]['bytes'],
        })
    document = metadata.get('document', {})
    notes = [
        {'path': item['path'], 'sha256': item['sha256'], 'bytes': item['bytes']}
        for item in files.values() if item['kind'] == 'note'
    ]
    return {
        'mode': 'paperflow_zip',
        'source_name': Path(zip_path).name,
        'document': {
            'id': document.get('id'),
            'name': document.get('name'),
            'category': document.get('category'),
        },
        'paperflow': {
            'format': metadata.get('format'),
            'schema': metadata.get('schema'),
            'exported_at': metadata.get('exported_at'),
        },
        'page_order': list(order),
        'pages': pages,
        'sources': sources,
        'notes': sorted(notes, key=lambda item: item['path']),
        'files': sorted(files.values(), key=lambda item: item['path']),
        'total_bytes': total_bytes,
    }


def scan_directory(root: Path) -> dict:
    """扫描普通目录：图像按自然排序作为页序，文本文件作为说明项。"""
    root = Path(root)
    if not root.is_dir():
        raise SealError('来源目录不存在：' + str(root), 'not_directory')
    files: dict[str, dict] = {}
    total_bytes = 0
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise SealError('交付目录不得包含符号链接：' + str(path), 'symlink_member')
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        safe_member(relative)
        if len(files) >= MAX_FILES:
            raise SealError('交付文件数超过上限 %d' % MAX_FILES, 'too_many_files')
        size = path.stat().st_size
        total_bytes += size
        if total_bytes > MAX_TOTAL_BYTES:
            raise SealError('交付总大小超过 512MB 上限', 'too_large')
        files[relative] = {
            'path': relative,
            'sha256': file_sha256(path),
            'bytes': size,
            'kind': _kind_for(relative),
        }
    if not files:
        raise SealError('交付目录为空', 'empty_delivery')
    image_paths = sorted(
        (path for path, item in files.items() if item['kind'] == 'page_image'),
        key=natural_key,
    )
    pages = []
    for seq, image_path in enumerate(image_paths, start=1):
        pages.append({
            'seq': seq,
            'page_id': image_path,
            'name': PurePosixPath(image_path).name,
            'image_path': image_path,
            'image_sha256': files[image_path]['sha256'],
            'text_sha256': None,
            'ocr_edited': False,
            'source_file_id': None,
        })
    notes = [
        {'path': item['path'], 'sha256': item['sha256'], 'bytes': item['bytes']}
        for item in files.values() if item['kind'] == 'note'
    ]
    return {
        'mode': 'directory',
        'source_name': root.name,
        'document': {'id': None, 'name': root.name, 'category': None},
        'paperflow': None,
        'page_order': [page['page_id'] for page in pages],
        'pages': pages,
        'sources': [],
        'notes': sorted(notes, key=lambda item: item['path']),
        'files': sorted(files.values(), key=lambda item: item['path']),
        'total_bytes': total_bytes,
    }


def scan_input(source: Path) -> dict:
    source = Path(source)
    if source.is_dir():
        return scan_directory(source)
    if source.is_file() and source.suffix.lower() == '.zip':
        return scan_paperflow_zip(source)
    raise SealError('签封来源必须是 PaperFlow 导出 ZIP 或目录：' + str(source), 'bad_source')


def _text_digest(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def build_manifest(scan: dict, delivery_name: str, delivery_id: str,
                   extra_notes: list[dict] | None = None) -> dict:
    """把扫描结果组装成待签名清单（尚不含签名字段）。"""
    files = [dict(item) for item in scan['files']]
    notes = [dict(item) for item in scan['notes']]
    existing = {item['path'] for item in files}
    for note in extra_notes or []:
        if note['path'] in existing:
            raise SealError('说明文件与交付内文件重名：' + note['path'], 'duplicate_member')
        existing.add(note['path'])
        entry = {
            'path': note['path'],
            'sha256': note['sha256'],
            'bytes': note['bytes'],
            'kind': 'note',
        }
        files.append(entry)
        notes.append({'path': note['path'], 'sha256': note['sha256'], 'bytes': note['bytes']})
    files.sort(key=lambda item: item['path'])
    notes.sort(key=lambda item: item['path'])
    return {
        'format': MANIFEST_FORMAT,
        'schema': MANIFEST_SCHEMA,
        'delivery_id': delivery_id,
        'delivery_name': delivery_name,
        'created_at': now_iso(),
        'source': {
            'mode': scan['mode'],
            'source_name': scan['source_name'],
            'document': scan['document'],
            'paperflow': scan['paperflow'],
        },
        'page_order': list(scan['page_order']),
        'pages': [dict(page) for page in scan['pages']],
        'sources': [dict(source) for source in scan['sources']],
        'notes': notes,
        'files': files,
        'file_count': len(files),
        'total_bytes': sum(item['bytes'] for item in files),
    }


def load_manifest(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise SealError('清单必须是 JSON 对象', 'bad_manifest')
    if raw.get('format') != MANIFEST_FORMAT or raw.get('schema') != MANIFEST_SCHEMA:
        raise SealError('不是受支持的签封清单格式', 'bad_manifest')
    for field in ('delivery_id', 'page_order', 'pages', 'files', 'seal'):
        if field not in raw:
            raise SealError('清单缺少字段：' + field, 'bad_manifest')
    return raw


def _index_pages(manifest: dict) -> dict:
    return {page['page_id']: page for page in manifest.get('pages', [])}


def _index_files(manifest: dict) -> dict:
    return {item['path']: item for item in manifest.get('files', [])}


def diff_manifests(before: dict, after: dict) -> dict:
    """比较两份清单，定位到具体页面/说明/文件的变化。"""
    before_pages = _index_pages(before)
    after_pages = _index_pages(after)
    before_order = before.get('page_order', [])
    after_order = after.get('page_order', [])

    order_changed = before_order != after_order
    moved = []
    if order_changed:
        before_position = {page_id: index for index, page_id in enumerate(before_order)}
        after_position = {page_id: index for index, page_id in enumerate(after_order)}
        for page_id in after_order:
            if page_id in before_position and before_position[page_id] != after_position[page_id]:
                moved.append({
                    'page_id': page_id,
                    'name': after_pages[page_id]['name'],
                    'before_seq': before_position[page_id] + 1,
                    'after_seq': after_position[page_id] + 1,
                })
        moved.sort(key=lambda item: item['after_seq'])

    page_changes = []
    all_page_ids = sorted(set(before_pages) | set(after_pages))
    for page_id in all_page_ids:
        old = before_pages.get(page_id)
        new = after_pages.get(page_id)
        if old is None:
            page_changes.append({'page_id': page_id, 'name': new['name'], 'change': 'page_added',
                                 'before': None, 'after': _page_brief(new)})
            continue
        if new is None:
            page_changes.append({'page_id': page_id, 'name': old['name'], 'change': 'page_removed',
                                 'before': _page_brief(old), 'after': None})
            continue
        image_changed = old['image_sha256'] != new['image_sha256']
        text_changed = old.get('text_sha256') != new.get('text_sha256')
        if image_changed and text_changed:
            change = 'image_and_text_changed'
        elif image_changed:
            change = 'image_replaced'
        elif text_changed:
            change = 'text_modified'
        else:
            continue
        page_changes.append({'page_id': page_id, 'name': new['name'], 'change': change,
                             'before': _page_brief(old), 'after': _page_brief(new)})

    note_changes = _compare_named(before.get('notes', []), after.get('notes', []), 'path')
    file_changes = _compare_named(before.get('files', []), after.get('files', []), 'path')
    summary = {
        'order_changed': order_changed,
        'pages_moved': len(moved),
        'pages_image_replaced': sum(item['change'] in ('image_replaced', 'image_and_text_changed')
                                    for item in page_changes),
        'pages_text_modified': sum(item['change'] in ('text_modified', 'image_and_text_changed')
                                   for item in page_changes),
        'pages_added': sum(item['change'] == 'page_added' for item in page_changes),
        'pages_removed': sum(item['change'] == 'page_removed' for item in page_changes),
        'notes_changed': len(note_changes),
        'files_added': sum(item['change'] == 'added' for item in file_changes),
        'files_removed': sum(item['change'] == 'removed' for item in file_changes),
        'files_modified': sum(item['change'] == 'modified' for item in file_changes),
    }
    summary['has_changes'] = any(summary[key] for key in summary if key != 'has_changes')
    return {
        'before_delivery': before.get('delivery_id'),
        'after_delivery': after.get('delivery_id'),
        'summary': summary,
        'order': {
            'changed': order_changed,
            'before': before_order,
            'after': after_order,
            'moved': moved,
        },
        'pages': page_changes,
        'notes': note_changes,
        'files': file_changes,
    }


def _page_brief(page: dict) -> dict:
    return {
        'seq': page['seq'],
        'name': page['name'],
        'image_path': page['image_path'],
        'image_sha256': page['image_sha256'],
        'text_sha256': page.get('text_sha256'),
    }


def _compare_named(before_items: list, after_items: list, key: str) -> list:
    before_index = {item[key]: item for item in before_items}
    after_index = {item[key]: item for item in after_items}
    changes = []
    for name in sorted(set(before_index) | set(after_index)):
        old = before_index.get(name)
        new = after_index.get(name)
        if old is None:
            changes.append({'name': name, 'change': 'added', 'before': None, 'after': new})
        elif new is None:
            changes.append({'name': name, 'change': 'removed', 'before': old, 'after': None})
        elif old.get('sha256') != new.get('sha256') or old.get('bytes') != new.get('bytes'):
            changes.append({'name': name, 'change': 'modified', 'before': old, 'after': new})
    return changes
