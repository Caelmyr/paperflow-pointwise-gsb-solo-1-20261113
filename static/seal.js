'use strict';
/* 印章 / 批注分离工作区前端。
   原则：自动只给建议，重叠处保持待确认；人工决定优先；换参数重算保留人工选择；
   预览与导出共用后端同一条合成链路，保证边缘一致。 */
(function () {
  const seal = {
    projects: null, project: null, detail: null,
    tool: 'keep', mode: 'rect', background: 'checker', view: 'composed', pageId: null,
    params: { preset: 'red', hue_center: 0, hue_span: 18, saturation_min: 45, value_min: 70, overlap_gap: 28, min_component: 12 },
    drawing: false, start: null, imgEl: null,
  };
  const api = window.api; // 复用 app.js 的请求封装

  function esc(v) { return String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
  function $(id) { return document.getElementById(id); }
  function activeVersion() { return seal.detail.versions.find(v => v.id === seal.detail.active_version_id); }

  async function open(pageId) {
    seal.pageId = pageId;
    $('modalTitle').textContent = '印章 / 批注分离工作区';
    $('modalContent').innerHTML = `
      <div class="seal-shell">
        <div class="seal-left" id="sealLeft"></div>
        <div class="seal-stage">
          <div class="seal-toolbar">
            <div class="seg" id="sealBg">
              <button data-bg="checker" class="active">棋盘</button>
              <button data-bg="white">白底</button>
              <button data-bg="dark">深底</button>
              <button data-bg="paper">纸底</button>
            </div>
            <div class="seg" id="sealView">
              <button data-view="composed" class="active">透明预览</button>
              <button data-view="original">原图对照</button>
            </div>
            <span class="seal-hint" style="margin:0">预览边缘即导出边缘</span>
          </div>
          <div class="seal-canvas-wrap"><canvas id="sealCanvas"></canvas></div>
        </div>
        <div class="seal-right" id="sealRight"></div>
      </div>`;
    if (!$('modal').open) $('modal').showModal();
    $('modal').classList.add('seal-dialog');
    $('modal').addEventListener('close', function resetClass() {
      $('modal').removeEventListener('close', resetClass);
      $('modal').classList.remove('seal-dialog');
      seal.project = null; seal.detail = null;
    });
    bindStatic();
    await loadProjects();
  }

  function bindStatic() {
    $('sealBg').onclick = e => {
      const btn = e.target.closest('button'); if (!btn) return;
      seal.background = btn.dataset.bg;
      $('sealBg').querySelectorAll('button').forEach(b => b.classList.toggle('active', b === btn));
      drawCanvas();
    };
    $('sealView').onclick = e => {
      const btn = e.target.closest('button'); if (!btn) return;
      seal.view = btn.dataset.view;
      $('sealView').querySelectorAll('button').forEach(b => b.classList.toggle('active', b === btn));
      drawCanvas();
    };
  }

  async function loadProjects() {
    seal.projects = await api('/seal/projects?page_id=' + seal.pageId);
    if (seal.project && seal.project.id) {
      seal.project = seal.projects.find(p => p.id === seal.project.id) || null;
    }
    if (seal.project) { await openProject(seal.project.id); return; }
    renderLeftNoProject();
    $('sealRight').innerHTML = '<p class="seal-hint">先在左侧创建工作区，系统将基于当前页面提出可保留的红色区域。</p>';
    const canvas = $('sealCanvas');
    canvas.width = canvas.height = 0;
  }

  function renderLeftNoProject() {
    $('sealLeft').innerHTML = `
      <div class="seal-section"><h3><span>01</span> 建立分离工作区</h3>
        <p class="seal-hint">只读当前页面图像，不会改动正文、原稿或其它素材。</p>
        <label>工作区名称</label><input id="sealName" value="印章分离 · ${new Date().toLocaleString('zh-CN')}">
        <button class="primary seal-btn" id="sealCreate">创建并自动提出可保留部分</button>
      </div>
      ${seal.projects.length ? `<div class="seal-section"><h3><span>·</span> 本页已有工作区</h3>${seal.projects.map(p =>
        `<div class="seal-project-row" data-open="${p.id}"><b>${esc(p.name)}</b><small>${p.version_count} 个版本 · ${p.has_pending_review ? '含待确认' : '无待确认'}</small></div>`).join('')}</div>` : ''}`;
    $('sealCreate').onclick = () => window.busyWrap('正在创建工作区…', async () => {
      const rec = await api('/seal/projects', 'POST', { page_id: seal.pageId, name: $('sealName').value });
      seal.project = rec;
      await extract('auto');
    });
    $('sealLeft').querySelectorAll('[data-open]').forEach(el => el.onclick = () => openProject(el.dataset.open));
  }

  async function openProject(id) {
    seal.detail = await api('/seal/projects/' + id);
    seal.project = seal.detail;
    seal.background = seal.detail.background;
    document.querySelectorAll('#sealBg button').forEach(b => b.classList.toggle('active', b.dataset.bg === seal.background));
    renderLeft(); renderRight(); await drawCanvas();
  }

  function paramInputs() {
    return `
      <div class="seal-section"><h3><span>02</span> 自动提取参数</h3>
        <label>颜色预设<select id="pPreset">
          <option value="red">朱红印章</option><option value="vermilion">橘红批注</option>
          <option value="blue">蓝色批注</option><option value="black">深色墨迹</option></select></label>
        <label>色相容差 <output id="oHue">${seal.params.hue_span}</output><input type="range" id="pHue" min="3" max="60" value="${seal.params.hue_span}"></label>
        <label>最低饱和度 <output id="oSat">${seal.params.saturation_min}</output><input type="range" id="pSat" min="0" max="255" value="${seal.params.saturation_min}"></label>
        <label>印泥纳入下限（越浅越纳入） <output id="oVal">${seal.params.value_min}</output><input type="range" id="pVal" min="0" max="255" value="${seal.params.value_min}"></label>
        <label>重叠判定间隔 <output id="oGap">${seal.params.overlap_gap}</output><input type="range" id="pGap" min="0" max="120" value="${seal.params.overlap_gap}"></label>
        <label>最小连通面积<input type="number" id="pComp" min="0" max="5000" value="${seal.params.min_component}"></label>
        <button class="seal-btn" id="sealExtract">用当前参数重新提出（新版本）</button>
        <p class="seal-hint">换参数会生成新版本并保留你此前的人工决定；可随时切回旧版本。</p>
      </div>`;
  }

  function syncParamsFromInputs() {
    // 首次创建时参数面板尚未渲染，使用 seal.params 中的默认值
    if (!$('pPreset')) return { ...seal.params };
    seal.params.preset = $('pPreset').value;
    seal.params.hue_span = Number($('pHue').value);
    seal.params.saturation_min = Number($('pSat').value);
    seal.params.value_min = Number($('pVal').value);
    seal.params.overlap_gap = Number($('pGap').value);
    seal.params.min_component = Number($('pComp').value);
    // 预设联动色相中心
    const centers = { red: 0, vermilion: 6, blue: 110, black: 0 };
    seal.params.hue_center = centers[seal.params.preset];
    return { ...seal.params };
  }

  function bindParamEvents() {
    [['pHue', 'oHue'], ['pSat', 'oSat'], ['pVal', 'oVal'], ['pGap', 'oGap']].forEach(([a, b]) => {
      $(a).oninput = () => { $(b).textContent = $(a).value; };
    });
    $('pPreset').value = seal.params.preset;
    $('sealExtract').onclick = () => window.busyWrap('正在重新提取…', () => extract('reextract'));
  }

  function renderLeft() {
    const d = seal.detail;
    $('sealLeft').innerHTML = `
      <div class="seal-section"><h3><span>·</span> ${esc(d.name)}</h3>
        <p class="seal-hint">来源：第 ${d.page_number} 页 · ${esc(d.page_name)}<br>当前版本 # ${d.active_version_id.slice(-6)}</p>
        <button class="seal-btn subtle" id="sealNewProject">＋ 为本页另建工作区</button>
      </div>
      ${paramInputs()}
      <div class="seal-section"><h3><span>03</span> 版本（可退回）</h3><div id="sealVersions"></div></div>`;
    bindParamEvents();
    $('sealNewProject').onclick = () => { seal.project = null; seal.detail = null; loadProjects(); };
    renderVersions();
  }

  function renderVersions() {
    const d = seal.detail;
    $('sealVersions').innerHTML = d.versions.map(v => `
      <div class="seal-version ${v.id === d.active_version_id ? 'active' : ''}" data-version="${v.id}">
        <span class="vtag">${v.origin === 'auto' ? '自动初提' : '换参数重算'}</span>
        <small>${new Date(v.created_at).toLocaleTimeString('zh-CN')} · 人工标记 ${v.manual_edit_count}</small>
        <small>保留 ${v.stats.keep} · 待确认 ${v.stats.review} · 排除 ${v.stats.exclude}</small>
      </div>`).join('');
    $('sealVersions').querySelectorAll('[data-version]').forEach(el =>
      el.onclick = () => window.busyWrap('切换版本…', async () => {
        await api('/seal/projects/' + d.id + '/activate', 'POST', { version_id: el.dataset.version });
        await openProject(d.id);
      }));
  }

  function renderRight() {
    const d = seal.detail; const v = activeVersion();
    $('sealRight').innerHTML = `
      ${v.stats.review > 0 ? `<div class="seal-pending">⚠ 有 <b>${v.stats.review}</b> 个像素处于待确认（红章与黑字重叠、照片无法判定归属）。系统不会替你补内容；请用下方工具人工保留或排除。</div>`
        : '<div class="seal-pending" style="background:#eef4ea;border-color:#bcd2b0;color:#4f7040">✓ 当前没有待确认像素。</div>'}
      <div class="seal-section"><h3><span>04</span> 人工补选 / 排除（优先于自动）</h3>
        <div class="seal-tools" id="sealTools">
          <button data-tool="keep" class="active">⊕ 保留</button>
          <button data-tool="review">？ 待确认</button>
          <button data-tool="exclude">⊖ 排除</button>
        </div>
        <div class="seal-tools" style="margin-top:6px">
          <button data-mode="rect" class="active">框选矩形</button>
          <button data-mode="points">画笔涂抹</button>
        </div>
        <p class="seal-brushtip" id="brushTip">按住拖动涂抹；松开即提交。像素级决定会覆盖自动结果。</p>
        <div class="seal-legend">
          <span><i class="seal-keep-dot"></i>保留：纳入透明图</span>
          <span><i class="seal-review-dot"></i>待确认：默认不导出</span>
          <span><i class="seal-exclude-dot"></i>排除：透明（含黑字、纸底）</span>
        </div>
      </div>
      <div class="seal-section"><h3><span>05</span> 当前版本统计</h3>
        <div class="seal-stat"><span><i class="seal-keep-dot"></i>保留</span><b>${v.stats.keep}</b></div>
        <div class="seal-stat"><span><i class="seal-review-dot"></i>待确认</span><b>${v.stats.review}</b></div>
        <div class="seal-stat"><span><i class="seal-exclude-dot"></i>排除</span><b>${v.stats.exclude}</b></div>
      </div>
      <div class="seal-section"><h3><span>06</span> 导出透明 PNG</h3>
        <label><input type="checkbox" id="expCrop" checked> 裁剪到内容边缘</label>
        <label><input type="checkbox" id="expReview"> 同时导出待确认像素（默认不导出）</label>
        <button class="primary seal-btn" id="sealExport">导出与预览一致的透明图</button>
        <p class="seal-hint">导出记录会保存来源页、页码、页面版本、提取参数与 SHA256 依据。</p>
        <div id="sealExports" style="margin-top:10px"></div>
      </div>`;
    $('sealTools').querySelectorAll('[data-tool]').forEach(b => b.onclick = () => {
      seal.tool = b.dataset.tool;
      $('sealTools').querySelectorAll('[data-tool]').forEach(x => x.classList.toggle('active', x === b));
    });
    document.querySelectorAll('[data-mode]').forEach(b => b.onclick = () => {
      seal.mode = b.dataset.mode;
      document.querySelectorAll('[data-mode]').forEach(x => x.classList.toggle('active', x === b));
      $('brushTip').classList.toggle('show', seal.mode === 'points');
    });
    $('sealExport').onclick = () => window.busyWrap('正在导出透明图…', async () => {
      const rec = await api('/seal/projects/' + d.id + '/export', 'POST', {
        crop: $('expCrop').checked, include_review: $('expReview').checked,
      });
      const a = document.createElement('a');
      a.href = '/api/seal/exports/' + rec.id + '/download'; a.download = rec.filename; a.click();
      if (rec.unresolved_review_at_export > 0 && !$('expReview').checked)
        window.toastMsg(`已导出，${rec.unresolved_review_at_export} 个待确认像素按规则未纳入`, false);
      await openProject(d.id);
    });
    bindCanvas();
    renderExports();
  }

  function renderExports() {
    const d = seal.detail;
    $('sealExports').innerHTML = d.exports.slice().reverse().map(e => `
      <div class="seal-export"><b>${esc(e.filename)}</b>
        <small>第 ${e.page_number} 页 · ${e.width}×${e.height} · ${new Date(e.created_at).toLocaleString('zh-CN')}</small>
        <small>待确认未纳入 ${e.unresolved_review_at_export} · 版本 ${e.version_id.slice(-6)}</small>
        <a href="/api/seal/exports/${e.id}/download" download>下载</a></div>`).join('');
  }

  async function extract(origin) {
    const params = syncParamsFromInputs();
    seal.detail = await api('/seal/projects/' + seal.project.id + '/extract', 'POST', {
      origin, params,
    });
    seal.project = seal.detail;
    renderLeft(); renderRight(); await drawCanvas();
    window.toastMsg(origin === 'auto' ? '已提出可保留部分，重叠处已标记为待确认' : '已用新参数生成版本，人工决定已保留', false);
  }

  function loadImage(src) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img); img.onerror = reject; img.src = src;
    });
  }

  async function drawCanvas() {
    if (!seal.detail) return;
    const d = seal.detail; const v = activeVersion();
    const canvas = $('sealCanvas'); const ctx = canvas.getContext('2d');
    const rev = encodeURIComponent(v.id + d.updated_at);
    let baseSrc;
    if (seal.view === 'original') {
      baseSrc = `/api/pages/${d.page_id}/image?variant=current`;
    } else {
      baseSrc = `/api/seal/projects/${d.id}/composed?background=${seal.background}&r=${rev}`;
    }
    const img = await loadImage(baseSrc);
    seal.imgEl = img;
    const maxW = Math.min(900, img.naturalWidth);
    const scale = maxW / img.naturalWidth;
    canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
    canvas.style.width = img.naturalWidth * scale + 'px';
    canvas.style.height = img.naturalHeight * scale + 'px';
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    // 叠加状态着色（透明预览下仍提示待确认/保留边界）
    const mask = await loadImage(`/api/seal/projects/${d.id}/versions/${v.id}/asset/mask?r=${rev}`);
    const mc = document.createElement('canvas'); mc.width = canvas.width; mc.height = canvas.height;
    mc.getContext('2d').drawImage(mask, 0, 0);
    const data = mc.getContext('2d').getImageData(0, 0, mc.width, mc.height).data;
    const overlay = ctx.createImageData(canvas.width, canvas.height);
    for (let i = 0; i < data.length; i += 4) {
      const g = data[i];
      if (g === 255) { overlay.data[i] = 192; overlay.data[i + 1] = 57; overlay.data[i + 2] = 43; overlay.data[i + 3] = 70; }
      else if (g === 128) { overlay.data[i] = 210; overlay.data[i + 1] = 165; overlay.data[i + 2] = 42; overlay.data[i + 3] = 110; }
    }
    ctx.putImageData(overlay, 0, 0);
  }

  function eventPoint(e) {
    const canvas = $('sealCanvas');
    const rect = canvas.getBoundingClientRect();
    const x = Math.round((e.clientX - rect.left) / rect.width * canvas.width);
    const y = Math.round((e.clientY - rect.top) / rect.height * canvas.height);
    // 约束在图像范围内，避免拖出画布造成后端越界
    return [Math.min(canvas.width - 1, Math.max(0, x)), Math.min(canvas.height - 1, Math.max(0, y))];
  }

  function bindCanvas() {
    const canvas = $('sealCanvas');
    let points = [];
    canvas.onmousedown = e => {
      if (!seal.detail) return;
      seal.drawing = true; seal.start = eventPoint(e); points = [seal.start];
    };
    canvas.onmousemove = e => {
      if (!seal.drawing || seal.mode !== 'points') return;
      points.push(eventPoint(e));
    };
    canvas.onmouseup = async e => {
      if (!seal.drawing) return;
      seal.drawing = false;
      const end = eventPoint(e);
      let geometry;
      if (seal.mode === 'rect') {
        const x = Math.min(seal.start[0], end[0]), y = Math.min(seal.start[1], end[1]);
        const w = Math.min(canvas.width - x, Math.abs(end[0] - seal.start[0]) || 1);
        const h = Math.min(canvas.height - y, Math.abs(end[1] - seal.start[1]) || 1);
        geometry = { type: 'rect', rect: [x, y, w, h] };
      } else {
        geometry = { type: 'points', points: points.length ? points : [end] };
      }
      await window.busyWrap('正在保存人工决定…', async () => {
        seal.detail = await api('/seal/projects/' + seal.detail.id + '/edit', 'POST', { target: seal.tool, geometry });
        renderLeft(); renderRight(); await drawCanvas();
      });
    };
  }

  // 暴露给 app.js
  window.SealWorkbench = { open };
})();
