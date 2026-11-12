"""签封/核验命令行入口：

    python -m paperflow.seal keygen      生成本地签发密钥
    python -m paperflow.seal prepare     扫描交付来源，生成待确认清单
    python -m paperflow.seal seal        复核未变后签封，生成只读交付件
    python -m paperflow.seal verify      只读核验（先判受信，再查内容/页序/差异）
    python -m paperflow.seal diff        比较两份已签封交付件
    python -m paperflow.seal trust       管理收件方受信公钥库
    python -m paperflow.seal log         查看/复核追加式签封登记
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ''):  # 支持直接 python /path/to/__main__.py 运行
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from paperflow.sealing.bundles import (
        diff_bundles,
        listing_text,
        prepare_draft,
        read_bundle_manifest,
        seal_draft,
        verify_bundle,
    )
    from paperflow.sealing.core import SealError, openssl_available
    from paperflow.sealing.keys import KeyStore
    from paperflow.sealing.registry import SealRegistry
else:
    from .bundles import (
        diff_bundles,
        listing_text,
        prepare_draft,
        read_bundle_manifest,
        seal_draft,
        verify_bundle,
    )
    from .core import SealError, openssl_available
    from .keys import KeyStore
    from .registry import SealRegistry

EXIT_OK = 0
EXIT_TAMPERED = 1
EXIT_USAGE = 2


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def _keystore(args) -> KeyStore:
    return KeyStore(Path(args.home) if args.home else None)


def cmd_keygen(args) -> int:
    keystore = _keystore(args)
    record = keystore.create_signer(args.name)
    print('已生成 Ed25519 签发密钥：')
    print(f"  名称：{record['name']}")
    print(f"  指纹：{record['fingerprint']}")
    print(f"  位置：{keystore.keys_dir / record['fingerprint']}")
    public_path = Path(args.export_public) if args.export_public else \
        Path.cwd() / (record['fingerprint'][:12] + '-public.pem')
    keystore.export_public_key(record['fingerprint'], public_path)
    print(f"  公钥已导出：{public_path}（请通过独立可信渠道交给收件方）")
    return EXIT_OK


def cmd_signers(args) -> int:
    records = _keystore(args).list_signers()
    if not records:
        print('（尚无本地签发密钥，先用 keygen 生成）')
        return EXIT_OK
    for record in records:
        print(f"{record['fingerprint']}  {record['name']}  创建于 {record['created_at']}")
    return EXIT_OK


def cmd_trust_add(args) -> int:
    record = _keystore(args).add_trusted(Path(args.public_key), args.name)
    print('已加入受信公钥库：')
    print(f"  名称：{record['name']}")
    print(f"  指纹：{record['fingerprint']}")
    return EXIT_OK


def cmd_trust_remove(args) -> int:
    removed = _keystore(args).remove_trusted(args.fingerprint)
    print('已移除' if removed else '受信库中无此指纹：', args.fingerprint)
    return EXIT_OK


def cmd_trust_list(args) -> int:
    records = _keystore(args).list_trusted()
    if not records:
        print('（受信库为空，verify 时任何签发者都将判为不受信）')
        return EXIT_OK
    for record in records:
        print(f"{record['fingerprint']}  {record['name']}  加入于 {record['added_at']}")
    return EXIT_OK


def cmd_prepare(args) -> int:
    result = prepare_draft(Path(args.source), Path(args.draft_dir),
                           args.name, [Path(item) for item in args.note or []])
    manifest = result['manifest']
    print(listing_text(manifest, signed=False))
    print(f"待确认清单：{result['draft_manifest']}")
    print(f"内容确认单：{result['draft_listing']}")
    print('请核对页面、来源与说明；确认无误后执行 seal（签封前会自动复扫，确认后改动将被拒绝）。')
    return EXIT_OK


def cmd_seal(args) -> int:
    keystore = _keystore(args)
    signers = keystore.list_signers()
    if args.signer:
        fingerprint = args.signer
    elif len(signers) == 1:
        fingerprint = signers[0]['fingerprint']
    else:
        raise SealError('存在多个签发密钥，请用 --signer 指定指纹（可用 signers 查看）',
                        'signer_required')
    output = Path(args.output)
    result = seal_draft(Path(args.draft_dir), keystore, fingerprint, output, args.note)
    print('签封完成，交付件已设为只读：')
    print(f"  交付件：{result['bundle_path']}")
    print(f"  交付编号：{result['delivery_id']}")
    print(f"  签发者：{result['signer']['name']}（{result['signer']['fingerprint']}）")
    print(f"  清单指纹：{result['manifest_fingerprint']}")
    print(f"  文件数：{result['file_count']}")
    print(f"  登记表链记录：{result['registry_record_hash']}")
    return EXIT_OK


def _format_diff(diff: dict) -> str:
    lines = []
    summary = diff['summary']
    if not summary['has_changes']:
        lines.append('与基线版本内容完全一致（重新压缩/改名不会产生差异）。')
        return '\n'.join(lines)
    if summary['order_changed']:
        lines.append('页序变化：')
        for item in diff['order']['moved']:
            lines.append(f"  · 第 {item['before_seq']} 页 → 第 {item['after_seq']} 页："
                         f"{item['name']}（{item['page_id']}）")
    change_labels = {
        'image_replaced': '图像被替换',
        'text_modified': '人工校订文字被修改',
        'image_and_text_changed': '图像与文字均被修改',
        'page_added': '页面新增',
        'page_removed': '页面缺失',
    }
    if diff['pages']:
        lines.append('页面变化：')
        for item in diff['pages']:
            lines.append(f"  · {change_labels.get(item['change'], item['change'])}："
                         f"{item['name']}（{item['page_id']}）")
            if item['before'] and item['after']:
                if item['before']['image_sha256'] != item['after']['image_sha256']:
                    lines.append(f"      图像哈希 {item['before']['image_sha256'][:16]}… "
                                 f"→ {item['after']['image_sha256'][:16]}…")
                if item['before'].get('text_sha256') != item['after'].get('text_sha256'):
                    lines.append(f"      文字哈希 {str(item['before'].get('text_sha256'))[:16]} "
                                 f"→ {str(item['after'].get('text_sha256'))[:16]}")
    if diff['notes']:
        lines.append('说明文件变化：')
        for item in diff['notes']:
            lines.append(f"  · {item['change']}：{item['name']}")
    if diff['files']:
        lines.append('其他文件变化：')
        for item in diff['files']:
            lines.append(f"  · {item['change']}：{item['name']}")
    return '\n'.join(lines)


def cmd_verify(args) -> int:
    keystore = _keystore(args)
    report = verify_bundle(Path(args.bundle), keystore,
                           Path(args.baseline) if args.baseline else None)
    print(f"交付件：{report['bundle']}")
    print(f"交付编号：{report['delivery_id']}  名称：{report['delivery_name']}")
    signer = report['signer']
    trust_state = '受信' if signer['trusted'] else '不受信'
    print(f"签发者：{signer['name']}  指纹：{signer['fingerprint']}  信任状态：{trust_state}")
    print(f"签名验证：{'通过' if report['signature_valid'] else '失败'}")
    print(f"文件核验：{report['verified_files']}/{report['file_count']} 项与清单一致，"
          f"页面数：{report['page_count']}")
    if report['problems']:
        print('发现问题：')
        for problem in report['problems']:
            extra = f"（{problem.get('path') or problem.get('page_id') or ''}）" \
                if problem.get('path') or problem.get('page_id') else ''
            print(f"  · [{problem['scope']}/{problem['code']}] {problem['message']}{extra}")
    else:
        print('核验通过：交付件与签封版本完全一致。')
    if 'diff' in report:
        print('')
        print(_format_diff(report['diff']))
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding='utf-8')
        print(f'\n核验报告已另存（未写入交付件）：{report_path}')
    return EXIT_OK if report['valid'] else EXIT_TAMPERED


def cmd_diff(args) -> int:
    diff = diff_bundles(Path(args.before), Path(args.after))
    print(_format_diff(diff))
    if args.json:
        _print_json(diff)
    return EXIT_OK if not diff['summary']['has_changes'] else EXIT_TAMPERED


def cmd_manifest(args) -> int:
    manifest = read_bundle_manifest(Path(args.bundle))
    _print_json(manifest)
    return EXIT_OK


def cmd_log(args) -> int:
    registry = SealRegistry(Path(args.home) if args.home else None)
    if args.verify_chain:
        result = registry.verify_chain()
        if result['valid']:
            print(f"登记表哈希链完整，共 {result['records']} 条签封记录。")
            return EXIT_OK
        print(f"登记表在第 {result['broken_at']} 条断链：{', '.join(result['problems'])}")
        return EXIT_TAMPERED
    entries = registry.entries()
    if not entries:
        print('（尚无签封记录）')
        return EXIT_OK
    for index, entry in enumerate(entries, start=1):
        print(f"{index}. {entry['sealed_at']}  {entry['delivery_name']}  "
              f"{entry['file_count']} 个文件")
        print(f"   交付编号：{entry['delivery_id']}")
        print(f"   签发者：{entry['signer_name']}（{entry['signer_fingerprint']}）")
        print(f"   清单指纹：{entry['manifest_fingerprint']}")
        print(f"   交付件：{entry['bundle_path']}")
        print(f"   记录哈希：{entry['record_hash']}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='python -m paperflow.seal',
        description='PaperFlow 本地签封与离线收件核验工具（Ed25519 + SHA-256）')
    parser.add_argument('--home', help='密钥库/受信库/登记表目录（默认 ~/.paperflow-seal，'
                                       '也可用环境变量 PAPERFLOW_SEAL_HOME）')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('keygen', help='生成本地 Ed25519 签发密钥')
    p.add_argument('--name', required=True, help='签发者/单位名称')
    p.add_argument('--export-public', help='公钥导出路径')
    p.set_defaults(func=cmd_keygen)

    p = sub.add_parser('signers', help='列出现有本地签发密钥')
    p.set_defaults(func=cmd_signers)

    p = sub.add_parser('prepare', help='扫描交付来源，生成待确认清单')
    p.add_argument('--source', required=True, help='PaperFlow 导出 ZIP 或交付目录')
    p.add_argument('--draft-dir', required=True, help='草稿输出目录（新空目录）')
    p.add_argument('--name', help='交付名称（默认取来源文档名）')
    p.add_argument('--note', action='append', help='附加说明文件，可重复传入')
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser('seal', help='复核来源未变后签封，生成只读交付件')
    p.add_argument('--draft-dir', required=True)
    p.add_argument('--signer', help='签发密钥指纹（仅一个密钥时可省略）')
    p.add_argument('--output', required=True, help='输出 .sealed.zip 路径，已存在则拒绝')
    p.add_argument('--note', help='写入本地登记表的备注（不进入交付件）')
    p.set_defaults(func=cmd_seal)

    p = sub.add_parser('verify', help='只读核验交付件；退出码 0=通过，1=不可信/被改动')
    p.add_argument('--bundle', required=True, help='待核验的 .sealed.zip 或已解包目录')
    p.add_argument('--baseline', help='上一版交付件，用于逐项比对变化')
    p.add_argument('--report', help='核验报告另存路径（绝不写入交付件）')
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser('diff', help='比较两份已签封交付件的差异')
    p.add_argument('--before', required=True)
    p.add_argument('--after', required=True)
    p.add_argument('--json', action='store_true', help='同时输出机器可读 JSON')
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser('manifest', help='查看交付件内的签封清单')
    p.add_argument('--bundle', required=True)
    p.set_defaults(func=cmd_manifest)

    trust = sub.add_parser('trust', help='管理本地受信公钥库')
    trust_sub = trust.add_subparsers(dest='trust_command', required=True)
    p = trust_sub.add_parser('add', help='加入受信签发者公钥')
    p.add_argument('--public-key', required=True)
    p.add_argument('--name', required=True)
    p.set_defaults(func=cmd_trust_add)
    p = trust_sub.add_parser('remove', help='移除受信公钥')
    p.add_argument('fingerprint')
    p.set_defaults(func=cmd_trust_remove)
    p = trust_sub.add_parser('list', help='列出受信公钥')
    p.set_defaults(func=cmd_trust_list)

    p = sub.add_parser('log', help='查看/复核追加式签封登记表')
    p.add_argument('--verify-chain', action='store_true', help='复核哈希链完整性')
    p.set_defaults(func=cmd_log)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not openssl_available():
        print('错误：需要 OpenSSL 3.0 及以上版本支持 Ed25519，请先安装或检查 PATH。',
              file=sys.stderr)
        return EXIT_USAGE
    try:
        return args.func(args)
    except SealError as error:
        print(f'错误：{error.message}', file=sys.stderr)
        return EXIT_USAGE


if __name__ == '__main__':
    raise SystemExit(main())
