import ast
import io
import json
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def count(path):
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    excluded = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.body and isinstance(node.body[0], ast.Expr):
                expression = node.body[0]
                if isinstance(expression.value, ast.Constant) and isinstance(expression.value.value, str):
                    excluded.update(range(expression.lineno, expression.end_lineno + 1))
    meaningful = set()
    ignored = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER, tokenize.ENCODING}
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in ignored and token.string.strip():
            for line in range(token.start[0], token.end[0] + 1):
                if line not in excluded and source.splitlines()[line - 1].strip():
                    meaningful.add(line)
    return len(meaningful)


def report():
    files = sorted((ROOT / 'paperflow').rglob('*.py')) + [ROOT / 'run.py']
    results = [{'file': path.relative_to(ROOT).as_posix(), 'effective_lines': count(path)} for path in files]
    result = {'method': 'Python tokenize 非空非注释物理行；AST 排除模块、类、函数文档字符串；仅后端自有源码，排除脚本、测试、前端、第三方、虚拟环境', 'files': results, 'total': sum(item['effective_lines'] for item in results)}
    result['passes_5000'] = result['total'] > 5000
    return result


if __name__ == '__main__':
    result = report()
    (ROOT / 'docs' / 'line-count.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\n'.join(f'{item["file"]}: {item["effective_lines"]}' for item in result['files']))
    print('TOTAL:', result['total'], 'PASS >5000:', result['passes_5000'])
