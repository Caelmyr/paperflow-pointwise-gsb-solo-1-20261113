"""非破坏性处理链（recipe）。

同一次扫描调参的依据被表达为一条有序、可回看的处理链：每个节点只记录
操作与参数，最终图像始终从原图按链重算，而不是在已经丢失信息的中间稿上
继续叠加。修改任一节点会重算其下游；不适用于新输入的下游节点会被标记，
由调用方决定保留还是重算，绝不静默丢弃或假装恢复已丢失的信息。
"""
from __future__ import annotations

import copy

from . import imaging
from .core import DomainError, identity, now, require
from .parameters import REGISTRY, validate_operation

# 操作在处理链中的阶段与信息属性。
# destructive=True 表示输出会丢失颜色或文字细节，无法由其输出反推输入。
# color=True 表示该操作需要彩色输入才有意义（在灰度/黑白稿上没有适用依据）。
OPERATION_INFO = {
    'perspective': {'stage': 'geometry', 'label': '透视校正', 'destructive': False, 'color': False},
    'rotate': {'stage': 'geometry', 'label': '旋转', 'destructive': False, 'color': False},
    'deskew': {'stage': 'geometry', 'label': '倾斜校正', 'destructive': False, 'color': False},
    'crop': {'stage': 'geometry', 'label': '区域裁切', 'destructive': False, 'color': False},
    'flip': {'stage': 'geometry', 'label': '镜像翻转', 'destructive': False, 'color': False},
    'pad': {'stage': 'geometry', 'label': '扩展白边', 'destructive': False, 'color': False},
    'resize': {'stage': 'geometry', 'label': '缩放', 'destructive': False, 'color': False},
    'remove_border': {'stage': 'geometry', 'label': '清除边框', 'destructive': False, 'color': False},
    'illumination': {'stage': 'background', 'label': '光照修复', 'destructive': False, 'color': False},
    'flatten_background': {'stage': 'background', 'label': '背景归一化', 'destructive': True, 'color': False},
    'enhance': {'stage': 'background', 'label': '扫描增强', 'destructive': True, 'color': False},
    'color': {'stage': 'color', 'label': '扫描色彩', 'destructive': True, 'color': False},
    'contrast': {'stage': 'color', 'label': '亮度对比度', 'destructive': False, 'color': True},
    'clahe': {'stage': 'color', 'label': '局部对比增强', 'destructive': False, 'color': False},
    'white_balance': {'stage': 'color', 'label': '白平衡', 'destructive': False, 'color': True},
    'gamma': {'stage': 'color', 'label': '伽马校正', 'destructive': False, 'color': False},
    'stretch': {'stage': 'color', 'label': '直方图拉伸', 'destructive': False, 'color': False},
    'invert': {'stage': 'color', 'label': '反色', 'destructive': False, 'color': False},
    'sauvola': {'stage': 'color', 'label': 'Sauvola 二值化', 'destructive': True, 'color': False},
    'morph_text': {'stage': 'color', 'label': '文字形态优化', 'destructive': True, 'color': False},
    'denoise': {'stage': 'cleanup', 'label': '清除污点', 'destructive': False, 'color': False},
    'bilateral': {'stage': 'cleanup', 'label': '保边降噪', 'destructive': False, 'color': False},
    'sharpen': {'stage': 'cleanup', 'label': '文字锐化', 'destructive': False, 'color': False},
    'erase': {'stage': 'cleanup', 'label': '区域擦除', 'destructive': False, 'color': False},
    'remove_lines': {'stage': 'cleanup', 'label': '清除表格线', 'destructive': True, 'color': False},
}

# color 节点的参数 mode 决定它是否真正丢弃颜色。
COLOR_LOSS_MODES = {'gray', 'binary', 'adaptive'}
STAGE_ORDER = ['geometry', 'background', 'color', 'cleanup']
MAX_NODES = 20


def operation_label(operation):
    info = OPERATION_INFO.get(operation)
    if info:
        return info['label']
    return REGISTRY.get(operation).describe()['name'] if operation in REGISTRY else operation


def loses_color(operation, params):
    """该节点在给定参数下是否丢弃彩色信息。"""
    if operation == 'color':
        return params.get('mode', 'gray') in COLOR_LOSS_MODES
    return bool(OPERATION_INFO.get(operation, {}).get('destructive'))


def node_status(node):
    return node.get('status', 'active') if isinstance(node, dict) else 'active'


def _active(nodes):
    return [node for node in nodes if node_status(node) == 'active']


def validate_chain(nodes):
    require(isinstance(nodes, list) and len(nodes) <= MAX_NODES, '处理链最多包含 %d 个节点' % MAX_NODES)
    normalized = []
    for node in nodes:
        require(isinstance(node, dict), '处理链节点必须为对象')
        operation = node['operation']
        require(operation in OPERATION_INFO, '操作不能进入处理链：' + str(operation))
        params = validate_operation(operation, node.get('params', {}))
        record = {
            'id': node.get('id') if isinstance(node.get('id'), str) and node['id'] else identity('node'),
            'operation': operation,
            'params': params,
            'created_at': node.get('created_at') or now(),
            'status': node.get('status', 'active'),
            'note': node.get('note', ''),
        }
        require(record['status'] in ('active', 'inapplicable'), '处理链节点状态无效')
        normalized.append(record)
    return normalized


def render_chain(original, nodes, upto_index=None, collect=False, safe=False):
    """从原图按链顺序重算图像。

    返回最终图像；collect=True 时额外返回每个节点输出（含原图，索引偏移1）
    与每个节点的处理元数据。inapplicable 节点直接跳过。

    safe=True 时，某个节点因输入变化而不再适用（例如几何变换后选区越界）
    不会抛异常中断整条链，而是把该节点标记为运行期不适用并跳过，错误收集在
    errors 中，由调用方交给用户选择保留或重算。
    """
    image = original
    outputs = [original]
    metadatas = [{}]
    errors = []
    active_count = 0
    for index, node in enumerate(nodes):
        if upto_index is not None and index > upto_index:
            break
        if node_status(node) == 'inapplicable':
            outputs.append(image)
            metadatas.append({})
            continue
        try:
            active_count += 1
            image, metadata = imaging.process(image, node['operation'], node['params'])
            imaging.validate_image(image)
        except DomainError as error:
            if not safe:
                raise
            errors.append({
                'index': index,
                'operation': node['operation'],
                'code': 'runtime_inapplicable',
                'message': '输入已变化，%s 不再适用：%s。节点已保留但未参与本次重算，请调整参数或移除。'
                           % (operation_label(node['operation']), error.message),
            })
            node['status'] = 'inapplicable'
            metadata = {}
        outputs.append(image)
        metadatas.append(metadata)
    if collect:
        return image, outputs, metadatas, active_count, errors
    return image


def evaluate_applicability(nodes):
    """重算每个节点对当前输入是否仍有明确适用依据。

    依据完全来自链上已确认的节点：一旦上游出现信息丢失（灰度/二值化等），
    需要彩色输入的节点就不再适用；依据不明确时只标记、不静默处理。
    检查时先依据“进入该节点之前”的状态判定，再更新丢失标记，保证紧挨在
    丢色节点之后的需彩色节点也能被准确识别。
    """
    color_available = True
    notes = []
    for index, node in enumerate(nodes):
        if node_status(node) == 'inapplicable':
            notes.append({'index': index, 'code': 'kept_inapplicable', 'message': '节点已被标记为不适用并保留，等待人工选择。'})
            # 不适用节点不参与重算，但其原本会造成的信息丢失仍然成立：
            # 若它是丢色节点，下游的彩色依据同样不存在。
            if loses_color(node['operation'], node['params']):
                color_available = False
            continue
        info = OPERATION_INFO[node['operation']]
        if info['color'] and not color_available:
            notes.append({
                'index': index,
                'operation': node['operation'],
                'code': 'color_unavailable',
                'message': '上游色彩模式已转为灰度或黑白，彩色信息已丢失；%s 需要彩色输入，重算也无法恢复颜色。' % info['label'],
            })
        if loses_color(node['operation'], node['params']):
            color_available = False
    return color_available, notes


def public_chain(page):
    """对外暴露的处理链视图（不暴露内部文件路径以外的状态变化）。"""
    nodes = page.get('recipe', [])
    color_available, notes = evaluate_applicability(nodes)
    # 最近一次重算中因输入变化而无法执行的节点，同样作为适用性差异呈现。
    for error in page.get('render', {}).get('runtime_errors', []):
        if not any(item['index'] == error['index'] for item in notes):
            notes.append(error)
    chain_revision = page.get('chain_revision', 0)
    rendered = page.get('rendered_revision') == chain_revision and bool(page.get('current'))
    items = []
    for index, node in enumerate(nodes):
        info = OPERATION_INFO[node['operation']]
        items.append({
            'id': node['id'],
            'index': index,
            'operation': node['operation'],
            'label': info['label'],
            'stage': info['stage'],
            'params': copy.deepcopy(node['params']),
            'status': node_status(node),
            'loses_color': loses_color(node['operation'], node['params']),
            'needs_color': bool(info['color']),
            'created_at': node.get('created_at'),
            'note': node.get('note', ''),
        })
    return {
        'nodes': items,
        'color_available': color_available,
        'applicability': notes,
        'chain_revision': chain_revision,
        'rendered': rendered,
        'node_count': len(nodes),
        'active_count': len(_active(nodes)),
    }


def empty_chain_record():
    return {'recipe': [], 'chain_revision': 0, 'rendered_revision': 0, 'chain_basis_revision': 0}
