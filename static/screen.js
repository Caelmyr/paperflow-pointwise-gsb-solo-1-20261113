'use strict';
/* 印刷网纹分区治理：检测 → 分区复核 → 全分辨率预览 → 确认保存。
   依赖 app.js 提供的 api / toast / busy / state / openPage / selectPanel。 */
(function () {
  const $ = (id) => document.getElementById(id);
  const screenState = {
    review: null,
    preview: null,
    drawProtect: false,
    protectStart: null,
    overlayRegions: [],
  };

  const levelName = { high: '高置信网点', medium: '待确认（灰度）', uncertain: '依据不足·保留', none: '普通内容' };
  const levelClass = { high: 'lv-high', medium: 'lv-medium', uncertain: 'lv-uncertain', none: 'lv-none' };

  function ensurePage() {
    if (!state.page) throw Error('请先选择一个页面');
    return state.page;
  }

  async function refreshReview() {
    screenState.review = await api('/pages/' + state.page.id + '/screen/review');
    renderReview();
  }

  function renderReview() {
    const review = screenState.review;
    const summary = $('screenSummary');
    const regionsRoot = $('screenRegions');
    const protectedRoot = $('screenProtected');
    $('screenApply').disabled = true;
    screenState.preview = null;
    $('screenPreviewLinks').innerHTML = '';
    $('screenMetrics').textContent = '';
    if (!review || !review.available) {
      summary.textContent = '尚未检测。检测不会修改图像，只生成证据与区域建议。';
      regionsRoot.innerHTML = '';
      protectedRoot.innerHTML = '';
      screenState.overlayRegions = [];
      drawOverlay();
      return;
    }
    const g = review.grid_summary || {};
    summary.innerHTML = `图块统计：<b class="lv-high">高置信 ${g.high || 0}</b> · ` +
      `<b class="lv-medium">待确认 ${g.medium || 0}</b> · ` +
      `<b class="lv-uncertain">依据不足 ${g.uncertain || 0}</b> · 普通 ${g.none || 0}` +
      (review.confirmed ? '<br>已确认抑制，历史版本可回看。' : '');

    screenState.overlayRegions = review.regions.map((r) => ({
      id: r.id, rect: r.rect, level: r.confidence_level, decision: r.decision,
    }));
    regionsRoot.innerHTML = review.regions.length ? '' : '<p class="hint">未发现达到阈值的周期性网纹。</p>';
    review.regions.forEach((region) => {
      const card = document.createElement('div');
      card.className = 'screen-region ' + levelClass[region.confidence_level];
      card.innerHTML =
        `<div class="region-head"><b>${levelName[region.confidence_level] || region.confidence_level}</b>` +
        `<span>${region.decision === 'suppress' ? '将抑制' : '保留'}</span></div>` +
        `<small>间距 ${region.spacing}px · 角度 ${region.angle}° · ${region.tile_count} 图块</small>` +
        `<small class="region-reason">${escapeHtml(region.reason)}</small>` +
        `<div class="row region-actions">` +
        `<button data-act="locate">定位</button>` +
        `<button data-act="suppress" ${region.decision === 'suppress' ? 'class="active-btn"' : ''}>抑制</button>` +
        `<button data-act="keep" ${region.decision === 'keep' ? 'class="active-btn"' : ''}>保留</button>` +
        (screenState.preview ? `<a data-act="crop">1:1 局部对比</a>` : '') +
        `</div>`;
      card.querySelector('[data-act="locate"]').onclick = () => locateRegion(region.rect);
      card.querySelector('[data-act="suppress"]').onclick = () => setDecision(region.id, 'suppress');
      card.querySelector('[data-act="keep"]').onclick = () => setDecision(region.id, 'keep');
      regionsRoot.appendChild(card);
    });

    protectedRoot.innerHTML = review.manual_protected.length
      ? review.manual_protected.map((p) =>
          `<div class="protect-item"><small>▱ ${escapeHtml(p.label)} [${p.rect.join(', ')}]</small>` +
          `<button data-protect="${p.id}">移除</button></div>`).join('')
      : '<p class="hint">暂无手动保护区域。</p>';
    protectedRoot.querySelectorAll('[data-protect]').forEach((button) => {
      button.onclick = () => busy('正在移除保护区域…', async () => {
        screenState.review = await api('/pages/' + state.page.id + '/screen/protection/' + button.dataset.protect, 'DELETE');
        renderReview();
      });
    });
    drawOverlay();
  }

  function locateRegion(rect) {
    const [x, y, w, h] = rect;
    const stage = $('imageStage');
    const scroll = $('canvasScroll');
    // 缩放到区域可见，并在画布上高亮
    state.zoom = Math.min(2, Math.max(0.5, Math.min(stage.clientWidth / Math.max(w, 1), stage.clientHeight / Math.max(h, 1)) * 0.9));
    applyZoom();
    const scale = stage.clientWidth / Math.max(1, $('previewImage').naturalWidth);
    scroll.scrollTo({
      left: x * scale - scroll.clientWidth / 2 + (w * scale) / 2,
      top: y * scale - scroll.clientHeight / 2 + (h * scale) / 2,
      behavior: 'smooth',
    });
    screenState.flashRect = rect;
    drawOverlay();
    setTimeout(() => { screenState.flashRect = null; drawOverlay(); }, 2500);
  }

  async function setDecision(regionId, decision) {
    await busy('正在更新区域决策…', async () => {
      screenState.review = await api('/pages/' + state.page.id + '/screen/regions/' + regionId + '/decision', 'POST', { decision });
      invalidatePreview();
      renderReview();
      toast(decision === 'suppress' ? '该区域将参与抑制，请重新生成预览' : '该区域已设为保留，请重新生成预览');
    });
  }

  function invalidatePreview() {
    screenState.preview = null;
    $('screenApply').disabled = true;
    $('screenPreviewLinks').innerHTML = '';
    $('screenMetrics').textContent = '区域决策已变更，请重新生成全分辨率预览。';
  }

  async function runAnalyze() {
    ensurePage();
    await busy('正在分析局部频谱（原始分辨率）…', async () => {
      screenState.review = await api('/pages/' + state.page.id + '/screen/analyze', 'POST', {});
      renderReview();
      const g = screenState.review.grid_summary || {};
      toast(`检测完成：高置信 ${g.high || 0} 块，待确认 ${g.medium || 0} 块，依据不足 ${g.uncertain || 0} 块（默认保留）`);
    });
  }

  async function runPreview() {
    ensurePage();
    if (!screenState.review || !screenState.review.available) {
      toast('请先执行网纹检测', true);
      return;
    }
    const strength = Number($('screenStrength').value) / 100;
    await busy('正在按原始分辨率渲染抑制预览…', async () => {
      screenState.preview = await api('/pages/' + state.page.id + '/screen/preview', 'POST', { strength });
      $('screenApply').disabled = false;
      renderMetrics();
      renderPreviewLinks();
      // 在预览区直接显示全分辨率结果（浏览器缩放不改变原图数据）
      $('previewImage').src = `/api/pages/${state.page.id}/screen/preview/${screenState.preview.id}/image?r=${Date.now()}`;
      toast('全分辨率预览已生成，可放大到 100% 核对波纹与文字');
    });
  }

  function renderMetrics() {
    const p = screenState.preview;
    const m = p.metrics;
    const regionLines = p.region_metrics
      .filter((x) => x.decision === 'suppress')
      .map((x) => {
        const reduction = ((x.energy_before - x.energy_after) / Math.max(1e-6, x.energy_before) * 100).toFixed(1);
        return `区域波纹能量下降 ${reduction}% · 细节损失 ${x.detail_loss} · 平均变化 ${x.mean_change}`;
      });
    $('screenMetrics').innerHTML =
      `<div>抑制像素占比：${(m.suppressed_pixel_ratio * 100).toFixed(1)}%</div>` +
      `<div>整体细节损失指数：${m.detail_loss}（越低越安全）</div>` +
      regionLines.map((line) => `<div>· ${escapeHtml(line)}</div>`).join('');
  }

  function renderPreviewLinks() {
    const p = screenState.preview;
    const base = `/api/pages/${state.page.id}/screen/preview/${p.id}`;
    $('screenPreviewLinks').innerHTML =
      `<a href="${base}/image" target="_blank" rel="noopener">↗ 新窗口查看全分辨率结果</a>` +
      `<a href="${base}/image?kind=overlay" target="_blank" rel="noopener">↗ 查看区域依据叠加图</a>` +
      screenState.review.regions.map((r) =>
        `<a href="${base}/regions/${r.id}/crop?kind=before" target="_blank" rel="noopener">局部原图 · ${levelName[r.confidence_level]}</a>` +
        `<a href="${base}/regions/${r.id}/crop?kind=after" target="_blank" rel="noopener">局部结果 · ${levelName[r.confidence_level]}</a>`
      ).join('');
  }

  async function runApply() {
    ensurePage();
    if (!screenState.preview) { toast('请先生成并核对预览', true); return; }
    await busy('正在确认并保存（与预览逐位一致）…', async () => {
      const previewId = screenState.preview.id;
      await api('/pages/' + state.page.id + '/screen/apply', 'POST', { preview_id: previewId });
      screenState.preview = null;
      await reloadCurrent();
      await refreshReview();
      toast('网纹抑制已保存，历史版本与原图可回看');
    });
  }

  // 保护区域框选：复用画布指针事件
  function bindProtectionDrawing() {
    $('screenProtect').onclick = () => {
      ensurePage();
      screenState.drawProtect = !screenState.drawProtect;
      $('screenProtect').classList.toggle('active-btn', screenState.drawProtect);
      state.drawRegion = false;
      $('selectRegion').classList.remove('active');
      toast(screenState.drawProtect ? '请在预览图上拖动框选要保护的文字/织物区域' : '已退出保护框选');
    };
  }

  // 由 app.js 的画布指针逻辑在每帧后调用，绘制网纹区域与保护框
  window.renderScreenOverlay = function (ctx, ratio) {
    const color = { high: '#e08a2e', medium: '#e3c548', uncertain: '#5a8fd6', none: '#999' };
    screenState.overlayRegions.forEach((region) => {
      const [x, y, w, h] = region.rect;
      ctx.lineWidth = Math.max(1.5, ratio);
      ctx.strokeStyle = color[region.level] || '#999';
      ctx.fillStyle = (color[region.level] || '#999') + '22';
      ctx.setLineDash(region.decision === 'suppress' ? [] : [7 * ratio, 5 * ratio]);
      ctx.fillRect(x, y, w, h);
      ctx.strokeRect(x, y, w, h);
      ctx.setLineDash([]);
    });
    if (screenState.flashRect) {
      const [x, y, w, h] = screenState.flashRect;
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth = Math.max(2, 2.5 * ratio);
      ctx.strokeRect(x, y, w, h);
    }
    if (state.region && screenState.protectDraft) {
      ctx.strokeStyle = '#3d865b';
      ctx.fillStyle = '#3d865b22';
      ctx.lineWidth = 2 * ratio;
      ctx.setLineDash([6 * ratio, 4 * ratio]);
      ctx.fillRect(...state.region);
      ctx.strokeRect(...state.region);
      ctx.setLineDash([]);
    }
  };

  document.addEventListener('DOMContentLoaded', () => {
    $('screenAnalyze').onclick = () => runAnalyze().catch((e) => toast(e.message, true));
    $('screenPreview').onclick = () => runPreview().catch((e) => toast(e.message, true));
    $('screenApply').onclick = () => runApply().catch((e) => toast(e.message, true));
    $('screenStrength').oninput = () => { $('screenStrengthValue').textContent = $('screenStrength').value + '%'; };
    bindProtectionDrawing();

    // 打开页面时加载已有复核状态；切走面板或页面时还原主预览
    const originalOpenPage = window.openPage;
    window.openPage = async function (identifier) {
      const result = await originalOpenPage(identifier);
      screenState.preview = null;
      $('screenApply').disabled = true;
      $('screenPreviewLinks').innerHTML = '';
      $('screenMetrics').textContent = '';
      if (state.page) refreshReview().catch(() => {});
      return result;
    };

    // 保护区域框选：监听画布指针，落点转换为图像坐标
    const overlay = $('overlay');
    overlay.addEventListener('pointerdown', (event) => {
      if (!screenState.drawProtect || !state.page) return;
      const rect = overlay.getBoundingClientRect();
      screenState.protectStart = [
        (event.clientX - rect.left) / rect.width * overlay.width,
        (event.clientY - rect.top) / rect.height * overlay.height,
      ];
      screenState.protectDraft = true;
    });
    overlay.addEventListener('pointermove', (event) => {
      if (!screenState.protectDraft) return;
      const rect = overlay.getBoundingClientRect();
      const point = [
        (event.clientX - rect.left) / rect.width * overlay.width,
        (event.clientY - rect.top) / rect.height * overlay.height,
      ];
      const [sx, sy] = screenState.protectStart;
      state.region = [
        Math.round(Math.min(point[0], sx)), Math.round(Math.min(point[1], sy)),
        Math.round(Math.abs(point[0] - sx)), Math.round(Math.abs(point[1] - sy)),
      ];
      drawOverlay();
    });
    overlay.addEventListener('pointerup', async () => {
      if (!screenState.protectDraft || !state.region) return;
      const region = state.region;
      screenState.protectDraft = false;
      screenState.drawProtect = false;
      $('screenProtect').classList.remove('active-btn');
      state.region = null;
      drawOverlay();
      if (region[2] < 16 || region[3] < 16) { toast('保护区域至少 16×16 像素', true); return; }
      try {
        const label = await new Promise((resolve) => {
          const value = window.prompt('保护区域说明（如：织物纹理 / 小字）', '手动保护区域');
          resolve(value || '手动保护区域');
        });
        await busy('正在保存保护区域…', async () => {
          screenState.review = await api('/pages/' + state.page.id + '/screen/protection', 'POST', { rect: region, label });
          invalidatePreview();
          renderReview();
          toast('保护区域已保存，抑制时将完全跳过；请重新生成预览');
        });
      } catch (e) { toast(e.message, true); }
    });

    // 切回处理结果视图时若没有屏幕预览则恢复主图
    document.querySelectorAll('[data-view]').forEach((button) => {
      button.addEventListener('click', () => {
        if (screenState.preview && button.dataset.view !== 'current') {
          // 离开当前视图时清除屏幕预览图，避免与原图/对比视图混淆
          invalidatePreview();
        }
      });
    });
  });
})();
