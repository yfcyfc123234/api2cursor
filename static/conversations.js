/* ===== 认证 & 工具函数 ===== */
const API = '';
let authKey = '';
let liveEs = null;
let liveEnabled = true;

function togglePwd(id) {
  const el = document.getElementById(id);
  el.type = el.type === 'password' ? 'text' : 'password';
}

function toast(msg, ok) {
  if (ok === undefined) ok = true;
  var area = document.getElementById('toasts');
  var el = document.createElement('div');
  el.className = 'toast ' + (ok ? 'toast-ok' : 'toast-err');
  el.innerHTML = '<span>' + escHtml(String(msg)) + '</span>';
  if (!ok) {
    var btn = document.createElement('button');
    btn.textContent = '📋复制';
    btn.style.cssText = 'margin-left:8px;padding:1px 6px;font-size:11px;border:1px solid #555;border-radius:3px;background:#333;color:#ccc;cursor:pointer';
    btn.onclick = function() {
      var ta = document.createElement('textarea');
      ta.value = msg; ta.style.cssText = 'position:fixed;left:-9999px';
      document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); document.body.removeChild(ta);
      btn.textContent = '✅已复制';
      setTimeout(function(){ btn.textContent = '📋复制'; }, 2000);
    };
    el.appendChild(btn);
  }
  area.appendChild(el);
  setTimeout(function () { el.remove(); }, ok ? 3000 : 10000);
}

async function api(path, opts) {
  opts = opts || {};
  var headers = { 'Content-Type': 'application/json' };
  if (authKey) headers['Authorization'] = 'Bearer ' + authKey;
  var res = await fetch(API + path, Object.assign({}, opts, { headers: headers }));
  var ct = res.headers.get('content-type') || '';
  if (!ct.includes('application/json')) {
    var text = await res.text();
    if (!res.ok) throw new Error('HTTP ' + res.status + ': ' + text.substring(0, 100));
    throw new Error('服务器返回了非 JSON 响应');
  }
  var data = await res.json();
  if (!res.ok) {
    var e = data.error;
    var msg = (typeof e === 'object' && e !== null) ? (e.message || JSON.stringify(e)) : (e || data.message || 'HTTP ' + res.status);
    throw new Error(msg);
  }
  return data;
}

/* ===== 登录 ===== */
async function doLogin() {
  var key = document.getElementById('loginKey').value.trim();
  if (!key) { toast('请输入密钥', false); return; }
  try {
    var r = await api('/api/admin/login', { method: 'POST', body: JSON.stringify({ key: key }) });
    if (r.ok) {
      authKey = key;
      sessionStorage.setItem('_ak', key);
      document.getElementById('login').style.display = 'none';
      document.getElementById('dashboard').style.display = 'block';
      loadConversationList();
      startLiveSSE();
    }
  } catch (e) {
    toast('密钥无效', false);
  }
}

function doLogout() {
  authKey = '';
  sessionStorage.removeItem('_ak');
  if (liveEs) { try { liveEs.close(); } catch (e) {} liveEs = null; }
  document.getElementById('dashboard').style.display = 'none';
  document.getElementById('login').style.display = 'flex';
}

function autoLogin() {
  // 1. sessionStorage 缓存
  var saved = sessionStorage.getItem('_ak');
  // 2. URL hash #key=xxx (hash 不发送到服务器，安全)
  var hash = window.location.hash.substring(1);
  var hashKey = new URLSearchParams(hash).get('key');
  if (hashKey) saved = hashKey;
  if (saved) {
    authKey = saved;
    sessionStorage.setItem('_ak', saved);
    // 清除 hash 中的 key，避免留在地址栏
    if (hashKey) {
      history.replaceState(null, '', window.location.pathname + window.location.search);
    }
    document.getElementById('login').style.display = 'none';
    document.getElementById('dashboard').style.display = 'block';
    loadConversationList();
    startLiveSSE();
    return true;
  }
  return false;
}

/* ===== 会话列表 ===== */
var CONVERSATIONS = [];
var currentSort = { field: 'updated_at', dir: 'desc' };
var currentConvId = null;
var currentDate = null;
var activeFilters = {};
var editMode = false;
var selectedConvIds = {};

autoLogin();

function setSort(field) {
  if (currentSort.field === field) {
    currentSort.dir = currentSort.dir === 'asc' ? 'desc' : 'asc';
  } else {
    currentSort.field = field;
    currentSort.dir = 'desc';
  }
  updateSortButtons();
  renderConversationList();
}

function updateSortButtons() {
  var buttons = document.querySelectorAll('.sort-btn');
  buttons.forEach(function (btn) {
    var f = btn.getAttribute('data-sort');
    btn.classList.remove('active', 'asc', 'desc');
    if (f === currentSort.field) {
      btn.classList.add('active');
      btn.classList.add(currentSort.dir === 'asc' ? 'asc' : 'desc');
    }
  });
}

function onSearch() {
  renderConversationList();
}

// ─── 筛选标签 ──────────────────────────────────────
function toggleFilter(name) {
  if (activeFilters[name]) {
    delete activeFilters[name];
  } else {
    activeFilters[name] = true;
  }
  updateFilterTags();
  loadConversationList();
}

function updateFilterTags() {
  var tags = document.querySelectorAll('.filter-tag');
  tags.forEach(function(t) {
    var f = t.getAttribute('data-filter');
    t.classList.toggle('active', !!activeFilters[f]);
  });
}

// ─── 编辑模式 ──────────────────────────────────────
function toggleEditMode() {
  editMode = !editMode;
  selectedConvIds = {};
  document.getElementById('convList').classList.toggle('edit-mode', editMode);
  document.getElementById('convEditBar').style.display = editMode ? 'flex' : 'none';
  document.getElementById('btnEditMode').textContent = editMode ? '✖ 退出编辑' : '✏️ 编辑';
  if (!editMode) updateBatchDeleteBtn();
  renderConversationList();
}

function exitEditMode() {
  editMode = false;
  selectedConvIds = {};
  document.getElementById('convList').classList.remove('edit-mode');
  document.getElementById('convEditBar').style.display = 'none';
  document.getElementById('btnEditMode').textContent = '✏️ 编辑';
  renderConversationList();
}

function toggleSelectConv(convId) {
  if (selectedConvIds[convId]) {
    delete selectedConvIds[convId];
  } else {
    selectedConvIds[convId] = true;
  }
  updateBatchDeleteBtn();
}

function selectAll() {
  var all = CONVERSATIONS.length > 0;
  CONVERSATIONS.forEach(function(c) {
    if (all && Object.keys(selectedConvIds).length === CONVERSATIONS.length) {
      selectedConvIds = {};
    } else {
      selectedConvIds[c.conversation_id] = true;
    }
  });
  updateBatchDeleteBtn();
  renderConversationList();
}

function updateBatchDeleteBtn() {
  var n = Object.keys(selectedConvIds).length;
  document.getElementById('btnBatchDelete').textContent = '🗑 批量删除(' + n + ')';
  document.getElementById('btnBatchDelete').disabled = n === 0;
  document.getElementById('btnBatchCopy').textContent = '📋 复制错误信息(' + n + ')';
  document.getElementById('btnBatchCopy').disabled = n === 0;
}

async function batchCopy() {
  var ids = Object.keys(selectedConvIds);
  if (!ids.length) return;
  // 从列表数据中提取选中会话的错误信息
  var lines = [];
  lines.push('# 批量错误信息 (' + ids.length + ' 条会话)\n');
  CONVERSATIONS.forEach(function(c) {
    if (!selectedConvIds[c.conversation_id]) return;
    lines.push('会话: ' + c.conversation_id);
    lines.push('Turn: ' + (c.error_turn_count || '?') + ' 个错误 / ' + c.turn_count + ' 总轮数');
    lines.push('模型: ' + (c.last_client_model || '?') + ' | 后端: ' + (c.last_backend || '?'));
    lines.push('时间: ' + (c.updated_at || '?'));
    if (c.fix_status) {
      var fs = c.fix_status === 'fixed_success' ? '✅修复成功' :
               c.fix_status === 'fixed_failed' ? '❌修复失败' : '⏳待验证';
      lines.push('修复状态: ' + fs);
    }
    lines.push('---');
  });
  var text = lines.join('\n');

  try {
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.left = '-9999px';
    document.body.appendChild(ta); ta.focus(); ta.select();
    document.execCommand('copy'); document.body.removeChild(ta);
    toast('已复制 ' + ids.length + ' 条会话的错误信息');
  } catch(e) {
    toast('复制失败: ' + e.message, false);
  }
}

async function batchDelete() {
  var ids = Object.keys(selectedConvIds);
  console.log('[前端] batchDelete 选中 ' + ids.length + ' 条, ids=' + ids.join(','));
  if (!ids.length) return;
  if (!confirm('确认删除 ' + ids.length + ' 条会话日志？此操作不可撤销。')) return;
  console.log('[前端] batchDelete 确认删除, 发送请求...');
  try {
    var r = await api('/api/admin/logs/batch-delete', {
      method: 'POST',
      body: JSON.stringify({ ids: ids }),
    });
    console.log('[前端] batchDelete 服务器返回:', r);
    toast('已删除 ' + (r.deleted || ids.length) + ' 条');
    exitEditMode();
    loadConversationList();
  } catch (e) {
    toast('批量删除失败: ' + e.message, false);
  }
}

async function loadConversationList() {
  console.time('[前端] 加载会话列表');
  try {
    var t0 = performance.now();
    if (!currentSort) currentSort = { field: 'updated_at', dir: 'desc' };
    var params = '?limit=200&sort=' + currentSort.field + '&dir=' + currentSort.dir + '&_t=' + Date.now();
    var fs = Object.keys(activeFilters).join(',');
    if (fs) params += '&fix_status=' + encodeURIComponent(fs);
    var data = await api('/api/admin/logs' + params);
    var apiMs = (performance.now() - t0).toFixed(0);
    console.log('[前端] API /api/admin/logs 返回 ' + (data.items || []).length + ' 条, 耗时 ' + apiMs + ' ms');

    CONVERSATIONS = data.items || [];
    renderConversationList();
    console.timeEnd('[前端] 加载会话列表');
  } catch (e) {
    console.timeEnd('[前端] 加载会话列表');
    toast('加载列表失败: ' + e.message, false);
    document.getElementById('convList').innerHTML = '<div class="empty">加载失败</div>';
  }
}

function sortConversations(items) {
  if (!currentSort) currentSort = { field: 'updated_at', dir: 'desc' };
  var field = currentSort.field;
  var dir = currentSort.dir;
  return items.slice().sort(function (a, b) {
    var va = a[field], vb = b[field];
    if (va === undefined || va === null) va = '';
    if (vb === undefined || vb === null) vb = '';
    if (field === 'turn_count') {
      va = parseInt(va) || 0; vb = parseInt(vb) || 0;
      return dir === 'asc' ? va - vb : vb - va;
    }
    va = String(va).toLowerCase();
    vb = String(vb).toLowerCase();
    if (va < vb) return dir === 'asc' ? -1 : 1;
    if (va > vb) return dir === 'asc' ? 1 : -1;
    return 0;
  });
}

function renderConversationList() {
  var t0 = performance.now();
  var listEl = document.getElementById('convList');
  var search = (document.getElementById('convSearch').value || '').toLowerCase();

  var filtered = CONVERSATIONS.filter(function (c) {
    if (!search) return true;
    var hay = [c.conversation_id, c.route, c.last_client_model,
               c.last_backend, c.date, c.note].join(' ').toLowerCase();
    return hay.indexOf(search) !== -1;
  });

  var sorted = sortConversations(filtered);

  if (!sorted.length) {
    listEl.innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }

  var html = '';
  sorted.forEach(function (c) {
    var hasErr = c.turn_count > 0 && c.last_client_model === ''; // 近似判断
    var cls = 'conv-item';
    if (c.conversation_id === currentConvId && c.date === currentDate) {
      cls += ' active';
    }
    html += '<div class="' + cls + '" onclick="if(editMode){toggleSelectConv(\'' +
      escAttr(c.conversation_id) + '\');event.stopPropagation();}else{openConversation(\'' +
      escAttr(c.conversation_id) + '\',\'' + escAttr(c.date) + '\')}">';
    html += '<div class="conv-item-top">';
    if (editMode) {
      html += '<input type="checkbox" class="conv-checkbox" ' +
        (selectedConvIds[c.conversation_id] ? 'checked' : '') +
        ' onclick="event.stopPropagation();toggleSelectConv(\'' + escAttr(c.conversation_id) + '\')">';
    }
    html += '<span class="conv-id">' + escHtml(c.conversation_id) + '</span>';
    // 状态标签
    var et = c.error_turn_count || 0;
    if (c.fix_status === 'fixed_success') {
      html += '<span class="conv-badge" style="background:rgba(34,197,94,.15);color:#22c55e">✅已修复</span>';
    } else if (c.fix_status === 'fixed_failed') {
      html += '<span class="conv-badge" style="background:rgba(239,68,68,.15);color:#ef4444">❌修复失效(' + et + 'err)</span>';
    } else if (c.fix_status === 'fixed_pending') {
      html += '<span class="conv-badge" style="background:rgba(234,179,8,.15);color:#eab308">⏳待验证(' + et + 'err)</span>';
    } else if (c.has_error) {
      html += '<span class="conv-badge conv-badge-error">⚠' + et + '错误</span>';
    }
    html += '</div>';
    html += '<div class="conv-item-meta">';
    html += '<span>' + escHtml(c.last_client_model || '?') + '</span>';
    html += '<span>' + escHtml(c.last_backend || '?') + '</span>';
    html += '<span>' + c.turn_count + '轮</span>';
    if (c.has_error && et > 0) {
      html += '<span style="color:var(--red)">' + et + '/' + c.turn_count + '轮有误</span>';
    }
    html += '</div>';
    html += '<div class="conv-item-time">' + formatTime(c.updated_at) + '</div>';
    html += '</div>';
  });
  listEl.innerHTML = html;
  console.log('[前端] 渲染列表 ' + sorted.length + ' 条, 耗时 ' + (performance.now() - t0).toFixed(0) + ' ms');
}

function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function escAttr(s) {
  if (!s) return '';
  return String(s).replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

function formatTime(iso) {
  if (!iso) return '';
  // 2026-06-04T08:33:23.841101Z → 06-04 08:33:23.841
  var m = iso.match(/^[\d-]+T(\d{2}):(\d{2}):(\d{2})\.(\d{3})/);
  if (m) return iso.substring(5, 10) + ' ' + m[1] + ':' + m[2] + ':' + m[3] + '.' + m[4];
  return iso.substring(0, 19).replace('T', ' ');
}

/* ===== 打开会话 ===== */
var currentDoc = null;
var currentTurnIdx = 0;
var contrastOpen = false;

async function openConversation(convId, date) {
  console.time('[前端] 打开会话');
  currentConvId = convId;
  currentDate = date;
  currentTurnIdx = 0;

  // 高亮列表项
  renderConversationList();

  // 显示加载状态
  var viewerContent = document.getElementById('viewerContent');
  var viewerEmpty = document.getElementById('viewerEmpty');
  viewerEmpty.style.display = 'none';
  viewerContent.style.display = 'flex';

  document.getElementById('chatMessages').innerHTML =
    '<div class="empty" style="padding:40px">加载中…</div>';
  document.getElementById('turnBar').innerHTML = '';
  document.getElementById('convMeta').innerHTML = '';

  try {
    var t0 = performance.now();
    // 先用 summary 模式获取整体结构（轻量），同时加载第一个 turn 的详情
    var params = '?turn=0';
    if (date) params += '&date=' + encodeURIComponent(date);
    var data = await api('/api/admin/logs/' + encodeURIComponent(convId) + params);
    var apiMs = (performance.now() - t0).toFixed(0);
    var doc = data.conversation;
    var totalTurns = doc._total_turns || 1;
    console.log('[前端] API /api/admin/logs/' + convId + '?turn=0 总turns=' + totalTurns + ', 耗时 ' + apiMs + ' ms');
    // 补全 turn 数组以便 turn bar 渲染
    currentDoc = doc;
    currentDoc._allTurnCount = totalTurns;
    // 为其他未加载的 turn 创建占位
    while (currentDoc.turns.length < totalTurns) {
      currentDoc.turns.push(null);
    }
    loadTurn(0);
    console.timeEnd('[前端] 打开会话');
  } catch (e) {
    console.timeEnd('[前端] 打开会话');
    document.getElementById('chatMessages').innerHTML =
      '<div class="chat-messages"><div class="chat-msg error"><div class="chat-bubble">加载失败: ' +
      escHtml(e.message) + '</div></div></div>';
  }
}

function loadTurn(idx) {
  var _timerLabel = '[前端] 渲染Turn#' + idx;
  console.time(_timerLabel);
  if (!currentDoc || !currentDoc.turns || !currentDoc.turns.length) {
    document.getElementById('chatMessages').innerHTML =
      '<div class="empty">该会话没有 turn 数据</div>';
    console.timeEnd('[前端] 渲染Turn');
    return;
  }
  var totalTurns = currentDoc._allTurnCount || currentDoc.turns.length;
  if (idx < 0) idx = 0;
  if (idx >= totalTurns) idx = totalTurns - 1;
  currentTurnIdx = idx;
  if (!currentDoc.turns[idx]) {
    document.getElementById('chatMessages').innerHTML =
      '<div class="empty" style="padding:40px">加载 Turn ' + (idx + 1) + '…</div>';
    fetchTurn(idx);
    return;
  }

  var turn = currentDoc.turns[idx];
  renderTurnBar();
  renderConvMeta(turn);
  renderMessages(turn);

  // 滚动到顶部
  document.getElementById('chatViewport').scrollTop = 0;
  console.timeEnd(_timerLabel);
}

async function fetchTurn(idx) {
  var params = '?turn=' + idx;
  if (currentDate) params += '&date=' + encodeURIComponent(currentDate);
  try {
    var t0 = performance.now();
    var data = await api('/api/admin/logs/' + encodeURIComponent(currentConvId) + params);
    var apiMs = (performance.now() - t0).toFixed(0);
    var newDoc = data.conversation;
    var fetchedTurn = newDoc.turns[0];
    currentDoc.turns[idx] = fetchedTurn;
    if (newDoc._total_turns) currentDoc._allTurnCount = newDoc._total_turns;
    console.log('[前端] fetchTurn(' + idx + ') 耗时 ' + apiMs + ' ms');
    loadTurn(idx);
  } catch (e) {
    document.getElementById('chatMessages').innerHTML =
      '<div class="chat-messages"><div class="chat-msg error"><div class="chat-bubble">加载 Turn ' +
      (idx + 1) + ' 失败: ' + escHtml(e.message) + '</div></div></div>';
    console.timeEnd('[前端] 渲染Turn');
  }
}

function renderTurnBar() {
  var totalTurns = currentDoc._allTurnCount || currentDoc.turns.length;
  var html = '';
  for (var i = 0; i < totalTurns; i++) {
    var t = currentDoc.turns[i];
    var cls = 'turn-tab';
    if (i === currentTurnIdx) cls += ' active';
    if (t && t.error) cls += ' turn-error';
    var label = 'Turn ' + (i + 1);
    if (!t) label += ' …';
    else if (t.error) label += ' ⚠';
    html += '<button class="' + cls + '" onclick="loadTurn(' + i + ')">' + label + '</button>';
  }
  document.getElementById('turnBar').innerHTML = html;
}

function renderConvMeta(turn) {
  var html = '';
  html += '<span>客户端: <strong>' + escHtml(turn.client_type || '?') + '</strong></span>';
  var cr = turn.client_request || {};
  if (cr.model) html += '<span>请求模型: <strong>' + escHtml(cr.model) + '</strong></span>';
  html += '<span>后端: <strong>' + escHtml(turn.backend || '?') + '</strong></span>';
  html += '<span>上游: <strong>' + escHtml(turn.upstream_model || '?') + '</strong></span>';
  html += '<span>流式: <strong>' + (turn.stream ? '是' : '否') + '</strong></span>';
  if (cr.user) html += '<span>用户: <strong>' + escHtml(cr.user.substring(0, 40)) + '</strong></span>';
  if (turn.usage) {
    html += '<span>Token: <strong>' +
      (turn.usage.prompt_tokens || 0) + ' in / ' +
      (turn.usage.completion_tokens || 0) + ' out</strong></span>';
  }
  if (turn.duration_ms) {
    html += '<span>耗时: <strong>' + (turn.duration_ms / 1000).toFixed(1) + 's</strong></span>';
  }
  if (turn.error) {
    html += '<span class="meta-err">⚠ 有错误</span>';
  }
  if (turn.matched_fix_id) {
    var fl2 = turn.fix_status === 'fixed_success' ? '✅ 修复生效' :
              turn.fix_status === 'fixed_failed' ? '❌ 修复失效' : '⏳ 修复待验证';
    html += '<span class="timing-info">' + fl2 + ' (规则: ' + escHtml(turn.matched_fix_id) + ')</span>';
  }
  // 耗时统计（可展开详情）
  if (turn.timing) {
    var t = turn.timing;
    var ms = t.total_ms || t.upstream_total_ms || t.upstream_ttfb_ms || 0;
    html += '<details class="timing-details" style="display:inline;font-size:11px">';
    html += '<summary class="timing-summary" style="cursor:pointer;color:var(--yellow);display:inline;margin-left:8px">⏱ ' + ms + 'ms</summary>';
    html += '<div class="timing-popup" style="position:absolute;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 14px;z-index:10;margin-top:4px;font-size:11px;line-height:1.8;white-space:nowrap;box-shadow:0 4px 12px rgba(0,0,0,.3)">';
    if (t.proxy_prepare_ms !== undefined) html += '代理处理: <strong>' + t.proxy_prepare_ms + 'ms</strong><br>';
    if (t.upstream_ttfb_ms) html += '上游TTFB: <strong>' + t.upstream_ttfb_ms + 'ms</strong><br>';
    if (t.upstream_total_ms) html += '上游总耗时: <strong>' + t.upstream_total_ms + 'ms</strong><br>';
    if (t.stream_first_chunk_ms) html += '首chunk到达: <strong>' + t.stream_first_chunk_ms + 'ms</strong><br>';
    if (t.total_ms) html += '端到端总耗时: <strong>' + t.total_ms + 'ms</strong><br>';
    if (t.retries && t.retries > 0) html += '补丁重试: <strong>' + t.retries + '次</strong><br>';
    if (t.attempts && t.attempts > 1) html += '总尝试: <strong>' + t.attempts + '次</strong><br>';
    html += '</div></details>';
  }
  // 客户端 headers（可折叠）
  if (turn.client_headers) {
    html += '<details style="font-size:11px"><summary style="cursor:pointer;color:var(--muted)">📋 请求头</summary>';
    html += '<pre style="font-size:10px;margin:4px 0;white-space:pre-wrap">';
    for (var k in turn.client_headers) {
      html += escHtml(k) + ': ' + escHtml(String(turn.client_headers[k])) + '\n';
    }
    html += '</pre></details>';
  }
  document.getElementById('convMeta').innerHTML = html;
}

/* ===== 消息渲染 ===== */
function renderMessages(turn) {
  var t0 = performance.now();
  var chatEl = document.getElementById('chatMessages');
  try {
  var html = '<div class="chat-messages">';

  // 1. 渲染 client_request.messages
  var msgs = (turn.client_request && turn.client_request.messages) || [];
  console.log('[前端] 开始渲染 ' + msgs.length + ' 条消息...');
  var tBuild = performance.now();
  var foldedCount = 0;
  var totalChars = 0;
  msgs.forEach(function (msg, i) {
    try {
      var c = msg.content;
      if (typeof c === 'string') totalChars += c.length;
      else if (Array.isArray(c)) c.forEach(function(p) { if (p.text) totalChars += p.text.length; });
      html += renderMessageBubble(msg, i);
    } catch(e) {
      console.error('[前端] renderMessageBubble msg[' + i + '] 出错:', e, msg);
    }
  });
  console.log('[前端] HTML构建: ' + (performance.now() - tBuild).toFixed(0) + 'ms, 总字符: ' + totalChars + ', 折叠: ' + (totalChars > 3000 * msgs.length ? '是' : '否'));

  // 2. 如果有非流式响应，渲染
  if (turn.client_response && !turn.stream) {
    html += renderNonStreamResponse(turn.client_response);
  }

  // 3. 错误
  if (turn.error) {
    html += renderErrorBubble(turn.error);
  }

  // 3b. 修复标注（在错误消息下方显示）
  if (turn.matched_fix_id || turn.fix_status) {
    html += renderFixAnnotation(turn);
  }

  // 4. 流式数据直接渲染（服务端已折叠为文本）
  var folded = turn.stream_trace && turn.stream_trace.folded;
  if (folded && (folded.content || folded.reasoning)) {
    html += '<div class="chat-msg assistant">';
    html += '<div class="chat-avatar">🤖</div>';
    html += '<div class="chat-bubble">';
    if (folded.reasoning) {
      html += '<details style="margin-bottom:8px"><summary style="cursor:pointer;color:var(--muted);font-size:12px">💭 思考过程 (' +
        folded.reasoning.length + ' 字符)</summary>';
      html += '<div style="margin-top:4px;color:var(--muted);font-style:italic;white-space:pre-wrap;font-size:12px">' +
        escHtml(folded.reasoning) + '</div></details>';
    }
    if (folded.content) {
      html += renderMarkdown(folded.content);
    }
    if (folded.tool_calls && folded.tool_calls.length > 0) {
      html += '<div class="tool-calls-block">';
      folded.tool_calls.forEach(function(tc) {
        var func = tc.function || {};
        var args = '';
        try { args = JSON.stringify(JSON.parse(func.arguments || '{}'), null, 2); } catch(e) { args = func.arguments || ''; }
        html += '<div class="tool-call-card">';
        html += '<div class="tool-call-header"><span class="tool-call-icon">🔧</span>';
        html += '<span class="tool-call-name">' + escHtml(func.name || 'unknown') + '</span>';
        html += '<span class="tool-call-id">' + escHtml(tc.id || '') + '</span></div>';
        html += '<div class="tool-call-body">' + escHtml(args) + '</div></div>';
      });
      html += '</div>';
    }
    html += '</div></div>';
  }

  // 5. 流式摘要 + 耗时统计
  var metaBlocks = [];
  if (turn.stream && turn.stream_trace && turn.stream_trace.summary) {
    var sum = turn.stream_trace.summary;
    var s = '📊 流式摘要: ' + (sum.chunk_count || sum.event_count || 0) + ' 个片段';
    if (sum.usage) s += ' | Token: ' + (sum.usage.prompt_tokens || 0) + ' in / ' + (sum.usage.completion_tokens || 0) + ' out';
    if (sum.truncated) s += ' | ⚠ 数据被截断';
    metaBlocks.push(s);
  }
  if (turn.timing) {
    var t = turn.timing;
    var parts = [];
    if (t.upstream_ttfb_ms) parts.push('上游TTFB: ' + t.upstream_ttfb_ms + 'ms');
    if (t.upstream_total_ms) parts.push('上游总耗时: ' + t.upstream_total_ms + 'ms');
    if (t.attempts && t.attempts > 1) parts.push('重试' + t.attempts + '次');
    if (parts.length) metaBlocks.push('⏱ 转发耗时: ' + parts.join(' | '));
  }
  if (metaBlocks.length) {
    html += '<div class="chat-msg system" style="margin-top:8px">';
    html += '<div class="chat-bubble" style="max-height:none;font-size:11px;padding:6px 10px">';
    html += metaBlocks.join('<br>');
    html += '</div></div>';
  }

  // 非错误 turn 的修复标注
  if (!turn.error && (turn.matched_fix_id || turn.fix_status)) {
    html += renderFixAnnotation(turn);
  }

  html += '</div>';
  var tDom = performance.now();
  chatEl.innerHTML = html;
  console.log('[前端] DOM插入: ' + (performance.now() - tDom).toFixed(0) + 'ms');

  // 高亮代码块
  var tHL = performance.now();
  if (typeof hljs !== 'undefined') {
    try {
      chatEl.querySelectorAll('pre code').forEach(function (block) {
        hljs.highlightElement(block);
      });
    } catch(e) {}
  }

  // 绑定工具调用的点击展开
  chatEl.querySelectorAll('.tool-call-header').forEach(function (el) {
    el.addEventListener('click', function () {
      this.classList.toggle('open');
      this.nextElementSibling.classList.toggle('open');
    });
  });
  chatEl.querySelectorAll('.tool-result-header').forEach(function (el) {
    el.addEventListener('click', function () {
      this.nextElementSibling.classList.toggle('open');
    });
  });

  // 长消息折叠展开
  chatEl.querySelectorAll('.fold-toggle').forEach(function (btn) {
    btn.addEventListener('click', function() {
      var full = this.nextElementSibling;
      var preview = this.previousElementSibling;
      if (full.style.display === 'none' || !full.style.display) {
        preview.style.display = 'none'; full.style.display = 'block'; this.textContent = '收起';
      } else {
        preview.style.display = 'block'; full.style.display = 'none'; this.textContent = '展开全文';
      }
    });
  });

  console.log('[前端] ═══ 总:' + (performance.now() - t0).toFixed(0) + 'ms 构建:' +
    (tDom - t0).toFixed(0) + 'ms DOM:' + (performance.now() - tDom).toFixed(0) + 'ms ═══');
  } catch(e) {
    console.error('[前端] renderMessages 崩溃:', e);
  }
}

function renderMessageBubble(msg, idx) {
  var role = msg.role || 'unknown';
  var content = msg.content;
  var html = '';

  // System prompt → 折叠
  if (role === 'system' || role === 'developer') {
    var text = extractText(content);
    var short = text.substring(0, 200);
    html += '<div class="chat-msg system">';
    html += '<div class="chat-avatar">⚙</div>';
    html += '<div>';
    html += '<button class="system-toggle" onclick="var b=this.nextElementSibling;var v=b.style.display===\'block\';b.style.display=v?\'none\':\'block\';this.textContent=v?\'展开 System Prompt\':\'收起 System Prompt\'">展开 System Prompt</button>';
    html += '<div class="chat-bubble" style="display:none">' +
      escHtml(text) + '</div>';
    html += '<div style="font-size:11px;color:var(--muted);padding:2px 0">' +
      escHtml(short) + '… (' + text.length + ' 字符)</div>';
    html += '</div></div>';
    return html;
  }

  // Tool 结果
  if (role === 'tool') {
    var resultText = extractText(content);
    var shortResult = resultText.substring(0, 150);
    html += '<div class="chat-msg tool">';
    html += '<div class="chat-avatar">📦</div>';
    html += '<div>';
    html += '<div class="tool-result-card">';
    html += '<div class="tool-result-header">';
    html += '📋 工具结果 <span class="tool-result-id">' +
      escHtml(msg.tool_call_id || '') + '</span>';
    html += '</div>';
    html += '<div class="tool-result-body">' + escHtml(resultText) + '</div>';
    html += '</div>';
    html += '<div style="font-size:11px;color:var(--muted);padding:2px 0">' +
      escHtml(shortResult) + (resultText.length > 150 ? '…' : '') + '</div>';
    html += '</div></div>';
    return html;
  }

  // User & Assistant
  var avatar = role === 'user' ? '👤' : '🤖';
  var cls = 'chat-msg ' + (role === 'user' ? 'user' : 'assistant');
  html += '<div class="' + cls + '">';
  html += '<div class="chat-avatar">' + avatar + '</div>';
  html += '<div style="min-width:0">';

  // 内容
  if (typeof content === 'string') {
    html += '<div class="chat-bubble">' + renderContent(content) + '</div>';
  } else if (Array.isArray(content)) {
    // 多模态内容
    var textParts = [];
    content.forEach(function (part) {
      if (part.type === 'text') {
        textParts.push(part.text || '');
      } else if (part.type === 'image_url') {
        textParts.push('[🖼 图片: ' + (part.image_url && part.image_url.url ?
          part.image_url.url.substring(0, 50) : '?') + '…]');
      } else if (part.type === 'input_audio') {
        textParts.push('[🎤 音频输入]');
      } else if (part.type === 'tool_use') {
        textParts.push('[🔧 工具调用: ' + (part.name || '?') + ']');
      } else if (part.type === 'tool_result') {
        textParts.push('[📋 工具结果]');
      } else {
        textParts.push('[' + (part.type || 'unknown') + ']');
      }
    });
    var joined = textParts.join('\n\n');
    html += '<div class="chat-bubble">' + renderContent(joined) + '</div>';
  } else {
    html += '<div class="chat-bubble">' + escHtml(String(content || '')) + '</div>';
  }

  // 工具调用
  if (msg.tool_calls && msg.tool_calls.length > 0) {
    html += '<div class="tool-calls-block">';
    msg.tool_calls.forEach(function (tc) {
      var func = tc.function || {};
      var args = '';
      try {
        args = JSON.stringify(JSON.parse(func.arguments || '{}'), null, 2);
      } catch (e) {
        args = func.arguments || '';
      }
      html += '<div class="tool-call-card">';
      html += '<div class="tool-call-header">';
      html += '<span class="tool-call-icon">🔧</span>';
      html += '<span class="tool-call-name">' + escHtml(func.name || 'unknown') + '</span>';
      html += '<span class="tool-call-id">' + escHtml(tc.id || '') + '</span>';
      html += '</div>';
      html += '<div class="tool-call-body">' + escHtml(args) + '</div>';
      html += '</div>';
    });
    html += '</div>';
  }

  html += '</div></div>';
  return html;
}

function extractText(content) {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content.map(function (p) {
      if (p.type === 'text') return p.text || '';
      if (p.type === 'image_url') return '[图片]';
      return '[' + (p.type || '?') + ']';
    }).join('');
  }
  if (content === null || content === undefined) return '';
  return String(content);
}

function renderNonStreamResponse(response) {
  if (!response || !response.choices) return '';
  var html = '<div class="chat-msg assistant"><div class="chat-avatar">🤖</div><div class="chat-bubble">';
  response.choices.forEach(function (choice) {
    var msg = choice.message || {};
    if (msg.content) {
      html += renderMarkdown(typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content));
    }
    if (msg.tool_calls) {
      msg.tool_calls.forEach(function (tc) {
        var func = tc.function || {};
        html += '<div class="tool-call-card" style="margin:4px 0"><div class="tool-call-header">';
        html += '🔧 ' + escHtml(func.name || '?');
        html += '</div></div>';
      });
    }
  });
  html += '</div></div>';
  return html;
}

function renderErrorBubble(error) {
  var html = '<div class="chat-msg error"><div class="chat-avatar">⚠️</div>';
  html += '<div class="chat-bubble">';
  html += '<strong>错误</strong>\n\n';
  if (typeof error === 'string') {
    html += escHtml(error);
  } else if (error && typeof error === 'object') {
    html += escHtml('阶段: ' + (error.stage || 'unknown') + '\n');
    html += escHtml('消息: ' + (error.message || JSON.stringify(error)));
  }
  html += '</div></div>';
  return html;
}

// 长消息折叠阈值
var FOLD_LEN = 3000;

function renderContent(text) {
  if (!text) return '';
  if (text.length <= FOLD_LEN) return renderMarkdown(text);
  // 折叠：显示前 800 字符预览
  var preview = text.substring(0, 800);
  var previewHtml = renderMarkdown(preview);
  var fullHtml = renderMarkdown(text);
  var id = 'fold_' + Math.random().toString(36).substr(2, 8);
  return '<div class="fold-preview" id="' + id + '_pre">' + previewHtml + '</div>' +
    '<button class="fold-toggle btn btn-ghost btn-sm" style="margin:4px 0">展开全文 (' + text.length + ' 字符)</button>' +
    '<div class="fold-full" id="' + id + '_full" style="display:none">' + fullHtml + '</div>';
}

function renderMarkdown(text) {
  if (!text) return '';
  // Cursor 消息中包含大量 XML 标签（<user_info>, <agent_skill> 等），
  // marked.js 会将其当作 HTML 吃掉内容。检测到 XML 标签时直接用纯文本渲染。
  var hasXmlTags = /<[a-zA-Z_][a-zA-Z0-9_.-]*(\s[^>]*)?>/.test(text);
  if (hasXmlTags) {
    return '<pre style="white-space:pre-wrap;margin:0;font-family:Consolas,Monaco,monospace;font-size:12px;max-height:400px;overflow-y:auto">' +
      escHtml(text) + '</pre>';
  }
  if (typeof marked !== 'undefined') {
    try {
      var rendered = marked.parse(text);
      return rendered;
    } catch (e) {
      // fall through
    }
  }
  return '<pre style="white-space:pre-wrap;margin:0;font-family:inherit">' +
    escHtml(text) + '</pre>';
}

// ─── 修复标注缓存 ────────────────────────────────
var errorFixesCache = null;
async function loadErrorFixes() {
  if (errorFixesCache) return errorFixesCache;
  try {
    var data = await api('/api/admin/error-fixes');
    errorFixesCache = data.fixes || [];
  } catch(e) {
    errorFixesCache = [];
  }
  return errorFixesCache;
}

function renderFixAnnotation(turn) {
  var fid = turn.matched_fix_id || '';
  var fstatus = turn.fix_status || '';
  if (!fid && !fstatus) return '';

  var html = '<div class="chat-msg system">';
  html += '<div class="chat-avatar">🔧</div>';
  html += '<div class="chat-bubble" style="max-height:none;border-color:var(--yellow);background:rgba(234,179,8,.06)">';

  // 状态
  if (fstatus === 'fixed_success') {
    html += '<strong style="color:var(--green)">✅ 修复生效</strong>';
  } else if (fstatus === 'fixed_failed') {
    html += '<strong style="color:var(--red)">❌ 修复失效</strong>';
  } else if (fstatus === 'fixed_pending') {
    html += '<strong style="color:var(--yellow)">⏳ 修复待验证</strong>';
  } else {
    html += '<strong style="color:var(--muted)">🔧 已匹配修复规则</strong>';
  }

  if (fid) {
    html += '<br><span style="font-size:11px;color:var(--muted)">规则: ' + escHtml(fid) + '</span>';
  }

  // 异步加载修复详情
  html += '<div id="fixDetail_' + escAttr(fid) + '" style="font-size:11px;color:var(--muted);margin-top:4px">加载修复详情…</div>';

  html += '</div></div>';

  // 异步加载
  setTimeout(async function() {
    var fixes = await loadErrorFixes();
    var detail = '';
    for (var i = 0; i < fixes.length; i++) {
      if (fixes[i].id === fid) {
        var f = fixes[i];
        detail = '<strong>问题:</strong> ' + escHtml(f.problem || '?') + '<br>' +
          '<strong>修复:</strong> ' + escHtml(f.fix_description || '?') + '<br>' +
          '<strong>版本:</strong> ' + escHtml(f.fix_version || '?') +
          ' | <strong>状态:</strong> ' + escHtml(f.status || '?');
        break;
      }
    }
    if (!detail) detail = '未找到修复规则详情';
    var el = document.getElementById('fixDetail_' + fid);
    if (el) el.innerHTML = detail;
  }, 100);

  return html;
}

/* ===== 对比视图 ===== */
function toggleContrast() {
  var turn = currentDoc.turns[currentTurnIdx];
  if (!turn) return;

  var panel = document.getElementById('contrastPanel');

  if (contrastOpen) {
    panel.style.display = 'none';
    contrastOpen = false;
    document.getElementById('btnContrast').innerHTML = '△ 对比视图';
    return;
  }

  contrastOpen = true;
  panel.style.display = 'flex';
  document.getElementById('btnContrast').innerHTML = '▽ 收起对比';

  var clientReq = turn.client_request || {};
  var upstreamReq = turn.upstream_request || {};

  document.getElementById('contrastClient').textContent = JSON.stringify(
    { model: clientReq.model, messages: clientReq.messages, tools: clientReq.tools },
    null, 2);
  document.getElementById('contrastUpstream').textContent = JSON.stringify(
    { body: upstreamReq.body, headers: upstreamReq.headers }, null, 2);
}

/* ===== SSE 实时更新 ===== */
function startLiveSSE() {
  if (liveEs) { try { liveEs.close(); } catch (e) {} }

  var url = '/api/admin/logs/live';
  if (authKey) url += '?key=' + encodeURIComponent(authKey);
  liveEs = new EventSource(url);

  liveEs.onmessage = function (e) {
    if (!liveEnabled) return;
    try {
      var evt = JSON.parse(e.data);
      if (evt.type === 'turn_started' || evt.type === 'turn_done') {
        // 有新 turn，刷新列表
        loadConversationList();
      }
    } catch (err) {}
  };

  liveEs.onerror = function () {
    document.getElementById('statusBadge').textContent = '已断开';
    document.getElementById('statusBadge').style.color = 'var(--red)';
  };

  document.getElementById('statusBadge').textContent = '已连接';
  document.getElementById('statusBadge').style.color = 'var(--green)';
}

function toggleLiveUpdates() {
  liveEnabled = !liveEnabled;
  // 只用 class 切换，不用 innerHTML（避免 DOM 引用失效）
  var dot = document.getElementById('liveDot');
  if (dot) {
    if (liveEnabled) {
      dot.classList.add('live-on');
    } else {
      dot.classList.remove('live-on');
    }
  }
}

/* ===== 复制对话引用 ===== */
function copyConversation() {
  if (!currentDoc) { toast('请先选择一条会话', false); return; }
  var turn = currentDoc.turns[currentTurnIdx];
  if (!turn) { toast('当前 turn 数据为空', false); return; }

  var totalTurns = currentDoc._allTurnCount || currentDoc.turns.length;
  var model = turn.client_model || '?';
  var err = turn.error ? ' ⚠有错误' : '';
  var usage = turn.usage ? (' Token: ' + (turn.usage.prompt_tokens || 0) + ' in / ' + (turn.usage.completion_tokens || 0) + ' out') : '';

  var text = '会话: ' + currentConvId + '\n' +
    'Turn: ' + (currentTurnIdx + 1) + '/' + totalTurns + '\n' +
    '模型: ' + model + ' | 后端: ' + (turn.backend || '?') + '\n' +
    '流式: ' + (turn.stream ? '是' : '否') + usage + err;

  try {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.left = '-9999px';
    ta.style.top = '-9999px';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    var ok = document.execCommand('copy');
    document.body.removeChild(ta);
    if (ok) {
      toast('已复制: ' + currentConvId + ' Turn ' + (currentTurnIdx + 1));
    } else {
      toast('复制失败，请手动选择', false);
    }
  } catch(e) {
    toast('复制失败: ' + e.message, false);
  }
}

/* ===== 辅助 ===== */
function scrollToBottom() {
  var viewport = document.getElementById('chatViewport');
  viewport.scrollTop = viewport.scrollHeight;
}
