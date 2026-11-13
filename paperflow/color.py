"""Colour management for imported scans, review previews and delivery exports.

The workbench keeps three clearly separated colour facts for every page:

* ``source`` - what the imported file actually said (embedded ICC profile,
  decoded colour mode, or an explicit assumption when the file carries none).
* ``working`` - the single colour space used for screen review, thumbnails,
  OCR, measurements and every image operation.  It is always sRGB, the space
  OpenCV's Lab/HSV/LUT helpers and browser previews assume.
* ``delivery`` - the space an export should aim for.  When the source carried
  a usable profile, exports convert the *edited* sRGB working result back into
  that profile and embed it (or keep the untouched native pixels byte-exact
  when nothing was edited).  Otherwise exports embed sRGB and say why.

Nothing is ever "decoded as plain RGB and re-labelled on export": every
conversion is performed with LittleCMS through Pillow's ImageCms, and any
loss (missing/broken profile, unrepresentable colour, deliberate monochrome)
is recorded in the page's colour record so it can be reviewed later.
"""

from __future__ import annotations

import hashlib
import io
import warnings
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

from .core import DomainError, now

WORKING_SPACE = 'sRGB'
WORKING_DESCRIPTION = 'sRGB (IEC 61966-2.1)'
FAMILIES = {'RGB', 'CMYK', 'LAB', 'GRAY', 'XYZ', 'UNKNOWN'}
MODE_FAMILY = {
    'RGB': 'RGB',
    'RGBA': 'RGB',
    'P': 'RGB',
    'PA': 'RGB',
    'CMYK': 'CMYK',
    'LAB': 'LAB',
    'L': 'GRAY',
    'LA': 'GRAY',
    '1': 'GRAY',
    'I': 'GRAY',
    'I;16': 'GRAY',
    'I;16B': 'GRAY',
    'I;16L': 'GRAY',
    'F': 'GRAY',
}
FAMILY_MODE = {'RGB': 'RGB', 'CMYK': 'CMYK', 'LAB': 'LAB', 'GRAY': 'L'}
FORMAT_FAMILIES = {
    'png': {'RGB', 'GRAY'},
    'jpeg': {'RGB', 'GRAY', 'CMYK'},
    'tiff': {'RGB', 'GRAY', 'CMYK', 'LAB'},
}


def _srgb_profile():
    return ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB'))


def srgb_bytes():
    return _srgb_profile().tobytes()


SRGB_PROFILE = srgb_bytes()
SRGB_SHA256 = hashlib.sha256(SRGB_PROFILE).hexdigest()


def _family(space):
    value = (space or '').strip().upper()
    if value == 'LAB':
        return 'LAB'
    return value if value in FAMILIES else 'UNKNOWN'


def _recognized(description, family):
    text = (description or '').lower()
    if family == 'RGB' and ('srgb' in text or 'iec61966' in text or 'iec 61966' in text):
        return 'srgb'
    if family == 'RGB' and 'adobe rgb' in text:
        return 'adobe_rgb'
    if family == 'CMYK' and ('swop' in text or 'u.s. web coated' in text):
        return 'swop'
    if family == 'CMYK' and ('gracol' in text or 'coated' in text):
        return 'coated'
    if family == 'LAB':
        return 'lab'
    return 'custom'


def inspect_profile(data):
    """Describe ICC bytes without ever raising for damaged profiles."""
    if not data:
        return None
    result = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    try:
        profile = ImageCms.ImageCmsProfile(io.BytesIO(data))
        description = ImageCms.getProfileDescription(profile).strip()
        space = profile.profile.xcolor_space
        family = _family(space)
        result.update({
            'valid': True,
            'description': description or '未命名 ICC 描述',
            'space': (space or '').strip(),
            'family': family,
            'recognized': _recognized(description, family),
        })
    except Exception as error:  # noqa: BLE001 - malformed ICC must degrade to a record
        result.update({
            'valid': False,
            'description': None,
            'space': None,
            'family': 'UNKNOWN',
            'recognized': 'invalid',
            'error': str(error),
        })
    return result


def _profile_object(data):
    return ImageCms.ImageCmsProfile(io.BytesIO(data))


def _transform(source, target, in_mode, out_mode, intent=1):
    """Build an ICC transform, tolerating profiles without BPC/CLUT support."""
    attempts = []
    object_source = source if hasattr(source, 'profile') else _profile_object(source)
    object_target = target if hasattr(target, 'profile') else _profile_object(target)
    attempts.append((intent, ImageCms.Flags.BLACKPOINTCOMPENSATION))
    attempts.append((intent, ImageCms.Flags.NONE))
    attempts.append((0, ImageCms.Flags.NONE))
    error = None
    for chosen_intent, flags in attempts:
        try:
            return ImageCms.buildTransform(object_source, object_target, in_mode, out_mode, renderingIntent=chosen_intent, flags=flags)
        except Exception as failure:  # noqa: BLE001
            error = failure
    raise DomainError('色彩描述无法用于转换：' + str(error))


def flatten_alpha(image):
    """Composite transparency on white so colour facts stay defined."""
    if image.mode in ('RGBA', 'LA', 'PA'):
        image = image.convert('RGBA')
        background = Image.new('RGB', image.size, 'white')
        background.paste(image, mask=image.getchannel('A'))
        return background
    if image.mode == 'P':
        return image.convert('RGB')
    if image.mode in ('I', 'I;16', 'I;16B', 'I;16L', 'F'):
        return image.convert('I').point(lambda value: value / 256).convert('L')
    if image.mode == '1':
        return image.convert('L')
    return image


def _naive_working(image):
    """Convert without ICC, only when no trustworthy description exists."""
    if image.mode in ('RGB', 'L', 'CMYK', 'LAB'):
        return image.convert('RGB')
    return flatten_alpha(image).convert('RGB')


def native_to_working(image, profile_bytes):
    """Return (RGB working image, report) for one decoded source frame."""
    warnings_list = []
    report = {
        'source_mode': image.mode,
        'source_family': MODE_FAMILY.get(image.mode, 'UNKNOWN'),
        'profile': inspect_profile(profile_bytes),
        'assumption': None,
        'fallback': None,
        'warnings': warnings_list,
    }
    profile = report['profile']
    if profile is not None and not profile.get('valid'):
        warnings_list.append('内嵌色彩描述损坏，已按文件颜色模式回退；原始描述、原件与来源文件均保留可回看。')
        report['fallback'] = 'damaged_profile'
        report['source_profile_damaged'] = True
        return _naive_working(image), report
    if profile is None:
        if image.mode in ('RGB', 'RGBA', 'P', 'PA', 'L', 'LA', '1'):
            report['assumption'] = 'sRGB'
            report['fallback'] = 'assumed_srgb'
            return flatten_alpha(image).convert('RGB'), report
        if image.mode == 'CMYK':
            warnings_list.append('CMYK 文件没有内嵌色彩描述，无法确定印刷色域；屏幕预览按无描述 CMYK 回退，原件保留，交付时不能声称保留原色域。')
            report['fallback'] = 'cmyk_without_profile'
            return _naive_working(image), report
        if image.mode == 'LAB':
            warnings_list.append('Lab 文件没有内嵌色彩描述，按 Lab D50 基准解释；原件保留。')
            report['assumption'] = 'Lab D50'
            report['fallback'] = 'lab_identity'
            return _naive_working(image), report
        warnings_list.append('文件颜色模式无法识别，按 sRGB 回退；原件保留，交付按屏幕依据。')
        report['fallback'] = 'unknown_mode'
        return _naive_working(image), report

    family = profile.get('family')
    mode_family = MODE_FAMILY.get(image.mode, 'UNKNOWN')
    if family not in ('RGB', 'CMYK', 'LAB', 'GRAY'):
        warnings_list.append('色彩描述支持的色域无法用于图像数据，已按屏幕 sRGB 回退；原件保留。')
        report['fallback'] = 'unsupported_profile'
        return _naive_working(image), report
    if family != mode_family:
        warnings_list.append('色彩描述的色域与图片颜色模式不一致，未直接套用，按文件模式回退；原件保留。')
        report['fallback'] = 'profile_mode_mismatch'
        return _naive_working(image), report

    prepared = flatten_alpha(image)
    if family == 'RGB' and prepared.mode != 'RGB':
        prepared = prepared.convert('RGB')
    elif family == 'GRAY' and prepared.mode != 'L':
        prepared = prepared.convert('L')
    elif family in ('CMYK', 'LAB') and prepared.mode != family:
        prepared = prepared.convert(family)
    if profile.get('recognized') == 'srgb' and family == 'RGB':
        report['fallback'] = 'native_srgb'
        return prepared, report
    try:
        transform = _transform(profile_bytes, SRGB_PROFILE, prepared.mode, 'RGB')
        converted = ImageCms.applyTransform(prepared, transform)
        report['fallback'] = 'icc_to_srgb'
        report['conversion'] = 'ICC 转换到 sRGB 工作空间'
        return converted.convert('RGB'), report
    except DomainError as error:
        warnings_list.append('色彩描述无法建立转换，已按文件模式回退；原件保留。' + str(error))
        report['fallback'] = 'transform_failed'
        return _naive_working(image), report


def working_displayable(image, profile_bytes):
    """Whether a file may be served to a browser as-is and still be sRGB."""
    if image.mode not in ('RGB', 'L'):
        return False
    info = inspect_profile(profile_bytes)
    if info is None:
        return True  # untagged bytes are interpreted as sRGB by the workspace rule
    if not info.get('valid'):
        return False
    return info.get('recognized') == 'srgb'


def source_monochrome(image, profile_bytes):
    info = inspect_profile(profile_bytes)
    if image.mode in ('L', 'LA', '1', 'I', 'I;16', 'I;16B', 'I;16L', 'F'):
        return True
    return bool(info and info.get('valid') and info.get('family') == 'GRAY')


def record_from_file(page, path, manager=None):
    """Build (read-only) colour facts for a page stored before colour records."""
    resolved = manager.store.resolve(path) if manager else Path(path)
    try:
        with Image.open(resolved) as opened:
            opened.load()
            profile_bytes = opened.info.get('icc_profile')
            if profile_bytes is None and hasattr(opened, 'tag_v2'):
                profile_bytes = opened.tag_v2.get(34675)
            image = flatten_alpha(ImageOps.exif_transpose(opened)).copy()
    except (OSError, UnidentifiedImageError, ValueError) as error:
        return {
            'schema': 1,
            'source': {'kind': 'unknown', 'mode': None, 'profile': None, 'assumption': None,
                       'displayable': False, 'warnings': ['无法读取颜色信息：' + str(error)], 'monochrome': False},
            'working': {'space': WORKING_SPACE, 'profile': manager.srgb() if manager else None},
            'delivery': {'mode': 'screen', 'reason': '无法读取原始颜色信息'},
            'monochrome': {'converted': False},
            'losses': [],
            'events': [],
            'created_at': now(),
        }
    working, report = native_to_working(image, profile_bytes)
    info = manager.retain(profile_bytes) if (manager and profile_bytes) else None
    record = {
        'schema': 1,
        'source': {
            'kind': 'existing',
            'mode': image.mode,
            'profile': info,
            'assumption': report.get('assumption'),
            'fallback': report.get('fallback'),
            'displayable': working_displayable(image, profile_bytes),
            'warnings': report.get('warnings', []),
            'monochrome': source_monochrome(image, profile_bytes),
        },
        'working': {'space': WORKING_SPACE, 'profile': manager.srgb() if manager else None},
        'monochrome': {'converted': False},
        'losses': [],
        'events': [],
        'created_at': now(),
    }
    if info is not None and not info.get('valid'):
        record['losses'].append({
            'kind': 'damaged_profile',
            'message': '内嵌色彩描述损坏，无法解读其色域；原始描述字节与原件保留可回看。',
            'time': now(),
            'profile': info,
        })
    elif info is None:
        record['losses'].append({
            'kind': 'missing_profile',
            'message': '原件没有色彩描述，按工作区一致规则解释为 sRGB；交付时嵌入 sRGB 描述。',
            'time': now(),
        })
    edited = page.get('original') != page.get('current')
    record['delivery'] = delivery_plan(record, edited=edited)
    return record


def build_record(source_image, manager, kind='image'):
    """Persist colour facts captured by ``imaging.SourceImage``."""
    info = manager.retain(source_image.profile_bytes) if source_image.profile_bytes else None
    report = source_image.report
    family = report.get('source_family')
    record = {
        'schema': 1,
        'source': {
            'kind': kind,
            'mode': report.get('source_mode'),
            'family': family,
            'profile': info,
            'assumption': report.get('assumption'),
            'fallback': report.get('fallback'),
            'displayable': working_displayable(source_image.native, source_image.profile_bytes),
            'monochrome': source_monochrome(source_image.native, source_image.profile_bytes),
            'warnings': list(report.get('warnings', [])),
        },
        'working': {'space': WORKING_SPACE, 'profile': manager.srgb()},
        'monochrome': {'converted': False},
        'losses': [],
        'events': [],
        'created_at': now(),
    }
    if info is not None and not info.get('valid'):
        record['losses'].append({
            'kind': 'damaged_profile',
            'message': '内嵌色彩描述损坏，无法解读其色域；已保留原始描述字节与原件，可回看核对。',
            'time': now(),
            'profile': info,
        })
    elif info is None and kind not in ('generated', 'pdf_page', 'composite'):
        record['losses'].append({
            'kind': 'missing_profile',
            'message': '原件没有色彩描述，按工作区一致规则解释为 sRGB；交付时嵌入 sRGB 描述。',
            'time': now(),
        })
    record['delivery'] = delivery_plan(record, edited=False)
    return record


def rule_based_record(manager, kind='generated', reason=None):
    """Colour record for pages generated inside the workspace (blank, collage)."""
    record = {
        'schema': 1,
        'source': {
            'kind': kind, 'mode': 'RGB', 'family': 'RGB', 'profile': manager.srgb(),
            'assumption': 'sRGB', 'fallback': 'workspace_srgb', 'displayable': True,
            'monochrome': False, 'warnings': [],
        },
        'working': {'space': WORKING_SPACE, 'profile': manager.srgb()},
        'monochrome': {'converted': False},
        'losses': [],
        'events': [],
        'created_at': now(),
    }
    record['delivery'] = delivery_plan(record, edited=False)
    if reason:
        record['delivery']['reason'] = reason
    return record


def profile_paths(record):
    """All stored profile references inside one page colour record."""
    paths = []
    if not isinstance(record, dict):
        return paths
    for section in ('source', 'working', 'delivery'):
        info = (record.get(section) or {}).get('profile')
        if isinstance(info, dict) and info.get('path'):
            paths.append(info['path'])
    for loss in record.get('losses', []):
        info = loss.get('profile')
        if isinstance(info, dict) and info.get('path'):
            paths.append(info['path'])
    for event in record.get('events', []):
        info = event.get('profile')
        if isinstance(info, dict) and info.get('path'):
            paths.append(info['path'])
    return paths


class ColorManager:
    """Retain ICC bytes in the workspace so review never depends on originals."""

    def __init__(self, store):
        self.store = store
        (store.root / 'profiles').mkdir(exist_ok=True)
        self._srgb = None

    def record_from_file(self, page, path):
        return record_from_file(page, path, self)

    def retain(self, data):
        info = inspect_profile(data)
        if info is None:
            return None
        relative = f'profiles/{info["sha256"]}.icc'
        path = self.store.root / relative
        if not path.is_file() or path.stat().st_size != len(data):
            temporary = path.with_name(path.name + '.tmp')
            try:
                temporary.write_bytes(data)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        info = dict(info)
        info['path'] = relative
        return info

    def load(self, info):
        if not info:
            return None
        relative = info.get('path') if isinstance(info, dict) else info
        if not relative:
            return None
        try:
            return self.store.resolve(relative).read_bytes()
        except (OSError, DomainError):
            return None

    def srgb(self):
        if self._srgb is None:
            self._srgb = self.retain(SRGB_PROFILE)
        return dict(self._srgb)

    def ensure_page(self, page):
        """Build a colour record for legacy data that predates colour records."""
        record = page.get('color')
        if isinstance(record, dict) and record.get('schema') == 1:
            return record
        path = page.get('original') or page.get('current')
        record = self.record_from_file(page, path)
        return record


def delivery_plan(record, edited=False):
    """Decide, from a page colour record, which colour basis delivery should use."""
    source = record.get('source') or {}
    monochrome = record.get('monochrome') or {}
    profile = source.get('profile')
    plan = {
        'mode': 'screen',
        'family': 'RGB',
        'profile': None,
        'source': 'working',
        'archive_native': False,
        'reason': None,
        'warnings': [],
    }
    if monochrome.get('converted'):
        plan.update({
            'reason': '页面已主动转为单色，转换前的色彩在像素上已不存在；原件与处理历史保留可回看。',
            'warnings': ['主动单色化后不能恢复原色彩。'],
        })
        return plan
    kind = source.get('kind')
    if kind in ('pdf_page', 'composite'):
        plan.update({
            'reason': 'PDF 页面为渲染合成结果，未在重渲染后声明单一原始色域；按屏幕 sRGB 交付，原始 PDF 文件保留完整色彩信息。',
            'warnings': ['PDF 合成页面按 sRGB 交付；原 PDF 保留为重新核对的依据。'],
        })
        return plan
    if profile is not None and not profile.get('valid'):
        plan.update({
            'reason': '内嵌色彩描述损坏，无法在不猜测的前提下保留原色域；按 sRGB 交付。',
            'warnings': ['色彩描述损坏，交付已按 sRGB；原始描述字节与原件保留可回看。'],
        })
        return plan
    if profile is None:
        plan.update({
            'mode': 'preserve',
            'family': 'RGB',
            'profile': {'valid': True, 'family': 'RGB', 'description': WORKING_DESCRIPTION,
                        'recognized': 'srgb', 'assumed': True},
            'source': 'working',
            'reason': '原件没有色彩描述，按工作区一致规则视为 sRGB；屏幕与交付都使用 sRGB 工作结果并嵌入 sRGB 描述。',
            'warnings': ['原件无色彩描述：屏幕与交付均按 sRGB 解释。'],
        })
        return plan
    family = profile.get('family')
    if family not in ('RGB', 'CMYK', 'LAB', 'GRAY'):
        plan.update({
            'reason': '原件色彩描述无法用于受支持的色域，按屏幕 sRGB 交付。',
            'warnings': ['原件色彩描述不可用，交付按 sRGB。'],
        })
        return plan
    if profile.get('recognized') == 'srgb' and family == 'RGB':
        plan.update({
            'mode': 'preserve',
            'family': 'RGB',
            'profile': profile,
            'source': 'native' if not edited else 'working',
            'reason': ('原件色彩描述为 sRGB，屏幕与交付使用同一描述。' if not edited
                       else '原件色彩描述为 sRGB，编辑后的 sRGB 工作结果直接交付并嵌入同一描述。'),
        })
        return plan
    plan.update({
        'mode': 'preserve',
        'family': family,
        'profile': profile,
        'source': 'native' if not edited else 'working',
        'reason': f'原件带有可用色彩描述（{profile.get("description") or family}），交付转换回该色域并嵌入原描述。'
                  + ('页面已处理，交付从 sRGB 工作结果转换回原色域。' if edited else '页面未处理，交付直接使用原始像素。'),
    })
    if edited and family != 'RGB':
        plan['warnings'].append('页面处理在 sRGB 工作空间进行，交付回写原色域时超出 sRGB 的原色已不能精确恢复；原件保留可回看。')
    return plan


def convert_working_to_profile(working_rgb, profile_bytes, family):
    """Convert an sRGB working image into a delivery profile; report gamut loss."""
    mode = FAMILY_MODE.get(family, 'RGB')
    profile_info = inspect_profile(profile_bytes)
    if mode == 'RGB' and profile_info and profile_info.get('recognized') == 'srgb':
        return working_rgb.convert('RGB'), {
            'family': 'RGB', 'mode': 'RGB', 'profile': profile_info,
            'gamut_loss_fraction': 0.0, 'max_error': 0, 'lossy': False,
        }
    transform = _transform(SRGB_PROFILE, profile_bytes, 'RGB', mode)
    converted = ImageCms.applyTransform(working_rgb.convert('RGB'), transform)
    loss = roundtrip_loss(working_rgb, converted, mode, profile_bytes)
    return converted, {
        'family': family,
        'mode': mode,
        'profile': profile_info,
        'gamut_loss_fraction': loss['fraction'],
        'max_error': loss['max_error'],
        'lossy': loss['fraction'] > 0.0,
    }


def roundtrip_loss(working_rgb, delivered, mode, profile_bytes, limit=400_000):
    """Measure what the delivery could not represent back in the sRGB working space."""
    sample_working = working_rgb.convert('RGB')
    sample_delivered = delivered
    pixels = sample_working.width * sample_working.height
    if pixels > limit:
        ratio = (limit / pixels) ** 0.5
        size = (max(1, round(sample_working.width * ratio)), max(1, round(sample_working.height * ratio)))
        sample_working = sample_working.resize(size, Image.Resampling.BILINEAR)
        sample_delivered = sample_delivered.resize(size, Image.Resampling.BILINEAR)
    try:
        reverse = _transform(profile_bytes, SRGB_PROFILE, mode, 'RGB')
        back = ImageCms.applyTransform(sample_delivered.convert(mode), reverse)
    except DomainError:
        return {'fraction': 0.0, 'max_error': 0}
    first = np.asarray(sample_working).astype(np.int16)
    second = np.asarray(back.convert('RGB')).astype(np.int16)
    error = np.abs(first - second)
    if error.ndim == 3:
        error = error.max(axis=2)
    return {
        'fraction': round(float((error > 8).mean()), 6),
        'max_error': int(error.max()) if error.size else 0,
    }


def read_native(path):
    """Open one image file, returning the decoded frame and its ICC bytes."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as opened:
                opened.load()
                profile_bytes = opened.info.get('icc_profile')
                if profile_bytes is None and hasattr(opened, 'tag_v2'):
                    profile_bytes = opened.tag_v2.get(34675)
                image = ImageOps.exif_transpose(opened)
                image = flatten_alpha(image).copy()
                return image, profile_bytes
    except Image.DecompressionBombError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise DomainError('图片损坏或格式不支持：' + str(error)) from error


def read_native_frames(path):
    """Yield (image, profile_bytes) per frame; TIFF frames own their own ICC."""
    frames = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as opened:
                count = getattr(opened, 'n_frames', 1)
                for index in range(count):
                    opened.seek(index)
                    opened.load()
                    profile_bytes = opened.info.get('icc_profile')
                    if profile_bytes is None and hasattr(opened, 'tag_v2'):
                        profile_bytes = opened.tag_v2.get(34675)
                    image = ImageOps.exif_transpose(opened)
                    image = flatten_alpha(image).copy()
                    frames.append((image, profile_bytes))
        return frames
    except Image.DecompressionBombError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise DomainError('图片损坏或格式不支持：' + str(error)) from error


def save_image_file(path, image, profile_bytes, format_name=None):
    """Save an image with an embedded ICC profile, choosing a capable container."""
    path = Path(path)
    if format_name is None:
        extension = path.suffix.lower()
        format_name = {'jpeg': 'JPEG', 'jpg': 'JPEG', 'tif': 'TIFF', 'tiff': 'TIFF',
                       'bmp': 'BMP', 'webp': 'WEBP'}.get(extension.lstrip('.'), 'PNG')
    format_name = format_name.upper()
    image = image.copy()
    options = {}
    if profile_bytes and format_name in ('PNG', 'JPEG', 'TIFF', 'WEBP'):
        options['icc_profile'] = profile_bytes
    if format_name == 'TIFF':
        options.setdefault('compression', 'tiff_lzw')
    if format_name == 'JPEG':
        options.setdefault('quality', 95)
        if image.mode not in ('RGB', 'L', 'CMYK'):
            image = image.convert('RGB')
    if format_name == 'PNG' and image.mode not in ('RGB', 'L', 'RGBA', 'LA', 'P'):
        raise DomainError('PNG 无法保存该颜色模式：' + image.mode)
    temporary = path.with_name(path.name + '.tmp')
    try:
        image.save(temporary, format=format_name, **options)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def bgr_to_pil(image):
    if image.ndim == 2:
        return Image.fromarray(image, 'L')
    if image.shape[2] == 4:
        return Image.fromarray(image[:, :, [2, 1, 0, 3]], 'RGBA')
    return Image.fromarray(image[:, :, ::-1], 'RGB')


def pil_to_bgr(image):
    if image.mode == 'L':
        array = np.asarray(image)
        return np.stack((array, array, array), axis=2)
    if image.mode in ('RGB', 'RGBA'):
        array = np.asarray(image)
        if array.ndim == 3 and array.shape[2] == 4:
            array = array[:, :, :3]
        return array[:, :, ::-1].copy()
    return pil_to_bgr(image.convert('RGB'))
