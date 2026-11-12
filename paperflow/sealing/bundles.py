"""交付件打包、签封与只读核验。

交付 ZIP 内部布局::

    payload/<交付文件相对路径>
    delivery-notes/<附加说明>
    META-INF/manifest.json      # 清单（即被签名的规范化字节）
    META-INF/signature.bin      # Ed25519 原始签名（清单内另存十六进制）
    META-INF/signer-public.pem  # 签发者公钥（信任判定仍以本地 trust/ 为准）
    META-INF/VERIFY.txt         # 离线核验说明

哈希只覆盖 payload 内容与清单结构，与 ZIP 压缩参数无关，
所以重新压缩（甚至改名）不会误报；内容一动即定位到具体页面/文件。
"""

from __future__ import annotations

import json
import os
import stat
import uuid
import zipfile
from pathlib import Path

from .core import (
    SIGNATURE_ALGORITHM,
    SealError,
    canonical_bytes,
    file_sha256,
    hash_bytes,
    now_iso,
)
from .keys import (
    KeyStore,
    fingerprint_hex,
    manifest_fingerprint,
    sign_bytes,
    verify_bytes,
)
from .manifest import (
    build_manifest,
    diff_manifests,
    load_manifest,
    safe_member,
    scan_input,
)
from .registry import SealRegistry

META_PREFIX = 'META-INF/'
PAYLOAD_PREFIX = 'payload/'
MANIFEST_MEMBER = META_PREFIX + 'manifest.json'
SIGNATURE_MEMBER = META_PREFIX + 'signature.bin'
PUBLIC_KEY_MEMBER = META_PREFIX + 'signer-public.pem'
VERIFY_GUIDE_MEMBER = META_PREFIX + 'VERIFY.txt'

VERIFY_GUIDE = """PaperFlow 签封交付件 · 离线核验说明
====================================

本交付件由签发者用 Ed25519 私钥对 META-INF/manifest.json 的规范化字节签名。
manifest.json 内列出每个交付文件的相对路径、字节数与 SHA-256，以及页面顺序、
每页图像哈希、人工校订文字哈希和来源文件信息。

核验步骤（在收件方工作台执行，全程离线、只读）：

  1. 先把签发者公钥加入本地受信库（公钥应通过独立可信渠道获取，勿只信包内公钥）：
       python -m paperflow.seal trust add --public-key 签发者公钥.pem --name 单位名称
     查看受信指纹：
       python -m paperflow.seal trust list

  2. 核验交付件（先判定签发者是否受信，再逐项比对内容与页序）：
       python -m paperflow.seal verify --bundle 本交付件.sealed.zip
     与上一版交付件比较，查看哪一页/哪项说明变化：
       python -m paperflow.seal verify --bundle 本交付件.sealed.zip --baseline 上一版.sealed.zip

  3. 判定原则：
     - 重新压缩或修改 ZIP 文件名不影响核验（哈希只算文件内容，不算压缩容器）；
     - 替换图像、调整页序、修改人工校订文字或说明文件，报告会定位到具体页面/文件；
     - 核验不会向交付件写入任何内容；签发者不受信或签名不符时整体判为不可信。

无 Python 环境时，可用 OpenSSL 3.x 手工验签：
  openssl pkeyutl -verify -pubin -inkey signer-public.pem -rawin \\
    -in manifest.json -sigfile signature.bin
"""


def new_delivery_id() -> str:
    return 'deliv_' + uuid.uuid4().hex


def signed_blob_from_manifest(manifest: dict) -> bytes:
    """重建签封时的被签字节：把两个签名字段还原为空字符串后规范化。

    这样核验端不需要任何额外文件，仅凭包内 manifest.json 就能
    确定性地复算出与签发端完全相同的字节序列。
    """
    seal = manifest['seal']
    baseline_seal = dict(seal)
    baseline_seal['manifest_sha256'] = ''
    baseline_seal['signature_hex'] = ''
    baseline = dict(manifest)
    baseline['seal'] = baseline_seal
    return canonical_bytes(baseline)


def _read_note(note_path: Path) -> dict:
    note_path = Path(note_path)
    if not note_path.is_file() or note_path.is_symlink():
        raise SealError('说明文件不存在或为符号链接：' + str(note_path), 'bad_note')
    data = note_path.read_bytes()
    if len(data) > 20 * 1024 * 1024:
        raise SealError('说明文件超过 20MB：' + note_path.name, 'bad_note')
    stored = safe_member(NOTES_STORE_PREFIX + note_path.name)
    return {
        'path': stored,
        'sha256': hash_bytes(data),
        'bytes': len(data),
        'source_path': str(note_path),
    }


NOTES_STORE_PREFIX = 'delivery-notes/'


def listing_text(manifest: dict, *, signed: bool) -> str:
    lines = []
    title = '签封交付清单（已签封）' if signed else '交付内容确认单（待确认，尚未签封）'
    lines.append(title)
    lines.append('=' * 48)
    lines.append(f"交付编号：{manifest['delivery_id']}")
    lines.append(f"交付名称：{manifest['delivery_name']}")
    lines.append(f"来源模式：{manifest['source']['mode']}（{manifest['source']['source_name']}）")
    document = manifest['source'].get('document') or {}
    if document.get('name'):
        lines.append(f"来源文档：{document['name']}")
    lines.append(f"文件总数：{manifest['file_count']}    总字节：{manifest['total_bytes']}")
    lines.append(f"页面数量：{len(manifest['pages'])}    页序："
                 + ' -> '.join(manifest['page_order'][:20])
                 + (' ...' if len(manifest['page_order']) > 20 else ''))
    if signed:
        seal = manifest['seal']
        lines.append(f"签封时间：{seal['sealed_at']}")
        lines.append(f"签发者：{seal['signer']['name']}")
        lines.append(f"签发者指纹：{seal['signer']['fingerprint']}")
        lines.append(f"签名算法：{seal['algorithm']}")
    lines.append('')
    lines.append('页面与人工校订文字：')
    for page in manifest['pages']:
        edited = '（含人工校订）' if page.get('ocr_edited') else ''
        text_state = page['text_sha256'] or '无文字记录'
        lines.append(
            f"  第 {page['seq']:>3d} 页 {page['name']}{edited}\n"
            f"           图像 {page['image_path']}  sha256={page['image_sha256']}\n"
            f"           文字 sha256={text_state}"
        )
    if manifest['sources']:
        lines.append('')
        lines.append('来源文件：')
        for source in manifest['sources']:
            lines.append(f"  {source['name'] or source['source_id']}  {source['path']}  "
                         f"sha256={source['sha256']}")
    lines.append('')
    lines.append('全部交付文件：')
    for item in manifest['files']:
        lines.append(f"  [{item['kind']}] {item['path']}  {item['bytes']} 字节  "
                     f"sha256={item['sha256']}")
    lines.append('')
    lines.append('确认无误后执行 seal 子命令签封；签封后任何内容改动都会在 verify 时暴露。')
    return '\n'.join(lines) + '\n'


def prepare_draft(source: Path, draft_dir: Path, delivery_name: str | None = None,
                  note_paths: list[Path] | None = None) -> dict:
    """第一步：扫描来源并生成待确认清单（不签名、不改来源）。"""
    source = Path(source)
    draft_dir = Path(draft_dir)
    draft_dir.mkdir(parents=True, exist_ok=True)
    scan = scan_input(source)
    delivery_name = (delivery_name or scan['document'].get('name')
                     or scan['source_name']).strip()
    if not delivery_name or len(delivery_name) > 160:
        raise SealError('交付名称必填且不超过 160 字')
    notes = [_read_note(path) for path in (note_paths or [])]
    if len({item['path'] for item in notes}) != len(notes):
        raise SealError('附加说明文件重名，请先改名', 'duplicate_member')
    manifest = build_manifest(scan, delivery_name, new_delivery_id(), notes)
    draft_manifest_path = draft_dir / 'draft-manifest.unsigned.json'
    draft_listing_path = draft_dir / 'draft-listing.txt'
    draft_scan_path = draft_dir / 'draft-source.json'
    _write_atomic(draft_manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
    _write_atomic(draft_listing_path, listing_text(manifest, signed=False))
    _write_atomic(draft_scan_path, json.dumps(
        {'source': str(source.resolve()), 'notes': notes}, ensure_ascii=False, indent=2))
    return {
        'manifest': manifest,
        'draft_manifest': draft_manifest_path,
        'draft_listing': draft_listing_path,
        'draft_source': draft_scan_path,
    }


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + '.tmp')
    with open(temporary, 'w', encoding='utf-8') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _load_draft(draft_dir: Path) -> tuple[dict, dict]:
    draft_dir = Path(draft_dir)
    manifest_path = draft_dir / 'draft-manifest.unsigned.json'
    source_path = draft_dir / 'draft-source.json'
    if not manifest_path.is_file() or not source_path.is_file():
        raise SealError('草稿不完整，请重新执行 prepare', 'bad_draft')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    draft_source = json.loads(source_path.read_text(encoding='utf-8'))
    return manifest, draft_source


def _assert_source_unchanged(draft_manifest: dict, source: Path, notes: list[dict]) -> None:
    """签封前复扫来源，与用户确认过的草稿逐项比对，防止确认后被改动。"""
    fresh_scan = scan_input(source)
    fresh_manifest = build_manifest(fresh_scan, draft_manifest['delivery_name'],
                                    draft_manifest['delivery_id'], notes)
    old_files = {item['path']: item for item in draft_manifest['files']}
    new_files = {item['path']: item for item in fresh_manifest['files']}
    if set(old_files) != set(new_files):
        raise SealError('确认后交付文件集合发生变化，请重新 prepare 并再次确认', 'source_changed')
    for path, old in old_files.items():
        if old['sha256'] != new_files[path]['sha256'] or old['bytes'] != new_files[path]['bytes']:
            raise SealError(f'确认后文件内容发生变化：{path}，请重新 prepare 并再次确认',
                            'source_changed')
    if draft_manifest['page_order'] != fresh_manifest['page_order']:
        raise SealError('确认后页序发生变化，请重新 prepare 并再次确认', 'source_changed')


def seal_draft(draft_dir: Path, keystore: KeyStore, signer_fingerprint: str,
               output_path: Path, note: str | None = None) -> dict:
    """第二步：复核来源未变 → 签名 → 生成只读交付件 → 追加登记。"""
    keystore.ensure()
    draft_manifest, draft_source = _load_draft(draft_dir)
    source = Path(draft_source['source'])
    if not source.exists():
        raise SealError('签封来源已不存在：' + str(source), 'source_missing')
    notes = draft_source.get('notes', [])
    for item in notes:
        note_file = Path(item['source_path'])
        if not note_file.is_file() or file_sha256(note_file) != item['sha256']:
            raise SealError('附加说明在确认后发生变化：' + item['path'], 'source_changed')
    _assert_source_unchanged(draft_manifest, source, notes)

    signer = keystore.signer_record(signer_fingerprint)
    private_pem = keystore.signer_private_pem(signer_fingerprint)
    public_bundle = keystore.signer_public_bundle(signer_fingerprint)
    if fingerprint_hex(public_bundle['public_key_pem'].encode('ascii')) != signer_fingerprint:
        raise SealError('公钥与私钥不匹配', 'key_mismatch')

    unsigned = dict(draft_manifest)
    unsigned.pop('seal', None)
    # 签名基准 = 最终清单结构，但两个签名字段固定为空字符串。
    # 清单自哈希无法覆盖自身（先有鸡/蛋问题），因此记录“留空基准字节”的哈希；
    # 核验时把这两个字段还原为空即可复算出完全相同的被签字节。
    manifest = dict(unsigned)
    manifest['seal'] = {
        'algorithm': SIGNATURE_ALGORITHM,
        'sealed_at': now_iso(),
        'manifest_sha256': '',
        'signature_hex': '',
        'signer': public_bundle,
    }
    signed_blob = canonical_bytes(manifest)
    signature = sign_bytes(private_pem, signed_blob)
    manifest['seal']['manifest_sha256'] = hash_bytes(signed_blob)
    manifest['seal']['signature_hex'] = signature.hex()
    manifest_bytes = canonical_bytes(manifest)
    if signed_blob_from_manifest(manifest) != signed_blob:
        raise SealError('清单被签字节无法在核验端复算，签封中止', 'canonical_instability')

    output_path = Path(output_path)
    if output_path.exists():
        raise SealError('交付件已存在，拒绝覆盖旧版本：' + str(output_path), 'output_exists')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload_root = source if source.is_dir() else None
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for item in manifest['files']:
            relative = safe_member(item['path'])
            if relative.startswith(NOTES_STORE_PREFIX):
                source_note = next(n for n in notes if n['path'] == relative)
                with open(source_note['source_path'], 'rb') as handle:
                    archive.writestr(PAYLOAD_PREFIX + relative, handle.read())
                continue
            if payload_root is not None:
                archive.write(payload_root / relative, PAYLOAD_PREFIX + relative)
            else:
                with zipfile.ZipFile(source) as origin:
                    archive.writestr(PAYLOAD_PREFIX + relative, origin.read(relative))
        archive.writestr(MANIFEST_MEMBER, manifest_bytes)
        archive.writestr(SIGNATURE_MEMBER, signature)
        archive.writestr(PUBLIC_KEY_MEMBER, public_bundle['public_key_pem'])
        archive.writestr(VERIFY_GUIDE_MEMBER, VERIFY_GUIDE)
    # 交付件落盘后即只读：后续编辑只能另出新版本，不能改写旧件。
    os.chmod(output_path, stat.S_IRUSR | stat.S_IRGRP)

    record = SealRegistry(keystore.home).append(manifest, output_path, note)
    return {
        'bundle_path': output_path,
        'delivery_id': manifest['delivery_id'],
        'manifest_fingerprint': manifest_fingerprint(manifest),
        'signer': {'name': signer['name'], 'fingerprint': signer_fingerprint},
        'file_count': manifest['file_count'],
        'registry_record_hash': record['record_hash'],
    }


def _zip_payload_bytes(archive: zipfile.ZipFile, relative: str) -> bytes:
    return archive.read(PAYLOAD_PREFIX + safe_member(relative))


def _open_bundle(bundle_path: Path):
    bundle_path = Path(bundle_path)
    if bundle_path.is_dir():
        return 'directory', bundle_path
    if not bundle_path.is_file():
        raise SealError('交付件不存在：' + str(bundle_path), 'bundle_missing')
    try:
        archive = zipfile.ZipFile(bundle_path)
    except (OSError, zipfile.BadZipFile) as error:
        raise SealError('交付件无法打开：' + str(error), 'bad_bundle') from error
    return 'zip', archive


def read_bundle_manifest(bundle_path: Path) -> dict:
    """读取交付件清单（原始字节 + 解析结果），供 verify/diff 复用。"""
    kind, handle = _open_bundle(bundle_path)
    try:
        if kind == 'zip':
            raw = handle.read(MANIFEST_MEMBER)
        else:
            raw = (handle / MANIFEST_MEMBER).read_bytes()
    except KeyError as error:
        raise SealError('交付件缺少清单 manifest.json', 'bad_bundle') from error
    finally:
        if kind == 'zip':
            handle.close()
    manifest = load_manifest(json.loads(raw.decode('utf-8')))
    baseline = signed_blob_from_manifest(manifest)
    if hash_bytes(baseline) != manifest['seal']['manifest_sha256']:
        raise SealError('清单基准哈希与签封记录不一致（清单被改写）', 'manifest_tampered')
    return manifest


def verify_bundle(bundle_path: Path, keystore: KeyStore,
                  baseline_path: Path | None = None) -> dict:
    """只读核验：信任判定 → 签名验证 → 逐文件哈希 → 页序/文字 → 可选版本对比。

    本函数不向 bundle_path 所在交付件写入任何字节；
    OpenSSL 临时文件只出现在系统临时目录并立即清理。
    """
    kind, handle = _open_bundle(bundle_path)
    problems: list[dict] = []
    try:
        if kind == 'zip':
            names = set(handle.namelist())
            raw_manifest = handle.read(MANIFEST_MEMBER)
            raw_signature = handle.read(SIGNATURE_MEMBER)
            embedded_pem = handle.read(PUBLIC_KEY_MEMBER)
        else:
            names = {
                path.relative_to(handle).as_posix()
                for path in handle.rglob('*') if path.is_file() and not path.is_symlink()
            }
            raw_manifest = (handle / MANIFEST_MEMBER).read_bytes()
            raw_signature = (handle / SIGNATURE_MEMBER).read_bytes()
            embedded_pem = (handle / PUBLIC_KEY_MEMBER).read_bytes()
    except KeyError as error:
        raise SealError('交付件缺少 META-INF 组成：' + str(error), 'bad_bundle') from error
    finally:
        if kind == 'zip':
            handle.close()

    manifest = load_manifest(json.loads(raw_manifest.decode('utf-8')))
    seal = manifest['seal']

    # 1) 重建签封时的被签字节，并核对其哈希是否就是清单记录值。
    signed_blob = signed_blob_from_manifest(manifest)
    manifest_hash = hash_bytes(signed_blob)
    if manifest_hash != seal['manifest_sha256']:
        problems.append({'scope': 'manifest', 'code': 'manifest_hash_mismatch',
                         'message': '清单基准字节与签封记录不一致'})

    # 2) 签发者是否受信（以本地 trust/ 库为准，包内公钥仅作展示）。
    signer_info = seal['signer']
    fingerprint = signer_info['fingerprint']
    embedded_fingerprint = fingerprint_hex(embedded_pem)
    trusted_record = keystore.trusted_record(fingerprint)
    signer_trusted = bool(trusted_record)
    if embedded_fingerprint != fingerprint:
        problems.append({'scope': 'trust', 'code': 'embedded_key_fingerprint_mismatch',
                         'message': '包内公钥与清单记录的指纹不一致'})
        signer_trusted = False
    if not signer_trusted:
        problems.append({'scope': 'trust', 'code': 'untrusted_signer',
                         'message': f"签发者不在本地受信库：{signer_info.get('name')} "
                                    f"({fingerprint})"})

    # 3) 验签：受信时用受信库公钥；不受信也用包内公钥算一次，供诊断。
    verify_pem = keystore.trusted_public_pem(fingerprint) or embedded_pem
    try:
        signature_valid = verify_bytes(verify_pem, signed_blob, raw_signature)
    except SealError as error:
        signature_valid = False
        problems.append({'scope': 'signature', 'code': 'openssl_error', 'message': error.message})
    if not signature_valid:
        problems.append({'scope': 'signature', 'code': 'bad_signature',
                         'message': '清单签名验证失败'})
    if seal.get('signature_hex') and bytes.fromhex(seal['signature_hex']) != raw_signature:
        problems.append({'scope': 'signature', 'code': 'signature_record_mismatch',
                         'message': '签名文件与清单内签名记录不一致'})

    # 4) 逐文件重算哈希（与压缩容器无关，重新压缩不影响结果）。
    kind, handle = _open_bundle(bundle_path)
    verified_files = 0
    try:
        allowed = {MANIFEST_MEMBER, SIGNATURE_MEMBER, PUBLIC_KEY_MEMBER, VERIFY_GUIDE_MEMBER}
        allowed.update(PAYLOAD_PREFIX + safe_member(item['path'])
                       for item in manifest['files'])
        if kind == 'zip':
            # 目录条目（以 / 结尾）不是文件，不参与清单外成员判定。
            for unexpected in sorted(name for name in (names - allowed)
                                     if not name.endswith('/')):
                problems.append({'scope': 'payload', 'code': 'unlisted_member',
                                 'path': unexpected, 'message': '交付件包含清单之外的成员'})
        else:
            for unexpected in sorted(name for name in (names - allowed)):
                problems.append({'scope': 'payload', 'code': 'unlisted_member',
                                 'path': unexpected, 'message': '交付目录包含清单之外的文件'})
        for item in manifest['files']:
            relative = safe_member(item['path'])
            member = PAYLOAD_PREFIX + relative
            try:
                if kind == 'zip':
                    payload = handle.read(member)
                else:
                    payload = (handle / member).read_bytes()
            except (KeyError, FileNotFoundError):
                problems.append({'scope': 'payload', 'code': 'missing_file',
                                 'path': relative, 'message': '清单列出的文件缺失'})
                continue
            actual = hash_bytes(payload)
            if actual != item['sha256'] or len(payload) != item['bytes']:
                problems.append({'scope': 'payload', 'code': 'content_mismatch',
                                 'path': relative,
                                 'expected_sha256': item['sha256'],
                                 'actual_sha256': actual,
                                 'message': '文件内容与签封清单不一致（图像或文字被替换/修改）'})
            else:
                verified_files += 1
    finally:
        if kind == 'zip':
            handle.close()

    # 5) 页序与页面记录的内部一致性。
    page_ids = [page['page_id'] for page in manifest['pages']]
    if page_ids != manifest['page_order']:
        problems.append({'scope': 'order', 'code': 'order_inconsistent',
                         'message': '页面表顺序与 page_order 不一致'})
    file_index = {item['path']: item for item in manifest['files']}
    for page in manifest['pages']:
        entry = file_index.get(page['image_path'])
        if not entry or entry['sha256'] != page['image_sha256']:
            problems.append({'scope': 'pages', 'code': 'page_image_hash_inconsistent',
                             'page_id': page['page_id'], 'message': '页面图像哈希与文件表不一致'})

    report = {
        'verified_at': now_iso(),
        'bundle': str(Path(bundle_path)),
        'delivery_id': manifest['delivery_id'],
        'delivery_name': manifest['delivery_name'],
        'signer': {
            'name': signer_info.get('name'),
            'fingerprint': fingerprint,
            'trusted': signer_trusted,
            'trusted_as': trusted_record['name'] if trusted_record else None,
        },
        'signature_valid': signature_valid,
        'manifest_fingerprint': manifest_hash,
        'file_count': manifest['file_count'],
        'verified_files': verified_files,
        'page_count': len(manifest['pages']),
        'valid': not problems,
        'problems': problems,
    }

    if baseline_path is not None:
        baseline = read_bundle_manifest(baseline_path)
        report['diff'] = diff_manifests(baseline, manifest)
    return report


def diff_bundles(before_path: Path, after_path: Path) -> dict:
    before = read_bundle_manifest(before_path)
    after = read_bundle_manifest(after_path)
    return diff_manifests(before, after)
