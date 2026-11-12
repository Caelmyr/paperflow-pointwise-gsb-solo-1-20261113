"""签封模块的基础工具：哈希、规范化 JSON、时间与 OpenSSL 调用。"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_FORMAT = 'paperflow-sealed-delivery'
MANIFEST_SCHEMA = 1
SIGNATURE_ALGORITHM = 'Ed25519'
OPENSSL_MIN_VERSION = '3.0.0'


class SealError(Exception):
    """签封/核验过程中的可预期错误。"""

    def __init__(self, message, code='seal_error'):
        super().__init__(message)
        self.message = message
        self.code = code


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_hex(text: str) -> str:
    return hash_bytes(text.encode('utf-8'))


def file_sha256(path: Path) -> str:
    """按 1MB 分块计算文件 SHA-256，避免把大文件整体读入内存。"""
    hasher = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def canonical_bytes(value) -> bytes:
    """确定性 JSON 序列化：键排序、无空白、UTF-8、禁止非有限浮点。

    同一份清单无论在谁的机器上、无论压缩软件如何重排条目，
    规范化字节都一致，因此签名可以离线复算。
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    ).encode('utf-8')


def openssl_path() -> str:
    executable = shutil.which('openssl')
    if not executable:
        raise SealError('未找到 openssl 命令，无法进行 Ed25519 签名/验签', 'openssl_missing')
    return executable


def openssl_available() -> bool:
    try:
        executable = openssl_path()
    except SealError:
        return False
    completed = subprocess.run(
        [executable, 'version'],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return False
    head = completed.stdout.strip().split()
    if len(head) < 2:
        return False
    try:
        return tuple(int(part) for part in head[1].split('.')[:2]) >= (3, 0)
    except ValueError:
        return False


def run_openssl(arguments, input_bytes=None):
    """调用 openssl，返回 stdout；失败时抛出 SealError 且不泄露临时路径细节。"""
    executable = openssl_path()
    completed = subprocess.run(
        [executable] + arguments,
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode('utf-8', 'replace').strip() or '未知错误'
        raise SealError('openssl 执行失败：' + detail[:300], 'openssl_failed')
    return completed.stdout
