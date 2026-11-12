import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
storage = root / 'data'
storage.mkdir(parents=True, exist_ok=True)
if os.getuid() == 0:
    target_uid = int(os.environ.get('PAPERFLOW_UID', '10001'))
    target_gid = int(os.environ.get('PAPERFLOW_GID', '10001'))
    for path in [storage, *storage.rglob('*')]:
        if path.is_symlink():
            continue
        info = path.stat()
        if info.st_uid != target_uid or info.st_gid != target_gid:
            os.chown(path, target_uid, target_gid)
    os.setgroups([])
    os.setgid(target_gid)
    os.setuid(target_uid)
os.execvp(sys.argv[1], sys.argv[1:])
