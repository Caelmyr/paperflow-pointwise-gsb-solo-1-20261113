"""签封/核验流程测试（仅标准库 + 系统 openssl，可用 python -I -m unittest 运行）。"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paperflow.sealing.bundles import (  # noqa: E402
    MANIFEST_MEMBER,
    PAYLOAD_PREFIX,
    diff_bundles,
    prepare_draft,
    read_bundle_manifest,
    seal_draft,
    verify_bundle,
)
from paperflow.sealing.core import canonical_bytes, hash_bytes  # noqa: E402
from paperflow.sealing.keys import KeyStore  # noqa: E402
from paperflow.sealing.manifest import scan_input  # noqa: E402
from paperflow.sealing.registry import SealRegistry  # noqa: E402


def make_paperflow_zip(path: Path, images: dict[str, bytes], texts: dict[str, str],
                       order: list[str] | None = None, sources: dict | None = None) -> Path:
    """构造一个符合 PaperFlow 导出格式的 ZIP 供测试。"""
    order = order or list(images)
    pages = []
    for page_id in order:
        image_path = f'processed/{page_id}.png'
        page = {
            'id': page_id,
            'name': page_id + '_页面',
            'current': image_path,
            'original': f'originals/{page_id}.png',
            'thumbnail': f'thumbs/{page_id}.jpg',
            'width': 10,
            'height': 10,
            'source_file_id': None,
            'history': [],
            'regions': [],
        }
        if page_id in texts:
            page['ocr'] = {
                'text': texts[page_id],
                'boxes': [],
                'edited': True,
                'engine': 'tesseract',
                'width': 10,
                'height': 10,
            }
        pages.append(page)
    metadata = {
        'format': 'paperflow-archive',
        'schema': 1,
        'exported_at': '2026-11-12T00:00:00+00:00',
        'document': {'id': 'doc_test', 'name': '测试档案', 'category': '扫描件'},
        'pages': pages,
        'order': order,
        'sources': {
            source_id: {key: value for key, value in source.items() if key != 'content'}
            for source_id, source in (sources or {}).items()
        },
    }
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for page_id, data in images.items():
            archive.writestr(f'processed/{page_id}.png', data)
            archive.writestr(f'originals/{page_id}.png', data)
            archive.writestr(f'thumbs/{page_id}.jpg', data[:4] or b'jpg')
        for source_id, source in (sources or {}).items():
            archive.writestr(source['path'], source['content'])
        archive.writestr('metadata.json', json.dumps(metadata, ensure_ascii=False))
        archive.writestr('recognized.txt', '识别文本汇总')
        archive.writestr('README.txt', 'PaperFlow 归档')
    return path


def seal_roundtrip(source: Path, sender_home: Path, receiver_home: Path,
                   output: Path, delivery_name='测试交付', notes=None):
    """模拟：签发方 prepare→seal，收件方导入公钥→verify 的完整链路。"""
    sender = KeyStore(sender_home).ensure()
    signer = sender.create_signer('某档案馆')
    draft_dir = output.parent / 'draft'
    draft = prepare_draft(source, draft_dir, delivery_name, notes)
    sealed = seal_draft(draft_dir, sender, signer['fingerprint'], output)

    public_path = output.parent / 'sender-public.pem'
    sender.export_public_key(signer['fingerprint'], public_path)
    receiver = KeyStore(receiver_home).ensure()
    receiver.add_trusted(public_path, '某档案馆（核验通过的渠道）')
    report = verify_bundle(output, receiver)
    return signer, draft, sealed, report, sender, receiver


class SealingWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='seal-test-')
        self.base = Path(self.temp.name)
        self.sender_home = self.base / 'sender-home'
        self.receiver_home = self.base / 'receiver-home'
        self.work = self.base / 'work'
        self.work.mkdir()
        # 交付来源放在与代码隔离的独立目录（模拟不受信数据目录）。
        self.untrusted = self.base / 'untrusted-drop'
        self.untrusted.mkdir()
        self.images = {
            'page_a': b'PNG-IMAGE-A' * 100,
            'page_b': b'PNG-IMAGE-B' * 100,
            'page_c': b'PNG-IMAGE-C' * 100,
        }
        self.texts = {
            'page_a': '第一页人工校订文字',
            'page_b': '第二页人工校订文字',
            'page_c': '第三页人工校订文字',
        }
        self.source_zip = make_paperflow_zip(
            self.untrusted / 'archive.zip', self.images, self.texts)

    def tearDown(self):
        # Windows 下只读文件会阻止清理，先恢复权限。
        for root, _dirs, files in os.walk(self.temp.name):
            for name in files:
                try:
                    os.chmod(os.path.join(root, name), stat.S_IWUSR | stat.S_IRUSR)
                except OSError:
                    pass
        self.temp.cleanup()

    def test_01_full_workflow_valid_bundle_passes(self):
        output = self.work / 'delivery_v1.sealed.zip'
        signer, draft, sealed, report, sender, receiver = seal_roundtrip(
            self.source_zip, self.sender_home, self.receiver_home, output)
        self.assertTrue(report['valid'], report['problems'])
        self.assertTrue(report['signature_valid'])
        self.assertTrue(report['signer']['trusted'])
        self.assertEqual(report['verified_files'], report['file_count'])
        self.assertEqual(report['page_count'], 3)
        self.assertEqual(report['delivery_id'], sealed['delivery_id'])
        self.assertFalse(os.access(output, os.W_OK), '交付件必须是只读文件')
        # 清单确实记录了三页图像与人工校订文字哈希。
        manifest = read_bundle_manifest(output)
        self.assertEqual(len(manifest['pages']), 3)
        self.assertTrue(all(page['text_sha256'] for page in manifest['pages']))
        self.assertTrue(all(page['ocr_edited'] for page in manifest['pages']))

    def test_02_recompression_and_rename_do_not_trigger(self):
        output = self.work / 'delivery_v1.sealed.zip'
        seal_roundtrip(self.source_zip, self.sender_home, self.receiver_home, output)
        receiver = KeyStore(self.receiver_home)

        renamed = self.work / '随便改个名字.zip'
        shutil.copy2(output, renamed)
        report_renamed = verify_bundle(renamed, receiver)
        self.assertTrue(report_renamed['valid'], report_renamed['problems'])

        # 用不同压缩级别/条目顺序重新打包，内容字节保持不变。
        repacked = self.work / 'recompressed.zip'
        with zipfile.ZipFile(output) as src, \
                zipfile.ZipFile(repacked, 'w', zipfile.ZIP_STORED) as dst:
            for info in reversed(src.infolist()):
                dst.writestr(info.filename.replace('META-INF/', 'META-INF/'),
                             src.read(info.filename))
        report_repacked = verify_bundle(repacked, receiver)
        self.assertTrue(report_repacked['valid'], report_repacked['problems'])
        # 两份交付件 diff 应为零差异。
        diff = diff_bundles(output, repacked)
        self.assertFalse(diff['summary']['has_changes'])

    def test_03_image_replacement_is_detected_and_located(self):
        output = self.work / 'delivery_v1.sealed.zip'
        seal_roundtrip(self.source_zip, self.sender_home, self.receiver_home, output)
        tampered = self.work / 'delivery_tampered.zip'
        with zipfile.ZipFile(output) as src, zipfile.ZipFile(tampered, 'w') as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == PAYLOAD_PREFIX + 'processed/page_b.png':
                    data = b'PNG-IMAGE-B' * 99 + b'X'
                dst.writestr(info.filename, data)
        report = verify_bundle(tampered, KeyStore(self.receiver_home))
        self.assertFalse(report['valid'])
        mismatch = [p for p in report['problems'] if p['code'] == 'content_mismatch']
        self.assertEqual(len(mismatch), 1)
        self.assertEqual(mismatch[0]['path'], 'processed/page_b.png')

    def test_04_page_reorder_is_detected_and_differentiated(self):
        output_v1 = self.work / 'v1.sealed.zip'
        seal_roundtrip(self.source_zip, self.sender_home, self.receiver_home, output_v1)

        reordered_zip = self.untrusted / 'archive_reordered.zip'
        new_order = ['page_a', 'page_c', 'page_b']
        make_paperflow_zip(reordered_zip, self.images, self.texts, order=new_order)
        output_v2 = self.work / 'v2.sealed.zip'
        sender = KeyStore(self.sender_home)
        signer = sender.list_signers()[0]['fingerprint']
        draft_dir = self.work / 'draft2'
        prepare_draft(reordered_zip, draft_dir, '测试交付v2')
        seal_draft(draft_dir, sender, signer, output_v2)

        receiver = KeyStore(self.receiver_home)
        report = verify_bundle(output_v2, receiver, baseline_path=output_v1)
        self.assertTrue(report['valid'])  # 新交付件本身签名与内容自洽
        diff = report['diff']
        self.assertTrue(diff['summary']['order_changed'])
        self.assertEqual(diff['order']['moved'], [
            {'page_id': 'page_c', 'name': 'page_c_页面', 'before_seq': 3, 'after_seq': 2},
            {'page_id': 'page_b', 'name': 'page_b_页面', 'before_seq': 2, 'after_seq': 3},
        ])
        # 纯调页：没有图像/文字内容差异。
        self.assertEqual(diff['summary']['pages_image_replaced'], 0)
        self.assertEqual(diff['summary']['pages_text_modified'], 0)

    def test_05_text_modification_is_distinct_from_image_change(self):
        output_v1 = self.work / 'v1.sealed.zip'
        seal_roundtrip(self.source_zip, self.sender_home, self.receiver_home, output_v1)

        changed_texts = dict(self.texts)
        changed_texts['page_b'] = '第二页人工校订文字（被改写）'
        changed_zip = self.untrusted / 'archive_text.zip'
        make_paperflow_zip(changed_zip, self.images, changed_texts)
        output_v2 = self.work / 'v2-text.sealed.zip'
        sender = KeyStore(self.sender_home)
        signer = sender.list_signers()[0]['fingerprint']
        draft_dir = self.work / 'draft3'
        prepare_draft(changed_zip, draft_dir, '测试交付v3')
        seal_draft(draft_dir, sender, signer, output_v2)

        diff = diff_bundles(output_v1, output_v2)
        self.assertEqual(diff['summary']['pages_text_modified'], 1)
        self.assertEqual(diff['summary']['pages_image_replaced'], 0)
        self.assertEqual(diff['pages'][0]['change'], 'text_modified')
        self.assertEqual(diff['pages'][0]['page_id'], 'page_b')

    def test_06_note_change_is_reported_as_note(self):
        note = self.work / '说明.txt'
        note.write_text('交接说明：共三页', encoding='utf-8')
        output_v1 = self.work / 'v1-note.sealed.zip'
        seal_roundtrip(self.source_zip, self.sender_home, self.receiver_home,
                       output_v1, notes=[note])
        note2 = self.work / '说明_v2.txt'
        note2.write_text('交接说明：共三页（修订）', encoding='utf-8')
        output_v2 = self.work / 'v2-note.sealed.zip'
        sender = KeyStore(self.sender_home)
        signer = sender.list_signers()[0]['fingerprint']
        draft_dir = self.work / 'draft4'
        prepare_draft(self.source_zip, draft_dir, '测试交付', [note2])
        seal_draft(draft_dir, sender, signer, output_v2)
        diff = diff_bundles(output_v1, output_v2)
        self.assertEqual(len(diff['notes']), 2)  # 旧说明移除 + 新说明加入
        self.assertTrue(any(item['change'] == 'removed' for item in diff['notes']))

    def test_07_untrusted_signer_is_rejected(self):
        output = self.work / 'delivery_v1.sealed.zip'
        seal_roundtrip(self.source_zip, self.sender_home, self.receiver_home, output)
        empty_trust = KeyStore(self.base / 'no-trust').ensure()
        report = verify_bundle(output, empty_trust)
        self.assertFalse(report['valid'])
        self.assertFalse(report['signer']['trusted'])
        self.assertTrue(any(p['code'] == 'untrusted_signer' for p in report['problems']))
        # 包内公钥仍然能验证签名本身，区分“不受信”与“签名损坏”。
        self.assertTrue(report['signature_valid'])

    def test_08_forged_manifest_fails_signature(self):
        output = self.work / 'delivery_v1.sealed.zip'
        seal_roundtrip(self.source_zip, self.sender_home, self.receiver_home, output)
        forged = self.work / 'forged.zip'
        with zipfile.ZipFile(output) as src, zipfile.ZipFile(forged, 'w') as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == MANIFEST_MEMBER:
                    manifest = json.loads(data.decode('utf-8'))
                    manifest['delivery_name'] = '伪造的交付名称'
                    data = canonical_bytes(manifest)
                dst.writestr(info.filename, data)
        report = verify_bundle(forged, KeyStore(self.receiver_home))
        self.assertFalse(report['valid'])
        self.assertFalse(report['signature_valid'])
        self.assertTrue(any(p['code'] in ('bad_signature', 'manifest_hash_mismatch')
                            for p in report['problems']))

    def test_09_verify_never_writes_into_bundle_and_directory_form_works(self):
        output = self.work / 'delivery_v1.sealed.zip'
        seal_roundtrip(self.source_zip, self.sender_home, self.receiver_home, output)
        extracted = self.work / 'extracted'
        with zipfile.ZipFile(output) as archive:
            archive.extractall(extracted)
        before = {path.relative_to(extracted).as_posix(): path.stat().st_mtime_ns
                  for path in extracted.rglob('*') if path.is_file()}
        report = verify_bundle(extracted, KeyStore(self.receiver_home))
        after = {path.relative_to(extracted).as_posix(): path.stat().st_mtime_ns
                 for path in extracted.rglob('*') if path.is_file()}
        self.assertTrue(report['valid'])
        self.assertEqual(before, after)  # 核验前后交付件文件时间戳零变化

    def test_10_sealed_bundle_is_readonly_and_never_overwritten(self):
        output = self.work / 'delivery_v1.sealed.zip'
        seal_roundtrip(self.source_zip, self.sender_home, self.receiver_home, output)
        self.assertFalse(os.access(output, os.W_OK))
        with self.assertRaises(PermissionError):
            with open(output, 'wb'):
                pass
        # 即便恢复权限，seal 也拒绝覆盖已存在的交付件路径。
        os.chmod(output, stat.S_IWUSR | stat.S_IRUSR)
        sender = KeyStore(self.sender_home)
        signer = sender.list_signers()[0]['fingerprint']
        with self.assertRaisesRegex(Exception, '拒绝覆盖'):
            seal_draft(self.work / 'draft', sender, signer, output)

    def test_11_registry_chain_detects_rewrite(self):
        output1 = self.work / 'd1.sealed.zip'
        seal_roundtrip(self.source_zip, self.sender_home, self.receiver_home, output1)
        second_source_zip = make_paperflow_zip(
            self.untrusted / 'archive2.zip',
            {'p1': b'IMG-1'}, {'p1': '文字一'})
        sender = KeyStore(self.sender_home)
        signer = sender.list_signers()[0]['fingerprint']
        draft_dir = self.work / 'draft5'
        prepare_draft(second_source_zip, draft_dir, '第二份交付')
        seal_draft(draft_dir, sender, signer, self.work / 'd2.sealed.zip')

        registry = SealRegistry(self.sender_home)
        self.assertTrue(registry.verify_chain()['valid'])
        self.assertEqual(len(registry.entries()), 2)

        # 偷偷改写第一条记录的交付名称：哈希链必须在第 1 条就断。
        registry_path = registry.path
        os.chmod(registry_path, stat.S_IWUSR | stat.S_IRUSR)
        lines = registry_path.read_text(encoding='utf-8').splitlines()
        first = json.loads(lines[0])
        first['delivery_name'] = '被悄悄改写的名称'
        lines[0] = json.dumps(first, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':'))
        registry_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        result = registry.verify_chain()
        self.assertFalse(result['valid'])
        self.assertEqual(result['broken_at'], 1)

    def test_12_post_confirmation_change_is_refused_at_seal(self):
        sender = KeyStore(self.sender_home).ensure()
        signer = sender.create_signer('签发方')
        draft_dir = self.work / 'draft6'
        prepare_draft(self.source_zip, draft_dir, '待确认交付')
        # 确认之后、签封之前，来源图像被替换（模拟工作区又被编辑）。
        changed = dict(self.images)
        changed['page_a'] = b'PNG-IMAGE-A' * 100 + b'CHANGED'
        make_paperflow_zip(self.source_zip, changed, self.texts)
        with self.assertRaisesRegex(Exception, '确认后'):
            seal_draft(draft_dir, sender, signer,
                       self.work / 'should_not_exist.zip')
        self.assertFalse((self.work / 'should_not_exist.zip').exists())

    def test_13_directory_delivery_mode(self):
        folder = self.untrusted / 'loose-files'
        folder.mkdir()
        (folder / '001_首页.png').write_bytes(b'page-one-bytes')
        (folder / '002_次页.png').write_bytes(b'page-two-bytes')
        (folder / '10_说明.txt').write_text('目录交付说明', encoding='utf-8')
        scan = scan_input(folder)
        self.assertEqual(scan['page_order'],
                         [scan['pages'][0]['page_id'], scan['pages'][1]['page_id']])
        output = self.work / 'dir-delivery.sealed.zip'
        _signer, _draft, _sealed, report, _s, _r = seal_roundtrip(
            folder, self.sender_home, self.receiver_home, output)
        self.assertTrue(report['valid'], report['problems'])
        self.assertEqual(report['page_count'], 2)
        # 自然排序：001 在 002 前。
        manifest = read_bundle_manifest(output)
        self.assertEqual(manifest['pages'][0]['image_path'], '001_首页.png')

    def test_14_source_files_are_listed_in_manifest(self):
        sources = {
            'src_1': {
                'path': 'sources/upload.pdf',
                'name': '原始上传.pdf',
                'content': b'PDF-BYTES',
            }
        }
        source_zip = make_paperflow_zip(
            self.untrusted / 'with-source.zip', self.images, self.texts,
            sources=sources)
        output = self.work / 'with-source.sealed.zip'
        seal_roundtrip(source_zip, self.sender_home, self.receiver_home, output)
        manifest = read_bundle_manifest(output)
        self.assertEqual(len(manifest['sources']), 1)
        self.assertEqual(manifest['sources'][0]['name'], '原始上传.pdf')
        self.assertEqual(manifest['sources'][0]['sha256'], hash_bytes(b'PDF-BYTES'))

    def test_15_unlisted_member_is_flagged_in_zip_and_directory(self):
        output = self.work / 'delivery_v1.sealed.zip'
        seal_roundtrip(self.source_zip, self.sender_home, self.receiver_home, output)
        smuggled = self.work / 'smuggled.zip'
        with zipfile.ZipFile(output) as src, zipfile.ZipFile(smuggled, 'w') as dst:
            for info in src.infolist():
                dst.writestr(info, src.read(info.filename))
            dst.writestr(PAYLOAD_PREFIX + 'secret/夹带文件.txt', '未在清单中登记'.encode('utf-8'))
        report = verify_bundle(smuggled, KeyStore(self.receiver_home))
        self.assertFalse(report['valid'])
        self.assertTrue(any(p['code'] == 'unlisted_member'
                            and p['path'].endswith('夹带文件.txt')
                            for p in report['problems']))

        extracted = self.work / 'extracted-smuggle'
        with zipfile.ZipFile(output) as archive:
            archive.extractall(extracted)
        (extracted / PAYLOAD_PREFIX / 'secret').mkdir(parents=True)
        (extracted / PAYLOAD_PREFIX / 'secret' / '夹带文件.txt').write_bytes(b'x')
        report_dir = verify_bundle(extracted, KeyStore(self.receiver_home))
        self.assertFalse(report_dir['valid'])
        self.assertTrue(any(p['code'] == 'unlisted_member' for p in report_dir['problems']))


class CliEntryPointTest(unittest.TestCase):
    """通过子进程走完整 CLI，模拟在另一台工作台上操作。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='seal-cli-')
        self.base = Path(self.temp.name)
        self.sender_home = self.base / 'sender'
        self.receiver_home = self.base / 'receiver'
        self.untrusted = self.base / 'drop'
        self.untrusted.mkdir()
        make_paperflow_zip(
            self.untrusted / 'archive.zip',
            {'p1': b'IMG1', 'p2': b'IMG2'},
            {'p1': '文字一', 'p2': '文字二'})

    def tearDown(self):
        for root, _dirs, files in os.walk(self.temp.name):
            for name in files:
                try:
                    os.chmod(os.path.join(root, name), stat.S_IWUSR | stat.S_IRUSR)
                except OSError:
                    pass
        self.temp.cleanup()

    def run_cli(self, home: Path, *arguments: str) -> subprocess.CompletedProcess:
        # 直接以脚本路径运行，-I 隔离模式下 PYTHONPATH 会被忽略；
        # 脚本位于工作区内，sys.path[0] 即 /workspace，可正常 import paperflow。
        module_main = ROOT / 'paperflow' / 'sealing' / '__main__.py'
        return subprocess.run(
            [sys.executable, '-I', str(module_main), '--home', str(home), *arguments],
            capture_output=True, text=True, cwd=str(ROOT), check=False)

    def test_cli_end_to_end(self):
        created = self.run_cli(self.sender_home, 'keygen', '--name', '签发单位')
        self.assertEqual(created.returncode, 0, created.stderr)
        listed = self.run_cli(self.sender_home, 'signers')
        self.assertEqual(listed.returncode, 0)
        fingerprint = listed.stdout.split()[0]
        self.assertEqual(len(fingerprint), 64)

        draft_dir = self.base / 'draft'
        prepared = self.run_cli(self.sender_home, 'prepare',
                                '--source', str(self.untrusted / 'archive.zip'),
                                '--draft-dir', str(draft_dir))
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertIn('交付内容确认单', prepared.stdout)

        bundle = self.base / 'out' / 'delivery.sealed.zip'
        sealed = self.run_cli(self.sender_home, 'seal',
                              '--draft-dir', str(draft_dir),
                              '--signer', fingerprint,
                              '--output', str(bundle))
        self.assertEqual(sealed.returncode, 0, sealed.stderr)

        public_key = self.base / 'pub.pem'
        shutil.copy2(self.sender_home / 'keys' / fingerprint / 'public.pem', public_key)

        # 未受信先核验：退出码 1。
        unverified = self.run_cli(self.receiver_home, 'verify', '--bundle', str(bundle))
        self.assertEqual(unverified.returncode, 1)
        self.assertIn('不受信', unverified.stdout)

        trusted = self.run_cli(self.receiver_home, 'trust', 'add',
                               '--public-key', str(public_key), '--name', '签发单位')
        self.assertEqual(trusted.returncode, 0, trusted.stderr)
        verified = self.run_cli(self.receiver_home, 'verify', '--bundle', str(bundle))
        self.assertEqual(verified.returncode, 0, verified.stdout + verified.stderr)
        self.assertIn('核验通过', verified.stdout)

        chain = self.run_cli(self.sender_home, 'log', '--verify-chain')
        self.assertEqual(chain.returncode, 0)
        self.assertIn('哈希链完整', chain.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
