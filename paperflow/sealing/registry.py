"""追加式签封登记表：每条记录链接前一条的哈希，任何改写都会断链。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .core import SealError, canonical_bytes, hash_bytes, now_iso
from .keys import KeyStore, manifest_fingerprint

REGISTRY_NAME = 'seal-registry.jsonl'


class SealRegistry:
    """只追加（append-only）的本地签封记录。

    每条记录包含 ``prev_hash`` 与自身 ``record_hash``：
    第一条的 prev_hash 为全 0；任何一行被删除、修改或重排，
    verify_chain 都能定位到第一条断链位置。
    """

    def __init__(self, home: Path | None = None):
        self.keystore = KeyStore(home)
        self.path = self.keystore.home / REGISTRY_NAME
        self.genesis = '0' * 64

    def _append_line(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 'a' 模式只追加；写完立即 fsync，避免崩溃时记录残缺。
        with open(self.path, 'a', encoding='utf-8') as handle:
            handle.write(line + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(self.path, 0o640)
        except OSError:
            pass

    def _read_lines(self) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        with open(self.path, encoding='utf-8') as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    raise SealError(f'登记表第 {line_number} 行为空', 'registry_corrupt')
                try:
                    records.append(json.loads(line))
                except ValueError as error:
                    raise SealError(f'登记表第 {line_number} 行不是有效 JSON',
                                    'registry_corrupt') from error
        return records

    def latest_hash(self) -> str:
        records = self._read_lines()
        return records[-1]['record_hash'] if records else self.genesis

    def append(self, manifest: dict, bundle_path: Path, note: str | None = None) -> dict:
        record = {
            'record_type': 'sealed_delivery',
            'sealed_at': now_iso(),
            'delivery_id': manifest['delivery_id'],
            'delivery_name': manifest['delivery_name'],
            'manifest_fingerprint': manifest_fingerprint(manifest),
            'manifest_sha256_hex': manifest['seal']['manifest_sha256'],
            'signature_hex': manifest['seal']['signature_hex'],
            'signer_fingerprint': manifest['seal']['signer']['fingerprint'],
            'signer_name': manifest['seal']['signer']['name'],
            'file_count': manifest['file_count'],
            'total_bytes': manifest['total_bytes'],
            'page_count': len(manifest['pages']),
            'bundle_path': str(Path(bundle_path)),
            'note': (note or '')[:500],
            'prev_hash': self.latest_hash(),
        }
        record['record_hash'] = hash_bytes(canonical_bytes(record))
        self._append_line(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False))
        return record

    def verify_chain(self) -> dict:
        """复核整条哈希链；只读，不修改登记表。"""
        records = self._read_lines()
        previous = self.genesis
        for index, record in enumerate(records):
            problems = []
            stored = record.pop('record_hash', None)
            expected = hash_bytes(canonical_bytes(record))
            record['record_hash'] = stored
            if stored != expected:
                problems.append('record_hash_mismatch')
            if record.get('prev_hash') != previous:
                problems.append('prev_hash_break')
            if problems:
                return {
                    'valid': False,
                    'records': len(records),
                    'broken_at': index + 1,
                    'problems': problems,
                }
            previous = stored
        return {'valid': True, 'records': len(records), 'broken_at': None, 'problems': []}

    def entries(self) -> list[dict]:
        return self._read_lines()
