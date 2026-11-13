from __future__ import annotations

import io
import json
import re
import zipfile
import fitz
from PIL import Image

from . import color
from .core import DomainError, choice, identity, now, number, require


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')
    return value[:100] or 'PaperFlow'


def pdf_lab_colorspace(image):
    """Return a PDF /Lab colorspace array matching an 8-bit LAB image."""
    return '[/Lab << /WhitePoint [0.9642 1.0 0.8249] /Range [-128 127 -128 127] >>]'


class Exporter:
    def __init__(self, store):
        self.store = store
        self.colors = color.ColorManager(store)

    def select(self, document_id, page_ids=None):
        state = self.store.snapshot()
        document = state['documents'].get(document_id)
        require(document is not None, '文档不存在', 404)
        if page_ids is None:
            page_ids = document['page_ids']
        require(isinstance(page_ids, list) and len(page_ids) > 0, '没有可导出的页面')
        require(len(page_ids) == len(set(page_ids)), '导出页面重复')
        require(set(page_ids).issubset(document['page_ids']), '导出页面不属于该文档')
        pages = [state['pages'][page_id] for page_id in page_ids]
        return document, pages

    def color_record(self, page):
        record = page.get('color')
        if not isinstance(record, dict) or record.get('schema') != 1:
            record = self.colors.record_from_file(page, page.get('original') or page.get('current'))
        return record

    def color_plan(self, page):
        record = self.color_record(page)
        edited = page.get('original') != page.get('current')
        return record, color.delivery_plan(record, edited=edited)

    def profile_bytes(self, check):
        data = self.colors.load(check) if check else None
        return data or color.SRGB_PROFILE

    def _native_available(self, page):
        native = page.get('native')
        return bool(native) and self.store.resolve(native).is_file()

    def delivery(self, page, format_name):
        """Produce one delivery image and an explicit statement of its colour basis.

        Returns ``(PIL image, icc bytes or None, report dict)``.  The report is
        what both the export record and the user-facing summary are built from.
        """
        record, plan = self.color_plan(page)
        report = {
            'mode': plan.get('mode'),
            'family': plan.get('family'),
            'profile': plan.get('profile'),
            'source': plan.get('source'),
            'reason': plan.get('reason'),
            'warnings': list(plan.get('warnings') or []),
            'losses': [loss.get('message') for loss in record.get('losses', [])],
            'monochrome': bool((record.get('monochrome') or {}).get('converted')),
        }
        working = self.store.resolve(page['current'])
        family = plan.get('family') if plan.get('mode') == 'preserve' else 'RGB'
        if family not in color.FORMAT_FAMILIES.get(format_name, {'RGB', 'GRAY'}):
            report['requested_family'] = family
            report['delivery_family'] = 'RGB'
            report['warnings'].append(f'{format_name.upper()} 无法保存 {family} 色域；交付提供 sRGB 工作结果与单独的原色域文件，原件保留原色彩。')
            report['losses'].append(f'所选格式不支持 {family}，该页交付的 sRGB 图失去原色域声明（同批附带原色域副本）。')
            family = 'RGB'
            plan = dict(plan, mode='screen')
            report['mode'] = 'screen'
            report['native_fallback'] = bool(self._native_available(page))
        if plan.get('mode') != 'preserve':
            with Image.open(working) as image:
                converted = image.convert('RGB')
            report.setdefault('delivery_family', 'RGB')
            return converted.copy(), color.SRGB_PROFILE, report
        if plan.get('source') == 'native' and page.get('native'):
            try:
                with Image.open(self.store.resolve(page['native'])) as native:
                    native.load()
                    profile_bytes = native.info.get('icc_profile') or (
                        native.tag_v2.get(34675) if hasattr(native, 'tag_v2') else None)
                    profile_bytes = profile_bytes or self.profile_bytes(plan.get('profile'))
                    report['delivery_family'] = family
                    return native.copy(), profile_bytes, report
            except (OSError, ValueError):
                report['warnings'].append('原件原生颜色副本不可读，改用编辑后的 sRGB 工作结果并转换回原色域。')
        profile_bytes = self.profile_bytes(plan.get('profile'))
        with Image.open(working) as image:
            working_rgb = image.convert('RGB')
        try:
            delivered, loss = color.convert_working_to_profile(working_rgb, profile_bytes, family)
        except DomainError as error:
            report['mode'] = 'screen'
            report['warnings'].append('无法转换回原色域，按 sRGB 交付：' + str(error))
            report['losses'].append('交付未能保留原色域。')
            return working_rgb, color.SRGB_PROFILE, report
        report['delivery_family'] = family
        report['gamut_loss_fraction'] = loss['gamut_loss_fraction']
        report['gamut_max_error'] = loss['max_error']
        if loss['lossy']:
            report['warnings'].append('部分颜色超出可回写范围，交付按原色域近似表示；原件保留可回看。')
            report['losses'].append('超出 sRGB 工作空间的原色不能在处理后的交付中精确恢复。')
        return delivered, profile_bytes, report

    def text(self, pages):
        chunks = []
        for index, page in enumerate(pages):
            chunks.append(f'===== 第 {index + 1} 页 · {page["name"]} =====\n')
            chunks.append(page['ocr']['text'] if page.get('ocr') else '[此页尚未识别]')
            chunks.append('\n\n')
        return ''.join(chunks).encode('utf-8-sig')

    def pdf(self, pages, searchable=False, dpi=150):
        dpi = number(dpi, 'PDF 分辨率', 72, 600)
        document = fitz.open()
        colour_report = []
        try:
            for source in pages:
                width = source['width'] * 72 / dpi
                height = source['height'] * 72 / dpi
                page = document.new_page(width=width, height=height)
                image, profile_bytes, report = self.delivery(source, 'tiff')
                xref = self.insert_colour_image(document, page, image, profile_bytes, report)
                report['xref'] = xref
                report['page_id'] = source['id']
                colour_report.append(report)
                if searchable:
                    result = source.get('ocr')
                    require(result is not None, '可搜索 PDF 要求所有页面先完成 OCR', 409)
                    require(not result.get('stale'), '页面图像已变化，请更新 OCR 后导出可搜索 PDF', 409)
                    self.insert_text(page, source, dpi)
            document.set_metadata({
                'title': 'PaperFlow 数字化文档',
                'author': 'PaperFlow',
                'creator': 'PaperFlow local document workbench',
                'subject': '本地离线扫描文档',
            })
            self.last_pdf_colour = colour_report
            return document.tobytes(garbage=4, deflate=True)
        finally:
            document.close()

    def insert_colour_image(self, document, pdf_page, image, profile_bytes, report):
        """Insert an image and attach its ICC description inside the PDF.

        PDF has no ICC slot for Lab images, so Lab delivery uses the PDF /Lab
        colour space with the D50 white point and explicit decode range; every
        other family embeds the exact ICC profile bytes used for delivery.
        """
        family = (report.get('delivery_family') or report.get('family') or 'RGB').upper()
        buffer = io.BytesIO()
        if family == 'LAB':
            image = image.convert('LAB')
            image.save(buffer, format='TIFF', compression='tiff_lzw')
        elif family == 'CMYK':
            image = image.convert('CMYK')
            image.save(buffer, format='TIFF', compression='tiff_lzw')
        elif family == 'GRAY':
            image = image.convert('L')
            image.save(buffer, format='PNG')
        else:
            image = image.convert('RGB')
            image.save(buffer, format='PNG')
        xref = pdf_page.insert_image(pdf_page.rect, stream=buffer.getvalue(), alpha=0)
        info = color.inspect_profile(profile_bytes)
        if family == 'LAB':
            document.xref_set_key(xref, 'ColorSpace', pdf_lab_colorspace(image))
            document.xref_set_key(xref, 'Decode', '[0 100 -128 127 -128 127]')
            report['pdf_colorspace'] = 'Lab (D50)'
            report['pdf_profile_sha256'] = info.get('sha256') if info else None
            return xref
        if family == 'GRAY':
            if info and info.get('valid') and info.get('family') == 'GRAY':
                self._embed_icc(document, xref, profile_bytes, 1, '/DeviceGray')
                report['pdf_colorspace'] = 'ICCBased(1)'
                report['pdf_profile_sha256'] = info.get('sha256')
            else:
                report['pdf_colorspace'] = 'DeviceGray'
            return xref
        if not info or not info.get('valid') or info.get('family') != family:
            document.xref_set_key(xref, 'ColorSpace', '[ /ICCBased %d 0 R ]' % self._embed_icc(document, xref, color.SRGB_PROFILE, 3, '/DeviceRGB'))
            report['warnings'].append('交付色彩描述不可用，PDF 内嵌 sRGB 描述。')
            report['pdf_colorspace'] = 'ICCBased(3)'
            report['pdf_profile_sha256'] = color.SRGB_SHA256
            return xref
        components = 4 if family == 'CMYK' else 3
        alternate = '/DeviceCMYK' if family == 'CMYK' else '/DeviceRGB'
        self._embed_icc(document, xref, profile_bytes, components, alternate)
        report['pdf_colorspace'] = f'ICCBased({components})'
        report['pdf_profile_sha256'] = info.get('sha256')
        return xref

    @staticmethod
    def _embed_icc(document, xref, data, components, alternate):
        icc_xref = document.get_new_xref()
        document.update_object(icc_xref, f'<< /N {components} >>')
        document.update_stream(icc_xref, data, compress=True)
        document.xref_set_key(icc_xref, 'N', str(components))
        document.xref_set_key(icc_xref, 'Alternate', alternate)
        document.xref_set_key(xref, 'ColorSpace', f'[/ICCBased {icc_xref} 0 R]')
        return icc_xref

    def insert_text(self, pdf_page, source, dpi):
        result = source['ocr']
        scale = 72 / dpi
        if result.get('text_override'):
            lines = result['text'].splitlines() or ['']
            box = fitz.Rect(12, 12, pdf_page.rect.width - 12, pdf_page.rect.height - 12)
            font_size = min(11, max(3, box.height / (len(lines) * 1.5)))
            text = '\n'.join(lines)
            for _ in range(12):
                used = pdf_page.insert_textbox(box, text, fontname='china-s', fontsize=font_size, render_mode=3)
                if used >= 0:
                    return
                font_size *= 0.85
            raise DomainError('人工文本过长，无法完整写入此页文字层；请减少文本或编辑文字框')
        for word in result['boxes']:
            text = word['text']
            if not text:
                continue
            x = word['x'] * scale
            y = word['y'] * scale
            width = max(word['width'] * scale, 1)
            height = max(word['height'] * scale, 1)
            font_size = max(1, height * 0.83)
            estimated = fitz.get_text_length(text, fontname='china-s', fontsize=font_size)
            if estimated > width:
                font_size *= width / estimated
            pdf_page.insert_text((x, y + height * 0.85), text, fontname='china-s', fontsize=font_size, render_mode=3)

    def image_archive(self, pages, image_format='png', quality=95):
        choice(image_format, '图片格式', ('png', 'jpeg', 'tiff'))
        quality = number(quality, 'JPEG 质量', 30, 100, True)
        buffer = io.BytesIO()
        reports = []
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for index, page in enumerate(pages):
                image, profile_bytes, report = self.delivery(page, image_format)
                stream = io.BytesIO()
                options = {}
                if profile_bytes:
                    options['icc_profile'] = profile_bytes
                if image_format == 'tiff':
                    options['compression'] = 'tiff_lzw'
                if image_format == 'jpeg':
                    options['quality'] = quality
                    if image.mode not in ('RGB', 'L', 'CMYK'):
                        image = image.convert('RGB')
                image.save(stream, format=image_format.upper(), **options)
                extension = 'jpg' if image_format == 'jpeg' else image_format
                report['file'] = f'{index + 1:04d}_{safe_name(page["name"])}.{extension}'
                report['page_id'] = page['id']
                reports.append(report)
                archive.writestr(report['file'], stream.getvalue())
                if report.get('native_fallback') and self._native_available(page):
                    native_name = f'{index + 1:04d}_{safe_name(page["name"])}_原色域'
                    with Image.open(self.store.resolve(page['native'])) as native:
                        native.load()
                        extension = {'CMYK': '.tif', 'LAB': '.tif'}.get(report.get('requested_family'), '.tif')
                        native_stream = io.BytesIO()
                        native.save(native_stream, format='TIFF', compression='tiff_lzw',
                                    icc_profile=native.info.get('icc_profile') or profile_bytes)
                    archive.writestr(native_name + extension, native_stream.getvalue())
                    report['native_file'] = native_name + extension
            archive.writestr('colour-report.json', json.dumps({
                'format': 'paperflow-colour-report',
                'schema': 1,
                'working_space': color.WORKING_SPACE,
                'delivery_format': image_format,
                'pages': reports,
            }, ensure_ascii=False, indent=2))
        return buffer.getvalue(), reports

    def metadata_archive(self, document, pages, include_originals=True):
        buffer = io.BytesIO()
        records = []
        for page in pages:
            records.append({
                'page_id': page['id'],
                'color': self.color_record(page),
                'delivery': self.color_plan(page)[1],
            })
        metadata = {
            'format': 'paperflow-archive',
            'schema': 1,
            'exported_at': now(),
            'document': document,
            'pages': pages,
            'order': [page['id'] for page in pages],
            'sources': {},
            'colour': {
                'working_space': color.WORKING_SPACE,
                'description': '屏幕审阅、缩略图与全部像素处理在 sRGB 工作空间；交付色域见每页 delivery。',
                'pages': records,
            },
        }
        state = self.store.snapshot()
        source_ids = {page.get('source_file_id') for page in pages}
        metadata['sources'] = {key: value for key, value in state.get('sources', {}).items() if key in source_ids}
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            paths = {source['path'] for source in metadata['sources'].values()}
            colour_paths = set()
            for page in pages:
                paths.update((page['current'], page['thumbnail'], page['original']))
                if page.get('native'):
                    paths.add(page['native'])
                if include_originals:
                    for entry in page['history']:
                        paths.update((entry['before'], entry['after']))
                paths.update(region['path'] for region in page.get('regions', []))
                colour_paths.update(color.profile_paths(self.color_record(page)))
            for relative in sorted(paths):
                path = self.store.resolve(relative)
                require(path.is_file(), '文件缺失：' + relative, 500)
                archive.write(path, relative)
            for relative in sorted(colour_paths):
                path = self.store.resolve(relative)
                if path.is_file():
                    archive.write(path, relative)
            archive.writestr('metadata.json', json.dumps(metadata, ensure_ascii=False, indent=2))
            archive.writestr('recognized.txt', self.text(pages))
            archive.writestr('README.txt', (
                'PaperFlow 归档\nmetadata.json 中所有二进制文件引用均相对此 ZIP 根目录。\n'
                '文档页面顺序见 order；原图与处理历史单独保留。\n'
                'colour 段说明每页原件的色彩描述、sRGB 工作空间与交付色域；profiles/ 内为实际使用的 ICC 文件。\n'
                'pages[*].native 为原色域像素副本，pages[*].original 为 sRGB 工作空间的原始渲染。\n'
            ))
        return buffer.getvalue(), records

    def export(self, document_id, kind, options=None):
        options = options or {}
        choice(kind, '导出类型', ('images', 'txt', 'pdf', 'searchable_pdf', 'zip'))
        document, pages = self.select(document_id, options.get('page_ids'))
        suffix = '.zip'
        mime = 'application/zip'
        colour_summary = None
        if kind == 'txt':
            data = self.text(pages)
            suffix, mime = '.txt', 'text/plain; charset=utf-8'
        elif kind in ('pdf', 'searchable_pdf'):
            self.last_pdf_colour = None
            data = self.pdf(pages, searchable=kind == 'searchable_pdf', dpi=options.get('dpi', 150))
            suffix, mime = '.pdf', 'application/pdf'
            colour_summary = self.last_pdf_colour
        elif kind == 'images':
            data, colour_summary = self.image_archive(pages, options.get('image_format', 'png'), options.get('quality', 95))
        else:
            data, colour_summary = self.metadata_archive(document, pages, options.get('include_originals', True))
        if colour_summary is None:
            colour_summary = [dict(self.color_plan(page)[1], page_id=page['id']) for page in pages]
        path = self.store.new_file('exports', suffix)
        path.write_bytes(data)
        record = {
            'id': identity('export'),
            'document_id': document_id,
            'kind': kind,
            'path': self.store.relative(path),
            'filename': safe_name(document['name']) + ('_可搜索' if kind == 'searchable_pdf' else '') + suffix,
            'mime': mime,
            'bytes': len(data),
            'page_ids': [page['id'] for page in pages],
            'created_at': now(),
            'color': {
                'working_space': color.WORKING_SPACE,
                'pages': colour_summary,
            },
        }
        with self.store.transaction() as state:
            state.setdefault('exports', {})[record['id']] = record
            self.store.event(state, '导出文档', document_id, {'export_id': record['id'], 'kind': kind})
        return record
