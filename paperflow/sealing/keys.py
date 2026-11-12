"""Ed25519 密钥管理与签名/验签（通过系统 openssl，纯离线）。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .core import (
    SIGNATURE_ALGORITHM,
    SealError,
    hash_bytes,
    now_iso,
    run_openssl,
)

DEFAULT_HOME = Path.home() / '.paperflow-seal'
HOME_ENV = 'PAPERFLOW_SEAL_HOME'


def fingerprint_hex(public_pem: bytes) -> str:
    """公钥指纹 = SHA-256(SPKI DER)，与 PEM 文本换行风格无关。"""
    der = run_openssl(['pkey', '-pubin', '-outform', 'DER'], input_bytes=public_pem)
    return hash_bytes(der)


def generate_key() -> tuple[bytes, bytes]:
    """生成 Ed25519 密钥对，返回 (私钥 PEM, 公钥 PEM)。"""
    private_pem = run_openssl(['genpkey', '-algorithm', 'ED25519'])
    public_pem = run_openssl(['pkey', '-pubout'], input_bytes=private_pem)
    return private_pem, public_pem


def assert_ed25519_public(public_pem: bytes) -> None:
    text = run_openssl(['pkey', '-pubin', '-text', '-noout'], input_bytes=public_pem)
    decoded = text.decode('utf-8', 'replace').upper().replace(' ', '')
    if 'ED25519' not in decoded:
        raise SealError('仅支持 Ed25519 公钥', 'unsupported_key')


def sign_bytes(private_pem: bytes, message: bytes) -> bytes:
    """Ed25519 分离签名（rawin，对消息整体签名）。"""
    with tempfile.TemporaryDirectory(prefix='paperflow-sign-') as temporary:
        directory = Path(temporary)
        key_path = directory / 'private.pem'
        message_path = directory / 'message.bin'
        signature_path = directory / 'signature.bin'
        key_path.write_bytes(private_pem)
        message_path.write_bytes(message)
        run_openssl([
            'pkeyutl', '-sign',
            '-inkey', str(key_path),
            '-rawin',
            '-in', str(message_path),
            '-out', str(signature_path),
        ])
        return signature_path.read_bytes()


def verify_bytes(public_pem: bytes, message: bytes, signature: bytes) -> bool:
    """验证分离签名；签名不匹配返回 False，工具本身故障才抛 SealError。"""
    with tempfile.TemporaryDirectory(prefix='paperflow-verify-') as temporary:
        directory = Path(temporary)
        key_path = directory / 'public.pem'
        message_path = directory / 'message.bin'
        signature_path = directory / 'signature.bin'
        key_path.write_bytes(public_pem)
        message_path.write_bytes(message)
        signature_path.write_bytes(signature)
        completed = run_openssl([
            'pkeyutl', '-verify',
            '-pubin', '-inkey', str(key_path),
            '-rawin',
            '-in', str(message_path),
            '-sigfile', str(signature_path),
        ])
        result = completed.decode('utf-8', 'replace').lower()
        if 'successfully' in result:
            return True
        if 'failure' in result:
            return False
        raise SealError('openssl 验签输出无法识别：' + result[:200], 'openssl_failed')


class KeyStore:
    """本机密钥与受信公钥库。

    目录结构::

        home/
          keys/<指纹>/private.pem(0600) public.pem meta.json
          trust/<指纹>.pem
          trust-index.json
          registry.jsonl        # 追加式签封记录（见 registry.py）
    """

    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else Path(os.environ.get(HOME_ENV, DEFAULT_HOME))
        self.keys_dir = self.home / 'keys'
        self.trust_dir = self.home / 'trust'
        self.trust_index = self.home / 'trust-index.json'

    def ensure(self) -> 'KeyStore':
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        self.trust_dir.mkdir(parents=True, exist_ok=True)
        if not self.trust_index.exists():
            self._write_json(self.trust_index, {})
        return self

    @staticmethod
    def _write_json(path: Path, value) -> None:
        temporary = path.with_name(path.name + '.tmp')
        with open(temporary, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temporary, path)

    def _read_json(self, path: Path):
        if not path.exists():
            return {}
        try:
            with open(path, encoding='utf-8') as handle:
                return json.load(handle)
        except (OSError, ValueError) as error:
            raise SealError('密钥库记录无法读取：' + str(error), 'keystore_corrupt') from error

    # -- 签发者 ----------------------------------------------------------

    def create_signer(self, name: str) -> dict:
        self.ensure()
        name = (name or '').strip()
        if not name or len(name) > 120:
            raise SealError('签发者名称必填且不超过 120 字')
        private_pem, public_pem = generate_key()
        fingerprint = fingerprint_hex(public_pem)
        target = self.keys_dir / fingerprint
        if target.exists():
            raise SealError('该密钥已存在：' + fingerprint, 'key_exists')
        target.mkdir(parents=True)
        private_path = target / 'private.pem'
        public_path = target / 'public.pem'
        private_path.write_bytes(private_pem)
        os.chmod(private_path, 0o600)
        public_path.write_bytes(public_pem)
        record = {
            'fingerprint': fingerprint,
            'name': name,
            'algorithm': SIGNATURE_ALGORITHM,
            'created_at': now_iso(),
        }
        self._write_json(target / 'meta.json', record)
        return record

    def list_signers(self) -> list[dict]:
        self.ensure()
        records = []
        for child in sorted(self.keys_dir.iterdir()):
            meta_path = child / 'meta.json'
            if child.is_dir() and meta_path.is_file():
                records.append(self._read_json(meta_path))
        return records

    def signer_private_pem(self, fingerprint: str) -> bytes:
        path = self.keys_dir / fingerprint / 'private.pem'
        if not path.is_file():
            raise SealError('本地签发密钥不存在：' + fingerprint, 'unknown_signer')
        return path.read_bytes()

    def signer_record(self, fingerprint: str) -> dict:
        path = self.keys_dir / fingerprint / 'meta.json'
        if not path.is_file():
            raise SealError('本地签发密钥不存在：' + fingerprint, 'unknown_signer')
        return self._read_json(path)

    # -- 受信公钥 --------------------------------------------------------

    def add_trusted(self, public_pem_path: Path, name: str) -> dict:
        self.ensure()
        name = (name or '').strip()
        if not name or len(name) > 120:
            raise SealError('受信方名称必填且不超过 120 字')
        public_pem = Path(public_pem_path).read_bytes()
        assert_ed25519_public(public_pem)
        fingerprint = fingerprint_hex(public_pem)
        index = self._read_json(self.trust_index)
        record = {
            'fingerprint': fingerprint,
            'name': name,
            'algorithm': SIGNATURE_ALGORITHM,
            'added_at': now_iso(),
        }
        (self.trust_dir / (fingerprint + '.pem')).write_bytes(public_pem)
        index[fingerprint] = record
        self._write_json(self.trust_index, index)
        return record

    def remove_trusted(self, fingerprint: str) -> bool:
        self.ensure()
        index = self._read_json(self.trust_index)
        if fingerprint not in index:
            return False
        (self.trust_dir / (fingerprint + '.pem')).unlink(missing_ok=True)
        index.pop(fingerprint)
        self._write_json(self.trust_index, index)
        return True

    def list_trusted(self) -> list[dict]:
        self.ensure()
        return [self._read_json(self.trust_index)[key]
                for key in sorted(self._read_json(self.trust_index))]

    def trusted_record(self, fingerprint: str) -> dict | None:
        index = self._read_json(self.trust_index)
        record = index.get(fingerprint)
        if record and (self.trust_dir / (fingerprint + '.pem')).is_file():
            return record
        return None

    def trusted_public_pem(self, fingerprint: str) -> bytes | None:
        path = self.trust_dir / (fingerprint + '.pem')
        return path.read_bytes() if path.is_file() else None

    # -- 供打包时携带的公钥信息 ------------------------------------------

    def signer_public_bundle(self, fingerprint: str) -> dict:
        record = self.signer_record(fingerprint)
        public_pem = (self.keys_dir / fingerprint / 'public.pem').read_bytes()
        return {
            'fingerprint': fingerprint,
            'name': record['name'],
            'algorithm': SIGNATURE_ALGORITHM,
            'public_key_pem': public_pem.decode('ascii'),
        }

    def export_public_key(self, fingerprint: str, destination: Path) -> Path:
        pem = (self.keys_dir / fingerprint / 'public.pem').read_bytes()
        destination = Path(destination)
        destination.write_bytes(pem)
        return destination


def manifest_fingerprint(manifest: dict) -> str:
    """清单指纹 = 被签字节（签名字段留空的规范化清单）的 SHA-256。"""
    from .bundles import signed_blob_from_manifest
    return hash_bytes(signed_blob_from_manifest(manifest))
