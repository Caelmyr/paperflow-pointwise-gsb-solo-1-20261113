from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(command):
    print('执行：', ' '.join(map(str, command)), flush=True)
    subprocess.run(list(map(str, command)), check=True, cwd=ROOT)


def environment_python():
    return ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def locate_tesseract():
    command = shutil.which('tesseract')
    if command:
        return Path(command)
    if os.name == 'nt':
        for variable in ('ProgramFiles', 'LOCALAPPDATA', 'ProgramFiles(x86)'):
            base = os.environ.get(variable)
            if not base:
                continue
            for relative in ('Tesseract-OCR/tesseract.exe', 'Programs/Tesseract-OCR/tesseract.exe'):
                candidate = Path(base) / relative
                if candidate.is_file():
                    return candidate
    local = ROOT / '.tools/ocr/usr/bin/tesseract'
    if local.is_file():
        return local
    return None


def install_ocr():
    command = locate_tesseract()
    if command is None:
        if os.name == 'nt' and shutil.which('winget'):
            run(['winget', 'install', '--id', 'UB-Mannheim.TesseractOCR', '--exact', '--accept-package-agreements', '--accept-source-agreements'])
        elif platform.system() == 'Linux' and shutil.which('apt-get'):
            prefix = [] if os.geteuid() == 0 else ['sudo']
            run(prefix + ['apt-get', 'update'])
            run(prefix + ['apt-get', 'install', '-y', 'tesseract-ocr', 'tesseract-ocr-chi-sim', 'tesseract-ocr-eng', 'fonts-noto-cjk', 'fontconfig'])
        elif platform.system() == 'Darwin' and shutil.which('brew'):
            run(['brew', 'install', 'tesseract', 'tesseract-lang'])
        else:
            raise RuntimeError('无法自动安装 Tesseract。请安装 Tesseract 和 chi_sim、eng 语言包，或使用 Docker Compose。')
        command = locate_tesseract()
    if command is None:
        raise RuntimeError('安装后仍未找到 Tesseract，请重新打开终端运行。')
    sys.path.insert(0, str(ROOT))
    from paperflow.ocr import configure, status
    if os.name == 'nt':
        os.environ['PATH'] = str(command.parent) + os.pathsep + os.environ.get('PATH', '')
    configure()
    health = status()
    if not health['available'] and os.name == 'nt':
        local = ROOT / '.tools' / 'Tesseract-OCR'
        local.mkdir(parents=True, exist_ok=True)
        language_directory = local / 'tessdata'
        language_directory.mkdir(exist_ok=True)
        for language in ('chi_sim', 'eng', 'osd'):
            target = language_directory / (language + '.traineddata')
            existing = command.parent / 'tessdata' / target.name
            if not target.is_file():
                if existing.is_file():
                    shutil.copyfile(existing, target)
                else:
                    url = 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/4.1.0/' + target.name
                    print('下载离线语言包：', language, flush=True)
                    temporary = target.with_suffix('.tmp')
                    urllib.request.urlretrieve(url, temporary)
                    temporary.replace(target)
        os.environ['TESSDATA_PREFIX'] = str(language_directory)
        health = status()
    if not health['available']:
        raise RuntimeError(health['message'])
    print(health['message'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inside', action='store_true')
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        raise RuntimeError('需要 Python 3.10 或更高版本')
    interpreter = environment_python()
    if not args.inside:
        if not interpreter.is_file():
            venv.EnvBuilder(with_pip=True).create(ROOT / '.venv')
        run([interpreter, '-m', 'pip', 'install', '-r', ROOT / 'requirements.lock.txt'])
        run([interpreter, Path(__file__).resolve(), '--inside'])
        return
    install_ocr()
    from paperflow.demo import build_demo
    build_demo()
    print('PaperFlow 环境准备完成。')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('环境准备失败：', error, file=sys.stderr)
        raise SystemExit(1)
