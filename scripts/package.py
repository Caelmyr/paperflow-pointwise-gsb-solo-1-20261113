from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / 'paperflow-source.zip'
EXCLUDED = {'.venv', '.tools', '__pycache__', '.pytest_cache', '.git', 'data', '.agents', '.codex', '.aws'}
with zipfile.ZipFile(OUTPUT, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(ROOT.rglob('*')):
        relative = path.relative_to(ROOT)
        if not path.is_file() or any(part in EXCLUDED for part in relative.parts):
            continue
        if path == OUTPUT or path.suffix == '.log' or path.name == '.paperflow.pid':
            continue
        archive.write(path, 'gsb_3/' + relative.as_posix())
print(OUTPUT.name, OUTPUT.stat().st_size)
