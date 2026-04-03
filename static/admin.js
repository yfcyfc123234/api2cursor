const API = '';
let authKey = '';
let editingName = null;
let liveEs = null;
let livePaused = false;
let liveItems = 0;
let currentLogId = null;

function togglePwd(id) {
  const el = document.getElementById(id);
  el.type = el.type === 'password' ? 'text' : 'password';
}

function toast(msg, ok = true) {
  const area = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + (ok ? 'toast-ok' : 'toast-err');
  el.textContent = msg;
  area.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (authKey) headers['Authorization'] = 'Bearer ' + authKey;
  const res = await fetch(API + path, { ...opts, headers });
  const ct = res.headers.get('content-type') || '';
  if (!ct.includes('application/json')) {
    const text = await res.text();
    if (!res.ok) throw new Error('HTTP ' + res.status + ': ' + text.substring(0, 100));
    throw new Error('服务器返回了非 JSON 响应');
  }
  const data = await res.json();
  if (!res.ok) {
    const e = data.error;
    const msg = (typeof e === 'object' && e !== null) ? (e.message || JSON.stringify(e)) : (e || data.message || 'HTTP ' + res.status);
    throw new Error(msg);
  }
  return data;
}

// ─── 登录 ───────────────────────────────────────────
async function doLogin() {
  const key = document.getElementById('loginKey').value.trim();
  if (!key) { toast('请输入密钥', false); return; }
  try {
    const r = await api('/api/admin/login', { method: 'POST', body: JSON.stringify({ key }) });
    if (r.ok) {
      authKey = key;
      sessionStorage.setItem('_ak', key);
      document.getElementById('login').style.display = 'none';
      document.getElementById('dashboard').style.display = 'block';
      loadDashboard();
    }
  } catch (e) {
    toast('密钥无效', false);
  }
}

function doLogout() {
  authKey = '';
  sessionStorage.removeItem('_ak');
  if (liveEs) {
    try { liveEs.close(); } catch { }
    liveEs = null;
  }
  document.getElementById('dashboard').style.display = 'none';
  document.getElementById('login').style.display = 'flex';
}

// ─── 仪表盘 ─────────────────────────────────────────
async function loadDashboard() {
  try {
    const s = await api('/api/admin/settings');
    const targetUrlEl = document.getElementById('targetUrl');
    if (targetUrlEl) targetUrlEl.value = s.proxy_target_url || '';
    const proxyKeyEl = document.getElementById('proxyKey');
    if (proxyKeyEl) proxyKeyEl.value = s.proxy_api_key || '';
    const debugModeEl = document.getElementById('debugMode');
    if (debugModeEl) debugModeEl.value = s.debug_mode || 'off';
    const envUrlEl = document.getElementById('envUrl');
    if (envUrlEl) envUrlEl.textContent = s.env_target_url ? '环境变量: ' + s.env_target_url : '';
    const envKeyEl = document.getElementById('envKey');
    if (envKeyEl) envKeyEl.textContent = s.env_api_key ? '环境变量: (已配置)' : '环境变量: (未设置)';

    const mappingListEl = document.getElementById('mappingList');
    if (mappingListEl) await loadMappings();

    const statusBadgeEl = document.getElementById('statusBadge');
    if (statusBadgeEl) checkHealth();

    const statsContentEl = document.getElementById('statsContent');
    if (statsContentEl) loadStats();

    const liveLogsEl = document.getElementById('liveLogs');
    if (liveLogsEl) connectLiveLogs();

    const logsListEl = document.getElementById('logsList');
    if (logsListEl) loadLogs();
  } catch (e) {
    toast('加载设置失败: ' + e.message, false);
  }
}

async function loadStats() {
  const el = document.getElementById('statsContent');
  if (!el) return;
  try {
    const data = await api('/api/admin/stats');
    const models = data.models || {};
    const keys = Object.keys(models);
    if (!keys.length) {
      el.innerHTML = '<div class="empty">暂无请求统计数据</div>';
      return;
    }
    const uptime = data.uptime_seconds || 0;
    const h = Math.floor(uptime / 3600);
    const m = Math.floor((uptime % 3600) / 60);
    let html = '<div class="hint" style="margin-bottom:12px">运行时长: ' + h + '小时' + m + '分钟</div>';
    html += '<table class="stats-table"><thead><tr><th>模型</th><th>请求数</th><th>输入 Tokens</th><th>输出 Tokens</th><th>总 Tokens</th></tr></thead><tbody>';
    keys.sort((a, b) => models[b].request_count - models[a].request_count);
    for (const name of keys) {
      const s = models[name];
      html += '<tr><td>' + esc(name) + '</td><td>' + s.request_count + '</td><td>' + s.input_tokens.toLocaleString() + '</td><td>' + s.output_tokens.toLocaleString() + '</td><td>' + s.total_tokens.toLocaleString() + '</td></tr>';
    }
    html += '</tbody></table>';
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = '<div class="empty">加载统计失败</div>';
  }
}

async function checkHealth() {
  try {
    const r = await fetch(API + '/health');
    const d = await r.json();
    const b = document.getElementById('statusBadge');
    if (!b) return;
    if (d.status === 'ok') {
      b.textContent = '已连接';
      b.style.background = 'rgba(34,197,94,.15)';
      b.style.color = 'var(--green)';
    } else {
      b.textContent = '异常';
    }
  } catch {
    const b = document.getElementById('statusBadge');
    b.textContent = '离线';
    b.style.background = 'rgba(239,68,68,.15)';
    b.style.color = 'var(--red)';
  }
}

async function saveSettings() {
  try {
    await api('/api/admin/settings', {
      method: 'PUT',
      body: JSON.stringify({
        proxy_target_url: document.getElementById('targetUrl').value.trim(),
        proxy_api_key: document.getElementById('proxyKey').value.trim(),
        debug_mode: document.getElementById('debugMode').value,
      }),
    });
    toast('设置已保存');
  } catch (e) {
    toast('保存失败: ' + e.message, false);
  }
}

// ─── 模型映射 ───────────────────────────────────────
async function loadMappings() {
  const mappings = await api('/api/admin/mappings');
  const el = document.getElementById('mappingList');
  if (!el) return;
  const keys = Object.keys(mappings);

  if (!keys.length) {
    el.innerHTML = '<div class="empty">暂无模型映射<br><span style="font-size:13px">点击「+ 添加映射」开始配置</span></div>';
    return;
  }

  el.innerHTML = '<div class="mapping-list">' + keys.map(name => {
    const m = mappings[name];
    const backend = m.backend || 'auto';
    const tagClass = backend === 'anthropic'
      ? 'tag-anthropic'
      : backend === 'responses'
        ? 'tag-responses'
        : backend === 'openai'
          ? 'tag-openai'
          : backend === 'gemini'
            ? 'tag-gemini'
            : 'tag-auto';
    const tagLabel = backend === 'auto'
      ? '自动'
      : backend === 'responses'
        ? 'responses'
        : backend;
    const hasOverride = m.target_url || m.api_key;
    const hasInstructions = !!m.custom_instructions;
    const hasBodyMods = m.body_modifications && Object.keys(m.body_modifications).length > 0;
    const hasHeaderMods = m.header_modifications && Object.keys(m.header_modifications).length > 0;
    return `<div class="mapping-item">
      <div class="mapping-top">
        <span class="mapping-name">${esc(name)}</span>
        <span class="mapping-arrow">&rarr;</span>
        <span class="mapping-upstream">${esc(m.upstream_model || name)}</span>
        <div class="mapping-meta">
          <span class="tag ${tagClass}">${tagLabel}</span>
          ${hasOverride ? '<span class="tag tag-override">自定义地址</span>' : ''}
          ${hasInstructions ? '<span class="tag tag-instructions">自定义指令</span>' : ''}
          ${hasBodyMods ? '<span class="tag tag-mods">Body修改</span>' : ''}
          ${hasHeaderMods ? '<span class="tag tag-mods">Header修改</span>' : ''}
        </div>
        <div class="mapping-actions">
          <button class="btn btn-ghost btn-sm" onclick="openEditModal('${esc(name)}')">编辑</button>
          <button class="btn btn-red btn-sm" onclick="deleteMapping('${esc(name)}')">删除</button>
        </div>
      </div>
    </div>`;
  }).join('') + '</div>';
}

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

// ─── 弹窗 ──────────────────────────────────────────
function openAddModal() {
  editingName = null;
  document.getElementById('modalTitle').textContent = '添加模型映射';
  document.getElementById('mName').value = '';
  document.getElementById('mName').disabled = false;
  document.getElementById('mUpstream').value = '';
  document.getElementById('mBackend').value = 'auto';
  document.getElementById('mUrl').value = '';
  document.getElementById('mKey').value = '';
  document.getElementById('mInstructions').value = '';
  document.getElementById('mInsPosition').value = 'prepend';
  document.getElementById('mBodyMods').value = '';
  document.getElementById('mHeaderMods').value = '';
  document.getElementById('modal').classList.add('active');
}

async function openEditModal(name) {
  editingName = name;
  document.getElementById('modalTitle').textContent = '编辑模型映射';
  try {
    const mappings = await api('/api/admin/mappings');
    const m = mappings[name];
    if (!m) { toast('映射未找到', false); return; }
    document.getElementById('mName').value = name;
    document.getElementById('mName').disabled = false;
    document.getElementById('mUpstream').value = m.upstream_model || '';
    document.getElementById('mBackend').value = m.backend || 'auto';
    document.getElementById('mUrl').value = m.target_url || '';
    document.getElementById('mKey').value = m.api_key || '';
    document.getElementById('mInstructions').value = m.custom_instructions || '';
    document.getElementById('mInsPosition').value = m.instructions_position || 'prepend';
    document.getElementById('mBodyMods').value = m.body_modifications && Object.keys(m.body_modifications).length ? JSON.stringify(m.body_modifications, null, 2) : '';
    document.getElementById('mHeaderMods').value = m.header_modifications && Object.keys(m.header_modifications).length ? JSON.stringify(m.header_modifications, null, 2) : '';
    document.getElementById('modal').classList.add('active');
  } catch (e) {
    toast('错误: ' + e.message, false);
  }
}

function closeModal() {
  document.getElementById('modal').classList.remove('active');
  editingName = null;
}

async function saveMapping() {
  const name = document.getElementById('mName').value.trim();
  const upstream = document.getElementById('mUpstream').value.trim();
  if (!name) { toast('请填写 Cursor 模型名', false); return; }
  if (!upstream) { toast('请填写上游模型名', false); return; }

  let bodyMods = {};
  const bodyModsStr = document.getElementById('mBodyMods').value.trim();
  if (bodyModsStr) {
    try { bodyMods = JSON.parse(bodyModsStr); }
    catch { toast('Body 修改不是有效的 JSON', false); return; }
  }

  let headerMods = {};
  const headerModsStr = document.getElementById('mHeaderMods').value.trim();
  if (headerModsStr) {
    try { headerMods = JSON.parse(headerModsStr); }
    catch { toast('Header 修改不是有效的 JSON', false); return; }
  }

  const payload = {
    name,
    upstream_model: upstream,
    backend: document.getElementById('mBackend').value,
    target_url: document.getElementById('mUrl').value.trim(),
    api_key: document.getElementById('mKey').value.trim(),
    custom_instructions: document.getElementById('mInstructions').value,
    instructions_position: document.getElementById('mInsPosition').value,
    body_modifications: bodyMods,
    header_modifications: headerMods,
  };

  try {
    if (editingName) {
      await api('/api/admin/mappings/' + encodeURIComponent(editingName), {
        method: 'PUT', body: JSON.stringify(payload),
      });
      toast('映射已更新');
    } else {
      await api('/api/admin/mappings', {
        method: 'POST', body: JSON.stringify(payload),
      });
      toast('映射已添加');
    }
    closeModal();
    await loadMappings();
  } catch (e) {
    toast('操作失败: ' + e.message, false);
  }
}

async function deleteMapping(name) {
  if (!confirm('确定要删除映射「' + name + '」吗？')) return;
  try {
    await api('/api/admin/mappings/' + encodeURIComponent(name), { method: 'DELETE' });
    toast('映射已删除');
    await loadMappings();
  } catch (e) {
    toast('删除失败: ' + e.message, false);
  }
}

// ─── Config Import/Export ──────────────────────────────
async function exportConfig() {
  try {
    const data = await api('/api/admin/config/export');
    const json = JSON.stringify(data, null, 2);
    const ta = document.getElementById('configJson');
    if (ta) ta.value = json;

    const blob = new Blob([json], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const ts = new Date().toISOString().replace(/[:.]/g, '-');
    a.href = url;
    a.download = `api2cursor-config-${ts}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    toast('已导出配置');
  } catch (e) {
    toast('导出失败: ' + e.message, false);
  }
}

function pickImportFile() {
  const input = document.getElementById('importFile');
  if (!input) return;
  input.value = '';
  input.click();
}

async function importConfigObject(obj) {
  await api('/api/admin/config/import', {
    method: 'POST',
    body: JSON.stringify(obj),
  });
}

async function importConfigFromTextarea() {
  const ta = document.getElementById('configJson');
  if (!ta) { toast('找不到输入框', false); return; }
  const text = (ta.value || '').trim();
  if (!text) { toast('请粘贴 JSON', false); return; }
  if (!confirm('确定要导入并覆盖当前配置吗？')) return;
  try {
    const obj = JSON.parse(text);
    await importConfigObject(obj);
    toast('配置已导入');
    await loadDashboard();
  } catch (e) {
    toast('导入失败: ' + (e.message || e), false);
  }
}

const importFileEl = document.getElementById('importFile');
if (importFileEl) {
  importFileEl.addEventListener('change', async function() {
    const f = this.files && this.files[0];
    if (!f) return;
    if (!confirm('确定要导入并覆盖当前配置吗？')) return;
    try {
      const text = await f.text();
      const obj = JSON.parse(text);
      await importConfigObject(obj);
      toast('配置已导入');
      await loadDashboard();
    } catch (e) {
      toast('导入失败: ' + (e.message || e), false);
    }
  });
}

// ─── Logs ZIP export ───────────────────────────────────
function localDatetimeToIso(id) {
  const el = document.getElementById(id);
  if (!el || !el.value) return '';
  const d = new Date(el.value);
  if (isNaN(d.getTime())) return '';
  return d.toISOString();
}

async function downloadLogsZip(payload) {
  const headers = { 'Content-Type': 'application/json' };
  if (authKey) headers['Authorization'] = 'Bearer ' + authKey;
  const res = await fetch(API + '/api/admin/logs/export', {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const ct = res.headers.get('content-type') || '';
    if (ct.includes('application/json')) {
      const j = await res.json();
      const e = j.error;
      const msg = (typeof e === 'object' && e !== null) ? (e.message || JSON.stringify(e)) : (e || 'HTTP ' + res.status);
      throw new Error(msg);
    }
    const text = await res.text();
    throw new Error('HTTP ' + res.status + ': ' + text.substring(0, 200));
  }
  const blob = await res.blob();
  const cd = res.headers.get('content-disposition') || '';
  let name = 'api2cursor-logs.zip';
  const m = /filename\*?=(?:UTF-8'')?["']?([^";\n]+)/i.exec(cd);
  if (m && m[1]) name = decodeURIComponent(m[1].replace(/"/g, ''));
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  toast('已下载 ZIP');
}

async function exportLogsZipAll() {
  if (!confirm('将导出全部会话日志（可能较大），是否继续？')) return;
  try {
    await downloadLogsZip({ all: true });
  } catch (e) {
    toast('导出失败: ' + e.message, false);
  }
}

async function exportLogsZipRange() {
  const start = localDatetimeToIso('logExportStart');
  const end = localDatetimeToIso('logExportEnd');
  if (!start || !end) {
    toast('请填写开始与结束时间（精确到秒）', false);
    return;
  }
  if (!confirm('将按时间范围导出会话日志，是否继续？')) return;
  try {
    await downloadLogsZip({ all: false, start, end });
  } catch (e) {
    toast('导出失败: ' + e.message, false);
  }
}

// ─── Live Logs ─────────────────────────────────────────
function clearLiveLogs() {
  const el = document.getElementById('liveLogs');
  if (!el) return;
  el.innerHTML = '<div class="empty">已清空</div>';
  liveItems = 0;
}

function toggleLivePause() {
  livePaused = !livePaused;
  const btn = document.getElementById('livePauseBtn');
  if (btn) btn.textContent = livePaused ? '继续' : '暂停';
  toast(livePaused ? '已暂停实时日志' : '已继续实时日志');
}

function liveKindClass(kind) {
  const k = String(kind || '').toLowerCase();
  if (k === 'error') return 'log-kind log-kind-error';
  if (k.includes('client')) return 'log-kind log-kind-client';
  if (k.includes('upstream')) return 'log-kind log-kind-upstream';
  if (k.includes('summary') || k.includes('done') || k.includes('turn_done')) return 'log-kind log-kind-summary';
  return 'log-kind';
}

function appendLiveLog(evt) {
  if (livePaused) return;
  const container = document.getElementById('liveLogs');
  if (!container) return;
  if (liveItems === 0) container.innerHTML = '';
  if (container.firstChild && liveItems > 180) container.removeChild(container.firstChild);

  const line = document.createElement('div');
  line.className = 'log-line';

  const meta = document.createElement('div');
  meta.className = 'log-meta';

  const kind = document.createElement('span');
  kind.className = liveKindClass(evt.kind);
  kind.textContent = evt.kind || '';

  const ts = document.createElement('span');
  ts.textContent = evt.ts ? String(evt.ts).replace('T', ' ').replace('Z', '') : '';

  const route = document.createElement('span');
  route.textContent = evt.route ? ('[' + evt.route + ']') : '';

  const model = document.createElement('span');
  model.textContent = evt.client_model ? ('model=' + evt.client_model) : '';

  meta.appendChild(kind);
  meta.appendChild(ts);
  meta.appendChild(route);
  meta.appendChild(model);

  const pre = document.createElement('pre');
  pre.className = 'log-payload';
  pre.textContent = evt.payload || '';

  line.appendChild(meta);
  line.appendChild(pre);
  container.appendChild(line);

  liveItems += 1;
}

function connectLiveLogs() {
  const container = document.getElementById('liveLogs');
  if (!container) return;

  if (liveEs) {
    try { liveEs.close(); } catch { }
    liveEs = null;
  }

  livePaused = false;
  liveItems = 0;
  if (document.getElementById('livePauseBtn')) document.getElementById('livePauseBtn').textContent = '暂停';
  container.innerHTML = '<div class="empty">连接中…</div>';

  const key = authKey ? encodeURIComponent(authKey) : '';
  const url = API + '/api/admin/logs/live?key=' + key;

  try {
    liveEs = new EventSource(url);
  } catch (e) {
    container.innerHTML = '<div class="empty">无法建立 SSE 连接</div>';
    return;
  }

  liveEs.onmessage = (e) => {
    let msg = null;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (!msg) return;
    if (msg.type === 'ping') return;
    if (msg.type === 'hello') {
      container.innerHTML = '<div class="empty">已连接</div>';
      return;
    }
    appendLiveLog(msg);
  };

  liveEs.onerror = () => {
    // EventSource 会自动重连；这里保持 UI 友好
    if (container.innerText.indexOf('离线') !== -1) return;
    container.innerHTML = '<div class="empty">离线（可手动刷新或等待重连）</div>';
  };
}

// ─── History Logs (CRUD) ───────────────────────────────
async function loadLogs() {
  const list = document.getElementById('logsList');
  const detail = document.getElementById('logsDetail');
  if (!list || !detail) return;

  list.innerHTML = '<div class="empty">加载中…</div>';
  detail.innerHTML = '<div class="empty">请选择一条日志</div>';

  const q = document.getElementById('logsSearch') ? document.getElementById('logsSearch').value.trim() : '';
  const qs = ['limit=40'];
  if (q) qs.push('q=' + encodeURIComponent(q));

  try {
    const data = await api('/api/admin/logs?' + qs.join('&'));
    const items = data.items || [];
    if (!items.length) {
      list.innerHTML = '<div class="empty">暂无数据</div>';
      return;
    }

    list.innerHTML = '';
    for (const it of items) {
      const row = document.createElement('div');
      row.className = 'logs-row';
      row.onclick = () => viewLogDetail(it.conversation_id);

      const top = document.createElement('div');
      top.className = 'row-top';

      const id = document.createElement('div');
      id.className = 'row-id';
      id.textContent = it.conversation_id;

      const pill = document.createElement('span');
      pill.className = 'log-kind';
      pill.textContent = it.route || 'unknown';

      top.appendChild(id);
      top.appendChild(pill);

      const meta = document.createElement('div');
      meta.className = 'row-meta';
      meta.textContent = `updated: ${it.updated_at || ''} | model: ${it.last_client_model || ''} | turns: ${it.turn_count || 0}`;

      row.appendChild(top);
      row.appendChild(meta);
      list.appendChild(row);
    }
  } catch (e) {
    list.innerHTML = '<div class="empty">加载失败</div>';
    toast('加载日志失败: ' + e.message, false);
  }
}

async function viewLogDetail(conversationId) {
  if (!conversationId) return;
  currentLogId = conversationId;
  const detail = document.getElementById('logsDetail');
  if (!detail) return;

  detail.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const data = await api('/api/admin/logs/' + encodeURIComponent(conversationId));
    const conv = data.conversation || {};
    const note = data.note || '';

    detail.innerHTML = '';

    const actions = document.createElement('div');
    actions.className = 'log-detail-actions';

    const delBtn = document.createElement('button');
    delBtn.className = 'btn btn-red btn-sm';
    delBtn.textContent = '删除日志';
    delBtn.onclick = () => deleteLog(conversationId);

    const refreshNoteBtn = document.createElement('button');
    refreshNoteBtn.className = 'btn btn-ghost btn-sm';
    refreshNoteBtn.textContent = '保存备注';
    refreshNoteBtn.onclick = () => saveLogNote();

    actions.appendChild(delBtn);
    actions.appendChild(refreshNoteBtn);
    detail.appendChild(actions);

    const meta = document.createElement('div');
    meta.className = 'hint';
    meta.textContent = `conversation=${conversationId} | route=${conv.route || ''} | turns=${conv.turn_count || 0} | updated=${conv.updated_at || ''}`;
    detail.appendChild(meta);

    const noteField = document.createElement('div');
    noteField.className = 'field';
    noteField.style.marginTop = '12px';

    const label = document.createElement('label');
    label.textContent = '备注（可选，用于标记调试重点）';

    const input = document.createElement('textarea');
    input.id = 'logNoteInput';
    input.value = note;
    input.rows = 3;
    input.style.resize = 'vertical';
    input.className = 'input';

    noteField.appendChild(label);
    noteField.appendChild(document.createElement('div'));
    noteField.lastChild.className = 'input-wrap';
    noteField.lastChild.appendChild(input);
    detail.appendChild(noteField);

    const pre = document.createElement('pre');
    pre.className = 'log-json';
    let s = '';
    try { s = JSON.stringify(conv, null, 2); } catch { s = String(conv); }
    if (s.length > 60000) s = s.slice(0, 60000) + '\n...[truncated]...';
    pre.textContent = s;
    detail.appendChild(pre);
  } catch (e) {
    detail.innerHTML = '<div class="empty">加载失败</div>';
    toast('查看日志失败: ' + e.message, false);
  }
}

async function deleteLog(conversationId) {
  if (!confirm('确定要删除该会话日志吗？')) return;
  try {
    await api('/api/admin/logs/' + encodeURIComponent(conversationId), { method: 'DELETE' });
    toast('日志已删除');
    currentLogId = null;
    await loadLogs();
  } catch (e) {
    toast('删除失败: ' + e.message, false);
  }
}

async function saveLogNote() {
  if (!currentLogId) return;
  const ta = document.getElementById('logNoteInput');
  const note = ta ? ta.value : '';
  try {
    await api('/api/admin/logs/' + encodeURIComponent(currentLogId) + '/note', {
      method: 'PUT',
      body: JSON.stringify({ note }),
    });
    toast('备注已保存');
    await loadLogs();
  } catch (e) {
    toast('保存备注失败: ' + e.message, false);
  }
}

function openClearProgressModal() {
  const el = document.getElementById('clearProgressModal');
  if (!el) return;
  el.classList.add('active');
  el.setAttribute('aria-hidden', 'false');
  const bar = document.getElementById('clearProgressBarInner');
  const track = document.getElementById('clearProgressTrack');
  if (bar) bar.style.width = '0%';
  if (track) track.setAttribute('aria-valuenow', '0');
  const t = document.getElementById('clearProgressText');
  if (t) t.textContent = '正在连接服务器…';
  const d = document.getElementById('clearProgressDetail');
  if (d) d.textContent = '';
  const c = document.getElementById('clearProgressClose');
  if (c) c.disabled = true;
}

function closeClearProgressModal() {
  const el = document.getElementById('clearProgressModal');
  if (!el) return;
  el.classList.remove('active');
  el.setAttribute('aria-hidden', 'true');
}

function applyClearProgressPayload(msg) {
  const bar = document.getElementById('clearProgressBarInner');
  const track = document.getElementById('clearProgressTrack');
  const text = document.getElementById('clearProgressText');
  const detail = document.getElementById('clearProgressDetail');
  if (!msg || typeof msg !== 'object') return;

  if (msg.phase === 'start') {
    if (text) {
      text.textContent =
        msg.total === 0
          ? '没有需要删除的日志文件'
          : '共 ' + msg.total + ' 个文件，开始删除…';
    }
    if (bar) bar.style.width = msg.total ? '3%' : '100%';
    if (track) track.setAttribute('aria-valuenow', msg.total ? '3' : '100');
  } else if (msg.phase === 'progress') {
    const pct = msg.total ? Math.min(100, Math.round((msg.done / msg.total) * 100)) : 100;
    if (bar) bar.style.width = pct + '%';
    if (track) track.setAttribute('aria-valuenow', String(pct));
    if (text) text.textContent = '已删除 ' + msg.done + ' / ' + msg.total;
    if (detail) {
      let s = '';
      if (msg.errors) s += '失败 ' + msg.errors + ' 个。';
      if (msg.current) s += (s ? ' ' : '') + '当前：' + msg.current;
      detail.textContent = s;
    }
  } else if (msg.phase === 'done') {
    if (bar) bar.style.width = '100%';
    if (track) track.setAttribute('aria-valuenow', '100');
    if (text) {
      text.textContent =
        '完成：成功删除 ' +
        msg.removed +
        ' 个' +
        (msg.errors ? '，失败 ' + msg.errors + ' 个' : '');
    }
    if (detail) detail.textContent = '';
  } else if (msg.phase === 'error') {
    if (text) text.textContent = '出错';
    if (detail) detail.textContent = msg.message || '未知错误';
  }
}

async function clearLogs() {
  if (!confirm('确定要清空历史日志吗？这会删除服务器上的 conversations json 文件。')) return;

  const hasModal = !!document.getElementById('clearProgressModal');
  if (!hasModal) {
    try {
      await api('/api/admin/logs/clear', {
        method: 'POST',
        body: JSON.stringify({ confirm: true }),
      });
      toast('历史日志已清空');
      currentLogId = null;
      await loadLogs();
    } catch (e) {
      toast('清空失败: ' + e.message, false);
    }
    return;
  }

  openClearProgressModal();
  let streamError = null;
  try {
    const headers = { 'Content-Type': 'application/json' };
    if (authKey) headers['Authorization'] = 'Bearer ' + authKey;
    const res = await fetch(API + '/api/admin/logs/clear', {
      method: 'POST',
      headers,
      body: JSON.stringify({ confirm: true }),
    });

    if (!res.ok) {
      const text = await res.text();
      let errMsg = 'HTTP ' + res.status;
      try {
        const j = JSON.parse(text);
        errMsg =
          (j.error && (j.error.message || j.error)) ||
          j.message ||
          j.error ||
          errMsg;
      } catch {
        if (text) errMsg = text.substring(0, 200);
      }
      throw new Error(errMsg);
    }

    const reader = res.body && res.body.getReader();
    if (!reader) throw new Error('无法读取响应流');

    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop() || '';
      for (let li = 0; li < lines.length; li++) {
        const line = lines[li].trim();
        if (!line) continue;
        let msg;
        try {
          msg = JSON.parse(line);
        } catch {
          continue;
        }
        applyClearProgressPayload(msg);
        if (msg.phase === 'error') streamError = new Error(msg.message || '清空失败');
      }
    }
    const tail = buf.trim();
    if (tail) {
      try {
        const msg = JSON.parse(tail);
        applyClearProgressPayload(msg);
        if (msg.phase === 'error') streamError = new Error(msg.message || '清空失败');
      } catch {
        /* ignore */
      }
    }

    if (streamError) throw streamError;

    const closeBtn = document.getElementById('clearProgressClose');
    if (closeBtn) closeBtn.disabled = false;
    toast('历史日志已清空');
    currentLogId = null;
    await loadLogs();
  } catch (e) {
    applyClearProgressPayload({ phase: 'error', message: e.message || String(e) });
    const closeBtn = document.getElementById('clearProgressClose');
    if (closeBtn) closeBtn.disabled = false;
    toast('清空失败: ' + e.message, false);
  }
}

// ─── 初始化 ─────────────────────────────────────────
(function init() {
  const saved = sessionStorage.getItem('_ak');
  if (saved) {
    authKey = saved;
    document.getElementById('login').style.display = 'none';
    document.getElementById('dashboard').style.display = 'block';
    loadDashboard();
  }
})();

const modalEl = document.getElementById('modal');
if (modalEl) {
  modalEl.addEventListener('click', function(e) {
    if (e.target === this) closeModal();
  });
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeModal();
  });
}
