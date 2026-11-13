"""页面物理尺寸依据（measurement basis）。

页面只保存像素信息本身并不足以推断纸张大小：必须有可信的密度（DPI）或
人工确认的纸边/长度依据。本模块集中管理这类依据的建立、查询，以及各类
几何操作（旋转、裁切、重新采样、透视校正等）对依据的影响，避免在导出环节
暗中套用同一个密度。

依据结构（记录在 page['measurement'] 中）：

{
  'status': 'confirmed' | 'unconfirmed',
  'basis': 'source_dpi' | 'pdf_page' | 'manual_paper' | 'manual_edge' | 'generated',
  'source_dpi': 300.0,                # 依据密度（与原始像素对应）
  'physical_width_mm': 210.0,         # 已确认的当前纸面物理宽度
  'physical_height_mm': 297.0,
  'reference': {...},                 # 建立依据时的可信来源说明
  'confirmed_by': '导入元数据' | '本机用户',
  'confirmed_at': '...',
  'history': [ ... ]                  # 依据自身的变更轨迹（不借用其他版本）
}

注意：
- 来源文件缺少可信密度时绝不推断默认 DPI，页面保持 unconfirmed。
- 透视校正会破坏“像素均匀对应纸面”的前提，依据先失效，交回用户确认。
- 只改变输出清晰度（导出 DPI / resize 重采样）时，已确认的物理尺寸保持不变。
"""
from __future__ import annotations

import copy
import math

from PIL import Image

from .core import DomainError, identity, now, number, require

MM_PER_INCH = 25.4
PT_PER_INCH = 72.0
PT_PER_MM = PT_PER_INCH / MM_PER_INCH

# 可信密度的合理区间，防止写入明显错误的元数据。
MIN_DPI = 8.0
MAX_DPI = 4800.0
MIN_MM = 5.0
MAX_MM = 5000.0

# 会重采样像素但不改变已确认物理尺寸的操作。
RESAMPLE_OPERATIONS = {'resize'}
# 只改变方向、不改变纸边与测量前提的操作。
ORIENTATION_OPERATIONS = {'rotate', 'flip'}
# 不触及几何与采样的操作，依据完全不变。
PRESERVING_OPERATIONS = {
    'illumination', 'color', 'denoise', 'enhance', 'contrast', 'clahe',
    'sharpen', 'white_balance', 'gamma', 'stretch', 'bilateral', 'invert',
    'morph_text', 'sauvola', 'remove_lines', 'flatten_background', 'erase',
}
# 改变纸边/采样范围、需要专门处理的操作（crop/remove_border/pad 在下方显式分派）。
# 破坏均匀测量前提、依据必须失效并交回用户确认的操作。
INVALIDATING_OPERATIONS = {'perspective', 'deskew'}


def empty_measurement():
    """没有任何可信依据的初始状态。"""
    return {
        'status': 'unconfirmed',
        'basis': None,
        'source_dpi': None,
        'physical_width_mm': None,
        'physical_height_mm': None,
        'reference': None,
        'confirmed_by': None,
        'confirmed_at': None,
        'history': [],
    }


def read_file_dpi(path):
    """读取栅格文件自带的密度元数据；缺失或不可信时返回 None，绝不猜测。"""
    try:
        with Image.open(path) as image:
            return read_image_dpi(image)
    except (OSError, ValueError):
        return None


def read_image_dpi(image):
    """从已打开的 PIL 图像（含 TIFF 当前帧）读取可信密度，不可信时返回 None。"""
    dpi = image.info.get('dpi')
    if not dpi:
        return None
    horizontal, vertical = dpi
    try:
        horizontal = float(horizontal)
        vertical = float(vertical)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(horizontal) and math.isfinite(vertical)):
        return None
    if not (MIN_DPI <= horizontal <= MAX_DPI and MIN_DPI <= vertical <= MAX_DPI):
        return None
    # 横纵密度不一致意味着像素不是正方形，无法用单一密度可靠换算，交给人工。
    if abs(horizontal - vertical) > max(1.0, 0.02 * max(horizontal, vertical)):
        return None
    return round(horizontal, 4)


def from_source_dpi(width, height, dpi, reference):
    """依据来源文件中可信的密度元数据建立依据（导入时调用）。"""
    dpi = number(dpi, '来源密度', MIN_DPI, MAX_DPI)
    measurement = empty_measurement()
    measurement.update({
        'status': 'confirmed',
        'basis': 'source_dpi',
        'source_dpi': round(float(dpi), 4),
        'physical_width_mm': round(width * MM_PER_INCH / dpi, 3),
        'physical_height_mm': round(height * MM_PER_INCH / dpi, 3),
        'reference': reference,
        'confirmed_by': '导入元数据',
        'confirmed_at': now(),
    })
    measurement['history'].append({
        'id': identity('measure'),
        'time': measurement['confirmed_at'],
        'action': 'import_source_dpi',
        'basis': 'source_dpi',
        'detail': copy.deepcopy(reference),
    })
    return measurement


def from_pdf_page(width, height, rect, reference):
    """PDF 导入：PDF 页面本身带有以点为单位的真实纸幅，属于可信依据。"""
    rect_width = number(rect.width, 'PDF 页面宽度', 1, 14400)
    rect_height = number(rect.height, 'PDF 页面高度', 1, 14400)
    physical_width = rect_width * MM_PER_INCH / PT_PER_INCH
    physical_height = rect_height * MM_PER_INCH / PT_PER_INCH
    dpi = width * PT_PER_INCH / rect_width
    measurement = empty_measurement()
    measurement.update({
        'status': 'confirmed',
        'basis': 'pdf_page',
        'source_dpi': round(float(dpi), 4),
        'physical_width_mm': round(physical_width, 3),
        'physical_height_mm': round(physical_height, 3),
        'reference': reference,
        'confirmed_by': 'PDF 页面尺寸',
        'confirmed_at': now(),
    })
    measurement['history'].append({
        'id': identity('measure'),
        'time': measurement['confirmed_at'],
        'action': 'import_pdf_page',
        'basis': 'pdf_page',
        'detail': copy.deepcopy(reference),
    })
    return measurement


def generated_measurement():
    """空白页、拼接页等本机生成内容：没有外部物理依据。"""
    measurement = empty_measurement()
    measurement['reference'] = {'kind': 'generated'}
    return measurement


def confirm(measurement, values):
    """人工确认物理尺寸依据。

    支持两种方式：
    - basis=manual_paper：直接给出纸张物理宽高（毫米），可附标准纸名；
    - basis=manual_edge：给出沿图像某条边的实测长度（毫米）及方向，
      另一维按当前像素比例换算。
    两者都必须由用户显式确认，系统不会替缺少依据的照片套用密度。
    """
    require(isinstance(values, dict), '确认参数必须为对象')
    basis = values.get('basis')
    require(basis in ('manual_paper', 'manual_edge'), '确认依据无效')
    current = copy.deepcopy(measurement or empty_measurement())
    entry = {'time': now()}
    if basis == 'manual_paper':
        physical_width = number(values.get('physical_width_mm'), '纸张宽度（毫米）', MIN_MM, MAX_MM)
        physical_height = number(values.get('physical_height_mm'), '纸张高度（毫米）', MIN_MM, MAX_MM)
        width_px = number(values.get('width_px'), '当前图像宽度', 1, 100000, True)
        height_px = number(values.get('height_px'), '当前图像高度', 1, 100000, True)
        reference = {
            'kind': 'manual_paper',
            'paper': values.get('paper'),
            'width_px': width_px,
            'height_px': height_px,
        }
        current.update({
            'status': 'confirmed',
            'basis': 'manual_paper',
            'source_dpi': round(width_px * MM_PER_INCH / physical_width, 4),
            'physical_width_mm': round(float(physical_width), 3),
            'physical_height_mm': round(float(physical_height), 3),
            'reference': reference,
            'confirmed_by': string_or_default(values.get('confirmed_by'), '本机用户'),
            'confirmed_at': entry['time'],
        })
        entry.update(action='confirm_paper', basis='manual_paper',
                     detail={'physical_width_mm': current['physical_width_mm'],
                             'physical_height_mm': current['physical_height_mm'],
                             'paper': values.get('paper')})
    else:
        length_mm = number(values.get('length_mm'), '实测边长（毫米）', MIN_MM, MAX_MM)
        edge = values.get('edge')
        require(edge in ('top', 'bottom', 'left', 'right'), '测量边无效')
        width_px = number(values.get('width_px'), '当前图像宽度', 1, 100000, True)
        height_px = number(values.get('height_px'), '当前图像高度', 1, 100000, True)
        horizontal = edge in ('top', 'bottom')
        edge_pixels = width_px if horizontal else height_px
        dpi = edge_pixels * MM_PER_INCH / length_mm
        physical_width = width_px * MM_PER_INCH / dpi
        physical_height = height_px * MM_PER_INCH / dpi
        reference = {
            'kind': 'manual_edge',
            'edge': edge,
            'edge_pixels': edge_pixels,
            'length_mm': round(float(length_mm), 3),
            'width_px': width_px,
            'height_px': height_px,
        }
        current.update({
            'status': 'confirmed',
            'basis': 'manual_edge',
            'source_dpi': round(float(dpi), 4),
            'physical_width_mm': round(physical_width, 3),
            'physical_height_mm': round(physical_height, 3),
            'reference': reference,
            'confirmed_by': string_or_default(values.get('confirmed_by'), '本机用户'),
            'confirmed_at': entry['time'],
        })
        entry.update(action='confirm_edge', basis='manual_edge', detail=copy.deepcopy(reference))
    entry['id'] = identity('measure')
    current.setdefault('history', []).append(entry)
    return current


def string_or_default(value, default):
    if value is None:
        return default
    require(isinstance(value, str) and value.strip(), '确认人必须为文本')
    return value.strip()[:80]


def invalidate(measurement, operation, detail=None):
    """依据无法继续成立：保留历史记录，但交回用户重新确认。"""
    current = copy.deepcopy(measurement or empty_measurement())
    previous = {
        'basis': current.get('basis'),
        'source_dpi': current.get('source_dpi'),
        'physical_width_mm': current.get('physical_width_mm'),
        'physical_height_mm': current.get('physical_height_mm'),
    }
    current['status'] = 'unconfirmed'
    current['basis'] = None
    current['source_dpi'] = None
    current['physical_width_mm'] = None
    current['physical_height_mm'] = None
    current['confirmed_by'] = None
    current['confirmed_at'] = None
    current.setdefault('history', []).append({
        'id': identity('measure'),
        'time': now(),
        'action': 'invalidate',
        'operation': operation,
        'previous': previous,
        'detail': detail or {},
    })
    return current


def apply_geometry(measurement, operation, params, before_size, after_size, metadata=None):
    """根据几何操作的性质更新测量依据。

    before_size / after_size 为 (width_px, height_px)。返回新的 measurement。
    """
    current = copy.deepcopy(measurement or empty_measurement())
    before_w, before_h = before_size
    after_w, after_h = after_size
    metadata = metadata or {}

    if operation in PRESERVING_OPERATIONS:
        return current
    if operation in ORIENTATION_OPERATIONS:
        if current['status'] == 'confirmed':
            dimensions_swapped = (before_w, before_h) != (after_w, after_h) and (before_w, before_h) == (after_h, after_w)
            if dimensions_swapped:
                # 只有像素宽高真正互换时（90/270 度旋转、改变方向的镜像）才交换物理尺寸；
                # 0/180 度等像素尺寸不变的操作保持物理尺寸与密度不变。
                old_width_mm = current['physical_width_mm']
                old_height_mm = current['physical_height_mm']
                current['physical_width_mm'] = round(old_height_mm, 3)
                current['physical_height_mm'] = round(old_width_mm, 3)
                current['source_dpi'] = round(after_w * MM_PER_INCH / current['physical_width_mm'], 4)
                current.setdefault('history', []).append({
                    'id': identity('measure'), 'time': now(), 'action': 'orientation',
                    'operation': operation,
                    'physical_width_mm': current['physical_width_mm'],
                    'physical_height_mm': current['physical_height_mm'],
                })
        return current
    if operation in RESAMPLE_OPERATIONS:
        # 重新采样只改变像素密度：物理尺寸保持不变，按新像素数反推密度。
        if current['status'] == 'confirmed':
            current['source_dpi'] = round(after_w * MM_PER_INCH / current['physical_width_mm'], 4)
            current.setdefault('history', []).append({
                'id': identity('measure'), 'time': now(), 'action': 'resample',
                'operation': operation, 'before_dpi': measurement.get('source_dpi'),
                'after_dpi': current['source_dpi'],
                'physical_width_mm': current['physical_width_mm'],
                'physical_height_mm': current['physical_height_mm'],
            })
        return current
    if operation == 'crop':
        return apply_crop(current, operation, params.get('region'), before_size, after_size)
    if operation == 'remove_border':
        border = int(params.get('size', 12))
        region = [border, border, before_w - 2 * border, before_h - 2 * border]
        return apply_crop(current, operation, region, before_size, after_size)
    if operation == 'pad':
        return apply_pad(current, operation, params, before_size, after_size)
    if operation in INVALIDATING_OPERATIONS:
        return invalidate(current, operation, metadata)
    # 未识别的几何操作保守处理：交回用户确认，绝不沿用旧密度。
    return invalidate(current, operation, {'reason': '未登记的几何操作'})


def apply_crop(current, operation, region, before_size, after_size):
    """裁切保留的是裁出区域对应的纸面：密度不变，物理宽高按像素比例缩小。"""
    before_w, before_h = before_size
    after_w, after_h = after_size
    if current['status'] != 'confirmed':
        return current
    require(region is not None and len(region) == 4, '裁切区域缺失')
    x, y, region_w, region_h = region
    dpi = current['source_dpi']
    physical_width = after_w * MM_PER_INCH / dpi
    physical_height = after_h * MM_PER_INCH / dpi
    current.update({
        'source_dpi': round(float(dpi), 4),
        'physical_width_mm': round(physical_width, 3),
        'physical_height_mm': round(physical_height, 3),
        'confirmed_at': now(),
    })
    current.setdefault('history', []).append({
        'id': identity('measure'), 'time': now(), 'action': 'crop',
        'operation': operation, 'region': list(region),
        'physical_width_mm': current['physical_width_mm'],
        'physical_height_mm': current['physical_height_mm'],
    })
    return current


def apply_pad(current, operation, params, before_size, after_size):
    """加白边扩大了画面但没有改变原纸；原纸依据需要用户重新界定，先失效。"""
    return invalidate(current, operation, {'reason': '扩展白边改变了画面边界'})


def is_confirmed(measurement):
    return bool(measurement and measurement.get('status') == 'confirmed'
                and measurement.get('physical_width_mm') and measurement.get('physical_height_mm'))


def physical_points(measurement):
    """返回当前页面对应的 PDF 纸幅（点）；无可靠依据时返回 None。"""
    if not is_confirmed(measurement):
        return None
    return (
        round(measurement['physical_width_mm'] * PT_PER_MM, 3),
        round(measurement['physical_height_mm'] * PT_PER_MM, 3),
    )


def effective_dpi(measurement, width_px):
    """当前像素与已确认物理尺寸对应的有效密度。"""
    if not is_confirmed(measurement):
        return None
    return round(width_px * MM_PER_INCH / measurement['physical_width_mm'], 4)


def require_confirmed(pages):
    """导出 PDF 前逐页核对依据，缺少可靠依据的页面不得暗中套用密度。"""
    missing = [page['name'] for page in pages if not is_confirmed(page.get('measurement'))]
    if missing:
        raise DomainError(
            '以下页面缺少可信的物理尺寸依据，请先逐页确认纸张大小或实测边长后再导出 PDF：'
            + '、'.join(missing[:10]) + ('…' if len(missing) > 10 else ''),
            409, 'measurement_unconfirmed')


def summary(measurement):
    """供界面与报告逐页核对的依据摘要。"""
    current = measurement or empty_measurement()
    result = {
        'status': current.get('status', 'unconfirmed'),
        'basis': current.get('basis'),
        'source_dpi': current.get('source_dpi'),
        'physical_width_mm': current.get('physical_width_mm'),
        'physical_height_mm': current.get('physical_height_mm'),
        'confirmed_by': current.get('confirmed_by'),
        'confirmed_at': current.get('confirmed_at'),
        'reference': current.get('reference'),
        'history_length': len(current.get('history', [])),
    }
    points = physical_points(current)
    result['physical_width_pt'] = points[0] if points else None
    result['physical_height_pt'] = points[1] if points else None
    return result


def reanchor(measurement_basis, width_px, height_px):
    """把已确认依据重新锚定到给定像素尺寸。

    用于历史版本恢复：恢复目标图像的像素方向可能与依据记录时不同（例如经历过
    旋转链），不能直接复用旧密度。物理尺寸是已经确认的事实，保持不变；
    密度按恢复后图像的实际像素与物理尺寸重新推导。只有像素宽高比与物理宽高比
    明确指向相反方向时才交换物理宽高，避免仅凭比例差异误判。
    若该版本依据已失效，则原样返回失效状态，绝不复活旧密度。
    """
    current = copy.deepcopy(measurement_basis or empty_measurement())
    current = normalize(current)
    if not is_confirmed(current):
        return current
    physical_width = current['physical_width_mm']
    physical_height = current['physical_height_mm']
    pixel_landscape = width_px > height_px
    physical_landscape = physical_width > physical_height
    swapped = pixel_landscape != physical_landscape
    if swapped:
        physical_width, physical_height = physical_height, physical_width
    dpi = width_px * MM_PER_INCH / physical_width
    current.update({
        'status': 'confirmed',
        'source_dpi': round(float(dpi), 4),
        'physical_width_mm': round(physical_width, 3),
        'physical_height_mm': round(physical_height, 3),
    })
    current.setdefault('history', []).append({
        'id': identity('measure'),
        'time': now(),
        'action': 'reanchor',
        'width_px': width_px,
        'height_px': height_px,
        'swapped': bool(swapped),
        'physical_width_mm': current['physical_width_mm'],
        'physical_height_mm': current['physical_height_mm'],
    })
    return current


def normalize(measurement):
    """读取历史数据时补齐字段，兼容没有 measurement 记录的旧页面。"""
    if not isinstance(measurement, dict):
        return empty_measurement()
    base = empty_measurement()
    base.update({key: measurement.get(key) for key in base if key != 'history'})
    base['history'] = measurement.get('history') if isinstance(measurement.get('history'), list) else []
    if base['status'] not in ('confirmed', 'unconfirmed'):
        base['status'] = 'unconfirmed'
    if base['status'] != 'confirmed':
        for key in ('basis', 'source_dpi', 'physical_width_mm', 'physical_height_mm',
                    'confirmed_by', 'confirmed_at'):
            base[key] = None
    return base
