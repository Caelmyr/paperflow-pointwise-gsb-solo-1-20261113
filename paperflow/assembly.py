from __future__ import annotations

import cv2
import numpy as np

from .core import choice, now, number, require, string
from .imaging import read_image, validate_image


class Assembly:
    def __init__(self, store, documents):
        self.store = store
        self.documents = documents

    def blank(self, document_id, values):
        width = number(values.get('width', 1240), '空白页宽度', 32, 10000, True)
        height = number(values.get('height', 1754), '空白页高度', 32, 10000, True)
        require(width * height <= 36_000_000, '空白页像素过多')
        name = string(values.get('name', '空白页'), '页面名称', 200)
        color = number(values.get('gray', 255), '背景灰度', 0, 255, True)
        image = np.full((height, width, 3), color, dtype=np.uint8)
        return self.documents.add_image(document_id, image, name, {'type': 'generated', 'kind': 'blank'}, values.get('position'))

    def split_spread(self, page_id, values):
        page = self.store.get('pages', page_id)
        image = read_image(self.store.resolve(page['current']))
        axis = choice(values.get('axis', 'vertical'), '分割方向', ('vertical', 'horizontal'))
        ratio = number(values.get('ratio', 0.5), '分割比例', 0.1, 0.9)
        overlap = number(values.get('overlap', 0), '重叠像素', 0, 200, True)
        target_id = values.get('target_id', page['document_id'])
        self.store.get('documents', target_id)
        height, width = image.shape[:2]
        if axis == 'vertical':
            split = round(width * ratio)
            first = image[:, :min(width, split + overlap)].copy()
            second = image[:, max(0, split - overlap):].copy()
        else:
            split = round(height * ratio)
            first = image[:min(height, split + overlap)].copy()
            second = image[max(0, split - overlap):].copy()
        validate_image(first)
        validate_image(second)
        source = {'type': 'generated', 'kind': 'split_spread', 'parent_page_id': page_id}
        first_page = self.documents.add_image(target_id, first, page['name'] + ' · 1', source)
        second_page = self.documents.add_image(target_id, second, page['name'] + ' · 2', source)
        return {'page_ids': [first_page['id'], second_page['id']], 'original_preserved': True}

    def contact_sheet(self, document_id, values):
        document = self.documents.document(document_id)
        require(bool(document['pages']), '文档没有页面')
        columns = number(values.get('columns', 3), '联系表列数', 1, 10, True)
        cell_width = number(values.get('cell_width', 320), '单元格宽度', 120, 1000, True)
        cell_height = number(values.get('cell_height', 460), '单元格高度', 120, 1400, True)
        rows = (len(document['pages']) + columns - 1) // columns
        width = columns * cell_width
        height = rows * cell_height
        require(width * height <= 36_000_000 and max(width, height) <= 16000, '联系表过大，请减少页面或单元格尺寸')
        result = np.full((height, width, 3), 245, np.uint8)
        for index, page in enumerate(document['pages']):
            image = read_image(self.store.resolve(page['current']))
            ratio = min((cell_width - 24) / image.shape[1], (cell_height - 44) / image.shape[0])
            resized = cv2.resize(image, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_AREA)
            row, column = divmod(index, columns)
            x = column * cell_width + (cell_width - resized.shape[1]) // 2
            y = row * cell_height + 12
            result[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
            cv2.putText(result, str(index + 1), (column * cell_width + 12, (row + 1) * cell_height - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (80, 100, 80), 1, cv2.LINE_AA)
        target_id = values.get('target_id', document_id)
        return self.documents.add_image(target_id, result, document['name'] + ' · 联系表', {'type': 'generated', 'kind': 'contact_sheet', 'document_id': document_id})

    def concatenate(self, page_ids, target_id, values):
        require(isinstance(page_ids, list) and 2 <= len(page_ids) <= 20, '拼接需要2–20页')
        require(len(set(page_ids)) == len(page_ids), '拼接页面重复')
        orientation = choice(values.get('orientation', 'vertical'), '拼接方向', ('vertical', 'horizontal'))
        gap = number(values.get('gap', 20), '页面间距', 0, 200, True)
        images = []
        for page_id in page_ids:
            page = self.store.get('pages', page_id)
            images.append(read_image(self.store.resolve(page['current'])))
        if orientation == 'vertical':
            width = max(image.shape[1] for image in images)
            height = sum(image.shape[0] for image in images) + gap * (len(images) - 1)
        else:
            width = sum(image.shape[1] for image in images) + gap * (len(images) - 1)
            height = max(image.shape[0] for image in images)
        require(width * height <= 36_000_000 and max(width, height) <= 16000, '拼接结果过大')
        result = np.full((height, width, 3), 255, np.uint8)
        cursor = 0
        for image in images:
            if orientation == 'vertical':
                x = (width - image.shape[1]) // 2
                result[cursor:cursor + image.shape[0], x:x + image.shape[1]] = image
                cursor += image.shape[0] + gap
            else:
                y = (height - image.shape[0]) // 2
                result[y:y + image.shape[0], cursor:cursor + image.shape[1]] = image
                cursor += image.shape[1] + gap
        name = string(values.get('name', '拼接页面'), '页面名称', 200)
        return self.documents.add_image(target_id, result, name, {'type': 'generated', 'kind': 'concatenate', 'page_ids': page_ids})
