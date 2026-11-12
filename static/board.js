'use strict';
/* 多票据工作台：一张桌面照片（票据板）上的候选轮廓、人工拆开/合并、独立校正与出处对应。 */
const boardState = {board: null, selected: null, mode: 'select', checked: new Set(), drag: null, manualPoints: null};
const candidateStatus = {candidate: '待确认', confirmed: '已确认', blocked: '无法处理', extracted: '已校正'};
const statusColor = {candidate: '#b98241', confirmed: '#3d865b', blocked: '#9b5440', extracted: '#34638a'};

function boardCanvasPoint(event) {
  const rect = $('boardOverlay').getBoundingClientRect();
  return [(event.clientX - rect.left) / rect.width * $('boardOverlay').width,
          (event.clientY - rect.top) / rect.height * $('boardOverlay').height];
}

function currentCandidate() {
  if (!boardState.board || !boardState.selected) return null;
  return boardState.board.candidates.find(item => item.id === boardState.selected) || null;
}

async function openBoardFromPage(page) {
  const boards = await api('/boards?document_id=' + page.document_id);
  let board = boards.find(item => item.page_id === page.id);
  if (!board) {
    board = await api('/pages/' + page.id + '/boards', 'POST', {name: page.name + ' · 票据板'});
    board = await api('/boards/' + board.id + '/detect', 'POST', {replace: true});
  } else {
    board = await api('/boards/' + board.id);
  }
  await showBoard(board.id);
}

async function showBoard(boardId) {
  boardState.board = await api('/boards/' + boardId);
  boardState.selected = boardState.board.candidates[0]?.id || null;
  boardState.checked = new Set(boardState.board.candidates.filter(c => c.status !== 'extracted').map(c => c.id).slice(0, 0));
  $('workbench').classList.add('hidden');
  $('boardWorkbench').classList.remove('hidden');
  $('boardImage').src = '/api/boards/' + boardState.board.id + '/image?r=' + boardState.board.revision;
  $('boardTitle').textContent = boardState.board.name;
  renderBoardTargetDocs();
  renderBoardCandidates();
  renderBoardDetail();
  drawBoardOverlay();
  window.scrollTo({top: 0, behavior: 'smooth'});
}

function closeBoard() {
  $('boardWorkbench').classList.add('hidden');
  $('workbench').classList.remove('hidden');
  boardState.board = null;
}

function renderBoardTargetDocs() {
  const current = $('boardTargetDoc').value;
  $('boardTargetDoc').innerHTML = state.workspace.documents
    .map(d => `<option value="${d.id}" ${d.id === boardState.board.document_id ? 'selected' : ''}>${escapeHtml(d.name)}</option>`).join('');
  if (current) $('boardTargetDoc').value = current;
}

function renderBoardCandidates() {
  const board = boardState.board;
  const counts = board.candidates.reduce((acc, item) => (acc[item.status] = (acc[item.status] || 0) + 1, acc), {});
  $('boardStats').textContent = `${board.candidates.length} 张候选 · 已校正 ${counts.extracted || 0} · 无法处理 ${counts.blocked || 0}`;
  const root = $('boardCandidates');
  root.innerHTML = board.candidates.length ? '' : '<p class="hint">还没有候选，点击「自动寻找」或「手描一张」。</p>';
  board.candidates.forEach((item, index) => {
    const card = document.createElement('div');
    card.className = 'board-candidate' + (boardState.selected === item.id ? ' active' : '');
    card.style.borderLeftColor = statusColor[item.status];
    const result = item.result;
    card.innerHTML = `
      <label class="board-check"><input type="checkbox" data-check="${item.id}" ${boardState.checked.has(item.id) ? 'checked' : ''} ${item.status === 'extracted' ? 'disabled' : ''}></label>
      <div class="board-candidate-body" data-open="${item.id}">
        <b>${index + 1}. ${escapeHtml(item.name)}</b>
        <small>${candidateStatus[item.status]}${item.confidence != null ? ' · 置信度 ' + item.confidence : ''}${item.origin === 'auto' ? ' · 自动' : ' · 手动'}</small>
        ${item.status === 'blocked' ? `<small class="block-reason">原因：${escapeHtml(item.block_reason || '')}</small>` : ''}
        ${result ? `<small class="board-result" data-page="${result.page_id}">↗ 已生成页面：${escapeHtml(result.document_name)}${result.ocr ? '' : '（未识别）'}${result.ocr_failure ? ' · OCR 失败：' + escapeHtml(result.ocr_failure) : ''}</small>` : ''}
      </div>`;
    card.querySelector('[data-open]').onclick = () => {boardState.selected = item.id; renderBoardCandidates(); renderBoardDetail(); drawBoardOverlay();};
    const resultLink = card.querySelector('[data-page]');
    if (resultLink) resultLink.onclick = event => {event.stopPropagation(); openResultPage(result.page_id);};
    const checkbox = card.querySelector('[data-check]');
    checkbox.onclick = event => event.stopPropagation();
    checkbox.onchange = () => {checkbox.checked ? boardState.checked.add(item.id) : boardState.checked.delete(item.id);};
    root.append(card);
  });
}

function openResultPage(pageId) {
  busy('正在回到原照片并定位结果页面…', async () => {
    const provenance = await api('/pages/' + pageId + '/board-provenance');
    closeBoard();
    await openDocument(provenance.board.document_id, pageId);
    toast('该页面来自票据板「' + provenance.board.name + '」候选「' + provenance.candidate.name + '」，出处已在页面信息中保留');
  });
}

function renderBoardDetail() {
  const item = currentCandidate();
  const root = $('boardDetail');
  if (!item) {root.innerHTML = '<p class="hint">在左侧选择一个候选。</p>'; return;}
  const editable = item.status !== 'extracted';
  root.innerHTML = `
    <label>候选名称<input id="boardName" value="${escapeHtml(item.name)}" maxlength="160"></label>
    <p class="hint">状态：${candidateStatus[item.status]}${item.visible_ratio != null ? ' · 可见比例 ' + item.visible_ratio : ''}</p>
    <div class="row">
      <button data-act="confirm" ${item.status === 'confirmed' || item.status === 'extracted' ? 'disabled' : ''}>确认范围</button>
      <button data-act="reopen" ${item.status === 'blocked' || item.status === 'confirmed' ? '' : 'disabled'}>重新打开</button>
    </div>
    <button class="full subtle" data-act="block" ${editable ? '' : 'disabled'}>标记无法处理（保留位置与原因）</button>
    <button class="full danger-text" data-act="remove" ${editable ? '' : 'disabled'}>移除此候选</button>
    <label>备注<textarea id="boardNote" rows="3" placeholder="遮挡、描边依据等人工说明">${escapeHtml(item.note || '')}</textarea></label>`;
  $('boardName').onchange = () => boardAction(() => api('/boards/' + boardState.board.id + '/candidates/' + item.id, 'PATCH', {revision: boardState.board.revision, name: $('boardName').value}));
  $('boardNote').onchange = () => boardAction(() => api('/boards/' + boardState.board.id + '/candidates/' + item.id, 'PATCH', {revision: boardState.board.revision, note: $('boardNote').value}));
  root.querySelector('[data-act="confirm"]').onclick = () => boardAction(() => api('/boards/' + boardState.board.id + '/candidates/' + item.id + '/confirm', 'POST', {}));
  root.querySelector('[data-act="reopen"]').onclick = () => boardAction(() => api('/boards/' + boardState.board.id + '/candidates/' + item.id + '/reopen', 'POST', {}));
  root.querySelector('[data-act="block"]').onclick = () => blockCandidate(item.id);
  root.querySelector('[data-act="remove"]').onclick = () => removeCandidate(item.id);
  $('boardExtract').disabled = item.status === 'blocked' || item.status === 'extracted';
  $('boardReextract').disabled = item.status !== 'extracted';
  $('boardSplit').disabled = !editable;
}

async function boardAction(action, keepSelection) {
  try {
    const selected = boardState.selected;
    boardState.board = await action();
    if (!keepSelection && !boardState.board.candidates.some(c => c.id === selected)) boardState.selected = null;
    renderBoardCandidates(); renderBoardDetail(); drawBoardOverlay();
  } catch (error) {toast(error.message, true);}
}

function blockCandidate(candidateId) {
  form('标记无法处理的票据', '<p class="hint">该候选在板上的位置和原因会保留，其他票据仍可独立完成。</p><label>原因</label><input name="reason" required maxlength="300" placeholder="如：关键角点被遮挡，无法可靠校正">', async values => {
    await boardAction(() => api('/boards/' + boardState.board.id + '/candidates/' + candidateId + '/block', 'POST', values));
    toast('已保留待处理位置与原因');
  });
}

function removeCandidate(candidateId) {
  modal('移除候选', '<p class="help-text">仅移除候选轮廓记录；已校正生成的页面需要先在票据板中重新校正。确认移除？</p><button class="danger-text full" id="confirmRemove">确认移除</button>');
  $('confirmRemove').onclick = () => busy('正在移除…', async () => {
    await boardAction(() => api('/boards/' + boardState.board.id + '/candidates/' + candidateId, 'DELETE'));
    $('modal').close();
  });
}

function drawBoardOverlay() {
  const canvas = $('boardOverlay');
  const image = $('boardImage');
  if (!image.naturalWidth || !boardState.board) return;
  canvas.width = image.naturalWidth;
  canvas.height = image.naturalHeight;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const ratio = canvas.width / Math.max(canvas.clientWidth, 1);
  boardState.board.candidates.forEach((item, index) => {
    const selected = item.id === boardState.selected;
    ctx.beginPath();
    item.points.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
    ctx.closePath();
    ctx.fillStyle = statusColor[item.status] + (selected ? '2e' : '14');
    ctx.strokeStyle = statusColor[item.status];
    ctx.lineWidth = (selected ? 3.5 : 1.8) * ratio;
    if (boardState.checked.has(item.id)) ctx.setLineDash([8 * ratio, 5 * ratio]);
    ctx.fill(); ctx.stroke(); ctx.setLineDash([]);
    const anchor = item.points[0];
    ctx.font = `${13 * ratio}px sans-serif`;
    ctx.fillStyle = statusColor[item.status];
    ctx.fillText(`${index + 1} · ${candidateStatus[item.status]}`, anchor[0] + 6 * ratio, anchor[1] - 8 * ratio);
    if (selected && boardState.mode === 'corners') {
      item.points.forEach((p, i) => {
        ctx.beginPath(); ctx.arc(p[0], p[1], 8 * ratio, 0, Math.PI * 2);
        ctx.fillStyle = '#fff'; ctx.fill(); ctx.strokeStyle = statusColor[item.status]; ctx.lineWidth = 2 * ratio; ctx.stroke();
        ctx.fillStyle = '#26352d'; ctx.fillText(String(i + 1), p[0] + 10 * ratio, p[1] - 8 * ratio);
      });
    }
  });
  if (boardState.manualPoints) {
    ctx.strokeStyle = '#b98241'; ctx.fillStyle = '#b9824122'; ctx.lineWidth = 2 * ratio;
    ctx.beginPath();
    boardState.manualPoints.forEach((p, i) => i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]));
    if (boardState.manualPoints.length === 4) ctx.closePath();
    ctx.fill(); ctx.stroke();
    boardState.manualPoints.forEach(p => {ctx.beginPath(); ctx.arc(p[0], p[1], 7 * ratio, 0, Math.PI * 2); ctx.fillStyle = '#b98241'; ctx.fill();});
  }
}

$('boardImage').onload = () => drawBoardOverlay();
new ResizeObserver(() => drawBoardOverlay()).observe($('boardStage'));

$('boardOverlay').onpointerdown = event => {
  if (!boardState.board) return;
  const point = boardCanvasPoint(event);
  const item = currentCandidate();
  if (boardState.mode === 'corners' && item && item.status !== 'extracted') {
    const tolerance = 22 * $('boardOverlay').width / Math.max(1, $('boardOverlay').clientWidth);
    const index = item.points.findIndex(p => Math.hypot(p[0] - point[0], p[1] - point[1]) < tolerance);
    if (index >= 0) {boardState.drag = {kind: 'corner', index}; event.target.setPointerCapture(event.pointerId); return;}
  }
  if (boardState.mode === 'manual') {
    boardState.manualPoints = boardState.manualPoints || [];
    if (boardState.manualPoints.length < 4) {
      boardState.manualPoints.push([Math.round(point[0]), Math.round(point[1])]);
      drawBoardOverlay();
      if (boardState.manualPoints.length === 4) {
        const points = boardState.manualPoints;
        boardState.manualPoints = null;
        boardState.mode = 'select'; $('boardManual').classList.remove('active'); $('boardEditCorners').classList.remove('active');
        boardAction(async () => {
          const board = await api('/boards/' + boardState.board.id + '/candidates', 'POST', {name: '手描票据 ' + (boardState.board.candidates.length + 1), points});
          boardState.selected = board.candidates[board.candidates.length - 1].id;
          return board;
        });
      }
    }
    return;
  }
  const hit = [...boardState.board.candidates].reverse().find(candidate => {
    const x = point[0], y = point[1];
    let inside = false;
    for (let i = 0, j = candidate.points.length - 1; i < candidate.points.length; j = i++) {
      const xi = candidate.points[i][0], yi = candidate.points[i][1], xj = candidate.points[j][0], yj = candidate.points[j][1];
      if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  });
  if (hit) {boardState.selected = hit.id; renderBoardCandidates(); renderBoardDetail(); drawBoardOverlay();}
};

$('boardOverlay').onpointermove = event => {
  if (!boardState.drag || boardState.drag.kind !== 'corner') return;
  const item = currentCandidate();
  const [x, y] = boardCanvasPoint(event).map((v, i) => Math.max(0, Math.min(v, i ? $('boardOverlay').height - 1 : $('boardOverlay').width - 1)));
  item.points[boardState.drag.index] = [Math.round(x), Math.round(y)];
  drawBoardOverlay();
};

$('boardOverlay').onpointerup = async () => {
  if (!boardState.drag) return;
  const item = currentCandidate();
  const points = item.points.map(p => [...p]);
  boardState.drag = null;
  await boardAction(() => api('/boards/' + boardState.board.id + '/candidates/' + item.id, 'PATCH', {revision: boardState.board.revision, points}), true);
};

$('boardEntry').onclick = () => {if (!state.page) return toast('请先选择桌面照片页面', true); busy('正在建立票据板并寻找轮廓…', async () => openBoardFromPage(state.page));};
$('loadBoardDemo').onclick = () => busy('正在载入多票据练习照片…', async () => {
  form('多票据练习', '<label>文档名称</label><input name="name" value="桌面多票据练习" required><label>所属集合</label><select name="collection_id">' + collectionOptions(state.collection) + '</select>', async values => {
    const doc = await api('/documents', 'POST', values);
    const result = await api('/demo', 'POST', {document_id: doc.id, sample: 'board'});
    await refresh(); await openDocument(doc.id);
    await showBoard(result.board.id);
    toast('已自动找到 ' + result.board.candidates.length + ' 个票据候选，请逐个确认、校正与识别');
  });
});
$('boardBack').onclick = () => closeBoard();
$('boardDetect').onclick = () => busy('正在自动寻找票据轮廓…', async () => {
  await boardAction(() => api('/boards/' + boardState.board.id + '/detect', 'POST', {replace: false}));
  const n = boardState.board.candidates.length;
  toast(n ? '发现 ' + n + ' 个票据候选，请逐个确认范围' : '未发现可靠轮廓，请手描票据边界');
});
$('boardManual').onclick = () => {
  boardState.mode = boardState.mode === 'manual' ? 'select' : 'manual';
  boardState.manualPoints = boardState.mode === 'manual' ? [] : null;
  $('boardManual').classList.toggle('active', boardState.mode === 'manual');
  $('boardEditCorners').classList.remove('active');
  toast(boardState.mode === 'manual' ? '依次点击票据的四个角点' : '已退出手描');
  drawBoardOverlay();
};
$('boardEditCorners').onclick = () => {
  const item = currentCandidate();
  if (!item) return toast('请先选择候选', true);
  if (item.status === 'extracted') return toast('已校正票据的轮廓已固定，可用「重新校正」生成新页面', true);
  boardState.mode = boardState.mode === 'corners' ? 'select' : 'corners';
  $('boardEditCorners').classList.toggle('active', boardState.mode === 'corners');
  $('boardManual').classList.remove('active');
  drawBoardOverlay();
};
$('boardSplit').onclick = () => {
  const item = currentCandidate();
  if (!item) return toast('请先选择要拆开的候选', true);
  form('拆开票据候选', '<p class="hint">把当前候选按两条新轮廓拆开，原候选将被替换；新轮廓仍受重叠校验保护。</p><label>轮廓一：四角 x1,y1;x2,y2;x3,y3;x4,y4</label><input name="p1" required placeholder="如 100,100;400,120;380,500;90,480"><label>轮廓二：四角</label><input name="p2" required>', async values => {
    const parse = text => text.trim().split(';').map(pair => pair.split(',').map(Number));
    await boardAction(() => api('/boards/' + boardState.board.id + '/candidates/' + item.id + '/split', 'POST', {candidates: [parse(values.p1), parse(values.p2)]}));
    toast('候选已拆开，请分别确认');
  });
};
$('boardMerge').onclick = () => {
  const ids = [...boardState.checked];
  if (ids.length < 2) return toast('请先在左侧勾选至少两个候选', true);
  form('合并票据候选', '<p class="hint">将勾选的 ' + ids.length + ' 个候选按外轮廓合并为一张票据。</p><label>合并后名称</label><input name="name" value="合并票据" required maxlength="160">', async values => {
    await boardAction(() => api('/boards/' + boardState.board.id + '/merge', 'POST', {candidate_ids: ids, name: values.name}));
    boardState.checked.clear();
    toast('候选已合并');
  });
};
$('boardExtract').onclick = () => {
  const item = currentCandidate();
  if (!item) return toast('请先选择候选', true);
  busy('正在透视校正并识别票据…', async () => {
    const payload = {revision: boardState.board.revision, document_id: $('boardTargetDoc').value, ocr: $('boardOcr').checked, language: $('ocrLanguage').value, psm: Number($('ocrPsm').value)};
    boardState.board = await api('/boards/' + boardState.board.id + '/candidates/' + item.id + '/extract', 'POST', payload);
    const updated = boardState.board.candidates.find(c => c.id === item.id);
    renderBoardCandidates(); renderBoardDetail(); drawBoardOverlay();
    if (updated.status === 'blocked') toast('该票据无法处理，位置与原因已保留：' + (updated.block_reason || ''), true);
    else toast('票据已校正生成独立页面' + (updated.result.ocr ? '并完成识别' : updated.result.ocr_failure ? '，OCR 失败：' + updated.result.ocr_failure : ''));
  });
};
$('boardReextract').onclick = () => {
  const item = currentCandidate();
  if (!item) return;
  modal('重新校正票据', '<p class="help-text">将基于当前轮廓重新透视校正并生成一个新页面；旧页面与其人工校订保留，其他票据不受影响。</p><button class="primary full" id="confirmReextract">生成新的校正页面</button>');
  $('confirmReextract').onclick = () => busy('正在重新校正…', async () => {
    boardState.board = await api('/boards/' + boardState.board.id + '/candidates/' + item.id + '/reextract', 'POST', {document_id: $('boardTargetDoc').value, ocr: $('boardOcr').checked, language: $('ocrLanguage').value, psm: Number($('ocrPsm').value)});
    $('modal').close(); renderBoardCandidates(); renderBoardDetail(); drawBoardOverlay();
    toast('已生成新的校正页面，出处仍指向同一张原照片');
  });
};
$('boardDelete').onclick = () => {
  modal('删除票据板', '<p class="help-text">票据板仅保存候选与出处关系；已有校正结果时需先处理对应页面。原始照片页面不会被删除。</p><button class="danger-text full" id="confirmBoardDelete">确认删除票据板</button>');
  $('confirmBoardDelete').onclick = () => busy('正在删除票据板…', async () => {
    try {
      await api('/boards/' + boardState.board.id, 'DELETE');
      $('modal').close(); closeBoard(); toast('票据板已删除，原照片页面保留');
    } catch (error) {toast(error.message, true);}
  });
};
