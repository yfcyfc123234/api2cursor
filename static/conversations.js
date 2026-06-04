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
  const area = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast ' + (ok ? 'toast-ok' : 'toast-err');
  el.textContent = msg;
  area.appendChild(el);
  setTimeout(function () { el.remove(); }, 3000);
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

(function () {
  var saved = sessionStorage.getItem('_ak');
  if (saved) {
    authKey = saved;
    document.getElementById('login').style.display = 'none';
    document.getElementById('dashboard').style.display = 'block';
    loadConversationList();
    startLiveSSE();
  }
})();

/* ===== 会话列表 ===== */
var CONVERSATIONS = [];
var currentSort = { field: 'updated_at', dir: 'desc' };
var currentConvId = null;
var currentDate = null;

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

async function loadConversationList() {
  console.time('[前端] 加载会话列表');
  try {
    var t0 = performance.now();
    var data = await api('/api/admin/logs?limit=200');
    var apiMs = (performance.now() - t0).toFixed(0);
    console.log('[前端] API /api/admin/logs 返回 %d 条, 耗时 %s ms', (data.items || []).length, apiMs);

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
    html += '<div class="' + cls + '" onclick="openConversation(\'' +
      escAttr(c.conversation_id) + '\',\'' + escAttr(c.date) + '\')">';
    html += '<div class="conv-item-top">';
    html += '<span class="conv-id">' + escHtml(c.conversation_id) + '</span>';
    // 检查 turns 是否有 error — 通过后端传来的数据无法直接判断，
    // 这里根据 last_client_model 为空且 route 存在来推断可能有问题
    html += '</div>';
    html += '<div class="conv-item-meta">';
    html += '<span>' + escHtml(c.last_client_model || 'unknown') + '</span>';
    html += '<span>via ' + escHtml(c.last_backend || '?') + '</span>';
    html += '<span>' + c.turn_count + ' 轮</span>';
    if (c.date) html += '<span>' + escHtml(c.date) + '</span>';
    html += '</div>';
    html += '</div>';
  });
  listEl.innerHTML = html;
  console.log('[前端] 渲染列表 %d 条, 耗时 %.0f ms', sorted.length, performance.now() - t0);
}

function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function escAttr(s) {
  if (!s) return '';
  return String(s).replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

/* ===== 打开会话 ===== */
var currentDoc = null;
var currentTurnIdx = 0;
var playbackTimer = null;
var playbackIdx = 0;
var playbackSpeed = 1;
var isPlaying = false;
var contrastOpen = false;

async function openConversation(convId, date) {
  console.time('[前端] 打开会话');
  currentConvId = convId;
  currentDate = date;
  currentTurnIdx = 0;
  stopPlayback();

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
  document.getElementById('playbackBar').style.display = 'none';

  try {
    var t0 = performance.now();
    var params = date ? '?date=' + encodeURIComponent(date) : '';
    var data = await api('/api/admin/logs/' + encodeURIComponent(convId) + params);
    var apiMs = (performance.now() - t0).toFixed(0);
    var doc = data.conversation;
    var turns = doc.turns || [];
    console.log('[前端] API /api/admin/logs/%s 返回%d turns, 耗时 %s ms', convId, turns.length, apiMs);
    currentDoc = doc;
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
  console.time('[前端] 渲染Turn');
  if (!currentDoc || !currentDoc.turns || !currentDoc.turns.length) {
    document.getElementById('chatMessages').innerHTML =
      '<div class="empty">该会话没有 turn 数据</div>';
    console.timeEnd('[前端] 渲染Turn');
    return;
  }
  if (idx < 0) idx = 0;
  if (idx >= currentDoc.turns.length) idx = currentDoc.turns.length - 1;
  currentTurnIdx = idx;
  stopPlayback();

  var turn = currentDoc.turns[idx];
  renderTurnBar();
  renderConvMeta(turn);
  renderMessages(turn);
  setupPlayback(turn);

  // 滚动到顶部
  document.getElementById('chatViewport').scrollTop = 0;
  console.timeEnd('[前端] 渲染Turn');
}

function renderTurnBar() {
  var turns = currentDoc.turns;
  var html = '';
  turns.forEach(function (t, i) {
    var cls = 'turn-tab';
    if (i === currentTurnIdx) cls += ' active';
    if (t.error) cls += ' turn-error';
    var label = 'Turn ' + (i + 1);
    if (t.error) label += ' ⚠';
    html += '<button class="' + cls + '" onclick="loadTurn(' + i + ')">' + label + '</button>';
  });
  document.getElementById('turnBar').innerHTML = html;
}

function renderConvMeta(turn) {
  var html = '';
  html += '<span>模型: <strong>' + escHtml(turn.client_model || '?') + '</strong></span>';
  html += '<span>后端: <strong>' + escHtml(turn.backend || '?') + '</strong></span>';
  html += '<span>上游: <strong>' + escHtml(turn.upstream_model || '?') + '</strong></span>';
  html += '<span>流式: <strong>' + (turn.stream ? '是' : '否') + '</strong></span>';
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
  document.getElementById('convMeta').innerHTML = html;
}

/* ===== 消息渲染 ===== */
function renderMessages(turn) {
  var t0 = performance.now();
  var chatEl = document.getElementById('chatMessages');
  var html = '<div class="chat-messages">';

  // 1. 渲染 client_request.messages
  var msgs = (turn.client_request && turn.client_request.messages) || [];
  msgs.forEach(function (msg, i) {
    html += renderMessageBubble(msg, i);
  });

  // 2. 如果有非流式响应，渲染
  if (turn.client_response && !turn.stream) {
    html += renderNonStreamResponse(turn.client_response);
  }

  // 3. 错误
  if (turn.error) {
    html += renderErrorBubble(turn.error);
  }

  // 4. 流式数据占位（将由播放器填充）
  if (turn.stream && turn.stream_trace && turn.stream_trace.client_events &&
      turn.stream_trace.client_events.length > 0) {
    html += '<div id="streamPlaceholder" class="chat-msg assistant">';
    html += '<div class="chat-avatar">🤖</div>';
    html += '<div class="chat-bubble" id="streamContent" style="min-height:20px">';
    html += '<span class="cursor-blink" id="streamCursor"></span>';
    html += '</div>';
    html += '</div>';
  }

  // 5. 流式摘要
  if (turn.stream && turn.stream_trace && turn.stream_trace.summary) {
    var sum = turn.stream_trace.summary;
    html += '<div class="chat-msg system" style="margin-top:8px">';
    html += '<div class="chat-bubble" style="max-height:none;font-size:11px;padding:6px 10px">';
    html += '📊 流式摘要: ' + (sum.chunk_count || sum.event_count || 0) + ' 个片段';
    if (sum.usage) {
      html += ' | Token: ' + (sum.usage.prompt_tokens || 0) + ' in / ' +
        (sum.usage.completion_tokens || 0) + ' out';
    }
    if (sum.truncated) {
      html += ' | ⚠ 数据被截断（开启 VERBOSE_FULL_STREAM=1 可保留完整数据）';
    }
    html += '</div></div>';
  }

  html += '</div>';
  chatEl.innerHTML = html;

  // 高亮代码块
  if (typeof hljs !== 'undefined') {
    chatEl.querySelectorAll('pre code').forEach(function (block) {
      hljs.highlightElement(block);
    });
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

  var msgs = (turn.client_request && turn.client_request.messages) || [];
  console.log('[前端] 渲染消息 %d 条 (含%s流式), 耗时 %.0f ms',
    msgs.length,
    (turn.stream && turn.stream_trace && turn.stream_trace.client_events &&
     turn.stream_trace.client_events.length > 0) ? '' : '无',
    performance.now() - t0);
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
    html += '<div class="chat-bubble">' + renderMarkdown(content) + '</div>';
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
    html += '<div class="chat-bubble">' + renderMarkdown(textParts.join('\n\n')) + '</div>';
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

function renderMarkdown(text) {
  if (!text) return '';
  if (typeof marked !== 'undefined') {
    try {
      var rendered = marked.parse(text);
      return rendered;
    } catch (e) {
      // fall through
    }
  }
  // Fallback: basic HTML escape + newlines
  return '<pre style="white-space:pre-wrap;margin:0;font-family:inherit">' +
    escHtml(text) + '</pre>';
}

/* ===== 流式回放 ===== */
function setupPlayback(turn) {
  stopPlayback();
  var bar = document.getElementById('playbackBar');
  var trace = turn.stream_trace;

  if (!turn.stream || !trace || !trace.client_events || !trace.client_events.length) {
    bar.style.display = 'none';
    // 隐藏占位光标
    var cursor = document.getElementById('streamCursor');
    if (cursor) cursor.style.display = 'none';
    return;
  }

  bar.style.display = 'flex';
  playbackIdx = 0;
  document.getElementById('playbackFill').style.width = '0%';
  document.getElementById('playbackText').textContent = '0 / ' + trace.client_events.length;
  updatePlayButton();
}

function startPlayback() {
  var turn = currentDoc.turns[currentTurnIdx];
  var trace = turn.stream_trace;
  if (!trace || !trace.client_events || !trace.client_events.length) return;

  isPlaying = true;
  updatePlayButton();
  document.getElementById('streamCursor').style.display = 'inline-block';
  console.log('[前端] 开始流式回放 %d 个事件, 速度=%dx', trace.client_events.length, playbackSpeed);
  playbackStep();
}

function playbackStep() {
  if (!isPlaying) return;

  var turn = currentDoc.turns[currentTurnIdx];
  var trace = turn.stream_trace;
  var events = trace.client_events;

  if (playbackIdx >= events.length) {
    stopPlayback();
    document.getElementById('streamCursor').style.display = 'none';
    return;
  }

  var event = events[playbackIdx];
  appendStreamChunk(event);
  playbackIdx++;

  // 更新进度条
  var pct = (playbackIdx / events.length) * 100;
  document.getElementById('playbackFill').style.width = pct + '%';
  document.getElementById('playbackText').textContent = playbackIdx + ' / ' + events.length;

  // 自动滚动
  var viewport = document.getElementById('chatViewport');
  viewport.scrollTop = viewport.scrollHeight;

  // 根据速度安排下一步
  if (playbackSpeed === 0) {
    // 瞬间完成：使用 requestAnimationFrame 快速处理
    if (playbackIdx < events.length) {
      requestAnimationFrame(playbackStep);
    } else {
      stopPlayback();
      document.getElementById('streamCursor').style.display = 'none';
    }
  } else {
    var delay = Math.round(50 / playbackSpeed);
    playbackTimer = setTimeout(playbackStep, delay);
  }
}

function appendStreamChunk(event) {
  var container = document.getElementById('streamContent');
  if (!container) return;

  // 移除光标再追加
  var cursor = document.getElementById('streamCursor');
  var data = (event && event.data) ? event.data : event;

  // 解析 chunk 数据
  var chunk;
  if (typeof data === 'string') {
    try { chunk = JSON.parse(data); } catch (e) { return; }
  } else {
    chunk = data;
  }

  if (!chunk || !chunk.choices || !chunk.choices.length) return;

  var delta = chunk.choices[0].delta || {};
  var text = '';

  if (delta.reasoning_content) {
    text = '<span style="color:var(--muted);font-style:italic">' +
      escHtml(delta.reasoning_content) + '</span>';
  }
  if (delta.content) {
    text = renderMarkdown(delta.content);
  }
  if (delta.tool_calls) {
    delta.tool_calls.forEach(function (tc) {
      var func = tc.function || {};
      var name = func.name || '?';
      var args = func.arguments || '';
      text += '<div class="tool-call-card" style="margin:4px 0">';
      text += '<div class="tool-call-header">🔧 ' + escHtml(name);
      text += '</div></div>';
    });
  }

  if (text) {
    // 用临时 span 包裹，方便移除旧光标
    var span = document.createElement('span');
    span.innerHTML = text;
    container.insertBefore(span, cursor);
  }
}

function stopPlayback() {
  isPlaying = false;
  if (playbackTimer) {
    clearTimeout(playbackTimer);
    playbackTimer = null;
  }
  updatePlayButton();
}

function togglePlay() {
  if (isPlaying) {
    stopPlayback();
  } else {
    // 如果已经播完，从头开始
    var turn = currentDoc.turns[currentTurnIdx];
    var trace = turn.stream_trace;
    if (trace && trace.client_events && playbackIdx >= trace.client_events.length) {
      playbackIdx = 0;
      document.getElementById('streamContent').innerHTML =
        '<span class="cursor-blink" id="streamCursor"></span>';
      document.getElementById('playbackFill').style.width = '0%';
      document.getElementById('playbackText').textContent = '0 / ' + trace.client_events.length;
    }
    startPlayback();
  }
}

function updatePlayButton() {
  var btn = document.getElementById('btnPlay');
  if (isPlaying) {
    btn.innerHTML = '⏸ 暂停';
  } else {
    btn.innerHTML = '▶ 播放';
  }
}

function setSpeed(speed) {
  playbackSpeed = speed;
  document.querySelectorAll('.speed-btn').forEach(function (b) {
    b.classList.toggle('active', parseInt(b.getAttribute('data-speed')) === speed);
  });
}

function playbackPrev() {
  if (currentTurnIdx > 0) {
    loadTurn(currentTurnIdx - 1);
  }
}

function playbackNext() {
  if (currentDoc && currentTurnIdx < currentDoc.turns.length - 1) {
    loadTurn(currentTurnIdx + 1);
  }
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
  var dot = document.getElementById('liveDot');
  var btn = document.getElementById('liveToggleBtn');
  if (liveEnabled) {
    dot.classList.add('live-on');
    btn.innerHTML = '<span class="live-dot live-on"></span> 实时';
  } else {
    dot.classList.remove('live-on');
    btn.innerHTML = '<span class="live-dot"></span> 实时';
  }
}

/* ===== 辅助 ===== */
function scrollToBottom() {
  var viewport = document.getElementById('chatViewport');
  viewport.scrollTop = viewport.scrollHeight;
}
