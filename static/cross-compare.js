'use strict';
/* 双文档对比审阅：配对确认、重复/缺失决定、对齐差异图与文字增删、版本失效提醒 */
(function () {
  const VERDICTS = {
    equivalent: '仅拍摄差异，内容一致',
    content_changed: '图像内容有改动',
    text_changed: '文字有增删',
    different: '不同页面/附件',
    uncertain: '存疑待查',
  };
  const MATCH_STATUS = {
    auto_confirmed: '自动配对',
    confirmed: '已人工确认',
    suggested_review: '待人工确认',
  };
  const DECISIONS = {
    duplicate: '重复页',
    missing: '对方缺失',
    inserted: '新增插入页',
    ignored: '忽略不审',
    pending: '待决定',
  };

  async function api(url, method = 'GET', data) {
    const options = { method, headers: { 'X-PaperFlow': '1' } };
    if (data !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(data);
    }
    const response = await fetch('/api' + url, options);
    const result = await response.json();
    if (!response.ok) throw Error(result.error || '请求失败');
    return result;
  }

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function docOptions(docs, selectedId) {
    return docs.map(doc =>
      `<option value="${doc.id}" ${doc.id === selectedId ? 'selected' : ''}>${esc(doc.name)}（${doc.page_count}页）</option>`
    ).join('');
  }

  function thumb(pageId, revisionKey = '') {
    return `/api/pages/${pageId}/image?variant=thumbnail&r=${revisionKey}${Date.now()}`;
  }

  async function run(label, action) {
    const busy = document.getElementById('busy');
    const text = document.getElementById('busyText');
    text.textContent = label;
    busy.classList.remove('hidden');
    try {
      return await action();
    } catch (error) {
      const toast = document.getElementById('toast');
      toast.textContent = error.message;
      toast.className = 'error';
      toast.style.display = 'block';
      throw error;
    } finally {
      busy.classList.add('hidden');
    }
  }

  function modal(title, html, wide) {
    const dialog = document.getElementById('modal');
    document.getElementById('modalTitle').textContent = title;
    const content = document.getElementById('modalContent');
    content.innerHTML = html;
    content.classList.toggle('cc-wide', !!wide);
    if (!dialog.open) dialog.showModal();
  }

  // --------------------------------------------------------------- 项目列表 / 创建
  async function openHome() {
    const [workspace, projects] = await Promise.all([
      api('/workspace'), api('/cross-comparisons'),
    ]);
    const rows = projects.map(project => `
      <div class="task-item">
        <b>${esc(project.name)}</b>
        <small>${esc(project.left_document_name)} ⇌ ${esc(project.right_document_name)}</small>
        <small>${new Date(project.created_at).toLocaleString('zh-CN')} · 配对 ${project.pair_count} 组 ·
          未配 ${project.left_unmatched_count}/${project.right_unmatched_count} 页</small>
        ${project.stale ? `<span class="cc-stale">${project.stale_pair_count} 组结论因重扫/校订需重看${project.missing_page_count ? '，含页面缺失' : ''}</span>` : ''}
        <div class="cc-actions" style="margin-top:8px">
          <button data-open="${project.id}">打开审阅</button>
          <button data-delete="${project.id}" class="danger-text">删除项目</button>
        </div>
      </div>`).join('') || '<p class="cc-empty">还没有对比项目。选择两份独立扫描的文档开始。</p>';
    modal('双文档对比审阅', `
      <form id="ccCreateForm" class="modal-form">
        <div class="cc-doc-picker">
          <div><label>左侧文档（通常为较早扫描版）</label>
            <select name="left_document_id" required>${docOptions(workspace.documents)}</select></div>
          <div><label>右侧文档（通常为较新扫描版）</label>
            <select name="right_document_id" required>${docOptions(workspace.documents)}</select></div>
        </div>
        <label>对比项目名称</label>
        <input name="name" maxlength="160" placeholder="如：采购合同 11月版 vs 12月版">
        <button type="submit" class="primary">创建并自动匹配页面</button>
      </form>
      <h3 style="margin:18px 0 8px">已有对比项目</h3>${rows}`, true);

    document.getElementById('ccCreateForm').onsubmit = async event => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(event.currentTarget));
      if (data.left_document_id === data.right_document_id) {
        alert('请选择两份不同的文档');
        return;
      }
      await run('正在创建对比项目并匹配页面…', async () => {
        const project = await api('/cross-comparisons', 'POST', data);
        await api(`/cross-comparisons/${project.id}/auto-match`, 'POST', {});
        await openProject(project.id);
      });
    };
    document.querySelectorAll('[data-open]').forEach(button => {
      button.onclick = () => run('正在打开对比项目…', () => openProject(button.dataset.open));
    });
    document.querySelectorAll('[data-delete]').forEach(button => {
      button.onclick = async () => {
        if (!confirm('删除该对比项目？审阅结论与配对记录将一并删除，文档本身不受影响。')) return;
        await api('/cross-comparisons/' + button.dataset.delete, 'DELETE');
        await openHome();
      };
    });
  }

  // --------------------------------------------------------------- 项目详情
  async function openProject(projectId) {
    const project = await api('/cross-comparisons/' + projectId);
    const stalePairs = project.stale.pairs.length;
    const pairRows = project.pairs.map(pair => {
      const visual = pair.visual || {};
      const text = pair.text || {};
      const verdict = pair.review
        ? `<span class="cc-verdict ${pair.review.verdict}">${esc(VERDICTS[pair.review.verdict] || pair.review.verdict)}</span>`
        : '<span class="hint">未下结论</span>';
      return `
      <tr data-pair="${pair.id}">
        <td><img src="${thumb(pair.left_page_id)}" alt="左页" data-zoom-left="${pair.left_page_id}"><br>
          <small>${esc(pair.left_name)}${pair.left_exists ? '' : '（已删除）'}</small></td>
        <td><img src="${thumb(pair.right_page_id)}" alt="右页" data-zoom-right="${pair.right_page_id}"><br>
          <small>${esc(pair.right_name)}${pair.right_exists ? '' : '（已删除）'}</small></td>
        <td><span class="cc-status ${pair.match_status}">${esc(MATCH_STATUS[pair.match_status] || pair.match_status)}</span><br>
          <small>视觉相关 ${visual.correlation ?? '—'}<br>笔画改动 ${(visual.ink_change_ratio ?? 0).toFixed(4)} · 区域 ${visual.region_count ?? 0}<br>
          文字相似 ${text.similarity ?? '无OCR'}</small></td>
        <td>${verdict}
          ${pair.stale.stale ? `<br><span class="cc-stale">版本已变，结论需重看：${pair.stale.reasons.map(r => esc(r.reason)).join('、')}</span>` : ''}
        </td>
        <td class="cc-actions">
          <button data-detail="${pair.id}">差异详情</button>
          ${pair.match_status !== 'confirmed' ? `<button data-confirm="${pair.id}">确认配对</button>` : ''}
          ${pair.stale.stale ? `<button data-refresh="${pair.id}">按新版本重算</button>` : ''}
          <button data-unmatch="${pair.id}" class="danger-text">解除配对</button>
        </td>
      </tr>`;
    }).join('');

    const decisionMap = Object.fromEntries(project.decisions.map(item => [item.page_id, item]));
    const unmatchedCard = (item, side) => {
      const decision = decisionMap[item.page_id];
      return `
      <div class="cc-card">
        <img src="${thumb(item.page_id)}" alt="未配对页">
        <b>${esc(item.name)}</b>
        <div style="margin-top:6px">
          <select data-decision-side="${side}" data-decision-page="${item.page_id}">
            ${Object.entries(DECISIONS).map(([value, label]) =>
              `<option value="${value}" ${decision?.decision === value ? 'selected' : ''}>${label}</option>`).join('')}
          </select>
          ${decision ? `<small>审阅人：${esc(decision.reviewer)}<br>${esc(decision.note || '')}</small>` : '<small class="hint">等待人工决定</small>'}
        </div>
      </div>`;
    };

    modal(esc(project.name), `
      <div class="cc-summary">
        <span class="cc-chip">左：${esc(project.left_document_name)}</span>
        <span class="cc-chip">右：${esc(project.right_document_name)}</span>
        <span class="cc-chip">配对 ${project.pair_count} 组</span>
        <span class="cc-chip">左未配 ${project.left_unmatched_count} 页</span>
        <span class="cc-chip">右未配 ${project.right_unmatched_count} 页</span>
        ${stalePairs ? `<span class="cc-chip warn">⚠ ${stalePairs} 组结论需重看</span>` : ''}
      </div>
      <div class="cc-toolbar">
        <button id="ccRematch" class="primary">重新自动匹配（保留人工配对）</button>
        <button id="ccResetMatch">清空配对重新匹配</button>
        <button id="ccRefreshAll">按当前版本全部重算</button>
        <button id="ccManual">人工指定配对</button>
      </div>
      <table class="cc-table">
        <thead><tr><th>左侧页面</th><th>右侧页面</th><th>匹配证据</th><th>审阅结论</th><th>操作</th></tr></thead>
        <tbody>${pairRows || '<tr><td colspan="5" class="cc-empty">暂无配对，点击重新自动匹配或人工指定。</td></tr>'}</tbody>
      </table>
      <div class="cc-unmatched">
        <div><h3>左侧未配对页（重复/缺失决定）</h3>
          ${project.left_unmatched.map(item => unmatchedCard(item, 'left')).join('') || '<p class="hint">无</p>'}</div>
        <div><h3>右侧未配对页（插入/重复决定）</h3>
          ${project.right_unmatched.map(item => unmatchedCard(item, 'right')).join('') || '<p class="hint">无</p>'}</div>
      </div>`, true);

    const reload = () => openProject(projectId);
    document.getElementById('ccRematch').onclick = () =>
      run('正在重新自动匹配…', async () => { await api(`/cross-comparisons/${projectId}/auto-match`, 'POST', {}); await reload(); });
    document.getElementById('ccResetMatch').onclick = () => {
      if (!confirm('清空全部配对并重新自动匹配？已有审阅结论会随配对删除。')) return;
      run('正在重置匹配…', async () => { await api(`/cross-comparisons/${projectId}/auto-match`, 'POST', { reset: true }); await reload(); });
    };
    document.getElementById('ccRefreshAll').onclick = () =>
      run('正在按当前版本重算证据…', async () => { await api(`/cross-comparisons/${projectId}/refresh`, 'POST'); await reload(); });
    document.getElementById('ccManual').onclick = () => openManual(projectId, project, reload);

    document.querySelectorAll('[data-confirm]').forEach(button => {
      button.onclick = () => run('正在确认配对…', async () => {
        await api(`/cross-comparisons/${projectId}/pairs/${button.dataset.confirm}/confirm`, 'POST');
        await reload();
      });
    });
    document.querySelectorAll('[data-refresh]').forEach(button => {
      button.onclick = () => run('正在重算该配对…', async () => {
        await api(`/cross-comparisons/${projectId}/pairs/${button.dataset.refresh}/refresh`, 'POST');
        await reload();
      });
    });
    document.querySelectorAll('[data-unmatch]').forEach(button => {
      button.onclick = () => {
        if (!confirm('解除该配对？两页将回到未配对列表。')) return;
        run('正在解除配对…', async () => { await api(`/cross-comparisons/${projectId}/pairs/${button.dataset.unmatch}`, 'DELETE'); await reload(); });
      };
    });
    document.querySelectorAll('[data-detail]').forEach(button => {
      button.onclick = () => openPairDetail(projectId, button.dataset.detail);
    });
    document.querySelectorAll('[data-decision-side]').forEach(select => {
      select.onchange = () => run('正在保存页面决定…', async () => {
        await api(`/cross-comparisons/${projectId}/decisions`, 'POST', {
          page_id: select.dataset.decisionPage,
          side: select.dataset.decisionSide,
          decision: select.value,
          note: select.value === 'pending' ? '' : prompt('备注（可留空）') || '',
        });
        await reload();
      });
    });
    document.querySelectorAll('img[data-zoom-left],img[data-zoom-right]').forEach(img => {
      img.onclick = () => window.open(img.src.replace('variant=thumbnail', 'variant=current'), '_blank', 'noopener');
    });
  }

  // --------------------------------------------------------------- 人工配对
  async function openManual(projectId, project, reload) {
    const leftIds = new Set(project.left_unmatched.map(item => item.page_id));
    const rightIds = new Set(project.right_unmatched.map(item => item.page_id));
    const workspace = await api('/workspace');
    const leftDoc = workspace.documents.find(doc => doc.id === project.left_document_id);
    const rightDoc = workspace.documents.find(doc => doc.id === project.right_document_id);
    const fullLeft = await api('/documents/' + project.left_document_id);
    const fullRight = await api('/documents/' + project.right_document_id);
    const options = (doc, pages, available) => pages
      .map((page, index) => available.has(page.id)
        ? `<option value="${page.id}">第${index + 1}页 · ${esc(page.name)}</option>` : '')
      .join('');
    modal('人工指定页面配对', `
      <form id="ccManualForm" class="modal-form">
        <p class="hint">仅可选择当前未配对页面。人工配对会被标记为“已人工确认”，系统仍展示全部视觉与文字证据，不会把不同附件伪装成同页。</p>
        <label>左侧（${esc(leftDoc.name)}）</label>
        <select name="left_page_id" required>${options(leftDoc, fullLeft.pages, leftIds)}</select>
        <label>右侧（${esc(rightDoc.name)}）</label>
        <select name="right_page_id" required>${options(rightDoc, fullRight.pages, rightIds)}</select>
        <button class="primary" type="submit">建立配对并计算证据</button>
      </form>`, true);
    document.getElementById('ccManualForm').onsubmit = event => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(event.currentTarget));
      run('正在建立人工配对…', async () => {
        await api(`/cross-comparisons/${projectId}/pairs`, 'POST', data);
        await reload();
      });
    };
  }

  // --------------------------------------------------------------- 配对差异详情
  async function openPairDetail(projectId, pairId) {
    const project = await api('/cross-comparisons/' + projectId);
    const pair = project.pairs.find(item => item.id === pairId);
    if (!pair) return;
    const visual = pair.visual || {};
    const text = pair.text || {};
    const textRows = (text.rows || []).map(row => {
      if (row.type === 'equal') return `<div class="row">${esc(row.left)}</div>`;
      if (row.type === 'removed') return `<div class="row removed">－ ${esc(row.left)}</div>`;
      return `<div class="row added">＋ ${esc(row.right)}</div>`;
    }).join('');
    const wordChanges = (text.word_changes || []).slice(0, 60).map(item =>
      `<span class="${item.type}">${item.type === 'added' ? '＋' : '－'} ${esc(item.text)}</span>`).join('');
    const staleHistory = (pair.stale_reviews || []).map((item, index) =>
      `<p class="hint">旧结论 ${index + 1}：${esc(VERDICTS[item.verdict] || item.verdict)} · ${esc(item.reviewer)} · ${new Date(item.created_at).toLocaleString('zh-CN')}<br>依据版本：左 rev ${item.left_ref.revision} / 右 rev ${item.right_ref.revision}<br>${esc(item.note || '')}</p>`
    ).join('');
    modal('配对差异详情 · ' + esc(pair.left_name) + ' ⇌ ' + esc(pair.right_name), `
      ${pair.stale.stale ? `<div class="cc-stale" style="display:block;margin-bottom:10px">当前页面版本与审阅时不同：${pair.stale.reasons.map(r => esc(r.reason)).join('、')}。请重算后复核，旧依据保留在下方。</div>` : ''}
      <div class="cc-diff-meta">
        配准：位移 x=${visual.shift_x ?? '—'}px / y=${visual.shift_y ?? '—'}px，旋转 ${visual.rotation_degrees ?? '—'}°，
        相关系数 ${visual.correlation ?? '—'}，判定 <b>${esc(visual.classification || '—')}</b><br>
        笔画失配率 ${(visual.ink_change_ratio ?? 0).toFixed(5)}，差异区域 ${visual.region_count ?? 0} 个，
        感知指纹距离 ${visual.phash_distance ?? '—'}。红色=左页有而右页无，绿色=右页新增。
      </div>
      <div class="cc-diff-view">
        <div><img src="/api/cross-comparisons/${projectId}/pairs/${pairId}/diff-image?ts=${Date.now()}" alt="对齐差异图"></div>
        <div>
          <h3 style="font-size:12px">文字增删（基于双方 OCR）</h3>
          ${text.available ? `
            <p class="hint">文字相似度 ${text.similarity} · 新增 ${text.added_tokens} · 删除 ${text.removed_tokens} token</p>
            <div class="cc-textdiff">${textRows || '<p class="hint">文字内容一致。</p>'}</div>
            <div class="cc-wordchanges">${wordChanges || '<span class="hint">无 token 级增删</span>'}</div>`
            : '<p class="hint">至少一侧尚无 OCR 结果，无法比对文字；请先在文字识别面板执行 OCR。视觉证据仍可使用。</p>'}
        </div>
      </div>
      <form id="ccReviewForm" class="modal-form cc-review-form">
        <h3 style="font-size:12px;margin-top:14px">审阅结论（绑定双方当前版本）</h3>
        <label>结论</label>
        <select name="verdict">
          ${Object.entries(VERDICTS).map(([value, label]) =>
            `<option value="${value}" ${pair.review?.verdict === value ? 'selected' : ''}>${label}</option>`).join('')}
        </select>
        <label>审阅人</label><input name="reviewer" value="${esc(pair.review?.reviewer || '本机用户')}" maxlength="80" required>
        <label>备注</label><textarea name="note" maxlength="5000">${esc(pair.review?.note || '')}</textarea>
        <button type="submit" class="primary">保存审阅结论</button>
      </form>
      ${pair.review ? `<p class="hint">当前结论：${esc(VERDICTS[pair.review.verdict] || pair.review.verdict)} · ${new Date(pair.review.created_at).toLocaleString('zh-CN')} · 左 rev ${pair.review.left_ref.revision} / 右 rev ${pair.review.right_ref.revision}</p>` : ''}
      ${staleHistory ? `<h3 style="font-size:12px;margin-top:10px">失效的历史依据（仅供核对）</h3>${staleHistory}` : ''}`, true);

    document.getElementById('ccReviewForm').onsubmit = event => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(event.currentTarget));
      run('正在保存审阅结论…', async () => {
        await api(`/cross-comparisons/${projectId}/pairs/${pairId}/review`, 'POST', data);
        await openPairDetail(projectId, pairId);
      });
    };
  }

  document.getElementById('crossCompareMenu').addEventListener('click', () => {
    run('正在加载对比项目…', openHome).catch(() => undefined);
  });
})();
