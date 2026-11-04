/* AgentMesh OS unified web console. */
const state = {
  workspace: null, overview: null, plans: [], runs: [], tasks: [], approvals: [],
  costs: null, usage: [], audit: [], conversations: [], memories: [], bus: [],
  busStats: null, busSubs: [], sync: null, models: null, versionStatus: null,
  surface: null,
  mcp: null, selected: null, view: 'command', stream: null, lastSeq: 0,
  consoleLines: 0, reconnectTimer: null, refreshTimer: null,
};
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
  {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const fmt = (n) => Number(n || 0).toLocaleString('zh-CN');
const money = (n) => '$' + Number(n || 0).toFixed(4);
const pillClass = (s) => ({
  completed: 'ok', passed: 'ok', approved: 'ok', released: 'ok', synced: 'ok',
  running: 'info', verifying: 'info', merging: 'info', planning: 'info',
  pending_acceptance: 'warn', pending_merge: 'warn', waiting_user: 'warn',
  warning: 'warn', pending: 'warn', upgraded: 'warn', drift: 'warn',
  failed: 'bad', blocked: 'bad', rejected: 'bad', canceled: 'bad', conflict: 'bad',
}[s] || '');

function toast(msg, error = false) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show' + (error ? ' error' : '');
  setTimeout(() => { t.className = 'toast' + (error ? ' error' : ''); }, 3800);
}

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts });
  let data;
  try { data = await res.json(); } catch (e) { data = { error: { message: '响应不是 JSON' } }; }
  if (!res.ok) throw new Error((data.error && data.error.message) || ('HTTP ' + res.status));
  return data;
}
function post(path, body) { return api(path, { method: 'POST', body: JSON.stringify(body || {}) }); }

/* ------------------------------------------------------------------ */
/* Routing / refresh                                                   */
/* ------------------------------------------------------------------ */
function route() {
  const hash = (location.hash || '#command').slice(1);
  const views = ['command', 'agents', 'versions', 'graph', 'task', 'approvals', 'bus', 'audit'];
  state.view = views.includes(hash) ? hash : 'command';
  document.querySelectorAll('.section').forEach((s) => s.classList.remove('active'));
  const target = $('view-' + state.view);
  if (target) target.classList.add('active');
  document.querySelectorAll('#nav a').forEach((a) => (
    a.classList.toggle('active', a.dataset.view === state.view)));
  render();
}

async function refresh() {
  try {
    const ws = await api('/api/workspace');
    state.workspace = ws;
    if (!ws.open) {
      state.overview = null; state.models = null; state.versionStatus = null;
      render();
      return;
    }
    const [ov, plans, runs, tasks, approvals, costs, usage, audit, conversations, memory,
      bus, busStats, busSubs, sync, models, versionStatus] = await Promise.all([
      api('/api/overview'), api('/api/plans'), api('/api/runs'), api('/api/tasks'),
      api('/api/approvals'), api('/api/costs'), api('/api/costs'), api('/api/audit?limit=300'),
      api('/api/conversations'), api('/api/memory'), api('/api/bus?limit=120'),
      api('/api/bus/stats'), api('/api/bus/subscriptions'), api('/api/sync'),
      api('/api/agents/models'), api('/api/agents/version-status'),
    ]);
    state.overview = ov;
    state.plans = plans.plans || [];
    state.runs = runs.runs || [];
    state.tasks = tasks.tasks || [];
    state.approvals = approvals.approvals || [];
    state.costs = costs.costs || {};
    state.usage = usage.usage || [];
    state.audit = audit.events || [];
    state.conversations = conversations.conversations || [];
    state.memories = memory.memories || [];
    state.bus = bus.events || [];
    state.busStats = busStats;
    state.busSubs = busSubs.subscriptions || [];
    state.sync = sync;
    state.models = models;
    state.versionStatus = versionStatus;
    state.surface = await api('/api/surface').catch(() => null);
    if (state.selected) {
      const fresh = state.tasks.find((x) => x.uid === state.selected.uid);
      if (fresh) state.selected = fresh;
    }
    render();
  } catch (e) {
    toast('刷新失败：' + e.message, true);
  }
}

function scheduleRefresh() {
  if (state.refreshTimer) return;
  // Do not rebuild the command center while the user is editing a field.
  const active = document.activeElement;
  if (state.view === 'command' && active
      && ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName)) {
    return;
  }
  const delay = state.view === 'command' ? 4000 : 1200;
  state.refreshTimer = setTimeout(() => { state.refreshTimer = null; refresh(); }, delay);
}

/* ------------------------------------------------------------------ */
/* Header / workspace                                                  */
/* ------------------------------------------------------------------ */
function renderHeader() {
  const ws = state.workspace || {};
  const ov = state.overview;
  $('dataDir').textContent = ws.open ? (ws.root || '') : '未打开项目';
  if (!ws.open) {
    $('projectName').textContent = 'AgentMesh OS 指挥中心';
    $('projectMeta').textContent = '初始化项目目录，输入需求，选配主/子 Agent 与模型。';
    $('topBadges').innerHTML = '<span class="pill warn">工作区未打开</span>';
    return;
  }
  const p = ov ? ov.project : (ws.project || {});
  $('projectName').textContent = p.name || '项目';
  $('projectMeta').textContent = [p.root, p.branch || 'no branch',
    (p.commit || '').slice(0, 8)].filter(Boolean).join(' · ');
  const main = ov && ov.main;
  const run = ov && ov.latest_run;
  const drift = state.versionStatus ? state.versionStatus.drift_count : 0;
  const budgetWarning = ov && ov.budget ? ov.budget.warning : '';
  $('topBadges').innerHTML =
    '<span class="pill ok">工作区已打开</span>' +
    `<span class="pill ${main ? 'ok' : 'warn'}">主节点 ${main ? esc((main.agent && main.agent.name) || main.agent_id) : '未设置'}</span>` +
    (ov ? `<span class="pill ${ov.agents.online ? 'ok' : 'warn'}">Agent ${ov.agents.online}/${ov.agents.total} 在线</span>` : '') +
    (run ? `<span class="pill ${pillClass(run.status)}">运行 ${esc(run.status)}</span>` : '') +
    (ov ? `<span class="pill info">待审批 ${ov.pending_approvals.length}</span>` : '') +
    (budgetWarning ? `<span class="pill warn">${esc(budgetWarning)}</span>` : '') +
    (drift ? `<span class="pill warn">版本漂移 ${drift}</span>` : '');
}

function renderWorkspace() {
  const ws = state.workspace || {};
  const ov = state.overview;
  if (ws.open && ws.root && !$('wsPath').dataset.touched) {
    $('wsPath').value = ws.root;
  }
  if (!ws.open) {
    $('workspaceBanner').innerHTML = `<div class="banner"><b>先初始化项目：</b>${esc(ws.error || '输入一个目录路径，点击“初始化项目”即可创建目录、git init 并打开控制台。')}</div>`;
    $('wsInfo').innerHTML = '<div class="empty">尚未打开项目</div>';
    return;
  }
  $('workspaceBanner').innerHTML = '';
  const p = ov ? ov.project : (ws.project || {});
  if (ov && ov.budget && !$('budgetTokens').dataset.touched) {
    $('budgetTokens').value = ov.budget.project_budget || 0;
  }
  $('wsInfo').innerHTML = `<dl class="kv">
    <dt>项目 ID</dt><dd class="mono">${esc(p.project_id || '')}</dd>
    <dt>工作空间</dt><dd class="mono">${esc(p.workspace_id || '')}</dd>
    <dt>分支 / Commit</dt><dd>${esc(p.branch || '—')} · ${esc((p.commit || '').slice(0, 12) || '—')}</dd>
    <dt>Git Remote</dt><dd>${esc(p.git_remote || '（无）')}</dd>
    <dt>技术栈</dt><dd>${(p.tech_stack || []).map((x) => `<span class="tag">${esc(x)}</span>`).join('') || '未识别'}</dd>
    <dt>构建 / 测试</dt><dd>${esc(p.build_command || '未配置')} / ${esc(p.test_command || '未配置')}</dd>
    <dt>数据目录</dt><dd class="mono">${esc(ov ? ov.data_dir : '')}</dd>
  </dl>`;
}

/* ------------------------------------------------------------------ */
/* Command center: agents + models                                     */
/* ------------------------------------------------------------------ */
function modelOptions(agentId, selected) {
  const item = ((state.models || {}).by_agent || {})[agentId] || { models: [] };
  let html = '<option value="">跟随 Agent 默认</option>';
  const names = [];
  (item.models || []).forEach((m) => {
    names.push(m.name);
    html += `<option value="${esc(m.name)}" ${m.name === selected ? 'selected' : ''}>${esc(m.name)} · ${esc(m.source_label || m.source)}</option>`;
  });
  if (selected && !names.includes(selected)) {
    html += `<option value="${esc(selected)}" selected>${esc(selected)} · 手动</option>`;
  }
  return html;
}

function renderCommandAgents() {
  const ov = state.overview;
  if (!ov) {
    $('subAgents').innerHTML = '<div class="empty">尚未打开项目或没有 Agent</div>';
    $('mainAgent').innerHTML = '';
    $('mainModelSelect').innerHTML = '';
    return;
  }
  const agents = ov.agents.list || [];
  const main = ov.main;
  const mainId = main ? main.agent_id : '';
  $('mainAgent').innerHTML = agents.map((a) => (
    `<option value="${esc(a.agent_id)}" ${a.agent_id === mainId ? 'selected' : ''}>${esc(a.name)} · ${esc(a.access_level)} · ${esc(a.status)} · v${esc(a.version)}</option>`
  )).join('') || '<option value="">没有可用 Agent</option>';
  const selectedMain = $('mainAgent').value || mainId;
  const mainEntry = ((state.models || {}).by_agent || {})[selectedMain] || {};
  $('mainModelSelect').innerHTML = modelOptions(selectedMain, mainEntry.selected_model || (main ? main.model : ''));
  if (main && !$('mainModel').value) $('mainModel').value = main.model || '';
  $('mainBudget').value = main ? (main.budget_tokens || 0) : 0;
  $('mainInfo').innerHTML = main
    ? `<span class="pill ok">${esc(main.agent_id)}</span> 模型 ${esc(main.model || '未指定')} · 设置于 ${esc(main.set_at || '')}`
    : '尚未指定主节点。选择一个 Agent 并保存。';

  const mainModelCount = ((state.models || {}).models || []).length;
  $('modelSummary').textContent = `${mainModelCount} 个模型 / ${agents.length} 个 Agent`;
  $('subAgents').innerHTML = agents.map((a) => {
    const entry = ((state.models || {}).by_agent || {})[a.agent_id] || {};
    const selected = entry.selected_model || '';
    const isMain = a.agent_id === mainId;
    return `<div class="models-row">
      <div><b>${esc(a.name)}</b> <span class="pill ${isMain ? 'ok' : ''}">${isMain ? '主' : '子'}</span>
        <div class="sub">${esc(a.agent_id)} · v${esc(a.version)} · ${esc(a.status)} · ${esc(a.adapter_type)}</div></div>
      <select id="model-${esc(a.agent_id)}">${modelOptions(a.agent_id, selected)}</select>
      <input id="custom-${esc(a.agent_id)}" placeholder="或手填模型名">
      <button class="small" onclick="saveSubModel('${esc(a.agent_id)}')">保存</button>
      <span class="sub">${selected ? '已选 ' + esc(selected) : '未选配'}</span>
    </div>`;
  }).join('') || '<div class="empty">没有 Agent。点击“重新探测 Agent”。</div>';
}

function onMainAgentChange() {
  const id = $('mainAgent').value;
  const entry = ((state.models || {}).by_agent || {})[id] || {};
  $('mainModelSelect').innerHTML = modelOptions(id, entry.selected_model || '');
  $('mainModel').value = entry.selected_model || '';
  $('mainInfo').innerHTML = entry.models && entry.models.length
    ? `该 Agent 已探测到 ${entry.models.length} 个模型：${entry.models.map((m) => esc(m.name)).join(', ')}`
    : '该 Agent 暂未探测到模型；可手填模型名。';
}
function syncMainCustom() { $('mainModel').value = $('mainModelSelect').value; }

async function autoMain() {
  try {
    const r = await post('/api/main/auto', {
      model: $('mainModel').value.trim() || $('mainModelSelect').value,
    });
    toast(`自动主节点：${r.agent_id} · ${r.model}`);
    refresh();
  } catch (e) { toast(e.message, true); }
}

async function saveBudget() {
  try {
    const r = await post('/api/budget', { tokens: Number($('budgetTokens').value || 0) });
    toast(`项目预算已保存：${r.token_budget}`);
    refresh();
  } catch (e) { toast(e.message, true); }
}

async function saveMain() {
  const agent = $('mainAgent').value;
  if (!agent) { toast('请选择主节点 Agent', true); return; }
  const model = $('mainModel').value.trim() || $('mainModelSelect').value;
  try {
    await post('/api/main', {
      agent_id: agent, model, budget_tokens: Number($('mainBudget').value || 0),
    });
    if (model) await post('/api/agents/' + encodeURIComponent(agent) + '/model', { model });
    toast('主节点与模型已保存');
    refresh();
  } catch (e) { toast(e.message, true); }
}

async function saveSubModel(agentId) {
  const select = $('model-' + agentId);
  const custom = $('custom-' + agentId);
  const model = (custom && custom.value.trim()) || (select && select.value) || '';
  try {
    await post('/api/agents/' + encodeURIComponent(agentId) + '/model', { model });
    toast(`${agentId} 模型已${model ? '选配：' + model : '清空'}`);
    if (custom) custom.value = '';
    refresh();
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ */
/* Command center: plans / approvals / runs                            */
/* ------------------------------------------------------------------ */
function renderCommandApprovals() {
  const pending = (state.approvals || []).filter((a) => a.status === 'pending');
  $('commandApprovals').innerHTML = pending.length ? pending.slice(0, 8).map((a) => `
    <div class="check"><span><b>${esc(a.subject)}</b>
      <div class="sub">${esc(a.kind)} · ${esc(a.approval_id)}</div></span>
      <span class="row"><button class="primary small" onclick="decideApproval('${esc(a.approval_id)}',true)">批准</button>
      <button class="danger small" onclick="decideApproval('${esc(a.approval_id)}',false)">拒绝</button></span>
    </div>`).join('') : '<div class="empty">没有待审批事项</div>';
}

function renderCommandRun() {
  const run = state.overview && state.overview.latest_run;
  if (!run) {
    $('commandRun').innerHTML = '<div class="empty">暂无运行</div>';
    return;
  }
  const counts = {};
  (state.tasks || []).forEach((t) => { counts[t.status] = (counts[t.status] || 0) + 1; });
  $('commandRun').innerHTML = `<dl class="kv">
    <dt>运行 ID</dt><dd class="mono">${esc(run.run_id)}</dd>
    <dt>状态</dt><dd><span class="pill ${pillClass(run.status)}">${esc(run.status)}</span></dd>
    <dt>主节点</dt><dd>${esc(run.main_agent_id)} · ${esc(run.model || '')}</dd>
    <dt>任务</dt><dd>${Object.entries(counts).map(([k, v]) => `<span class="pill ${pillClass(k)}">${esc(k)} ${v}</span>`).join('') || '无'}</dd>
    <dt>验收 / 发布</dt><dd>${esc(run.acceptance_status || 'pending')} / ${esc(run.release_status || 'not_requested')}</dd>
  </dl>
  <div class="row" style="margin-top:10px">
    ${run.status === 'ready' ? `<button class="primary" onclick="startRun('${esc(run.run_id)}')">开始执行</button>` : ''}
    ${run.status === 'running' ? '<button onclick="refresh()">刷新进度</button>' : ''}
    ${['waiting_user', 'paused'].includes(run.status) ? `<button class="primary" onclick="resumeRun('${esc(run.run_id)}')">继续运行</button>` : ''}
    ${run.status === 'pending_acceptance' ? `<button onclick="location.hash='#approvals'">验收确认</button>` : ''}
    ${run.status === 'pending_merge' ? `<button class="primary" onclick="mergeRun('${esc(run.run_id)}')">批准并合并</button>` : ''}
    ${run.status === 'completed' ? `<button class="primary" onclick="releaseRun('${esc(run.run_id)}')">申请发布审批</button>` : ''}
    <button onclick="location.hash='#graph'">任务图</button>
  </div>`;
}

function renderCommandPlans() {
  const plans = state.plans || [];
  $('commandPlans').innerHTML = plans.length ? plans.map((p) => `
    <div class="card" style="box-shadow:none;margin:8px 0">
      <div class="row" style="justify-content:space-between">
        <div><b>${esc(p.goal)}</b><div class="sub">${esc(p.plan_id)} · ${(p.tasks || []).length} 任务 · <span class="pill ${pillClass(p.status)}">${esc(p.status)}</span></div></div>
        <div class="row">
          ${p.status === 'pending_approval' ? `<button class="primary small" onclick="approvePlan('${esc(p.plan_id)}')">批准计划</button>` : ''}
          <button class="small" onclick="showPlan('${esc(p.plan_id)}')">查看</button>
        </div>
      </div>
      <div id="plan-${esc(p.plan_id)}"></div>
    </div>`).join('') : '<div class="empty">暂无计划</div>';
}

/* ------------------------------------------------------------------ */
/* Agents / versions / MCP                                             */
/* ------------------------------------------------------------------ */
function renderAgents() {
  const ov = state.overview;
  if (!ov) { $('agentCards').innerHTML = '<div class="empty">未打开项目</div>'; return; }
  const agents = ov.agents.list || [];
  const manifest = ((ov.connectors || {}).manifest || {}).agents || [];
  const capabilityById = Object.fromEntries(manifest.map((m) => [m.agent_id, m.capability || []]));
  const scoreById = Object.fromEntries((((ov.router || {}).agents) || []).map((r) => [r.agent_id, r.stats || {}]));
  const modelsByAgent = (state.models || {}).by_agent || {};
  $('agentCards').innerHTML = agents.map((a) => {
    const capabilityList = capabilityById[a.agent_id] || Object.entries(a.capabilities || {}).filter(([k, v]) => v).map(([k]) => k);
    const caps = capabilityList.map((k) => `<span class="tag">${esc(k)}</span>`).join('');
    const stats = scoreById[a.agent_id] || {};
    const entry = modelsByAgent[a.agent_id] || {};
    return `<div class="card">
      <div class="row" style="justify-content:space-between">
        <h3>${esc(a.name)}</h3><span class="pill ${a.status === 'online' ? 'ok' : 'warn'}">${esc(a.status)}</span>
      </div>
      <div class="sub">${esc(a.agent_id)} · ${esc(a.product)} ${esc(a.edition)} · v${esc(a.version)}</div>
      <div style="margin:8px 0"><span class="pill info">${esc(a.access_level)}</span><span class="pill">${esc(a.adapter_type)}</span>
        <span class="pill ${entry.selected_model ? 'ok' : ''}">模型 ${esc(entry.selected_model || '未选配')}</span></div>
      <div class="sub">模型清单：${(entry.models || []).slice(0, 8).map((m) => esc(m.name)).join(', ') || '未探测'}</div>
      <label>选配模型</label>
      <select onchange="setAgentModelFromCard('${esc(a.agent_id)}', this.value)">${modelOptions(a.agent_id, entry.selected_model || '')}</select>
      <div class="sub" style="margin-top:8px">任务 ${a.metrics ? esc(a.metrics.tasks_total) : 0} · 成功率 ${a.metrics ? esc((a.metrics.success_rate * 100).toFixed(1) + '%') : '—'} · 平均成本 ${money(stats.avg_cost)} · 平均耗时 ${fmt(Math.round(stats.avg_duration_ms || 0))}ms</div>
      <div class="sub">最近错误 ${esc((a.metrics && a.metrics.last_error) || '无')}</div>
      <div style="margin-top:6px">${caps || '<span class="sub">无能力声明</span>'}</div>
      <div class="sub" style="margin-top:6px">数据：${esc((a.adapter_config || {}).data_path || (a.adapter_config || {}).config_path || '')}</div>
      <div class="row" style="margin-top:10px">
        <button class="small" onclick="setMainAs('${esc(a.agent_id)}')">设为主节点</button>
        <button class="small" onclick="healthAgent('${esc(a.agent_id)}')">健康检查</button>
        ${a.adapter_type === 'mcp' ? `<button class="small" onclick="loadMcpTools('${esc(a.agent_id)}')">MCP 工具</button>` : ''}
      </div>
    </div>`;
  }).join('') || '<div class="empty">未发现 Agent，点击上方按钮探测</div>';
}

function renderSurface() {
  const box = $('surfaceTable');
  if (!box) return;
  const report = state.surface;
  if (!report || !report.agents) {
    box.innerHTML = '<div class="empty">能力矩阵未加载（/api/surface）</div>';
    return;
  }
  const labels = { open_activate: '打开', workspace: '工作区', model: '模型', dev_mode: '开发模式', chat: '对话' };
  const statusText = { verified: '通过', partial: '部分', unsupported: '未通', not_installed: '未装' };
  const statusPill = { verified: 'ok', partial: 'warn', unsupported: 'bad', not_installed: '' };
  const head = '<tr><th>Agent</th>' + Object.keys(labels).map((k) => `<th>${labels[k]}</th>`).join('') + '<th>零输入</th></tr>';
  const rows = report.agents.map((a) => {
    const cells = Object.keys(labels).map((k) => {
      const c = a.capabilities[k] || {};
      const tip = esc(c.gap || c.interface || '');
      return `<td title="${tip}"><span class="pill ${statusPill[c.status] || ''}">${statusText[c.status] || esc(c.status)}</span></td>`;
    }).join('');
    return `<tr><td><b>${esc(a.name)}</b><div class="sub">${esc(a.agent_id)}</div></td>${cells}
      <td><span class="pill ${a.zero_input ? 'ok' : 'bad'}">${a.zero_input ? '是' : '否'}</span></td></tr>`;
  }).join('');
  const s = report.summary || {};
  box.innerHTML = `<div class="sub" style="margin-bottom:6px">${esc((report.zero_input || {}).mode || 'blocked')} · 已验证 ${esc(s.verified || 0)} / 部分 ${esc(s.partial || 0)} / 未通 ${esc(s.unsupported || 0)} / 未装 ${esc(s.not_installed || 0)}</div>
    <table class="table">${head}${rows}</table>`;
}

async function setAgentModelFromCard(agentId, model) {
  try {
    await post('/api/agents/' + encodeURIComponent(agentId) + '/model', { model });
    toast(`${agentId} 模型已更新`); refresh();
  } catch (e) { toast(e.message, true); }
}

function renderVersions() {
  const vs = state.versionStatus;
  if (!vs) { $('versionTable').innerHTML = '<div class="empty">未打开项目</div>'; return; }
  $('driftBox').innerHTML = (vs.drifts || []).length ? vs.drifts.map((d) => `
    <div class="drift"><b>${esc(d.name)} · ${esc(d.agent_id)}</b> 版本 ${esc(d.version)}
      <div class="sub">${esc(d.recommendation)}</div>
      <pre style="max-height:160px;margin-top:6px">${esc(JSON.stringify(d.changes || {}, null, 2))}</pre>
    </div>`).join('') : '<div class="sub">当前没有版本漂移。每次 Agent 升级/能力变化都会自动记录。</div>';
  $('versionTable').innerHTML = `<table class="table"><thead><tr>
      <th>Agent</th><th>版本</th><th>兼容状态</th><th>首次记录</th><th>最近确认</th><th>历史</th><th></th>
    </tr></thead><tbody>` + (vs.agents || []).map((a) => {
      const latest = a.latest || {};
      return `<tr>
        <td><b>${esc(a.name)}</b><div class="sub mono">${esc(a.agent_id)}</div></td>
        <td class="mono">${esc(a.version)}</td>
        <td><span class="pill ${pillClass(a.compat_status)}">${esc(a.compat_label || a.compat_status)}</span></td>
        <td class="mono">${esc((latest.first_seen || '').slice(0, 19))}</td>
        <td class="mono">${esc((latest.last_seen || '').slice(0, 19))}</td>
        <td>${esc(a.history_count || 0)}</td>
        <td><button class="small" onclick="loadVersionHistory('${esc(a.agent_id)}')">历史</button></td>
      </tr>`;
    }).join('') + '</tbody></table>';
}

async function loadVersionHistory(agentId) {
  try {
    const data = await api('/api/agents/versions?agent_id=' + encodeURIComponent(agentId));
    const rows = data.versions || [];
    $('versionHistory').className = '';
    $('versionHistory').innerHTML = `<h3>${esc(agentId)} · ${rows.length} 条记录</h3><div class="timeline">` +
      rows.map((r) => `<div class="item"><div class="dot"></div>
        <div><b>v${esc(r.version)}</b> <span class="pill ${pillClass(r.compat_status)}">${esc(r.compat_label || r.compat_status)}</span>
        <span class="sub">revision ${esc(r.revision)} · ${esc(r.adapter_type)}</span></div>
        <div class="sub">首次 ${esc((r.first_seen || '').slice(0, 19))} · 最近 ${esc((r.last_seen || '').slice(0, 19))}</div>
        <div class="sub">指纹 <span class="mono">${esc((r.fingerprint || '').slice(0, 16))}</span> · 模型 ${(r.models || []).map((m) => `<span class="tag">${esc(m)}</span>`).join('') || '无'}</div>
        <pre style="max-height:160px">${esc(JSON.stringify(r.changes || {}, null, 2))}</pre>
      </div>`).join('') + '</div>';
  } catch (e) { toast(e.message, true); }
}

async function loadMcpTools(agentId) {
  $('mcpPanel').innerHTML = `<div class="card"><h3>${esc(agentId)} MCP 工具</h3><div class="sub">正在执行真实 MCP initialize + tools/list…</div></div>`;
  try {
    const data = await api('/api/agents/' + encodeURIComponent(agentId) + '/mcp/tools');
    state.mcp = data;
    $('mcpPanel').innerHTML = `<div class="card"><div class="row" style="justify-content:space-between">
      <h3>${esc(agentId)} · MCP 工具 ${esc(data.count)} 个</h3>
      <span class="sub">protocol ${esc(data.protocol_version || '—')} · ${esc((data.server || {}).name || '')}</span>
      </div><div class="sub">工具参数结构来自服务端 tools/list；调用时使用你自己的 JSON 参数，系统不猜测。</div>
      <div style="margin-top:10px;max-height:520px;overflow:auto">${(data.tools || []).map((t) => `
        <div class="check"><span><b>${esc(t.name)}</b><div class="sub">${esc((t.description || '').slice(0, 240))}</div></span>
        <span><button class="small" onclick="callMcpTool('${esc(agentId)}','${esc(t.name)}')">调用</button></span></div>`).join('') || '<div class="empty">服务端未返回工具</div>'}</div>
      <div id="mcpResult"></div></div>`;
  } catch (e) {
    $('mcpPanel').innerHTML = `<div class="card"><h3>MCP 工具</h3><div class="empty">${esc(e.message)}</div></div>`;
    toast(e.message, true);
  }
}

async function callMcpTool(agentId, tool) {
  const raw = prompt(`输入 ${tool} 的 JSON 参数（必须符合服务端 inputSchema）：`, '{}');
  if (raw == null) return;
  let args;
  try { args = JSON.parse(raw || '{}'); } catch (e) { toast('参数不是 JSON：' + e.message, true); return; }
  try {
    const data = await post('/api/agents/' + encodeURIComponent(agentId) + '/mcp/call',
      { tool, arguments: args });
    $('mcpResult').innerHTML = `<pre>${esc(JSON.stringify(data, null, 2))}</pre>`;
    toast('MCP 工具调用完成');
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ */
/* Graph / task / approvals / bus / audit                              */
/* ------------------------------------------------------------------ */
function renderGraph() {
  const tasks = state.tasks || [];
  const svg = $('dag');
  $('graphRun').textContent = state.overview && state.overview.latest_run ? '（' + state.overview.latest_run.run_id + '）' : '';
  if (!tasks.length) { svg.innerHTML = ''; $('graphTasks').innerHTML = '<div class="empty">暂无任务</div>'; return; }
  const levels = {}; const byId = {};
  tasks.forEach((t) => { byId[t.task_id] = t; });
  function level(id, seen = new Set()) {
    if (levels[id] != null) return levels[id];
    if (seen.has(id)) return 0;
    seen.add(id);
    const deps = (byId[id] && byId[id].spec && byId[id].spec.dependencies) || [];
    const v = deps.length ? 1 + Math.max(...deps.map((d) => level(d, new Set(seen)))) : 0;
    levels[id] = v; return v;
  }
  tasks.forEach((t) => level(t.task_id));
  const cols = Math.max(...Object.values(levels)) + 1;
  const width = 900, height = 380, padX = 40, padY = 40;
  const colW = (width - padX * 2) / Math.max(cols, 1);
  const rows = {};
  tasks.forEach((t) => { rows[levels[t.task_id]] = (rows[levels[t.task_id]] || 0) + 1; });
  const pos = {};
  Object.keys(rows).forEach((l) => {
    const ids = Object.keys(levels).filter((id) => levels[id] === Number(l));
    ids.forEach((id, i) => {
      const x = padX + Number(l) * colW + 10;
      const y = padY + (height - padY * 2) * ((i + 1) / (ids.length + 1)) - 18;
      pos[id] = { x, y };
    });
  });
  let edges = '';
  tasks.forEach((t) => ((t.spec && t.spec.dependencies) || []).forEach((d) => {
    if (pos[d] && pos[t.task_id]) edges += `<line class="edge" x1="${pos[d].x + 150}" y1="${pos[d].y + 18}" x2="${pos[t.task_id].x}" y2="${pos[t.task_id].y + 18}"/>`;
  }));
  let nodes = '';
  tasks.forEach((t) => {
    const p = pos[t.task_id]; if (!p) return;
    nodes += `<g class="node ${esc(t.status)}" onclick="selectTask('${esc(t.uid)}')">
      <rect x="${p.x}" y="${p.y}" width="150" height="38" rx="8"/>
      <text x="${p.x + 8}" y="${p.y + 16}">${esc(t.task_id)} ${esc((t.title || '').slice(0, 12))}</text>
      <text class="st" x="${p.x + 8}" y="${p.y + 30}">${esc(t.status)} · ${esc(t.assigned_agent || '未分配')}</text>
    </g>`;
  });
  svg.innerHTML = `<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#cbd5e1"/></marker></defs>${edges}${nodes}`;
  $('graphTasks').innerHTML = `<table class="table"><thead><tr><th>任务</th><th>状态</th><th>类型</th><th>优先级</th><th>Agent</th><th>验证</th><th>依赖</th></tr></thead><tbody>` +
    tasks.map((t) => `<tr onclick="selectTask('${esc(t.uid)}')" style="cursor:pointer"><td>${esc(t.task_id)} · ${esc(t.title)}</td><td><span class="pill ${pillClass(t.status)}">${esc(t.status)}</span></td><td>${esc(t.type)}</td><td>${esc((t.spec || {}).priority == null ? 5 : (t.spec || {}).priority)}</td><td>${esc(t.assigned_agent)}</td><td><span class="pill ${pillClass(t.verification_status)}">${esc(t.verification_status)}</span></td><td>${esc(((t.spec || {}).dependencies || []).join(', '))}</td></tr>`).join('') + '</tbody></table>';
}

async function selectTask(uid) {
  location.hash = '#task';
  try {
    state.selected = await api('/api/tasks/' + encodeURIComponent(uid) + '?level=evidence');
    renderTask(); loadLogs();
  } catch (e) { toast('加载任务失败：' + e.message, true); }
}

function renderTask() {
  const t = state.selected;
  if (!t) {
    $('taskTitle').textContent = '未选择任务';
    $('taskBody').innerHTML = '<div class="muted">从任务图或列表中选择一个任务。</div>';
    $('taskActions').innerHTML = '';
    return;
  }
  $('taskTitle').textContent = `${t.task_id} · ${t.title}`;
  $('taskActions').innerHTML = `<span class="pill ${pillClass(t.status)}">${esc(t.status)}</span><span class="pill ${pillClass(t.verification_status)}">验证 ${esc(t.verification_status)}</span>`;
  const r = t.result || {};
  const ver = t.verification || {};
  $('taskBody').innerHTML = `
    <dl class="kv">
      <dt>目标</dt><dd>${esc(t.objective)}</dd>
      <dt>执行 Agent</dt><dd>${esc(t.assigned_agent || '未分配')}</dd>
      <dt>优先级</dt><dd>${esc(t.priority == null ? 5 : t.priority)}</dd>
      <dt>路由决策</dt><dd>${t.routing ? `${esc(t.routing.agent_id)} · 评分 ${esc(t.routing.score)} · ${esc(t.routing.reason)}` : '等待派发'}</dd>
      <dt>尝试次数</dt><dd>${t.attempts}</dd>
      <dt>Scope</dt><dd>${(t.scope || []).map((s) => `<span class="tag">${esc(s)}</span>`).join('') || '—'}</dd>
      <dt>禁止范围</dt><dd>${(t.out_of_scope || []).map((s) => `<span class="tag">${esc(s)}</span>`).join('') || '—'}</dd>
      <dt>验收条件</dt><dd>${(t.acceptance_criteria || []).map((s) => `<div>· ${esc(s)}</div>`).join('') || '—'}</dd>
      <dt>错误</dt><dd>${esc(t.error || '—')}</dd>
    </dl>
    <h3 style="margin-top:14px">成果摘要</h3>
    <div class="plan-box">${esc(r.summary || '（无成果包）')}<div class="sub">状态 ${esc(r.status || '—')} · valid=${esc(String(r.valid))}</div></div>
    ${(t.findings || []).length ? `<h3 style="margin-top:12px">发现</h3><table class="table"><thead><tr><th>严重级别</th><th>摘要</th></tr></thead><tbody>${t.findings.map((f) => `<tr><td>${esc(f.severity || '')}</td><td>${esc(f.summary || JSON.stringify(f))}</td></tr>`).join('')}</tbody></table>` : ''}
    ${(t.evidence || []).length ? `<h3 style="margin-top:12px">证据</h3><table class="table"><thead><tr><th>文件</th><th>行</th><th>说明</th></tr></thead><tbody>${t.evidence.map((e) => `<tr><td class="mono">${esc(e.file || '')}</td><td>${esc(e.lines || '')}</td><td>${esc(e.description || '')}</td></tr>`).join('')}</tbody></table>` : ''}
    ${(t.tests || []).length ? `<h3 style="margin-top:12px">测试</h3><table class="table"><thead><tr><th>命令</th><th>结果</th></tr></thead><tbody>${t.tests.map((x) => `<tr><td class="mono">${esc(x.command || '')}</td><td><span class="pill ${pillClass(x.result)}">${esc(x.result || '')}</span></td></tr>`).join('')}</tbody></table>` : ''}
    <h3 style="margin-top:12px">验证检查</h3>
    ${ver.checks ? `<div>${Object.entries(ver.checks).map(([k, v]) => `<div class="check"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join('')}</div>` : '<div class="muted">尚未验证</div>'}
    ${(ver.warnings || []).length ? `<div class="hint">警告：${ver.warnings.map(esc).join('；')}</div>` : ''}
    <div class="sub" style="margin-top:8px">Patch：${esc(t.patch_path || '无')}</div>`;
}

function renderApprovals() {
  const run = state.overview && state.overview.latest_run;
  const byKind = {};
  (state.approvals || []).filter((a) => a.status === 'pending').forEach((a) => {
    byKind[a.kind] = (byKind[a.kind] || 0) + 1;
  });
  $('governanceStatus').innerHTML = `
    <div class="check"><span>架构设计门禁</span><b>${byKind.architecture ? '待审批' : '无待办'}</b></div>
    <div class="check"><span>计划批准</span><b>${byKind.plan ? '待审批' : '无待办'}</b></div>
    <div class="check"><span>验收确认</span><b>${run ? esc(run.acceptance_status || 'pending') : '无运行'}</b></div>
    <div class="check"><span>代码合并</span><b>${byKind.merge ? '待审批' : '无待办'}</b></div>
    <div class="check"><span>发布审批</span><b>${run ? esc(run.release_status || 'not_requested') : '无运行'}</b></div>`;
  const list = state.approvals || [];
  $('approvalCards').innerHTML = list.length ? list.map((a) => `
    <div class="card">
      <div class="row" style="justify-content:space-between">
        <div><h3>${esc(a.subject)}</h3><div class="sub">${esc(a.kind)} · ${esc(a.approval_id)} · ${esc(a.status)} · ${esc(a.requested_at || '')}</div></div>
        <div class="row">${a.status === 'pending' ? `<button class="primary small" onclick="decideApproval('${esc(a.approval_id)}',true)">批准</button><button class="danger small" onclick="decideApproval('${esc(a.approval_id)}',false)">拒绝</button>` : `<span class="pill ${pillClass(a.status)}">${esc(a.status)}</span>`}</div>
      </div>
      <pre>${esc(JSON.stringify(a.details || {}, null, 2))}</pre>
    </div>`).join('') : '<div class="empty">没有审批事项</div>';
}

function renderBus() {
  const stats = state.busStats || {};
  const totals = stats.totals || {};
  $('busStats').innerHTML = `<dl class="kv">
    <dt>事件总数</dt><dd>${fmt(totals.events)}</dd>
    <dt>总线 Token</dt><dd>${fmt(totals.tokens)}</dd>
    <dt>最新序号</dt><dd>${esc(totals.last_seq || 0)}</dd>
    <dt>同步级别</dt><dd>${Object.entries(((state.sync || {}).levels || {})).map(([k, v]) => `<span class="pill">L${esc(k)} ${esc(v.memory_records)}</span>`).join('')}</dd>
  </dl>
  <div style="margin-top:8px">${(stats.by_kind || []).map((k) => `<span class="tag">${esc(k.kind)} · ${esc(k.events)}</span>`).join('') || '<span class="sub">暂无事件</span>'}</div>`;
  $('busSubs').innerHTML = (state.busSubs || []).length ? `<table class="table"><thead><tr><th>订阅者</th><th>主题</th><th>游标</th><th>ID</th></tr></thead><tbody>` +
    state.busSubs.map((s) => `<tr><td>${esc(s.subscriber)}</td><td>${esc((s.topics || []).join(',') || '全部')} / ${esc((s.kinds || []).join(',') || '全部')}</td><td>${esc(s.cursor_seq)}</td><td class="mono">${esc(s.subscription_id)}</td></tr>`).join('') + '</tbody></table>' : '<div class="empty">暂无订阅</div>';
  $('busEvents').innerHTML = (state.bus || []).length ? `<table class="table"><thead><tr><th>Seq</th><th>类型</th><th>主题</th><th>来源</th><th>关联</th><th>摘要</th></tr></thead><tbody>` +
    state.bus.slice(-80).reverse().map((e) => `<tr><td>${esc(e.seq)}</td><td><span class="tag">${esc(e.kind)}</span></td><td class="mono">${esc(e.topic)}</td><td>${esc(e.source)}</td><td class="mono">${esc(e.correlation_id)}</td><td class="mono">${esc(JSON.stringify(e.payload).slice(0, 220))}</td></tr>`).join('') + '</tbody></table>' : '<div class="empty">暂无事件</div>';
}

function renderAudit() {
  const c = state.costs || {};
  const t = c.totals || {};
  const opt = c.optimizer || {};
  const savings = opt.savings || {};
  const bench = opt.benchmark || [];
  $('costCards').innerHTML = `<dl class="kv">
    <dt>输入 tokens</dt><dd>${fmt(t.input_tokens)}</dd>
    <dt>输出 tokens</dt><dd>${fmt(t.output_tokens)}</dd>
    <dt>缓存 tokens</dt><dd>${fmt(t.cached_tokens)}</dd>
    <dt>总 tokens</dt><dd>${fmt(t.total_tokens)}</dd>
    <dt>费用</dt><dd>${money(t.cost)}</dd>
    <dt>项目预算</dt><dd>${fmt(c.project_budget)}（剩余 ${fmt(c.remaining)}）</dd>
  </dl>`;
  const st = savings.totals || {};
  const benchRows = bench.map((b) => `<tr><td>${esc(b.agent_id)}</td><td>${esc(b.score)}</td><td>${esc((b.snapshot || {}).success_rate)}</td><td>${esc((b.snapshot || {}).verification_pass_rate)}</td><td>${money((b.snapshot || {}).avg_cost)}</td><td>${fmt(Math.round((b.snapshot || {}).avg_duration_ms || 0))}ms</td></tr>`).join('');
  const benchHtml = bench.length ? ('<table class="table"><thead><tr><th>Agent</th><th>评分</th><th>成功率</th><th>验证通过</th><th>平均成本</th><th>平均耗时</th></tr></thead><tbody>' + benchRows + '</tbody></table>') : '<div class="empty">尚未运行基准评测</div>';
  const savingTags = (savings.by_kind || []).map((k) => `<span class="tag">${esc(k.kind)} ${fmt(k.tokens_saved)}</span>`).join('') || '暂无';
  $('optimizerCards').innerHTML = `<dl class="kv">
    <dt>累计节省</dt><dd>${fmt(st.tokens_saved)} tokens · ${money(st.cost_saved)}</dd>
    <dt>语义缓存</dt><dd>${esc((savings.semantic_cache || {}).entries || 0)} 条 · 命中 ${esc((savings.semantic_cache || {}).hits || 0)} 次</dd>
    <dt>节省来源</dt><dd>${savingTags}</dd>
  </dl><h3 style="margin-top:12px">Agent Benchmark</h3>${benchHtml}`;
  $('auditSummary').innerHTML = `<div class="sub">共 ${state.audit.length} 条已加载事件。审计链只追加，任何修改都会使校验失败。</div>`;
  $('usageTable').innerHTML = `<table class="table"><thead><tr><th>Agent</th><th>模型</th><th>Tokens</th><th>费用</th><th>重试</th></tr></thead><tbody>` +
    ((c.by_agent || []).map((r) => `<tr><td>${esc(r.agent_id)}</td><td>${esc(r.model)}</td><td>${fmt(r.total_tokens)}</td><td>${money(r.cost)}</td><td>${r.retries}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">无用量记录</td></tr>') + '</tbody></table>';
  $('auditEvents').innerHTML = `<table class="table"><thead><tr><th>时间</th><th>角色</th><th>动作</th><th>对象</th></tr></thead><tbody>` +
    (state.audit.slice(-80).reverse().map((e) => `<tr><td class="mono">${esc(e.ts)}</td><td>${esc(e.actor)}</td><td>${esc(e.action)}</td><td class="mono">${esc(e.subject)}</td></tr>`).join('') || '<tr><td colspan="4" class="muted">无审计事件</td></tr>') + '</tbody></table>';
  $('memoryList').innerHTML = (state.memories || []).slice(0, 40).map((m) => `
    <div class="check"><span><b>${esc(m.title)}</b><div class="sub">${esc(m.type)} · ${esc(m.status)} · 置信度 ${m.confidence}</div></span><span class="sub" style="max-width:48%">${esc((m.body || '').slice(0, 140))}</span></div>`).join('') || '<div class="empty">暂无记忆</div>';
}

function renderModules() {
  const ov = state.overview;
  if (!ov) { $('moduleStatus').innerHTML = ''; return; }
  const modules = ov.modules || {};
  const names = {
    connector: 'Agent Connector', unified_memory: 'Unified Memory', context_bus: 'Context Bus',
    task_scheduler: 'Task Scheduler', capability_router: 'Capability Router',
    cost_optimizer: 'Cost Optimizer', human_governance: 'Human Governance',
  };
  const sync = state.sync || {};
  $('moduleStatus').innerHTML = Object.entries(names).map(([k, label]) => (
    `<div class="check"><span><b>${esc(label)}</b><div class="sub">${esc(moduleDetail(k, ov, sync))}</div></span><span class="pill ok">${esc(modules[k] || 'ready')}</span></div>`
  )).join('');
}

function moduleDetail(k, ov, sync) {
  if (k === 'connector') return `${(ov.connectors || {}).count || 0} 个 Agent · ${(ov.connectors || {}).supported_products || 0} 类产品`;
  if (k === 'unified_memory') return `Level 1-5 已同步 ${Object.values((sync.levels || {})).reduce((n, x) => n + (x.memory_records || 0), 0)} 条记忆`;
  if (k === 'context_bus') return `${((ov.bus || {}).totals || {}).events || 0} 个事件 · 只追加哈希链`;
  if (k === 'task_scheduler') return `优先级 + 依赖 DAG · 并发 ${ov.config.max_parallel}`;
  if (k === 'capability_router') return '实时评估能力/成功率/验证/速度/成本';
  if (k === 'cost_optimizer') return `已节省 ${(((ov.cost_optimizer || {}).savings || {}).tokens_saved) || 0} tokens · 语义缓存 ${((ov.cost_optimizer || {}).semantic_cache || {}).entries || 0} 条`;
  if (k === 'human_governance') return `待处理审批 ${(ov.pending_approvals || []).length} 项`;
  return '';
}

function renderConversations() {
  $('conversations').innerHTML = (state.conversations || []).slice(0, 8).map((c) => (
    `<div class="check"><span>${esc(c.title || c.session_id)}</span><span class="sub">${esc(c.agent_id)} · ${c.message_count} 条</span></div>`
  )).join('') || '<div class="empty">暂无已导入会话</div>';
}

function renderLiveTasks() {
  const run = state.overview && state.overview.latest_run;
  $('streamRun').innerHTML = (state.runs || []).map((r) => (
    `<option value="${esc(r.run_id)}">${esc(r.run_id)} · ${esc(r.status)}</option>`
  )).join('') || '<option value="">（无运行）</option>';
  if (run) $('streamRun').value = run.run_id;
  const tasks = state.tasks || [];
  $('liveTasks').innerHTML = tasks.length ? `<table class="table"><thead><tr><th>任务</th><th>状态</th><th>Agent</th><th>验证</th><th>更新</th></tr></thead><tbody>` +
    tasks.map((t) => `<tr><td>${esc(t.task_id)} · ${esc(t.title)}</td><td><span class="pill ${pillClass(t.status)}">${esc(t.status)}</span></td><td>${esc(t.assigned_agent || '—')}</td><td>${esc(t.verification_status)}</td><td class="mono">${esc((t.updated_at || '').slice(11, 19))}</td></tr>`).join('') + '</tbody></table>' : '<div class="empty">暂无任务</div>';
}

/* ------------------------------------------------------------------ */
/* Live stream                                                         */
/* ------------------------------------------------------------------ */
function appendConsole(kind, text) {
  const box = $('liveConsole');
  if (!box) return;
  const line = document.createElement('div');
  line.className = kind;
  line.textContent = text;
  box.appendChild(line);
  while (box.childElementCount > 1600) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
  state.consoleLines += 1;
}
function clearConsole() { $('liveConsole').innerHTML = ''; state.consoleLines = 0; }

function connectStream() {
  if (!state.workspace || !state.workspace.open) { toast('请先打开项目', true); return; }
  disconnectStream();
  const run = ($('streamRun') && $('streamRun').value) || '';
  const url = '/api/stream?after_seq=' + state.lastSeq + '&run_id=' + encodeURIComponent(run);
  const es = new EventSource(url);
  state.stream = es;
  $('streamStatus').textContent = '连接中…';
  $('streamStatus').className = 'pill warn';
  es.addEventListener('hello', (e) => {
    $('streamStatus').textContent = '实时连接';
    $('streamStatus').className = 'pill ok';
    appendConsole('sys', '[系统] SSE 实时监控已连接');
    try { const d = JSON.parse(e.data); appendConsole('sys', '[系统] ' + (d.time || '')); } catch (err) {}
  });
  es.addEventListener('bus', (e) => {
    try {
      const ev = JSON.parse(e.data);
      state.lastSeq = Math.max(state.lastSeq, Number(ev.seq || 0));
      const payload = JSON.stringify(ev.payload || {});
      appendConsole('bus', `[${ev.seq}] ${ev.created_at || ''} ${ev.kind} ${ev.topic} ← ${ev.source} :: ${payload.slice(0, 300)}`);
      if (['task_state', 'workflow', 'governance', 'agent_status'].includes(ev.kind)) scheduleRefresh();
    } catch (err) {}
  });
  es.addEventListener('snapshot', (e) => {
    try { renderLiveSnapshot(JSON.parse(e.data)); } catch (err) {}
  });
  es.addEventListener('logs', (e) => {
    try {
      const d = JSON.parse(e.data);
      appendConsole('logs', `[日志 ${d.task_id} / ${d.agent_id}]\n${d.chunk}`);
    } catch (err) {}
  });
  es.addEventListener('ping', () => {
    $('streamStatus').textContent = '实时连接';
    $('streamStatus').className = 'pill ok';
  });
  es.onerror = () => {
    $('streamStatus').textContent = '重连中…';
    $('streamStatus').className = 'pill warn';
    try { es.close(); } catch (e) {}
    state.stream = null;
    if (state.reconnectTimer) return;
    state.reconnectTimer = setTimeout(() => {
      state.reconnectTimer = null;
      if (state.workspace && state.workspace.open && !state.stream) connectStream();
    }, 2500);
  };
}

function disconnectStream() {
  if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
  if (state.stream) { try { state.stream.close(); } catch (e) {} state.stream = null; }
  $('streamStatus').textContent = '未连接';
  $('streamStatus').className = 'pill';
}

function renderLiveSnapshot(s) {
  const run = s.run && s.run.run ? s.run.run : s.run;
  const counts = s.task_counts || {};
  const costs = s.costs || {};
  const totals = costs.totals || {};
  $('liveStats').innerHTML = `
    <div class="card" style="box-shadow:none;margin:0">
      <h3>运行 ${esc(run ? run.status : '—')}</h3>
      <div class="sub mono">${esc(run ? run.run_id : '无运行')}</div>
      <div style="margin-top:8px">${Object.entries(counts).map(([k, v]) => `<span class="pill ${pillClass(k)}">${esc(k)} ${v}</span>`).join('') || '无任务'}</div>
      <div class="sub" style="margin-top:8px">待审批 ${esc(s.pending_approvals || 0)} · 版本漂移 ${esc(s.drift_count || 0)} · 成本 ${money(totals.cost)}</div>
    </div>
    <div class="card" style="box-shadow:none;margin:0">
      <h3>Agent 在线状态</h3>
      ${(s.agents || []).map((a) => `<div class="check"><span>${esc(a.agent_id)}<div class="sub">v${esc(a.version)} · 模型 ${esc(a.selected_model || '未选配')}</div></span><span class="pill ${a.status === 'online' ? 'ok' : 'warn'}">${esc(a.status)}</span></div>`).join('') || '<div class="empty">无 Agent</div>'}
    </div>`;
  $('liveTasks').innerHTML = (s.tasks || []).length ? `<table class="table"><thead><tr><th>任务</th><th>状态</th><th>Agent</th><th>验证</th></tr></thead><tbody>` +
    s.tasks.map((t) => `<tr onclick="selectTask('${esc(t.uid)}')" style="cursor:pointer"><td>${esc(t.task_id)} · ${esc(t.title)}</td><td><span class="pill ${pillClass(t.status)}">${esc(t.status)}</span></td><td>${esc(t.agent || '—')}</td><td>${esc(t.verification)}</td></tr>`).join('') + '</tbody></table>' : '<div class="empty">暂无任务</div>';
}

/* ------------------------------------------------------------------ */
/* Actions                                                             */
/* ------------------------------------------------------------------ */
async function initWorkspace() {
  const path = $('wsPath').value.trim();
  if (!path) { toast('请输入项目目录', true); return; }
  try {
    const r = await post('/api/workspace/init', {
      path, name: $('wsName').value.trim(), create: true, git_init: true,
    });
    const p = (r.workspace || {}).project || {};
    $('wsPath').value = p.root || path;
    $('wsPath').dataset.touched = '1';
    toast('项目已初始化并打开');
    state.lastSeq = 0; clearConsole(); await refresh();
  } catch (e) { toast(e.message, true); }
}

async function openWorkspace() {
  const path = $('wsPath').value.trim();
  if (!path) { toast('请输入项目目录', true); return; }
  try {
    const r = await post('/api/workspace/open', { path, create: false, git_init: false });
    $('wsPath').value = (r.workspace || {}).root || path;
    $('wsPath').dataset.touched = '1';
    toast('项目已打开');
    state.lastSeq = 0; clearConsole(); await refresh();
  } catch (e) { toast(e.message, true); }
}

async function discoverAgents() {
  try {
    const r = await post('/api/agents/discover', {});
    toast('已探测 ' + (r.agents || []).length + ' 个 Agent');
    refresh();
  } catch (e) { toast(e.message, true); }
}

async function refreshModels() {
  try {
    const r = await post('/api/agents/models/refresh', {});
    toast('模型清单已刷新：' + r.agents.length + ' 个 Agent');
    refresh();
  } catch (e) { toast(e.message, true); }
}

async function requestPlan() {
  const goal = $('goal').value.trim();
  if (!goal) { toast('请输入需求 Prompt', true); return; }
  try {
    const body = { goal, async: true, agent_id: $('mainAgent').value || '',
      model: $('mainModelSelect').value || $('mainModel').value.trim() };
    const r = await post('/api/plans', body);
    $('jobStatus').textContent = '规划中… ' + r.job_id;
    let job;
    for (let i = 0; i < 400; i++) {
      await new Promise((res) => setTimeout(res, 1200));
      job = await api('/api/jobs/' + r.job_id);
      if (job.status !== 'running') break;
      $('jobStatus').textContent = '规划中… ' + (i * 1.2).toFixed(0) + 's';
    }
    $('jobStatus').textContent = '';
    if (!job || job.status === 'failed') {
      toast((job && job.error && job.error.message) || '规划失败', true); return;
    }
    const out = job.result || {};
    if (out.status === 'pending_approval') { toast('计划已生成，待审批'); refresh(); }
    else if (out.status === 'waiting_user') {
      $('planResult').innerHTML = `<div class="card"><h3>等待主节点 UI 执行</h3><div class="sub">提示词：${esc(out.prompt_path || '')}</div><pre>${esc(out.message || '')}</pre></div>`;
    } else { toast(out.error || '规划未完成', true); }
  } catch (e) { toast(e.message, true); }
}

async function importPlanPrompt() {
  const text = prompt('粘贴计划 JSON（或填写计划文件路径）：', '');
  if (!text) return;
  try {
    let body = {};
    try { body.plan = JSON.parse(text); } catch (e) { body.path = text; }
    await post('/api/plans/import', body);
    toast('计划已导入，待审批'); refresh();
  } catch (e) { toast(e.message, true); }
}

async function showPlan(id) {
  try {
    const p = await api('/api/plans/' + encodeURIComponent(id));
    $('plan-' + id).innerHTML = `<pre>${esc(JSON.stringify(p, null, 2))}</pre>`;
  } catch (e) { toast(e.message, true); }
}
async function approvePlan(id) {
  try { await post('/api/plans/' + encodeURIComponent(id) + '/approve', { approve: true }); toast('计划已批准，可开始运行'); refresh(); }
  catch (e) { toast(e.message, true); }
}
async function startRun(id) {
  try { await post('/api/runs/' + encodeURIComponent(id) + '/start', { background: true, max_parallel: 2 }); toast('运行已启动'); setTimeout(refresh, 1200); }
  catch (e) { toast(e.message, true); }
}
async function resumeRun(id) {
  try { await post('/api/runs/' + encodeURIComponent(id) + '/resume', { background: true, max_parallel: 2 }); toast('已继续运行'); setTimeout(refresh, 1200); }
  catch (e) { toast(e.message, true); }
}
async function mergeRun(id) {
  try {
    const approvals = state.approvals.filter((a) => a.run_id === id && a.kind === 'merge' && a.status === 'pending');
    const approval_id = approvals.length ? approvals[0].approval_id : '';
    await post('/api/merge', { run_id: id, approval_id });
    toast('合并完成'); refresh();
  } catch (e) { toast(e.message, true); }
}
async function releaseRun(id) {
  try { await post('/api/runs/' + encodeURIComponent(id) + '/release', {}); toast('发布审批已创建'); refresh(); }
  catch (e) { toast(e.message, true); }
}
async function decideApproval(id, approve) {
  try {
    const r = await post('/api/approvals/' + encodeURIComponent(id), { approve, note: '' });
    toast('审批已处理：' + ((r.outcome && r.outcome.status) || (approve ? 'approved' : 'rejected')));
    refresh();
  } catch (e) { toast(e.message, true); }
}
async function setMainAs(id) {
  $('mainAgent').value = id;
  onMainAgentChange();
  await saveMain();
}
async function healthAgent(id) {
  try {
    const h = await api('/api/agents/' + encodeURIComponent(id) + '/health');
    toast(`${id}: ${h.status} · 成功率 ${(((h.metrics || {}).success_rate || 0) * 100).toFixed(1)}%${h.tool_count ? ' · MCP 工具 ' + h.tool_count : ''}`);
    refresh();
  } catch (e) { toast(e.message, true); }
}
async function submitResult() {
  const t = state.selected; if (!t) { toast('先选择任务', true); return; }
  const text = $('resultInput').value.trim(); if (!text) { toast('请粘贴成果 JSON 或路径', true); return; }
  try {
    let body = {};
    try { body.payload = JSON.parse(text); } catch (e) { body.path = text; }
    await post('/api/tasks/' + encodeURIComponent(t.uid) + '/result', body);
    toast('成果包已提交');
    state.selected = await api('/api/tasks/' + encodeURIComponent(t.uid) + '?level=evidence');
    renderTask(); refresh();
  } catch (e) { toast(e.message, true); }
}
async function retryTask() {
  const t = state.selected; if (!t) return;
  try { await post('/api/tasks/' + encodeURIComponent(t.uid) + '/retry', {}); toast('任务已重新排队'); refresh(); }
  catch (e) { toast(e.message, true); }
}
async function cancelTask() {
  const t = state.selected; if (!t) return;
  try { await post('/api/tasks/' + encodeURIComponent(t.uid) + '/cancel', {}); toast('任务已取消'); refresh(); }
  catch (e) { toast(e.message, true); }
}
async function rollbackTask() {
  const t = state.selected; if (!t) return;
  if (!confirm('确认回滚该任务已应用的变更？')) return;
  try { await post('/api/rollback/task', { task_uid: t.uid }); toast('已回滚'); refresh(); }
  catch (e) { toast(e.message, true); }
}
async function loadPatch() {
  const t = state.selected; if (!t) return;
  try {
    const r = await api('/api/patches/' + encodeURIComponent(t.uid));
    const box = $('patchBox'); box.style.display = 'block'; box.textContent = r.patch;
  } catch (e) { toast(e.message, true); }
}
async function loadLogs() {
  const t = state.selected; if (!t) return;
  try {
    const r = await api('/api/tasks/' + encodeURIComponent(t.uid) + '/logs');
    $('logBox').textContent = r.logs || '（无日志）';
  } catch (e) { $('logBox').textContent = '（无日志）'; }
}
async function importSessions() {
  const agent = $('sessionAgent').value;
  if (!agent) { toast('没有支持会话读取的 Agent', true); return; }
  try {
    const r = await post('/api/sessions/import', { agent_id: agent, limit: Number($('sessionLimit').value || 10) });
    toast(`导入 ${r.imported} 个会话，跳过 ${r.skipped}`);
    refresh();
  } catch (e) { toast(e.message, true); }
}
async function verifyBus() {
  try {
    const r = await api('/api/bus/verify');
    toast(r.ok ? '总线哈希链完整（' + r.count + ' 条）' : '总线链损坏：' + r.errors.join('; '), !r.ok);
  } catch (e) { toast(e.message, true); }
}
async function refreshBus() {
  try {
    const [b, s, subs] = await Promise.all([api('/api/bus?limit=120'), api('/api/bus/stats'), api('/api/bus/subscriptions')]);
    state.bus = b.events || []; state.busStats = s; state.busSubs = subs.subscriptions || []; renderBus();
  } catch (e) { toast(e.message, true); }
}
async function subscribeBus() {
  const subscriber = $('busSubscriber').value.trim();
  if (!subscriber) { toast('请输入订阅者', true); return; }
  try {
    const r = await post('/api/bus/subscribe', { subscriber });
    $('busSubId').value = r.subscription_id; toast('订阅已创建：' + r.subscription_id); refreshBus();
  } catch (e) { toast(e.message, true); }
}
async function pollBus() {
  const id = $('busSubId').value.trim();
  if (!id) { toast('请填写订阅 ID', true); return; }
  try {
    const r = await post('/api/bus/poll', { subscription_id: id, limit: 100 });
    toast(`消费 ${r.count} 个事件，游标 ${r.cursor}`); refreshBus();
  } catch (e) { toast(e.message, true); }
}
async function verifyAudit() {
  try {
    const r = await api('/api/audit/verify');
    toast(r.ok ? '审计链完整（' + r.count + ' 条）' : '审计链损坏：' + r.errors.join('; '), !r.ok);
  } catch (e) { toast(e.message, true); }
}
async function runRecover() {
  try {
    const r = await post('/api/recover', {});
    toast('恢复完成，暂停任务 ' + (r.tasks || []).filter((t) => t.action === 'paused').length + ' 个');
    refresh();
  } catch (e) { toast(e.message, true); }
}
async function loadRecovery() {
  try {
    const r = await api('/api/recovery');
    $('auditSummary').innerHTML = `<pre>${esc(JSON.stringify(r, null, 2))}</pre>`;
  } catch (e) { toast(e.message, true); }
}

/* ------------------------------------------------------------------ */
/* Render orchestrator / boot                                          */
/* ------------------------------------------------------------------ */
function render() {
  renderHeader();
  renderWorkspace();
  if (state.overview) {
    renderCommandAgents();
    renderCommandApprovals();
    renderCommandRun();
    renderCommandPlans();
    renderLiveTasks();
    const sa = $('sessionAgent');
    const sessionAgents = (state.overview.agents.list || []).filter((a) => a.capabilities && a.capabilities.session_read);
    const current = sa.value;
    sa.innerHTML = sessionAgents.map((a) => `<option value="${esc(a.agent_id)}">${esc(a.name)}</option>`).join('');
    if (current && sessionAgents.some((a) => a.agent_id === current)) sa.value = current;
  }
  renderAgents();
  renderSurface();
  renderVersions();
  renderGraph();
  renderTask();
  renderApprovals();
  renderBus();
  renderAudit();
  renderModules();
  renderConversations();
}

$('wsPath').addEventListener('input', () => { $('wsPath').dataset.touched = '1'; });
$('budgetTokens').addEventListener('input', () => { $('budgetTokens').dataset.touched = '1'; });
window.addEventListener('hashchange', route);
route();
refresh();
setInterval(() => {
  if (state.workspace && state.workspace.open && !state.stream
      && ['agents', 'graph', 'task', 'approvals', 'bus', 'audit', 'versions'].includes(state.view)) {
    refresh();
  }
}, 12000);
