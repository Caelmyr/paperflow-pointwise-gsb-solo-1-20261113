'use strict';
/* 对折小册子拼版工作台：选项变更即向后端重新规划，本地不保留任何旧方案状态。 */

const BOOKLET_PAPERS = [
  { id: 'a4', label: 'A4 横向（297 × 210 mm）' },
  { id: 'a3', label: 'A3 横向（420 × 297 mm）' },
  { id: 'letter', label: 'Letter 横向（11 × 8.5 in）' },
  { id: 'tabloid', label: 'Tabloid 横向（17 × 11 in）' },
];

let bookletState = { plan: null, documentId: null, tab: 'sheets', busy: false, serial: 0 };

function bookletEscape(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function bookletOptionForm() {
  const value = id => document.getElementById(id).value;
  const checked = id => document.getElementById(id).checked;
  const options = {
    binding: value('bkBinding'),
    flip: value('bkFlip'),
    signature_mode: value('bkSignature'),
    paper: value('bkPaper'),
    orientation: value('bkOrientation'),
    cover: value('bkCover'),
    padding: value('bkPadding'),
    margin_mm: Number(value('bkMargin')),
    gap_mm: Number(value('bkGap')),
    fold_marks: checked('bkFoldMarks'),
    searchable: checked('bkSearchable'),
  };
  if (options.signature_mode === 'split') {
    options.sheets_per_signature = Number(value('bkSheetsPerSignature'));
  }
  return options;
}

async function bookletRefresh(initial) {
  if (!bookletState.documentId) return;
  const serial = ++bookletState.serial;
  bookletState.busy = true;
  const badge = $('bkPlanBadge');
  if (badge) badge.textContent = '正在重新核算拼版…';
  try {
    const plan = await api('/documents/' + bookletState.documentId + '/booklet/plan', 'POST',
      { options: bookletOptionForm() });
    if (serial !== bookletState.serial) return; // 已有更新的一次规划，丢弃过期响应
    bookletState.plan = plan;
    bookletState.busy = false;
    bookletRenderSummary(plan);
    bookletRenderSheets(plan);
    bookletRenderReading(plan);
    if (badge) badge.textContent = '规划已更新 · 校验值 ' + plan.checksum.slice(0, 10);
  } catch (error) {
    if (serial === bookletState.serial) {
      bookletState.busy = false;
      if (badge) badge.textContent = '规划失败：' + error.message;
      toast(error.message, true);
    }
  }
}

function bookletRenderSummary(plan) {
  const sigText = plan.signatures.map(s =>
    `第 ${s.index} 册：${s.sheet_count} 张纸 / ${s.leaf_count} 面，含 ${s.blank_count} 张空白`).join('；');
  $('bkSummary').innerHTML = `
    <div class="bk-stat"><b>${plan.content_page_count}</b><span>正文页</span></div>
    <div class="bk-stat"><b>${plan.blank_page_count}</b><span>补白/封面空白</span></div>
    <div class="bk-stat"><b>${plan.signature_count}</b><span>分册数</span></div>
    <div class="bk-stat"><b>${plan.sheet_count}</b><span>纸张（正反 ${plan.pdf_page_count} 面）</span></div>
    <p class="bk-sig-detail">${bookletEscape(sigText)}</p>
    <p class="bk-checksum">规划校验值：<code>${bookletEscape(plan.checksum)}</code></p>
    ${plan.warnings.length ? `<ul class="bk-warnings">${plan.warnings
      .map(w => `<li>⚠ ${bookletEscape(w)}</li>`).join('')}</ul>` : ''}`;
}

function bookletPanelCard(panel, plan) {
  const isBlank = panel.kind === 'blank';
  const thumb = !isBlank && panel.thumbnail_url
    ? `<img src="${panel.thumbnail_url}&r=${state.workspace.revision}" alt="第${panel.logical_number ?? ''}页缩略图">`
    : '<div class="bk-blank-thumb">空白</div>';
  const label = isBlank
    ? bookletEscape({ cover: '空白封面', pad_front: '补白 · 帖首', pad_end: '补白 · 帖尾' }[panel.reason] || '空白页')
    : `第 ${panel.logical_number} 页`;
  const rot = panel.rotation ? `<span class="bk-rot">旋转 ${panel.rotation}°</span>` : '';
  return `<div class="bk-panel bk-slot-${panel.slot}">
    <div class="bk-panel-head"><span>${label}</span>${rot}</div>
    <div class="bk-panel-thumb bk-rot-${panel.rotation}">${thumb}</div>
    <small>${bookletEscape(panel.name || '')} · ${panel.slot === 'left' ? '左面板' : '右面板'}</small>
  </div>`;
}

function bookletRenderSheets(plan) {
  // 把正、反两面按纸张配对展示，同一张纸的两面相邻。
  const byPaper = new Map();
  for (const side of plan.sheets) {
    if (!byPaper.has(side.paper_index)) byPaper.set(side.paper_index, {});
    byPaper.get(side.paper_index)[side.side] = side;
  }
  const flipNote = plan.options.flip === 'long'
    ? '长边翻面：背面已整体旋转 180°，按长边翻转纸张后方向正确'
    : '短边翻面：背面面板正向，按短边翻转纸张后方向正确';
  $('bkSheets').innerHTML = [...byPaper.entries()].map(([index, pair]) => `
    <div class="bk-sheet">
      <h4>第 ${index} 张纸 · 第 ${pair.front.signature} 册
        <small>${bookletEscape(flipNote)}</small></h4>
      <div class="bk-sheet-sides">
        <div class="bk-side">
          <div class="bk-side-title">正面（先打印的一面）</div>
          <div class="bk-panels">${pair.front.panels.map(p => bookletPanelCard(p, plan)).join('')}</div>
        </div>
        <div class="bk-side">
          <div class="bk-side-title">背面（翻面后打印）${plan.options.flip === 'long' ? ' · 180°' : ''}</div>
          <div class="bk-panels ${plan.options.flip === 'long' ? 'bk-side-180' : ''}">
            ${pair.back.panels.map(p => bookletPanelCard(p, plan)).join('')}
          </div>
        </div>
      </div>
    </div>`).join('');
}

function bookletRenderReading(plan) {
  $('bkReading').innerHTML = plan.reading_order.map(item => `
    <div class="bk-read-card" title="第 ${item.sheet_in_signature} 张纸 · ${item.side === 'front' ? '正面' : '背面'} · ${item.slot === 'left' ? '左' : '右'}面板">
      <b>${item.logical_number}</b>
      <img src="/api/pages/${item.page_id}/image?variant=thumbnail&r=${state.workspace.revision}" alt="">
      <small>${bookletEscape(item.name)}</small>
      ${item.rotation ? '<em>↻ 90°</em>' : ''}
    </div>`).join('');
  const blanks = plan.sheets.flatMap(s => s.panels).filter(p => p.kind === 'blank');
  $('bkReadingBlanks').innerHTML = blanks.length
    ? `<h4>补白与封面的明确去处（${blanks.length}）</h4>` + blanks.map(p =>
      `<p class="bk-blank-line">第 ${p.sheet_in_signature} 张纸 · ${p.side === 'front' ? '正面' : '背面'} · ${p.slot === 'left' ? '左' : '右'}面板 → ${bookletEscape({ cover: '空白封面', pad_front: '帖首补白', pad_end: '帖尾补白' }[p.reason] || '空白页')}</p>`).join('')
    : '<p class="hint">本方案无需补白。</p>';
}

function bookletSwitchTab(tab) {
  bookletState.tab = tab;
  document.querySelectorAll('.bk-tab').forEach(btn => btn.classList.toggle('active', btn.dataset.bkTab === tab));
  $('bkTabSheets').classList.toggle('hidden', tab !== 'sheets');
  $('bkTabReading').classList.toggle('hidden', tab !== 'reading');
}

async function bookletExport() {
  if (!bookletState.plan) return;
  await busy('正在生成拼版打印 PDF…', async () => {
    const result = await api('/documents/' + bookletState.documentId + '/booklet/export', 'POST',
      { options: bookletState.plan.options });
    const verification = await api('/booklet/' + result.record.id + '/verify');
    if (!verification.valid) throw new Error('打印文件校核失败：' + verification.errors.join('；'));
    const anchor = document.createElement('a');
    anchor.href = '/api/exports/' + result.record.id + '/download';
    anchor.download = result.record.filename;
    anchor.click();
    toast(`拼版打印文件已生成并通过校核（${result.record.booklet.sheet_count} 张纸，校验值 ${verification.checksum.slice(0, 10)}）`);
  });
}

function openBooklet(documentId) {
  bookletState = { plan: null, documentId, tab: 'sheets', busy: false, serial: 0 };
  const paperOptions = BOOKLET_PAPERS.map(p => `<option value="${p.id}">${bookletEscape(p.label)}</option>`).join('');
  modal('对折小册子拼版打印', `
  <div class="booklet-workbench">
    <form class="bk-options" id="bkOptionsForm">
      <label>装订方向
        <select id="bkBinding">
          <option value="left">左装订（西式左翻页）</option>
          <option value="right">右装订（右翻页）</option>
        </select>
      </label>
      <label>双面翻转方式
        <select id="bkFlip">
          <option value="short">短边翻面（横向纸推荐）</option>
          <option value="long">长边翻面（背面整体旋转180°）</option>
        </select>
      </label>
      <label>分册方式
        <select id="bkSignature">
          <option value="none">整本一册</option>
          <option value="split">按帖分册（每帖独立骑马钉）</option>
        </select>
      </label>
      <label class="bk-sheets-input hidden" id="bkSheetsWrap">每帖纸张数
        <input id="bkSheetsPerSignature" type="number" min="1" max="40" value="4">
      </label>
      <label>纸张尺寸<select id="bkPaper">${paperOptions}</select></label>
      <label>横竖页处理
        <select id="bkOrientation">
          <option value="auto">横向页自动旋转90°（推荐）</option>
          <option value="keep">保持原方向，仅等比缩放居中</option>
        </select>
      </label>
      <label>封面处理
        <select id="bkCover">
          <option value="none">首页即封面，不另加空白</option>
          <option value="blank">额外加一张空白封面页</option>
        </select>
      </label>
      <label>补白位置
        <select id="bkPadding">
          <option value="end">补白集中在正文末尾</option>
          <option value="both">封面侧与末尾分别补白</option>
        </select>
      </label>
      <label>安全边距 (mm)<input id="bkMargin" type="number" min="0" max="40" step="0.5" value="8"></label>
      <label>中缝间距 (mm)<input id="bkGap" type="number" min="0" max="30" step="0.5" value="0"></label>
      <label class="bk-check"><input type="checkbox" id="bkFoldMarks" checked> 显示折线与对位标记</label>
      <label class="bk-check"><input type="checkbox" id="bkSearchable" checked> 保留可检索文字层（随缩放旋转）</label>
    </form>
    <div class="bk-main">
      <div id="bkPlanBadge" class="bk-badge">正在核算拼版…</div>
      <div id="bkSummary" class="bk-summary"></div>
      <div class="bk-tabs">
        <button type="button" class="bk-tab active" data-bk-tab="sheets">纸张两面校核</button>
        <button type="button" class="bk-tab" data-bk-tab="reading">折好后的阅读顺序</button>
      </div>
      <div id="bkTabSheets" class="bk-tab-panel"><div id="bkSheets" class="bk-sheets"></div></div>
      <div id="bkTabReading" class="bk-tab-panel hidden">
        <div id="bkReading" class="bk-reading"></div>
        <div id="bkReadingBlanks"></div>
      </div>
      <div class="bk-actions">
        <p class="hint">源文档保持正常阅读顺序不变；每次调整选项都会从零重新核算，旧补白页与坐标不会混入。</p>
        <button type="button" class="primary" id="bkExportBtn">生成并校核打印 PDF</button>
      </div>
    </div>
  </div>`);
  // 选项变更：去抖后整体重算，杜绝局部沿用旧状态。
  let timer = null;
  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(() => bookletRefresh(), 180);
  };
  $('bkOptionsForm').addEventListener('input', event => {
    $('bkSheetsWrap').classList.toggle('hidden', $('bkSignature').value !== 'split');
    schedule();
  });
  document.querySelectorAll('.bk-tab').forEach(btn => {
    btn.onclick = () => bookletSwitchTab(btn.dataset.bkTab);
  });
  $('bkExportBtn').onclick = bookletExport;
  bookletRefresh(true);
}
