from __future__ import annotations

import csv
import html
import io
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

from . import measurement
from .core import choice, identity, now, require
from .exports import safe_name
from .layout import ocr_lines, paragraphs


def xml_bytes(root):
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def hocr_page(page, page_number):
    result = page.get('ocr')
    width = page['width']
    height = page['height']
    page_id = 'page_' + str(page_number)
    root = ET.Element('div', {
        'class': 'ocr_page',
        'id': page_id,
        'title': f'bbox 0 0 {width} {height}; ppageno {page_number - 1}',
    })
    if not result:
        return root
    boxes = {box['id']: box for box in result['boxes']}
    for line_index, line in enumerate(ocr_lines(result)):
        x, y, w, h = line['rect']
        line_node = ET.SubElement(root, 'span', {
            'class': 'ocr_line',
            'id': f'line_{page_number}_{line_index}',
            'title': f'bbox {x} {y} {x + w} {y + h}',
        })
        for word_index, identifier in enumerate(line['word_ids']):
            word = boxes[identifier]
            x = word['x']
            y = word['y']
            right = x + word['width']
            bottom = y + word['height']
            node = ET.SubElement(line_node, 'span', {
                'class': 'ocrx_word',
                'id': f'word_{page_number}_{line_index}_{word_index}',
                'title': f'bbox {x} {y} {right} {bottom}; x_wconf {round(word["confidence"])}',
            })
            node.text = word['text']
            node.tail = ' '
    return root


def export_hocr(document, pages):
    root = ET.Element('html', {'lang': 'zh-CN'})
    head = ET.SubElement(root, 'head')
    ET.SubElement(head, 'meta', {'charset': 'utf-8'})
    ET.SubElement(head, 'meta', {'name': 'ocr-system', 'content': 'PaperFlow / Tesseract'})
    ET.SubElement(head, 'meta', {'name': 'ocr-capabilities', 'content': 'ocr_page ocr_line ocrx_word'})
    ET.SubElement(head, 'title').text = document['name']
    body = ET.SubElement(root, 'body')
    for index, page in enumerate(pages):
        body.append(hocr_page(page, index + 1))
    return b'<!DOCTYPE html>\n' + ET.tostring(root, encoding='utf-8', method='html')


def export_alto(document, pages):
    namespace = 'http://www.loc.gov/standards/alto/ns-v4#'
    ET.register_namespace('', namespace)
    def tag(name):
        return '{' + namespace + '}' + name
    root = ET.Element(tag('alto'))
    description = ET.SubElement(root, tag('Description'))
    ET.SubElement(description, tag('MeasurementUnit')).text = 'pixel'
    source = ET.SubElement(description, tag('sourceImageInformation'))
    ET.SubElement(source, tag('fileName')).text = document['name']
    processing = ET.SubElement(description, tag('OCRProcessing'), {'ID': 'processing_1'})
    step = ET.SubElement(processing, tag('ocrProcessingStep'))
    ET.SubElement(step, tag('processingDateTime')).text = now()
    software = ET.SubElement(step, tag('processingSoftware'))
    ET.SubElement(software, tag('softwareName')).text = 'PaperFlow'
    ET.SubElement(software, tag('softwareVersion')).text = '1.0.0'
    layout = ET.SubElement(root, tag('Layout'))
    for page_index, page in enumerate(pages):
        page_node = ET.SubElement(layout, tag('Page'), {
            'ID': 'page_' + str(page_index + 1),
            'PHYSICAL_IMG_NR': str(page_index + 1),
            'WIDTH': str(page['width']),
            'HEIGHT': str(page['height']),
        })
        space = ET.SubElement(page_node, tag('PrintSpace'), {
            'HPOS': '0',
            'VPOS': '0',
            'WIDTH': str(page['width']),
            'HEIGHT': str(page['height']),
        })
        result = page.get('ocr')
        if not result:
            continue
        boxes = {box['id']: box for box in result['boxes']}
        for line_index, line in enumerate(ocr_lines(result)):
            x, y, w, h = line['rect']
            block = ET.SubElement(space, tag('TextBlock'), {
                'ID': f'block_{page_index}_{line_index}',
                'HPOS': str(x),
                'VPOS': str(y),
                'WIDTH': str(w),
                'HEIGHT': str(h),
            })
            line_node = ET.SubElement(block, tag('TextLine'), {
                'ID': f'line_{page_index}_{line_index}',
                'HPOS': str(x),
                'VPOS': str(y),
                'WIDTH': str(w),
                'HEIGHT': str(h),
            })
            for word_index, identifier in enumerate(line['word_ids']):
                word = boxes[identifier]
                ET.SubElement(line_node, tag('String'), {
                    'ID': f'word_{page_index}_{line_index}_{word_index}',
                    'CONTENT': word['text'],
                    'HPOS': str(word['x']),
                    'VPOS': str(word['y']),
                    'WIDTH': str(word['width']),
                    'HEIGHT': str(word['height']),
                    'WC': str(round(max(0, word['confidence']) / 100, 3)),
                })
    return xml_bytes(root)


def export_csv(document, pages):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(['document', 'page', 'page_name', 'text', 'confidence', 'x', 'y', 'width', 'height', 'edited'])
    for index, page in enumerate(pages):
        result = page.get('ocr')
        if not result:
            continue
        for word in result['boxes']:
            text = word['text']
            if text.startswith(('=', '+', '-', '@', '\t', '\r')):
                text = "'" + text
            writer.writerow([
                document['name'],
                index + 1,
                page['name'],
                text,
                word['confidence'],
                word['x'],
                word['y'],
                word['width'],
                word['height'],
                word.get('edited', False),
            ])
    return stream.getvalue().encode('utf-8-sig')


def export_markdown(document, pages):
    parts = ['# ' + document['name'], '']
    for index, page in enumerate(pages):
        parts.append(f'## 第 {index + 1} 页：{page["name"]}')
        parts.append('')
        result = page.get('ocr')
        if result:
            parts.append(result['text'])
        else:
            parts.append('（尚未进行文字识别）')
        parts.append('')
        parts.append('---')
        parts.append('')
    return '\n'.join(parts).encode('utf-8')


def export_multipage_tiff(store, pages):
    images = []
    output = io.BytesIO()
    try:
        for page in pages:
            with Image.open(store.resolve(page['current'])) as source:
                frame = source.convert('RGB').copy()
            # 每帧携带所属页面自己的有效密度；无依据页面不写入密度，避免暗中假设。
            effective = measurement.effective_dpi(page.get('measurement'), frame.size[0])
            if effective:
                frame.info['dpi'] = (effective, effective)
            images.append(frame)
        require(bool(images), '没有可导出页面')
        first, rest = images[0], images[1:]
        first_kwargs = {'format': 'TIFF', 'save_all': True, 'append_images': rest,
                        'compression': 'tiff_lzw'}
        if 'dpi' in first.info:
            first_kwargs['dpi'] = first.info['dpi']
        first.save(output, **first_kwargs)
        return output.getvalue()
    finally:
        for image in images:
            image.close()


class Interchange:
    def __init__(self, store, exporter):
        self.store = store
        self.exporter = exporter

    def export(self, document_id, kind):
        choice(kind, '交换格式', ('hocr', 'alto', 'csv', 'markdown', 'tiff'))
        document, pages = self.exporter.select(document_id)
        formats = {
            'hocr': (export_hocr, '.html', 'text/html'),
            'alto': (export_alto, '.xml', 'application/xml'),
            'csv': (export_csv, '.csv', 'text/csv'),
            'markdown': (export_markdown, '.md', 'text/markdown'),
        }
        if kind == 'tiff':
            data = export_multipage_tiff(self.store, pages)
            suffix = '.tiff'
            mime = 'image/tiff'
        else:
            function, suffix, mime = formats[kind]
            data = function(document, pages)
        path = self.store.new_file('exports', suffix)
        path.write_bytes(data)
        record = {
            'id': identity('export'),
            'document_id': document_id,
            'kind': kind,
            'path': self.store.relative(path),
            'filename': safe_name(document['name']) + suffix,
            'mime': mime,
            'bytes': len(data),
            'page_ids': [page['id'] for page in pages],
            'created_at': now(),
        }
        with self.store.transaction() as state:
            state.setdefault('exports', {})[record['id']] = record
            self.store.event(state, '导出交换格式', document_id, {'kind': kind})
        return record
