"""本地签封与收件核验（离线 Ed25519，依赖系统 openssl 命令）。"""

from .core import (
    SealError,
    canonical_bytes,
    file_sha256,
    hash_bytes,
    now_iso,
    openssl_available,
    sha256_hex,
)
from .keys import (
    KeyStore,
    fingerprint_hex,
    generate_key,
    sign_bytes,
    verify_bytes,
)
from .manifest import (
    build_manifest,
    diff_manifests,
    load_manifest,
    scan_input,
)
from .bundles import (
    prepare_draft,
    seal_draft,
    verify_bundle,
)
from .registry import SealRegistry

__all__ = [
    'SealError',
    'canonical_bytes',
    'file_sha256',
    'hash_bytes',
    'now_iso',
    'openssl_available',
    'sha256_hex',
    'KeyStore',
    'fingerprint_hex',
    'generate_key',
    'sign_bytes',
    'verify_bytes',
    'build_manifest',
    'diff_manifests',
    'load_manifest',
    'scan_input',
    'prepare_draft',
    'seal_draft',
    'verify_bundle',
    'SealRegistry',
]
