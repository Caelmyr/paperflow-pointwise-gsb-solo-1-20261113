from __future__ import annotations

import difflib
import re

CJK_RE = re.compile(r'[㐀-䶿一-鿿豈-﫿]')
TOKEN_RE = re.compile(r'[㐀-䶿一-鿿豈-﫿]|[A-Za-z0-9]+(?:[.\-/][A-Za-z0-9]+)*|\S')


def tokenize(text):
    """中文逐字、英文/数字按词切分，标点单独成 token。"""
    if not text:
        return []
    return TOKEN_RE.findall(text)


def normalize_text(text):
    if not text:
        return ''
    return re.sub(r'\s+', ' ', text).strip()


def text_similarity(first_text, second_text):
    """0–1 的 token 级相似度；任一侧无文字时返回 None 表示无法用文字判断。"""
    first_tokens = tokenize(normalize_text(first_text))
    second_tokens = tokenize(normalize_text(second_text))
    if not first_tokens or not second_tokens:
        return None
    sequence = difflib.SequenceMatcher(a=first_tokens, b=second_tokens, autojunk=False)
    return round(sequence.ratio(), 5)


def line_diff(first_text, second_text):
    """行级 LCS 差异，返回新增/删除/相同行及 token 级增删数量。"""
    # OCR 文本多以换行分隔段落，保留换行结构；无换行时退化为空格分词
    first_lines = [line.strip() for line in (first_text or '').splitlines() if line.strip()]
    second_lines = [line.strip() for line in (second_text or '').splitlines() if line.strip()]
    if not first_lines and not second_lines:
        first_lines = [token for token in normalize_text(first_text).split(' ') if token]
        second_lines = [token for token in normalize_text(second_text).split(' ') if token]
    matcher = difflib.SequenceMatcher(a=first_lines, b=second_lines, autojunk=False)
    rows = []
    added_tokens = 0
    removed_tokens = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for line in first_lines[i1:i2]:
                rows.append({'type': 'equal', 'left': line, 'right': line})
        elif tag == 'delete':
            for line in first_lines[i1:i2]:
                removed_tokens += len(tokenize(line))
                rows.append({'type': 'removed', 'left': line, 'right': None})
        elif tag == 'insert':
            for line in second_lines[j1:j2]:
                added_tokens += len(tokenize(line))
                rows.append({'type': 'added', 'left': None, 'right': line})
        else:
            for line in first_lines[i1:i2]:
                removed_tokens += len(tokenize(line))
                rows.append({'type': 'removed', 'left': line, 'right': None})
            for line in second_lines[j1:j2]:
                added_tokens += len(tokenize(line))
                rows.append({'type': 'added', 'left': None, 'right': line})
    return {
        'rows': rows,
        'added_tokens': added_tokens,
        'removed_tokens': removed_tokens,
        'change_ratio': round((added_tokens + removed_tokens) / (
            max(1, len(tokenize(normalize_text(first_text))) +
                len(tokenize(normalize_text(second_text))))
        ), 5),
        'token_change_total': added_tokens + removed_tokens,
    }


def word_level_diff(first_text, second_text, limit=400):
    """token 级增删清单，供界面逐条展示。"""
    first_tokens = tokenize(normalize_text(first_text))
    second_tokens = tokenize(normalize_text(second_text))
    matcher = difflib.SequenceMatcher(a=first_tokens, b=second_tokens, autojunk=False)
    items = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            continue
        if tag in ('delete', 'replace'):
            items.append({'type': 'removed', 'text': ''.join(first_tokens[i1:i2])})
        if tag in ('insert', 'replace'):
            items.append({'type': 'added', 'text': ''.join(second_tokens[j1:j2])})
    return items[:limit]


def compare_text(first_text, second_text):
    """汇总文字层面的全部信号。"""
    similarity = text_similarity(first_text, second_text)
    lines = line_diff(first_text, second_text)
    return {
        'available': similarity is not None,
        'similarity': similarity,
        'added_tokens': lines['added_tokens'],
        'removed_tokens': lines['removed_tokens'],
        'change_ratio': lines['change_ratio'],
        'rows': lines['rows'][:300],
        'word_changes': word_level_diff(first_text, second_text),
        'left_length': len(tokenize(normalize_text(first_text))),
        'right_length': len(tokenize(normalize_text(second_text))),
    }


def has_cjk(text):
    return bool(CJK_RE.search(text or ''))
