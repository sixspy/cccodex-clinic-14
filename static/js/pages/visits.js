import { api, chip, escapeHtml } from '../api.js';

export async function renderVisits(el) {
  const [batches, sites, protocols] = await Promise.all([
    api('/api/batches'),
    api('/api/sites'),
    api('/api/protocols?status=' + encodeURIComponent('生效')),
  ]);
  const siteOpts = (sites.items || [])
    .filter((s) => s.status !== '停用')
    .map((s) => `<option value="${s.id}">${escapeHtml(s.site_code + ' · ' + s.name)}</option>`)
    .join('');
  const protoOpts = (protocols.items || [])
    .map((p) => `<option value="${p.id}">${escapeHtml(p.protocol_code + ' · ' + p.title)}</option>`)
    .join('');

  el.innerHTML = `
    <div class="card">
      <h2>访视清单抓取批次</h2>
      <p class="muted">按试验中心开批次，模拟抓取访视行，合格行可入库，缺口行进入质控与补采。</p>
      <div class="row">
        <select id="siteId">${siteOpts}</select>
        <select id="protocolId"><option value="">默认生效方案</option>${protoOpts}</select>
        <input id="bnote" placeholder="批次备注" />
        <button id="createBatch">开新批次</button>
      </div>
    </div>
    <div class="card">
      <div class="row">
        <select id="statusFilter">
          <option value="">全部批次状态</option>
          <option>采集中</option>
          <option>已收齐</option>
          <option>已关闭</option>
        </select>
        <button id="reload" class="ghost">刷新列表</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>批次编码</th><th>中心</th><th>方案</th><th>状态</th>
            <th>行数</th><th>缺口</th><th>幂等徽标</th><th>追加次数</th><th>备注</th><th>操作</th>
          </tr>
        </thead>
        <tbody id="batchBody">
          ${(batches.items || []).map(batchRow).join('')}
        </tbody>
      </table>
    </div>
    <div class="card">
      <h2>批次详情与访视行</h2>
      <div id="batchDetail" class="muted">点击「查看」加载批次内访视行与质量分布。</div>
    </div>
    <div class="card">
      <h2>幂等键账册（旁路台账）</h2>
      <p class="muted">读轨只认本账册现行旗：同键重放挡回刷、戳记拧花拦截、争用不加锁不双写。批次关闭后可归档收口为只读档。</p>
      <div class="row">
        <button id="idemReload" class="ghost">刷新账册</button>
        <button id="idemSeal" class="warn">归档已关闭批次（乙路收口）</button>
      </div>
      <div class="row">
        <button class="tab-btn active" data-idem-tab="queue">幂等键队列（现行）</button>
        <button class="tab-btn" data-idem-tab="archive">只读归档</button>
      </div>
      <div id="idemMsg" class="muted"></div>
      <div id="idemPanel"><p class="muted">在批次详情选择批次后展示该批幂等键落点。</p></div>
    </div>`;

  function idemBadge(b) {
    if (b.idem_riveted) {
      const archived = b.idem_archived > 0;
      return `<span class="chip ${archived ? 'idle' : 'ok'}">幂等铆键${archived ? '·部分归档' : ''}</span>`;
    }
    return '<span class="chip idle">未铆键</span>';
  }

  function appendCell(b) {
    const attempts = b.append_attempts || 0;
    const keys = b.idem_keys || 0;
    // 徽标对照追加次数：重放次数 > 0 即出现拧花警示，提示重复追加已被拦
    const replays = Math.max(0, attempts - keys);
    if (replays > 0) {
      return `<span class="chip warn" title="含 ${replays} 次重复追加已被幂等拦截">${attempts}（重放×${replays}）</span>`;
    }
    return `<span>${attempts}</span>`;
  }

  function batchRow(b) {
    return `
      <tr>
        <td>${escapeHtml(b.batch_code)}</td>
        <td>${escapeHtml(b.site_name || '')}</td>
        <td>${escapeHtml((b.protocol_code || '-') + (b.protocol_version ? '@' + b.protocol_version : ''))}</td>
        <td>${chip(b.status)}</td>
        <td>${b.item_count}</td>
        <td>${b.gap_count}</td>
        <td>${idemBadge(b)}</td>
        <td>${appendCell(b)}</td>
        <td class="muted">${escapeHtml(b.note || '')}</td>
        <td class="row">
          <button data-view="${b.id}" class="ghost">查看</button>
          <button data-crawl="${b.id}">模拟抓取</button>
          <button data-ingest="${b.id}">合格入库</button>
          <button data-complete="${b.id}" class="warn">标已收齐</button>
          <button data-close="${b.id}" class="danger">关闭</button>
        </td>
      </tr>`;
  }

  const detail = el.querySelector('#batchDetail');
  const showErr = (msg) => { detail.innerHTML = `<p class="err">${escapeHtml(msg)}</p>`; };
  let currentBatchId = null;
  let idemTab = 'queue';

  async function loadIdemPanel() {
    const panel = el.querySelector('#idemPanel');
    if (!panel) return;
    if (!currentBatchId) {
      panel.innerHTML = '<p class="muted">在批次详情选择批次后展示该批幂等键落点。</p>';
      return;
    }
    if (idemTab === 'archive') {
      const data = await api(`/api/idempotency/archive?batch_id=${currentBatchId}`);
      const items = data.items || [];
      panel.innerHTML = `
        <table>
          <thead><tr><th>幂等键（只读）</th><th>受试者/访视</th><th>落点行</th><th>追加次数</th><th>归档时间</th></tr></thead>
          <tbody>
            ${items.map((x) => `
              <tr>
                <td><code class="idem-key">${escapeHtml(x.idem_key)}</code></td>
                <td>${escapeHtml(x.subject_key)} / ${escapeHtml(x.visit_code)}</td>
                <td>#${x.row_id}</td>
                <td>${x.attempt_count}</td>
                <td class="muted">${escapeHtml(x.archived_at || '')}</td>
              </tr>`).join('') || '<tr><td colspan="5" class="muted">该批暂无已归档档</td></tr>'}
          </tbody>
        </table>`;
      return;
    }
    const data = await api(`/api/idempotency/ledger?batch_id=${currentBatchId}&status_flag=${encodeURIComponent('现行')}`);
    const items = data.items || [];
    panel.innerHTML = `
      <table>
        <thead><tr><th>幂等键</th><th>受试者/访视</th><th>落点行</th><th>状态旗</th><th>追加次数</th><th>钤印（前16位）</th></tr></thead>
        <tbody>
          ${items.map((x) => `
            <tr>
              <td><code class="idem-key">${escapeHtml(x.idem_key)}</code></td>
              <td>${escapeHtml(x.subject_key)} / ${escapeHtml(x.visit_code)}</td>
              <td>#${x.row_id}</td>
              <td>${chip(x.status_flag)}</td>
              <td>${x.attempt_count}${x.attempt_count > 1 ? ' <span class="chip warn">含重放</span>' : ''}</td>
              <td class="muted">${escapeHtml((x.stamp || '').slice(0, 16))}…</td>
            </tr>`).join('') || '<tr><td colspan="6" class="muted">该批现行账册为空（批次关闭归档后在此页签消失）</td></tr>'}
        </tbody>
      </table>`;
  }

  el.querySelectorAll('[data-idem-tab]').forEach((btn) => {
    btn.onclick = () => {
      idemTab = btn.dataset.idemTab;
      el.querySelectorAll('[data-idem-tab]').forEach((b) => b.classList.toggle('active', b === btn));
      loadIdemPanel().catch((e) => {
        el.querySelector('#idemMsg').textContent = e.message;
      });
    };
  });
  el.querySelector('#idemReload').onclick = () => loadIdemPanel().catch((e) => {
    el.querySelector('#idemMsg').textContent = e.message;
  });
  el.querySelector('#idemSeal').onclick = async () => {
    const msg = el.querySelector('#idemMsg');
    try {
      const r = await api('/api/idempotency/seal', { method: 'POST', body: '{}' });
      msg.className = 'muted';
      msg.textContent = `归档收口完成，迁入只读档 ${r.archived} 笔。`;
      await softReload();
      await loadIdemPanel();
    } catch (e) { msg.className = 'err'; msg.textContent = e.message; }
  };

  async function loadDetail(id) {
    const b = await api(`/api/batches/${id}`);
    currentBatchId = id;
    const qs = b.quality_stats || {};
    const idem = b.idem_summary || { keys: 0, attempts: 0, archived: 0, riveted: false };
    detail.innerHTML = `
      <div class="row">
        <strong>${escapeHtml(b.batch_code)}</strong>
        ${chip(b.status)}
        <span class="muted">${escapeHtml(b.site_name)} · 方案 ${escapeHtml(b.protocol_title || '-')}</span>
      </div>
      <div class="grid">
        <div class="stat"><div class="n">${b.item_count}</div><div class="l">访视行</div></div>
        <div class="stat"><div class="n">${b.gap_count}</div><div class="l">缺口行</div></div>
        <div class="stat"><div class="n">${qs['合格'] || 0}</div><div class="l">合格</div></div>
        <div class="stat"><div class="n">${(qs['缺字段'] || 0) + (qs['编码空'] || 0) + (qs['重复键'] || 0)}</div><div class="l">异常质量</div></div>
      </div>
      <div class="card idem-summary">
        <h2 style="font-size:15px">幂等摘要台</h2>
        <div class="grid">
          <div class="stat"><div class="n">${idem.riveted ? '<span class="chip ok">已铆键</span>' : '<span class="chip idle">未铆键</span>'}</div><div class="l">幂等徽标</div></div>
          <div class="stat"><div class="n">${idem.keys}</div><div class="l">幂等键落点</div></div>
          <div class="stat"><div class="n">${idem.attempts}</div><div class="l">累计追加次数</div></div>
          <div class="stat"><div class="n">${idem.archived}</div><div class="l">已归档档</div></div>
        </div>
        <p class="muted">口径：业务键=批次ID:受试者键:访视编码；钤印 sha256；同键重放挡回刷，戳记拧花拦截。</p>
      </div>
      <div class="row">
        <input id="subjectKey" placeholder="受试者键" />
        <input id="visitCode" placeholder="访视编码" value="V1" style="width:90px" />
        <input id="payload" placeholder='载荷 JSON，如 {"visit_date":"2026-11-20","investigator":"王医生","weight_kg":60}' style="min-width:320px" />
        <input id="idemKey" placeholder="幂等键（留空按口径自动生成）" style="min-width:220px" />
        <select id="qualityForce">
          <option value="">自动推断质量</option>
          <option>合格</option>
          <option>缺字段</option>
          <option>编码空</option>
          <option>重复键</option>
        </select>
        <button id="addRow">追加访视行</button>
        <button id="runQc" class="warn">对本批质控扫描</button>
        <button id="enqueueGaps">缺口排队补采</button>
      </div>
      <table>
        <thead><tr><th>ID</th><th>受试者</th><th>访视</th><th>质量</th><th>状态</th><th>幂等键落点</th><th>追加次数</th><th>载荷摘要</th><th>操作</th></tr></thead>
        <tbody>
          ${(b.items || []).map((r) => `
            <tr>
              <td>${r.id}</td>
              <td>${escapeHtml(r.subject_key)}</td>
              <td>${escapeHtml(r.visit_code)}</td>
              <td>${chip(r.quality)}</td>
              <td>${chip(r.status)}</td>
              <td>${r.idem_key ? `<code class="idem-key">${escapeHtml(r.idem_key)}</code><div>${chip(r.idem_flag || '现行')}</div>` : '<span class="chip idle">无落点</span>'}</td>
              <td>${r.idem_attempts || 1}</td>
              <td class="muted">${escapeHtml((r.payload || '').slice(0, 80))}</td>
              <td class="row">
                <button data-enqueue-row="${r.id}" ${r.quality === '合格' && r.status === '已入库' ? 'disabled' : ''}>排队补采</button>
                <button data-mark="${r.id}" data-st="待补采" class="ghost">标待补采</button>
              </td>
            </tr>`).join('') || '<tr><td colspan="9" class="muted">暂无访视行</td></tr>'}
        </tbody>
      </table>`;

    detail.querySelector('#addRow').onclick = async () => {
      try {
        const q = detail.querySelector('#qualityForce').value;
        const idemInput = detail.querySelector('#idemKey').value.trim();
        const r = await api(`/api/batches/${id}/items`, {
          method: 'POST',
          headers: idemInput ? { 'Idempotency-Key': idemInput } : {},
          body: JSON.stringify({
            subject_key: detail.querySelector('#subjectKey').value,
            visit_code: detail.querySelector('#visitCode').value,
            payload: detail.querySelector('#payload').value,
            quality: q || null,
            idem_key: idemInput || null,
          }),
        });
        showErr(r.replay ? `幂等拦截：该键为重复追加，已回放首笔结果（第 ${r.attempt_count} 次），未新增行。` : '');
        await loadDetail(id);
        await softReload();
        await loadIdemPanel();
      } catch (e) { showErr(e.message); }
    };
    detail.querySelector('#runQc').onclick = async () => {
      try {
        const r = await api(`/api/batches/${id}/qc`, { method: 'POST', body: '{}' });
        detail.insertAdjacentHTML('afterbegin', `<p class="muted">质控新建发现 ${r.created} 条</p>`);
        await loadDetail(id);
      } catch (e) { showErr(e.message); }
    };
    detail.querySelector('#enqueueGaps').onclick = async () => {
      try {
        const r = await api(`/api/batches/${id}/backfill`, { method: 'POST', body: '{}' });
        detail.insertAdjacentHTML('afterbegin', `<p class="muted">已排队 ${r.enqueued}，跳过 ${r.skipped}</p>`);
      } catch (e) { showErr(e.message); }
    };
    detail.querySelectorAll('[data-enqueue-row]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/rows/${btn.dataset.enqueueRow}/enqueue`, { method: 'POST', body: '{}' });
          await loadDetail(id);
        } catch (e) { showErr(e.message); }
      };
    });
    detail.querySelectorAll('[data-mark]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/rows/${btn.dataset.mark}/status`, {
            method: 'POST',
            body: JSON.stringify({ status: btn.dataset.st }),
          });
          await loadDetail(id);
        } catch (e) { showErr(e.message); }
      };
    });
  }

  async function softReload() {
    const status = el.querySelector('#statusFilter').value;
    const url = status ? `/api/batches?status=${encodeURIComponent(status)}` : '/api/batches';
    const data = await api(url);
    el.querySelector('#batchBody').innerHTML = (data.items || []).map(batchRow).join('');
    bindBatchActions();
  }

  function bindBatchActions() {
    el.querySelectorAll('[data-view]').forEach((btn) => {
      btn.onclick = () => {
        loadDetail(btn.dataset.view)
          .then(() => loadIdemPanel())
          .catch((e) => showErr(e.message));
      };
    });
    el.querySelectorAll('[data-crawl]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/batches/${btn.dataset.crawl}/crawl`, {
            method: 'POST',
            body: JSON.stringify({ count: 5, mix_bad: true }),
          });
          await softReload();
          await loadDetail(btn.dataset.crawl);
        } catch (e) { showErr(e.message); }
      };
    });
    el.querySelectorAll('[data-ingest]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/batches/${btn.dataset.ingest}/ingest`, { method: 'POST', body: '{}' });
          await softReload();
          await loadDetail(btn.dataset.ingest);
        } catch (e) { showErr(e.message); }
      };
    });
    el.querySelectorAll('[data-complete]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/batches/${btn.dataset.complete}/complete`, { method: 'POST', body: '{}' });
          await softReload();
        } catch (e) { showErr(e.message); }
      };
    });
    el.querySelectorAll('[data-close]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/batches/${btn.dataset.close}/close`, { method: 'POST', body: '{}' });
          await softReload();
          await loadIdemPanel();
        } catch (e) { showErr(e.message); }
      };
    });
  }

  el.querySelector('#createBatch').onclick = async () => {
    try {
      const body = {
        site_id: Number(el.querySelector('#siteId').value),
        note: el.querySelector('#bnote').value,
      };
      const pid = el.querySelector('#protocolId').value;
      if (pid) body.protocol_id = Number(pid);
      const created = await api('/api/batches', { method: 'POST', body: JSON.stringify(body) });
      await softReload();
      await loadDetail(created.id);
    } catch (e) { showErr(e.message); }
  };
  el.querySelector('#reload').onclick = () => softReload().catch((e) => showErr(e.message));
  el.querySelector('#statusFilter').onchange = () => softReload().catch((e) => showErr(e.message));
  bindBatchActions();
  loadIdemPanel().catch(() => {});
}
