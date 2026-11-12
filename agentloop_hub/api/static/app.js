/* AgentMesh OS unified web console. */
const state = {
  workspace: null, overview: null, plans: [], runs: [], tasks: [], approvals: [],
  costs: null, usage: [], audit: [], conversations: [], memories: [], bus: [],
  busStats: null, busSubs: [], sync: null, models: null, versionStatus: null,
  modelsSync: null, settings: null,
  surface: null,
  roles: null,
  mcp: null, selected: null, view: 'command', stream: null, lastSeq: 0,
  consoleLines: 0, reconnectTimer: null, refreshTimer: null,
  chainDraft: null, chainRunId: '', chainTimer: null,
  chainHydratedFor: '', chainPersistTimer: null, manualAlertKey: '',
  bindingTask: null,
  summaryRun: '', adoptTask: null, textInputCallback: null,
  // Which control last changed the main model.  Save and plan generation must
  // read the same canonical value, not two competing fields.
  mainModelSource: 'select',
  chainDraftStatus: 'saved', chainConflict: false,
  // Incremented on every project switch.  Late responses from the previous
  // project are discarded instead of overwriting the new view.
  wsGeneration: 0,
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

let controlToken = '';
async function ensureControlToken() {
  if (controlToken) return controlToken;
  const res = await fetch('/api/session', { cache: 'no-store' });
  const data = await res.json();
  controlToken = data.token || '';
  return controlToken;
}
async function api(path, opts = {}) {
  const token = await ensureControlToken();
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (token) headers['X-AgentMesh-Token'] = token;
  const res = await fetch(path, { ...opts, headers });
  let data;
  try { data = await res.json(); } catch (e) { data = { error: { message: '响应不是 JSON' } }; }
  if (!res.ok) throw new Error((data.error && data.error.message) || ('HTTP ' + res.status));
  return data;
}
function post(path, body) { return api(path, { method: 'POST', body: JSON.stringify(body || {}) }); }

/* ------------------------------------------------------------------ */
/* Full control (GUI permission auto-accept)                           */
/* ------------------------------------------------------------------ */
function renderFullControl() {
  const box = $('fullControl');
  if (!box) return;
  const enabled = !state.settings || state.settings.full_control !== false;
  box.checked = enabled;
  const info = $('fullControlInfo');
  if (info) {
    info.textContent = enabled
      ? '已开启：GUI Agent 的越界读取/命令确认将通过应用自身接口自动接受'
      : '已关闭：确认会留在 Agent 应用内，由你手动处理';
  }
}
async function saveFullControl() {
  const enabled = !!($('fullControl') && $('fullControl').checked);
  try {
    const r = await post('/api/settings/full_control', { enabled });
    state.settings = { ...(state.settings || {}), ...r };
    renderFullControl();
    toast(enabled ? '完全控制已开启' : '完全控制已关闭');
  } catch (e) {
    toast('保存失败：' + e.message, true);
    renderFullControl();
  }
}

/* Multi-round continuation (default on, bounded rounds) */
function renderMultiRound() {
  const box = $('multiRound');
  if (!box) return;
  const settings = state.settings || {};
  const enabled = settings.multi_round !== false;
  box.checked = enabled;
  const maxInput = $('maxRounds');
  if (maxInput && settings.max_rounds) maxInput.value = settings.max_rounds;
  const info = $('multiRoundInfo');
  if (info) {
    info.textContent = enabled
      ? `已开启：最多 ${settings.max_rounds || 3} 轮（含首轮）；未通过时自动重新规划下一轮`
      : '已关闭：一轮结束即进入验收/合并门禁';
  }
}
async function saveMultiRound() {
  const enabled = !!($('multiRound') && $('multiRound').checked);
  const maxRounds = Math.max(1, Math.min(20, Number($('maxRounds') && $('maxRounds').value || 3)));
  try {
    const r = await post('/api/settings/multi_round', { enabled, max_rounds: maxRounds });
    state.settings = { ...(state.settings || {}), ...r };
    renderMultiRound();
    toast(enabled ? `多轮自动续跑已开启（最多 ${maxRounds} 轮）` : '多轮自动续跑已关闭');
  } catch (e) {
    toast('保存失败：' + e.message, true);
    renderMultiRound();
  }
}

/* ------------------------------------------------------------------ */
/* Routing / refresh                                                   */
/* ------------------------------------------------------------------ */
function route() {
  const hash = (location.hash || '#command').slice(1);
  const views = ['command', 'agents', 'versions', 'graph', 'roles', 'task', 'approvals', 'bus', 'audit'];
  state.view = views.includes(hash) ? hash : 'command';
  document.querySelectorAll('.section').forEach((s) => s.classList.remove('active'));
  const target = $('view-' + state.view);
  if (target) target.classList.add('active');
  document.querySelectorAll('#nav a').forEach((a) => (
    a.classList.toggle('active', a.dataset.view === state.view)));
  render();
}

async function refresh() {
  const generation = state.wsGeneration;
  try {
    const ws = await api('/api/workspace');
    if (generation !== state.wsGeneration) return;
    state.workspace = ws;
    state.settings = await api('/api/settings').catch(() => null);
    if (generation !== state.wsGeneration) return;
    if (!ws.open) {
      state.overview = null; state.models = null; state.versionStatus = null;
      render();
      return;
    }
    const [ov, plans, runs, tasks, approvals, costs, usage, audit, conversations, memory,
      bus, busStats, busSubs, sync, models, versionStatus, modelsSync] = await Promise.all([
      api('/api/overview'), api('/api/plans'), api('/api/runs'), api('/api/tasks'),
      api('/api/approvals'), api('/api/costs'), api('/api/costs'), api('/api/audit?limit=300'),
      api('/api/conversations'), api('/api/memory'), api('/api/bus?limit=120'),
      api('/api/bus/stats'), api('/api/bus/subscriptions'), api('/api/sync'),
      api('/api/agents/models'), api('/api/agents/version-status'),
      api('/api/agents/models/sync').catch(() => null),
    ]);
    if (generation !== state.wsGeneration) return;
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
    state.modelsSync = modelsSync;
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
  const gitTop = String((p.extra || {}).git_toplevel || '');
  const norm = (value) => String(value || '').replace(/[\\/]+/g, '/').replace(/\/$/, '').toLowerCase();
  if (gitTop && norm(gitTop) !== norm(p.root)) {
    $('wsInfo').innerHTML += `<div class="banner" style="margin-top:8px;background:#fff7ed;color:#9a3412">
      <b>注意：</b>当前项目目录位于 Git 仓库子目录中。Git 根是
      <span class="mono">${esc(gitTop)}</span>，任务 Worktree 会从 Git 根创建。
      建议直接打开 Git 根目录，避免 Agent 看到“项目根”与 Git 根不一致。</div>`;
  }
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
    const baseLabel = m.display_name || m.name;
    // Two real ids can share one display label; keep the id visible so the
    // picker is not ambiguous.
    const label = (m.display_name && m.display_name !== m.name)
      ? `${baseLabel} (${m.name})` : baseLabel;
    const rate = (m.rate != null) ? ` · x${m.rate}` : '';
    const degraded = ((m.health || {}).status === 'degraded')
      ? ` · ⚠${(m.health || {}).reason || '已降级'}` : '';
    html += `<option value="${esc(m.name)}" ${m.name === selected ? 'selected' : ''}>${esc(label)}${rate}${esc(degraded)} · ${esc(m.source_label || m.source)}</option>`;
  });
  if (selected && !names.includes(selected)) {
    html += `<option value="${esc(selected)}" selected>${esc(selected)} · 手动</option>`;
  }
  return html;
}

const CAPABILITY_LABELS = {
  none: '关闭思考', minimal: '最小', light: '轻', low: '低', medium: '中',
  high: '高', xhigh: '极高', extra_high: '极高', max: '最大', ultra: '极高',
};
function modelProfile(agentId, model) {
  const entry = ((state.models || {}).by_agent || {})[agentId] || {};
  const profiles = entry.model_profiles || {};
  if (profiles[model]) return profiles[model];
  const row = (entry.models || []).find((m) => m.name === model) || {};
  if (model && (row.context_default || row.capability_default)) {
    return {
      context_length: row.context_default || 0,
      capability_level: row.capability_default || '',
    };
  }
  return {
    context_length: entry.context_length || 0,
    capability_level: entry.capability_level || '',
  };
}
function modelMeta(agentId, model) {
  const entry = ((state.models || {}).by_agent || {})[agentId] || {};
  const row = (entry.models || []).find((m) => m.name === model) || {};
  const fallback = !model ? entry : {};
  return {
    contextOptions: (row.context_options || fallback.context_options || []).map(Number),
    contextMax: Number(row.context_max || fallback.context_max || 0),
    capabilityOptions: row.capability_options || fallback.capability_options || [],
    capabilityLabels: row.capability_labels || fallback.capability_labels || {},
  };
}
function capabilityLabel(level, labels) {
  return (labels && labels[level]) || CAPABILITY_LABELS[level] || level;
}
function capabilitySelectHtml(agentId, model, selected) {
  const meta = modelMeta(agentId, model);
  let options = (meta.capabilityOptions || []).slice();
  if (!options.length) options = ['low', 'medium', 'high', 'ultra'];
  if (selected && !options.includes(selected)) options.unshift(selected);
  return ['<option value="">默认</option>'].concat(options.map((level) =>
    `<option value="${esc(level)}" ${level === selected ? 'selected' : ''}>${esc(capabilityLabel(level, meta.capabilityLabels))}</option>`)).join('');
}
function contextDatalistHtml(id, meta) {
  return `<datalist id="${esc(id)}">${(meta.contextOptions || []).map((value) =>
    `<option value="${Number(value)}"></option>`).join('')}</datalist>`;
}
function contextTitle(meta) {
  if (meta.contextOptions && meta.contextOptions.length) {
    return `该模型的上下文选项：${meta.contextOptions.join(' / ')}`;
  }
  return meta.contextMax ? `该模型的上下文上限：${meta.contextMax}` : '上下文长度（0=默认）';
}
function applyContextMeta(ctx, meta) {
  if (!ctx) return;
  ctx.setAttribute('list', ctx.id + 'Options');
  let list = document.getElementById(ctx.id + 'Options');
  if (!list) {
    list = document.createElement('datalist');
    list.id = ctx.id + 'Options';
    document.body.appendChild(list);
  }
  list.innerHTML = (meta.contextOptions || []).map((value) =>
    `<option value="${Number(value)}"></option>`).join('');
  ctx.title = contextTitle(meta);
  if (meta.contextMax) ctx.max = meta.contextMax; else ctx.removeAttribute('max');
  if (meta.contextOptions && meta.contextOptions.length) {
    ctx.placeholder = `可选 ${meta.contextOptions.join(' / ')}`;
  } else if (meta.contextMax) {
    ctx.placeholder = `≤ ${meta.contextMax}`;
  }
}
function validateProfileInputs(agentId, model, context, capability) {
  const meta = modelMeta(agentId, model);
  if (context > 0 && meta.contextOptions.length && !meta.contextOptions.includes(context)) {
    throw new Error(`该模型上下文只支持 ${meta.contextOptions.join(' / ')}`);
  }
  if (context > 0 && meta.contextMax && context > meta.contextMax) {
    throw new Error(`该模型上下文上限为 ${meta.contextMax}`);
  }
  if (capability && meta.capabilityOptions.length
      && !meta.capabilityOptions.includes(capability)) {
    throw new Error(`该模型能力等级只支持 ${meta.capabilityOptions.join(' / ')}`);
  }
}
function fillProfileInputs(contextId, capabilityId, agentId, model, overrides = {}) {
  const profile = modelProfile(agentId, model);
  const meta = modelMeta(agentId, model);
  const ctx = $(contextId);
  const cap = $(capabilityId);
  if (ctx) {
    if (document.activeElement !== ctx && overrides.context !== undefined) {
      ctx.value = overrides.context;
    } else if (document.activeElement !== ctx) {
      ctx.value = profile.context_length || 0;
    }
    applyContextMeta(ctx, meta);
  }
  if (cap) {
    const selected = overrides.capability !== undefined
      ? overrides.capability : (profile.capability_level || '');
    cap.innerHTML = capabilitySelectHtml(agentId, model, selected);
    cap.value = selected;
  }
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
  const modelInput = $('mainModel');
  if (modelInput && !modelInput.dataset.bound) {
    modelInput.dataset.bound = '1';
    modelInput.addEventListener('input', onMainModelInput);
  }
  if (main && !$('mainModel').value) $('mainModel').value = main.model || '';
  const mainModel = currentMainModel() || (main ? main.model : '');
  fillProfileInputs('mainContext', 'mainCapability', selectedMain, mainModel);
  $('mainBudget').value = main ? (main.budget_tokens || 0) : 0;
  const mainWarning = mainEntry.profile_warning || '';
  $('mainInfo').innerHTML = main
    ? `<span class="pill ok">${esc(main.agent_id)}</span> 模型 ${esc(main.model || '未指定')} · 设置于 ${esc(main.set_at || '')}`
      + (mainWarning ? `<div style="color:#b45309">配置警告：${esc(mainWarning)}，请按真实选项重新保存</div>` : '')
    : '尚未指定主节点。选择一个 Agent 并保存。';

  const mainModelCount = ((state.models || {}).models || []).length;
  $('modelSummary').textContent = `${mainModelCount} 个模型 / ${agents.length} 个 Agent`;
  $('subAgents').innerHTML = agents.map((a) => {
    const entry = ((state.models || {}).by_agent || {})[a.agent_id] || {};
    const selected = entry.selected_model || '';
    const isMain = a.agent_id === mainId;
    const profile = modelProfile(a.agent_id, selected);
    const meta = modelMeta(a.agent_id, selected);
    const selectedRow = (entry.models || []).find((m) => m.name === selected) || {};
    const warning = selectedRow.profile_warning || '';
    return `<div class="models-row">
      <div><b>${esc(a.name)}</b> <span class="pill ${isMain ? 'ok' : ''}">${isMain ? '主' : '子'}</span>
        <div class="sub">${esc(a.agent_id)} · v${esc(a.version)} · ${esc(a.status)} · ${esc(a.adapter_type)}</div></div>
      <select id="model-${esc(a.agent_id)}" onchange="onSubModelChange('${esc(a.agent_id)}')">${modelOptions(a.agent_id, selected)}</select>
      <input id="custom-${esc(a.agent_id)}" placeholder="或手填模型名">
      <input id="context-${esc(a.agent_id)}" type="number" min="0" list="context-${esc(a.agent_id)}Options" placeholder="上下文 tokens" title="${esc(contextTitle(meta))}" value="${esc(profile.context_length || 0)}">
      ${contextDatalistHtml('context-' + a.agent_id + 'Options', meta)}
      <select id="capability-${esc(a.agent_id)}" title="能力等级（来自该模型真实选项）">
        ${capabilitySelectHtml(a.agent_id, selected, profile.capability_level || '')}
      </select>
      <button class="small" onclick="saveSubModel('${esc(a.agent_id)}')">保存</button>
      <span class="sub">${selected ? '已选 ' + esc(selected) : '未选配'}
        ${warning ? `<span style="color:#b45309"> · 配置警告：${esc(warning)}</span>` : ''}</span>
    </div>`;
  }).join('') || '<div class="empty">没有 Agent。点击“重新探测 Agent”。</div>';
}

function onSubModelChange(agentId) {
  const model = $('model-' + agentId) ? $('model-' + agentId).value : '';
  fillProfileInputs('context-' + agentId, 'capability-' + agentId, agentId, model);
}
function onMainAgentChange() {
  const id = $('mainAgent').value;
  const entry = ((state.models || {}).by_agent || {})[id] || {};
  $('mainModelSelect').innerHTML = modelOptions(id, entry.selected_model || '');
  $('mainModel').value = entry.selected_model || '';
  fillProfileInputs('mainContext', 'mainCapability', id,
    $('mainModelSelect').value || entry.selected_model || '');
  $('mainInfo').innerHTML = entry.models && entry.models.length
    ? `该 Agent 已探测到 ${entry.models.length} 个模型：${entry.models.map((m) => esc(m.name)
        + ((m.health || {}).status === 'degraded' ? '（已降级）' : '')).join(', ')}`
    : '该 Agent 暂未探测到模型；可手填模型名。';
}
function syncMainCustom() {
  state.mainModelSource = 'select';
  $('mainModel').value = $('mainModelSelect').value;
  fillProfileInputs('mainContext', 'mainCapability', $('mainAgent').value,
    $('mainModelSelect').value);
}
function onMainModelInput() {
  state.mainModelSource = 'input';
}
function currentMainModel() {
  const typed = ($('mainModel') && $('mainModel').value || '').trim();
  const selected = ($('mainModelSelect') && $('mainModelSelect').value) || '';
  if (state.mainModelSource === 'input') return typed || selected;
  return selected || typed;
}

async function autoMain() {
  try {
    const r = await post('/api/main/auto', { model: currentMainModel() });
    toast(`自动主节点：${r.agent_id} · ${r.model}`);
    refresh();
  } catch (e) { toast(e.message, true); }
}

async function pauseRun(runId) {
  try {
    await post('/api/runs/' + encodeURIComponent(runId) + '/pause', { reason: '用户暂停' });
    toast('已暂停：当前任务会跑完，后续任务不再派发');
    refresh();
  } catch (e) { toast('暂停失败：' + e.message, true); }
}

async function cancelRun(runId) {
  if (!confirm('停止整个运行？排队/运行中的节点会取消，WorkBuddy/Trae 当前任务会被停止。')) return;
  try {
    const r = await post('/api/runs/' + encodeURIComponent(runId) + '/cancel',
      { reason: '用户停止运行' });
    toast(`运行已停止：${(r.tasks || []).length} 个节点取消`);
    refresh();
  } catch (e) { toast('停止失败：' + e.message, true); }
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
  const model = currentMainModel();
  const contextLength = Math.max(0, Number($('mainContext').value || 0));
  const capabilityLevel = $('mainCapability').value || '';
  try {
    validateProfileInputs(agent, model, contextLength, capabilityLevel);
    await post('/api/main', {
      agent_id: agent, model, budget_tokens: Number($('mainBudget').value || 0),
      context_length: contextLength, capability_level: capabilityLevel,
    });
    toast('主节点、模型、上下文与能力等级已保存');
    refresh();
  } catch (e) { toast(e.message, true); }
}

async function saveSubModel(agentId) {
  const select = $('model-' + agentId);
  const custom = $('custom-' + agentId);
  const model = (custom && custom.value.trim()) || (select && select.value) || '';
  const contextLength = Math.max(0, Number(
    ($('context-' + agentId) && $('context-' + agentId).value) || 0));
  const capabilityLevel = ($('capability-' + agentId) && $('capability-' + agentId).value) || '';
  try {
    validateProfileInputs(agentId, model, contextLength, capabilityLevel);
    await post('/api/agents/' + encodeURIComponent(agentId) + '/model', {
      model, context_length: contextLength, capability_level: capabilityLevel,
    });
    toast(`${agentId} 已保存模型/上下文/能力等级`);
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
  (state.tasks || []).filter((t) => t.run_id === run.run_id)
    .forEach((t) => { counts[t.status] = (counts[t.status] || 0) + 1; });
  const manualSubmit = (((state.overview || {}).manual_actions) || []).some(
    (a) => ((a.manual_action || {}).kind === 'manual_submit'));
  const pendingHighRisk = (((state.overview || {}).pending_approvals) || []).filter(
    (a) => a.status === 'pending' && a.kind === 'high_risk');
  $('commandRun').innerHTML = `<dl class="kv">
    <dt>运行 ID</dt><dd class="mono">${esc(run.run_id)}</dd>
    <dt>状态</dt><dd><span class="pill ${pillClass(run.status)}">${esc(run.status)}</span></dd>
    <dt>轮次</dt><dd>第 ${esc(run.round_number || 1)} / ${esc(run.max_rounds || '—')} 轮 · ${esc(run.goal_status || 'active')}${run.blocked_reason ? ` · <span class="sub">${esc(run.blocked_reason)}</span>` : ''}</dd>
    <dt>主节点</dt><dd>${esc(run.main_agent_id)} · ${esc(run.model || '')}</dd>
    <dt>任务</dt><dd>${Object.entries(counts).map(([k, v]) => `<span class="pill ${pillClass(k)}">${esc(k)} ${v}</span>`).join('') || '无'}</dd>
    <dt>验收 / 发布</dt><dd>${esc(run.acceptance_status || 'pending')} / ${esc(run.release_status || 'not_requested')}</dd>
  </dl>
  <div class="row" style="margin-top:10px">
    ${run.status === 'ready' ? `<button class="primary" onclick="startRun('${esc(run.run_id)}')">开始执行</button>` : ''}
    ${run.status === 'running' ? `<button class="danger" onclick="pauseRun('${esc(run.run_id)}')">暂停运行</button><button onclick="refresh()">刷新进度</button>` : ''}
    ${['running', 'waiting_user', 'paused'].includes(run.status)
      ? `<button class="danger" onclick="cancelRun('${esc(run.run_id)}')">停止运行（取消节点）</button>` : ''}
    ${['waiting_user', 'paused'].includes(run.status) && !manualSubmit && !pendingHighRisk.length ? `<button class="primary" onclick="resumeRun('${esc(run.run_id)}')">继续运行</button>` : ''}
    ${['running', 'waiting_user', 'paused'].includes(run.status) ? `<button onclick="openSummaryModal('${esc(run.run_id)}')">回填汇总 JSON</button>` : ''}
    ${manualSubmit ? '<span class="sub">已阻塞：请按弹窗提示在 Agent 应用中点击发送，完成后会自动继续</span>' : ''}
    ${pendingHighRisk.length ? '<span class="sub">存在待批准的高风险操作，请在上方弹窗点击“批准高风险并继续”</span>' : ''}
    ${run.status === 'pending_acceptance' ? `<button onclick="location.hash='#approvals'">验收确认</button>` : ''}
    ${run.status === 'pending_merge' ? `<button class="primary" onclick="mergeRun('${esc(run.run_id)}')">批准并合并</button>` : ''}
    ${run.status === 'completed' ? `<button class="primary" onclick="releaseRun('${esc(run.run_id)}')">申请发布审批</button>` : ''}
    <button onclick="location.hash='#graph'">任务图</button>
  </div>`;
}

function renderMainActivity() {
  const box = $('mainActivity');
  if (!box) return;
  const activity = (state.overview || {}).main_activity || {};
  if (!activity || !activity.phase) {
    box.innerHTML = '<div class="empty">主节点没有正在进行的规划或汇总</div>';
    return;
  }
  const phase = activity.phase === 'summary'
    ? '主节点汇总'
    : `主节点第 ${activity.round || '?'} 轮规划`;
  const status = activity.status || '';
  const started = activity.started_at ? new Date(activity.started_at).getTime() : 0;
  const finished = activity.finished_at ? new Date(activity.finished_at).getTime() : 0;
  const elapsed = started ? Math.max(0, Math.round(((finished || Date.now()) - started) / 1000)) : 0;
  box.innerHTML = `<div class="card" style="box-shadow:none;margin:0">
    <div class="row" style="justify-content:space-between">
      <div><b>${esc(phase)}</b>
        <span class="pill ${pillClass(status)}">${esc(status)}</span>
        <div class="sub">${esc(activity.agent_name || activity.agent_id || '')} · ${esc(activity.model || '')}
          · 已运行 ${elapsed}s</div></div>
      <span class="pill info">${esc(activity.task_id || 'MAIN')}</span>
    </div>
    ${activity.error ? `<div class="sub" style="color:#b91c1c;margin-top:6px">${esc(activity.error)}</div>` : ''}
    ${activity.prompt_path ? `<div class="sub mono" style="margin-top:6px;word-break:break-all">提示词：${esc(activity.prompt_path)}</div>` : ''}
  </div>`;
}

function renderCommandChainNodes() {
  const box = $('commandChainNodes');
  if (!box) return;
  const run = state.overview && state.overview.latest_run;
  if (!run) { box.innerHTML = '<div class="empty">暂无运行</div>'; return; }
  const tasks = (state.tasks || []).filter((t) => t.run_id === run.run_id);
  if (!tasks.length) { box.innerHTML = '<div class="empty">本次运行还没有任务</div>'; return; }
  const manualByUid = Object.fromEntries(
    (((state.overview || {}).manual_actions) || []).map((a) => [a.task_uid, a.manual_action || {}]));
  const rows = tasks.map((t) => {
    const manualKind = (manualByUid[t.uid] || {}).kind || '';
    const submissionUnconfirmed = String(t.error || '').startsWith('SUBMISSION_');
    const canRetry = !submissionUnconfirmed && (
      ['failed', 'blocked'].includes(t.status)
      || (t.status === 'waiting_user' && manualKind !== 'manual_submit'));
    const manual = String(t.error || '').startsWith('MANUAL_ACTION_REQUIRED');
    const started = t.started_at ? new Date(t.started_at).getTime() : 0;
    const finished = t.finished_at ? new Date(t.finished_at).getTime() : 0;
    const elapsed = started ? Math.max(0, Math.round(((finished || Date.now()) - started) / 1000)) : 0;
    const limitMin = (t.spec && t.spec.time_limit_minutes) || 0;
    const elapsedText = started
      ? `${Math.floor(elapsed / 60)}m${String(elapsed % 60).padStart(2, '0')}s / ${limitMin || '—'}m`
      : `— / ${limitMin || '—'}m`;
    return `<tr>
      <td class="mono">${esc(t.task_id)}</td>
      <td>${esc(t.assigned_agent || (t.spec && t.spec.suggested_agent) || '')}</td>
      <td class="mono">${esc(t.model || '')}</td>
      <td><span class="pill ${pillClass(t.status)}">${esc(t.status)}</span>
        ${t.verification_status === 'warning' ? '<span class="pill warn" title="验证通过但有警告，详见验证报告">验证警告</span>' : ''}</td>
      <td class="sub">${esc(elapsedText)}</td>
      <td class="sub">${esc((t.error || (t.verification_status === 'warning'
        ? '验证通过但有警告（可能是 scope 超出计划范围），详见任务详情' : '')).slice(0, 160))}</td>
      <td>${!['completed', 'canceled', 'rejected'].includes(t.status)
        ? `<button class="small" onclick="openBindingEditor('${esc(t.uid)}')">编辑绑定</button>` : ''}
        ${['waiting_user', 'failed', 'blocked', 'paused'].includes(t.status)
          ? `<button class="small" onclick="openAdoptModal('${esc(t.uid)}')">采纳成果</button>` : ''}
        ${submissionUnconfirmed ? `<button class="primary small" onclick="reconcileNode('${esc(t.uid)}','${esc(run.run_id)}')">核对原请求</button>` : ''}
        ${canRetry ? `<button class="small" onclick="retryNode('${esc(t.uid)}','${esc(run.run_id)}')">重试节点</button>` : ''}
        ${t.status === 'failed' ? `<button class="small" onclick="reverifyNode('${esc(t.uid)}')">重新验证</button>` : ''}
        ${manual ? `<button class="small" onclick="refresh()">刷新状态</button>` : ''}</td>
    </tr>`;
  }).join('');
  box.innerHTML = `<div class="sub" style="margin-bottom:6px">运行 <span class="mono">${esc(run.run_id)}</span>
    · <span class="pill ${pillClass(run.status)}">${esc(run.status)}</span>
    ${run.blocked_reason ? ` · ${esc(run.blocked_reason)}` : ''}</div>
    <table class="table"><thead><tr><th>节点</th><th>Agent</th><th>模型</th>
    <th>状态</th><th>耗时/上限</th><th>错误/阻塞原因</th><th>操作</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderBlockAlert() {
  const box = $('blockAlert');
  if (!box) return;
  const taskByUid = Object.fromEntries((state.tasks || []).map((t) => [t.uid, t]));
  const runStatus = ((state.overview || {}).latest_run || {}).status || '';
  const liveRun = ['running', 'waiting_user', 'paused'].includes(runStatus);
  // 2026-11-07 defect #10: cards for SUMMARY/ROUNDx_PLAN stayed after the
  // synthetic node finished.  Only actions that are still actionable survive.
  const actions = (((state.overview || {}).manual_actions) || []).filter((a) => {
    const t = taskByUid[a.task_uid];
    if (t) return ['waiting_user', 'failed', 'blocked', 'paused', 'running']
      .includes(t.status);
    return liveRun;
  });
  const highRisk = (((state.overview || {}).pending_approvals) || []).filter(
    (a) => a.status === 'pending' && a.kind === 'high_risk');
  if (!actions.length && !highRisk.length) {
    box.classList.add('hidden');
    box.innerHTML = '';
    if (document.title.startsWith('⚠')) document.title = 'AgentMesh OS 指挥中心';
    return;
  }
  const run = state.overview.latest_run || {};
  const key = actions.map((a) => `${a.task_uid}:${(a.manual_action || {}).kind}:${(a.manual_action || {}).since}`).join('|')
    + '||' + highRisk.map((a) => a.approval_id).join(',');
  if (key !== state.manualAlertKey) {
    state.manualAlertKey = key;
    toast('链路已阻塞：请处理弹窗中的审批或 Agent 节点', true);
    document.title = '⚠ 链路阻塞 - AgentMesh OS';
  }
  const approvalItems = highRisk.map((a) => {
    const d = a.details || {};
    return `<div class="item" style="border-left:3px solid #b45309;padding-left:10px">
      <div><b>${esc(a.task_uid || a.subject || a.approval_id)}</b>
        <span class="pill warn">高风险操作待批准</span></div>
      <div class="sub"><b>将要执行：</b>${esc(d.what || a.subject || '')}</div>
      <div class="sub"><b>风险：</b>${esc(d.why || '')} ${esc(d.risk_level || '')}</div>
      ${(d.files || []).length ? `<div class="sub mono">文件范围：${esc((d.files || []).join(', '))}</div>` : ''}
      <div class="row" style="margin-top:6px">
        <button class="primary small" onclick="approveHighRiskAndResume('${esc(a.approval_id)}','${esc(run.run_id || '')}')">批准高风险并继续</button>
        <button class="danger small" onclick="decideApproval('${esc(a.approval_id)}',false)">拒绝</button>
        <button class="small" onclick="refresh()">刷新状态</button>
      </div>
    </div>`;
  }).join('');
  const items = actions.map((a) => {
    const manual = a.manual_action || {};
    const kind = manual.kind || '';
    const synthetic = !!a.synthetic;
    const restart = !synthetic
      && (kind === 'restart_required' || manual.error_code === 'APP_RESTART_REQUIRED');
    return `<div class="item">
      <div><b>${esc(a.task_id)}</b> · ${esc(a.agent_name || a.agent_id || '')}
        <span class="pill warn">${esc(kind || '需要人工处理')}</span></div>
      <div class="sub">${esc(a.error || manual.message || '')}</div>
      <div style="margin:6px 0">${esc(manual.instructions || '请在对应 Agent 应用中完成提交/审批；完成后链路会自动继续。')}</div>
      ${manual.prompt_file ? `<div class="sub mono">任务说明：${esc(manual.prompt_file)}</div>` : ''}
      <div class="row" style="margin-top:6px">
        ${restart ? `<button class="primary small" onclick="restartAgentInterface('${esc(a.agent_id)}','${esc(a.task_uid)}','${esc(run.run_id)}')">重启 Agent 接口</button>` : ''}
        ${synthetic ? `<button class="primary small" onclick="resumeRun('${esc(run.run_id)}')">继续运行</button>` : ''}
        ${!synthetic && String(a.error || '').startsWith('SUBMISSION_') ? `<button class="primary small" onclick="reconcileNode('${esc(a.task_uid)}','${esc(run.run_id)}')">核对原请求</button>` : ''}
        ${!synthetic ? `<button class="small" onclick="openAdoptModal('${esc(a.task_uid)}')">采纳成果</button>` : ''}
        ${!synthetic && !String(a.error || '').startsWith('SUBMISSION_') && (restart || ['agent_not_running', 'agent_busy', 'login_required', 'workspace_trust'].includes(kind)) ? `<button class="small" onclick="retryNode('${esc(a.task_uid)}','${esc(run.run_id)}')">重试该节点</button>` : ''}
        <button class="small" onclick="refresh()">刷新状态</button>
      </div>
    </div>`;
  }).join('');
  box.innerHTML = `<h3>链路已阻塞：需要人工处理 ${actions.length + highRisk.length} 项</h3>
    <div class="sub">阻塞期间不会启动下一个节点；批准高风险或完成应用内操作后自动继续。</div>
    ${liveRun ? `<div class="row" style="margin:6px 0 2px"><button class="small" onclick="openSummaryModal('${esc(run.run_id || '')}')">回填汇总 JSON</button></div>` : ''}
    ${approvalItems}${items}`;
  box.classList.remove('hidden');
}

async function approveHighRiskAndResume(approvalId, runId) {
  try {
    await post('/api/approvals/' + encodeURIComponent(approvalId),
      { approve: true, note: 'WebUI 高风险审批弹窗确认' });
    if (runId) {
      await post('/api/runs/' + encodeURIComponent(runId) + '/resume',
        { background: true, max_parallel: 1 }).catch(() => {});
    }
    toast('高风险审批已批准，运行继续');
    refresh();
  } catch (e) { toast('批准高风险失败：' + e.message, true); }
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
  const evidenceById = Object.fromEntries(manifest.map((m) => [m.agent_id, m.capability_evidence || {}]));
  const scoreById = Object.fromEntries((((ov.router || {}).agents) || []).map((r) => [r.agent_id, r.stats || {}]));
  const modelsByAgent = (state.models || {}).by_agent || {};
  $('agentCards').innerHTML = agents.map((a) => {
    const capabilityList = capabilityById[a.agent_id] || Object.entries(a.capabilities || {}).filter(([k, v]) => v).map(([k]) => k);
    const evidence = evidenceById[a.agent_id] || {};
    const evidenceClass = { disabled: 'bad', degraded: 'warn', probed: 'ok', declared: '' };
    const caps = capabilityList.map((k) => {
      const e = evidence[k] || {};
      const cls = evidenceClass[e.status] || '';
      const tip = [e.status, e.source].filter(Boolean).join(' · ');
      return `<span class="tag ${cls}" title="${esc(tip)}">${esc(k)}${e.status ? ' · ' + esc(e.status) : ''}</span>`;
    }).join('');
    const auto = evidence.auto_executable || {};
    const autoPill = auto.status
      ? `<span class="pill ${auto.status === 'probed' ? 'ok' : auto.status === 'degraded' ? 'warn' : 'bad'}"
           title="${esc(auto.last_error || auto.source || '')}">自动执行 ${esc(auto.status)}</span>`
      : '';
    const stats = scoreById[a.agent_id] || {};
    const entry = modelsByAgent[a.agent_id] || {};
    return `<div class="card">
      <div class="row" style="justify-content:space-between">
        <h3>${esc(a.name)}</h3><span class="pill ${a.status === 'online' ? 'ok' : 'warn'}">${esc(a.status)}</span>
      </div>
      <div class="sub">${esc(a.agent_id)} · ${esc(a.product)} ${esc(a.edition)} · v${esc(a.version)}</div>
      <div style="margin:8px 0"><span class="pill info">${esc(a.access_level)}</span><span class="pill">${esc(a.adapter_type)}</span>
        ${autoPill}
        <span class="pill ${entry.selected_model ? 'ok' : ''}">模型 ${esc(entry.selected_model || '未选配')}</span></div>
      <div class="sub">模型清单（${(entry.models || []).length}）：${(entry.models || []).map((m) => esc(m.name)).join(', ') || '未探测'}</div>
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

/* ------------------------------------------------------------------ */
/* Multi-agent chain: one agent + one real model per stage             */
/* ------------------------------------------------------------------ */
const CHAIN_TASK_TYPES = [
  'analysis', 'review', 'implementation', 'verification', 'documentation', 'architecture',
];
const CHAIN_DEFAULT_STAGES = [
  { agent_id: 'trae_cn', type: 'analysis', title: '阶段 1 · 扫描' },
  { agent_id: 'qoder_cn', type: 'review', title: '阶段 2 · 查 Bug' },
  { agent_id: 'opencode', type: 'implementation', title: '阶段 3 · 生成测试' },
];

function chainAgentList() {
  return (((state.overview || {}).agents || {}).list || []);
}

function chainProjectKey() {
  return (((state.workspace || {}).project_id) ||
    (((state.overview || {}).project || {}).project_id) || 'default');
}
function chainStorageKey() {
  return 'agentmesh.chain.' + chainProjectKey();
}
function pendingPlan() {
  return (state.plans || []).find((p) => p.status === 'pending_approval') || null;
}

function agentModels(agentId) {
  return (((state.models || {}).by_agent || {})[agentId] || {}).models || [];
}
function firstAgentWithModel() {
  const agents = chainAgentList();
  return agents.find((a) => agentModels(a.agent_id).length)
    || agents.find((a) => a.status === 'online') || agents[0] || {};
}

function defaultChainStages() {
  const agents = chainAgentList();
  return CHAIN_DEFAULT_STAGES.map((stage) => {
    let agent = agents.find((a) => a.agent_id === stage.agent_id);
    if (!agent || !agentModels(agent.agent_id).length) agent = firstAgentWithModel();
    const models = agentModels(agent.agent_id || '');
    return {
      task_id: '', title: stage.title,
      agent_id: agent.agent_id || stage.agent_id,
      model: models.length ? models[0].name : '',
      type: stage.type,
    };
  });
}

function chainStagesFromPlan(plan) {
  const agents = chainAgentList();
  return (plan.tasks || []).map((task, index) => {
    const agentId = task.suggested_agent || (agents[0] && agents[0].agent_id) || '';
    const agent = agents.find((a) => a.agent_id === agentId);
    const models = (((state.models || {}).by_agent || {})[agentId] || {}).models || [];
    const selected = (agent && agent.extra && agent.extra.selected_model) || '';
    return {
      task_id: task.task_id || '',
      title: `${task.task_id || 'T' + String(index + 1).padStart(3, '0')} · ${task.title || ''}`,
      agent_id: agentId,
      model: task.model || selected || (models.length ? models[0].name : ''),
      type: task.type || 'analysis',
    };
  });
}

function ensureChainDraft() {
  const projectKey = chainProjectKey();
  if (state.chainDraft && state.chainHydratedFor === projectKey) return;
  let draft = null;
  const server = ((state.overview || {}).chain_draft) || {};
  const serverValid = Array.isArray(server.stages) && server.stages.length
    && (!server.project_id || server.project_id === projectKey);
  let local = null;
  try { local = JSON.parse(localStorage.getItem(chainStorageKey()) || 'null'); }
  catch (e) { local = null; }
  const serverRev = serverValid ? Number(server.revision || 0) : 0;
  const localRev = local ? Number(local.revision || 0) : 0;
  const localBase = local ? Number(local.base_revision || local.revision || 0) : 0;
  if (local && local.dirty && serverValid && localBase < serverRev) {
    // Both sides changed since the local draft was based on the server copy.
    draft = local;
    state.chainDraftStatus = 'conflict';
  } else if (local && local.dirty && (!serverValid || localBase >= serverRev)) {
    // Unsaved local edits on top of the current server revision must win over
    // the server copy on refresh (same revision number is not "older").
    draft = local;
    state.chainDraftStatus = 'dirty';
  } else if (local && localRev > serverRev) {
    draft = local;
    state.chainDraftStatus = 'saved';
  } else if (serverValid) {
    draft = server;
    state.chainDraftStatus = 'saved';
  } else if (local) {
    draft = local;
    state.chainDraftStatus = local.dirty ? 'dirty' : 'saved';
  }
  if (!draft) {
    state.chainDraftStatus = 'saved';
  }
  if (!draft) {
    const plan = pendingPlan();
    draft = {
      plan_id: plan ? plan.plan_id : '',
      goal: (plan && plan.goal) || ($('chainGoal') && $('chainGoal').value) || '',
      policy: 'balanced',
      stages: plan ? chainStagesFromPlan(plan) : defaultChainStages(),
    };
  }
  draft.stages = (draft.stages || []).map((s) => ({ ...s }));
  if (!draft.stages.length) draft.stages = defaultChainStages();
  state.chainDraft = draft;
  state.chainHydratedFor = projectKey;
  if ($('chainGoal') && draft.goal) $('chainGoal').value = draft.goal;
  if ($('chainPolicy') && draft.policy) $('chainPolicy').value = draft.policy;
  renderChainPlanInfo();
}

function renderChainPlanInfo() {
  const box = $('chainPlanInfo');
  if (!box || !state.chainDraft) return;
  const plan = pendingPlan();
  const bound = state.chainDraft.plan_id || '';
  const count = (state.chainDraft.stages || []).length;
  const draftState = {
    saved: '草稿已保存',
    dirty: '草稿未保存',
    saving: '草稿保存中…',
    failed: '草稿保存失败（本地已保留）',
    conflict: '草稿冲突：服务器已有更新，请确认后另存',
  }[state.chainDraftStatus || 'saved'];
  if (bound) {
    const target = (state.plans || []).find((p) => p.plan_id === bound);
    box.innerHTML = `绑定计划 <span class="mono">${esc(bound)}</span> · ${count} 个阶段`
      + (target ? ` · <span class="pill ${pillClass(target.status)}">${esc(target.status)}</span>` : '')
      + (plan && plan.plan_id !== bound ? ' · <b style="color:#b45309">已有更新的待审批计划，请点击“从计划载入阶段”</b>' : '')
      + ` · <span class="sub">${esc(draftState)}</span>`;
  } else {
    box.textContent = `独立链路 · ${count} 个阶段（保存后生成待审批计划）· ${draftState}`;
  }
}

function chainModelOptions(agentId, selected) {
  const item = ((state.models || {}).by_agent || {})[agentId] || { models: [] };
  const rows = item.models || [];
  const names = rows.map((m) => m.name);
  let html = '';
  rows.forEach((m) => {
    const name = m.name;
    const degraded = ((m.health || {}).status === 'degraded');
    const label = degraded
      ? `${name} · ⚠${(m.health || {}).reason || '已降级'}`
      : name;
    html += `<option value="${esc(name)}" ${name === selected ? 'selected' : ''}>${esc(label)}</option>`;
  });
  if (selected && !names.includes(selected)) {
    html += `<option value="${esc(selected)}" selected>${esc(selected)} · 手动</option>`;
  }
  return html || '<option value="">该 Agent 暂无真实模型清单</option>';
}

function renderChain() {
  const box = $('chainStages');
  if (!box) return;
  ensureChainDraft();
  // A refresh can finish while the user is mid-edit; rebuilding the whole
  // chain here is exactly what used to wipe custom stage/model choices.
  const active = document.activeElement;
  if (active && ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName)
      && active.closest && active.closest('#chainStages, #chainGoal, #chainPolicy')) {
    return;
  }
  const agents = chainAgentList();
  if (!agents.length) {
    box.innerHTML = '<div class="empty">尚未打开项目或没有 Agent</div>';
    return;
  }
  const stages = state.chainDraft.stages || [];
  const rows = stages.map((stage, index) => {
    const agentId = stage.agent_id || '';
    const agentOptions = agents.map((a) => (
      `<option value="${esc(a.agent_id)}" ${a.agent_id === agentId ? 'selected' : ''}>${esc(a.name)} · ${esc(a.adapter_type || '')}</option>`
    )).join('') + (agentId && !agents.some((a) => a.agent_id === agentId)
      ? `<option value="${esc(agentId)}" selected>${esc(agentId)}（未探测）</option>` : '');
    const label = stage.title || `阶段 ${index + 1}`;
    return `<div class="row chain-row" style="align-items:flex-end;gap:8px;margin-bottom:8px">
      <div style="min-width:180px;max-width:220px"><label>${esc(label)}</label></div>
      <div class="grow"><label>Agent</label>
        <select id="chainAgent${index}" onchange="onChainAgentChange(${index})">${agentOptions}</select></div>
      <div class="grow"><label>模型（真实清单）</label>
        <select id="chainModel${index}" onchange="chainFieldChanged()">${chainModelOptions(agentId, stage.model || '')}</select></div>
      <div style="min-width:150px"><label>任务类型</label>
        <select id="chainType${index}" onchange="chainFieldChanged()">
          ${CHAIN_TASK_TYPES.map((t) => `<option value="${t}" ${t === (stage.type || 'analysis') ? 'selected' : ''}>${t}</option>`).join('')}
        </select></div>
      <button class="del small" title="删除该阶段" onclick="removeChainStage(${index})">×</button>
    </div>`;
  }).join('');
  box.innerHTML = rows;
  renderChainPlanInfo();
}

function onChainAgentChange(index) {
  const agentId = $('chainAgent' + index).value;
  $('chainModel' + index).innerHTML = chainModelOptions(agentId, '');
  chainFieldChanged();
}

function chainCollect() {
  const stages = [];
  (state.chainDraft.stages || []).forEach((stage, index) => {
    const agent = $('chainAgent' + index);
    const model = $('chainModel' + index);
    const type = $('chainType' + index);
    stages.push({
      task_id: stage.task_id || '',
      title: stage.title || '',
      agent_id: agent ? agent.value : stage.agent_id,
      model: model ? model.value : stage.model,
      type: type ? type.value : stage.type,
    });
  });
  return stages;
}

function chainFieldChanged() {
  ensureChainDraft();
  state.chainDraft.stages = chainCollect();
  state.chainDraft.goal = $('chainGoal') ? $('chainGoal').value : state.chainDraft.goal;
  state.chainDraft.policy = $('chainPolicy') ? $('chainPolicy').value : state.chainDraft.policy;
  state.chainDraft.dirty = true;
  if (state.chainDraft.base_revision === undefined) {
    state.chainDraft.base_revision = Number(state.chainDraft.revision || 0);
  }
  state.chainDraft.local_updated_at = new Date().toISOString();
  state.chainDraftStatus = 'dirty';
  try { localStorage.setItem(chainStorageKey(), JSON.stringify(state.chainDraft)); } catch (e) { /* quota */ }
  renderChainPlanInfo();
  clearTimeout(state.chainPersistTimer);
  const editProjectKey = state.chainHydratedFor;
  state.chainPersistTimer = setTimeout(() => {
    if (!state.chainDraft || state.chainHydratedFor !== editProjectKey) return;
    const projectKey = state.chainHydratedFor;
    const sent = JSON.parse(JSON.stringify(state.chainDraft));
    sent.base_revision = Number(sent.revision || 0);
    state.chainDraftStatus = 'saving';
    renderChainPlanInfo();
    post('/api/chain/draft', { draft: sent }).then((saved) => {
      if (state.chainHydratedFor !== projectKey) return;
      state.chainDraft.revision = Number(saved.revision || sent.base_revision + 1);
      state.chainDraft.base_revision = state.chainDraft.revision;
      const unchanged = JSON.stringify(state.chainDraft.stages || [])
        === JSON.stringify(sent.stages || []);
      state.chainDraft.dirty = !unchanged;
      state.chainDraftStatus = unchanged ? 'saved' : 'dirty';
      try { localStorage.setItem(chainStorageKey(), JSON.stringify(state.chainDraft)); } catch (e) { /* quota */ }
      renderChainPlanInfo();
    }).catch((err) => {
      if (state.chainHydratedFor !== projectKey) return;
      const conflict = /另一处|409|conflict/i.test(err.message || '');
      state.chainDraftStatus = conflict ? 'conflict' : 'failed';
      renderChainPlanInfo();
    });
  }, 700);
}

function loadChainFromPlan(silent = false) {
  const plan = pendingPlan();
  if (!plan) { if (!silent) toast('当前没有待审批计划，请先在“生成任务计划”生成计划', true); return false; }
  state.chainDraft = {
    plan_id: plan.plan_id, goal: plan.goal || '',
    policy: ($('chainPolicy') && $('chainPolicy').value) || 'balanced',
    stages: chainStagesFromPlan(plan),
  };
  state.chainHydratedFor = chainProjectKey();
  if ($('chainGoal')) $('chainGoal').value = state.chainDraft.goal;
  try { localStorage.setItem(chainStorageKey(), JSON.stringify(state.chainDraft)); } catch (e) { /* quota */ }
  renderChain();
  chainFieldChanged();
  if (!silent) toast(`已从计划载入 ${state.chainDraft.stages.length} 个阶段`);
  return true;
}

function addChainStage() {
  ensureChainDraft();
  if (state.chainDraft.plan_id) {
    toast('阶段数量由绑定的计划决定；请先在“生成任务计划”生成新计划，或点击“从计划载入阶段”', true);
    return;
  }
  const agent = firstAgentWithModel();
  const models = agentModels(agent.agent_id || '');
  state.chainDraft.stages.push({
    task_id: '', title: `阶段 ${state.chainDraft.stages.length + 1}`,
    agent_id: agent.agent_id || '', model: models.length ? models[0].name : '',
    type: 'analysis',
  });
  renderChain();
  chainFieldChanged();
}

function removeChainStage(index) {
  ensureChainDraft();
  if (state.chainDraft.plan_id) {
    toast('阶段数量由绑定的计划决定，不能单独删除；请先生成新计划', true);
    return;
  }
  if (state.chainDraft.stages.length <= 1) { toast('至少保留一个阶段', true); return; }
  state.chainDraft.stages.splice(index, 1);
  renderChain();
  chainFieldChanged();
}

function resetChain() {
  state.chainDraft = null;
  state.chainHydratedFor = '';
  const plan = pendingPlan();
  if (plan) { loadChainFromPlan(true); toast('已重置为当前计划的任务'); }
  else {
    state.chainDraft = { plan_id: '', goal: '', policy: 'balanced', stages: defaultChainStages() };
    state.chainHydratedFor = chainProjectKey();
    renderChain();
    chainFieldChanged();
    toast('已重置为默认三个阶段');
  }
}

async function autoSelectChain() {
  ensureChainDraft();
  $('chainStatus').textContent = '正在计算最省且够用的组合…';
  const reasons = [];
  try {
    const count = (state.chainDraft.stages || []).length;
    for (let i = 0; i < count; i += 1) {
      const typeEl = $('chainType' + i);
      if (!typeEl) continue;
      const type = typeEl.value;
      const policy = ($('chainPolicy') && $('chainPolicy').value) || 'balanced';
      const rec = await api('/api/models/recommend?task_type=' + encodeURIComponent(type)
        + '&limit=8&profile=' + encodeURIComponent(policy));
      const pick = rec.recommended;
      if (!pick) { reasons.push(`${type}: 无可用组合`); continue; }
      $('chainAgent' + i).value = pick.agent_id;
      $('chainModel' + i).innerHTML = chainModelOptions(pick.agent_id, pick.model);
      $('chainModel' + i).value = pick.model;
      reasons.push(`${type}: ${pick.agent_name} / ${pick.model}（成本 ${pick.cost} · ${pick.cost_source}）`);
    }
    chainFieldChanged();
    $('chainRecommend').innerHTML = reasons.map((r) => `<div>· ${esc(r)}</div>`).join('');
    $('chainStatus').textContent = '已按“质量阈值内最低成本”填好';
    toast('智能选型完成');
  } catch (e) {
    $('chainStatus').textContent = '选型失败';
    toast('智能选型失败：' + e.message, true);
  }
}

async function preflightChain() {
  const stages = chainCollect();
  if (stages.some((s) => !s.agent_id || !s.model)) {
    toast('每个阶段都必须选择 Agent 和模型', true);
    return;
  }
  if (!confirm('预检会把已选 Agent 打开/重启到带程序接口的状态（Trae、WorkBuddy 会被关闭后重新打开）。继续？')) {
    return;
  }
  $('chainStatus').textContent = '正在打开 Agent 并绑定工作区/模型…（可能需要 1-3 分钟）';
  $('chainPreflight').innerHTML = '<div class="sub">执行中：会重启 Trae/Qoder 等桌面应用，请勿操作。</div>';
  try {
    const result = await post('/api/preflight', {
      stages,
      workspace: (state.workspace && state.workspace.root) || '',
      open_apps: true,
      full_control: !!($('fullControl') && $('fullControl').checked),
    });
    const rows = (result.agents || []).map((a) => `<tr>
      <td>${esc(a.agent_id)}</td>
      <td>${esc(a.process || '')}</td>
      <td class="mono">${esc(a.visible_model || a.model || '')}</td>
      <td>${a.workspace_set ? '✅' : '—'}</td>
      <td>${a.model_set ? '✅' : '—'}</td>
      <td>${a.ok ? '✅ ready' : '❌ ' + esc((a.error || a.stage || '').slice(0, 90))}</td>
    </tr>`).join('');
    $('chainPreflight').innerHTML = `<div class="sub" style="margin-bottom:6px">
      工作区 ${esc(result.workspace || '')} · 就绪 ${result.ready}/${result.total}</div>
      <table class="table"><thead><tr><th>Agent</th><th>进程</th><th>实际模型</th>
      <th>工作区</th><th>模型</th><th>结果</th></tr></thead><tbody>${rows}</tbody></table>`;
    $('chainStatus').textContent = `预检完成 ${result.ready}/${result.total}`;
    toast(`预检完成：${result.ready}/${result.total} 就绪`);
  } catch (e) {
    $('chainStatus').textContent = '预检失败';
    $('chainPreflight').innerHTML = `<div class="sub">${esc(e.message)}</div>`;
    toast('预检失败：' + e.message, true);
  }
}

async function saveChainConfig() {
  ensureChainDraft();
  const goal = ($('chainGoal').value || '').trim();
  const stages = chainCollect();
  if (!goal) { toast('请填写链路目标（或从计划载入）', true); return; }
  if (!stages.length || stages.some((s) => !s.agent_id || !s.model)) {
    toast('每个阶段都必须选择一个 Agent 和它的真实模型', true);
    return;
  }
  let planId = state.chainDraft.plan_id || '';
  const pending = pendingPlan();
  if (planId && (!pending || pending.plan_id !== planId)) {
    const bound = (state.plans || []).find((p) => p.plan_id === planId);
    if (bound && bound.status === 'approved') {
      // Editing an approved plan must be an explicit new revision, never a
      // silent standalone plan created by this save button.
      toast('该计划已批准；如需变更请先“生成任务计划”形成新修订并重新审批', true);
      return;
    }
    if (pending) {
      toast('当前待审批计划已更新，请先点击“从计划载入阶段”再保存', true);
      return;
    }
    planId = '';
  }
  if (!planId && pending) {
    const planCount = (pending.tasks || []).length;
    if (stages.length !== planCount) {
      toast(`当前计划有 ${planCount} 个任务，与 ${stages.length} 个阶段不一致；请先点击“从计划载入阶段”`, true);
      return;
    }
    planId = pending.plan_id;
  }
  $('chainStatus').textContent = '正在保存链路配置…';
  try {
    const result = await post('/api/chain', {
      goal, stages, plan_id: planId, save_only: true,
      main_agent_id: ($('mainAgent').value || ''), max_parallel: 1,
      start: false, approve: false,
    });
    state.chainDraft.plan_id = result.plan_id || planId;
    state.chainDraft.goal = goal;
    state.chainDraft.base_revision = Number(state.chainDraft.revision || 0);
    state.chainDraft.dirty = false;
    try { localStorage.setItem(chainStorageKey(), JSON.stringify(state.chainDraft)); } catch (e) { /* quota */ }
    try {
      const savedDraft = await post('/api/chain/draft', { draft: state.chainDraft });
      state.chainDraft.revision = Number(savedDraft.revision || 0);
      state.chainDraftStatus = 'saved';
    } catch (err) {
      state.chainDraftStatus = /另一处|409|conflict/i.test(err.message || '')
        ? 'conflict' : 'failed';
    }
    $('chainStatus').textContent = `已保存 ${result.plan_id} · ${stages.length} 阶段（待审批）`;
    toast('链路配置已保存。请到第 4 步批准计划，再点击“开始执行”。');
    $('chainPlanInfo').textContent = `已保存到 ${result.plan_id}；第 4 步批准后才会开始执行`;
    const warnings = result.warnings || [];
    if (warnings.length) {
      $('chainPlanInfo').textContent += ' · ⚠ ' + warnings.join('；');
      toast('⚠ ' + warnings.join('；'), true);
    }
    refresh();
  } catch (e) {
    $('chainStatus').textContent = '保存失败';
    toast('保存链路配置失败：' + e.message, true);
  }
}

async function reconcileNode(uid, runId) {
  if (!uid) return;
  try {
    const r = await post('/api/tasks/' + encodeURIComponent(uid) + '/reconcile', {});
    const receipt = r.receipt || {};
    if (r.status === 'accepted') {
      toast(r.verified ? '已核对原请求并继续验证' : '已确认送达，仍在等待应用完成');
    } else if (r.status === 'unsupported') {
      toast(r.reason || '该 Agent 不支持回执核对', true);
    } else {
      toast('仍未找到送达证据；不会自动重发，请人工检查应用', true);
    }
    if (runId && receipt.status === 'accepted') {
      await post('/api/runs/' + encodeURIComponent(runId) + '/resume',
        { background: true, max_parallel: 1 }).catch(() => {});
    }
    refresh();
  } catch (e) { toast('核对失败：' + e.message, true); }
}

async function retryNode(uid, runId) {
  if (!uid) return;
  try {
    await post('/api/tasks/' + encodeURIComponent(uid) + '/retry', {});
    if (runId) {
      await post('/api/runs/' + encodeURIComponent(runId) + '/resume',
        { background: true, max_parallel: 1 }).catch(() => {});
    }
    toast('节点已重新排队，运行继续');
    refresh();
  } catch (e) { toast('重试失败：' + e.message, true); }
}

function openBindingEditor(uid) {
  const task = (state.tasks || []).find((t) => t.uid === uid);
  if (!task) { toast('任务不在当前列表，请先刷新', true); return; }
  state.bindingTask = task;
  const agents = chainAgentList();
  const agentId = task.assigned_agent || (task.spec && task.spec.suggested_agent) || '';
  const model = (task.spec && task.spec.model) || task.model || '';
  $('bindingTitle').textContent = `编辑节点 ${task.task_id}`;
  $('bindingInfo').textContent = `状态 ${task.status} · 修改后对排队/阻塞/等待节点立即生效；` +
    `正在运行的节点需勾选“停止当前尝试并改绑”，保存后点击“重试该节点”。`;
  $('bindingAgent').innerHTML = agents.map((a) => (
    `<option value="${esc(a.agent_id)}" ${a.agent_id === agentId ? 'selected' : ''}>${esc(a.name)} · ${esc(a.adapter_type || '')}</option>`
  )).join('') + (agentId && !agents.some((a) => a.agent_id === agentId)
    ? `<option value="${esc(agentId)}" selected>${esc(agentId)}（未探测）</option>` : '');
  $('bindingModel').innerHTML = chainModelOptions(agentId, model);
  const chosen = $('bindingModel').value || model;
  const profile = modelProfile(agentId, chosen);
  const spec = task.spec || {};
  fillProfileInputs('bindingContext', 'bindingCapability', agentId, chosen, {
    context: spec.context_length || profile.context_length || 0,
    capability: spec.capability_level || profile.capability_level || '',
  });
  if ($('bindingStopActive')) $('bindingStopActive').checked = false;
  $('bindingModal').style.display = 'flex';
}
function onBindingAgentChange() {
  const agentId = $('bindingAgent').value;
  $('bindingModel').innerHTML = chainModelOptions(agentId, '');
  fillProfileInputs('bindingContext', 'bindingCapability', agentId,
    $('bindingModel').value);
}
function onBindingModelChange() {
  const agentId = $('bindingAgent').value;
  fillProfileInputs('bindingContext', 'bindingCapability', agentId,
    $('bindingModel').value);
}
function closeBindingEditor() {
  $('bindingModal').style.display = 'none';
  state.bindingTask = null;
}
async function saveBindingEditor() {
  const task = state.bindingTask;
  if (!task) return;
  const contextLength = Math.max(0, Number($('bindingContext').value || 0));
  const capabilityLevel = $('bindingCapability').value || '';
  try {
    validateProfileInputs($('bindingAgent').value, $('bindingModel').value,
      contextLength, capabilityLevel);
    await post('/api/tasks/' + encodeURIComponent(task.uid) + '/binding', {
      agent_id: $('bindingAgent').value,
      model: $('bindingModel').value,
      context_length: contextLength,
      capability_level: capabilityLevel,
      stop_active: !!($('bindingStopActive') && $('bindingStopActive').checked),
    });
    closeBindingEditor();
    toast('节点绑定已更新');
    refresh();
  } catch (e) { toast('更新绑定失败：' + e.message, true); }
}

async function reverifyNode(uid) {
  if (!uid) return;
  try {
    const r = await post('/api/tasks/' + encodeURIComponent(uid) + '/reverify', {});
    toast(`重新验证完成：${r.status} · ${r.verification_status || ''}`,
      r.status === 'failed');
    refresh();
  } catch (e) { toast('重新验证失败：' + e.message, true); }
}

async function restartAgentInterface(agentId, uid, runId) {
  try {
    const r = await post('/api/agents/' + encodeURIComponent(agentId) + '/interface/restart', {});
    toast(r.ok ? '程序接口已就绪' : ('重启失败：' + (r.reason || '未知原因')), !r.ok);
    if (r.ok && uid) {
      await post('/api/tasks/' + encodeURIComponent(uid) + '/retry', {}).catch(() => {});
      if (runId) {
        await post('/api/runs/' + encodeURIComponent(runId) + '/resume',
          { background: true, max_parallel: 1 }).catch(() => {});
      }
    }
    refresh();
  } catch (e) { toast('重启失败：' + e.message, true); }
}

function renderChainResult(payload) {
  const stages = payload.stages || [];
  if (!stages.length) { $('chainResult').innerHTML = ''; return; }
  const rows = stages.map((s) => `<tr>
    <td>${esc(s.task_id || '')}</td>
    <td>${esc(s.agent_name || s.agent_id || '')}</td>
    <td class="mono">${esc(s.model || '')}</td>
    <td>${esc(s.status || payload.status || '')}</td>
    <td>${esc(s.verification || '')}</td>
    <td class="sub">${esc((s.error || '').slice(0, 120))}</td>
  </tr>`).join('');
  $('chainResult').innerHTML = `<table class="table">
    <thead><tr><th>任务</th><th>Agent</th><th>模型</th><th>状态</th><th>验证</th><th>错误</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function renderChainBindings(payload) {
  const rows = (payload.stages || []).map((s) => `<tr>
    <td>${esc(s.task_id)}</td>
    <td>${esc(s.agent_name || s.agent_id || '')}</td>
    <td class="mono">${esc(s.model || '')}</td>
    <td class="mono">${esc((s.workspace || '').split('\\').slice(-2).join('\\'))}</td>
    <td>${esc(s.status)}</td>
    <td>${s.verified ? '✅ ' + esc(s.verify_method || '') : '—'}</td>
    <td class="sub">${esc((s.error || '').slice(0, 100))}</td>
  </tr>`).join('');
  $('chainResult').innerHTML = `<div class="sub" style="margin:10px 0 6px">
    运行 ${esc(payload.run_id)} · 每阶段实际绑定</div>
    <table class="table"><thead><tr><th>任务</th><th>Agent</th><th>模型</th>
    <th>工作区</th><th>状态</th><th>接口核验</th><th>错误</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function pollChain(runId) {
  if (state.chainTimer) clearInterval(state.chainTimer);
  state.chainTimer = setInterval(async () => {
    try {
      const data = await api('/api/chain?run_id=' + encodeURIComponent(runId));
      renderChainResult(data);
      $('chainStatus').textContent = `${data.status} · 验收 ${data.acceptance_status || '-'}`;
      if (['completed', 'released', 'failed', 'pending_acceptance', 'paused'].includes(data.status)) {
        clearInterval(state.chainTimer);
        state.chainTimer = null;
        try {
          const bindings = await api('/api/runs/' + encodeURIComponent(runId) + '/bindings');
          renderChainBindings(bindings);
        } catch (e) { /* 绑定报告不影响主流程 */ }
        refresh();
      }
    } catch (e) {
      clearInterval(state.chainTimer);
      state.chainTimer = null;
    }
  }, 3000);
}

async function syncModels() {
  const box = $('modelSyncInfo');
  if (box) box.textContent = '正在同步全部 Agent 的真实模型目录…';
  try {
    const r = await post('/api/agents/models/sync', {});
    const n = (r.agents || []).length;
    toast(`模型目录已同步：${n} 个 Agent`);
    await refresh();
  } catch (e) {
    toast('同步失败：' + e.message, true);
  }
}

function renderModelSync() {
  const box = $('modelSyncInfo');
  if (!box) return;
  const s = state.modelsSync || {};
  if (!s.synced_at) {
    box.textContent = '模型目录尚未同步（每天自动同步；也可点“同步模型”）';
    return;
  }
  const age = (s.age_hours == null) ? '未知' : `${s.age_hours} 小时前`;
  box.innerHTML = `模型目录上次同步：<span class="mono">${esc(s.synced_at)}</span>（${esc(age)}）`
    + (s.stale ? ' <span class="pill warn">已过期，将自动同步</span>' : ' <span class="pill ok">最新</span>');
}

/* ------------------------------------------------------------------ */
/* 角色路由                                                            */
/* ------------------------------------------------------------------ */
async function loadRoles(force) {
  if (state.roles && !force) return state.roles;
  try {
    state.roles = await api('/api/roles');
  } catch (e) {
    state.roles = { error: e.message, roles: [] };
  }
  return state.roles;
}

function solvePins() {
  const split = (value) => {
    const text = (value || '').trim();
    if (!text) return {};
    const idx = text.indexOf(':');
    if (idx <= 0) throw new Error('格式应为 agent:model，例如 qoder_cn:DeepSeek-Flash');
    return { agent: text.slice(0, idx), model: text.slice(idx + 1) };
  };
  const ex = split($('solveExecutor').value);
  const cm = split($('solveCommander').value);
  return {
    executor_agent: ex.agent || '', executor_model: ex.model || '',
    commander_agent: cm.agent || '', commander_model: cm.model || '',
  };
}

function roleTableHtml(policy) {
  const rows = policy.roles || [];
  if (!rows.length) return `<div class="empty">${esc(policy.error || '当前没有可解析的角色')}</div>`;
  return `<table class="table"><thead><tr>
      <th>角色</th><th>任务类型</th><th>可写</th><th>首选候选</th><th>候选数</th><th>状态</th>
    </tr></thead><tbody>` + rows.map((r) => {
      const first = (r.resolved || [])[0] || {};
      return `<tr>
        <td><b>${esc(r.role)}</b><div class="sub">${esc(r.description || '')}</div></td>
        <td class="mono">${esc(r.task_type)}</td>
        <td>${r.write ? '是' : '否'}</td>
        <td class="mono">${first.agent_id ? esc(first.agent_id) + ' / ' + esc(first.model) : '—'}
          ${first.tier !== undefined ? `<span class="pill ${first.tier >= 3 ? 'warn' : 'ok'}">tier ${esc(first.tier)} ${esc(first.tier_label || '')}</span>` : ''}</td>
        <td>${esc(r.resolved_count || 0)}</td>
        <td><span class="pill ${r.status === 'ready' ? 'ok' : 'bad'}">${esc(r.status)}</span></td>
      </tr>`;
    }).join('') + '</tbody></table>';
}

async function renderRoles() {
  if (state.view !== 'roles') return;
  const policy = await loadRoles(false);
  const box = $('rolePolicy');
  if (!box) return;
  box.className = '';
  box.innerHTML = `<div class="sub" style="margin-bottom:8px">策略 <b>${esc(policy.name || '')}</b>
      来源 ${esc(policy.source || '')} · premium 档从 tier ${esc(policy.premium_tier)} 起 ·
      单次最多 ${esc(policy.max_premium_tasks_per_run)} 个贵任务</div>` + roleTableHtml(policy);
}

async function previewSegment() {
  const goal = $('solveGoal').value.trim();
  if (!goal) { toast('请先输入需求', true); return; }
  try {
    const body = Object.assign({ goal }, solvePins());
    const data = await post('/api/segment', body);
    const tasks = data.tasks || [];
    const bindings = data.bindings || [];
    $('segmentResult').className = '';
    $('segmentResult').innerHTML = `<div class="sub">置信度 ${esc(data.confidence)} ·
        切出 ${tasks.length} 个任务</div>` + tasks.map((t, i) => {
      const b = bindings[i] || {};
      const c = (b.candidates || [])[0] || {};
      return `<div class="drift" style="border-left-color:#6366f1;background:#eef2ff">
        <b>${esc(t.task_id)} · ${esc(t.role)} · ${esc(t.type)}</b>
        <span class="sub">scope ${esc((t.scope || []).join(', ') || '（只读）')} ·
          候选 ${esc(c.agent_id || '无')} / ${esc(c.model || '无')}
          ${c.tier !== undefined ? '· tier ' + esc(c.tier) : ''}</span>
        <div class="sub">${esc(t.objective || '').split('\n').slice(-2).join(' ')}</div>
      </div>`;
    }).join('') + ((data.notes || []).length
      ? `<div class="sub">${esc(data.notes.join('；'))}</div>` : '');
  } catch (e) { toast(e.message, true); }
}

async function runSolve() {
  const goal = $('solveGoal').value.trim();
  if (!goal) { toast('请先输入需求', true); return; }
  try {
    // "一键执行" is an explicit batch authorization; the approval service
    // still records the ticket and its exact plan revision.
    const body = Object.assign({ goal, start: true, background: false, approve: true }, solvePins());
    $('solveResult').className = 'muted';
    $('solveResult').textContent = '运行中（GUI Agent 可能需要 1–5 分钟）…';
    const out = await post('/api/solve', body);
    const answer = out.answer || {};
    const bindings = out.bindings || [];
    let html = `<div class="sub">run ${esc(out.run_id)} · 置信度 ${esc(out.confidence)} ·
        premium 任务 ${esc(out.premium_tasks)}/${esc(out.premium_budget)}</div>`;
    html += `<table class="table"><thead><tr><th>任务</th><th>角色</th><th>Agent</th>
        <th>模型</th><th>档位</th><th>降级</th></tr></thead><tbody>` + bindings.map((b) => `
        <tr><td class="mono">${esc(b.task_id)}</td><td>${esc(b.role)}</td>
        <td>${esc(b.agent_id || '—')}</td><td class="mono">${esc(b.model || '—')}</td>
        <td>${b.tier !== undefined ? 'tier ' + esc(b.tier) : '—'}</td>
        <td>${b.downgraded ? '是' : '否'}</td></tr>`).join('') + '</tbody></table>';
    if (answer.answer_markdown) {
      html += `<pre style="white-space:pre-wrap;max-height:520px">${esc(answer.answer_markdown)}</pre>`;
    }
    $('solveResult').className = '';
    $('solveResult').innerHTML = html;
    toast('已生成汇总答案');
    refresh();
  } catch (e) {
    $('solveResult').className = 'muted';
    $('solveResult').textContent = e.message;
    toast(e.message, true);
  }
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
  openTextInputModal({
    title: `调用 MCP 工具 · ${tool}`,
    label: `输入 ${tool} 的 JSON 参数（必须符合服务端 inputSchema）`,
    value: '{}',
    onOk: async (raw) => {
      let args;
      try { args = JSON.parse(raw || '{}'); } catch (e) {
        toast('参数不是 JSON：' + e.message, true); return false;
      }
      try {
        const data = await post('/api/agents/' + encodeURIComponent(agentId) + '/mcp/call',
          { tool, arguments: args });
        $('mcpResult').innerHTML = `<pre>${esc(JSON.stringify(data, null, 2))}</pre>`;
        toast('MCP 工具调用完成');
      } catch (e) { toast(e.message, true); }
      return true;
    },
  });
}

function openTextInputModal({ title, label, value, onOk }) {
  $('textInputTitle').textContent = title || '输入';
  $('textInputLabel').textContent = label || '';
  $('textInputValue').value = value || '';
  state.textInputCallback = onOk || null;
  $('textInputModal').style.display = 'flex';
  $('textInputValue').focus();
}
function closeTextInputModal() {
  $('textInputModal').style.display = 'none';
  state.textInputCallback = null;
}
async function confirmTextInputModal() {
  const callback = state.textInputCallback;
  const value = $('textInputValue').value;
  if (!callback) { closeTextInputModal(); return; }
  const done = await callback(value);
  if (done !== false) closeTextInputModal();
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
    renderTask(); loadTaskAttempts(uid); loadLogs();
  } catch (e) { toast('加载任务失败：' + e.message, true); }
}

async function loadTaskAttempts(uid) {
  const box = $('taskAttempts');
  const select = $('resultAttempt');
  if (box) box.innerHTML = '加载中…';
  if (select) select.innerHTML = '';
  try {
    const data = await api('/api/tasks/' + encodeURIComponent(uid) + '/attempts');
    const attempts = (data && data.attempts) || [];
    if (select) {
      select.innerHTML = '<option value="">不关联（仅历史任务可用）</option>' + attempts.map((a) => (
        `<option value="${esc(a.attempt_id)}" ${a.active ? 'selected' : ''}>` +
        `#${esc(a.attempt_number)} ${esc(a.attempt_id)} · ${esc(a.status || '')}` +
        `${a.result_exists ? ' · 有成果文件' : ''}</option>`
      )).join('');
    }
    if (!box) return;
    if (!attempts.length) { box.innerHTML = '<div class="muted">该任务还没有尝试记录。</div>'; return; }
    box.innerHTML = `<table class="table"><thead><tr><th>尝试</th><th>状态</th>
      <th>结果文件</th><th>错误</th><th>操作</th></tr></thead><tbody>${attempts.map((a) => `
        <tr><td class="mono">#${esc(a.attempt_number)} ${esc(a.attempt_id)}${a.active ? ' <span class="pill info">活动</span>' : ''}</td>
        <td>${esc(a.status || '')}</td>
        <td class="mono">${a.result_exists ? '<span class="pill ok">有</span>' : '<span class="pill">无</span>'}</td>
        <td class="sub mono">${esc((a.error || '').slice(0, 160))}</td>
        <td>${a.result_exists ? `<button class="small" onclick="adoptAttemptDirect('${esc(a.attempt_id)}')">采纳</button>` : ''}</td>
        </tr>`).join('')}</tbody></table>`;
  } catch (e) {
    if (box) box.innerHTML = `<div class="muted">尝试记录加载失败：${esc(e.message)}</div>`;
  }
}

async function adoptAttemptDirect(attemptId) {
  const t = state.selected; if (!t) return;
  try {
    await post('/api/tasks/' + encodeURIComponent(t.uid) + '/adopt-attempt',
      { attempt_id: attemptId });
    toast('已采纳该尝试成果，进入验证');
    selectTask(t.uid);
    refresh();
  } catch (e) { toast(e.message, true); }
}

function renderTask() {
  const t = state.selected;
  if (!t) {
    $('taskTitle').textContent = '未选择任务';
    $('taskBody').innerHTML = '<div class="muted">从任务图或列表中选择一个任务。</div>';
    $('taskActions').innerHTML = '';
    if ($('taskAttempts')) $('taskAttempts').innerHTML = '选择任务后加载。';
    if ($('resultAttempt')) $('resultAttempt').innerHTML = '';
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
  const usageRows = c.by_agent || [];
  const byAgent = {};
  usageRows.forEach((r) => {
    const a = byAgent[r.agent_id] || (byAgent[r.agent_id] = {
      agent_id: r.agent_id, input: 0, output: 0, cached: 0, reasoning: 0,
      total: 0, cost: 0, records: 0, models: [],
    });
    a.input += Number(r.input_tokens || 0);
    a.output += Number(r.output_tokens || 0);
    a.cached += Number(r.cached_tokens || 0);
    a.reasoning += Number(r.reasoning_tokens || 0);
    a.total += Number(r.total_tokens || 0);
    a.cost += Number(r.cost || 0);
    a.records += Number(r.records || 0);
    a.models.push(r);
  });
  const agentList = Object.values(byAgent).sort((x, y) => y.total - x.total);
  const grand = agentList.reduce((n, a) => n + a.total, 0) || 1;
  const agentRows = agentList.map((a) => {
    const pct = (a.total / grand * 100).toFixed(1);
    return `<tr>
      <td><b>${esc(a.agent_id)}</b></td>
      <td>${fmt(a.input)}</td><td>${fmt(a.output)}</td><td>${fmt(a.cached)}</td>
      <td>${a.reasoning ? fmt(a.reasoning) : '—'}</td>
      <td><b>${fmt(a.total)}</b></td>
      <td style="min-width:120px"><div style="height:8px;background:var(--line);border-radius:4px">
        <div style="height:8px;width:${pct}%;background:var(--accent);border-radius:4px"></div></div>
        <span class="sub">${pct}%</span></td>
      <td>${money(a.cost)}</td><td>${a.records}</td></tr>`;
  }).join('') || '<tr><td colspan="9" class="muted">暂无用量记录</td></tr>';
  const detailRows = usageRows.map((r) => `<tr>
    <td>${esc(r.agent_id)}</td><td class="mono">${esc(r.model)}</td>
    <td>${fmt(r.input_tokens)}</td><td>${fmt(r.output_tokens)}</td>
    <td>${fmt(r.cached_tokens)}</td><td>${fmt(r.total_tokens)}</td>
    <td>${money(r.cost)}</td><td>${r.records}</td><td>${r.retries}</td></tr>`).join('');
  $('usageTable').innerHTML = `
    <div class="sub" style="margin-bottom:6px">按 Agent 汇总（预算仅统计，不会自动停止任务）</div>
    <table class="table"><thead><tr><th>Agent</th><th>输入</th><th>输出</th><th>缓存</th>
    <th>推理</th><th>总 tokens</th><th>占比</th><th>费用</th><th>记录</th></tr></thead>
    <tbody>${agentRows}</tbody></table>
    <div class="sub" style="margin:12px 0 6px">按模型明细</div>
    <table class="table"><thead><tr><th>Agent</th><th>模型</th><th>输入</th><th>输出</th>
    <th>缓存</th><th>总 tokens</th><th>费用</th><th>记录</th><th>重试</th></tr></thead>
    <tbody>${detailRows || '<tr><td colspan="9" class="muted">暂无明细</td></tr>'}</tbody></table>`;
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
  const generation = state.wsGeneration;
  state.stream = es;
  es.addEventListener('project_changed', () => { disconnectStream(); scheduleRefresh(); });
  $('streamStatus').textContent = '连接中…';
  $('streamStatus').className = 'pill warn';
  es.addEventListener('hello', (e) => {
    $('streamStatus').textContent = '实时连接';
    $('streamStatus').className = 'pill ok';
    appendConsole('sys', '[系统] SSE 实时监控已连接');
    try { const d = JSON.parse(e.data); appendConsole('sys', '[系统] ' + (d.time || '')); } catch (err) {}
  });
  es.addEventListener('bus', (e) => {
    if (generation !== state.wsGeneration) return;
    try {
      const ev = JSON.parse(e.data);
      state.lastSeq = Math.max(state.lastSeq, Number(ev.seq || 0));
      const payload = JSON.stringify(ev.payload || {});
      appendConsole('bus', `[${ev.seq}] ${ev.created_at || ''} ${ev.kind} ${ev.topic} ← ${ev.source} :: ${payload.slice(0, 300)}`);
      if (['task_state', 'workflow', 'governance', 'agent_status', 'manual_action'].includes(ev.kind)) scheduleRefresh();
    } catch (err) {}
  });
  es.addEventListener('snapshot', (e) => {
    if (generation !== state.wsGeneration) return;
    try { renderLiveSnapshot(JSON.parse(e.data)); } catch (err) {}
  });
  es.addEventListener('logs', (e) => {
    if (generation !== state.wsGeneration) return;
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
    const wasStreaming = !!state.stream;
    state.wsGeneration += 1;
    clearTimeout(state.chainPersistTimer);
    state.chainPersistTimer = null;
    clearTimeout(state.refreshTimer);
    state.refreshTimer = null;
    disconnectStream();
    state.lastSeq = 0;
    state.overview = null;
    state.chainDraft = null;
    state.chainHydratedFor = '';
    clearConsole();
    await refresh();
    if (wasStreaming) connectStream();
    loadRecentProjects();
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
    const wasStreaming = !!state.stream;
    state.wsGeneration += 1;
    clearTimeout(state.chainPersistTimer);
    state.chainPersistTimer = null;
    clearTimeout(state.refreshTimer);
    state.refreshTimer = null;
    disconnectStream();
    state.lastSeq = 0;
    state.overview = null;
    state.plans = []; state.runs = []; state.tasks = []; state.approvals = [];
    state.chainDraft = null; state.chainHydratedFor = '';
    clearConsole();
    await refresh();
    if (wasStreaming) connectStream();
    loadRecentProjects();
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
      model: currentMainModel() };
    const r = await post('/api/plans', body);
    const jobUrl = new URL(window.location.href);
    jobUrl.searchParams.set('plan_job', r.job_id);
    window.history.replaceState({}, '', jobUrl);
    await watchPlanJob(r.job_id);
  } catch (e) { toast(e.message, true); }
}

async function watchPlanJob(jobId) {
  $('jobStatus').textContent = '规划中… ' + jobId;
  const planTimeout = Number((state.overview && state.overview.config
    && state.overview.config.plan_timeout_seconds) || 900);
  const limit = Math.ceil((planTimeout * 1000) / 1200) + 10;
  let job;
  for (let i = 0; i < limit; i++) {
    await new Promise((res) => setTimeout(res, 1200));
    job = await api('/api/jobs/' + jobId);
    if (job.status !== 'running') break;
    $('jobStatus').textContent = `规划中… ${(i * 1.2).toFixed(0)}s · ${job.phase || 'running'}`;
  }
  if (!job || job.status === 'running') {
    $('jobStatus').textContent = `规划仍在运行（job ${jobId}），可刷新页面继续查看`;
    toast('规划仍在运行，稍后刷新即可恢复查看', false);
    return;
  }
  $('jobStatus').textContent = '';
  if (job.status === 'failed' || job.status === 'interrupted') {
    toast((job.error && job.error.message) || '规划失败', true);
    await refresh();
    return;
  }
  const out = job.result || {};
  if (job.business_status && job.business_status !== 'pending_approval'
      && job.business_status !== 'completed') {
    toast('规划业务状态：' + job.business_status, false);
  }
  if (out.status === 'pending_approval') {
    toast('计划已生成，待审批；3.5 已载入该计划的阶段');
    await refresh();
    loadChainFromPlan(true);
    const url = new URL(window.location.href);
    url.searchParams.delete('plan_job');
    window.history.replaceState({}, '', url);
  } else if (out.status === 'waiting_user') {
    renderWaitingPlanCard(out);
  } else {
    toast(out.error || '规划未完成', true);
  }
}

function renderWaitingPlanCard(out) {
  const ma = out.manual_action || {};
  const importPath = out.result_path || out.plan_path || '';
  $('planResult').innerHTML = `<div class="card"><h3>等待主节点 UI 执行</h3><div class="sub">计划 ${esc(out.plan_id || '')} · 提示词：${esc(out.prompt_path || '')}</div><pre class="plain">${esc(out.message || ma.message || '')}</pre><div class="sub">${esc(ma.instructions || '')}</div><div class="sub" id="planWatchStatus">${out.plan_id ? '自动检测计划文件中…' : ''}</div><div class="row" style="margin-top:8px"><button class="small" onclick="openImportPlanModal(${esc(JSON.stringify(importPath))})">导入该计划 JSON</button></div></div>`;
  watchPlanCompletion(out.plan_id);
}

let planWatchTimer = null;
function watchPlanCompletion(planId) {
  if (planWatchTimer) { clearInterval(planWatchTimer); planWatchTimer = null; }
  if (!planId) return;
  const started = Date.now();
  planWatchTimer = setInterval(async () => {
    const el = $('planWatchStatus');
    try {
      const r = await api('/api/plans/watches');
      const w = (r.watches || []).find((x) => x.plan_id === planId);
      if (!w) { if (el) el.textContent = '监视记录不存在'; clearInterval(planWatchTimer); planWatchTimer = null; return; }
      if (w.status === 'completed') {
        clearInterval(planWatchTimer); planWatchTimer = null;
        toast('已检测到主节点写出的计划，载入待审批', false);
        await refresh();
        loadChainFromPlan(true);
        return;
      }
      if (['invalid', 'expired', 'failed', 'superseded'].includes(w.status)) {
        clearInterval(planWatchTimer); planWatchTimer = null;
        if (el) el.textContent = '自动接收结束：' + w.status + (w.error ? ' · ' + w.error : '');
        return;
      }
      if (el) el.textContent = `自动检测计划文件中… ${Math.round((Date.now() - started) / 1000)}s（完成后自动载入待审批）`;
    } catch (e) { /* 轮询失败下一轮重试 */ }
  }, 3000);
}

async function resumePlanJobFromUrl() {
  const jobId = new URLSearchParams(window.location.search).get('plan_job');
  if (!jobId) return;
  try {
    const job = await api('/api/jobs/' + jobId);
    if (job.status === 'running') {
      await watchPlanJob(jobId);
    } else {
      $('jobStatus').textContent = `上次规划 ${job.status}（job ${jobId}）`;
      const out = job.result || {};
      if (job.status === 'completed' && out.status === 'pending_approval') {
        await refresh();
        loadChainFromPlan(true);
        const url = new URL(window.location.href);
        url.searchParams.delete('plan_job');
        window.history.replaceState({}, '', url);
      } else if (job.status === 'completed' && out.status === 'waiting_user') {
        renderWaitingPlanCard(out);
      }
    }
  } catch (e) {
    $('jobStatus').textContent = '上次规划任务已不存在：' + jobId;
  }
}

async function openImportPlanModal(prefillPath) {
  // 2026-11-07 defect #11: ZCode's embedded browser has no prompt(); use an
  // in-page modal so plan import works in every supported browser surface.
  $('importPlanText').value = '';
  $('importPlanPath').value = prefillPath || '';
  $('importPlanModal').style.display = 'flex';
}
function closeImportPlanModal() {
  $('importPlanModal').style.display = 'none';
}
async function doImportPlan() {
  const text = $('importPlanText').value.trim();
  const path = $('importPlanPath').value.trim();
  if (!text && !path) { toast('请粘贴计划 JSON 或填写路径', true); return; }
  try {
    let body = {};
    if (text) {
      let t = text;
      if (t.startsWith('```')) {
        t = t.replace(/^```[^\n]*\n?/, '').replace(/\n?```\s*$/, '');
      }
      try { body.plan = JSON.parse(t); } catch (e) { body.path = text; }
    } else {
      body.path = path;
    }
    await post('/api/plans/import', body);
    closeImportPlanModal();
    toast('计划已导入，待审批');
    refresh();
  } catch (e) { toast(e.message, true); }
}

function openSummaryModal(runId) {
  state.summaryRun = runId || ((state.overview && state.overview.latest_run || {}).run_id || '');
  $('summaryText').value = '';
  $('summaryPath').value = '';
  $('summaryRunInfo').textContent = state.summaryRun
    ? `目标运行：${state.summaryRun}` : '没有可回填的运行';
  $('summaryModal').style.display = 'flex';
}
function closeSummaryModal() {
  $('summaryModal').style.display = 'none';
  state.summaryRun = '';
}
async function submitSummaryJson() {
  const runId = state.summaryRun;
  if (!runId) { toast('没有可回填的运行', true); return; }
  const text = $('summaryText').value.trim();
  const path = $('summaryPath').value.trim();
  if (!text && !path) { toast('请粘贴汇总 JSON 或填写路径', true); return; }
  try {
    let body = {};
    if (text) {
      let parsed;
      try { parsed = JSON.parse(text); } catch (e) { body.path = text; }
      if (parsed) body.summary = parsed;
    } else {
      body.path = path;
    }
    await post('/api/runs/' + encodeURIComponent(runId) + '/summary', body);
    closeSummaryModal();
    toast('汇总已提交');
    refresh();
  } catch (e) { toast(e.message, true); }
}

async function openAdoptModal(uid) {
  const task = (state.tasks || []).find((t) => t.uid === uid);
  state.adoptTask = task || { uid };
  $('adoptTitle').textContent = `采纳已有尝试成果 · ${task ? task.task_id : uid}`;
  $('adoptList').innerHTML = '加载中…';
  $('adoptModal').style.display = 'flex';
  try {
    const data = await api('/api/tasks/' + encodeURIComponent(uid) + '/attempts');
    const attempts = (data && data.attempts) || [];
    if (!attempts.length) {
      $('adoptList').innerHTML = '<div class="muted">该任务还没有尝试记录。</div>';
      return;
    }
    $('adoptList').innerHTML = `<table class="table"><thead><tr><th>尝试</th><th>状态</th>
      <th>结果文件</th><th>错误</th><th>选择</th></tr></thead><tbody>${attempts.map((a) => `
        <tr><td class="mono">#${esc(a.attempt_number)} ${esc(a.attempt_id)}${a.active ? ' <span class="pill info">活动</span>' : ''}</td>
        <td>${esc(a.status || '')}</td>
        <td>${a.result_exists ? '<span class="pill ok">有 result.json</span>' : '<span class="pill">无</span>'}</td>
        <td class="sub mono">${esc((a.error || '').slice(0, 120))}</td>
        <td><input type="radio" name="adoptAttempt" value="${esc(a.attempt_id)}"
          ${a.result_exists ? '' : 'disabled'} ${a.active && a.result_exists ? 'checked' : ''}
          style="width:auto"></td></tr>`).join('')}</tbody></table>`;
  } catch (e) {
    $('adoptList').innerHTML = `<div class="muted">加载失败：${esc(e.message)}</div>`;
  }
}
function closeAdoptModal() {
  $('adoptModal').style.display = 'none';
  state.adoptTask = null;
}
async function confirmAdopt() {
  const task = state.adoptTask;
  if (!task) return;
  const picked = document.querySelector('input[name="adoptAttempt"]:checked');
  if (!picked) { toast('请选择一个有 result.json 的尝试', true); return; }
  try {
    await post('/api/tasks/' + encodeURIComponent(task.uid) + '/adopt-attempt',
      { attempt_id: picked.value });
    closeAdoptModal();
    toast('已采纳该尝试成果，进入验证');
    refresh();
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
    // 2026-11-07 defect #9: an active attempt requires attempt_id/request_id;
    // the UI now sends the selected attempt instead of a bare payload.
    const attemptId = ($('resultAttempt') && $('resultAttempt').value) || '';
    if (attemptId) body.attempt_id = attemptId;
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
/* 工作目录选择：本机文件系统浏览（对应 DSH web 版的 browse 能力）      */
/* ------------------------------------------------------------------ */
const dirState = { path: '', entries: [], selected: '', hidden: false, seq: 0 };

function dirJoinName(name) {
  const base = dirState.path || '';
  if (!base) return name;
  const sep = base.includes('\\') ? '\\' : '/';
  return base.endsWith(sep) ? base + name : base + sep + name;
}

async function loadRecentProjects() {
  const box = $('recentBox');
  if (!box) return;
  try {
    const data = await api('/api/workspaces/recent');
    const rows = data.projects || [];
    if (!rows.length) { box.innerHTML = ''; return; }
    box.innerHTML = '<span class="sub" style="margin-right:2px">最近打开</span>';
    rows.forEach((project) => {
      const chip = document.createElement('span');
      chip.className = 'recent-chip';
      chip.title = project.path + (project.exists ? '' : '（目录已不存在）');
      chip.innerHTML = project.exists ? esc(project.name) : `<span class="miss">${esc(project.name)}</span>`;
      chip.onclick = () => {
        $('wsPath').value = project.path;
        $('wsPath').dataset.touched = '1';
        toast(project.exists ? '已填入最近项目' : '目录已不存在，请重新选择', !project.exists);
      };
      box.appendChild(chip);
    });
  } catch (e) { /* 最近列表只是便捷入口，失败不影响主流程 */ }
}

function openDirPicker() {
  $('dirModal').style.display = 'flex';
  const initial = ($('wsPath').value || '').trim();
  loadDir(initial);
}

function closeDirPicker() {
  $('dirModal').style.display = 'none';
  dirState.seq += 1;
}

function dirToggleHidden() {
  dirState.hidden = !dirState.hidden;
  $('dirHiddenBtn').textContent = dirState.hidden ? '隐藏隐藏项' : '显示隐藏项';
  if (dirState.last) renderDir(dirState.last);
}

async function loadDir(path) {
  const seq = ++dirState.seq;
  $('dirList').innerHTML = '<div class="empty">加载中…</div>';
  const url = '/api/fs/list' + (path ? ('?path=' + encodeURIComponent(path)) : '');
  try {
    const data = await api(url);
    if (seq !== dirState.seq) return;  // 已被更晚的一次导航取代
    dirState.last = data;
    dirState.path = data.path || '';
    dirState.entries = data.entries || [];
    dirState.selected = '';
    renderDir(data);
  } catch (e) {
    if (seq !== dirState.seq) return;
    $('dirList').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  }
}

function renderDir(data) {
  $('dirPathInput').value = data.path || '';
  $('dirCrumbs').innerHTML = (data.ancestry || []).map((node, index) =>
    (index ? '<span class="sep">›</span>' : '') +
    `<a data-path="${esc(node.path)}">${esc(node.name)}</a>`).join('');
  $('dirCrumbs').querySelectorAll('a').forEach((link) => {
    link.onclick = () => loadDir(link.dataset.path);
  });
  $('dirRoots').innerHTML = (data.roots || []).map((root) =>
    `<button class="small" data-path="${esc(root.path)}">${esc(root.name)}</button>`).join('');
  $('dirRoots').querySelectorAll('button').forEach((button) => {
    button.onclick = () => loadDir(button.dataset.path);
  });

  const rows = dirState.entries.filter((entry) => dirState.hidden || !entry.hidden);
  if (!rows.length) {
    $('dirList').innerHTML = `<div class="empty">${esc(data.error || '空目录')}</div>`;
  } else {
    $('dirList').innerHTML = '';
    rows.forEach((entry) => {
      const row = document.createElement('div');
      row.className = 'dirrow' + (entry.is_dir ? '' : ' file');
      row.innerHTML = `<span>${entry.is_dir ? '📁' : '📄'}</span>
        <span class="nm">${esc(entry.name)}</span>
        <span class="meta">${entry.is_dir ? '目录' : (entry.size ? Math.max(1, Math.round(entry.size / 1024)) + ' KB' : '文件')}</span>`;
      if (entry.is_dir) {
        row.onclick = () => {
          dirState.selected = entry.path;
          $('dirSelected').textContent = entry.name;
          $('dirList').querySelectorAll('.dirrow').forEach((el) => el.classList.remove('sel'));
          row.classList.add('sel');
        };
        row.ondblclick = () => loadDir(entry.path);
      } else {
        row.style.opacity = '.55';
      }
      $('dirList').appendChild(row);
    });
  }
  $('dirSelected').textContent = '未选择';
  if (data.error) toast(data.error, true);
}

function dirUp() {
  const parent = (dirState.last || {}).parent;
  if (parent) loadDir(parent);
}

function dirHome() {
  const home = (dirState.last || {}).home;
  if (home) loadDir(home);
}

function dirGo() {
  const value = ($('dirPathInput').value || '').trim();
  if (value) loadDir(value);
}

async function dirMkdir() {
  const name = ($('dirNewName').value || '').trim();
  if (!name) { toast('请输入文件夹名', true); return; }
  try {
    const entry = await post('/api/fs/mkdir', { path: dirState.path, name });
    $('dirNewName').value = '';
    await loadDir(dirState.path);
    dirState.selected = entry.path;
    $('dirSelected').textContent = entry.name;
    toast('已创建 ' + entry.name);
  } catch (e) { toast(e.message, true); }
}

function dirConfirm() {
  const chosen = dirState.selected || dirState.path;
  if (!chosen) { toast('请先选择目录', true); return; }
  $('wsPath').value = chosen;
  $('wsPath').dataset.touched = '1';
  closeDirPicker();
  toast('已选择工作目录');
}

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && $('dirModal') && $('dirModal').style.display !== 'none') {
    closeDirPicker();
  }
});

/* ------------------------------------------------------------------ */
/* Render orchestrator / boot                                          */
/* ------------------------------------------------------------------ */
function render() {
  renderHeader();
  renderWorkspace();
  renderFullControl();
  renderMultiRound();
  if (state.overview) {
    renderCommandAgents();
    renderModelSync();
    renderChain();
    renderCommandApprovals();
    renderCommandRun();
    renderMainActivity();
    renderCommandChainNodes();
    renderBlockAlert();
    renderCommandPlans();
    renderLiveTasks();
  } else {
    const alertBox = $('blockAlert');
    if (alertBox) alertBox.classList.add('hidden');
  }
  if (state.overview) {
    const sa = $('sessionAgent');
    const sessionAgents = (state.overview.agents.list || []).filter((a) => a.capabilities && a.capabilities.session_read);
    const current = sa.value;
    sa.innerHTML = sessionAgents.map((a) => `<option value="${esc(a.agent_id)}">${esc(a.name)}</option>`).join('');
    if (current && sessionAgents.some((a) => a.agent_id === current)) sa.value = current;
  }
  renderAgents();
  renderSurface();
  renderVersions();
  renderRoles();
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
resumePlanJobFromUrl();
loadRecentProjects();
setInterval(() => {
  if (state.workspace && state.workspace.open && !state.stream
      && ['agents', 'graph', 'task', 'approvals', 'bus', 'audit', 'versions'].includes(state.view)) {
    refresh();
  }
}, 12000);

// Command view: keep progress and the blocked-node popup fresh even when the
// SSE stream is disconnected. scheduleRefresh() skips while a field is focused.
setInterval(() => {
  const run = (state.overview || {}).latest_run || {};
  if (state.workspace && state.workspace.open && state.view === 'command'
      && ['running', 'waiting_user', 'paused'].includes(run.status)) {
    scheduleRefresh();
  }
}, 4000);
