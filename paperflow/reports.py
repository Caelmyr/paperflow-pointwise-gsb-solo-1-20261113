from __future__ import annotations

import csv
import html
import io
import json
from collections import Counter

from . import measurement
from .core import identity, now, require
from .exports import safe_name
from .layout import extract_fields


class Reports:
    def __init__(self, store, auditor, review):
        self.store = store
        self.auditor = auditor
        self.review = review

    def document(self, document_id):
        state = self.store.snapshot()
        document = state['documents'].get(document_id)
        require(document is not None, '文档不存在', 404)
        pages = []
        totals = Counter()
        for index, page_id in enumerate(document['page_ids']):
            page = state['pages'][page_id]
            result = page.get('ocr')
            operations = Counter(entry['operation'] for entry in page['history'])
            issues = self.review.issues(page)
            totals['pages'] += 1
            totals['processed_pages'] += bool(page['history'])
            totals['ocr_pages'] += bool(result)
            totals['edited_pages'] += bool(result and result.get('edited'))
            totals['stale_pages'] += bool(result and result.get('stale'))
            totals['regions'] += len(page.get('regions', []))
            totals['warnings'] += sum(issue['severity'] == 'warning' for issue in issues)
            totals['errors'] += sum(issue['severity'] == 'error' for issue in issues)
            pages.append({
                'number': index + 1,
                'id': page_id,
                'name': page['name'],
                'width': page['width'],
                'height': page['height'],
                'revision': page['revision'],
                'sha256': page['sha256'],
                'source': page.get('source'),
                'operations': dict(operations),
                'history_count': len(page['history']),
                'ocr': {
                    'recognized': bool(result),
                    'confidence': result['confidence'] if result else None,
                    'words': len(result['boxes']) if result else 0,
                    'characters': len(result['text']) if result else 0,
                    'edited': bool(result and result.get('edited')),
                    'stale': bool(result and result.get('stale')),
                },
                'quality': page.get('quality'),
                'review': page.get('review'),
                'measurement': measurement.summary(page.get('measurement')),
                'issues': issues,
                'fields': extract_fields(result),
            })
        return {
            'generated_at': now(),
            'workspace_revision': state['revision'],
            'document': document,
            'totals': dict(totals),
            'pages': pages,
            'text_statistics': self.review.text_statistics(document_id),
        }

    def html(self, report):
        escape = lambda value: html.escape(str(value))
        document = report['document']
        title = escape(document['name'])
        rows = []
        for page in report['pages']:
            ocr = page['ocr']
            confidence = str(ocr['confidence']) + '%' if ocr['recognized'] else '未识别'
            issues = '<br>'.join(escape(issue['message']) for issue in page['issues']) or '无'
            basis = page['measurement']
            if basis['status'] == 'confirmed':
                physical = (f"{basis['physical_width_mm']} × {basis['physical_height_mm']} mm"
                            f"<br><small>{escape(basis['basis'])} · {basis['source_dpi']} DPI</small>")
            else:
                physical = '<small>缺少可信依据<br>待人工确认</small>'
            rows.append(
                '<tr>'
                + f'<td>{page["number"]}</td>'
                + f'<td>{escape(page["name"])}</td>'
                + f'<td>{page["width"]} × {page["height"]}</td>'
                + f'<td>{physical}</td>'
                + f'<td>{page["history_count"]}</td>'
                + f'<td>{confidence}</td>'
                + f'<td>{ocr["characters"]}</td>'
                + f'<td>{issues}</td>'
                + '</tr>'
            )
        totals = report['totals']
        metrics = [
            ('总页数', totals.get('pages', 0)),
            ('已处理', totals.get('processed_pages', 0)),
            ('已识别', totals.get('ocr_pages', 0)),
            ('已校订', totals.get('edited_pages', 0)),
            ('待更新 OCR', totals.get('stale_pages', 0)),
            ('错误', totals.get('errors', 0)),
        ]
        cards = ''.join(f'<div><b>{value}</b><span>{label}</span></div>' for label, value in metrics)
        css = 'body{font:14px/1.8 system-ui,sans-serif;color:#253b30;background:#f7f8f2;margin:40px}h1{font-size:26px}small{color:#83917c}.metrics{display:flex;gap:20px;margin:25px 0}.metrics div{background:white;padding:16px 24px;border:1px solid #e0e6d8;border-radius:8px}.metrics b{display:block;font-size:26px}.metrics span{font-size:12px;color:#829276}table{width:100%;border-collapse:collapse;background:white}td,th{padding:12px;border:1px solid #e0e6d8;text-align:left}th{background:#edf2e7}.footer{font-size:11px;margin-top:30px;color:#8c9b80}@media print{body{margin:10px}.metrics{gap:10px}}'
        output = (
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            + f'<title>{title} · 数字化报告</title><style>{css}</style></head><body>'
            + f'<small>PAPERFLOW / DIGITIZATION REPORT</small><h1>{title}</h1>'
            + f'<p>生成时间：{escape(report["generated_at"])} · 数据版本：{report["workspace_revision"]}</p>'
            + f'<section class="metrics">{cards}</section>'
            + '<table><thead><tr><th>页码</th><th>页面名称</th><th>像素尺寸</th><th>物理尺寸与依据</th><th>处理次数</th><th>OCR 置信度</th><th>字符数</th><th>待检查项</th></tr></thead><tbody>'
            + ''.join(rows)
            + '</tbody></table><p class="footer">PaperFlow 本地数字化报告。质量指标用于辅助人工检查，原始文件始终保留。</p></body></html>'
        )
        return output.encode('utf-8')

    def csv(self, report):
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['页码', '页面', '宽', '高', '版本', '处理次数', '已识别', 'OCR置信度', '字符数', '已校订', '需更新', '物理宽mm', '物理高mm', '尺寸依据', '依据密度DPI', '原图SHA256'])
        for page in report['pages']:
            name = page['name']
            if name.startswith(('=', '+', '-', '@')):
                name = "'" + name
            basis = page['measurement']
            writer.writerow([
                page['number'], name, page['width'], page['height'],
                page['revision'], page['history_count'], page['ocr']['recognized'],
                page['ocr']['confidence'], page['ocr']['characters'],
                page['ocr']['edited'], page['ocr']['stale'],
                basis.get('physical_width_mm'), basis.get('physical_height_mm'),
                basis.get('basis') or 'unconfirmed', basis.get('source_dpi'), page['sha256'],
            ])
        return output.getvalue().encode('utf-8-sig')

    def export(self, document_id, kind='html'):
        require(kind in ('html', 'json', 'csv'), '报告格式无效')
        report = self.document(document_id)
        if kind == 'html':
            content = self.html(report)
            mime = 'text/html'
        elif kind == 'csv':
            content = self.csv(report)
            mime = 'text/csv'
        else:
            content = json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8')
            mime = 'application/json'
        path = self.store.new_file('exports', '.' + kind)
        path.write_bytes(content)
        record = {
            'id': identity('export'),
            'document_id': document_id,
            'kind': 'report_' + kind,
            'path': self.store.relative(path),
            'filename': safe_name(report['document']['name']) + '_数字化报告.' + kind,
            'mime': mime,
            'bytes': len(content),
            'page_ids': [page['id'] for page in report['pages']],
            'created_at': now(),
        }
        with self.store.transaction() as state:
            state.setdefault('exports', {})[record['id']] = record
            self.store.event(state, '导出数字化报告', document_id, {'kind': kind})
        return record

    def workspace(self):
        state = self.store.snapshot()
        collections = []
        for collection in state['collections'].values():
            documents = [document for document in state['documents'].values() if document['collection_id'] == collection['id']]
            collections.append({
                'id': collection['id'],
                'name': collection['name'],
                'category': collection['category'],
                'documents': len(documents),
                'pages': sum(len(document['page_ids']) for document in documents),
            })
        return {
            'generated_at': now(),
            'summary': self.auditor.summary(),
            'storage': self.auditor.storage(),
            'collections': collections,
            'review_queue': self.review.queue(),
        }
